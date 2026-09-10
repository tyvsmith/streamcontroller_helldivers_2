"""Offline screenshot recognition without StreamController writes."""

import json
from pathlib import Path
from threading import RLock

import cv2
import numpy as np
from PIL import Image

from .mission_references import match_reference
from .icon_normalization import normalize_icon, icon_category, icon_occluded, stretch_icon
from .mission_layout import (border_track_edges, complete_frames,
                             frame_track_strength, valid_row_lattice)


ROOT = Path(__file__).resolve().parents[2]


def catalog():
    sequences = json.loads((ROOT / "assets/data/stratagems.json").read_text())
    locale = json.loads((ROOT / "locales/en_US.json").read_text())
    return {
        key: {"name": locale[f"actions.{key}.name"], "sequence": sequence}
        for key, sequence in sequences.items()
    }


def _mission_frames_at(im, pixels, x, size, scale):
    """Read one observed border track without choosing between tracks."""
    r, g, b = (pixels[:, x:x + max(1, round(5 * scale)), i] for i in range(3))
    colors = (((r > 245) & (g > 245) & (b < 190))
              | ((g > 245) & (b > 245) & (r < 200))
              | ((g > 245) & (r < 220) & (b < 190))
              | ((r > 245) & (g < 200) & (b < 180)))
    signal = colors.mean(axis=1) > .6
    runs, start = [], None
    for y, present in enumerate(np.r_[signal, False]):
        if present and start is None:
            start = y
        elif not present and start is not None:
            if .8 * size <= y - start <= 1.2 * size and start > im.height * .06:
                # A dim lower edge can shorten the bright run; keep the observed top.
                runs.append([x, start, size, size])
            start = None
    thickness = max(1, round(5 * scale))
    hsv = cv2.cvtColor(pixels[:, x:x + thickness], cv2.COLOR_RGB2HSV)
    contrast = np.linalg.norm(pixels[:, x:x + thickness].astype(float)
                              - pixels[:, x - thickness:x].astype(float), axis=2)
    signal = (((hsv[:, :, 1] > 20) & (hsv[:, :, 2] > 120)
               & (contrast > 25)).mean(axis=1) >= .5).astype(np.uint8)
    signal = cv2.morphologyEx(signal.reshape(-1, 1), cv2.MORPH_CLOSE,
                              np.ones((max(1, round(9 * scale)), 1), np.uint8)).ravel()
    start = None
    for y, present in enumerate(np.r_[signal, False]):
        if present and start is None:
            start = y
        elif not present and start is not None:
            if (.8 * size <= y - start <= 1.2 * size and start > im.height * .06
                    and not any(abs(start - row[1]) < size * .6 for row in runs)):
                runs.append([x, start, size, size])
            start = None
    # Covered icons lose their colored upper border but retain a white rectangle.
    signal = (((hsv[:, :, 2] > 120) & (contrast > 25)).mean(axis=1) >= .5).astype(np.uint8)
    signal = cv2.morphologyEx(signal.reshape(-1, 1), cv2.MORPH_CLOSE,
                              np.ones((max(1, round(9 * scale)), 1), np.uint8)).ravel()
    start = None
    for y, present in enumerate(np.r_[signal, False]):
        if present and start is None:
            start = y
        elif not present and start is not None:
            if (.8 * size <= y - start <= 1.2 * size and start > im.height * .06
                    and not any(abs(start - row[1]) < size * .6 for row in runs)
                    and icon_occluded(im.crop((x, start, x + size, start + size)))):
                runs.append([x, start, size, size])
            start = None
    runs = complete_frames(pixels, runs, x, size, 104.4 * scale, im.height * .06)
    return (sorted(runs, key=lambda r: r[1])
            if valid_row_lattice(runs, 104.4 * scale, size) else [])


def mission_frames(im):
    """Locate one observed border track in the supported 5120x2160 HUD area."""
    if abs(im.width / im.height - 5120 / 2160) > .005:
        return []
    scale = im.height / 2160
    expected_x = round(im.width * 104 / 5120)
    size = round(87 * scale)
    pixels = np.array(im.convert("RGB"))[:round(im.height * .53)]
    pitch = 104.4 * scale
    groups = border_track_edges(pixels, expected_x, size, pitch,
                                round(30 * scale), scale)
    candidates = []
    for group in groups:
        probe_start = group[0] - max(3, round(7 * scale))
        probes = range(probe_start, group[0] + 1)
        attempts = []
        for x in probes:
            if x < 1 or x + size > im.width:
                continue
            rows = _mission_frames_at(im, pixels, x, size, scale)
            attempts.append((len(rows), frame_track_strength(pixels, rows), x, rows))
        if not attempts:
            continue
        quality = max((count, strength) for count, strength, _, _ in attempts)
        best = [attempt for attempt in attempts if attempt[:2] == quality]
        _, _, x, rows = best[(len(best) - 1) // 2]
        if rows:
            candidates.append((x, rows))
    if len(candidates) != 1:
        return []
    return candidates[0][1]



def ordered_map(executor, function, values):
    return list(executor.map(function, values)) if executor is not None else list(map(function, values))


def detect_mission_icons(im, entries, *, executor=None, cache=None):
    bank = IconTemplates(cache=cache)
    frames = mission_frames(im)
    if not frames:
        return []
    tiles = Image.new('RGB', (150 * len(frames), 150))
    for i, (x, y, size, _) in enumerate(frames):
        tiles.paste(im.crop((x, y, x + size, y + size)).resize((150, 150)), (150 * i, 0))
    icons, cache_keys, cached_indices = [], [], set()
    for i, (x, y, size, _) in enumerate(frames):
        tile = im.crop((x, y, x + size, y + size)).convert('RGB')
        cache_key = None
        saved = None
        if cache is not None:
            # The complete pipeline also uses raw contrast/reference detail, so
            # this cache requires exact RGB pixels, not just normalized features.
            cache_key = cache.key(tile.tobytes(), {'matcher': 'mission-complete',
                                  'box': [x, y, size, size], 'index': i, 'candidates': list(entries)})
            saved = restore_mission_result(cache.get(cache_key), entries)
        cache_keys.append(cache_key)
        if saved is not None:
            icons.append(saved)
            cached_indices.add(i)
        else:
            icons.append({'id': None, 'occluded': True,
                          'text': 'Icon obscured by a solid white overlay'}
                         if icon_occluded(tile) else match_reference(tile, entries))
    unresolved = [i for i, icon in enumerate(icons) if icon['id'] is None and not icon.get('occluded')]
    if unresolved:
        fallback = detect_icons(tiles, entries, [[150 * i, 0, 150, 150] for i in unresolved], mission=True, bank=bank, executor=executor)
        for i, icon in zip(unresolved, fallback):
            icons[i] = {**icons[i], **icon}
    enhancement_jobs = []
    for i, ((x, y, size, _), icon) in enumerate(zip(frames, icons)):
        if i in cached_indices or icon.get('occluded') or icon.get('conflict'):
            continue
        tile = im.crop((x, y, x + size, y + size))
        enhanced = stretch_icon(tile)
        if enhanced is not None or icon['id'] is None:
            enhancement_jobs.append((i, tile, enhanced))
    # One costly row can use the pool for candidates. Multiple rows own the pool
    # themselves; workers never submit nested work to the same executor.
    candidate_executor = executor if len(enhancement_jobs) == 1 else None
    def enhance(job):
        i, tile, enhanced = job
        icon = icons[i]
        # Normalize before interpolation mixes the pale glyph with its background.
        prepared = enhanced if enhanced is not None else normalize_icon(tile, mission=True)
        category = icon_category(tile, frame=True)
        allowed = {}
        for key, value in entries.items():
            if category is None or bank.get(key)['category'] == category:
                allowed[key] = value
        candidate = detect_icons(prepared.resize((150, 150)), allowed, [[0, 0, 150, 150]], bank=bank, executor=candidate_executor)[0]
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
    for (x, y, size, _), icon in zip(frames, icons):
        if icon['id'] is None and not icon.get('conflict') and not icon.get('occluded'):
            attempt = match_colorless(im.crop((x, y, x + size, y + size)), entries)
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
    mask = (gray > threshold).astype(np.float32)
    ys, xs = np.where(mask)
    canvas = np.zeros((64, 64), np.float32)
    if len(xs):
        mask = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        scale = 56 / max(mask.shape)
        mask = cv2.resize(mask, (max(1, round(mask.shape[1] * scale)),
                                 max(1, round(mask.shape[0] * scale))), interpolation=cv2.INTER_AREA)
        y, x = (64 - mask.shape[0]) // 2, (64 - mask.shape[1]) // 2
        canvas[y:y + mask.shape[0], x:x + mask.shape[1]] = mask
    return cv2.GaussianBlur(canvas, (3, 3), .7)


def icon_features(rgb):
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    saturation, value = hsv[:, :, 1], hsv[:, :, 2]
    # Preserve pale-cyan glyphs as colored shapes in bright game captures.
    white = ((value > 170) & (saturation < 60)).astype(np.float32)
    color = ((value > 90) & (saturation >= 60)).astype(np.float32)
    return cv2.GaussianBlur(np.dstack((white, color)), (3, 3), .6)


def icon_details(features):
    color = (features[:, :, 1] > .5).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(color)
    if count > 1:
        # Ammo/backpack badges are separate from the weapon silhouette.
        color = (labels == 1 + stats[1:, cv2.CC_STAT_AREA].argmax()).astype(np.uint8)
    return [silhouette((features[:, :, 0] * 255).astype(np.uint8), 127),
            silhouette(color * 255, 127)]


class IconTemplates:
    """Prepared catalog assets shared across all matching passes in one scan."""

    def __init__(self, *, cache=None):
        self.cache = cache
        self.lock = RLock()
        self.assets = {}
        self.scales = {}

    def get(self, key):
        with self.lock:
            if key not in self.assets:
                source = cv2.imread(str(ROOT / 'assets/icons' / f'{key}.png'))
                rgb = cv2.cvtColor(source, cv2.COLOR_BGR2RGB)
                gray = cv2.cvtColor(source[23:121, 23:121], cv2.COLOR_BGR2GRAY)
                features = icon_features(rgb[23:121, 23:121])
                self.assets[key] = dict(category=icon_category(Image.fromarray(rgb)),
                                        gray=gray, silhouette=silhouette(gray, 85),
                                        features=features, details=icon_details(features))
            return self.assets[key]

    def scaled(self, key, size, kind):
        with self.lock:
            identity = key, int(size), kind
            if identity not in self.scales:
                scaled = cv2.resize(self.get(key)[kind], (size, size))
                self.scales[identity] = (scaled, cv2.Canny(scaled, 40, 100)) if kind == 'gray' else scaled
            return self.scales[identity]



def restore_mission_result(saved, candidates):
    if (not isinstance(saved, dict) or not isinstance(saved.get('id'), str)
            or saved['id'] not in candidates or saved.get('method') != 'mission-icon'
            or saved.get('conflict', False) is not False or saved.get('occluded', False) is not False):
        return None

    def restore(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith('_top3'):
                    if (not isinstance(item, list) or any(not isinstance(pair, list) or len(pair) != 2
                            or not isinstance(pair[0], (int, float)) or not np.isfinite(pair[0])
                            or not isinstance(pair[1], str) for pair in item)):
                        raise ValueError('invalid cached ranking')
                    value[key] = [tuple(pair) for pair in item]
                else:
                    restore(item)
        elif isinstance(value, list):
            for item in value:
                restore(item)
    try:
        restore(saved)
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
    required = ('detail_top3', 'shortlist_top3' if 'shortlist_top3' in saved else 'feature_top3')
    for field in required:
        ranking = saved.get(field)
        if not isinstance(ranking, list) or len(ranking) < 2:
            return None
        if any(not isinstance(item, (list, tuple)) or len(item) != 2
               or not isinstance(item[0], (int, float)) or not np.isfinite(item[0])
               or not isinstance(item[1], str) or item[1] not in candidates for item in ranking):
            return None
        saved[field] = [tuple(item) for item in ranking]
    return saved


def match_equipped(rgb, references, bank=None, *, fast_filter=True, executor=None):
    features = icon_features(rgb[8:-8, 8:-8])
    details = icon_details(icon_features(rgb[12:-12, 12:-12]))
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


def _match_equipped(width, features, details, references, bank, *, fast_filter, executor):
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
            return {"id": ranked[0][1], "shortlist_top3": ranked[:3],
                    "detail_top3": shapes[:3], "conflict": False,
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
    conflict = len(set(accepted)) > 1
    return {"id": accepted[0] if accepted and not conflict else None,
            "feature_top3": correlation[:3], "detail_top3": shapes[:3], "conflict": conflict,
            "decision": decision,
            "detail_components": {key: components[key] for _, key in shapes[:3]}}


def detect_icons(im, entries, boxes, *, mission=False, _normalized=False, bank=None, executor=None, cache=None):
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
        x, y, width, height = box
        category = icon_category(im.crop((x, y, x + width, y + height)), frame=True) if mission else None
        candidates = {key for key in entries if category is None or categories[key] == category}
        detail = {'icon_category': category}
        if min(width, height) >= 125:
            detail.update(match_equipped(pixels[y:y + height, x:x + width],
                                         [r for r in references if r[0] in candidates], bank=bank, executor=candidate_executor))
            if detail["id"] is not None or detail["conflict"]:
                return {**detail, "box": [x, y, width, height], "method": "icon-details"}
        gray = cv2.cvtColor(pixels[y + 6:y + height - 6, x + 6:x + width - 6], cv2.COLOR_RGB2GRAY)
        edge = cv2.Canny(gray, 40, 100)
        mask = silhouette(cv2.cvtColor(pixels[y + 12:y + height - 12, x + 12:x + width - 12],
                                      cv2.COLOR_RGB2GRAY), 120)
        def correlate_gray(item):
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
                     "correlation_top3": correlations[:3], "silhouette_top3": shapes[:3]}

    rows = ordered_map(row_executor, match_box, boxes)
    if not _normalized:
        unresolved = [i for i, row in enumerate(rows) if row['id'] is None and not row.get('conflict')]
        normalization_executor = executor if len(unresolved) == 1 else None
        def normalize_row(row):
            x, y, w, h = row['box']
            tile = normalize_icon(im.crop((x, y, x + w, y + h)), mission=mission)
            tile = tile.resize((150, 150))
            allowed = {key: value for key, value in entries.items()
                       if row.get('icon_category') is None or categories[key] == row['icon_category']}
            candidate = detect_icons(tile, allowed, [[0, 0, 150, 150]],
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
