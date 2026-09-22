import io
import json
import os
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image
from automatic_stratagems.scanner import scan_game
from automatic_stratagems.scanner import stratagem_detection as detection


class PerformanceTests(unittest.TestCase):
    def test_live_debug_encodes_only_one_set_and_keeps_root_files(self):
        image = Image.new('RGB', (200, 200), 'gray')
        report = {'mode': 'mission', 'status': 'matched', 'rows': [], 'warnings': []}
        seen = {}
        def capture(backend, recognize, save_attempt, prepare, **kwargs):
            seen.update(kwargs)
            save_attempt('gamescope', image, report)
            return image, {'backend': 'gamescope'}, report
        with TemporaryDirectory() as root:
            directory = Path(root) / 'scan'
            with patch.object(scan_game, 'scan_live', side_effect=capture), \
                 patch.object(scan_game, 'write_debug', wraps=scan_game.write_debug) as writer, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(scan_game.main(['--capture-backend', 'gamescope', '--debug-dir', str(directory), '--json']), 0)
            self.assertEqual(json.loads((directory/'findings.json').read_text())['schema_version'], 1)
            self.assertIn('cancel_event', seen)
            self.assertEqual(writer.call_count, 1)
            for name in ('capture.png', 'annotated.png'):
                self.assertEqual((directory/name).read_bytes(), (directory/'gamescope'/name).read_bytes())
            self.assertIn('seconds', json.loads((directory/'findings.json').read_text()))
            self.assertNotIn('seconds', json.loads((directory/'gamescope'/'findings.json').read_text()))

    def test_auto_debug_reuses_the_selected_screenshot_attempt(self):
        gamescope = Image.new('RGB', (200, 200), 'red')
        screenshot = Image.new('RGB', (200, 200), 'blue')
        partial = {'mode': 'mission', 'status': 'partial',
                   'rows': [{'id': None, 'box': [10, 10, 10, 10]}],
                   'warnings': []}
        matched = {'mode': 'mission', 'status': 'matched',
                   'rows': [{'id': 'Reinforce', 'box': [10, 10, 10, 10]}],
                   'warnings': []}

        def capture(_backend, _recognize, save_attempt, _prepare, **_kwargs):
            save_attempt('gamescope', gamescope, partial)
            save_attempt('screenshot', screenshot, matched)
            return screenshot, {
                'kind': 'file', 'backend': 'screenshot',
                'path': '/screenshots/new.png'}, matched

        with TemporaryDirectory() as root:
            directory = Path(root) / 'scan'
            with patch.object(scan_game, 'scan_live', side_effect=capture), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(scan_game.main([
                    '--image-source-kind', 'folder', '--image-source-path',
                    '/screenshots', '--debug-dir', str(directory), '--json']), 0)
            with Image.open(directory / 'capture.png') as selected:
                self.assertEqual(selected.getpixel((0, 0)), (0, 0, 255))
            self.assertEqual((directory / 'capture.png').read_bytes(),
                             (directory / 'screenshot' / 'capture.png').read_bytes())

    def test_template_bank_reuses_asset_preparation(self):
        entries = {key: value for key, value in detection.catalog().items()
                   if key in ('Reinforce', 'SOSBeacon')}
        bank = detection.IconTemplates()
        image = Image.open(detection.ROOT/'assets/icons/Reinforce.png').convert('RGB')
        with patch.object(detection.cv2, 'imread', wraps=detection.cv2.imread) as read:
            first = detection.detect_icons(image, entries, [[0, 0, 144, 144]], bank=bank)
            second = detection.detect_icons(image, entries, [[0, 0, 144, 144]], bank=bank)
        self.assertEqual(first, second)
        self.assertEqual(read.call_count, len(entries))

    def test_debug_reuse_falls_back_to_copy_when_links_unavailable(self):
        with TemporaryDirectory() as root:
            source, destination = Path(root)/'attempt', Path(root)/'final'
            source.mkdir()
            destination.mkdir()
            Image.new('RGB', (20, 20), 'blue').save(source/'capture.png')
            with patch.object(scan_game.os, 'link', side_effect=OSError('links unavailable')):
                scan_game.reuse_debug_images(source, destination)
            self.assertEqual((source/'capture.png').read_bytes(), (destination/'capture.png').read_bytes())

    def test_runner_owned_debug_directory_is_used_without_nested_child(self):
        with TemporaryDirectory() as root:
            directory = Path(root) / 'run'
            directory.mkdir()
            (directory / '.hd2-scan-run.json').write_text(
                '{"owner":"net_jslay_helldivers_2","status":"open","version":1}\n')
            self.assertEqual(scan_game.prepare_debug_directory(directory), directory)

    def test_cli_debug_directory_is_private_and_rejects_symlink_parent(self):
        with TemporaryDirectory() as root:
            root = Path(root)
            directory = root / 'new'
            self.assertEqual(scan_game.prepare_debug_directory(directory), directory)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            target = root / 'target'
            target.mkdir()
            link = root / 'link'
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(scan_game.ScanError, 'symlink'):
                scan_game.prepare_debug_directory(link / 'child')
            self.assertFalse((target / 'child').exists())

    def test_cli_runner_marker_must_be_regular_before_read(self):
        with TemporaryDirectory() as root:
            directory = Path(root) / 'run'
            directory.mkdir()
            os.mkfifo(directory / '.hd2-scan-run.json')
            with patch.object(Path, 'read_text', side_effect=AssertionError('read FIFO')):
                child = scan_game.prepare_debug_directory(directory)
            self.assertNotEqual(child, directory)

    def test_scaled_templates_match_uncached_preparation(self):
        bank = detection.IconTemplates()
        source = detection.cv2.imread(str(detection.ROOT/'assets/icons/Reinforce.png'))
        gray = detection.cv2.cvtColor(source[23:121, 23:121], detection.cv2.COLOR_BGR2GRAY)
        features = detection.icon_features(detection.cv2.cvtColor(source[23:121, 23:121], detection.cv2.COLOR_BGR2RGB))
        import numpy as np
        for size in (75, 103, 133):
            scaled, edge = bank.scaled('Reinforce', size, 'gray')
            expected = detection.cv2.resize(gray, (size, size))
            np.testing.assert_array_equal(scaled, expected)
            np.testing.assert_array_equal(edge, detection.cv2.Canny(expected, 40, 100))
            np.testing.assert_array_equal(bank.scaled('Reinforce', size, 'features'),
                                          detection.cv2.resize(features, (size, size)))

    def test_annotation_is_cropped_but_capture_pixels_and_boxes_are_preserved(self):
        import numpy as np
        image = Image.fromarray(np.random.default_rng(7).integers(0, 256, (480, 640, 3), dtype=np.uint8))
        report = {'mode': 'selection', 'rows': [{'id': 'Reinforce', 'box': [120, 200, 50, 50]}],
                  'layout': {'ready_bar': [100, 300, 200, 20], 'empty_tiles': [[220, 200, 50, 50]]}}
        with TemporaryDirectory() as root:
            directory = Path(root)
            scan_game.write_debug(directory, image, report)
            with Image.open(directory/'capture.png') as capture:
                self.assertEqual(capture.size, image.size)
                self.assertEqual(capture.tobytes(), image.tobytes())
            with Image.open(directory/'annotated.png') as annotated:
                self.assertLess(annotated.width, image.width)
                self.assertLess(annotated.height, image.height)
                x, y, width, height = report['debug_annotation_box']
                self.assertEqual(annotated.size, (width, height))
                self.assertEqual(annotated.getpixel((120-x, 200-y)), (0, 255, 0))
                self.assertLessEqual(x, 100)
                self.assertGreaterEqual(x+width, 300)
                self.assertGreaterEqual(y+height, 320)
            self.assertEqual(report['rows'][0]['box'], [120, 200, 50, 50])

    def test_rejected_capture_without_detected_region_needs_no_annotation(self):
        with TemporaryDirectory() as root:
            directory = Path(root)
            scan_game.write_debug(directory, Image.new('RGB', (200, 200)), {'status': 'capture_rejected', 'rows': []})
            self.assertTrue((directory/'capture.png').exists())
            self.assertFalse((directory/'annotated.png').exists())

    def test_replay_rejects_encoded_file_before_image_open(self):
        with TemporaryDirectory() as root:
            path = Path(root) / 'huge.png'
            path.write_bytes(b'123456789')
            with patch.object(scan_game, 'MAX_ENCODED_IMAGE_BYTES', 8), \
                 patch.object(scan_game, 'decode_image') as decode:
                with self.assertRaisesRegex(scan_game.ScanError, 'too large'):
                    scan_game.load_replay_image(path)
            decode.assert_not_called()

    def test_mission_crop_keeps_text_width_on_narrower_aspect_ratio(self):
        image = Image.new('RGB', (1920, 1080))
        report = {'mode': 'mission', 'rows': [{'id': 'Reinforce', 'box': [39, 81, 44, 44]}]}
        left, top, width, height = scan_game.annotation_box(image, report)
        self.assertGreaterEqual(left + width, 362)

    def test_detect_forwards_runtime_budget_and_cancellation_to_fallbacks(self):
        image = Image.new('RGB', (200, 200))
        cancelled = threading.Event()
        deadline = time.monotonic() + 123
        with patch.object(scan_game, 'find_selection_band', side_effect=scan_game.ScanError()), \
             patch.object(scan_game, 'detect_mission_icons', return_value=[]), \
             patch('automatic_stratagems.scanner.mission_fallbacks.apply_mission_fallbacks',
                   return_value=[]) as fallback:
            scan_game.detect(image, 'auto', deadline=deadline,
                             cancel_event=cancelled)
        self.assertEqual(fallback.call_args.kwargs,
                         {'deadline': deadline, 'cancel_event': cancelled})
