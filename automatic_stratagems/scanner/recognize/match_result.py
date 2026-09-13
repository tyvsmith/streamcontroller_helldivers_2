"""Declared ranking evidence in recognition results, and restoring it from the cache.

Matchers report their best candidates as ``[(score, key), ...]`` under one of the names below.
Rows stay plain dicts. This module names the rankings once and validates cached copies, which
come back from JSON as lists. Declare every ranking a cached result stores in RANKING_KEYS:
an undeclared one comes back from a warm cache unvalidated, as lists, where a cold run
returns tuples.
"""
import numpy as np

GAME_REFERENCE_TOP3 = 'game_reference_top3'  # mission_references: curated game crops
COLORLESS_TOP3 = 'colorless_top3'  # colorless_icons: hue-independent silhouettes
NAME_TOP3 = 'name_top3'  # mission_fallbacks: OCR name matches
SHORTLIST_TOP3 = 'shortlist_top3'  # match_equipped: verified shortlist correlation
DETAIL_TOP3 = 'detail_top3'  # match_equipped: component silhouette overlap
FEATURE_TOP3 = 'feature_top3'  # match_equipped: full-catalog feature correlation
CORRELATION_TOP3 = 'correlation_top3'  # detect_icons: gray and edge correlation
SILHOUETTE_TOP3 = 'silhouette_top3'  # detect_icons: gray silhouette overlap

RANKING_KEYS = frozenset({
    GAME_REFERENCE_TOP3, COLORLESS_TOP3, NAME_TOP3, SHORTLIST_TOP3,
    DETAIL_TOP3, FEATURE_TOP3, CORRELATION_TOP3, SILHOUETTE_TOP3,
})


def restore_ranking(value, *, pair_types=(list,), min_length=0, candidates=None):
    """Return a cached ranking as (score, key) tuples, or None when it is malformed.

    Each pair needs a finite numeric score and a string key. pair_types, min_length and
    candidates tighten the check for callers that require more.
    """
    if not isinstance(value, list) or len(value) < min_length:
        return None
    if any(not isinstance(pair, pair_types) or len(pair) != 2
           or not isinstance(pair[0], (int, float)) or not np.isfinite(pair[0])
           or not isinstance(pair[1], str)
           or (candidates is not None and pair[1] not in candidates) for pair in value):
        return None
    return [tuple(pair) for pair in value]


def restore_rankings(value):
    """Restore every declared ranking nested in a cached result, in place.

    Raises ValueError for a malformed declared ranking. Every other key is walked as an
    ordinary value, whatever its name.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if key in RANKING_KEYS:
                ranking = restore_ranking(item)
                if ranking is None:
                    raise ValueError('invalid cached ranking')
                value[key] = ranking
            else:
                restore_rankings(item)
    elif isinstance(value, list):
        for item in value:
            restore_rankings(item)
