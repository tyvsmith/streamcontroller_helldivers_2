"""Validate HUD rows against repeated spacing and visible square borders."""
import cv2
import numpy as np


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
