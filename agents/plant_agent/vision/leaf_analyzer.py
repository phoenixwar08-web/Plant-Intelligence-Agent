#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Leaf visual measurement

OpenCV:
image -> HSV -> green/yellow mask
"""


import cv2
import numpy as np


def analyze_leaf(image_path: str):

    image = cv2.imread(
        image_path
    )
    
    h, w = image.shape[:2]
    
    
    # 植物区域ROI
    image = image[
        int(h*0.15):int(h*0.75),
        int(w*0.15):int(w*0.85)
    ]

    if image is None:
        raise FileNotFoundError(
            image_path
        )


    total_pixels = (
        image.shape[0]
        *
        image.shape[1]
    )


    # BGR -> HSV

    hsv = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2HSV
    )


    # 绿色区域

    green_lower = np.array(
        [35, 40, 40]
    )

    green_upper = np.array(
        [85, 255, 255]
    )


    green_mask = cv2.inRange(
        hsv,
        green_lower,
        green_upper
    )


    green_pixels = int(
        cv2.countNonZero(
            green_mask
        )
    )


    # 黄色区域

    yellow_lower = np.array(
        [20, 80, 80]
    )

    yellow_upper = np.array(
        [35, 255, 255]
    )


    yellow_mask = cv2.inRange(
        hsv,
        yellow_lower,
        yellow_upper
    )


    yellow_pixels = int(
        cv2.countNonZero(
            yellow_mask
        )
    )


    return {

        "green_leaf_area_px":
            green_pixels,

        "green_leaf_ratio":
            round(
                green_pixels / total_pixels,
                4
            ),

        "yellow_leaf_area_px":
            yellow_pixels,

        "yellow_pixel_ratio":
            round(
                yellow_pixels / total_pixels,
                4
            )

    }
