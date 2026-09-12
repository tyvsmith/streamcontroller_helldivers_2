import os
import sys
import threading
import unittest
from concurrent.futures import CancelledError
from unittest.mock import MagicMock, patch

from automatic_stratagems import host_commands as hc
from automatic_stratagems.scanner import game_capture as gc


class GameCapturePrimitivesTests(unittest.TestCase):
    def test_encoded_image_limit_is_checked_before_open(self):
        with patch.object(gc, 'MAX_ENCODED_IMAGE_BYTES', 8), \
             patch.object(gc.Image, 'open') as open_image, \
             self.assertRaisesRegex(gc.ScanError, 'encoded image is too large'):
            gc.decode_image(b'123456789', 'test')
        open_image.assert_not_called()

    def test_pixel_limit_is_checked_before_load_or_conversion(self):
        source = MagicMock()
        source.size = (100_000, 100_000)
        source.__enter__.return_value = source
        with patch.object(gc.Image, 'open', return_value=source), \
             self.assertRaisesRegex(gc.ScanError, 'dimensions'):
            gc.decode_image(b'png', 'test')
        source.load.assert_not_called()
        source.convert.assert_not_called()

    def test_helper_stdout_is_bounded_while_running(self):
        with patch.object(gc, 'MAX_COMMAND_STDOUT_BYTES', 1024), \
             self.assertRaisesRegex(gc.ScanError, 'stdout exceeded'):
            gc.run_command([sys.executable, '-c',
                            'import os,time;os.write(1,b"x"*100000);time.sleep(60)'],
                           timeout=2)

    def test_helper_stderr_is_bounded_while_running(self):
        with patch.object(gc, 'MAX_COMMAND_STDERR_BYTES', 1024), \
             self.assertRaisesRegex(gc.ScanError, 'stderr exceeded'):
            gc.run_command([sys.executable, '-c',
                            'import os,time;os.write(2,b"x"*100000);time.sleep(60)'],
                           timeout=2)

    def test_helper_closes_pipes_when_reader_thread_cannot_start(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with patch.object(gc.subprocess, 'Popen', return_value=process), \
             patch.object(gc.threading.Thread, 'start',
                          side_effect=RuntimeError('no thread')), \
             self.assertRaisesRegex(RuntimeError, 'no thread'):
            gc.run_command(['helper'])
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def test_helper_cancellation_before_spawn_is_side_effect_free(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(gc.subprocess, 'Popen') as popen, \
             self.assertRaises(CancelledError):
            gc.run_command(['helper'], cancel_event=cancelled)
        popen.assert_not_called()

    def test_helper_reports_permission_denied_at_spawn(self):
        with patch.object(gc.subprocess, 'Popen',
                          side_effect=PermissionError('denied')), \
             self.assertRaisesRegex(gc.ScanError, 'Cannot start command.*denied'):
            gc.run_command(['helper'])

    def _flatpak_runner(self, command, **kwargs):
        stack = [
            patch.dict(os.environ, {'FLATPAK_ID': 'com.core447.StreamController'}),
            patch.object(gc.shutil, 'which', return_value='/usr/bin/flatpak-spawn'),
            patch('automatic_stratagems.host_commands.guard_command',
                  return_value=(['guarded', 'space ; $()'], MagicMock())),
            patch('automatic_stratagems.host_commands.launcher_command',
                  side_effect=lambda command, operation: command),
            patch('automatic_stratagems.host_commands.reconcile_operation'),
            patch('automatic_stratagems.host_commands.wait_operation'),
            patch.object(gc.subprocess, 'Popen'), patch.object(gc, '_read_bounded'),
        ]
        entered = [item.start() for item in stack]
        self.addCleanup(lambda: [item.stop() for item in reversed(stack)])
        process = entered[-2].return_value
        process.poll.return_value = 0
        process.returncode = 0
        process.stdout = MagicMock()
        process.stderr = MagicMock()
        gc.run_command(command, host=True, **kwargs)
        return entered[-2]

    def test_host_runner_prefixes_only_inside_flatpak(self):
        popen = self._flatpak_runner(['gamescopectl', 'screenshot'])
        self.assertEqual(popen.call_args.args[0], [
            'flatpak-spawn', '--host', '--watch-bus', '--directory=/',
            'guarded', 'space ; $()'])

    def test_host_runner_forwards_environment_without_shell_parsing(self):
        popen = self._flatpak_runner(
            ['gamescopectl', 'screenshot'],
            host_env={'GAMESCOPE_WAYLAND_DISPLAY': 'gamescope-7'})
        self.assertEqual(popen.call_args.args[0], [
            'flatpak-spawn', '--host', '--watch-bus',
            '--env=GAMESCOPE_WAYLAND_DISPLAY=gamescope-7', '--directory=/',
            'guarded', 'space ; $()'])

    def test_host_runner_fails_before_reservation_without_transport(self):
        with patch.dict(os.environ, {'FLATPAK_ID': 'com.core447.StreamController'}), \
             patch.object(gc.shutil, 'which', return_value=None), \
             patch('automatic_stratagems.host_commands.guard_command') as guard, \
             self.assertRaisesRegex(gc.ScanError, 'flatpak-spawn is unavailable'):
            gc.run_command(['gamescopectl'], host=True)
        guard.assert_not_called()

    def test_exited_wrapper_does_not_bypass_submitted_host_reconciliation(self):
        operation = MagicMock()
        with patch.dict(os.environ, {'FLATPAK_ID': 'com.core447.StreamController'}), \
             patch.object(gc.shutil, 'which', return_value='/usr/bin/flatpak-spawn'), \
             patch('automatic_stratagems.host_commands.guard_command',
                   return_value=(['guarded'], operation)), \
             patch('automatic_stratagems.host_commands.launcher_command',
                   side_effect=lambda command, item: command), \
             patch('automatic_stratagems.host_commands.reconcile_operation',
                   return_value=False) as reconcile, \
             patch('automatic_stratagems.host_commands.wait_operation',
                   side_effect=hc.HostCleanupUnconfirmed('unknown')), \
             patch('automatic_stratagems.host_commands.mark_not_started') as mark, \
             patch.object(gc.subprocess, 'Popen') as popen, \
             patch.object(gc, '_read_bounded'):
            process = popen.return_value
            process.poll.return_value = 1
            process.returncode = 1
            process.stdout = MagicMock()
            process.stderr = MagicMock()
            with self.assertRaises(hc.HostCleanupUnconfirmed):
                gc.run_command(['gamescopectl'], host=True)
        reconcile.assert_called_once_with(operation, 1)
        mark.assert_not_called()

    def test_host_runner_stays_direct_on_native(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(gc.Path, 'exists', return_value=False), \
             patch.object(gc.subprocess, 'Popen') as popen, \
             patch.object(gc, '_read_bounded'):
            process = popen.return_value
            process.poll.return_value = 0
            process.returncode = 0
            process.stdout = MagicMock()
            process.stderr = MagicMock()
            gc.run_command(['gamescopectl'], host=True)
        self.assertEqual(popen.call_args.args[0], ['gamescopectl'])
        self.assertNotIn('start_new_session', popen.call_args.kwargs)
        self.assertNotIn('process_group', popen.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
