"""Retain distinguishing badge evidence when equipment shares its main shape."""
import unittest
from pathlib import Path
from unittest.mock import patch
import cv2
import numpy as np
from PIL import Image
from automatic_stratagems.scanner.stratagem_detection import catalog, detect_icons
from automatic_stratagems.scanner import stratagem_detection as detection


class SharedGlyphTests(unittest.TestCase):
    def test_original_ballistic_shield_captures(self):
        for path in sorted((Path(__file__).parent / 'fixtures/shared-glyphs').glob('*.png')):
            with self.subTest(capture=path.stem), Image.open(path) as image:
                row = detect_icons(image, catalog(), [[0, 0, *image.size]])[0]
                self.assertEqual(row['id'], 'BallisticShield')
                self.assertFalse(row['conflict'])

    def test_shared_backpack_without_badge_stays_unknown(self):
        path = Path(__file__).parent / 'fixtures/shared-glyphs/kt6sva9f.png'
        with Image.open(path) as image:
            pixels = np.array(image.convert('RGB'))
        hsv = cv2.cvtColor(pixels, cv2.COLOR_RGB2HSV)
        pixels[(hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 170)] = (36, 36, 36)
        row = detect_icons(Image.fromarray(pixels), catalog(), [[0, 0, 150, 150]])[0]
        self.assertIsNone(row['id'])

    def test_ambiguous_or_conflicting_components_do_not_resolve_identity(self):
        for parts in [((.82, .92), (.80, .94)), ((.90, .81), (.81, .90))]:
            references = [(str(i), np.full((98, 98, 2), score, np.float32),
                           [np.full((64, 64), value, np.float32) for value in values])
                          for i, (score, values) in enumerate(zip((.964, .928), parts))]
            with self.subTest(parts=parts), \
                 patch.object(detection, 'icon_features', return_value=np.zeros((134, 134, 2), np.float32)), \
                 patch.object(detection, 'icon_details', return_value=[np.ones((64, 64), np.float32)] * 2), \
                 patch.object(detection.cv2, 'matchTemplate', side_effect=lambda image, template, method: template[:1, :1, 0]):
                row = detection.match_equipped(np.zeros((150, 150, 3), np.uint8), references)
                self.assertIsNone(row['id'])
