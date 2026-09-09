import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from automatic_stratagems.scanner import stratagem_detection as detection


class SilhouetteFilterTests(unittest.TestCase):
    def test_clear_icon_checks_fewer_templates_with_same_identity(self):
        bank = detection.IconTemplates()
        references = [(key, bank.get(key)['features'], bank.get(key)['details'])
                      for key in detection.catalog()]
        rgb = np.array(Image.open(detection.ROOT/'assets/icons/GL52DeEscalator.png').convert('RGB'))
        with patch.object(detection.cv2, 'matchTemplate', wraps=detection.cv2.matchTemplate) as match:
            fast = detection.match_equipped(rgb, references, bank=bank)
            fast_calls = match.call_count
        with patch.object(detection.cv2, 'matchTemplate', wraps=detection.cv2.matchTemplate) as match:
            full = detection.match_equipped(rgb, references, bank=bank, fast_filter=False)
            full_calls = match.call_count
        self.assertEqual(fast['id'], 'GL52DeEscalator')
        self.assertEqual(fast['id'], full['id'])
        self.assertEqual(fast['decision'], 'silhouette_verified')
        self.assertLess(fast_calls, full_calls / 3)

    def test_ambiguous_silhouettes_use_full_matcher(self):
        bank = detection.IconTemplates()
        asset = bank.get('WarpPack')
        references = [(key, asset['features'], asset['details']) for key in ('one', 'two')]
        rgb = np.array(Image.open(detection.ROOT/'assets/icons/WarpPack.png').convert('RGB'))
        fast = detection.match_equipped(rgb, references)
        full = detection.match_equipped(rgb, references, fast_filter=False)
        self.assertEqual(fast, full)
        self.assertIsNone(fast['id'])

    def test_low_ranked_shared_component_cannot_hide_a_conflict(self):
        details = [np.ones((64, 64), np.float32)] * 2
        references = []
        for i in range(9):
            parts = (.9, .9) if i == 0 else (.9, .3) if i == 8 else (.69, .69)
            score = .935 if i == 0 else 1.0 if i == 8 else .8
            references.append((str(i), np.full((98, 98, 2), score, np.float32),
                               [np.full((64, 64), value, np.float32) for value in parts]))
        with patch.object(detection, 'icon_features', return_value=np.zeros((134, 134, 2), np.float32)), \
             patch.object(detection, 'icon_details', return_value=details), \
             patch.object(detection.cv2, 'matchTemplate', side_effect=lambda image, template, method: template[:1, :1, 0]) as match:
            result = detection.match_equipped(np.zeros((150, 150, 3), np.uint8), references)
            self.assertIsNone(result['id'])
            self.assertTrue(result['conflict'])
            self.assertEqual(match.call_count, 9 * 13)
