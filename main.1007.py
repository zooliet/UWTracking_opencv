# import the necessary packages

from __future__ import print_function
import sys
PY3 = sys.version_info[0] == 3
if PY3:
    xrange = range

import cv2
import numpy as np
import imutils

from trackers.color_tracker import ColorTracker
from trackers.kcf_tracker import KCFTracker
from trackers.cmt_tracker import CMTTracker
from motor import Motor

import math
from threading import Timer
import time
import datetime

from utils import common
from utils import util

import os
import glob
import re

# from numpy import isnan
# import redis
# import json

# construct the argument parse and parse the arguments
import argparse

ap = argparse.ArgumentParser()
ap.add_argument("--camera", type=int, default=0, help = "camera number")
ap.add_argument("-p", "--path", help = "path to video file")
ap.add_argument("-n", "--num-frames", type=int, default=10000000, help="# of frames to loop over")
ap.add_argument("-d", "--display", action="store_true", help="Show display")
ap.add_argument("-s", "--serial", help = "path to serial device")
ap.add_argument("-z", "--zoom", help = "path to zoom control port")

ap.add_argument("--color", action="store_true", help="Enable color tracking")
ap.add_argument("--kcf", action="store_true", help="Enable KCF tracking")
ap.add_argument("--cmt", action="store_true", help="Enable CMT tracking")
ap.add_argument("--cmt-alone", action="store_true", help="Enable CMT tracking in best effort mode")
ap.add_argument("--autozoom", action="store_true", help="Enable automatic zoom control")

args = vars(ap.parse_args())
# print("[INFO] Command: ", args)

WIDTH       = 640  # 640x360, 1024x576, 1280x720, 1920x1080
HEIGHT      = WIDTH * 9 // 16
HALF_WIDTH  = WIDTH // 2
HALF_HEIGHT = HEIGHT // 2

MIN_SELECTION_WIDTH  = 16 # or 20, 10
MIN_SELECTION_HEIGHT = 9 # or 20, 10

if args['path']:
    stream = cv2.VideoCapture(args['path'])
else:
    stream = cv2.VideoCapture(args['camera'])

stream.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
stream.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)

grabbed, frame = stream.read()
frame = imutils.resize(frame, width=WIDTH)
prev_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

pause_flag = False
tracking_processing_flag = False
capture = None
tracking_window = {'x1': -1, 'y1': -1, 'x2': -1, 'y2': -1, 'dragging': False, 'start': False}
show_lap_time_flag = False

def onmouse(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        param['dragging'] = True
        param['x1'] = x
        param['y1'] = y
        param['x2'] = x
        param['y2'] = y

    elif event == cv2.EVENT_MOUSEMOVE:
        if param['dragging'] == True:
            param['x2'] = x
            param['y2'] = y

    elif event == cv2.EVENT_LBUTTONUP:
        if param['dragging'] == True:
            xmin = min(param['x1'], param['x2'])
            ymin = min(param['y1'], param['y2'])
            xmax = max(param['x1'], param['x2'])
            ymax = max(param['y1'], param['y2'])

            param['x1'] = xmin
            param['y1'] = ymin
            param['x2'] = xmax
            param['y2'] = ymax
            # print("[MOUSE]", xmin, xmax, ymin, ymax)
            param['start'] = True
            param['dragging'] = False

if args["display"] is True:
    cv2.namedWindow('Tracking')
    cv2.setMouseCallback('Tracking', onmouse, tracking_window)

if args['serial']:
    motor = Motor(dev = args['serial'], baud = 115200, screen_width = WIDTH)
else:
    motor = None

if args['zoom']:
    zoom = Motor(dev = args['zoom'], baud = 115200, screen_width = WIDTH)
    zoom.zoom_x1()
else:
    zoom = None

if args['color'] is True:
    color_tracker = ColorTracker()
else:
    color_tracker = None

if args['kcf'] is True:
    kcf_tracker = KCFTracker(True, False, True) # hog, fixed_window, multiscale
else:
    kcf_tracker = None

if args['cmt'] is True:
    cmt_tracker = CMTTracker(True, False, cmt_detector_threshold = 50, best_effort = False) # estimate_scale, estimate_rotation
elif args['cmt_alone'] is True:
    cmt_tracker = CMTTracker(True, False, cmt_detector_threshold = 50, best_effort = True) # estimate_scale, estimate_rotation
else:
    cmt_tracker = None

if cmt_tracker:
    gray0 = cv2.GaussianBlur(prev_gray, (3, 3), 0)

    for x in range(10, 500, 10):
        detector = cv2.BRISK_create(x, 3, 3.0)
        keypoints = detector.detect(gray0)
        cmt_detector_threshold = x
        if len(keypoints) < cmt_tracker.MIN_NUM_OF_KEYPOINTS_FOR_BRISK_THRESHOLD:
            break
    print("[CMT] BRISK threshold is set to {} with {} keypoints".format(x, len(keypoints)))
    cmt_tracker.detector = detector
    cmt_tracker.descriptor = detector

tic = time.time()
toc = time.time()

while True:
    if pause_flag is False:
        grabbed, frame = stream.read()
        if grabbed is not True:
            # print("End of Frame")
            break

        frame = imutils.resize(frame, width=WIDTH)

    if pause_flag is not True:
        if tracking_window['start'] == True:
            if((tracking_window['x2'] - tracking_window['x1']) > MIN_SELECTION_WIDTH) and ((tracking_window['y2'] - tracking_window['y1']) > MIN_SELECTION_HEIGHT):
                selected_width = tracking_window['x2'] - tracking_window['x1']
                selected_height = tracking_window['y2'] - tracking_window['y1']
                if zoom:
                    selected_width = int(selected_width / zoom.current_zoom)
                    selected_height = int(selected_height / zoom.current_zoom)

                print("[KCF] User selected width {} and height {}".format(selected_width, selected_height) )


                if color_tracker:
                    if color_tracker.init(frame, options = tracking_window):
                        print('[COLOR] Color Found at {}'.format(color_tracker.center))
                    else:
                        print('[COLOR] Color Not Found around at {}'.format(color_tracker.center))
                    tracking_processing_flag = True # 초기화 결과에 상관없이 tracking 시작

                if cmt_tracker:
                    cmt_tracker.x1 = tracking_window['x1']
                    cmt_tracker.y1 = tracking_window['y1']
                    cmt_tracker.x2 = tracking_window['x2']
                    cmt_tracker.y2 = tracking_window['y2']

                    cmt_tracker.init(frame)
                    if cmt_tracker.num_initial_keypoints == 0:
                        print('[CMT] No keypoints found in selection')
                        if tracking_processing_flag == True: # reinitialize case
                            tracking_window['start'] = True # 강제로 초기화를 다시하는 효과
                        else:
                            tracking_processing_flag = False
                    else:
                        # print("[CMT] num_selected_keypoints is {}".format(cmt_tracker.num_initial_keypoints))
                        tracking_processing_flag = True

                if kcf_tracker:
                    kcf_tracker.x1 = tracking_window['x1']
                    kcf_tracker.y1 = tracking_window['y1']
                    kcf_tracker.x2 = tracking_window['x2']
                    kcf_tracker.y2 = tracking_window['y2']

                    #if you use hog feature, there will be a short pause after you draw a first boundingbox, that is due to the use of Numba.
                    kcf_tracker.init(frame)
                    tracking_processing_flag = True # 초기화 결과에 상관없이 tracking 시작

            elif motor and motor.is_moving is not True:
                centerX = (tracking_window['x1'] + tracking_window['x2']) // 2
                centerY = (tracking_window['y1'] + tracking_window['y2']) // 2
                center_to_x = HALF_WIDTH - centerX
                center_to_y = centerY - HALF_HEIGHT
                if zoom is None:
                    motor.move_to(center_to_x, center_to_y)
                elif zoom.is_zooming is not True:
                    motor.move_to(center_to_x, center_to_y, zoom.current_zoom)

            capture = None
            tracking_window['start'] = False

        if tracking_processing_flag is True:
            if show_lap_time_flag is True: # 'l' key
                current_time = datetime.datetime.now().time().isoformat()
                toc = time.time()
                print("[INFO] Tracking duration: {:04.0f} ms @{}".format(1000*(toc-tic), current_time))
                tic = toc

            if color_tracker:
                if kcf_tracker and kcf_tracker.enable:
                    color_tracker.update(frame,  {'x1': kcf_tracker.x1, 'y1':kcf_tracker.y1, 'x2': kcf_tracker.x2, 'y2': kcf_tracker.y2})
                else:
                    color_tracker.update(frame)

            if cmt_tracker:
                if cmt_tracker.force_init_flag is True:
                    # print('[CMT]: Force init')
                    cmt_tracker.force_init_flag = False
                    cmt_tracker.init(frame)

                    if cmt_tracker.num_initial_keypoints == 0:
                        print('[CMT] No keypoints found in selection for ({},{}), ({},{})'.format(cmt_tracker.x1, cmt_tracker.y1, cmt_tracker.x2, cmt_tracker.y2))
                        cmt_tracker.force_init_flag = True
                        if kcf_tracker:
                            cmt_tracker.x1 = kcf_tracker.x1
                            cmt_tracker.x2 = kcf_tracker.x2
                            cmt_tracker.y1 = kcf_tracker.y1
                            cmt_tracker.y2 = kcf_tracker.y2
                    # else:
                    #     print("[CMT] num_selected_keypoints is {}".format(cmt_tracker.num_initial_keypoints))

                else:
                    cmt_tracker.update(frame)
                    if cmt_tracker.best_effort is not True and cmt_tracker.tracked_keypoints.shape[0] < 10: # or cmt_tracker.active_keypoints.shape[0] < 10
                        cmt_tracker.has_result = False

                    if cmt_tracker.num_of_failure > 0 and cmt_tracker.best_effort is True:
                        # print("[CMT] fail count: ", cmt_tracker.num_of_failure)
                        cmt_tracker.force_init_flag = True

                    if cmt_tracker.has_result:
                        num_of_tracked_keypoints = len(cmt_tracker.tracked_keypoints)
                        cmt_tracker.cX = int(cmt_tracker.center[0])
                        cmt_tracker.cY = int(cmt_tracker.center[1])

                        box_tl = cmt_tracker.tl
                        box_br = cmt_tracker.br

                        scale_change = cmt_tracker.scale_estimate/cmt_tracker.prev_scale_estimate
                        cmt_tracker.prev_scale_estimate = cmt_tracker.scale_estimate
                        # print("[CMT] {}. Tracked(inlier): {}, Outliers: {}, Votes: {}: Active: {}, Scale: {:02.2f}({:01.2f})"
                        #     .format(cmt_tracker.frame_idx, num_of_tracked_keypoints, len(cmt_tracker.outliers), len(cmt_tracker.votes), len(cmt_tracker.active_keypoints), cmt_tracker.scale_estimate, scale_change))

                        if cmt_tracker.best_effort is True:
                            if cmt_tracker.adjust_flag is True:
                                cmt_tracker.adjust_flag = False

                                if cmt_tracker.frame_idx < 10:
                                    # print("[CMT] Adjust center around keypoints at frame {}".format(cmt_tracker.frame_idx))
                                    # pause_flag = True
                                    cmt_tracker.force_init_flag = True

                                    width = int((cmt_tracker.br[0] - cmt_tracker.tl[0]) * 0.9)
                                    height = int((cmt_tracker.br[1] - cmt_tracker.tl[1]) * 0.9)

                                    box_tl = (int(cmt_tracker.cX - width / 2), int(cmt_tracker.cY - height / 2))
                                    box_br = (int(cmt_tracker.cX + width / 2), int(cmt_tracker.cY + height / 2))

                                elif cmt_tracker.frame_idx >= 10:
                                    # print("[CMT] Adjust center around previous area at frame {}".format(cmt_tracker.frame_idx))
                                    # pause_flag = True
                                    cmt_tracker.force_init_flag = True

                                    # box_tl = (int(box_center[0] - cmt_tracker.mean_width / 2), int(box_center[1] - cmt_tracker.mean_height / 2))
                                    # box_br = (int(box_center[0] + cmt_tracker.mean_width / 2), int(box_center[1] + cmt_tracker.mean_height / 2))
                                    box_tl = (int(cmt_tracker.cX - cmt_tracker.mean_width / 2), int(cmt_tracker.cY - cmt_tracker.mean_height / 2))
                                    box_br = (int(cmt_tracker.cX + cmt_tracker.mean_width / 2), int(cmt_tracker.cY + cmt_tracker.mean_height / 2))

                            elif num_of_tracked_keypoints < 5:
                                # print("[CMT] scale_estimate is greater or less than {:02f}".format(cmt_tracker.scale_estimate))
                                # pause_flag = True
                                cmt_tracker.force_init_flag = True
                                # box_tl = (int(box_center[0] - cmt_tracker.mean_width / 2), int(box_center[1] - cmt_tracker.mean_height / 2))
                                # box_br = (int(box_center[0] + cmt_tracker.mean_width / 2), int(box_center[1] + cmt_tracker.mean_height / 2))
                                box_tl = (int(cmt_tracker.cX - cmt_tracker.mean_width / 2), int(cmt_tracker.cY - cmt_tracker.mean_height / 2))
                                box_br = (int(cmt_tracker.cX + cmt_tracker.mean_width / 2), int(cmt_tracker.cY + cmt_tracker.mean_height / 2))

                            elif scale_change < 0.9 or scale_change > 2.0:
                                # print("[CMT] Scale change: {:.02f}".format(scale_change))
                                # pause_flag = True
                                cmt_tracker.force_init_flag = True
                                # box_tl = (int(box_center[0] - cmt_tracker.mean_width / 2), int(box_center[1] - cmt_tracker.mean_height / 2))
                                # box_br = (int(box_center[0] + cmt_tracker.mean_width / 2), int(box_center[1] + cmt_tracker.mean_height / 2))
                                box_tl = (int(cmt_tracker.cX - cmt_tracker.mean_width / 2), int(cmt_tracker.cY - cmt_tracker.mean_height / 2))
                                box_br = (int(cmt_tracker.cX + cmt_tracker.mean_width / 2), int(cmt_tracker.cY + cmt_tracker.mean_height / 2))

                        box_center = ((box_tl[0] + box_br[0]) // 2, (box_tl[1] + box_br[1]) // 2)
                        cmt_tracker.box_center = box_center

                        width = box_br[0] - box_tl[0]
                        height = box_br[1] - box_tl[1]

                        (cmt_tracker.x1, cmt_tracker.y1) = box_tl
                        (cmt_tracker.x2, cmt_tracker.y2) = box_br
                        cmt_tracker.area = width * height

                        # calulate averages for height, width, center
                        cmt_tracker.prev_widths = np.append(cmt_tracker.prev_widths, width)
                        cmt_tracker.prev_heights = np.append(cmt_tracker.prev_heights, height)
                        cmt_tracker.prev_centers = np.vstack([cmt_tracker.prev_centers, box_center])

                        if cmt_tracker.prev_widths.shape[0] > 10:
                            cmt_tracker.prev_widths = np.delete(cmt_tracker.prev_widths, (0), axis=0)
                            cmt_tracker.prev_heights = np.delete(cmt_tracker.prev_heights, (0), axis=0)
                            cmt_tracker.prev_centers = np.delete(cmt_tracker.prev_centers, (0), axis=0)

                        cmt_tracker.mean_width = np.round(np.mean(cmt_tracker.prev_widths)).astype(np.int)
                        cmt_tracker.mean_height = np.round(np.mean(cmt_tracker.prev_heights)).astype(np.int)
                        cmt_tracker.mean_center = np.round(np.mean(cmt_tracker.prev_centers, axis = 0)).astype(np.int)

                        # util.draw_str(frame, (550, 20), 'Tracking')
                        cv2.rectangle(frame, cmt_tracker.tl, cmt_tracker.br, (0,165,266), 1)
                        cv2.drawMarker(frame, cmt_tracker.box_center, (0,165,255))
                        # cv2.drawMarker(frame, tuple(cmt_tracker.center.astype(np.int16)), (0, 0, 255))

                        # util.draw_keypoints_by_number(cmt_tracker.tracked_keypoints, frame, (0, 0, 255))
                        # util.draw_keypoints_by_number(cmt_tracker.outliers, frame, (255, 0, 0))
                        # util.draw_keypoints(cmt_tracker.tracked_keypoints, frame, (255, 255, 255))
                        # util.draw_keypoints(cmt_tracker.votes[:, :2], frame, (0, 255, 255))
                        # util.draw_keypoints(cmt_tracker.outliers[:, :2], frame, (0, 0, 255))

                        # cv2.drawMarker(frame, (cmt_tracker.cX, cmt_tracker.cY), (255, 255, 255))
                        # cv2.drawMarker(frame, tuple(cmt_tracker.mean_center), (0, 0, 255))
                        # test_tl = box_center[0] - cmt_tracker.mean_width//2, box_center[1] - cmt_tracker.mean_height//2
                        # test_br = box_center[0] + cmt_tracker.mean_width//2, box_center[1] + cmt_tracker.mean_height//2
                        # cv2.rectangle(frame, test_tl, test_br, (255, 255, 255), 1)
                        if kcf_tracker:
                            if kcf_tracker.cmt_was_found is True:
                                kcf_tracker.consecutive_cmt_found += 1
                            else:
                                kcf_tracker.consecutive_cmt_found = 1
                                kcf_tracker.cmt_was_found = True
                    else: # kcf_tracker.has_result == False
                        if kcf_tracker:
                            if kcf_tracker.cmt_was_found is False:
                                kcf_tracker.consecutive_cmt_lost += 1
                            else:
                                kcf_tracker.consecutive_cmt_lost = 1
                                kcf_tracker.cmt_was_found = False

            if kcf_tracker:
                if kcf_tracker.force_init_flag is True:
                    # print('[KCF] Force init')
                    kcf_tracker.init(frame)
                    kcf_tracker.force_init_flag = False
                else: # kcf_tracker.force_init_flag is not True:
                    if kcf_tracker.enable:
                        boundingbox, loc = kcf_tracker.update(frame)
                        boundingbox = list(map(int, boundingbox))

                        kcf_tracker.x1 = boundingbox[0]
                        kcf_tracker.y1 = boundingbox[1]
                        kcf_tracker.x2 = boundingbox[0] + boundingbox[2]
                        kcf_tracker.y2 = boundingbox[1] + boundingbox[3]
                        kcf_tracker.center = ((kcf_tracker.x1 + kcf_tracker.x2) // 2, (kcf_tracker.y1 + kcf_tracker.y2) // 2)
                        kcf_tracker.area = int(boundingbox[2] * boundingbox[3])

                        cv2.rectangle(frame,(kcf_tracker.x1,kcf_tracker.y1), (kcf_tracker.x2,kcf_tracker.y2), (0,255,0), 1)
                        cv2.drawMarker(frame, tuple(kcf_tracker.center), (0,255,0))
                        # print("[KCF] peak_value: {:.04f}".format(kcf_tracker.peak_value))

                # print("[KCF/CMT] lost({}) vs found({})".format(kcf_tracker.consecutive_cmt_lost, kcf_tracker.consecutive_cmt_found))
                if color_tracker:
                    if color_tracker.consecutive_lost < color_tracker.FOUND_CONDITION:
                        if kcf_tracker.enable:
                            diff_width = abs(color_tracker.center[0] - kcf_tracker.center[0])
                            diff_height = abs(color_tracker.center[1] - kcf_tracker.center[1])
                            if diff_width > boundingbox[2] // 6 or diff_height < boundingbox[3] // 6: # boundingbox[2] == width
                                # print("[KCF] Adjust center with color object")
                                # print("[KCF] Bounding({},{}) vs Mean({},{})".format(boundingbox[2], boundingbox[3], kcf_tracker.mean_width, kcf_tracker.mean_height))
                                kcf_tracker.x1 = color_tracker.center[0] - kcf_tracker.mean_width // 2
                                kcf_tracker.x2 = color_tracker.center[0] + kcf_tracker.mean_width // 2
                                kcf_tracker.y1 = int(color_tracker.center[1] - kcf_tracker.mean_height/ 4)
                                kcf_tracker.y2 = int(color_tracker.center[1] + kcf_tracker.mean_height * 3 / 4)
                                kcf_tracker.center = ((kcf_tracker.x1 + kcf_tracker.x2) // 2, (kcf_tracker.y1 + kcf_tracker.y2) // 2)

                                kcf_tracker.force_init_flag = True
                                # pause_flag = True
                        # else:
                        #     if zoom.is_zooming is not True:
                        #         print("[KCF] Enabled")
                        #         kcf_tracker.enable = True
                        #         kcf_tracker.x1 = color_tracker.center[0] - 40 #selected_width // 2
                        #         kcf_tracker.x2 = color_tracker.center[0] + 40 #selected_width // 2
                        #         kcf_tracker.y1 = int(color_tracker.center[1] - 30) # selected_height/ 4)
                        #         kcf_tracker.y2 = int(color_tracker.center[1] + 30) # selected_height * 3 / 4)
                        #         kcf_tracker.center = ((kcf_tracker.x1 + kcf_tracker.x2) // 2, (kcf_tracker.y1 + kcf_tracker.y2) // 2)
                        #
                        #         kcf_tracker.force_init_flag = True

                    elif color_tracker.consecutive_lost >= color_tracker.LOST_CONDITION:
                        util.draw_str(frame, (520, 20), 'No color')
                        # kcf_tracker.enable = False
                        # if current_zoom != 1:
                        #     zoom_idx = 0
                        #     current_zoom = zooms[zoom_idx]
                        #     zoom.zoom_to(current_zoom, dur=3)

                elif cmt_tracker and cmt_tracker.has_result and cmt_tracker.best_effort is False:
                    # diff_width = abs(cmt_tracker.box_center[0] - kcf_tracker.center[0])
                    # diff_height = abs(cmt_tracker.box_center[1] - kcf_tracker.center[1])
                    diff_width = abs(cmt_tracker.center[0] - kcf_tracker.center[0])
                    diff_height = abs(cmt_tracker.center[1] - kcf_tracker.center[1])
                    area_ratio = kcf_tracker.area / cmt_tracker.area
                    # print("[KCF/CMT] diff of center: ({:.02f},{:.02f}), area ratio: {:.02f}".format(diff_width, diff_height, area_ratio))

                    if diff_width > boundingbox[2] or diff_height > boundingbox[3]:
                        print("[KCF/CMT] Too far away => init")
                        kcf_tracker.force_init_flag = True

                    if area_ratio > 2: # and kcf_tracker.consecutive_cmt_found < 100:
                        if diff_width < boundingbox[2]/6 or diff_height < boundingbox[3]/6:
                            print("[KCF/CMT] Area ratio: {:.02f} => init".format(area_ratio))
                            kcf_tracker.force_init_flag = True
                        else:
                            print("[KCF/CMT] Area ratio: {:.02f} => ignore".format(area_ratio))

                    # if kcf_tracker.consecutive_cmt_lost > 100 and kcf_tracker.consecutive_cmt_found > 100:
                    #     print("Lost to Found: => init")
                    #     kcf_tracker.force_init_flag = True

                    # if (diff_width > int(boundingbox[2]/4) and diff_width <= boundingbox[2]) or (diff_height > int(boundingbox[3]/4) and diff_height <= boundingbox[3]):
                    #     if kcf_tracker.consecutive_cmt_found > 20 and kcf_tracker.consecutive_cmt_found < 100:
                    #         print("Far away => init")
                    #         kcf_tracker.force_init_flag = True
                    #     else:
                    #         print("Far away => ignore")

                    if kcf_tracker.force_init_flag is True:
                        width = cmt_tracker.x2 - cmt_tracker.x1
                        height = cmt_tracker.y2 - cmt_tracker.y1
                        kcf_tracker.x1 = cmt_tracker.box_center[0] - width // 2
                        kcf_tracker.x2 = cmt_tracker.box_center[0] + width // 2
                        kcf_tracker.y1 = int(cmt_tracker.box_center[1] - height // 2)
                        kcf_tracker.y2 = int(cmt_tracker.box_center[1] + height // 2)
                        kcf_tracker.center = ((kcf_tracker.x1 + kcf_tracker.x2) // 2, (kcf_tracker.y1 + kcf_tracker.y2) // 2)
                        # pause_flag = True

                elif cmt_tracker and cmt_tracker.has_result and cmt_tracker.best_effort is True:

                    diff_width = abs(cmt_tracker.box_center[0] - kcf_tracker.center[0])
                    diff_height = abs(cmt_tracker.box_center[1] - kcf_tracker.center[1])
                    # diff_width = abs(cmt_tracker.center[0] - kcf_tracker.center[0])
                    # diff_height = abs(cmt_tracker.center[1] - kcf_tracker.center[1])
                    distance_between_cmt_and_kcf_center = math.sqrt(diff_width**2 + diff_width**2)

                    kcf_length = max(kcf_tracker.x2 - kcf_tracker.x1, kcf_tracker.y2 - kcf_tracker.y1)
                    cmt_length = max(cmt_tracker.x2 - cmt_tracker.x1, cmt_tracker.y2 - cmt_tracker.y1)

                    normalized_distance_from_kcf = distance_between_cmt_and_kcf_center / kcf_length
                    normalized_distance_from_cmt = distance_between_cmt_and_kcf_center / cmt_length

                    area_ratio = kcf_tracker.area / cmt_tracker.area
                    # print("[KCF/CMT] Center Distance: {:.02f}, CMT length: {} => {:.02f}, KCF length: {} => {:.02f}".
                    #     format(distance_between_cmt_and_kcf_center, cmt_length, normalized_distance_from_cmt, kcf_length, normalized_distance_from_kcf))

                    # if show_lap_time_flag is True: # 임시로
                    #     print("[KCF/CMT] Center Distance: {:.02f}, CMT length: {} => {:.02f}, KCF length: {} => {:.02f}".
                    #         format(distance_between_cmt_and_kcf_center, cmt_length, normalized_distance_from_cmt, kcf_length, normalized_distance_from_kcf))
                    #
                    #     show_lap_time_flag = False
                    #     pause_flag = True

                    kcf_init_with_cmt = False
                    kcf_init_with_shrink = False
                    cmt_init_with_kcf = False

                    distance_boundary = 1

                    if normalized_distance_from_cmt >= distance_boundary and normalized_distance_from_kcf >= distance_boundary:
                        print("[KCF/CMT] Too far => kcf_init_with_cmt")
                        kcf_init_with_cmt = True

                    # elif normalized_distance_from_cmt >= distance_boundary and normalized_distance_from_kcf < distance_boundary:
                    #     print("[KCF/CMT] KCF diverse => kcf_init_with_shrink")
                    #     kcf_init_with_shrink = True
                    #
                    # elif normalized_distance_from_cmt < distance_boundary and normalized_distance_from_kcf >= distance_boundary:
                    #     print("[KCF/CMT] CMT diverse => cmt_init_with_kcf")
                    #     # cmt_init_with_kcf = True

                    else: # normalized_distance_from_cmt < 0.1 and normalized_distance_from_kcf < 0.1:
                        if area_ratio >= 4.0 or kcf_tracker.area > 180000:
                            print("[KCF/CMT] kcf is as large as {:.02f} => kcf_init_with_cmt".format(area_ratio))
                            kcf_init_with_cmt = True
                        elif area_ratio < 0.25:
                            print("[KCF/CMT] cmt is as large as {:.02f} => cmt_init_with_kcf".format(1/area_ratio))
                            cmt_init_with_kcf = True


                    if kcf_init_with_cmt is True:
                        width = cmt_tracker.x2 - cmt_tracker.x1
                        height = cmt_tracker.y2 - cmt_tracker.y1
                        kcf_tracker.x1 = cmt_tracker.box_center[0] - width // 2
                        kcf_tracker.x2 = cmt_tracker.box_center[0] + width // 2
                        kcf_tracker.y1 = cmt_tracker.box_center[1] - height // 2
                        kcf_tracker.y2 = cmt_tracker.box_center[1] + height // 2
                        kcf_tracker.center = ((kcf_tracker.x1 + kcf_tracker.x2) // 2, (kcf_tracker.y1 + kcf_tracker.y2) // 2)
                        kcf_tracker.force_init_flag = True

                    elif kcf_init_with_shrink is True:
                        width = int((kcf_tracker.x2 - kcf_tracker.x1) * 0.5)
                        height = int((kcf_tracker.y2 - kcf_tracker.y1) * 0.5)
                        kcf_tracker.x1 = kcf_tracker.center[0] - width // 2
                        kcf_tracker.x2 = kcf_tracker.center[0] + width // 2
                        kcf_tracker.y1 = kcf_tracker.center[1] - height // 2
                        kcf_tracker.y2 = kcf_tracker.center[1] + height // 2
                        kcf_tracker.center = ((kcf_tracker.x1 + kcf_tracker.x2) // 2, (kcf_tracker.y1 + kcf_tracker.y2) // 2)
                        kcf_tracker.force_init_flag = True

                    elif cmt_init_with_kcf is True:
                        width = kcf_tracker.x2 - kcf_tracker.x1
                        height = kcf_tracker.y2 - kcf_tracker.y1
                        cmt_tracker.x1 = kcf_tracker.x1
                        cmt_tracker.x2 = kcf_tracker.x2
                        cmt_tracker.y1 = kcf_tracker.y1
                        cmt_tracker.y2 = kcf_tracker.y2
                        cmt_tracker.force_init_flag = True

                if kcf_tracker.enable:
                    # calulate averages for height, width, center
                    kcf_tracker.prev_widths = np.append(kcf_tracker.prev_widths, boundingbox[2])
                    kcf_tracker.prev_heights = np.append(kcf_tracker.prev_heights, boundingbox[3])

                    if kcf_tracker.prev_widths.shape[0] > 10: #kcf_tracker.PREV_HISTORY_SIZE: # 100
                        kcf_tracker.prev_widths = np.delete(kcf_tracker.prev_widths, (0), axis=0)
                        kcf_tracker.prev_heights = np.delete(kcf_tracker.prev_heights, (0), axis=0)

                    kcf_tracker.mean_width = np.round(np.mean(kcf_tracker.prev_widths)).astype(np.int)
                    kcf_tracker.mean_height = np.round(np.mean(kcf_tracker.prev_heights)).astype(np.int)
                    kcf_tracker.mean_area = int(kcf_tracker.mean_width * kcf_tracker.mean_height)

                    str = "{}x{}({}x{})".format(kcf_tracker.mean_width, kcf_tracker.mean_height, selected_width, selected_height)
                    util.draw_str(frame, (20, 20), str)


                if args['autozoom'] and zoom.is_zooming is not True:
                    next_zoom = zoom.find_next_auto_zoom(target_length = kcf_tracker.mean_width)
                    if next_zoom != zoom.current_zoom:
                        print("[ZOOM] {} to {}".format(zoom.current_zoom, next_zoom))
                        zoom.zoom_to(next_zoom, dur=3)

            if motor and motor.is_moving is not True: # and zoom.is_zooming is not True:
                motor.driving_flag = False

                if kcf_tracker:
                    if kcf_tracker.peak_value > 0.2:
                        cX, cY = kcf_tracker.center
                        motor.driving_flag = True
                    else:
                        print("[KCF] Disabled:", kcf_tracker.peak_value)
                        kcf_tracker.enable = False
                elif color_tracker and color_tracker.consecutive_lost < color_tracker.FOUND_CONDITION:
                    cX, cY = color_tracker.center
                    motor.driving_flag = True
                elif cmt_tracker and cmt_tracker.has_result:
                    cX, cY = cmt_tracker.box_center
                    motor.driving_flag = True
                else:
                    cX = HALF_WIDTH
                    cY = HALF_HEIGHT

                if args['serial'] and motor.driving_flag is True:
                    # cv2.drawMarker(frame, (cX, cY), (0,0,255))
                    center_to_x = HALF_WIDTH - cX
                    center_to_y = cY - HALF_HEIGHT
                    # print("[MOTOR] Distance from Center: ({}px, {}px)".format(center_to_x, center_to_y))

                    if zoom is None:
                        motor.track(center_to_x, center_to_y)
                    else:
                        motor.track(center_to_x, center_to_y, zoom.current_zoom)

        cv2.line(frame, (HALF_WIDTH, 0), (HALF_WIDTH, WIDTH), (200, 200, 200), 0)
        cv2.line(frame, (0, HALF_HEIGHT), (WIDTH, HALF_HEIGHT), (200, 200, 200), 0)

    if args["display"] is True:
        if tracking_window['dragging'] == True:
            pt1 = (tracking_window['x1'], tracking_window['y1'])
            pt2 = (tracking_window['x2'], tracking_window['y2'])

            if capture is None:
                capture = np.copy(frame)

            frame = np.copy(capture)
            cv2.rectangle(frame, pt1, pt2, (0, 255, 0,), 1)
            cv2.imshow("Tracking", frame)

        # pause가 아닌 상태에서 Tracking window 보이기(당연),
        # 그런데 pause 일때 굳이 동작 않도록 처리한 이유는? => pause 일때 마우스 조작이 일어나는 경우에 대처하기 위해, 즉, 다른곳에서 윈도우 처리
        if pause_flag is False:
            cv2.imshow("Tracking", frame)

        key = cv2.waitKey(1)
        if key == 27 or key == ord('q'):
            break
        elif key == ord(' '):
            pause_flag = not pause_flag
        elif key == ord('s'):
            tracking_processing_flag = False
        elif key == ord('i'):
            if args['kcf']:
                kcf_tracker.force_init_flag = True
            if args['cmt']:
                cmt_tracker.force_init_flag = True
        elif key == 65362: # 'up', 63232 for Mac
            if zoom.is_zooming is not True:
                next_zoom = zoom.find_next_zoom(dir='in')
                if next_zoom != zoom.current_zoom:
                    zoom.zoom_to(next_zoom, dur=0.1)
        elif key == 65364: # 'down', 63233 for Mac
            if zoom.is_zooming is not True:
                next_zoom = zoom.find_next_zoom(dir='out')
                if next_zoom != zoom.current_zoom:
                    zoom.zoom_to(next_zoom, dur=0.1)
        elif key == 65361: # 'left', 63234 for Mac
            if zoom.is_zooming is not True:
                next_zoom = zoom.find_next_zoom(dir='first')
                if next_zoom != zoom.current_zoom:
                    zoom.zoom_to(next_zoom, dur=0.1)
        elif key == 65363: # 'right', 63235 for Mac
            if zoom.is_zooming is not True:
                next_zoom = zoom.find_next_zoom(dir='last')
                if next_zoom != zoom.current_zoom:
                    zoom.zoom_to(next_zoom, dur=0.1)
        elif key == ord('d'):
            print("[MOTOR] Degree: ({:.02f}, {:.02f})".format(motor.sum_of_x_degree, motor.sum_of_y_degree))
        elif key == ord('t') and (args['cmt_alone'] or args['cmt']) is True:
            grabbed, frame = stream.read()
            frame = imutils.resize(frame, width=WIDTH)
            gray0 = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray0 = cv2.GaussianBlur(gray0, (3, 3), 0)

            for x in range(10, 500, 10):
                detector = cv2.BRISK_create(x, 3, 3.0)
                keypoints = detector.detect(gray0)
                cmt_detector_threshold = x
                if len(keypoints) < cmt_tracker.MIN_NUM_OF_KEYPOINTS_FOR_BRISK_THRESHOLD:
                    break
            print("[CMT] BRISK threshold is set to {} with {} keypoints".format(x, len(keypoints)))
            cmt_tracker.detector = detector
            cmt_tracker.descriptor = detector
        elif key == ord('l'):
            show_lap_time_flag = not show_lap_time_flag
        elif key == ord('z'):
            if args['serial']:
                motor.sum_of_x_degree = motor.sum_of_y_degree = 0


# do a bit of cleanup
cv2.destroyAllWindows()
stream.release()
