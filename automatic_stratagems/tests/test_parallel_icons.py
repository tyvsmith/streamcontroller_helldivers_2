from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest

from PIL import Image
from automatic_stratagems.scanner.stratagem_detection import catalog, detect_icons, detect_mission_icons


class ParallelTests(unittest.TestCase):
    def test_selection_rows_keep_order_and_exact_scores(self):
        entries = catalog()
        root = Path(__file__).resolve().parents[2]/'assets/icons'
        image = Image.new('RGB', (432, 144))
        for i, key in enumerate(('Reinforce', 'WarpPack', 'Resupply')):
            with Image.open(root/f'{key}.png') as tile:
                image.paste(tile, (144*i, 0))
        boxes = [[i*144, 0, 144, 144] for i in range(3)]
        expected = detect_icons(image, entries, boxes)
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(detect_icons(image, entries, boxes, executor=executor), expected)

    def test_mission_contrast_and_fallback_scores_are_identical(self):
        with Image.open(Path(__file__).parent/'fixtures/mission-panels/18n2fofu.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        entries = catalog()
        expected = detect_mission_icons(image, entries)
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(detect_mission_icons(image, entries, executor=executor), expected)

    def test_invalid_worker_count_is_rejected_before_capture(self):
        from unittest.mock import patch
        from contextlib import redirect_stderr
        import io
        from automatic_stratagems.scanner import scan_game
        for value in ('0', '33'):
            with self.subTest(value=value), patch.object(scan_game, 'scan_live') as capture, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    scan_game.main(['--workers', value])
                capture.assert_not_called()

    def test_worker_errors_propagate_instead_of_returning_partial_output(self):
        from automatic_stratagems.scanner.stratagem_detection import ordered_map
        def match(row):
            if row == 2:
                raise ValueError('invalid tile')
            return row
        with ThreadPoolExecutor(max_workers=2) as executor:
            with self.assertRaisesRegex(ValueError, 'invalid tile'):
                ordered_map(executor, match, [1, 2, 3])

    def test_single_icon_distributes_candidates_within_worker_budget(self):
        import threading
        from unittest.mock import patch
        from automatic_stratagems.scanner import stratagem_detection as detection
        image = Image.open(detection.ROOT/'assets/icons/WarpPack.png').convert('RGB')
        entries = catalog()
        expected = detect_icons(image, entries, [[0, 0, 144, 144]])
        threads = set()
        original = detection.cv2.matchTemplate
        def match(*args):
            threads.add(threading.current_thread().name)
            return original(*args)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='candidate-test') as executor, \
             patch.object(detection.cv2, 'matchTemplate', side_effect=match):
            actual = detect_icons(image, entries, [[0, 0, 144, 144]], executor=executor)
        self.assertEqual(actual, expected)
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(name.startswith('candidate-test') for name in threads))
