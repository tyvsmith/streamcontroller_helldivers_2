"""Offline screenshot recognition without StreamController writes."""

import json
from pathlib import Path
from threading import RLock

import cv2
import numpy as np
from PIL import Image

from .mission_references import match_reference
from .icon_normalization import normalize_icon, icon_category, icon_occluded, stretch_icon
from .mission_layout import mission_frames
from .recognize.constants import (
    EQUIPPED_DETAIL_INSET, EQUIPPED_FEATURE_INSET, ICON_GRAY_INSET, ICON_SILHOUETTE_INSET,
    SILHOUETTE_CANVAS, SILHOUETTE_EXTENT, TEMPLATE_INTERIOR, TILE_PX,
)
from .recognize.match_result import (
    CORRELATION_TOP3, DETAIL_TOP3, FEATURE_TOP3, SHORTLIST_TOP3, SILHOUETTE_TOP3,
    restore_ranking, restore_rankings,
)
from .recognize.tile import Tile


ROOT = Path(__file__).resolve().parents[2]


def catalog():
    """Load the stratagem catalog, keyed by id, with display name and input sequence."""
    sequences = json.loads((ROOT / "assets/data/stratagems.json").read_text())
    locale = json.loads((ROOT / "locales/en_US.json").read_text())
    return {
        key: {"name": locale[f"actions.{key}.name"], "sequence": sequence}
        for key, sequence in sequences.items()
    }


def ordered_map(executor, function, values):
    """Apply function to values in order, via executor.map when given a pool."""
    return list(executor.map(function, values)) if executor is not None else list(map(function, values))


def detect_mission_icons(im, entries, *, executor=None, cache=None):
    """Recognize mission-panel icons in a full HUD screenshot, top row to bottom.

    Runs reference and occlusion matching per tile from the cache when available.
    Unresolved tiles then go through detect_icons; washed-out or unresolved tiles
    get a contrast or normalization retry, then colorless. Returns a list of row
    dicts, each with at least box (x, y, width, height), method and id (None when
    unresolved); rows restored from the cache are not written back to it.
    """
    bank = IconTemplates(cache=cache)
    frames = mission_frames(im)
    if not frames:
        return []
    tiles = Image.new('RGB', (TILE_PX * len(frames), TILE_PX))
    for i, (x, y, size, _) in enumerate(frames):
        tiles.paste(im.crop((x, y, x + size, y + size)).resize((TILE_PX, TILE_PX)), (TILE_PX * i, 0))
    # One RGB crop per frame serves every full-size pass below. The strip above is a resized
    # copy, so its categories are separate measurements and never mix with these tiles.
    frame_tiles = [Tile.from_source(im, (x, y, size, size)) for x, y, size, _ in frames]
    icons, cache_keys, cached_indices = [], [], set()
    for i, ((x, y, size, _), tile) in enumerate(zip(frames, frame_tiles)):
        cache_key = None
        saved = None
        if cache is not None:
            # The complete pipeline also uses raw contrast/reference detail, so
            # this cache requires exact RGB pixels, not just normalized features.
            cache_key = cache.key(tile.image.tobytes(), {'matcher': 'mission-complete',
                                  'box': [x, y, size, size], 'index': i, 'candidates': list(entries)})
            saved = restore_mission_result(cache.get(cache_key), entries)
        cache_keys.append(cache_key)
        if saved is not None:
            icons.append(saved)
            cached_indices.add(i)
        else:
            icons.append({'id': None, 'occluded': True,
                          'text': 'Icon obscured by a solid white overlay'}
                         if icon_occluded(tile.image) else match_reference(tile.image, entries))
    unresolved = [i for i, icon in enumerate(icons) if icon['id'] is None and not icon.get('occluded')]
    if unresolved:
        fallback = detect_icons(tiles, entries, [[TILE_PX * i, 0, TILE_PX, TILE_PX] for i in unresolved], mission=True, bank=bank, executor=executor)
        for i, icon in zip(unresolved, fallback):
            icons[i] = {**icons[i], **icon}
    enhancement_jobs = []
    for i, (tile, icon) in enumerate(zip(frame_tiles, icons)):
        if i in cached_indices or icon.get('occluded') or icon.get('conflict'):
            continue
        enhanced = stretch_icon(tile.image)
        if enhanced is not None or icon['id'] is None:
            # Each job owns its frame's Tile, so no Tile is read from two threads at once.
            enhancement_jobs.append((i, tile, enhanced))
    # One costly row can use the pool for candidates. Multiple rows own the pool
    # themselves; workers never submit nested work to the same executor.
    candidate_executor = executor if len(enhancement_jobs) == 1 else None
    def enhance(job):
        """Retry one unresolved or washed-out icon via contrast or normalization.

        job is (index, tile, enhanced-image-or-None) from enhancement_jobs; enhanced is
        None when normalize_icon should run instead of the stretch_icon result. Mutates
        and returns icons[index] in place. Flags preprocessing_conflict when the retry
        disagrees with an existing native id, unless the retry's decision is
        discriminating_details, which replaces the id instead.
        """
        i, tile, enhanced = job
        icon = icons[i]
        category = tile.frame_category
        # Normalize before interpolation mixes the pale glyph with its background.
        prepared = (enhanced if enhanced is not None
                    else normalize_icon(tile.image, mission=True, frame_category=category))
        allowed = {}
        for key, value in entries.items():
            if category is None or bank.get(key)['category'] == category:
                allowed[key] = value
        candidate = detect_icons(prepared.resize((TILE_PX, TILE_PX)), allowed, [[0, 0, TILE_PX, TILE_PX]], bank=bank, executor=candidate_executor)[0]
        icon['contrast_attempt' if enhanced is not None else 'native_normalized_attempt'] = candidate
        if candidate['id'] is not None:
            # An alternate ID must explain both glyph components, not just a
            # rocket-shaped subset of a washed-out icon.
            if icon['id'] is None or icon['id'] == candidate['id'] or candidate.get('decision') == 'discriminating_details':
                icon.update(id=candidate['id'], decision='contrast' if enhanced is not None else 'native_normalized', conflict=False)
            else:
                icon.update(id=None, decision='preprocessing_conflict', conflict=True)
        return icon

    enhanced_icons = ordered_map(None if candidate_executor is not None else executor, enhance, enhancement_jobs)
    for (i, _, _), icon in zip(enhancement_jobs, enhanced_icons):
        icons[i] = icon
    from .colorless_icons import match_colorless
    for tile, icon in zip(frame_tiles, icons):
        if icon['id'] is None and not icon.get('conflict') and not icon.get('occluded'):
            attempt = match_colorless(tile.image, entries)
            icon['colorless_attempt'] = attempt
            if attempt['id'] is not None:
                icon.update(id=attempt['id'], decision='colorless')
    rows = []
    for i, ((x, y, size, _), icon) in enumerate(zip(frames, icons)):
        icon.update(box=[x, y, size, size], method='mission-icon')
        # Names can be scrambled into plausible text; a decisive icon wins.
        if icon['id'] is not None:
            if (cache is not None and i not in cached_indices
                    and not icon.get('conflict') and not icon.get('occluded')):
                cache.put(cache_keys[i], icon)
            rows.append(icon)
            continue
        rows.append(icon)
    return sorted(rows, key=lambda r: r['box'][1])


def silhouette(gray, threshold):
    """Center a thresholded shape on a fixed canvas for shape-overlap scoring.

    Scales the foreground's bounding box to fit within SILHOUETTE_EXTENT of the
    SILHOUETTE_CANVAS pixel canvas, then blurs it. An empty mask returns a blank canvas.
    """
    mask = (gray > threshold).astype(np.float32)
    ys, xs = np.where(mask)
    canvas = np.zeros((SILHOUETTE_CANVAS, SILHOUETTE_CANVAS), np.float32)
    if len(xs):
        mask = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        scale = SILHOUETTE_EXTENT / max(mask.shape)
        mask = cv2.resize(mask, (max(1, round(mask.shape[1] * scale)),
                                 max(1, round(mask.shape[0] * scale))), interpolation=cv2.INTER_AREA)
        y, x = (SILHOUETTE_CANVAS - mask.shape[0]) // 2, (SILHOUETTE_CANVAS - mask.shape[1]) // 2
        canvas[y:y + mask.shape[0], x:x + mask.shape[1]] = mask
    return cv2.GaussianBlur(canvas, (3, 3), .7)


def icon_features(rgb):
    """Split a tile into blurred white-glyph and colored-glyph planes for correlation.

    rgb is an RGB array. Returns a two-channel float32 array stacking the white mask and
    the colored mask, the layout icon_details and match_equipped expect.
    """
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    saturation, value = hsv[:, :, 1], hsv[:, :, 2]
    # Preserve pale-cyan glyphs as colored shapes in bright game captures.
    white = ((value > 170) & (saturation < 60)).astype(np.float32)
    color = ((value > 90) & (saturation >= 60)).astype(np.float32)
    return cv2.GaussianBlur(np.dstack((white, color)), (3, 3), .6)


def icon_details(features):
    """Reduce icon_features output to two comparable component silhouettes.

    Returns silhouettes for the white glyph and the largest connected colored region, so
    a separate ammo or backpack badge does not dilute the weapon shape.
    """
    color = (features[:, :, 1] > .5).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(color)
    if count > 1:
        # Ammo/backpack badges are separate from the weapon silhouette.
        color = (labels == 1 + stats[1:, cv2.CC_STAT_AREA].argmax()).astype(np.uint8)
    return [silhouette((features[:, :, 0] * 255).astype(np.uint8), 127),
            silhouette(color * 255, 127)]


def _normalized_component_correlation(observed, reference):
    """Zero-mean normalized correlation between two equal-shaped arrays.

    Returns -1.0 when the shapes differ or either array has near-zero variance.
    """
    if observed.shape != reference.shape:
        return -1.0
    left = np.asarray(observed, dtype=np.float32).ravel()
    right = np.asarray(reference, dtype=np.float32).ravel()
    left = left - left.mean()
    right = right - right.mean()
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator <= 1e-6:
        return -1.0
    return float(np.dot(left, right) / denominator)


class IconTemplates:
    """Prepared catalog assets shared across all matching passes in one scan."""

    def __init__(self, *, cache=None):
        self.cache = cache
        self.lock = RLock()
        self.assets = {}
        self.scales = {}

    def get(self, key):
        """Load and cache one catalog icon's derived matching assets by key.

        Reads assets/icons/<key>.png on first use; later calls return the cached dict
        with category, gray, silhouette, features and details. Thread-safe.
        """
        with self.lock:
            if key not in self.assets:
                source = cv2.imread(str(ROOT / 'assets/icons' / f'{key}.png'))
                rgb = cv2.cvtColor(source, cv2.COLOR_BGR2RGB)
                gray = cv2.cvtColor(source[TEMPLATE_INTERIOR, TEMPLATE_INTERIOR], cv2.COLOR_BGR2GRAY)
                features = icon_features(rgb[TEMPLATE_INTERIOR, TEMPLATE_INTERIOR])
                self.assets[key] = dict(category=icon_category(Image.fromarray(rgb)),
                                        gray=gray, silhouette=silhouette(gray, 85),
                                        features=features, details=icon_details(features))
            return self.assets[key]

    def scaled(self, key, size, kind):
        """Return a size-scaled copy of one cached asset, resizing only once per size.

        For kind 'gray' returns a (resized, Canny-edge) pair; other kinds return just
        the resized array. Thread-safe.
        """
        with self.lock:
            identity = key, int(size), kind
            if identity not in self.scales:
                scaled = cv2.resize(self.get(key)[kind], (size, size))
                self.scales[identity] = (scaled, cv2.Canny(scaled, 40, 100)) if kind == 'gray' else scaled
            return self.scales[identity]



def restore_mission_result(saved, candidates):
    """Validate and restore one cached mission-icon result, or reject it.

    Returns None unless saved is a dict with a candidate id, method 'mission-icon',
    conflict and occluded absent or False, and every declared ranking restores
    cleanly.
    """
    if (not isinstance(saved, dict) or not isinstance(saved.get('id'), str)
            or saved['id'] not in candidates or saved.get('method') != 'mission-icon'
            or saved.get('conflict', False) is not False or saved.get('occluded', False) is not False):
        return None

    try:
        restore_rankings(saved)
    except (TypeError, ValueError, RecursionError):
        return None
    return saved


def restore_equipped_result(saved, candidates):
    """Malformed disposable cache records must behave like misses."""
    if (not isinstance(saved, dict) or not isinstance(saved.get('id'), str)
            or saved['id'] not in candidates or saved.get('conflict') is not False
            or not isinstance(saved.get('decision'), str)
            or not isinstance(saved.get('detail_components'), dict)):
        return None
    required = (DETAIL_TOP3, SHORTLIST_TOP3 if SHORTLIST_TOP3 in saved else FEATURE_TOP3)
    for field in required:
        ranking = restore_ranking(saved.get(field), pair_types=(list, tuple), min_length=2,
                                  candidates=candidates)
        if ranking is None:
            return None
        saved[field] = ranking
    return saved


def match_equipped(rgb, references, bank=None, *, fast_filter=True, executor=None):
    """Identify one icon tile of 125px or more among references, checking the cache first.

    rgb is an RGB array of one tile. references is a list of (key, features, details)
    tuples in the layout icon_features/icon_details produce. Caches the result under
    bank.cache when given and the match is decisive.
    """
    features = icon_features(rgb[EQUIPPED_FEATURE_INSET:-EQUIPPED_FEATURE_INSET,
                                 EQUIPPED_FEATURE_INSET:-EQUIPPED_FEATURE_INSET])
    details = icon_details(icon_features(rgb[EQUIPPED_DETAIL_INSET:-EQUIPPED_DETAIL_INSET,
                                             EQUIPPED_DETAIL_INSET:-EQUIPPED_DETAIL_INSET]))
    cache = bank.cache if bank is not None else None
    cache_key = None
    if cache is not None:
        # These arrays are every pixel-derived input consumed by this matcher.
        # Exact normalized features can repeat despite different RGB brightness.
        cache_key = cache.key(features.tobytes() + b''.join(part.tobytes() for part in details),
                              {'matcher': 'equipped', 'shape': list(rgb.shape),
                               'candidates': [r[0] for r in references], 'fast_filter': fast_filter})
        saved = restore_equipped_result(cache.get(cache_key), {r[0] for r in references})
        if saved is not None:
            return saved
    result = _match_equipped(rgb.shape[1], features, details, references, bank,
                             fast_filter=fast_filter, executor=executor)
    if cache is not None and result['id'] is not None and not result.get('conflict'):
        cache.put(cache_key, result)
    return result


# Decision branches of _match_equipped, in evaluation order: eight decision values.
# match_equipped can instead return an earlier cached result; it stores only results with an
# id and no conflict.
#
# Scores. Component overlap: sum(min) / sum(max) of each of the two detail silhouettes (white
# glyph, largest colored part) against a candidate. Shape: the mean of the two overlaps, which
# ranks `shapes`. Correlation: the best TM_CCOEFF_NORMED of the features over 13 template sizes
# from .50 to .89 of the tile width, which ranks `correlation`. Margin: rank 1 minus rank 2.
#
# | # | decision                | id                          | conflict       | evidence      |
# |---|-------------------------|-----------------------------|----------------|---------------|
# | 1 | insufficient_candidates | None                        | False          | none          |
# | 2 | silhouette_verified     | top shape key               | False          | shortlist, D  |
# | 3 | threshold               | the vote, if votes agree    | votes disagree | feature, D    |
# | 4 | near_exact_consensus    | best correlation key        | False          | feature, D    |
# | 5 | discriminating_details  | top shape key               | False          | feature, D    |
# | 6 | component_consensus     | best correlation key        | False          | feature, D    |
# | 7 | component_conflict      | None                        | True           | feature, D    |
# | 8 | near_exact_component    | component winner K          | False          | feature, D    |
#
# Evidence: shortlist = shortlist_top3, feature = feature_top3, D = detail_top3 plus
# detail_components for the top three shapes.
#
# 1. Fewer than two candidates; returns early. Every later margin needs a runner-up.
# 2. Runs only when fast_filter is on, top shape >= .85, shape margin >= .15, and both of its
#    overlaps >= .80. Correlates a shortlist: the 8 best shapes plus any key with an overlap
#    >= .65. Returns when the best shortlist correlation is the top shape key, >= .93, with
#    margin >= .10; otherwise its scores are reused below. Intent (comments): skip the full
#    catalog for well-preserved glyphs, but keep shared-component rivals for conflict checks.
# 3. Correlates the remaining candidates and starts decision "threshold". Correlation votes for
#    its top key at >= .80 with margin >= .06; shape votes for its top key at >= .80 with
#    margin >= .05. Rows 4, 5, 6 and the 7/8 block are one if/elif/else chain after the votes;
#    whatever none of them replaces is returned here. Intent not documented.
# 4. Best correlation >= .95, its residual (1 - score) <= .5 of the runner-up's, the same key
#    tops shape, and top shape >= .85. Replaces the votes. Intent (comment): a near-exact fit
#    can be decisive even among similar shield icons.
# 5. Top shape key differs from the best correlation key, both top shape overlaps >= .84,
#    shape margin >= .15, both best correlation key overlaps < .75, and the top shape key's
#    correlation >= .70. Replaces the votes. Intent (comment): whole-icon alignment can favor
#    a different weapon, so both localized components must refute it.
# 6. No votes, best correlation >= .95, the same key tops shape, top shape >= .85, and both
#    its overlaps >= .80. Each component's top overlap key wins at >= .80 with margin >= .05;
#    accepts only when the winners are exactly the best key. If they are not, the result stays
#    "threshold" with id None and rows 7-8 are not evaluated. Intent (comment): shared shapes
#    dilute a distinctive badge's margin, so one decisive, uncontested component is required.
# 7-8. Only when rows 4-6 do not match. Per component, a zero-mean correlation winner at >= .95
#    and an overlap winner at >= .80, each with margin >= .05, must be the same single key K.
#    K is corroborated when it tops shape, top shape >= .85, both K overlaps >= .80, and K's
#    correlation >= .80. Otherwise the votes from row 3 stand.
# 7. Corroborated and votes exist that are not all K: K is added, making a conflict. Intent not
#    documented.
# 8. Corroborated, no votes, and best correlation minus K's correlation < .06: accepts K.
#    Intent not documented.
def _match_equipped(width, features, details, references, bank, *, fast_filter, executor):
    """Score references against one equipped icon tile per the branch table above."""
    shapes, components = [], {}
    for key, _, reference in references:
        components[key] = [float(np.minimum(a, b).sum() / (np.maximum(a, b).sum() + 1e-6))
                           for a, b in zip(details, reference)]
        shapes.append((sum(components[key]) / 2, key))
    shapes.sort(reverse=True)
    if len(shapes) < 2:
        return {"id": None, "conflict": False, "decision": "insufficient_candidates"}

    def correlate(reference):
        key, template, _ = reference
        best = -1
        for size in np.linspace(int(width * .50), int(width * .89), 13).astype(int):
            if size > min(features.shape[:2]):
                continue
            scaled = bank.scaled(key, size, 'features') if bank is not None else cv2.resize(template, (size, size))
            scores = cv2.matchTemplate(features, scaled, cv2.TM_CCOEFF_NORMED)
            best = max(best, float(scores.max()))
        return best, key

    scored = {}
    # Only distinctive, well-preserved glyph components qualify. Verify their
    # alignment against the closest silhouettes before skipping the full catalog.
    if (fast_filter and shapes[0][0] >= .85 and shapes[0][0] - shapes[1][0] >= .15
            and min(components[shapes[0][1]]) >= .80):
        shortlist = {key for _, key in shapes[:8]}
        # A shared weapon or badge component is a possible rival even when its
        # combined silhouette ranks poorly; retain it for conflict checking.
        shortlist.update(key for key, parts in components.items() if max(parts) >= .65)
        ranked = sorted(ordered_map(executor, correlate,
                                    [r for r in references if r[0] in shortlist]), reverse=True)
        scored = {key: score for score, key in ranked}
        if (ranked[0][1] == shapes[0][1] and ranked[0][0] >= .93
                and ranked[0][0] - ranked[1][0] >= .10):
            return {"id": ranked[0][1], SHORTLIST_TOP3: ranked[:3],
                    DETAIL_TOP3: shapes[:3], "conflict": False,
                    "decision": "silhouette_verified",
                    "detail_components": {key: components[key] for _, key in shapes[:3]}}
    correlation = [(score, key) for key, score in scored.items()]
    correlation.extend(ordered_map(executor, correlate, [r for r in references if r[0] not in scored]))
    correlation.sort(reverse=True)
    accepted = []
    decision = "threshold"
    for ranking, margin in [(correlation, .06), (shapes, .05)]:
        if ranking[0][0] >= .80 and ranking[0][0] - ranking[1][0] >= margin:
            accepted.append(ranking[0][1])
    best_score, best_key = correlation[0]
    detail_score, detail_key = shapes[0]
    # A near-exact fit can be decisive even among similar shield icons.
    residual_ratio = (1 - best_score) / max(1e-6, 1 - correlation[1][0])
    if (best_score >= .95 and residual_ratio <= .5
            and detail_key == best_key and detail_score >= .85):
        accepted = [best_key]
        decision = "near_exact_consensus"
    # Whole-icon alignment can favor a different weapon when badges or
    # proportions differ. Require both localized components to refute it.
    elif (detail_key != best_key and min(components[detail_key]) >= .84
          and detail_score - shapes[1][0] >= .15
          and max(components[best_key]) < .75
          and dict((key, score) for score, key in correlation)[detail_key] >= .70):
        accepted = [detail_key]
        decision = "discriminating_details"
    elif (not accepted and best_score >= .95 and detail_key == best_key
          and detail_score >= .85 and min(components[best_key]) >= .80):
        # Shared equipment shapes can dilute a distinctive badge's margin.
        # Require a decisive component and no competing component decision.
        component_winners = set()
        for index in range(2):
            ranking = sorted((parts[index], key) for key, parts in components.items())
            if ranking[-1][0] >= .80 and ranking[-1][0] - ranking[-2][0] >= .05:
                component_winners.add(ranking[-1][1])
        if component_winners == {best_key}:
            accepted = [best_key]
            decision = "component_consensus"
    else:
        correlations = {
            key: [_normalized_component_correlation(a, b)
                  for a, b in zip(details, reference)]
            for key, _, reference in references
        }

        def winners(scores, threshold):
            found = set()
            for index in range(2):
                ranking = sorted((parts[index], key)
                                 for key, parts in scores.items())
                if (ranking[-1][0] >= threshold
                        and ranking[-1][0] - ranking[-2][0] >= .05):
                    found.add(ranking[-1][1])
            return found

        correlation_winners = winners(correlations, .95)
        overlap_winners = winners(components, .80)
        if len(correlation_winners) == 1 and correlation_winners == overlap_winners:
            component_key = next(iter(correlation_winners))
            feature_score = dict(
                (key, score) for score, key in correlation)[component_key]
            corroborated = (detail_key == component_key and detail_score >= .85
                            and min(components[component_key]) >= .80
                            and feature_score >= .80)
            if corroborated and accepted and set(accepted) != {component_key}:
                accepted.append(component_key)
                decision = "component_conflict"
            elif (corroborated and not accepted
                  and best_score - feature_score < .06):
                accepted = [component_key]
                decision = "near_exact_component"
    conflict = len(set(accepted)) > 1
    return {"id": accepted[0] if accepted and not conflict else None,
            FEATURE_TOP3: correlation[:3], DETAIL_TOP3: shapes[:3], "conflict": conflict,
            "decision": decision,
            "detail_components": {key: components[key] for _, key in shapes[:3]}}


def detect_icons(im, entries, boxes, *, mission=False, _normalized=False, bank=None, executor=None, cache=None):
    """Identify the icon in each box of im, retrying via normalization when unresolved.

    boxes are (x, y, width, height) in im's pixels. Returns one row dict per box, each
    with id, box and method; a row with no id and no conflict after equipped/gray
    matching is retried once against a color-normalized copy of its crop, unless
    _normalized is set.
    """
    pixels = np.array(im.convert("RGB"))
    bank = bank if bank is not None else IconTemplates(cache=cache)
    templates, references, categories = [], [], {}
    for key in entries:
        asset = bank.get(key)
        categories[key] = asset['category']
        templates.append((key, asset['gray'], asset['silhouette']))
        references.append((key, asset['features'], asset['details']))
    boxes = list(boxes)
    candidate_executor = executor if len(boxes) == 1 else None
    row_executor = None if candidate_executor is not None else executor
    def match_box(box):
        """Identify one box via equipped-detail matching, then gray/edge correlation.

        Falls back to gray/edge template correlation and silhouette overlap when the
        box is under 125px, or when match_equipped found no id and no conflict.
        Returns a dict with id (None when fewer than two candidates remain, or when
        the correlation and shape rankings disagree), box, method and the evidence
        rankings.
        """
        x, y, width, height = box
        category = icon_category(im.crop((x, y, x + width, y + height)), frame=True) if mission else None
        candidates = {key for key in entries if category is None or categories[key] == category}
        detail = {'icon_category': category}
        if min(width, height) >= 125:
            detail.update(match_equipped(pixels[y:y + height, x:x + width],
                                         [r for r in references if r[0] in candidates], bank=bank, executor=candidate_executor))
            if detail["id"] is not None or detail["conflict"]:
                return {**detail, "box": [x, y, width, height], "method": "icon-details"}
        gray = cv2.cvtColor(pixels[y + ICON_GRAY_INSET:y + height - ICON_GRAY_INSET,
                                   x + ICON_GRAY_INSET:x + width - ICON_GRAY_INSET], cv2.COLOR_RGB2GRAY)
        edge = cv2.Canny(gray, 40, 100)
        mask = silhouette(cv2.cvtColor(pixels[y + ICON_SILHOUETTE_INSET:y + height - ICON_SILHOUETTE_INSET,
                                              x + ICON_SILHOUETTE_INSET:x + width - ICON_SILHOUETTE_INSET],
                                      cv2.COLOR_RGB2GRAY), 120)
        def correlate_gray(item):
            """Score one catalog key's gray/edge template and shape overlap against box.

            Returns ((correlation, key), (shape, key)) for the joint threshold/margin
            pass below.
            """
            key, template, reference = item
            best = -1
            for size in np.linspace(int(width * .50), int(width * .90), 9).astype(int):
                if size > min(gray.shape):
                    continue
                scaled, scaled_edge = bank.scaled(key, size, 'gray')
                g = cv2.matchTemplate(gray, scaled, cv2.TM_CCOEFF_NORMED)
                e = cv2.matchTemplate(edge, scaled_edge, cv2.TM_CCORR_NORMED)
                best = max(best, float((.65 * g + .35 * e).max()))
            shape = float(np.minimum(mask, reference).sum() / (np.maximum(mask, reference).sum() + 1e-6))
            return (best, key), (shape, key)
        scored = ordered_map(candidate_executor, correlate_gray, [t for t in templates if t[0] in candidates])
        if len(scored) < 2:
            return {**detail, 'id': None, 'box': list(box), 'method': 'icon'}
        correlations, shapes = (list(values) for values in zip(*scored))
        correlations.sort(reverse=True)
        shapes.sort(reverse=True)
        accepted = []
        for ranking, threshold, margin in [(correlations, .65, .12), (shapes, .80, .12)]:
            if ranking[0][0] >= threshold and ranking[0][0] - ranking[1][0] >= margin:
                accepted.append(ranking[0][1])
        key = accepted[0] if accepted and len(set(accepted)) == 1 else None
        return {**detail, "id": key, "box": [x, y, width, height], "method": "icon",
                     CORRELATION_TOP3: correlations[:3], SILHOUETTE_TOP3: shapes[:3]}

    rows = ordered_map(row_executor, match_box, boxes)
    if not _normalized:
        unresolved = [i for i, row in enumerate(rows) if row['id'] is None and not row.get('conflict')]
        normalization_executor = executor if len(unresolved) == 1 else None
        def normalize_row(row):
            """Retry an unresolved row against a color-normalized copy of its crop.

            Mutates row in place by adding 'normalized_attempt'; returns a replacement
            dict when the retry resolves an id, otherwise returns the same row.
            """
            x, y, w, h = row['box']
            crop = im.crop((x, y, x + w, y + h))
            if mission:
                # match_box read this row's frame category from the same crop of the same image.
                tile = normalize_icon(crop, mission=True, frame_category=row['icon_category'])
            else:
                tile = normalize_icon(crop, mission=mission)
            tile = tile.resize((TILE_PX, TILE_PX))
            allowed = {key: value for key, value in entries.items()
                       if row.get('icon_category') is None or categories[key] == row['icon_category']}
            candidate = detect_icons(tile, allowed, [[0, 0, TILE_PX, TILE_PX]],
                                     mission=mission, _normalized=True, bank=bank, executor=normalization_executor)[0]
            row['normalized_attempt'] = candidate
            if candidate['id'] is not None:
                return {**candidate, 'box': [x, y, w, h], 'method': 'normalized-icon',
                           'icon_category': row.get('icon_category')}
            return row
        normalized = ordered_map(None if normalization_executor is not None else executor,
                                 normalize_row, [rows[i] for i in unresolved])
        for i, row in zip(unresolved, normalized):
            rows[i] = row

    return rows
