"""Experimental selection layout anchored to the complete left Ready bar."""

import cv2
import numpy as np

from .errors import ScanError


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
    if len(bands) == 1:
        return bands[0]
    if not bands:
        bands = _washed_out_selection_bands(im, pixels, hsv)
        if len(bands) == 1:
            return bands[0]
    raise ScanError("Cannot isolate the left player's Ready bar. Use --selection-band x,y,w,h "
                    "with normalized coordinates, or save a debug screenshot for calibration.")


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


def _washed_out_selection_bands(im, pixels, hsv):
    """Recover a pale Ready bar from the complete local-player panel edge."""
    height, width = pixels.shape[:2]
    y_start, y_end = round(height * .84), round(height * .92)
    x_start, x_end = 0, round(width * .50)
    region = pixels[y_start - 1:y_end + 1, x_start:x_end].astype(np.int16)
    delta = region[1:] - region[:-1]
    strength = (delta.astype(np.int32) ** 2).sum(axis=2)
    edges = (strength > 25 ** 2).astype(np.uint8) * 255
    edges = cv2.morphologyEx(
        edges, cv2.MORPH_CLOSE,
        np.ones((1, max(3, round(width * .015))), np.uint8))

    raw = []
    for offset_y, row in enumerate(edges):
        changes = np.diff(np.pad((row > 0).astype(np.int8), (1, 1)))
        starts = np.flatnonzero(changes == 1) + x_start
        ends = np.flatnonzero(changes == -1) + x_start
        for start, end in zip(starts, ends):
            panel_width = end - start
            if not (.01 * width < start < .25 * width
                    and .20 * width < end < .48 * width
                    and .12 * width < panel_width < .30 * width):
                continue
            raw.append((y_start + offset_y, int(start), int(end)))

    groups = []
    row_tolerance = max(2, round(height * .004))
    edge_tolerance = max(2, round(width * .006))
    for candidate in raw:
        for group in groups:
            previous = group[-1]
            if (candidate[0] - previous[0] <= row_tolerance
                    and abs(candidate[1] - previous[1]) <= edge_tolerance
                    and abs(candidate[2] - previous[2]) <= edge_tolerance):
                group.append(candidate)
                break
        else:
            groups.append([candidate])

    bands = []
    for group in groups:
        edge_y = min(candidate[0] for candidate in group)
        panel_start = round(float(np.median([candidate[1] for candidate in group])))
        edge_x = round(float(np.median([candidate[2] for candidate in group])))
        # The complete captured player-panel edge is 984 pixels for an
        # 840-pixel Ready bar. Derive HUD scale from that observed edge.
        band_width = round((edge_x - panel_start) * 840 / 984)
        band_height = round(band_width * 84 / 840)
        band = [edge_x - band_width, edge_y - band_height + 1,
                band_width, band_height]
        x, y, w, h = band
        if min(x, y, w, h) <= 0 or x + w > width or y + h > height:
            continue
        band_hsv = hsv[y:y + h, x:x + w]
        pale = ((band_hsv[:, :, 1] < 60) & (band_hsv[:, :, 2] > 180)).mean()
        if float(pale) < .25:
            continue
        try:
            top = selection_boxes(im, band)[:7]
        except ScanError:
            continue
        if sum(not empty_tile(im, box, mission=True) for box in top) >= 2:
            bands.append(band)
    return bands
