"""Strict matches to visually labeled game icons, with blurred scenery removed."""
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .recognize.constants import MISSION_REFERENCE_INSET, MISSION_REFERENCE_PX
from .recognize.match_result import GAME_REFERENCE_TOP3

REFERENCE_DIR = Path(__file__).with_name('references')


def detail_image(image):
    """Extract high-frequency glyph detail from a reference crop or live tile.

    Detail is grayscale minus a heavily blurred copy of the image resized to
    MISSION_REFERENCE_PX, with the frame corner and quantity badge area (rows
    52+, columns 69+) zeroed before the inset crop.
    """
    rgb = np.array(image.convert('RGB').resize((MISSION_REFERENCE_PX, MISSION_REFERENCE_PX)))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    detail = gray - cv2.GaussianBlur(gray, (0, 0), 3)
    # Exclude frame edges and the quantity badge, which can change independently.
    detail[52:, 69:] = 0
    return detail[MISSION_REFERENCE_INSET:-MISSION_REFERENCE_INSET,
                  MISSION_REFERENCE_INSET:-MISSION_REFERENCE_INSET]


def badge_mask(image):
    """Exclude a white counter plate whose width varies with the digit count."""
    rgb = np.array(image.convert('RGB').resize((MISSION_REFERENCE_PX, MISSION_REFERENCE_PX)))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    white = ((hsv[:, :, 1] < 60)
             & (hsv[:, :, 2] > max(140, np.percentile(hsv[:, :, 2], 98) * .85)))
    mask = np.zeros((MISSION_REFERENCE_PX, MISSION_REFERENCE_PX), bool)
    for y in range(39, 70):
        line = white[y:y + 3].all(axis=0)
        dark = np.where(~line)[0]
        x = dark[-1] + 1 if len(dark) else 0
        if 26 <= x <= 73 and line[-1]:
            mask[max(0, y - 4):, max(0, x - 4):] = True
            break
    return mask[MISSION_REFERENCE_INSET:-MISSION_REFERENCE_INSET,
                MISSION_REFERENCE_INSET:-MISSION_REFERENCE_INSET]


@lru_cache(maxsize=1)
def references():
    """Load and cache every curated game-reference crop's detail image.

    Returns (name, detail) pairs for every PNG in REFERENCE_DIR, sorted by name and
    computed once per process.
    """
    result = []
    for path in sorted(REFERENCE_DIR.glob('*.png')):
        with Image.open(path) as image:
            result.append((path.stem, detail_image(image)))
    return result


def match_reference(image, entries):
    """Match a mission tile against curated game-reference crops, ignoring the badge.

    Compares only entries present in entries. Accepts the top match when its score and
    margin over the runner-up clear the threshold, returning a dict with id (None
    otherwise), GAME_REFERENCE_TOP3 ranking, and method 'mission-icon'.
    """
    detail = detail_image(image)
    mask = badge_mask(image)
    detail[mask] = 0
    # Border localization may differ by a pixel after scaling or JPEG decoding.
    aligned = cv2.copyMakeBorder(detail, 2, 2, 2, 2, cv2.BORDER_CONSTANT)
    ranking = sorted(
        [(float(cv2.matchTemplate(aligned, np.where(mask, 0, reference), cv2.TM_CCOEFF_NORMED).max()), key)
         for key, reference in references() if key in entries], reverse=True)
    accepted = (len(ranking) >= 2 and ranking[0][0] >= .90
                and ranking[0][0] - ranking[1][0] >= .12)
    return {'id': ranking[0][1] if accepted else None,
            GAME_REFERENCE_TOP3: ranking[:3], 'method': 'mission-icon'}
