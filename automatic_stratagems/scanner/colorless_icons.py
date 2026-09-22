"""Match glyph shape across horizontal cooldown overlays, independently of hue."""
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .recognize.constants import COLORLESS_RIM_FRACTION, COLORLESS_RIM_MIN, TEMPLATE_INTERIOR
from .recognize.match_result import COLORLESS_TOP3

ROOT = Path(__file__).resolve().parents[2]


def glyph_mask(image, template=False):
    """Extract a binary glyph mask from a tile, independent of hue.

    Trims the frame rim unless template is set, in which case TEMPLATE_INTERIOR is used
    instead. The threshold is per-row, relative to that row's dark and bright
    percentiles, and connected components smaller than 4 pixels are dropped as noise.
    """
    value = np.asarray(image.convert('RGB')).max(2).astype(float)
    if template:
        value = value[TEMPLATE_INTERIOR, TEMPLATE_INTERIOR]
    else:
        rim = max(COLORLESS_RIM_MIN, round(min(value.shape) * COLORLESS_RIM_FRACTION))
        value = value[rim:-rim, rim:-rim]
    value = cv2.GaussianBlur(value, (3, 3), .7)
    background = np.percentile(value, 20, axis=1)[:, None]
    foreground = np.percentile(value, 98, axis=1)[:, None]
    mask = ((value - background) > np.maximum(22, (foreground - background) * .5)).astype('uint8')
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    for index in range(1, count):
        if stats[index, cv2.CC_STAT_AREA] < 4:
            mask[labels == index] = 0
    return mask * 255


@lru_cache(maxsize=4)
def references(keys):
    """Build cached colorless silhouettes for one exact ordered tuple of catalog keys.

    keys must be a hashable tuple; a different order or set of keys is cached
    separately, up to 4 recent tuples.
    """
    from .stratagem_detection import silhouette
    result = []
    for key in keys:
        with Image.open(ROOT / 'assets/icons' / (key + '.png')) as image:
            result.append((key, silhouette(glyph_mask(image, template=True), 120)))
    return result


def match_colorless(image, entries):
    """Match a tile's glyph shape against the catalog, ignoring color entirely.

    Accepts the top-ranked entry when its score and margin over the runner-up clear
    score >= .50 with margin >= .15, or score >= .85 with margin >= .08. Returns a
    dict with id (None when undecided) and the top-3 ranking under COLORLESS_TOP3.
    """
    from .stratagem_detection import silhouette
    mask = glyph_mask(image)
    observed = silhouette(mask, 120)
    ranking = sorted(((float(np.minimum(observed, reference).sum() /
                              max(1, np.maximum(observed, reference).sum())), key)
                      for key, reference in references(tuple(entries))), reverse=True)
    accepted = False
    if len(ranking) >= 2:
        score, margin = ranking[0][0], ranking[0][0] - ranking[1][0]
        accepted = (score >= .50 and margin >= .15) or (score >= .85 and margin >= .08)
    return {'id': ranking[0][1] if accepted else None, COLORLESS_TOP3: ranking[:3]}
