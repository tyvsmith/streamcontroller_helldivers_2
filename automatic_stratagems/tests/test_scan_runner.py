import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from automatic_stratagems import scan_runner


VALID_REPORT = {
    "schema_version": 1,
    "phase": 1,
    "status": "matched",
    "rows": [{"id": "Reinforce", "name": "Reinforce", "sequence": ["UP"]}],
    "warnings": [],
}


class RunnerTests(unittest.TestCase):
    def test_host_command_runs_root_environment_module_without_shell(self):
        command = scan_runner.scan_command('/tmp/a b', flatpak=True, backend='steam', workers=4)
        self.assertEqual(command[:4], ['flatpak-spawn', '--host', '--watch-bus',
                                      '--directory=/tmp/a b'])
        self.assertEqual(command[4:7], ['/tmp/a b/.venv/bin/python', '-m',
                                       'automatic_stratagems.scanner'])
        self.assertIn('--json', command)
        self.assertEqual(command[command.index('--budget-seconds') + 1], '110')
        self.assertNotIn('--debug-dir', command)
        self.assertEqual(command[-4:], ['--workers', '4', '--capture-backend', 'steam'])

    def test_native_command_preserves_path_with_spaces_and_optional_debug(self):
        command = scan_runner.scan_command('/tmp/a b', flatpak=False,
                                           debug_dir=Path('/tmp/private run'))
        self.assertEqual(command[0], '/tmp/a b/.venv/bin/python')
        self.assertEqual(command[-2:], ['--debug-dir', '/tmp/private run'])

    def test_worker_settings_are_bounded_and_invalid_values_use_default(self):
        for value, expected in ((None, 2), ('bad', 2), (True, 2), (0, 1),
                                (99, 32), ('4', 4)):
            with self.subTest(value=value):
                self.assertEqual(scan_runner.scan_workers(value), expected)

    def run_script(self, source, *, cancel_event=None, timeout=2,
                   stdout_limit=None, stderr_limit=None):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            python = root / '.venv/bin/python'
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            command = [str(python), '-c', source]
            with patch.object(scan_runner, 'scan_command', return_value=command), \
                 patch.object(scan_runner, 'check_scan_setup'), \
                 patch.object(scan_runner, 'SCAN_TIMEOUT_SECONDS', timeout), \
                 patch.object(scan_runner, 'MAX_STDOUT_BYTES',
                              stdout_limit or scan_runner.MAX_STDOUT_BYTES), \
                 patch.object(scan_runner, 'MAX_STDERR_BYTES',
                              stderr_limit or scan_runner.MAX_STDERR_BYTES):
                return scan_runner.run_scan(root, cancel_event=cancel_event)

    def test_partial_is_a_valid_report(self):
        report = dict(VALID_REPORT, status='partial',
                      rows=[{"id": None, "name": "unknown", "sequence": None}])
        result = self.run_script(f'import json; print(json.dumps({report!r}))')
        self.assertEqual(result['status'], 'partial')

    def test_rejects_invalid_schema_at_one_boundary(self):
        report = dict(VALID_REPORT, schema_version=99)
        with self.assertRaisesRegex(ValueError, 'schema version'):
            self.run_script(f'import json; print(json.dumps({report!r}))')

    def test_rejects_boolean_schema_version(self):
        report = dict(VALID_REPORT, schema_version=True)
        with self.assertRaisesRegex(ValueError, 'schema version'):
            self.run_script(f'import json; print(json.dumps({report!r}))')

    def test_rejects_excessively_nested_json(self):
        with patch.object(scan_runner.json, 'loads', side_effect=RecursionError):
            with self.assertRaisesRegex(ValueError, 'invalid JSON'):
                self.run_script(f'import json; print(json.dumps({VALID_REPORT!r}))')

    def test_rejects_unbounded_rows(self):
        report = dict(VALID_REPORT, rows=VALID_REPORT['rows'] * 33)
        with self.assertRaisesRegex(ValueError, 'rows'):
            self.run_script(f'import json; print(json.dumps({report!r}))')

    def test_cancellation_before_spawn_is_idempotent(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(scan_runner.subprocess, 'Popen') as popen:
            with self.assertRaises(scan_runner.CancelledError):
                scan_runner.run_scan('/tmp/missing', cancel_event=cancelled)
            popen.assert_not_called()

    def test_cancellation_terminates_and_reaps_child_process_group(self):
        if not Path('/proc').is_dir():
            self.skipTest('requires procfs')
        with tempfile.TemporaryDirectory() as directory:
            child_pid = Path(directory) / 'child.pid'
            grandchild_pid = Path(directory) / 'grandchild.pid'
            source = (
                'import pathlib,subprocess,sys,time; '
                f'pathlib.Path({str(child_pid)!r}).write_text(str(__import__("os").getpid())); '
                f'p=subprocess.Popen([sys.executable,"-c","import os,time,pathlib;pathlib.Path({str(grandchild_pid)!r}).write_text(str(os.getpid()));time.sleep(60)"]); '
                'time.sleep(60)')
            cancel = threading.Event()
            timer = threading.Thread(target=self._cancel_after_files,
                                     args=(cancel, child_pid, grandchild_pid), daemon=True)
            timer.start()
            with self.assertRaises(scan_runner.CancelledError):
                self.run_script(source, cancel_event=cancel, timeout=10)
            timer.join(2)
            for path in (child_pid, grandchild_pid):
                pid = int(path.read_text())
                self.assert_process_stopped(pid)

    def test_cancellation_kills_term_ignoring_grandchild(self):
        if not Path('/proc').is_dir():
            self.skipTest('requires procfs')
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'grandchild.pid'
            grandchild = (
                'import os,pathlib,signal,time;'
                'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));'
                'time.sleep(60)')
            source = (f'import subprocess,sys,time; subprocess.Popen('
                      f'[sys.executable,"-c",{grandchild!r}]); time.sleep(60)')
            cancel = threading.Event()
            timer = threading.Thread(target=self._cancel_after_files,
                                     args=(cancel, pid_file), daemon=True)
            timer.start()
            with self.assertRaises(scan_runner.CancelledError):
                self.run_script(source, cancel_event=cancel, timeout=10)
            self.assert_process_stopped(int(pid_file.read_text()))

    def test_natural_leader_exit_cleans_descendant_holding_output_pipe(self):
        if not Path('/proc').is_dir():
            self.skipTest('requires procfs')
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'grandchild.pid'
            grandchild = (
                'import os,pathlib,time;'
                f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));'
                'time.sleep(60)')
            source = (f'import json,subprocess,sys,time; subprocess.Popen('
                      f'[sys.executable,"-c",{grandchild!r}]); '
                      f'print(json.dumps({VALID_REPORT!r}),flush=True); '
                      'time.sleep(.05)')
            self.assertEqual(self.run_script(source, timeout=5)['status'], 'matched')
            self.assert_process_stopped(int(pid_file.read_text()))

    def test_natural_leader_exit_cleans_descendant_with_closed_pipes(self):
        if not Path('/proc').is_dir():
            self.skipTest('requires procfs')
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'grandchild.pid'
            grandchild = (
                'import os,pathlib,time;'
                f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));'
                'time.sleep(60)')
            source = (f'import json,subprocess,sys,time; subprocess.Popen('
                      f'[sys.executable,"-c",{grandchild!r}],'
                      'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
                      f'print(json.dumps({VALID_REPORT!r}),flush=True); time.sleep(.05)')
            self.assertEqual(self.run_script(source, timeout=5)['status'], 'matched')
            self.assert_process_stopped(int(pid_file.read_text()))

    def assert_process_stopped(self, pid):
        status = Path(f'/proc/{pid}/status')
        if status.exists():
            state = next(line for line in status.read_text().splitlines()
                         if line.startswith('State:'))
            self.assertIn('Z (zombie)', state, f'process {pid} survived: {state}')

    @staticmethod
    def _cancel_after_files(event, *paths):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not all(path.exists() for path in paths):
            time.sleep(.01)
        event.set()

    def test_timeout_terminates_process(self):
        with self.assertRaisesRegex(TimeoutError, 'timed out'):
            self.run_script('import time; time.sleep(60)', timeout=.1)

    def test_forced_stop_does_not_return_before_group_exit(self):
        alive = threading.Event()
        alive.set()

        class Process:
            pid = 123456
            returncode = None
            waited = False

            def poll(self):
                return self.returncode

            def wait(self):
                self.waited = True
                self.returncode = -signal.SIGKILL

        process = Process()
        errors = []
        with patch.object(scan_runner.os, 'killpg') as killpg, \
             patch.object(scan_runner.log, 'error') as log_error, \
             patch.object(scan_runner, '_group_has_live_members',
                          side_effect=lambda _pid: alive.is_set()):
            worker = threading.Thread(
                target=lambda: self._record_error(
                    errors, scan_runner._stop_and_reap, process, 0), daemon=True)
            worker.start()
            deadline = time.monotonic() + 1
            while killpg.call_count < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(worker.is_alive())
            self.assertFalse(process.waited)
            log_error.assert_called_once()
            alive.clear()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(process.waited)

    @staticmethod
    def _record_error(errors, function, *args):
        try:
            function(*args)
        except BaseException as error:
            errors.append(error)

    def test_stdout_overflow_terminates_process(self):
        with self.assertRaisesRegex(ValueError, 'stdout exceeded'):
            self.run_script('import os; os.write(1, b"x" * 100000); import time; time.sleep(60)',
                            stdout_limit=1024)

    def test_stderr_overflow_terminates_process(self):
        with self.assertRaisesRegex(ValueError, 'stderr exceeded'):
            self.run_script('import os; os.write(2, b"x" * 100000); import time; time.sleep(60)',
                            stderr_limit=1024)

    def test_stderr_only_failure_is_actionable_and_bounded(self):
        with self.assertRaisesRegex(ValueError, 'dependency unavailable'):
            self.run_script('import sys; sys.stderr.write("dependency unavailable\\n"); sys.exit(1)')

    def test_missing_interpreter_is_actionable(self):
        with self.assertRaisesRegex(scan_runner.ScanSetupError,
                                    'automatic_stratagems/requirements.txt'):
            scan_runner.check_scan_setup('/tmp/root-that-does-not-exist', flatpak=False)

    def test_auto_preflight_checks_common_focus_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            interpreter = root / '.venv/bin/python'
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
            with patch.object(scan_runner, '_run_owned', return_value=(0, b'', b'')) as run:
                scan_runner.check_scan_setup(root, flatpak=False, backend='auto')
            script = run.call_args.args[0][-1]
            self.assertIn("('hyprctl',)", script)

    def test_flatpak_scanning_fails_preflight_until_forced_teardown_is_safe(self):
        with patch.object(scan_runner.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(scan_runner.ScanSetupError,
                                        'process-tree teardown'):
                scan_runner.check_scan_setup('/tmp/repo', flatpak=True)
            popen.assert_not_called()

    def test_diagnostics_use_private_owned_run_directory(self):
        with tempfile.TemporaryDirectory() as parent:
            cache = Path(parent) / 'diagnostics'
            run = scan_runner.prepare_diagnostic_run(cache)
            self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
            self.assertEqual(run.stat().st_mode & 0o777, 0o700)
            marker = json.loads((run / scan_runner.RUN_MARKER).read_text())
            self.assertEqual(marker['owner'], scan_runner.DIAGNOSTIC_OWNER)
            self.assertEqual(marker['status'], 'open')
            scan_runner.close_diagnostic_run(run, 'complete')
            self.assertEqual(json.loads((run / scan_runner.RUN_MARKER).read_text())['status'],
                             'complete')

    def test_diagnostics_reject_symlink_and_unowned_cache(self):
        with tempfile.TemporaryDirectory() as parent:
            parent = Path(parent)
            target = parent / 'target'
            target.mkdir()
            link = parent / 'link'
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                scan_runner.prepare_diagnostic_run(link)
            with self.assertRaisesRegex(ValueError, 'not owned'):
                scan_runner.prepare_diagnostic_run(target)
            self.assertEqual(list(target.iterdir()), [])

    def test_diagnostics_reject_fifo_marker_without_reading_it(self):
        with tempfile.TemporaryDirectory() as parent:
            cache = Path(parent) / 'diagnostics'
            cache.mkdir()
            os.mkfifo(cache / scan_runner.CACHE_MARKER)
            with patch.object(Path, 'read_text', side_effect=AssertionError('read FIFO')):
                with self.assertRaisesRegex(ValueError, 'not owned'):
                    scan_runner.prepare_diagnostic_run(cache)

    def test_diagnostic_pruning_preserves_open_and_unowned_directories(self):
        with tempfile.TemporaryDirectory() as parent, \
             patch.object(scan_runner, 'MAX_DIAGNOSTIC_RUNS', 1):
            cache = Path(parent) / 'diagnostics'
            old = scan_runner.prepare_diagnostic_run(cache)
            scan_runner.close_diagnostic_run(old, 'complete')
            time.sleep(.01)
            kept = scan_runner.prepare_diagnostic_run(cache)
            unrelated = cache / 'unrelated'
            unrelated.mkdir()
            scan_runner.prune_diagnostics(cache)
            self.assertFalse(old.exists())
            self.assertTrue(kept.exists())
            self.assertTrue(unrelated.exists())

    def test_diagnostic_pruning_closes_abandoned_owned_run(self):
        with tempfile.TemporaryDirectory() as parent, \
             patch.object(scan_runner, 'MAX_DIAGNOSTIC_RUNS', 1):
            cache = Path(parent) / 'diagnostics'
            abandoned = scan_runner.prepare_diagnostic_run(cache)
            marker_path = abandoned / scan_runner.RUN_MARKER
            marker = json.loads(marker_path.read_text())
            marker['runner']['pid'] = 2_000_000_000
            scan_runner._write_marker(marker_path, marker)
            live = scan_runner.prepare_diagnostic_run(cache)
            scan_runner.prune_diagnostics(cache)
            self.assertFalse(abandoned.exists())
            self.assertTrue(live.exists())

    def test_diagnostic_pruning_preserves_malformed_open_identity(self):
        with tempfile.TemporaryDirectory() as parent, \
             patch.object(scan_runner, 'MAX_DIAGNOSTIC_RUNS', 0):
            cache = Path(parent) / 'diagnostics'
            run = scan_runner.prepare_diagnostic_run(cache)
            marker_path = run / scan_runner.RUN_MARKER
            marker = json.loads(marker_path.read_text())
            marker['runner'] = {'pid': os.getpid(), 'boot_id': 'malformed',
                                'start_time': 'not-a-clock-tick'}
            scan_runner._write_marker(marker_path, marker)
            scan_runner.prune_diagnostics(cache)
            self.assertTrue(run.exists())
            self.assertEqual(json.loads(marker_path.read_text())['status'], 'open')

    def test_diagnostic_pruning_preserves_open_run_when_proc_is_unreadable(self):
        with tempfile.TemporaryDirectory() as parent, \
             patch.object(scan_runner, 'MAX_DIAGNOSTIC_RUNS', 0):
            cache = Path(parent) / 'diagnostics'
            run = scan_runner.prepare_diagnostic_run(cache)
            original_read = Path.read_text

            def read(path, *args, **kwargs):
                if str(path).startswith('/proc/'):
                    raise PermissionError('proc unavailable')
                return original_read(path, *args, **kwargs)

            with patch.object(Path, 'read_text', read):
                scan_runner.prune_diagnostics(cache)
            self.assertTrue(run.exists())
            self.assertEqual(json.loads((run / scan_runner.RUN_MARKER).read_text())['status'],
                             'open')

    def test_diagnostic_failure_does_not_mask_cancellation(self):
        with patch.object(scan_runner, 'is_flatpak', return_value=False), \
             patch.object(scan_runner, 'check_scan_setup'), \
             patch.object(scan_runner, 'prepare_diagnostic_run',
                          return_value=Path('/tmp/owned-run')), \
             patch.object(scan_runner, '_run_owned', side_effect=scan_runner.CancelledError()), \
             patch.object(scan_runner, 'close_diagnostic_run',
                          side_effect=ValueError('marker damaged')), \
             patch.object(scan_runner, 'prune_diagnostics'), \
             patch.object(scan_runner.log, 'warning') as warning:
            with self.assertRaises(scan_runner.CancelledError):
                scan_runner.run_scan('/tmp/root', debug_dir='/tmp/cache')
            warning.assert_called_once()


if __name__ == '__main__':
    unittest.main()
