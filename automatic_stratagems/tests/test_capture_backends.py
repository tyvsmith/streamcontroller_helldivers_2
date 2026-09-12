import os
import json
from concurrent.futures import CancelledError
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from automatic_stratagems import host_commands as hc
from automatic_stratagems.scanner import capture_backends as cb
from automatic_stratagems.scanner import gamescope_flatpak as gf
from automatic_stratagems.scanner import host_metadata as hm


class CaptureBackendsTests(unittest.TestCase):
    def report(self, ids=()):
        return {
            'status': 'partial' if None in ids else ('matched' if ids else 'no_detections'),
            'rows': [{'id': key, 'method': 'mission-icon'} for key in ids],
            'warnings': [],
        }

    def test_only_gamescope_and_screenshot_are_public_sources(self):
        self.assertEqual(cb.BACKENDS, ('auto', 'gamescope', 'screenshot'))

    def test_scan_live_rejects_unknown_sources(self):
        for backend in ('desktop', 'portal', 'steam', 'x11'):
            with self.subTest(backend=backend), \
                 patch.object(cb, 'capture_gamescope') as capture, \
                 self.assertRaisesRegex(cb.ScanError, 'Gamescope'):
                cb.scan_live(backend, lambda image: self.report())
            capture.assert_not_called()

    def test_auto_keeps_matched_gamescope_without_reading_screenshot(self):
        gamescope = Image.new('RGB', (10, 10), 'red')
        report = self.report(('Reinforce',))
        with patch.object(cb, 'capture_gamescope', return_value=(
                gamescope, {'kind': 'live', 'backend': 'gamescope'})), \
             patch.object(cb, 'capture_issue', return_value=None), \
             patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot') as screenshot:
            image, info, actual = cb.scan_live(
                'auto', lambda image: report, screenshot={'kind': 'folder'})
        self.assertIs(image, gamescope)
        self.assertIs(actual, report)
        self.assertEqual(info['backend'], 'gamescope')
        screenshot.assert_not_called()

    def test_auto_uses_screenshot_when_gamescope_is_unavailable(self):
        screenshot = Image.new('RGB', (10, 10), 'blue')
        report = self.report(('Reinforce',))
        config = {'kind': 'folder', 'trigger': 'none'}
        with patch.object(cb, 'capture_gamescope',
                          side_effect=cb.ScanError('no associated game')), \
             patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot',
                   return_value=(screenshot, {'kind': 'file', 'path': '/capture.png'})) as capture, \
             patch.object(cb, 'capture_issue', return_value=None):
            image, info, actual = cb.scan_live(
                'auto', lambda image: report, screenshot=config)
        self.assertIs(image, screenshot)
        self.assertIs(actual, report)
        self.assertEqual(info['backend'], 'screenshot')
        self.assertEqual(info['kind'], 'file')
        self.assertEqual(info['attempts'][0], {
            'backend': 'gamescope', 'status': 'error',
            'reason': 'no associated game'})
        capture.assert_called_once()
        self.assertIs(capture.call_args.args[0], config)

    def test_auto_uses_screenshot_after_gamescope_quality_rejection(self):
        gamescope = Image.new('RGB', (10, 10), 'red')
        screenshot = Image.new('RGB', (10, 10), 'blue')
        saved = []
        with patch.object(cb, 'capture_gamescope', return_value=(
                gamescope, {'kind': 'live', 'backend': 'gamescope'})), \
             patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot',
                   return_value=(screenshot, {'kind': 'file', 'path': '/capture.png'})), \
             patch.object(cb, 'capture_issue', return_value='uniform blank'):
            image, info, _ = cb.scan_live(
                'auto', lambda image: self.report(('Reinforce',)),
                save_attempt=lambda *args: saved.append(args),
                screenshot={'kind': 'folder', 'trigger': 'none'})
        self.assertIs(image, screenshot)
        self.assertEqual(info['backend'], 'screenshot')
        self.assertEqual(saved[0][0], 'gamescope')
        self.assertEqual(saved[0][2]['status'], 'capture_rejected')

    def test_auto_uses_screenshot_after_no_detections(self):
        gamescope = Image.new('RGB', (10, 10), 'red')
        screenshot = Image.new('RGB', (10, 10), 'blue')
        reports = iter((self.report(), self.report(('Reinforce',))))
        with patch.object(cb, 'capture_gamescope', return_value=(
                gamescope, {'kind': 'live', 'backend': 'gamescope'})), \
             patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot',
                   return_value=(screenshot, {'kind': 'file', 'path': '/capture.png'})), \
             patch.object(cb, 'capture_issue', return_value=None):
            image, info, _ = cb.scan_live(
                'auto', lambda image: next(reports),
                screenshot={'kind': 'folder', 'trigger': 'none'})
        self.assertIs(image, screenshot)
        self.assertEqual(info['backend'], 'screenshot')

    def test_auto_uses_screenshot_only_when_it_has_more_matches(self):
        gamescope = Image.new('RGB', (10, 10), 'red')
        screenshot = Image.new('RGB', (10, 10), 'blue')
        reports = iter((self.report(('Reinforce', None)),
                        self.report(('Reinforce', 'SOSBeacon'))))
        with patch.object(cb, 'capture_gamescope', return_value=(
                gamescope, {'kind': 'live', 'backend': 'gamescope'})), \
             patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot',
                   return_value=(screenshot, {'kind': 'file', 'path': '/capture.png'})), \
             patch.object(cb, 'capture_issue', return_value=None):
            image, info, report = cb.scan_live(
                'auto', lambda image: next(reports), screenshot={
                    'kind': 'folder', 'trigger': 'none'})
        self.assertIs(image, screenshot)
        self.assertEqual([row['id'] for row in report['rows']],
                         ['Reinforce', 'SOSBeacon'])
        self.assertEqual(info['backend'], 'screenshot')
        self.assertEqual(len(info['attempts']), 2)

    def test_auto_retains_better_gamescope_partial(self):
        gamescope = Image.new('RGB', (10, 10), 'red')
        screenshot = Image.new('RGB', (10, 10), 'blue')
        gamescope_report = self.report(('Reinforce', 'SOSBeacon', None))
        screenshot_report = self.report(('Reinforce', None))
        with patch.object(cb, 'capture_gamescope', return_value=(
                gamescope, {'kind': 'live', 'backend': 'gamescope'})), \
             patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot',
                   return_value=(screenshot, {'kind': 'file', 'path': '/capture.png'})), \
             patch.object(cb, 'capture_issue', return_value=None):
            image, info, report = cb.scan_live(
                'auto', lambda image: (gamescope_report if image is gamescope
                                       else screenshot_report),
                screenshot={'kind': 'folder', 'trigger': 'none'})
        self.assertIs(image, gamescope)
        self.assertIs(report, gamescope_report)
        self.assertEqual(info['backend'], 'gamescope')
        self.assertIn('Screenshot', report['warnings'][-1])

    def test_auto_retains_gamescope_when_screenshot_match_count_is_equal(self):
        gamescope = Image.new('RGB', (10, 10), 'red')
        screenshot = Image.new('RGB', (10, 10), 'blue')
        gamescope_report = self.report(('Reinforce', None))
        screenshot_report = self.report(('SOSBeacon',))
        with patch.object(cb, 'capture_gamescope', return_value=(
                gamescope, {'kind': 'live', 'backend': 'gamescope'})), \
             patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot',
                   return_value=(screenshot, {'kind': 'file', 'path': '/capture.png'})), \
             patch.object(cb, 'capture_issue', return_value=None):
            image, info, report = cb.scan_live(
                'auto', lambda image: (gamescope_report if image is gamescope
                                       else screenshot_report),
                screenshot={'kind': 'folder', 'trigger': 'none'})
        self.assertIs(image, gamescope)
        self.assertIs(report, gamescope_report)
        self.assertEqual(info['backend'], 'gamescope')

    def test_auto_passes_one_deadline_to_both_capture_attempts(self):
        gamescope = Image.new('RGB', (10, 10), 'red')
        screenshot = Image.new('RGB', (10, 10), 'blue')
        deadline = time.monotonic() + 10
        seen = []

        def capture_screenshot(config, **kwargs):
            seen.append(kwargs['deadline'])
            return screenshot, {'kind': 'file', 'path': '/capture.png'}

        with patch.object(cb, 'capture_gamescope', return_value=(
                gamescope, {'kind': 'live', 'backend': 'gamescope'})) as capture, \
             patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot',
                   side_effect=capture_screenshot), \
             patch.object(cb, 'capture_issue', return_value=None):
            cb.scan_live(
                'auto', lambda image: self.report((None,)), deadline=deadline,
                screenshot={'kind': 'folder', 'trigger': 'none'})
        self.assertEqual(capture.call_args.kwargs['deadline'], deadline)
        self.assertEqual(seen, [deadline])

    def test_explicit_screenshot_does_not_try_gamescope(self):
        screenshot = Image.new('RGB', (10, 10), 'blue')
        with patch.object(cb, 'capture_gamescope') as gamescope, \
             patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot',
                   return_value=(screenshot, {'kind': 'file', 'path': '/capture.png'})):
            image, info, report = cb.scan_live(
                'screenshot', lambda image: self.report(('Reinforce',)),
                screenshot={'kind': 'folder', 'trigger': 'none'})
        self.assertIs(image, screenshot)
        self.assertEqual(info['backend'], 'screenshot')
        self.assertEqual(report['status'], 'matched')
        gamescope.assert_not_called()

    def test_auto_retains_gamescope_partial_when_screenshot_fails(self):
        gamescope = Image.new('RGB', (10, 10), 'red')
        report = self.report(('Reinforce', None))
        with patch.object(cb, 'capture_gamescope', return_value=(
                gamescope, {'kind': 'live', 'backend': 'gamescope'})), \
             patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot',
                   side_effect=cb.ScanError('missing screenshot path')), \
             patch.object(cb, 'capture_issue', return_value=None):
            image, info, actual = cb.scan_live(
                'auto', lambda image: report,
                screenshot={'kind': 'folder', 'trigger': 'none'})
        self.assertIs(image, gamescope)
        self.assertIs(actual, report)
        self.assertEqual(info['attempts'][-1]['status'], 'error')
        self.assertIn('missing screenshot path', report['warnings'][-1])

    def test_auto_cancellation_never_falls_back(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot') as screenshot, \
             self.assertRaises(CancelledError):
            cb.scan_live('auto', lambda image: self.report(),
                         screenshot={'kind': 'folder'}, cancel_event=cancelled)
        screenshot.assert_not_called()

    def test_auto_exhausted_deadline_never_falls_back(self):
        with patch.object(cb, 'capture_gamescope',
                          side_effect=cb.ScanError('capture failed')), \
             patch.object(cb.time, 'monotonic', return_value=2.0), \
             patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot') as screenshot, \
             self.assertRaisesRegex(cb.ScanError, 'deadline'):
            cb.scan_live('auto', lambda image: self.report(),
                         screenshot={'kind': 'folder'}, deadline=1.0)
        screenshot.assert_not_called()

    def test_auto_unconfirmed_host_cleanup_never_falls_back(self):
        with patch.object(cb, 'capture_gamescope',
                          side_effect=hc.HostCleanupUnconfirmed('unknown')), \
             patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot') as screenshot, \
             self.assertRaises(hc.HostCleanupUnconfirmed):
            cb.scan_live('auto', lambda image: self.report(),
                         screenshot={'kind': 'folder'})
        screenshot.assert_not_called()

    def test_gamescope_capture_does_not_query_foreground_window(self):
        self.assertFalse(hasattr(cb, 'check_focus'))
        self.assertFalse(hasattr(cb, 'FocusChanged'))
        self.assertFalse(hasattr(cb, 'query_active_window'))

    def test_capture_uses_unique_socket_and_sdr_game_plane(self):
        def command(args, **kwargs):
            self.assertEqual(args[:2], ['gamescopectl', 'screenshot'])
            self.assertEqual(args[-1], '1')
            self.assertTrue(kwargs['host'])
            self.assertEqual(kwargs['operation'], 'gamescope-capture')
            self.assertEqual(kwargs['host_env'], {
                'GAMESCOPE_WAYLAND_DISPLAY': '/run/user/1000/gamescope-2'})
            Image.new('RGB', (100, 100), 'red').save(args[-2])

        with patch.object(hc, 'shared_host_directory',
                          side_effect=tempfile.TemporaryDirectory), \
             patch.object(cb, 'gamescope_capture_target',
                          return_value={'kind': 'native', 'socket': '/run/user/1000/gamescope-2'}), \
             patch('automatic_stratagems.scanner.game_capture.run_command',
                   side_effect=command):
            image, info = cb.capture_gamescope()
        self.assertEqual(image.size, (100, 100))
        self.assertEqual(info, {
            'kind': 'live', 'backend': 'gamescope',
            'socket': '/run/user/1000/gamescope-2'})

    def test_socket_resolution_and_capture_share_deadline(self):
        deadline = time.monotonic() + 2
        seen = {}

        def socket(*, deadline=None, cancel_event=None):
            seen['socket_deadline'] = deadline
            return {'kind': 'native', 'socket': 'gamescope-2'}

        def command(args, **kwargs):
            seen['command_timeout'] = kwargs['timeout']
            Image.new('RGB', (10, 10), 'red').save(args[-2])

        with patch.object(hc, 'shared_host_directory',
                          side_effect=tempfile.TemporaryDirectory), \
             patch.object(cb, 'gamescope_capture_target', side_effect=socket), \
             patch('automatic_stratagems.scanner.game_capture.run_command',
                   side_effect=command):
            cb.capture_gamescope(deadline=deadline)
        self.assertEqual(seen['socket_deadline'], deadline)
        self.assertGreater(seen['command_timeout'], 0)
        self.assertLessEqual(seen['command_timeout'], 2)

    def test_gamescope_target_auto_discovers_game(self):
        with patch('automatic_stratagems.scanner.host_metadata.resolve_gamescope_capture_target',
                   return_value={'kind': 'native', 'socket': 'gamescope-7'}) as resolve:
            self.assertEqual(cb.gamescope_capture_target(),
                             {'kind': 'native', 'socket': 'gamescope-7'})
        resolve.assert_called_once_with(timeout=5, cancel_event=None)

    def test_gamescope_metadata_failure_is_a_scan_error(self):
        from automatic_stratagems.scanner.host_metadata import HostMetadataError
        with patch('automatic_stratagems.scanner.host_metadata.resolve_gamescope_capture_target',
                   side_effect=HostMetadataError('multiple games')):
            with self.assertRaisesRegex(cb.ScanError, 'multiple games'):
                cb.gamescope_capture_target()

    def test_shared_capture_directory_is_removed(self):
        with tempfile.TemporaryDirectory(prefix='hd2 base with spaces ') as base:
            with hc.create_host_job(base=Path(base), hard_timeout=3) as job, \
                 patch.dict(os.environ, {
                     'FLATPAK_ID': 'com.core447.StreamController',
                     **hc.host_job_environment(job)}, clear=False), \
                 patch.object(cb, 'gamescope_capture_target', return_value={'kind': 'native', 'socket': 'gamescope-2'}), \
                 patch('automatic_stratagems.scanner.game_capture.run_command') as run:
                def write_image(args, **kwargs):
                    path = Path(args[-2])
                    self.assertTrue(path.is_relative_to(job.path))
                    Image.new('RGB', (100, 100), 'red').save(path)
                run.side_effect = write_image
                cb.capture_gamescope()
                self.assertFalse(any(job.path.glob('gamescope-*')))

    def flatpak_target(self):
        return hm.FlatpakGamescopeTarget(
            game_pid=100, game_start_time='1000', sandbox_pid=50,
            sandbox_start_time='500', instance_id='123',
            socket='/run/user/1000/gamescope-4', socket_dev=1, socket_ino=2,
            host_cache='/host/steam/cache', sandbox_cache='/steam/cache',
            cache_dev=3, cache_ino=4, gamescopectl=hm.GAMESCOPECTL_PATH)

    def test_flatpak_capture_transfers_then_decodes_in_scanner(self):
        target = self.flatpak_target()
        cancelled = threading.Event()
        deadline = time.monotonic() + 2
        outputs = []
        operations = []

        def command(argv, **kwargs):
            operation = kwargs['operation']
            operations.append(operation)
            self.assertTrue(kwargs['host'])
            if operation == 'gamescope-capture-target':
                return json.dumps({'kind': 'steam-flatpak',
                                   'target': target.to_dict()}).encode()
            if operation == 'gamescope-flatpak-capture':
                self.assertEqual(json.loads(argv[3]), target.to_dict())
                self.assertIs(kwargs['cancel_event'], cancelled)
                self.assertEqual(float(argv[6]), deadline)
                self.assertLessEqual(kwargs['timeout'], 2)
                output = Path(argv[4])
                Image.new('RGB', (23, 17), 'blue').save(output)
                outputs.append(output)
            else:
                self.assertEqual(operation, 'gamescope-flatpak-cleanup')
                self.assertNotIn('cancel_event', kwargs)
            return b''

        with tempfile.TemporaryDirectory() as base:
            with hc.create_host_job(base=Path(base), hard_timeout=5) as job, \
                 patch.dict(os.environ, hc.host_job_environment(job)), \
                 patch('automatic_stratagems.scanner.game_capture.run_command',
                       side_effect=command):
                image, info = cb.capture_gamescope(
                    deadline=deadline, cancel_event=cancelled)
            self.assertFalse(job.path.exists())
        self.assertEqual(image.size, (23, 17))
        self.assertEqual(image.getpixel((0, 0)), (0, 0, 255))
        self.assertEqual(info['transport'], 'flatpak-enter')
        self.assertEqual(info['compatibility'], 'unverified')
        self.assertEqual(operations, ['gamescope-capture-target',
                                     'gamescope-flatpak-capture',
                                     'gamescope-flatpak-cleanup'])
        self.assertFalse(outputs[0].parent.exists())

    def test_failed_steam_cleanup_retains_record_and_prevents_fallback(self):
        target = self.flatpak_target()
        records = []

        def command(argv, **kwargs):
            operation = kwargs['operation']
            if operation == 'gamescope-capture-target':
                return json.dumps({'kind': 'steam-flatpak',
                                   'target': target.to_dict()}).encode()
            if operation == 'gamescope-flatpak-capture':
                record = Path(argv[4]).parent / gf.RECORD_NAME
                record.write_text('owned Steam capture identity')
                records.append(record)
                raise CancelledError()
            self.assertEqual(operation, 'gamescope-flatpak-cleanup')
            raise cb.ScanError('cleanup denied')

        with tempfile.TemporaryDirectory() as base:
            with self.assertRaises(hc.HostArtifactCleanupError):
                with hc.create_host_job(base=Path(base), hard_timeout=5) as job, \
                     patch.dict(os.environ, hc.host_job_environment(job)), \
                     patch('automatic_stratagems.scanner.game_capture.run_command',
                           side_effect=command), \
                     patch('automatic_stratagems.scanner.screenshot_capture.capture_screenshot') as fallback:
                    cb.scan_live('auto', lambda image: self.report(),
                                 screenshot={'kind': 'folder'})
            fallback.assert_not_called()
            self.assertEqual(records[0].read_text(), 'owned Steam capture identity')
            self.assertTrue(job.path.exists())

    def test_metadata_cleanup_uncertainty_stops_before_capture(self):
        with patch('automatic_stratagems.scanner.host_metadata.resolve_gamescope_capture_target',
                   side_effect=hc.HostCleanupUnconfirmed('still running')), \
             patch('automatic_stratagems.scanner.game_capture.run_command') as native, \
             self.assertRaises(hc.HostCleanupUnconfirmed):
            cb.capture_gamescope()
        native.assert_not_called()

    def test_wait_frame_cancellation_stops_before_polling(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(cb, 'read_frame') as read:
            with self.assertRaises(CancelledError):
                cb.wait_frame(lambda: [], cancel_event=cancelled)
        read.assert_not_called()

    def test_wait_frame_deadline_is_diagnostic(self):
        with patch.object(cb.time, 'monotonic', side_effect=[1.0, 2.0, 2.0]):
            with self.assertRaisesRegex(cb.ScanError, 'deadline'):
                cb.wait_frame(lambda: [], deadline=1.5)

    def test_ambiguous_frames_are_rejected(self):
        with self.assertRaisesRegex(cb.ScanError, 'Multiple'):
            cb.wait_frame(lambda: [Path('a'), Path('b')])

    def test_growing_screenshot_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'growing.png'
            path.write_bytes(b'12345678')
            real_read = os.read
            grown = False

            def grow_then_read(descriptor, count):
                nonlocal grown
                if not grown:
                    grown = True
                    with path.open('ab') as stream:
                        stream.write(b'9')
                return real_read(descriptor, count)

            with patch.object(cb, 'MAX_ENCODED_IMAGE_BYTES', 8), \
                 patch.object(cb.os, 'read', side_effect=grow_then_read), \
                 self.assertRaisesRegex(cb.ScanError, 'encoded image is too large'):
                cb.read_frame(path)

    def test_screenshot_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target.png'
            Image.new('RGB', (10, 10), 'red').save(target)
            link = root / 'capture.png'
            link.symlink_to(target)
            with self.assertRaisesRegex(cb.ScanError, 'Cannot read screenshot'):
                cb.read_frame(link)

    def test_screenshot_change_during_read_is_rejected(self):
        before = type('Metadata', (), {
            'st_mode': 0o100600, 'st_dev': 1, 'st_ino': 2,
            'st_size': 3, 'st_mtime_ns': 4})()
        after = type('Metadata', (), {
            'st_mode': 0o100600, 'st_dev': 1, 'st_ino': 2,
            'st_size': 4, 'st_mtime_ns': 5})()
        with patch.object(cb.os, 'open', return_value=7), \
             patch.object(cb.os, 'fstat', side_effect=[before, after]), \
             patch.object(cb.os, 'read', side_effect=[b'png', b'']), \
             patch.object(cb.os, 'close'), \
             self.assertRaisesRegex(cb.ScanError, 'changed'):
            cb.read_frame(Path('capture.png'))

    def test_uniform_capture_is_rejected_without_new_darkness_threshold(self):
        self.assertIn('uniform blank', cb.capture_issue(
            Image.new('RGB', (512, 216), 'gray'), 'gamescope'))
        textured = Image.new('RGB', (512, 216), (1, 1, 1))
        textured.putpixel((0, 0), (255, 255, 255))
        self.assertIsNone(cb.capture_issue(textured, 'gamescope'))

    def test_scan_keeps_first_usable_frame_even_without_detections(self):
        report = self.report()
        frame = Image.new('RGB', (10, 10), 'red')
        with patch.object(cb, 'capture_gamescope', return_value=(
                frame, {'kind': 'live', 'backend': 'gamescope'})), \
             patch.object(cb, 'capture_issue', return_value=None):
            image, info, actual = cb.scan_live('gamescope', lambda image: report)
        self.assertIs(image, frame)
        self.assertIs(actual, report)
        self.assertEqual(info['attempts'], [{
            'backend': 'gamescope', 'status': 'no_detections',
            'matched': 0, 'icon_matches': 0}])

    def test_rejected_capture_is_saved_and_fails_without_fallback(self):
        image = Image.new('RGB', (10, 10))
        saved = []
        with patch.object(cb, 'capture_gamescope', return_value=(
                image, {'kind': 'live', 'backend': 'gamescope'})), \
             patch.object(cb, 'capture_issue', return_value='bad capture'), \
             self.assertRaisesRegex(cb.ScanError, 'No usable capture.*bad capture'):
            cb.scan_live('gamescope', lambda image: self.report(),
                         save_attempt=lambda *args: saved.append(args))
        self.assertEqual(saved[0][0], 'gamescope')
        self.assertEqual(saved[0][2]['status'], 'capture_rejected')

    def test_cancellation_after_capture_stops_before_prepare(self):
        cancelled = threading.Event()

        def capture(**kwargs):
            cancelled.set()
            return Image.new('RGB', (10, 10)), {'backend': 'gamescope'}

        with patch.object(cb, 'capture_gamescope', side_effect=capture):
            with self.assertRaises(CancelledError):
                cb.scan_live('gamescope', lambda image: self.report(),
                             prepare=lambda image: self.fail('prepare called'),
                             cancel_event=cancelled)

    def test_preparation_finishing_after_deadline_stops_before_recognition(self):
        clock = [0.0]

        def prepare(image):
            clock[0] = 2.0
            return image

        with patch.object(cb.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(cb, 'capture_gamescope', return_value=(
                 Image.new('RGB', (10, 10)), {'backend': 'gamescope'})), \
             patch.object(cb, 'capture_issue', return_value=None), \
             patch.object(self, 'report') as recognize, \
             self.assertRaisesRegex(cb.ScanError, 'deadline'):
            cb.scan_live('gamescope', recognize, prepare=prepare, deadline=1.0)
        recognize.assert_not_called()


if __name__ == '__main__':
    unittest.main()
