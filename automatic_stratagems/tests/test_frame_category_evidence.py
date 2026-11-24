"""The mission frame hue is evidence over a full-catalog match, never a filter.

Orbital artwork and SEAF Artillery share the planet-dome component at .96
overlap, so a frame hue used as a hard candidate filter deletes the true match
and leaves that shape twin to win by default among the survivors.
"""
from pathlib import Path
import unittest

import numpy as np
from PIL import Image

from automatic_stratagems.scanner.icon_normalization import icon_category
from automatic_stratagems.scanner.mission_layout import mission_frames
from automatic_stratagems.scanner.stratagem_detection import (
    IconTemplates, catalog, detect_icons)

YELLOW_STROKE = (235, 200, 40)
RED_STROKE = (210, 35, 45)


def framed(key, stroke=None):
    """Catalog artwork at tile size, optionally with a frame stroke down its left edge.

    icon_category(frame=True) samples rows .2-.8 of columns .01-.05, so the
    stroke alone decides the frame category the mission matchers read.
    """
    with Image.open(f'assets/icons/{key}.png') as image:
        pixels = np.array(image.convert('RGB').resize((150, 150)))
    if stroke is not None:
        pixels[30:120, 1:8] = stroke
    return Image.fromarray(pixels)


class FrameCategoryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.bank = IconTemplates()
        self.catalog = catalog()

    def match(self, tile, **kwargs):
        return detect_icons(tile, self.catalog, [[0, 0, 150, 150]],
                            bank=self.bank, **kwargs)[0]

    def test_misread_frame_hue_does_not_delete_the_true_match(self):
        """A red orbital whose frame reads yellow must not be called SEAF Artillery."""
        tile = framed('OrbitalWalkingBarrage', YELLOW_STROKE)
        self.assertEqual(icon_category(tile, frame=True), 'yellow')

        row = self.match(tile, mission=True)

        self.assertNotEqual(row['id'], 'SEAFArtillery')

    def test_frame_hue_disagreement_is_reported_not_resolved_silently(self):
        """When the best full-catalog match contradicts the frame, say so."""
        tile = framed('OrbitalWalkingBarrage', YELLOW_STROKE)

        row = self.match(tile, mission=True)

        self.assertIsNone(row['id'])
        self.assertTrue(row['conflict'])
        self.assertEqual(row['decision'], 'category_conflict')

    def test_conflicting_frame_and_artwork_do_not_assign_a_shape_twin(self):
        row = self.match(framed('OrbitalPrecisionStrike', YELLOW_STROKE), mission=True)

        self.assertIsNone(row['id'])
        self.assertEqual(row['decision'], 'category_conflict')

    def test_clipped_frame_uses_artwork_color_on_captured_precision_strikes(self):
        panels = Path(__file__).parent / 'fixtures' / 'mission-panels'
        for name in ('73lq54lq.png', 'jk0csfn4.png'):
            with self.subTest(capture=name), Image.open(panels / name) as panel:
                scene = Image.new('RGB', (5120, 2160))
                scene.paste(panel)
                x, y, size, _ = mission_frames(scene)[3]
                cropped = scene.crop((x, y, x + size, y + size)).resize((150, 150))
                pixels = np.array(cropped)
                pixels[30:120, 1:8] = (255, 255, 226)
                tile = Image.fromarray(pixels)

                self.assertIsNone(icon_category(tile, frame=True))
                row = self.match(tile, mission=True)
                self.assertEqual(row['id'], 'OrbitalPrecisionStrike')

    def test_shape_twins_abstain_when_frame_and_artwork_have_no_color(self):
        tile = framed('OrbitalPrecisionStrike').convert('L').convert('RGB')

        row = self.match(tile, mission=True)

        self.assertIsNone(row['id'])

    def test_hue_cannot_overrule_a_rival_shape_separates(self):
        """A misread hue must not promote a candidate shape has already rejected."""
        row = self.match(framed('OrbitalWalkingBarrage', YELLOW_STROKE), mission=True)

        self.assertNotEqual(row['decision'], 'category_tiebreak')
        self.assertIsNone(row['id'])

    def test_agreeing_frame_hue_keeps_the_match(self):
        """A correctly read frame must not cost the tile its identity."""
        tile = framed('OrbitalWalkingBarrage', RED_STROKE)
        self.assertEqual(icon_category(tile, frame=True), 'red')

        row = self.match(tile, mission=True)

        self.assertEqual(row['id'], 'OrbitalWalkingBarrage')
        self.assertFalse(row['conflict'])

    def test_shape_twins_still_separate_under_their_own_frames(self):
        """SEAF Artillery and Orbital Precision Strike overlap .95 on shape."""
        for key, stroke in (('SEAFArtillery', YELLOW_STROKE),
                            ('OrbitalPrecisionStrike', RED_STROKE)):
            with self.subTest(icon=key):
                row = self.match(framed(key, stroke), mission=True)
                self.assertEqual(row['id'], key)

    def test_unreadable_frame_falls_back_to_the_full_catalog(self):
        """No frame hue means no colour evidence, not an empty candidate set."""
        tile = framed('OrbitalWalkingBarrage')
        self.assertIsNone(icon_category(tile, frame=True))

        row = self.match(tile, mission=True)

        self.assertEqual(row['id'], 'OrbitalWalkingBarrage')

    def test_every_catalog_icon_survives_its_own_frame_hue(self):
        """Dropping the hard filter must not cost any icon its identity."""
        strokes = {'red': RED_STROKE, 'yellow': YELLOW_STROKE,
                   'green': (60, 200, 70), 'cyan': (60, 210, 225)}
        misidentified = []
        for key in self.catalog:
            family = self.bank.get(key)['category']
            if family not in strokes:
                continue
            tile = framed(key, strokes[family])
            if icon_category(tile, frame=True) != family:
                continue
            row = self.match(tile, mission=True)
            if row['id'] is not None and row['id'] != key:
                misidentified.append((key, row['id']))
        self.assertEqual(misidentified, [])


if __name__ == '__main__':
    unittest.main()
