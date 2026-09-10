from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from automatic_stratagems.scanner.recognition_cache import RecognitionCache
from automatic_stratagems.scanner import stratagem_detection as detection


class MatchCacheTests(unittest.TestCase):

    def test_recursive_mission_cache_result_is_a_miss(self):
        saved = {'id': 'A', 'method': 'mission-icon', 'extra': {}}
        nested = saved['extra']
        for _ in range(1200):
            nested['next'] = {}
            nested = nested['next']

        try:
            restored = detection.restore_mission_result(saved, {'A': {}})
        except RecursionError:
            self.fail('recursive mission cache escaped as RecursionError')
        self.assertIsNone(restored)

    def test_new_scanner_reuses_exact_features_but_not_changed_pixels_or_candidates(self):
        with TemporaryDirectory() as directory:
            def bank():
                return detection.IconTemplates(cache=RecognitionCache(directory, 'catalog-v1'))
            first = bank()
            references = [(key, first.get(key)['features'], first.get(key)['details']) for key in detection.catalog()]
            rgb = np.array(Image.open(detection.ROOT/'assets/icons/WarpPack.png').convert('RGB'))
            expected = detection.match_equipped(rgb, references, bank=first)
            self.assertEqual(expected['id'], 'WarpPack')
            with patch.object(detection, '_match_equipped', wraps=detection._match_equipped) as match:
                self.assertEqual(detection.match_equipped(rgb, references, bank=bank()), expected)
                match.assert_not_called()
                dimmed = np.where(rgb == 255, 254, rgb).astype(np.uint8)
                self.assertEqual(detection.match_equipped(dimmed, references, bank=bank()), expected)
                match.assert_not_called()
                changed = rgb.copy()
                changed[50:80, 50:80] = 255
                detection.match_equipped(changed, references, bank=bank())
                self.assertEqual(match.call_count, 1)
                detection.match_equipped(rgb, references[:-1], bank=bank())
                self.assertEqual(match.call_count, 2)

    def test_unknown_results_are_not_cached(self):
        with TemporaryDirectory() as directory:
            bank = detection.IconTemplates(cache=RecognitionCache(directory, 'catalog-v1'))
            refs = [(key, bank.get(key)['features'], bank.get(key)['details'])
                    for key in ('WarpPack', 'Reinforce')]
            with patch.object(bank.cache, 'put') as put:
                detection.match_equipped(np.zeros((150, 150, 3), np.uint8), refs, bank=bank)
                put.assert_not_called()

    def test_malformed_cache_records_are_recomputed(self):
        with TemporaryDirectory() as directory:
            bank = detection.IconTemplates(cache=RecognitionCache(directory, 'catalog-v1'))
            refs = [(key, bank.get(key)['features'], bank.get(key)['details'])
                    for key in ('WarpPack', 'Reinforce')]
            rgb = np.array(Image.open(detection.ROOT/'assets/icons/WarpPack.png').convert('RGB'))
            for saved in ({'id': [], 'conflict': False},
                          {'id': 'WarpPack', 'conflict': False, 'feature_top3': 1},
                          {'id': 'WarpPack', 'conflict': False}):
                with self.subTest(saved=saved), patch.object(bank.cache, 'get', return_value=saved), \
                     patch.object(detection, '_match_equipped', wraps=detection._match_equipped) as match:
                    self.assertEqual(detection.match_equipped(rgb, refs, bank=bank)['id'], 'WarpPack')
                    match.assert_called_once()

    def test_complete_mission_hits_skip_matching_and_keep_current_row_results(self):
        with Image.open(Path(__file__).parent/'fixtures/mission-panels/18n2fofu.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        entries = detection.catalog()
        with TemporaryDirectory() as directory:
            expected = detection.detect_mission_icons(image, entries,
                           cache=RecognitionCache(directory, 'mission-v1'))
            self.assertTrue(all(row['id'] for row in expected))
            with patch.object(detection, 'match_reference', side_effect=AssertionError('cache missed')):
                actual = detection.detect_mission_icons(image, entries,
                           cache=RecognitionCache(directory, 'mission-v1'))
            self.assertEqual(actual, expected)

    def test_mission_unknown_is_retried_beside_cached_matches(self):
        image = Image.new('RGB', (100, 350))
        image.paste('white', (0, 220, 87, 307))
        with TemporaryDirectory() as directory, \
             patch.object(detection, 'mission_frames', return_value=[[0, y, 87, 87] for y in (20, 120, 220)]), \
             patch.object(detection, 'match_reference', side_effect=lambda *args: {'id': 'WarpPack'}), \
             patch.object(detection, 'stretch_icon', return_value=None), \
             patch.object(detection, 'icon_occluded', side_effect=lambda tile: tile.getpixel((0, 0)) == (255, 255, 255)) as occluded:
            cache = RecognitionCache(directory, 'mission-v1')
            first = detection.detect_mission_icons(image, detection.catalog(), cache=cache)
            self.assertEqual([row['id'] for row in first], ['WarpPack', 'WarpPack', None])
            second = detection.detect_mission_icons(image, detection.catalog(), cache=cache)
            self.assertEqual(first, second)
            self.assertEqual(occluded.call_count, 4)
