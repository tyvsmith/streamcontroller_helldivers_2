"""Canonical foreground colors for comparing game tiles with button artwork."""
import cv2
import numpy as np
from PIL import Image

# OpenCV hue centers; hue is retained while exposure/saturation are discarded.
PALETTE = [(0, (255, 0, 0)), (30, (255, 255, 0)),
           (60, (0, 255, 0)), (90, (0, 255, 255))]


def normalize_icon(image, mission=False):
    rgb = np.array(image.convert('RGB'))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, saturation, value = (hsv[:, :, i] for i in range(3))
    size = min(image.size)
    rim = max(2, round(size * .07))
    foreground = np.zeros(value.shape, bool)
    foreground[rim:-rim, rim:-rim] = True
    color_threshold = 60
    frame_hue = None
    if mission and icon_category(image, frame=True) is not None:
        strip = hsv[round(image.height * .2):round(image.height * .8),
                    max(1, round(image.width * .01)):max(3, round(image.width * .05))]
        frame_hue = float(np.median(strip[:, :, 0]))
        color_threshold = max(12, float(np.median(strip[:, :, 1])) * .5)
    colored = foreground & (saturation >= color_threshold) & (value > 90)
    if frame_hue is not None:
        distance = abs(hue.astype(float) - frame_hue)
        colored &= np.minimum(distance, 180 - distance) < 12
    # Prefer the brightest colored pixels over muted scenery behind the tile.
    if np.any(colored):
        peak = np.percentile(value[colored], 90)
        samples = hue[colored & (value >= peak * .90)]
        dominant = frame_hue if frame_hue is not None else float(np.median(samples))
        distance = abs(hue.astype(float) - dominant)
        colored &= (np.minimum(distance, 180 - distance) < 12) & (value >= peak * .85)
        family = (0 if dominant < 22 or dominant >= 165 else
                  1 if dominant < 38 else 2 if dominant < 78 else 3)
        color = PALETTE[family][1]
    else:
        color = (0, 255, 255)
    # Use a tile-relative white point so dim/tinted captures retain their glyphs.
    pale = foreground & (saturation < color_threshold)
    white_level = np.percentile(value[pale], 98) if np.any(pale) else 255
    white = pale & (value >= max(140, white_level * .87))
    if mission:
        # Quantity badges are external overlays, unlike the glyph's own details.
        edge = (saturation < color_threshold) & (value >= max(140, white_level * .87))
        if edge[round(image.height * .60):, -max(1, round(size * .035)):].mean() > .35:
            white[round(image.height * .60):, round(image.width * .80):] = False
            colored[round(image.height * .60):, round(image.width * .80):] = False
    yy, xx = np.indices(value.shape)
    corners = (np.minimum(xx, image.width - 1 - xx)
               + np.minimum(yy, image.height - 1 - yy)) < size * .28
    # Yellow corner arrows are frame decoration, not stratagem artwork.
    if np.any(colored) and color == (255, 255, 0):
        colored[corners] = False
    output = np.zeros_like(rgb)
    output[colored] = color
    output[white] = 255
    return Image.fromarray(output)


def icon_category(image, *, frame=False):
    """Read the color family independently of brightness and glyph shape."""
    rgb = np.array(image.convert('RGB'))
    if frame:
        width = image.width
        rgb = rgb[round(image.height * .2):round(image.height * .8),
                  max(1, round(width * .01)):max(3, round(width * .05))]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    selected = (hsv[:, :, 1] >= (4 if frame else 15)) & (hsv[:, :, 2] >= 90)
    if selected.mean() < (.3 if frame else .01):
        return None
    hues = hsv[:, :, 0][selected]
    buckets = np.where((hues < 22) | (hues >= 165), 0,
                      np.where(hues < 38, 1, np.where(hues < 78, 2,
                               np.where(hues < 115, 3, 4))))
    counts = np.bincount(buckets, minlength=5)
    best = int(counts.argmax())
    if frame and best == 1 and np.median(hsv[:, :, 1]) < 40:
        return None  # Clipped red can look yellow; do not restrict the catalog by it.
    return ('red', 'yellow', 'green', 'cyan')[best] if best < 4 and counts[best] / len(hues) >= .75 else None


def icon_occluded(image):
    """Detect the solid white overlay hiding an inbound stratagem's glyph."""
    w, h = image.size
    interior = np.array(image.convert('RGB').crop((round(w * .12), round(h * .12),
                                                  round(w * .88), round(h * .60))))
    return bool((interior.min(axis=2) >= 245).mean() > .97)


def stretch_icon(image):
    """Expand surviving channel contrast in washed-out tiles, without recovering clipped detail."""
    rgb = np.array(image.convert('RGB')).astype(float)
    rim = max(2, round(min(image.size) * .10))
    floor = np.percentile(rgb[rim:-rim, rim:-rim], 5, axis=(0, 1))
    if floor.min() < 130 or (255 - floor).max() < 8:
        return None
    return Image.fromarray(np.clip((rgb - floor) / np.maximum(255 - floor, 1) * 255,
                                   0, 255).astype(np.uint8))
