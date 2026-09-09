"""Capture, selection layout, and canonical scanner CLI behavior."""
import io
from contextlib import redirect_stdout, redirect_stderr
import json
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from automatic_stratagems.scanner.game_capture import ScanError, capture_game, window_geometry
from automatic_stratagems.scanner.selection_layout import find_selection_band, selection_boxes
from automatic_stratagems.scanner.scan_game import main, rectangle, detect


class CaptureTests(unittest.TestCase):
    def game(self):
        return {'class': 'steam_app_553850', 'address': '0x123', 'at': [100, 200],
                'size': [1920, 1080], 'mapped': True, 'hidden': False}

    def test_terminal_title_is_not_game_identity(self):
        window = {**self.game(), 'class': 'com.mitchellh.ghostty', 'title': 'helldivers_2'}
        with self.assertRaisesRegex(ScanError, 'Focus Helldivers'):
            window_geometry(window)

    def test_geometry_uses_window_not_monitor(self):
        self.assertEqual(window_geometry(self.game()), '100,200 1920x1080')

    def test_focus_change_discards_capture(self):
        blob = io.BytesIO()
        Image.new('RGB', (10, 10)).save(blob, format='PNG')
        other = {**self.game(), 'address': '0x456'}
        with patch('automatic_stratagems.scanner.game_capture.query_active_window', side_effect=[self.game(), other]), \
             patch('automatic_stratagems.scanner.game_capture.run_command', return_value=blob.getvalue()) as run:
            with self.assertRaisesRegex(ScanError, 'changed'):
                capture_game()
            self.assertEqual(run.call_args.args[0], ['grim', '-g', '100,200 1920x1080', '-t', 'png', '-'])

    def test_no_game_does_not_capture(self):
        with patch('automatic_stratagems.scanner.game_capture.query_active_window', return_value={}), \
             patch('automatic_stratagems.scanner.game_capture.run_command') as run:
            with self.assertRaises(ScanError):
                capture_game()
            run.assert_not_called()

    def test_success_decodes_capture_and_reports_geometry(self):
        blob = io.BytesIO()
        Image.new('RGB', (192, 108), color='red').save(blob, format='PNG')
        with patch('automatic_stratagems.scanner.game_capture.query_active_window', return_value=self.game()), \
             patch('automatic_stratagems.scanner.game_capture.run_command', return_value=blob.getvalue()):
            image, source = capture_game()
        self.assertEqual(image.size, (192, 108))
        self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(source['geometry'], '100,200 1920x1080')


class LayoutTests(unittest.TestCase):
    def test_four_players_choose_complete_leftmost_bar(self):
        im = Image.new('RGB', (2000, 900))
        draw = ImageDraw.Draw(im)
        for x in [290, 650, 1010, 1370]:
            draw.rectangle((x, 775, x + 329, 804), fill=(255, 255, 0))
        band = find_selection_band(im)
        self.assertAlmostEqual(band[0], 290, delta=3)
        self.assertGreater(band[0] + band[2], im.width / 4)
        boxes = selection_boxes(im, band)
        self.assertEqual(len(boxes), 11)
        self.assertTrue(all(x + w <= band[0] + band[2] for x, y, w, h in boxes))

    def test_no_bar_is_explicit_failure(self):
        with self.assertRaises(ScanError):
            find_selection_band(Image.new('RGB', (2000, 900)))

    def test_invalid_manual_boundary_is_rejected(self):
        with self.assertRaises(ScanError):
            selection_boxes(Image.new('RGB', (2000, 900)), [0, 50, 500, 30])

    def test_right_player_only_is_not_selected(self):
        im = Image.new('RGB', (2000, 900))
        ImageDraw.Draw(im).rectangle((1000, 775, 1329, 804), fill='yellow')
        with self.assertRaises(ScanError):
            find_selection_band(im)


class CLITests(unittest.TestCase):
    def test_live_failure_is_json_and_captures_immediately(self):
        output = io.StringIO()
        with patch('automatic_stratagems.scanner.scan_game.scan_live', side_effect=ScanError('Focus Helldivers')) as capture, \
             patch('time.sleep') as sleep, redirect_stdout(output):
            self.assertEqual(main(['--json']), 1)
        capture.assert_called_once()
        self.assertEqual(capture.call_args.args[0], "auto")
        sleep.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['status'], 'error')

    def test_countdown_option_does_not_exist(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(['--delay', '5'])
        self.assertEqual(error.exception.code, 2)

    def test_unknown_has_no_sequence(self):
        row = {'id': None, 'text': 'NEW STRATAGEM', 'box': [10, 10, 100, 40]}
        with patch('automatic_stratagems.scanner.scan_game.detect_mission_icons', return_value=[row]):
            report = detect(Image.new('RGB', (1000, 1000)), 'mission')
        self.assertEqual(report['status'], 'partial')
        self.assertIsNone(report['rows'][0]['sequence'])

    def test_normalized_boundary_rejects_nan_and_overflow(self):
        import argparse
        for value in ['nan,0,.2,.2', '.9,0,.2,.2', '0,0,0,.2']:
            with self.assertRaises(argparse.ArgumentTypeError):
                rectangle(value)


if __name__ == '__main__':
    unittest.main()
