"""Validate HUD rows against repeated spacing and visible square borders."""
import cv2
import numpy as np

from .icon_normalization import icon_occluded


def valid_row_lattice(rows, pitch, size):
    """Require three observed rows to share the calibrated vertical cadence."""
    if len(rows) < 3:
        return False
    ys = np.array([row[1] for row in rows], dtype=float)
    for seed in ys:
        residual = abs((ys - seed) / pitch - np.round((ys - seed) / pitch)) * pitch
        if int((residual < size * .10).sum()) >= 3:
            return True
    return False


def border_track_edges(pixels, expected_x, size, pitch, tolerance, scale):
    """Return distinct vertical-frame edge groups in the supported HUD area."""
    edge_width = max(2, round(5 * scale))
    lower = max(1, expected_x - tolerance)
    upper = min(pixels.shape[1] - 1, expected_x + tolerance + edge_width + 1)
    candidates = []
    for x in range(lower, upper + 1):
        column = pixels[:, x].astype(float)
        contrast = np.linalg.norm(column - pixels[:, x - 1].astype(float), axis=1)
        signal = ((contrast > 25) & (column.max(axis=1) > 120)).astype(np.uint8)
        signal = cv2.morphologyEx(
            signal.reshape(-1, 1), cv2.MORPH_CLOSE,
            np.ones((max(1, round(9 * scale)), 1), np.uint8),
        ).ravel()
        runs = []
        start = None
        for y, present in enumerate(np.r_[signal, False]):
            if present and start is None:
                start = y
            elif not present and start is not None:
                if .72 * size <= y - start <= 1.2 * size:
                    runs.append([x, start, size, size])
                start = None
        if valid_row_lattice(runs, pitch, size):
            candidates.append(x)
    groups = []
    for x in candidates:
        if not groups or x > groups[-1][-1] + edge_width + 1:
            groups.append([x])
        else:
            groups[-1].append(x)
    return groups


def frame_track_strength(pixels, rows):
    """Score how well the recovered boxes align with surviving square borders."""
    if not rows:
        return 0.0
    size = rows[0][2]
    border = max(2, round(size * .057))
    return sum(_cooldown_top(pixels[y:y + size, x:x + size], border)
               for x, y, _, _ in rows)


def _cooldown_top(tile, border):
    """Score a surviving upper square; white cooldown fill may hide its bottom."""
    size = len(tile)
    a, b = round(size * .18), round(size * .63)
    lower = tile[-2 * border:-border, a:b].reshape(-1, 3)
    white_fill = np.median(lower.min(axis=1)) >= 240 and np.median(np.ptp(lower, axis=1)) <= 12
    bottom_contrast = np.median(tile[-border:-1, a:b]) - np.median(lower)
    if not white_fill and bottom_contrast < 12:
        return 0
    # Require both vertical strokes as well as the horizontal stroke. A blank
    # white grid position, or an isolated scenery edge, supplies neither.
    side_start, side_end = ((border, 3 * border) if white_fill
                            else (round(size * .35), round(size * .70)))
    edges = [tile[1:border, a:b], tile[side_start:side_end, 1:border],
             tile[side_start:side_end, -border:-1]]
    inner = [tile[border:2 * border, a:b],
             tile[side_start:side_end, border:2 * border],
             tile[side_start:side_end, -2 * border:-border]]
    colors = np.array([np.median(edge.reshape(-1, 3), axis=0) for edge in edges])
    contrast = [np.median(edge) - np.median(inside) for edge, inside in zip(edges, inner)]
    # A wash can vary vertically. Opposing side strokes still share a color;
    # the upper corner marker can cover the inner right stroke on full icons.
    compared = colors if white_fill else colors[1:]
    if min(contrast) < 12 or np.linalg.norm(compared - np.median(compared, axis=0), axis=1).max() > 18:
        return 0
    # The inner top edge pins the real y even when white scenery above the
    # square merges with its outer border.
    return float(np.median(tile[border - 1, a:b].astype(float)
                           - tile[border, a:b].astype(float)))


def _paired_side_strokes(pixels, x, y, size, border):
    """Require two matching, finite side strokes when the top is obscured."""
    tile = pixels[y:y + size, x:x + size]
    start, end = round(size * .35), round(size * .70)
    edges = [tile[start:end, 1:border], tile[start:end, -border:-1]]
    inner = [tile[start:end, border:2 * border],
             tile[start:end, -2 * border:-border]]
    colors = np.array([
        np.median(edge.reshape(-1, 3), axis=0) for edge in edges
    ])
    contrast = [
        np.median(edge) - np.median(inside)
        for edge, inside in zip(edges, inner)
    ]
    cap = max(border, round(size * .10))
    if y < cap or y + size + cap > len(pixels):
        return False
    before = pixels[y - cap:y, x:x + size]
    after = pixels[y + size:y + size + cap, x:x + size]
    cap_contrast = [
        np.median(edge) - np.median(inside)
        for edge, inside in (
            (before[:, 1:border], before[:, border:2 * border]),
            (before[:, -border:-1], before[:, -2 * border:-border]),
            (after[:, 1:border], after[:, border:2 * border]),
            (after[:, -border:-1], after[:, -2 * border:-border]),
        )
    ]
    return (min(contrast) > 25
            and np.linalg.norm(colors[0] - colors[1]) <= 18
            and max(abs(value) for value in cap_contrast) < 12)


def complete_frames(pixels, runs, x, size, pitch, min_y):
    if len(runs) < 3:
        return runs
    # Fit spacing from observed borders; a predicted location alone is not a row.
    ys = np.array([row[1] for row in runs], dtype=float)
    groups = [abs((ys - seed) / pitch - np.round((ys - seed) / pitch)) * pitch < size * .10
              for seed in ys]
    inliers = max(groups, key=lambda group: int(group.sum()))
    if inliers.sum() < 3:
        return runs
    anchor = ys[inliers].min()
    indices = np.round((ys[inliers] - anchor) / pitch)
    fitted_pitch, anchor = np.polyfit(indices, ys[inliers], 1)
    if abs(fitted_pitch - pitch) > pitch * .05:
        return runs
    pitch = fitted_pitch
    border = max(2, round(size * .057))
    template = np.ones((size, size), np.float32)
    template[border:-border, border:-border] = 0
    gray = cv2.cvtColor(pixels[:, x:x + size], cv2.COLOR_RGB2GRAY).astype(np.float32)
    scores = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED).ravel()
    result = []
    y = anchor - max(0, int((anchor - min_y) // pitch)) * pitch
    while y + size <= len(pixels):
        nearby = [row for row in runs if abs(row[1] - y) < size * .10]
        lo, hi = max(0, round(y - size * .10)), min(len(scores), round(y + size * .10) + 1)
        cooldown = [(candidate, _cooldown_top(pixels[candidate:candidate + size, x:x + size], border))
                    for candidate in range(lo, hi)]
        best, strength = max(cooldown, key=lambda item: item[1], default=(0, 0))
        if strength > 12:
            observed = min(nearby, key=lambda row: abs(row[1] - best), default=None)
            # Preserve an observed outer edge: antialiasing can move the inner
            # edge one pixel. White scenery has no such outer-edge evidence.
            onset = 0 if observed is None else np.linalg.norm(
                pixels[observed[1], x:x + border].astype(float)
                - pixels[observed[1] - 1, x:x + border].astype(float), axis=1).mean()
            preserve = observed is not None and abs(observed[1] - best) <= max(1, round(size * .023)) and onset > 25
            result.append(observed if preserve else [x, best, size, size])
        elif nearby:
            result.append(min(nearby, key=lambda row: abs(row[1] - y)))
        else:
            predicted = round(y)
            if _paired_side_strokes(pixels, x, predicted, size, border):
                result.append([x, predicted, size, size])
                y += pitch
                continue
            lo, hi = max(0, round(y - size * .06)), min(len(scores), round(y + size * .06) + 1)
            for best in sorted(range(lo, hi), key=lambda candidate: scores[candidate], reverse=True):
                if scores[best] < .30:
                    break
                tile = pixels[best:best + size, x:x + size]
                # Uniform border color on all four edges rejects scenery rectangles.
                a, b = round(size * .18), round(size * .63)
                edges = [tile[1:border, a:b], tile[-border:-1, a:b],
                         tile[a:b, 1:border], tile[a:b, -border:-1]]
                colors = np.array([np.median(edge.reshape(-1, 3), axis=0) for edge in edges])
                spread = np.linalg.norm(colors - np.median(colors, axis=0), axis=1).max()
                if spread <= 12:
                    result.append([x, best, size, size])
                    break
        y += pitch
    return result


def _has_menu_row_evidence(pixels, row, scale):
    """Reject a scene edge that only imitates the icon's left border."""
    if frame_track_strength(pixels, [row]) > 0:
        return True
    x, y, size, _ = row
    tile = pixels[y:y + size, x:x + size]
    border = max(2, round(size * .057))
    a, b = round(size * .18), round(size * .63)
    side_start, side_end = round(size * .35), round(size * .70)
    # The left stroke generated this candidate, so only another side can
    # independently corroborate it as a square.
    edges = [tile[1:border, a:b], tile[-border:-1, a:b],
             tile[side_start:side_end, -border:-1]]
    inner = [tile[border:2 * border, a:b], tile[-2 * border:-border, a:b],
             tile[side_start:side_end, -2 * border:-border]]
    stroke_contrast = [np.linalg.norm(
        np.median(edge.reshape(-1, 3), axis=0)
        - np.median(inside.reshape(-1, 3), axis=0))
        for edge, inside in zip(edges, inner)]
    if max(stroke_contrast) > 50:
        return True
    start = x + size + max(1, round(14 * scale))
    end = min(pixels.shape[1], x + 8 * size)
    gap = max(1, round(2 * scale))
    if y < gap or end <= start:
        return False
    adjoining_edge = np.linalg.norm(
        pixels[y, start:end].astype(float)
        - pixels[y - gap, start:end].astype(float), axis=1)
    return float(np.median(adjoining_edge)) <= 25


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
    runs = [row for row in runs if _has_menu_row_evidence(pixels, row, scale)]
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
