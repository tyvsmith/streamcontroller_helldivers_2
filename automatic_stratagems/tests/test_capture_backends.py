import unittest
from unittest.mock import MagicMock, patch
from concurrent.futures import CancelledError
from pathlib import Path
import sys
import tempfile
import threading
from PIL import Image
from automatic_stratagems.scanner import capture_backends as cb
from automatic_stratagems.scanner import game_capture as gc


class BackendsTests(unittest.TestCase):
    def setUp(self):
        quality = patch.object(cb, 'capture_issue', return_value=None)
        quality.start()
        self.addCleanup(quality.stop)

    def report(self, ids, method='mission-icon'):
        return {'status': 'partial' if None in ids else 'matched',
                'rows': [{'id': key, 'method': method} for key in ids], 'warnings': []}

    def scan(self, outputs):
        with patch.object(cb, 'query_active_window', return_value={}), \
             patch.object(cb, 'window_geometry'), patch.object(cb, 'check_focus'), \
             patch.object(cb, 'capture', side_effect=outputs) as capture:
            result = cb.scan_live('auto', lambda image: image)
            return result, capture.call_count

    def frame(self, report, name):
        return report, {'backend': name}

    def test_gamescope_complete_stops(self):
        result, calls = self.scan([self.frame(self.report(['a','b','c']), 'gamescope')])
        self.assertEqual(calls, 1)
        self.assertEqual(result[1]['backend'], 'gamescope')

    def test_missing_backend_falls_through_but_ocr_only_is_usable(self):
        result, calls = self.scan([cb.ScanError('No Gamescope'),
            self.frame(self.report(['a','b','c'], 'mission-name'), 'steam'),
            self.frame(self.report(['a','b','c']), 'desktop')])
        self.assertEqual(calls, 2)
        self.assertEqual(result[1]['backend'], 'steam')
        self.assertEqual(len(result[1]['attempts']), 2)

    def test_partial_keeps_first_capture_without_trying_others(self):
        result, calls = self.scan([
            self.frame(self.report(['a','b','c',None]), 'gamescope'),
            self.frame(self.report(['x','y','z']), 'steam')])
        self.assertEqual(calls, 1)
        self.assertEqual([r['id'] for r in result[2]['rows']], ['a','b','c',None])

    def test_partial_does_not_reach_later_backends(self):
        result, _ = self.scan([
            self.frame(self.report(['a','b','c',None]), 'gamescope'),
            cb.ScanError('No new Steam screenshot'), cb.ScanError('grim failed')])
        self.assertEqual(result[1]['backend'], 'gamescope')
        self.assertEqual(result[2]['status'], 'partial')

    def test_focus_change_aborts_without_fallback(self):
        with self.assertRaises(cb.FocusChanged):
            self.scan([cb.FocusChanged('focus moved')])

    def test_cancellation_aborts_without_fallback(self):
        cancelled = threading.Event()
        def first(*args):
            cancelled.set()
            raise cb.ScanError('unavailable')
        with patch.object(cb, 'query_active_window', return_value={}), \
             patch.object(cb, 'window_geometry'), patch.object(cb, 'check_focus'), \
             patch.object(cb, 'capture', side_effect=first) as capture:
            with self.assertRaises(CancelledError):
                cb.scan_live('auto', lambda image: image, cancel_event=cancelled)
            self.assertEqual(capture.call_count, 1)

    def test_all_unusable_fails(self):
        with self.assertRaisesRegex(cb.ScanError, 'No usable capture'):
            self.scan([cb.ScanError('unavailable')]*3)

    def test_manual_backend_does_not_fall_through(self):
        with patch.object(cb, 'query_active_window', return_value={}), \
             patch.object(cb, 'window_geometry'), patch.object(cb, 'check_focus'), \
             patch.object(cb, 'capture', side_effect=cb.ScanError('missing')) as capture:
            with self.assertRaises(cb.ScanError):
                cb.scan_live('steam', lambda image: image)
            self.assertEqual(capture.call_count, 1)

    def test_steam_only_uses_new_file_and_keeps_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            old = directory/'old.jpg'
            Image.new('RGB', (100,100)).save(old)
            def keypress(*args, **kwargs):
                Image.new('RGB', (100,100), 'red').save(directory/'new.jpg')
            with patch.object(cb, 'steam_directories', return_value=[directory]), \
                 patch.object(cb, 'check_focus'), patch('evdev.UInput') as device:
                device.return_value.__enter__.return_value.write.side_effect = keypress
                im, info = cb.capture('steam', {})
            self.assertEqual(Path(info['steam_path']).name, 'new.jpg')
            self.assertTrue(old.exists())
            self.assertTrue((directory/'new.jpg').exists())
            self.assertGreater(im.getpixel((0,0))[0], 240)

    def test_ambiguous_new_files_rejected(self):
        with patch.object(cb, 'check_focus'):
            with self.assertRaisesRegex(cb.ScanError, 'Multiple new'):
                cb.wait_frame(lambda: [Path('a'), Path('b')], {})

    def test_steam_timeout_does_not_reuse_existing_image(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            Image.new('RGB', (100,100)).save(directory/'old.jpg')
            with patch.object(cb, 'steam_directories', return_value=[directory]), \
                 patch.object(cb, 'check_focus'), patch('evdev.UInput'), \
                 patch.object(cb.time, 'monotonic', side_effect=[0, 0, 6]), \
                 patch.object(cb.time, 'sleep'):
                with self.assertRaisesRegex(cb.ScanError, 'No complete screenshot'):
                    cb.capture('steam', {})

    def test_gamescope_command_uses_owned_socket_and_sdr_game_plane(self):
        def command(args, **kwargs):
            self.assertEqual(args[:4], ['env', 'GAMESCOPE_WAYLAND_DISPLAY=/run/user/1000/gamescope-2',
                                      'gamescopectl', 'screenshot'])
            self.assertEqual(args[-1], '1')
            Image.new('RGB', (100,100)).save(args[-2])
        with patch.object(cb, 'check_focus'), \
             patch.object(cb, 'gamescope_socket', return_value='/run/user/1000/gamescope-2'), \
             patch('automatic_stratagems.scanner.game_capture.run_command', side_effect=command):
            im, info = cb.capture('gamescope', {})
        self.assertEqual(im.size, (100,100))
        self.assertEqual(info['backend'], 'gamescope')

    def test_python_keyboard_releases_and_closes_on_write_failure(self):
        from evdev import ecodes
        with patch('evdev.UInput') as device, patch.object(cb, 'check_focus'), \
             patch.object(cb.time, 'sleep'), patch.object(cb, 'wait_frame') as wait:
            keyboard = device.return_value.__enter__.return_value
            keyboard.syn.side_effect = [OSError('input failed'), None]
            with self.assertRaisesRegex(OSError, 'input failed'):
                cb.steam_frame({}, lambda: [])
            self.assertEqual(keyboard.write.call_args_list[-1].args,
                             (ecodes.EV_KEY, ecodes.KEY_F12, 0))
            device.return_value.__exit__.assert_called_once()
            wait.assert_not_called()

    def test_python_keyboard_checks_focus_before_press(self):
        with patch('evdev.UInput') as device, patch.object(cb.time, 'sleep'), \
             patch.object(cb, 'check_focus', side_effect=cb.FocusChanged('moved')):
            with self.assertRaises(cb.FocusChanged):
                cb.steam_frame({}, lambda: [])
            device.return_value.__enter__.return_value.write.assert_not_called()
            device.return_value.__exit__.assert_called_once()

    def test_desktop_accepts_name_fallbacks_without_three_icons(self):
        report = self.report(['a','b','c','d','e','f','g'], 'mission-name')
        for row in report['rows'][:2]:
            row['method'] = 'mission-icon'
        with patch.object(cb, 'query_active_window', return_value={}), \
             patch.object(cb, 'window_geometry'), patch.object(cb, 'check_focus'), \
             patch.object(cb, 'capture', return_value=self.frame(report, 'desktop')):
            _, info, result = cb.scan_live('desktop', lambda image: image)
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(info['attempts'][0]['matched'], 7)
        self.assertEqual(info['attempts'][0]['icon_matches'], 2)

    def test_zero_recognized_rows_keeps_capture(self):
        report = {'status':'no_detections','rows':[],'warnings':[]}
        result, calls = self.scan([self.frame(report,'gamescope')])
        self.assertEqual(calls,1)
        self.assertEqual(result[2]['status'],'no_detections')

    def test_positive_capture_rejection_tries_next_backend(self):
        frames=[self.frame(self.report(['a','b','c']),'gamescope'),
                self.frame(self.report(['a','b','c']),'steam')]
        with patch.object(cb, 'capture_issue', side_effect=['bad capture',None]):
            result, calls=self.scan(frames)
        self.assertEqual(calls,2)
        self.assertEqual(result[1]['backend'],'steam')

    def test_native_window_skips_gamescope(self):
        with self.assertRaisesRegex(cb.ScanError, 'not running in Gamescope'):
            cb.gamescope_socket({'class': 'steam_app_553850'})

    def test_encoded_image_limit_is_checked_before_open(self):
        with patch.object(gc, 'MAX_ENCODED_IMAGE_BYTES', 8), \
             patch.object(gc.Image, 'open') as open_image:
            with self.assertRaisesRegex(cb.ScanError, 'encoded image is too large'):
                gc.decode_image(b'123456789', 'test')
            open_image.assert_not_called()

    def test_pixel_limit_is_checked_before_load_or_conversion(self):
        source = MagicMock()
        source.size = (100_000, 100_000)
        source.__enter__.return_value = source
        with patch.object(gc.Image, 'open', return_value=source):
            with self.assertRaisesRegex(cb.ScanError, 'dimensions'):
                gc.decode_image(b'png', 'test')
        source.load.assert_not_called()
        source.convert.assert_not_called()

    def test_helper_stdout_is_bounded_while_running(self):
        with patch.object(gc, 'MAX_COMMAND_STDOUT_BYTES', 1024):
            with self.assertRaisesRegex(cb.ScanError, 'stdout exceeded'):
                gc.run_command([sys.executable, '-c',
                                'import os,time;os.write(1,b"x"*100000);time.sleep(60)'],
                               timeout=2)

    def test_helper_stderr_is_bounded_while_running(self):
        with patch.object(gc, 'MAX_COMMAND_STDERR_BYTES', 1024):
            with self.assertRaisesRegex(cb.ScanError, 'stderr exceeded'):
                gc.run_command([sys.executable, '-c',
                                'import os,time;os.write(2,b"x"*100000);time.sleep(60)'],
                               timeout=2)

    def test_helper_closes_pipes_when_reader_thread_cannot_start(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with patch.object(gc.subprocess, 'Popen', return_value=process), \
             patch.object(gc.threading.Thread, 'start', side_effect=RuntimeError('no thread')):
            with self.assertRaisesRegex(RuntimeError, 'no thread'):
                gc.run_command(['helper'])
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
