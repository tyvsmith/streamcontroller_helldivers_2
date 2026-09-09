"""Experimental selection layout anchored to the complete left Ready bar."""

import cv2
import numpy as np

from .game_capture import ScanError


def find_selection_band(im):
    pixels = np.array(im.convert("RGB"))
    height, width = pixels.shape[:2]
    hsv = cv2.cvtColor(pixels, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, np.array([20, 150, 140]), np.array([38, 255, 255]))
    mask[:int(height * .65)] = 0
    mask[int(height * .94):] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                           np.ones((3, max(3, int(width * .01))), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    bands = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if (.10 * width < w < .30 * width and .015 * height < h < .07 * height
                and w / h > 5 and x < .25 * width):
            bands.append([x, y, w, h])
    if len(bands) != 1:
        raise ScanError("Cannot isolate the left player's Ready bar. Use --selection-band x,y,w,h "
                        "with normalized coordinates, or save a debug screenshot for calibration.")
    return bands[0]


def selection_boxes(im, band):
    x, y, width, height = band
    if (min(x, y) < 0 or min(width, height) <= 0
            or x + width > im.width or y + height > im.height):
        raise ScanError("Selection boundary is outside the image.")
    # Ratios calibrated from the full 840-pixel Ready bar, not screen width.
    top = [(4 + 121 * i, -304, 104, 104) for i in range(7)]
    equipped = [(7 + 170 * i, -172, 150, 150) for i in range(4)]
    scale = width / 840
    boxes = [[round(x + bx * scale), round(y + by * scale),
              round(w * scale), round(h * scale)] for bx, by, w, h in top + equipped]
    for bx, by, w, h in boxes:
        if min(w, h) < 25 or by < 0 or bx < x or bx + w > x + width or by + h > y:
            raise ScanError("Selection tiles would be clipped or too small; recalibrate the Ready bar.")
    return boxes


def empty_tile(im, box, mission=False):
    x, y, w, h = box
    if mission:
        hsv = cv2.cvtColor(np.array(im.crop((x, y, x + w, y + h))), cv2.COLOR_RGB2HSV)
        # Pale gold frames in bright captures have saturation around 35.
        colored = ((hsv[:, :, 1] > 30) & (hsv[:, :, 2] > 110)).astype(float)
        rim = max(2, round(min(w, h) * .096))
        edges = [colored[:rim], colored[-rim:], colored[:, :rim], colored[:, -rim:]]
        # Filled mission slots have a colored frame; the player model may
        # remain visible through empty slots, so interior variance is not occupancy.
        return sum(float(edge.mean()) >= .06 for edge in edges) < 3
    patch = np.array(im.crop((x + w // 4, y + h // 4, x + 3 * w // 4, y + 3 * h // 4)))
    gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
    return float(gray.std()) < 8
