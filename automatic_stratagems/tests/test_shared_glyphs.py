"""Retain distinguishing badge evidence when equipment shares its main shape."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import cv2
import numpy as np
from PIL import Image
from automatic_stratagems.scanner.stratagem_detection import catalog, detect_icons
from automatic_stratagems.scanner import stratagem_detection as detection
from automatic_stratagems.scanner.recognition_cache import RecognitionCache


class SharedGlyphTests(unittest.TestCase):
    def rover_selection_tile(self):
        path = (Path(__file__).parent
                / 'fixtures/steam-full-scenes/20260911164727_1.jpg')
        return Image.open(path).convert('RGB').crop((757, 1682, 907, 1832))

    def guard_dog_selection_tile(self):
        path = (Path(__file__).parent
                / 'fixtures/steam-full-scenes/20260911164029_1.jpg')
        return Image.open(path).convert('RGB').crop((1097, 1682, 1247, 1832))

    def catalog_references(self, bank):
        return [(key, bank.get(key)['features'], bank.get(key)['details'])
                for key in catalog()]

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

    def test_real_rover_tile_uses_its_distinguishing_glyph(self):
        with self.rover_selection_tile() as image:
            row = detect_icons(image, catalog(), [[0, 0, *image.size]])[0]
        self.assertEqual(row['id'], 'GuardDogRover')

    def test_rover_without_distinguishing_glyph_stays_unknown(self):
        with self.rover_selection_tile() as image:
            pixels = np.array(image)
        hsv = cv2.cvtColor(pixels, cv2.COLOR_RGB2HSV)
        pixels[(hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 170)] = (36, 36, 36)
        row = detect_icons(Image.fromarray(pixels), catalog(), [[0, 0, 150, 150]])[0]
        self.assertIsNone(row['id'])

    def test_guard_dog_variants_keep_their_canonical_identity(self):
        for key in ('GuardDog', 'GuardDogRover', 'GuardDogK9',
                    'GuardDogHotDog', 'GuardDogBreath'):
            with self.subTest(key=key):
                path = detection.ROOT / 'assets/icons' / f'{key}.png'
                with Image.open(path) as image:
                    tile = image.convert('RGB').resize((150, 150))
                    row = detect_icons(tile, catalog(), [[0, 0, 150, 150]])[0]
                self.assertEqual(row['id'], key)

    def test_real_guard_dog_and_k9_sources_keep_their_identity(self):
        k9_path = Path(__file__).parent / 'fixtures/live-tiles/2-9.png'
        cases = (
            ('GuardDog', self.guard_dog_selection_tile()),
            ('GuardDogK9', Image.open(k9_path).convert('RGB')),
        )
        for key, image in cases:
            with self.subTest(key=key), image:
                row = detect_icons(image, catalog(), [[0, 0, *image.size]])[0]
            self.assertEqual(row['id'], key)

    def test_real_rover_fast_full_and_cached_paths_agree(self):
        with self.rover_selection_tile() as image:
            rgb = np.array(image)
        plain_bank = detection.IconTemplates()
        references = self.catalog_references(plain_bank)
        fast = detection.match_equipped(rgb, references, bank=plain_bank)
        full = detection.match_equipped(
            rgb, references, bank=plain_bank, fast_filter=False)
        with TemporaryDirectory() as directory:
            cached_bank = detection.IconTemplates(
                cache=RecognitionCache(directory, 'rover-test'))
            cached_references = self.catalog_references(cached_bank)
            first = detection.match_equipped(
                rgb, cached_references, bank=cached_bank)
            second = detection.match_equipped(
                rgb, cached_references, bank=cached_bank)
        self.assertEqual(fast['id'], 'GuardDogRover')
        self.assertEqual(full['id'], fast['id'])
        self.assertEqual(first['id'], fast['id'])
        self.assertEqual(second['id'], fast['id'])

    def test_unique_component_without_companion_support_stays_unknown(self):
        bank = detection.IconTemplates()
        details = bank.get('GuardDogRover')['details']
        references = [
            ('candidate', np.full((98, 98, 2), .86, np.float32),
             [details[0], np.zeros((64, 64), np.float32)]),
            ('rival', np.full((98, 98, 2), .84, np.float32),
             [bank.get('GuardDogK9')['details'][0],
              np.zeros((64, 64), np.float32)]),
        ]
        with patch.object(detection, 'icon_features',
                          return_value=np.zeros((134, 134, 2), np.float32)), \
             patch.object(detection, 'icon_details', return_value=details), \
             patch.object(detection.cv2, 'matchTemplate',
                          side_effect=lambda image, template, method: template[:1, :1, 0]):
            row = detection.match_equipped(
                np.zeros((150, 150, 3), np.uint8), references)
        self.assertIsNone(row['id'])

    def test_distinct_component_conflicting_with_whole_icon_stays_unknown(self):
        bank = detection.IconTemplates()
        details = bank.get('GuardDogRover')['details']
        references = [
            ('component', np.full((98, 98, 2), .82, np.float32),
             details),
            ('whole', np.full((98, 98, 2), .90, np.float32),
             bank.get('GuardDogK9')['details']),
        ]
        with patch.object(detection, 'icon_features',
                          return_value=np.zeros((134, 134, 2), np.float32)), \
             patch.object(detection, 'icon_details', return_value=details), \
             patch.object(detection.cv2, 'matchTemplate',
                          side_effect=lambda image, template, method: template[:1, :1, 0]):
            row = detection.match_equipped(
                np.zeros((150, 150, 3), np.uint8), references)
        self.assertIsNone(row['id'])
        self.assertTrue(row['conflict'])

    def test_blank_or_uniform_component_has_no_correlation_evidence(self):
        blank = np.zeros((64, 64), np.float32)
        uniform = np.ones((64, 64), np.float32)
        shape = detection.IconTemplates().get('GuardDogRover')['details'][0]
        for observed, reference in ((blank, blank), (uniform, shape),
                                    (shape, uniform)):
            with self.subTest(observed=observed.mean(), reference=reference.mean()):
                self.assertEqual(
                    detection._normalized_component_correlation(observed, reference),
                    -1.0)

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
