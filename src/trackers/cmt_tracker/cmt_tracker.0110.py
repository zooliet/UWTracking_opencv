
import cv2
import numpy as np
import imutils
from utils import util

import itertools
import scipy.spatial
import scipy.cluster


class CMTTracker:
    THR_OUTLIER = 20
    THR_CONF = 0.75
    THR_RATIO = 0.8
    DESC_LENGTH = 512
    MIN_NUM_OF_KEYPOINTS_FOR_BRISK_THRESHOLD = 100 # 900
    PREV_HISTORY_SIZE = 100

    def __init__(self, scale, rotation, cmt_detector_threshold = 70, best_effort = False):
        self.estimate_scale = scale
        self.estimate_rotation = rotation
        self.best_effort = best_effort

        self.detector = cv2.BRISK_create(cmt_detector_threshold, 3, 3.0)
        self.descriptor = self.detector
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)

    def init(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)

        (self.tl, self.br) = ((self.x1, self.y1), (self.x2, self.y2))
        self.cX = self.x1 + (self.x2 - self.x1) // 2
        self.cY = self.y1 + (self.y2 - self.y1) // 2

        self.adjust_flag = False
        self.force_init_flag = False
        self.has_result = False
        self.frame_idx = 0
        self.prev_scale_estimate = 1.0

        self.mean_width = self.x2 - self.x1
        self.mean_height = self.y2 - self.y1
        self.mean_center = (self.cX, self.cY)

        self.prev_widths = np.array([self.mean_width], dtype=np.int16)
        self.prev_heights = np.array([self.mean_height], dtype=np.int16)
        self.prev_centers = np.array([self.mean_center])

        self.num_of_failure = 0

        # Get initial keypoints in whole image
        keypoints_cv = self.detector.detect(gray)
        # Remember keypoints that are in the rectangle as selected keypoints
        if len(keypoints_cv) == 0:
            # print("[CMT] No keypoints found somehow")
            num_selected_keypoints = 0
        else:
            ind = util.in_rect(keypoints_cv, self.tl, self.br)

            selected_keypoints_cv = list(itertools.compress(keypoints_cv, ind))
            background_keypoints_cv = list(itertools.compress(keypoints_cv, ~ind))
            selected_keypoints_cv, self.selected_features = self.descriptor.compute(gray, selected_keypoints_cv)
            selected_keypoints = util.keypoints_cv_to_np(selected_keypoints_cv)
            num_selected_keypoints = len(selected_keypoints_cv)
            # print("[CMT] num_selected_keypoints is {}".format(num_selected_keypoints))
            # print("[CMT] num_background_keypoints is {}".format(len(background_keypoints_cv)))

        if num_selected_keypoints != 0:
            # Remember keypoints that are not in the rectangle as background keypoints
            # background_keypoints_cv = list(itertools.compress(keypoints_cv, ~ind))
            # print("[CMT] num_background_keypoints is {}".format(len(background_keypoints_cv)))
            background_keypoints_cv, background_features = self.descriptor.compute(gray, background_keypoints_cv)
            _ = util.keypoints_cv_to_np(background_keypoints_cv)

            # Assign each keypoint a class starting from 1, background is 0
            self.selected_classes = np.array(range(num_selected_keypoints)) + 1
            background_classes = np.zeros(len(background_keypoints_cv))

            # Stack background features and selected features into database
            if len(background_keypoints_cv) > 0:
                self.features_database = np.vstack((background_features, self.selected_features))
            else:
                self.features_database = self.selected_features

            # Same for classes
            self.classes_database = np.hstack((background_classes, self.selected_classes))

            # Get all distances between selected keypoints in squareform
            pdist = scipy.spatial.distance.pdist(selected_keypoints)
            self.squareform = scipy.spatial.distance.squareform(pdist)

            # Get all angles between selected keypoints
            angles = np.empty((num_selected_keypoints, num_selected_keypoints))
            for k1, i1 in zip(selected_keypoints, range(num_selected_keypoints)):
                for k2, i2 in zip(selected_keypoints, range(num_selected_keypoints)):
                    # Compute vector from k1 to k2
                    v = k2 - k1
                    # Compute angle of this vector with respect to x axis
                    angle = np.math.atan2(v[1], v[0])
                    # Store angle
                    angles[i1, i2] = angle
            self.angles = angles

            # Find the center of selected keypoints
            center = np.mean(selected_keypoints, axis=0)

            # Remember the rectangle coordinates relative to the center
            self.center_to_tl = np.array(self.tl) - center
            self.center_to_tr = np.array([self.br[0], self.tl[1]]) - center
            self.center_to_br = np.array(self.br) - center
            self.center_to_bl = np.array([self.tl[0], self.br[1]]) - center

            # Calculate springs of each keypoint
            self.springs = selected_keypoints - center

            # Set start image for tracking
            self.gray0 = gray

            # Make keypoints 'active' keypoints
            self.active_keypoints = np.copy(selected_keypoints)

            # Attach class information to active keypoints
            self.active_keypoints = np.hstack((selected_keypoints, self.selected_classes[:, None]))

            # Remember number of initial keypoints
            self.num_initial_keypoints = len(selected_keypoints_cv)
        else:
            self.num_initial_keypoints = 0

    def update(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)

        tracked_keypoints, _, self.removeClasses = self.track(gray)

        if self.removeClasses.size > 0:
            if np.all(np.in1d(self.removeClasses, self.selected_classes)) and np.all(np.in1d(self.removeClasses, self.classes_database)):
                sorted_idx = np.argsort(self.selected_classes)
                idx = sorted_idx[np.searchsorted(self.selected_classes[sorted_idx], self.removeClasses)]
                self.selected_classes[idx] = 0

                sorted_idx = np.argsort(self.classes_database)
                idx = sorted_idx[np.searchsorted(self.classes_database[sorted_idx], self.removeClasses)]
                self.classes_database[idx] = 0
            # print('[CMT] self.selected_classes:', self.selected_classes)
            # print('[CMT] self.classes_database:', self.classes_database)

        (center, scale_estimate, rotation_estimate, tracked_keypoints) = self.estimate(tracked_keypoints)

        # Detect keypoints, compute descriptors
        keypoints_cv = self.detector.detect(gray)
        keypoints_cv, features = self.descriptor.compute(gray, keypoints_cv)

        # Create list of active keypoints
        active_keypoints = np.zeros((0, 3))

        # Get the best two matches for each feature
        matches_all = self.matcher.knnMatch(features, self.features_database, 2)
        # Get all matches for selected features
        if not any(np.isnan(center)):
            selected_matches_all = self.matcher.knnMatch(features, self.selected_features, len(self.selected_features))

        # For each keypoint and its descriptor
        if len(keypoints_cv) > 0:
            transformed_springs = scale_estimate * util.rotate(self.springs, -rotation_estimate)
            for i in range(len(keypoints_cv)):
                # Retrieve keypoint location
                location = np.array(keypoints_cv[i].pt)

                # First: Match over whole image
                # Compute distances to all descriptors
                matches = matches_all[i]
                distances = np.array([m.distance for m in matches])

                # Convert distances to confidences, do not weight
                combined = 1 - distances / self.DESC_LENGTH
                classes = self.classes_database

                # Get best and second best index
                try:
                    bestInd = matches[0].trainIdx
                    secondBestInd = matches[1].trainIdx
                except Exception as e:
                    print(len(matches))

                # Compute distance ratio according to Lowe
                try:
                    ratio = (1 - combined[0]) / (1 - combined[1])
                except:
                    print(combined)
                # print('[CMT] Pts {}: Distance {} => combined {}: ratio{}'.format(location, distances, combined, ratio))

                # Extract class of best match
                keypoint_class = classes[bestInd]
                # print("[CMT] {}: Class[{}] => {}, Class[{}] => {}".format(i, bestInd, keypoint_class, secondBestInd, classes[secondBestInd]))
                # print('[CMT] {}: Pts {}: Distance {} => combined {}: ratio: {}'.format(i, location, distances, combined, ratio))

                # If distance ratio is ok and absolute distance is ok and keypoint class is not background
                if ratio < self.THR_RATIO and combined[0] > self.THR_CONF and keypoint_class != 0:
                    # print('[CMT] {}: {}: combined {}: ratio: {}, Class[{}] => {}'.format(i, location, combined[0], ratio, bestInd, keypoint_class))
                    # Add keypoint to active keypoints
                    new_kpt = np.append(location, keypoint_class)
                    active_keypoints = np.append(active_keypoints, np.array([new_kpt]), axis=0)

                # In a second step, try to match difficult keypoints
                # If structural constraints are applicable
                if not any(np.isnan(center)):
                    # Compute distances to initial descriptors
                    matches = selected_matches_all[i]
                    distances = np.array([m.distance for m in matches])

                    # Re-order the distances based on indexing
                    idxs = np.argsort(np.array([m.trainIdx for m in matches]))
                    distances = distances[idxs]

                    # Convert distances to confidences
                    confidences = 1 - distances / self.DESC_LENGTH

                    # Compute the keypoint location relative to the object center
                    relative_location = location - center

                    # Compute the distances to all springs
                    displacements = util.L2norm(transformed_springs - relative_location)

                    # For each spring, calculate weight
                    weight = displacements < self.THR_OUTLIER  # Could be smooth function

                    combined = weight * confidences

                    classes = self.selected_classes

                    # Sort in descending order
                    sorted_conf = np.argsort(combined)[::-1]  # reverse

                    # Get best and second best index
                    bestInd = sorted_conf[0]
                    secondBestInd = sorted_conf[1]

                    # Compute distance ratio according to Lowe
                    ratio = (1 - combined[bestInd]) / (1 - combined[secondBestInd])

                    # Extract class of best match
                    keypoint_class = classes[bestInd]

                    # If distance ratio is ok and absolute distance is ok and keypoint class is not background
                    if ratio < self.THR_RATIO and combined[bestInd] > self.THR_CONF and keypoint_class != 0:
                        # Add keypoint to active keypoints
                        new_kpt = np.append(location, keypoint_class)

                        # Check whether same class already exists
                        if active_keypoints.size > 0:
                            same_class = np.nonzero(active_keypoints[:, 2] == keypoint_class)
                            active_keypoints = np.delete(active_keypoints, same_class, axis=0)

                        active_keypoints = np.append(active_keypoints, np.array([new_kpt]), axis=0)

        # If some keypoints have been tracked
        if tracked_keypoints.size > 0:
            # Extract the keypoint classes
            tracked_classes = tracked_keypoints[:, 2]

            # If there already are some active keypoints
            if active_keypoints.size > 0:
                # Add all tracked keypoints that have not been matched
                associated_classes = active_keypoints[:, 2]
                missing = ~np.in1d(tracked_classes, associated_classes)
                active_keypoints = np.append(active_keypoints, tracked_keypoints[missing, :], axis=0)
            else: # Else use all tracked keypoints
                active_keypoints = tracked_keypoints

        if self.removeClasses.size > 0:
            adjusted_center = np.mean(active_keypoints[:, :2], axis=0)
            if np.sqrt(np.power(center - adjusted_center, 2).sum()) > 10: # need to adjust
                self.adjust_flag = True
                center = adjusted_center
                # print("[CMT] Adjust center, springs and center_to_tl(tr, br, bl)")

        self.center = center
        self.scale_estimate = scale_estimate
        self.rotation_estimate = rotation_estimate
        self.tracked_keypoints = tracked_keypoints
        self.active_keypoints = active_keypoints
        self.gray0 = gray
        self.frame_idx += 1

        if not any(np.isnan(self.center)): # and self.active_keypoints.shape[0] > self.num_initial_keypoints / 10:
            self.has_result = True

            tl = util.array_to_int_tuple(center + scale_estimate * util.rotate(self.center_to_tl[None, :], rotation_estimate).squeeze())
            tr = util.array_to_int_tuple(center + scale_estimate * util.rotate(self.center_to_tr[None, :], rotation_estimate).squeeze())
            br = util.array_to_int_tuple(center + scale_estimate * util.rotate(self.center_to_br[None, :], rotation_estimate).squeeze())
            bl = util.array_to_int_tuple(center + scale_estimate * util.rotate(self.center_to_bl[None, :], rotation_estimate).squeeze())

            self.tl = tl
            self.tr = tr
            self.bl = bl
            self.br = br
        else:
            self.has_result = False

    def track(self, gray, THR_FB=20, tl=(0,0), br=(0, 0)):
        num_keypoints = self.active_keypoints.shape[0]
        status = [False] * num_keypoints
        wrong_selected = np.zeros(0)

        # If at least one keypoint is active
        if num_keypoints > 0:
            # Prepare data for opencv:
            # Add singleton dimension
            # Use only first and second column
            # Make sure dtype is float32
            pts = self.active_keypoints[:, None, :2].astype(np.float32)

            # Calculate forward optical flow for prev_location
            nextPts, status, _ = cv2.calcOpticalFlowPyrLK(self.gray0, gray, pts, None)

            # Calculate backward optical flow for prev_location
            pts_back, _, _ = cv2.calcOpticalFlowPyrLK(gray, self.gray0, nextPts, None)

            # Remove singleton dimension
            # pts: (m, 1, 2) => (m, 2)
            # pts_back: (m, 1, 2) => (m, 2)
            # nextPts: (m, 1, 2) => (m, 2)
            # status: (m, 1) => (m,)

            pts_back = util.squeeze_pts(pts_back)
            pts = util.squeeze_pts(pts)
            nextPts = util.squeeze_pts(nextPts)
            status = status.squeeze()

            # Calculate forward-backward error
            fb_err = np.sqrt(np.power(pts_back - pts, 2).sum(axis=1))

            # Set status depending on fb_err and lk error
            large_fb = fb_err > THR_FB
            status = ~large_fb & status.astype(np.bool)

            nextPts = nextPts[status, :]
            keypoints_tracked = self.active_keypoints[status, :]
            keypoints_tracked[:, :2] = nextPts
            pts = pts[status, :]

            dists = np.sqrt(np.power(nextPts - pts, 2).sum(axis=1))
            num_of_dists = len(dists)

            if num_of_dists > 10:
                dists_mean = np.mean(np.sort(dists)[-10:])
            elif num_of_dists > 4:
                dists_mean = np.mean(np.sort(dists)[-4:])
            else:
                dists_mean = np.mean(dists)
            # print("[CMT] {} => {}".format(dists, dists_mean))

            if self.best_effort is True:
                DISPLAMENT = 0.3
                TAGET_CRITERIA = 0.1

                if dists_mean > DISPLAMENT:
                    conditions = dists < TAGET_CRITERIA
                    # print("[CMT] 배제될 조건:", conditions)
                    wrong_selected = keypoints_tracked[conditions, 2]

                    if wrong_selected.size > 0:
                        # print("[CMT] wrong_selected:", wrong_selected)
                        conditions = dists >= TAGET_CRITERIA
                        # print("[CMT] 살아남을 조건:", conditions)
                        # print('[CMT] keypoints_tracked(by classes):', keypoints_tracked[:, 2])
                        keypoints_tracked = keypoints_tracked[conditions, :]
                        # print('[CMT] keypoints_tracked(by classes) after filter:', keypoints_tracked[:, 2])

        else:
            keypoints_tracked = np.zeros((0,3))

        return keypoints_tracked, status, wrong_selected

    def estimate(self, keypoints):
        center = np.array((np.nan, np.nan))
        scale_estimate = np.nan
        med_rot = np.nan

        # At least 2 keypoints are needed for scale
        if len(keypoints) > 1:
            # Extract the keypoint classes
            keypoint_classes = keypoints[:, 2].squeeze().astype(np.int)
            # print("[CMT]", keypoint_classes.shape, keypoint_classes)

            # Retain singular dimension
            if keypoint_classes.size == 1:
                keypoint_classes = keypoint_classes[None]

            # Sort
            ind_sort = np.argsort(keypoint_classes)
            keypoints = keypoints[ind_sort]
            keypoint_classes = keypoint_classes[ind_sort]

            # Get all combinations of keypoints
            all_combs = np.array([val for val in itertools.product(range(keypoints.shape[0]), repeat=2)])

            # But exclude comparison with itself
            all_combs = all_combs[all_combs[:, 0] != all_combs[:, 1], :]

            # Measure distance between allcombs[0] and allcombs[1]
            ind1 = all_combs[:, 0]
            ind2 = all_combs[:, 1]

            class_ind1 = keypoint_classes[ind1] - 1
            class_ind2 = keypoint_classes[ind2] - 1

            duplicate_classes = class_ind1 == class_ind2

            if not all(duplicate_classes):
                ind1 = ind1[~duplicate_classes]
                ind2 = ind2[~duplicate_classes]

                class_ind1 = class_ind1[~duplicate_classes]
                class_ind2 = class_ind2[~duplicate_classes]

                pts_allcombs0 = keypoints[ind1, :2]
                pts_allcombs1 = keypoints[ind2, :2]

                # This distance might be 0 for some combinations,
                # as it can happen that there is more than one keypoint at a single location
                dists = util.L2norm(pts_allcombs0 - pts_allcombs1)

                original_dists = self.squareform[class_ind1, class_ind2]
                # print(np.isfinite(original_dists).all())
                scalechange = dists / original_dists

                # Compute angles
                angles = np.empty((pts_allcombs0.shape[0]))

                v = pts_allcombs1 - pts_allcombs0
                angles = np.arctan2(v[:, 1], v[:, 0])

                original_angles = self.angles[class_ind1, class_ind2]
                angle_diffs = angles - original_angles

                # # Fix long way angles
                long_way_angles = np.abs(angle_diffs) > np.math.pi

                angle_diffs[long_way_angles] = angle_diffs[long_way_angles] - np.sign(angle_diffs[long_way_angles]) * 2 * np.math.pi

                scale_estimate = np.median(scalechange)
                if not self.estimate_scale:
                    scale_estimate = 1;

                med_rot = np.median(angle_diffs)
                if not self.estimate_rotation:
                    med_rot = 0;

                keypoint_class = keypoints[:, 2].astype(np.int)
                votes = keypoints[:, :2] - scale_estimate * (util.rotate(self.springs[keypoint_class - 1], med_rot))
                # votes = keypoints[:, :2] - scale_estimate * self.springs[keypoint_class - 1]

                # Remember all votes including outliers
                self.votes = votes

                # Compute pairwise distance between votes
                pdist = scipy.spatial.distance.pdist(votes)

                # Compute linkage between pairwise distances
                linkage = scipy.cluster.hierarchy.linkage(pdist)

				# Perform hierarchical distance-based clustering
                T = scipy.cluster.hierarchy.fcluster(linkage, self.THR_OUTLIER, criterion='distance')

                # Count votes for each cluster
                cnt = np.bincount(T)  # Dummy 0 label remains

                # Get largest class
                Cmax = np.argmax(cnt)

                # Identify inliers (=members of largest class)
                inliers = T == Cmax
                # inliers = med_dists < THR_OUTLIER

                # Remember outliers
                self.outliers = keypoints[~inliers, :]

                # Stop tracking outliers
                keypoints = keypoints[inliers, :]

                # Remove outlier votes
                votes = votes[inliers, :]

                # Compute object center
                center = np.mean(votes, axis=0)
            else:
                self.num_of_failure += 1
                # print("[CMT] All are duplicate.")
        else:
            self.num_of_failure += 1
            # print("[CMT] At least 2 Keypoints are required for estimation.")

        return (center, scale_estimate, med_rot, keypoints)
