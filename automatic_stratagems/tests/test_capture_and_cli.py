"""Capture, selection layout, and canonical scanner CLI behavior."""
import io
from contextlib import redirect_stdout, redirect_stderr
import json
import unittest
from unittest.mock import patch

from PIL import Image

from automatic_stratagems.scanner.errors import ScanError
from automatic_stratagems.scanner.scan_game import main, rectangle, detect


class CLITests(unittest.TestCase):
    def test_default_auto_passes_screenshot_config_but_tries_gamescope_first(self):
        output = io.StringIO()
        image = Image.new('RGB', (100, 100), 'red')
        report = {'mode': 'mission', 'status': 'matched', 'rows': [],
                  'warnings': []}
        with patch('automatic_stratagems.scanner.scan_game.scan_live',
                   return_value=(image, {'kind': 'live', 'backend': 'gamescope'},
                                 report)) as capture, \
             redirect_stdout(output):
            self.assertEqual(main([
                '--image-source-kind', 'folder',
                '--image-source-path', '/screenshots', '--json']), 0)
        self.assertEqual(capture.call_args.args[0], 'auto')
        self.assertEqual(capture.call_args.kwargs['screenshot']['path'],
                         '/screenshots')
        serialized = json.loads(output.getvalue())
        self.assertEqual(serialized['schema_version'], 1)
        self.assertNotIn('phase', serialized)

    def test_explicit_gamescope_ignores_supplied_screenshot_config(self):
        image = Image.new('RGB', (100, 100), 'red')
        report = {'mode': 'mission', 'status': 'matched', 'rows': [],
                  'warnings': []}
        with patch('automatic_stratagems.scanner.scan_game.scan_live',
                   return_value=(image, {'kind': 'live', 'backend': 'gamescope'},
                                 report)) as capture, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main([
                '--capture-backend', 'gamescope', '--image-source-kind',
                'folder', '--image-source-path', '/screenshots', '--json']), 0)
        self.assertEqual(capture.call_args.args[0], 'gamescope')
        self.assertIsNone(capture.call_args.kwargs['screenshot'])

    def test_auto_setup_does_not_validate_either_capture_alternative(self):
        with patch('automatic_stratagems.scanner.scan_game.check_capture_setup') as gamescope, \
             patch('automatic_stratagems.scanner.screenshot_capture.check_screenshot_setup') as screenshot:
            self.assertEqual(main([
                '--check-setup', '--image-source-kind', 'folder',
                '--image-source-path', '/missing', '--screenshot-trigger',
                'script', '--screenshot-script', '/missing']), 0)
        gamescope.assert_called_once_with('auto', cancel_event=unittest.mock.ANY)
        screenshot.assert_not_called()

    def test_auto_does_not_cleanup_screenshot_when_gamescope_wins(self):
        image = Image.new('RGB', (100, 100), 'red')
        report = {'mode': 'mission', 'status': 'matched',
                  'rows': [{'id': 'Reinforce'}], 'warnings': []}
        with patch('automatic_stratagems.scanner.scan_game.scan_live',
                   return_value=(image, {'kind': 'live', 'backend': 'gamescope'},
                                 report)), \
             patch('automatic_stratagems.scanner.screenshot_capture.cleanup_screenshot') as cleanup, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main([
                '--image-source-kind', 'folder',
                '--image-source-path', '/screenshots',
                '--delete-screenshot', '--json']), 0)
        cleanup.assert_not_called()

    def test_selected_screenshot_is_cleaned_after_recognition(self):
        image = Image.new('RGB', (100, 100), 'blue')
        report = {'mode': 'mission', 'status': 'partial',
                  'rows': [{'id': 'Reinforce'}], 'warnings': []}
        source = {'kind': 'file', 'backend': 'screenshot',
                  'path': '/screenshots/new.png'}
        with patch('automatic_stratagems.scanner.scan_game.scan_live',
                   return_value=(image, source, report)), \
             patch('automatic_stratagems.scanner.screenshot_capture.cleanup_screenshot',
                   return_value=True) as cleanup, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main([
                '--image-source-kind', 'folder',
                '--image-source-path', '/screenshots',
                '--delete-screenshot', '--json']), 3)
        cleanup.assert_called_once()

    def test_live_failure_is_json_and_captures_immediately(self):
        output = io.StringIO()
        with patch('automatic_stratagems.scanner.scan_game.scan_live', side_effect=ScanError('No associated Gamescope session')) as capture, \
             patch('time.sleep') as sleep, redirect_stdout(output):
            self.assertEqual(main(['--capture-backend', 'gamescope', '--json']), 1)
        capture.assert_called_once()
        self.assertEqual(capture.call_args.args[0], "gamescope")
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
