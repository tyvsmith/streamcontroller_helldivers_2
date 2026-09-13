"""Read secondary evidence only for unresolved mission rows."""
import csv
from concurrent.futures import CancelledError
import io
import re
import subprocess
import time
from difflib import SequenceMatcher

import cv2
import numpy as np
from PIL import Image, ImageOps

from .recognize.constants import ARROW_GLYPH
from .recognize.match_result import NAME_TOP3


FALLBACK_BUDGET_SECONDS = 8.0

DISPLAY_NAME_ALIASES = {
    'GuardDogRover': ('Rover',),
}


def _raise_if_cancelled(cancel_event):
    """Raise CancelledError once cancel_event is set; do nothing when it is None."""
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError()


def _remaining(deadline):
    """Return seconds left until deadline, or None when there is no deadline."""
    return None if deadline is None else deadline - time.monotonic()


def name_variants(entries):
    """Build per-entry OCR-matchable name variants, keeping only unique ones.

    Each entry's full name is always kept. A short alias, from stripping a model
    number prefix or suffix, is added only when it differs from the full name and
    is at least 6 characters; DISPLAY_NAME_ALIASES entries skip that length
    minimum. Every alias, short or from DISPLAY_NAME_ALIASES, is then dropped
    unless no other entry's variants list the same string. Variant strings keep
    only A-Z and 0-9.
    """
    variants = {}
    for key, entry in entries.items():
        name = entry['name'].upper()
        short = re.sub(r'^(?:[A-Z]*\d+[A-Z]*(?:-[A-Z0-9]+)?|[A-Z]+-\d+[A-Z0-9-]*)\s+', '', name)
        short = re.sub(r'\s+MK\s+[IVXLCDM0-9]+$', '', short)
        full = ''.join(re.findall(r'[A-Z0-9]+', name))
        alias = ''.join(re.findall(r'[A-Z0-9]+', short))
        displayed = [''.join(re.findall(r'[A-Z0-9]+', value.upper()))
                     for value in DISPLAY_NAME_ALIASES.get(key, ())]
        variants[key] = ([full] + ([alias] if alias != full and len(alias) >= 6 else [])
                         + displayed)
    # Abbreviations must identify one catalog entry without competing full names.
    return {key: [name for index, name in enumerate(names) if index == 0 or
                  sum(name in other for other in variants.values()) == 1]
            for key, names in variants.items()}


def match_name(text, entries):
    """Match full names or unique model-free variants with conservative OCR tolerance."""
    words = re.findall(r'[A-Z0-9]+', text.upper())
    hits = []
    for key, targets in name_variants(entries).items():
        for target in targets:
            for start in range(len(words)):
                for end in range(start + 1, min(len(words), start + 8) + 1):
                    candidate = ''.join(words[start:end])
                    score = 1. if candidate == target else 0.
                    if len(target) >= 9 and abs(len(candidate) - len(target)) <= 1:
                        similarity = SequenceMatcher(None, candidate, target).ratio()
                        if similarity >= .94:
                            score = similarity
                    if score:
                        hits.append((score, key, start, end))
    # "Heavy Machine Gun" contains "Machine Gun". Only suppress a shorter
    # name when its actual word span is covered by the longer match.
    hits = [hit for hit in hits if not any(
        other[0] >= hit[0] and other[2] <= hit[2] and other[3] >= hit[3]
        and (other[2] < hit[2] or other[3] > hit[3]) for other in hits)]
    ranking = [(max((hit[0] for hit in hits if hit[1] == key), default=0.), key)
               for key in entries]
    ranking.sort(reverse=True)
    accepted = (len(ranking) >= 2 and ranking[0][0] >= .94
                and ranking[0][0] - ranking[1][0] >= .08)
    return {'id': ranking[0][1] if accepted else None, NAME_TOP3: ranking[:3]}


def read_name(image, box, entries, *, deadline=None, cancel_event=None):
    """Read a mission row's printed name via OCR and match it to one catalog entry.

    box is (x, y, size, size) of the icon tile; the name is read from the region to its
    right. Tries several image variants and Tesseract passes, accepting an id only when
    every attempt that produced one agrees. Returns a dict with id (None on failure),
    name_attempts, and a name_error when OCR failed or the deadline ran out; a set
    cancel_event raises CancelledError instead of returning.
    """
    x, y, size, _ = box
    left = round(x + size * 1.45)
    top, bottom = round(y + size * .06), round(y + size * .43)
    crop = image.crop((left, top, min(image.width, round(left + size * 6.2)), bottom)).convert('RGB')
    rgb = np.asarray(crop)
    mask = (rgb.min(2) > 225) & (rgb.max(2).astype(int) - rgb.min(2) < 35)
    white = Image.fromarray((255 - mask.astype(np.uint8) * 255))
    # Scenery can join the last letter. Retry a shorter line window instead of
    # allowing arbitrary suffixes or partial catalog names.
    variants = [white, white.crop((0, 0, round(white.width * .8), white.height)),
                ImageOps.autocontrast(ImageOps.grayscale(crop))]
    gray = np.asarray(ImageOps.autocontrast(ImageOps.grayscale(crop)))
    _, threshold = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(Image.fromarray(threshold))
    attempts = []
    # Raw-line OCR avoids clipping dim cooldown names during line segmentation.
    passes = [(variant, '7') for variant in variants]
    passes.append((ImageOps.invert(Image.fromarray(gray)), '13'))
    for variant, segmentation in passes:
        _raise_if_cancelled(cancel_event)
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            return {'id': None, 'name_attempts': attempts,
                    'name_error': 'fallback deadline exhausted'}
        if segmentation == '13' and any(attempt['id'] is not None for attempt in attempts):
            break
        data = io.BytesIO()
        variant.resize((variant.width * 2, variant.height * 2)).save(data, format='PNG')
        _raise_if_cancelled(cancel_event)
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            return {'id': None, 'name_attempts': attempts,
                    'name_error': 'fallback deadline exhausted'}
        try:
            result = subprocess.run(['tesseract', 'stdin', 'stdout', '--psm', segmentation, '-l', 'eng', 'tsv'],
                                    input=data.getvalue(), capture_output=True,
                                    timeout=3 if remaining is None else min(3, remaining), check=True)
        except (OSError, subprocess.SubprocessError) as error:
            return {'id': None, 'name_attempts': attempts, 'name_error': str(error)}
        _raise_if_cancelled(cancel_event)
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            return {'id': None, 'name_attempts': attempts,
                    'name_error': 'fallback deadline exhausted'}
        words = list(csv.DictReader(io.StringIO(result.stdout.decode()), delimiter='\t'))
        text = ' '.join(w['text'] for w in words if w.get('level') == '5' and float(w['conf']) >= 0)
        match = match_name(text, entries)
        attempts.append({'text': text, 'segmentation': segmentation, **match})
    ids = {attempt['id'] for attempt in attempts if attempt['id'] is not None}
    _raise_if_cancelled(cancel_event)
    remaining = _remaining(deadline)
    if remaining is not None and remaining <= 0:
        return {'id': None, 'name_attempts': attempts,
                'name_error': 'fallback deadline exhausted'}
    return {'id': next(iter(ids)) if len(ids) == 1 else None, 'name_attempts': attempts}


def arrow_templates():
    """Build rotated arrow-glyph templates for each direction and head length.

    Returns a dict of direction name to a list of ARROW_GLYPH x ARROW_GLYPH uint8 masks,
    one per head-length variant used when scoring a rasterized glyph.
    """
    templates = {name: [] for name in ('UP', 'LEFT', 'DOWN', 'RIGHT')}
    for head in (24, 27):
        up = np.zeros((ARROW_GLYPH, ARROW_GLYPH), np.uint8)
        points = np.array([(20, 0), (39, head), (29, head), (29, 39),
                           (10, 39), (10, head), (0, head)])
        cv2.fillPoly(up, [points], 1)
        for name, rotations in [('UP', 0), ('LEFT', 1), ('DOWN', 2), ('RIGHT', 3)]:
            templates[name].append(np.rot90(up, rotations))
    return templates


def read_arrows(image, box, entries, *, deadline=None, cancel_event=None):
    """Read a mission row's arrow input sequence from its glyph strip and match it.

    box is (x, y, size, size) of the icon tile; glyphs are read from the region to its
    right, sized for the longest sequence in entries. Returns a dict with id (None
    unless the spacing, glyph confidence, sequence and readable tail are all decisive
    and match exactly one candidate), the read observed_sequence, arrow_decision
    naming the outcome ('unique_sequence' on success), and arrow_glyphs debug data.
    """
    _raise_if_cancelled(cancel_event)
    remaining = _remaining(deadline)
    if remaining is not None and remaining <= 0:
        return {'id': None, 'observed_sequence': [],
                'arrow_decision': 'deadline_exhausted', 'arrow_glyphs': []}
    x, y, size, _ = box
    left, top = round(x + size * 1.45), round(y + size * .45)
    width = round(size * .46 * (max(len(e['sequence']) for e in entries.values()) + 1))
    crop = image.crop((left, top, min(image.width, left + width), round(y + size * 1.02))).convert('RGB')
    rgb = np.asarray(crop)
    mask = ((rgb.min(2) >= 240) & (rgb.max(2).astype(int) - rgb.min(2) <= 25)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    glyphs = []
    templates = arrow_templates()
    for index in range(1, count):
        _raise_if_cancelled(cancel_event)
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            return {'id': None, 'observed_sequence': [],
                    'arrow_decision': 'deadline_exhausted', 'arrow_glyphs': glyphs}
        gx, gy, w, h, area = stats[index]
        if not (.26 * size <= w <= .43 * size and .26 * size <= h <= .43 * size):
            continue
        glyph = cv2.resize((labels[gy:gy+h, gx:gx+w] == index).astype(np.uint8), (ARROW_GLYPH, ARROW_GLYPH), interpolation=cv2.INTER_NEAREST)
        ranking = []
        # Rasterization can move an arrow edge by half a source pixel. Compare
        # nearby alignments without relaxing the direction separation threshold.
        aligned = np.pad(glyph.astype(np.float32), 2)
        for name, variants in templates.items():
            scores = []
            for template in variants:
                overlap = float(cv2.matchTemplate(aligned, template.astype(np.float32), cv2.TM_CCORR).max())
                scores.append(overlap / max(float(glyph.sum() + template.sum()) - overlap, 1.))
            ranking.append((max(scores), name))
        ranking.sort(reverse=True)
        glyphs.append((int(gx), int(gy), ranking))
    _raise_if_cancelled(cancel_event)
    remaining = _remaining(deadline)
    if remaining is not None and remaining <= 0:
        return {'id': None, 'observed_sequence': [],
                'arrow_decision': 'deadline_exhausted', 'arrow_glyphs': glyphs}
    glyphs.sort()
    sequence = []
    for index, (gx, gy, ranking) in enumerate(glyphs):
        if ((index == 0 and gx > size * .20)
                or (index and not .34 * size <= gx - glyphs[index-1][0] <= .52 * size)
                or (index and abs(gy - glyphs[0][1]) > size * .08)
                or ranking[0][0] < .80 or ranking[0][0] - ranking[1][0] < .12):
            return {'id': None, 'observed_sequence': [], 'arrow_decision': 'incomplete_or_ambiguous', 'arrow_glyphs': glyphs}
        sequence.append(ranking[0][1])
    if glyphs:
        # A bright or damaged continuation must not become a shorter legal code.
        tail = glyphs[-1][0] + round(size * .45)
        band = mask[round(size * .10):round(size * .52), tail:tail + round(size * .40)]
        if band.size == 0 or band.mean() > .10:
            return {'id': None, 'observed_sequence': sequence, 'arrow_decision': 'unreadable_tail', 'arrow_glyphs': glyphs}
    _raise_if_cancelled(cancel_event)
    remaining = _remaining(deadline)
    if remaining is not None and remaining <= 0:
        return {'id': None, 'observed_sequence': sequence,
                'arrow_decision': 'deadline_exhausted', 'arrow_glyphs': glyphs}
    candidates = [key for key, entry in entries.items() if entry['sequence'] == sequence]
    return {'id': candidates[0] if len(candidates) == 1 else None, 'observed_sequence': sequence,
            'arrow_decision': 'unique_sequence' if len(candidates) == 1 else 'no_unique_sequence', 'arrow_glyphs': glyphs}


def apply_mission_fallbacks(image, rows, entries, *, deadline=None, cancel_event=None):
    """Resolve remaining unidentified mission rows in place, via name OCR then arrows.

    Skips rows that already have an id or a conflict. Shares one deadline across every
    row, capped at FALLBACK_BUDGET_SECONDS, and marks every still-unresolved row's
    fallback_status 'deadline_exhausted' once it runs out. Mutates and returns rows.
    """
    budget_deadline = time.monotonic() + FALLBACK_BUDGET_SECONDS
    deadline = budget_deadline if deadline is None else min(deadline, budget_deadline)
    for row in rows:
        _raise_if_cancelled(cancel_event)
        if row['id'] is not None or row.get('conflict'):
            continue
        if _remaining(deadline) <= 0:
            for unresolved in rows:
                if unresolved['id'] is None and not unresolved.get('conflict'):
                    unresolved['fallback_status'] = 'deadline_exhausted'
            break
        name = read_name(image, row['box'], entries, deadline=deadline,
                         cancel_event=cancel_event)
        row['name_fallback'] = name
        _raise_if_cancelled(cancel_event)
        if _remaining(deadline) <= 0:
            for unresolved in rows:
                if unresolved['id'] is None and not unresolved.get('conflict'):
                    unresolved['fallback_status'] = 'deadline_exhausted'
            break
        if name['id'] is not None:
            row.update(id=name['id'], method='mission-name')
            continue
        arrows = read_arrows(image, row['box'], entries, deadline=deadline,
                             cancel_event=cancel_event)
        row['arrow_fallback'] = arrows
        _raise_if_cancelled(cancel_event)
        if _remaining(deadline) <= 0:
            for unresolved in rows:
                if unresolved['id'] is None and not unresolved.get('conflict'):
                    unresolved['fallback_status'] = 'deadline_exhausted'
            break
        if arrows['id'] is not None:
            row.update(id=arrows['id'], method='mission-arrows')
        elif arrows.get('arrow_decision') == 'deadline_exhausted':
            row['fallback_status'] = 'deadline_exhausted'
    return rows
