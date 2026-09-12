import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
import io
from contextlib import redirect_stdout
from contextlib import contextmanager
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from PIL import Image

from automatic_stratagems import scan_diagnostics, scan_runner
from automatic_stratagems.provision.scanner_runtime import ScannerRuntime
from automatic_stratagems.scanner import scan_game


VALID_REPORT = {
    "schema_version": 1,
    "status": "matched",
    "rows": [{"id": "Reinforce", "name": "Reinforce", "sequence": ["UP"]}],
    "warnings": [],
}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        environment = patch.dict(os.environ, {'XDG_CACHE_HOME': cache.name})
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def runtime(interpreter='/sandbox/python', *, profile='sandbox', env=None):
        return ScannerRuntime(Path(interpreter), MappingProxyType(env or {}),
                              profile, None)

    def test_auto_accepts_only_gamescope_live_metadata_with_screenshot_fallback(self):
        source = {'kind': 'folder', 'path': '/shared/captures'}
        report = {**VALID_REPORT, 'source': {
            'kind': 'live', 'backend': 'gamescope', 'socket': '/run/gamescope-1'}}
        self.assertIsNone(scan_runner.validate_image_source_report(
            report, source, backend='auto'))
        for backend in ('screenshot', 'gamescope'):
            with self.subTest(backend=backend), self.assertRaises(ValueError):
                scan_runner.validate_image_source_report(report, source, backend=backend)
        for changes in ({'backend': 'desktop'}, {'socket': ''}, {'kind': 'file'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                scan_runner.validate_image_source_report(
                    {**report, 'source': {**report['source'], **changes}}, source,
                    backend='auto')

    def test_auto_defers_incomplete_script_validation_until_fallback(self):
        source = {'kind': 'folder', 'path': 'unfinished-folder', 'trigger': 'script', 'script': ''}
        command = scan_runner.scan_command('/repo', backend='auto',
            image_source=source, _runtime=self.runtime())
        self.assertEqual(command[command.index('--screenshot-script') + 1], '')
        with self.assertRaises(ValueError):
            scan_runner.scan_command('/repo', backend='screenshot',
                image_source=source, _runtime=self.runtime())

    def test_auto_preflight_does_not_create_an_unused_host_job(self):
        source = {'kind': 'folder', 'path': '', 'trigger': 'script', 'script': ''}
        with patch.object(scan_runner, 'resolve_scanner_runtime', return_value=self.runtime()), \
             patch.object(scan_runner, '_run_owned', return_value=(0, b'', b'')), \
             patch.object(scan_runner, 'create_host_job') as host:
            scan_runner.check_scan_setup('/repo', flatpak=True, backend='auto',
                                         image_source=source)
        host.assert_not_called()

    def test_auto_creates_host_job_even_with_hotkey_fallback(self):
        source = {'kind': 'folder', 'path': '/shared/captures', 'trigger': 'hotkey'}
        report = {**VALID_REPORT, 'source': {
            'kind': 'live', 'backend': 'gamescope', 'socket': '/run/gamescope-1'}}
        events = []
        @contextmanager
        def job(**kwargs):
            events.append('enter')
            yield SimpleNamespace(path=Path('/tmp/job'), nonce='test', hard_deadline=1)
            events.append('exit')
        with patch.object(scan_runner, 'is_flatpak', return_value=True), \
             patch.object(scan_runner, 'check_scan_setup'), \
             patch.object(scan_runner, 'resolve_scanner_runtime', return_value=self.runtime()), \
             patch.object(scan_runner, 'create_host_job', side_effect=job), \
             patch.object(scan_runner, 'host_job_environment', return_value={}), \
             patch.object(scan_runner, '_run_owned', return_value=(0, json.dumps(report).encode(), b'')):
            result = scan_runner.run_scan('/repo', backend='auto', image_source=source)
        self.assertEqual(result, report)
        self.assertEqual(events, ['enter', 'exit'])

    def test_flatpak_command_runs_scanner_in_resolved_sandbox_runtime(self):
        runtime = self.runtime()
        with patch.object(scan_runner, 'resolve_scanner_runtime',
                          return_value=runtime):
            command = scan_runner.scan_command(
                '/tmp/a b', flatpak=True, backend='gamescope', workers=4)
        self.assertEqual(command[:3], ['/sandbox/python', '-m',
                                      'automatic_stratagems.scanner'])
        self.assertNotIn('flatpak-spawn', command)
        self.assertIn('--json', command)
        self.assertEqual(command[command.index('--budget-seconds') + 1], '110')
        self.assertNotIn('--debug-dir', command)
        self.assertEqual(command[-4:], ['--workers', '4', '--capture-backend', 'gamescope'])

    def test_image_source_command_keeps_path_literal_and_rescan_explicit(self):
        source = {'kind': 'folder', 'path': '/shared/a ; $(touch nope)',
                  'previous_fingerprint': 'a' * 64, 'allow_rescan': True}
        command = scan_runner.scan_command(
            '/repo', flatpak=True, _runtime=self.runtime(), image_source=source)
        self.assertEqual(command[command.index('--image-source-path') + 1],
                         source['path'])
        self.assertIn('--allow-image-rescan', command)
        self.assertEqual(command[command.index('--previous-image-fingerprint') + 1],
                         'a' * 64)
        self.assertNotIn('flatpak-spawn', command)

    def test_screenshot_trigger_arguments_are_literal_and_validated(self):
        source = {'kind': 'path', 'path': '', 'trigger': 'script',
                  'script': '/shared/capture ; literal', 'delete_after_scan': True}
        command = scan_runner.scan_command('/repo', _runtime=self.runtime(),
                                           image_source=source)
        self.assertEqual(command[command.index('--screenshot-script') + 1],
                         source['script'])
        self.assertIn('--delete-screenshot', command)
        self.assertEqual(command[command.index('--image-source-path') + 1], '')
        for field, value in (('trigger', 'bad'), ('delete_after_scan', 'yes'),
                             ('hotkey', ['KEY_F12']), ('script', 'relative')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                scan_runner.scan_command('/repo', backend='screenshot', _runtime=self.runtime(),
                                         image_source={**source, field: value})

    def test_image_source_path_requires_absolute_or_home_path(self):
        with self.assertRaisesRegex(ValueError, 'absolute'):
            scan_runner.scan_command('/repo', backend='screenshot', _runtime=self.runtime(),
                image_source={'kind': 'file', 'path': 'capture.png'})
        command = scan_runner.scan_command('/repo', _runtime=self.runtime(),
            image_source={'kind': 'file', 'path': '~/capture.png'})
        self.assertEqual(command[command.index('--image-source-path') + 1],
                         os.path.expanduser('~/capture.png'))

    def test_path_source_accepts_only_resolved_file_or_direct_folder_child(self):
        source = {'kind': 'path', 'path': '/shared/source'}
        command = scan_runner.scan_command('/repo', _runtime=self.runtime(),
                                           image_source=source)
        self.assertEqual(command[command.index('--image-source-kind') + 1], 'path')
        for selection, path, accepted in (
                ('file', '/shared/source', True),
                ('folder', '/shared/source/frame.png', True),
                ('file', '/shared/other.png', False),
                ('folder', '/shared/source/sub/frame.png', False),
                ('path', '/shared/source', False)):
            metadata = {'kind': 'file', 'selection': selection, 'path': path,
                        'fingerprint': 'a' * 64, 'mtime_ns': 1, 'size_bytes': 10}
            with self.subTest(selection=selection, path=path):
                if accepted:
                    self.assertEqual(scan_runner.validate_image_source_report(
                        {'source': metadata}, source), metadata)
                else:
                    with self.assertRaises(ValueError):
                        scan_runner.validate_image_source_report({'source': metadata}, source)

    def test_image_source_preflight_uses_no_host_job(self):
        runtime = self.runtime(env={'PATH': '/payload/bin'})
        source = {'kind': 'file', 'path': '/shared/capture.png'}
        with patch.object(scan_runner, 'resolve_scanner_runtime', return_value=runtime), \
             patch.object(scan_runner, '_run_owned', return_value=(0, b'', b'')) as run, \
             patch.object(scan_runner, 'create_host_job') as host:
            scan_runner.check_scan_setup('/repo', flatpak=True, backend='screenshot', image_source=source)
        host.assert_not_called()
        self.assertEqual(run.call_count, 2)
        self.assertIn('--check-setup', run.call_args.args[0])
        self.assertIn('--image-source-kind', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['env'], dict(runtime.env))

    def test_file_scan_validates_metadata_without_host_capture(self):
        source = {'kind': 'file', 'path': '/shared/capture.png'}
        metadata = {'kind': 'file', 'selection': 'file', 'path': source['path'],
                    'fingerprint': 'b' * 64, 'mtime_ns': 1, 'size_bytes': 10}
        report = dict(VALID_REPORT, source=metadata)
        with patch.object(scan_runner, 'is_flatpak', return_value=True), \
             patch.object(scan_runner, 'resolve_scanner_runtime', return_value=self.runtime()), \
             patch.object(scan_runner, 'check_scan_setup') as setup, \
             patch.object(scan_runner, '_run_owned',
                          return_value=(0, json.dumps(report).encode(), b'')), \
             patch.object(scan_runner, 'create_host_job') as host:
            self.assertEqual(scan_runner.run_scan('/repo', backend='screenshot', image_source=source), report)
            host.assert_not_called()
            self.assertEqual(setup.call_args.kwargs['image_source'], source)
            report['source']['fingerprint'] = 'invalid'
            with patch.object(scan_runner, '_run_owned',
                              return_value=(0, json.dumps(report).encode(), b'')):
                with self.assertRaisesRegex(ValueError, 'image source'):
                    scan_runner.run_scan('/repo', backend='screenshot', image_source=source)

    def test_image_source_report_is_bound_to_selected_path(self):
        for kind, configured, reported, accepted in (
                ('file', '/shared/capture.png', '/shared/capture.png', True),
                ('file', '/shared/capture.png', '/shared/other.png', False),
                ('folder', '/shared', '/shared/capture.png', True),
                ('folder', '/shared', '/shared/nested/capture.png', False),
                ('folder', '/shared', '/elsewhere/capture.png', False),
                ('folder', '/shared', '/shared/../elsewhere/capture.png', False)):
            metadata = {'kind': 'file', 'selection': kind, 'path': reported,
                        'fingerprint': 'b' * 64, 'mtime_ns': 1, 'size_bytes': 10}
            with self.subTest(kind=kind, path=reported):
                if accepted:
                    scan_runner.validate_image_source_report(
                        {'source': metadata}, {'kind': kind, 'path': configured})
                else:
                    with self.assertRaisesRegex(ValueError, 'image source'):
                        scan_runner.validate_image_source_report(
                            {'source': metadata}, {'kind': kind, 'path': configured})

    def test_image_source_cli_uses_loader_for_preflight_and_scan(self):
        source = {'kind': 'file', 'selection': 'folder', 'path': '/shared/new.png',
                  'fingerprint': 'c' * 64, 'mtime_ns': 12, 'size_bytes': 100}
        loader = unittest.mock.Mock(return_value=(Image.new('RGB', (100, 100)), source))
        setup = unittest.mock.Mock()
        module = SimpleNamespace(capture_screenshot=loader, check_screenshot_setup=setup,
                                 is_steam_managed=lambda path: False)
        args = ['--capture-backend', 'screenshot', '--image-source-kind', 'folder', '--image-source-path', '/shared',
                '--previous-image-fingerprint', 'a' * 64, '--allow-image-rescan']
        with patch.dict(sys.modules, {'automatic_stratagems.scanner.screenshot_capture': module}), \
             patch.object(scan_game, 'check_capture_setup') as capture_setup, \
             patch('automatic_stratagems.scanner.capture_backends.capture_gamescope') as capture, \
             patch.object(scan_game, 'detect', return_value={
                 'status': 'matched', 'rows': [], 'warnings': []}), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(scan_game.main([*args, '--check-setup']), 0)
            self.assertEqual(scan_game.main([*args, '--json', '--no-cache']), 0)
        capture_setup.assert_not_called()
        capture.assert_not_called()
        setup.assert_called_once()
        loader.assert_called_once()
        self.assertTrue(loader.call_args.args[0]['allow_rescan'])
        self.assertEqual(json.loads(output.getvalue())['source'], source)

    def test_image_source_cli_rejects_conflicting_or_incomplete_options(self):
        cases = (['--image-source-kind', 'file'], ['--image-source-path', '/shared'],
                 ['--allow-image-rescan'],
                 ['--image', '/a.png', '--image-source-kind', 'file',
                  '--image-source-path', '/b.png'])
        for args in cases:
            with self.subTest(args=args), patch.object(sys, 'stderr', io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    scan_game.main(args)
                self.assertEqual(error.exception.code, 2)

    def test_native_command_preserves_path_with_spaces_and_optional_debug(self):
        runtime = self.runtime('/tmp/a b/automatic_stratagems/.venv/bin/python',
                             profile='native')
        with patch.object(scan_runner, 'resolve_scanner_runtime',
                          return_value=runtime):
            command = scan_runner.scan_command('/tmp/a b', flatpak=False,
                                               debug_dir=Path('/tmp/private run'))
        self.assertEqual(command[0],
                         '/tmp/a b/automatic_stratagems/.venv/bin/python')
        self.assertEqual(command[-2:], ['--debug-dir', '/tmp/private run'])

    def test_worker_settings_are_bounded_and_invalid_values_use_default(self):
        for value, expected in ((None, 2), ('bad', 2), (True, 2), (0, 1),
                                (99, 32), ('4', 4)):
            with self.subTest(value=value):
                self.assertEqual(scan_runner.scan_workers(value), expected)

    def test_scanner_cli_passes_one_absolute_work_deadline_to_capture(self):
        report = {'mode': 'mission', 'status': 'no_detections', 'rows': [],
                  'layout': {}, 'warnings': []}
        with patch.object(scan_game.time, 'monotonic', return_value=10.0), \
             patch.object(scan_game, 'scan_live', return_value=(
                 Image.new('RGB', (100, 100)),
                 {'kind': 'live', 'backend': 'desktop'}, report)) as capture, \
             redirect_stdout(io.StringIO()):
            code = scan_game.main(['--capture-backend', 'gamescope', '--json', '--no-cache',
                                   '--budget-seconds', '5'])
        self.assertEqual(code, 4)
        self.assertEqual(capture.call_args.kwargs['deadline'], 15.0)

    def test_recognition_does_not_start_after_work_deadline(self):
        image = Image.new('RGB', (100, 100))
        with patch.object(scan_game.time, 'monotonic', return_value=2.0), \
             patch.object(scan_game, 'detect_mission_icons') as icons:
            with self.assertRaisesRegex(scan_game.ScanError, 'deadline'):
                scan_game.detect(image, 'mission', deadline=1.0)
        icons.assert_not_called()

    def run_script(self, source, *, cancel_event=None, timeout=2,
                   stdout_limit=None, stderr_limit=None):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            python = root / 'automatic_stratagems/.venv/bin/python'
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            command = [str(python), '-c', source]
            with patch.object(scan_runner, 'scan_command', return_value=command), \
                 patch.object(scan_runner, 'is_flatpak', return_value=False), \
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
        with self.assertRaisesRegex(ValueError, 'invalid (JSON|report)'):
            self.run_script('print("[" * 10000 + "]" * 10000)')

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

    def test_owned_child_receives_only_the_resolved_environment(self):
        command = [sys.executable, '-c',
                   'import os; print(os.environ.get("HD2_ONLY", "missing")); '
                   'print(os.environ.get("HD2_PARENT", "missing"))']
        with patch.dict(os.environ, {'HD2_PARENT': 'parent'}, clear=False):
            code, stdout, _ = scan_runner._run_owned(
                command, cwd=Path('/tmp'), timeout=2,
                env={'HD2_ONLY': 'child'})
        self.assertEqual(code, 0)
        self.assertEqual(stdout.splitlines(), [b'child', b'missing'])

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

    def test_preflight_passes_exact_backend_to_capture_child(self):
        runtime = self.runtime(profile='native')
        for backend in ('gamescope', 'screenshot'):
            with self.subTest(backend=backend), \
                 patch.object(scan_runner, 'resolve_scanner_runtime',
                              return_value=runtime), \
                 patch.object(scan_runner, '_run_owned',
                              return_value=(0, b'', b'')) as run:
                scan_runner.check_scan_setup('/tmp/repo', flatpak=False,
                                             backend=backend)
            self.assertEqual(run.call_args_list[1].args[0][-2:],
                             ['--capture-backend', backend])

    def test_flatpak_preflight_runs_runtime_and_capture_checks_in_sandbox(self):
        runtime = self.runtime(env={'PATH': '/payload/bin', 'PYTHONPATH': '/payload/python'})
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return 0, b'{}', b''

        with tempfile.TemporaryDirectory() as base, \
             patch.object(scan_runner, 'resolve_scanner_runtime',
                          return_value=runtime), \
             patch.object(scan_runner, '_run_owned', side_effect=run), \
             patch.dict(os.environ, {'XDG_CACHE_HOME': base}, clear=False):
            scan_runner.check_scan_setup('/tmp/repo', flatpak=True,
                                         backend='gamescope')
        self.assertEqual(len(calls), 2)
        self.assertIn('--preflight', calls[0][0])
        self.assertEqual(calls[0][1]['env'], dict(runtime.env))
        self.assertIn('--check-setup', calls[1][0])
        self.assertIn('HD2_HOST_JOB_DIRECTORY', calls[1][1]['env'])
        self.assertEqual(calls[1][1]['env']['PYTHONPATH'], '/payload/python')

    def test_flatpak_jobs_use_the_shared_cache_directory(self):
        manager = object()
        with patch.object(scan_runner, 'create_host_job',
                          return_value=manager) as create:
            self.assertIs(scan_runner._job_context(
                True, 20), manager)
        create.assert_called_once_with(hard_timeout=20)

    def test_preflight_reports_capture_helper_failure(self):
        runtime = self.runtime(profile='native')
        with patch.object(scan_runner, 'resolve_scanner_runtime',
                          return_value=runtime), \
             patch.object(scan_runner, '_run_owned', side_effect=[
                 (0, b'{}', b''), (1, b'', b'Missing capture helper: grim')]):
            with self.assertRaisesRegex(scan_runner.ScanSetupError, 'grim'):
                scan_runner.check_scan_setup('/tmp/repo', flatpak=False,
                                             backend='gamescope')

    def test_host_capture_preflight_checks_transport_and_capture_helpers(self):
        with patch.dict(os.environ, {
                'FLATPAK_ID': 'com.core447.StreamController',
                'HD2_HOST_JOB_DIRECTORY': '/shared/job',
                'XDG_SESSION_TYPE': 'wayland',
                'XDG_CURRENT_DESKTOP': 'Hyprland',
                'WAYLAND_DISPLAY': 'wayland-0'}, clear=True), \
             patch('automatic_stratagems.scanner.game_capture.run_command',
                   return_value=b'') as command:
            scan_game.check_capture_setup('gamescope')
        argv = command.call_args.args[0]
        self.assertEqual(argv[0:2], ['/usr/bin/sh', '-c'])
        self.assertIn('/usr/bin/grep', argv[2])
        self.assertIn('/usr/bin/mv', argv[2])
        self.assertIn('/usr/bin/rm', argv[2])
        self.assertIn('/usr/bin/sleep', argv[2])
        self.assertIn('/usr/bin/sync', argv[2])
        self.assertIn('/shared/job', argv)
        self.assertEqual(argv[-3:], ['/usr/bin/python3', 'gamescopectl', '/usr/bin/flatpak'])
        self.assertNotIn('hyprctl', argv)
        self.assertNotIn('xprop', argv)
        self.assertTrue(command.call_args.kwargs['host'])

    def test_flatpak_gamescope_requires_host_python_for_metadata(self):
        with patch.dict(os.environ, {
                'FLATPAK_ID': 'com.core447.StreamController',
                'HD2_HOST_JOB_DIRECTORY': '/shared/job',
                'XDG_SESSION_TYPE': 'wayland',
                'XDG_CURRENT_DESKTOP': 'Hyprland',
                'WAYLAND_DISPLAY': 'wayland-0'}, clear=True), \
             patch('automatic_stratagems.scanner.game_capture.run_command',
                   return_value=b'') as command:
            scan_game.check_capture_setup('gamescope')
        self.assertIn('/usr/bin/python3', command.call_args.args[0][-3])

    def test_native_preflight_does_not_require_flatpak_transport_leaves(self):
        available = {'gamescopectl', '/usr/bin/python3', '/usr/bin/setsid'}
        with patch.dict(os.environ, {
                'XDG_SESSION_TYPE': 'wayland',
                'XDG_CURRENT_DESKTOP': 'Hyprland',
                'WAYLAND_DISPLAY': 'wayland-0'}, clear=True), \
             patch.object(scan_game.Path, 'exists', return_value=False), \
             patch.object(scan_game.shutil, 'which',
                          side_effect=lambda value: ('/host/' + value
                                                     if value in available else None)), \
             patch('automatic_stratagems.scanner.game_capture.run_command') as command:
            scan_game.check_capture_setup('gamescope')
        command.assert_not_called()

    def test_native_preflight_accepts_flatpak_without_native_gamescopectl(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(scan_game.Path, 'exists', return_value=False), \
             patch.object(scan_game.shutil, 'which', side_effect=lambda name:
                          name if name in {'/usr/bin/flatpak', '/usr/bin/python3',
                                           '/usr/bin/setsid'} else None):
            scan_game.check_capture_setup('gamescope')

    def test_native_preflight_requires_identity_and_process_guard_tools(self):
        for missing in ('/usr/bin/python3', '/usr/bin/setsid'):
            with self.subTest(missing=missing), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch.object(scan_game.Path, 'exists', return_value=False), \
                 patch.object(scan_game.shutil, 'which',
                              side_effect=lambda name: None if name == missing else name):
                with self.assertRaisesRegex(scan_game.ScanError, missing):
                    scan_game.check_capture_setup('gamescope')

    def test_native_gamescope_and_auto_scans_own_host_jobs(self):
        from automatic_stratagems import host_commands as hc
        for backend in ('auto', 'gamescope'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as base:
                runtime = self.runtime(profile='native')
                seen = []

                def run(_command, **kwargs):
                    job = Path(kwargs['env'][hc.JOB_DIRECTORY_ENV])
                    self.assertTrue((job / hc.REGISTRY_FILE).is_file())
                    seen.append(job)
                    return 0, json.dumps(VALID_REPORT).encode(), b''

                with patch.object(scan_runner, 'is_flatpak', return_value=False), \
                     patch.object(scan_runner, 'check_scan_setup'), \
                     patch.object(scan_runner, 'resolve_scanner_runtime', return_value=runtime), \
                     patch.object(scan_runner, 'create_host_job', side_effect=lambda **kw:
                                  hc.create_host_job(base=Path(base), **kw)), \
                     patch.object(scan_runner, '_run_owned', side_effect=run):
                    scan_runner.run_scan('/tmp/repo', backend=backend)
                self.assertEqual(len(seen), 1)
                self.assertFalse(seen[0].exists())

    def test_flatpak_run_holds_host_job_until_scanner_is_reaped(self):
        runtime = self.runtime(env={'PATH': '/payload/bin'})
        events = []

        @contextmanager
        def job(**_kwargs):
            class Job:
                pass
            value = Job()
            value.path = Path('/tmp/owned-job')
            value.nonce = 'nonce'
            value.hard_deadline = 1
            events.append('job-enter')
            try:
                yield value
            finally:
                events.append('job-exit')

        def run(_command, **kwargs):
            events.append('scanner-reaped')
            self.assertEqual(kwargs['env']['PATH'], '/payload/bin')
            self.assertEqual(kwargs['env']['HD2_HOST_JOB_DIRECTORY'],
                             '/tmp/owned-job')
            return 0, json.dumps(VALID_REPORT).encode(), b''

        with patch.object(scan_runner, 'is_flatpak', return_value=True), \
             patch.object(scan_runner, 'check_scan_setup'), \
             patch.object(scan_runner, 'resolve_scanner_runtime',
                          return_value=runtime), \
             patch.object(scan_runner, 'create_host_job', side_effect=job), \
             patch.object(scan_runner, 'host_job_environment', return_value={
                 'HD2_HOST_JOB_DIRECTORY': '/tmp/owned-job'}), \
             patch.object(scan_runner, '_run_owned', side_effect=run):
            result = scan_runner.run_scan('/tmp/repo', backend='gamescope')
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(events, ['job-enter', 'scanner-reaped', 'job-exit'])

    def test_cancel_waits_for_delayed_host_cleanup_before_returning(self):
        runtime = self.runtime(env={'PATH': '/payload/bin'})
        cleanup_started = threading.Event()
        cleanup_release = threading.Event()
        caught = []

        @contextmanager
        def job(**_kwargs):
            class Job:
                path = Path('/tmp/owned-job')
                nonce = 'nonce'
                hard_deadline = 1
            try:
                yield Job()
            finally:
                cleanup_started.set()
                cleanup_release.wait(2)

        def scan():
            try:
                scan_runner.run_scan('/tmp/repo', backend='gamescope')
            except BaseException as error:
                caught.append(error)

        with patch.object(scan_runner, 'is_flatpak', return_value=True), \
             patch.object(scan_runner, 'check_scan_setup'), \
             patch.object(scan_runner, 'resolve_scanner_runtime',
                          return_value=runtime), \
             patch.object(scan_runner, 'create_host_job', side_effect=job), \
             patch.object(scan_runner, 'host_job_environment', return_value={}), \
             patch.object(scan_runner, '_run_owned',
                          side_effect=scan_runner.CancelledError()):
            worker = threading.Thread(target=scan, daemon=True)
            worker.start()
            self.assertTrue(cleanup_started.wait(1))
            self.assertTrue(worker.is_alive())
            cleanup_release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(caught), 1)
        self.assertIsInstance(caught[0], scan_runner.CancelledError)

    def test_diagnostics_use_private_owned_run_directory(self):
        with tempfile.TemporaryDirectory() as parent:
            cache = Path(parent) / 'diagnostics'
            run = scan_runner.prepare_diagnostic_run(cache)
            self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
            self.assertEqual(run.stat().st_mode & 0o777, 0o700)
            marker = json.loads((run / scan_diagnostics.RUN_MARKER).read_text())
            self.assertEqual(marker['owner'], scan_diagnostics.DIAGNOSTIC_OWNER)
            self.assertEqual(marker['status'], 'open')
            scan_runner.close_diagnostic_run(run, 'complete')
            self.assertEqual(json.loads((run / scan_diagnostics.RUN_MARKER).read_text())['status'],
                             'complete')

    def test_invalid_scanner_results_mark_diagnostics_failed(self):
        invalid_schema = dict(VALID_REPORT, schema_version=99)
        error_report = {
            'schema_version': 1, 'status': 'error', 'error': 'capture failed',
            'rows': [], 'warnings': [],
        }
        cases = (
            ('invalid JSON', 0, b'not-json'),
            ('schema version', 0, json.dumps(invalid_schema).encode()),
            ('capture failed', 1, json.dumps(error_report).encode()),
            ('Scanner failed', 2, json.dumps(VALID_REPORT).encode()),
        )
        for expected, returncode, stdout in cases:
            with self.subTest(expected=expected), \
                 tempfile.TemporaryDirectory() as parent, \
                 patch.object(scan_runner, 'is_flatpak', return_value=False), \
                 patch.object(scan_runner, 'check_scan_setup'), \
                 patch.object(scan_runner, 'resolve_scanner_runtime',
                              return_value=self.runtime(profile='native')), \
                 patch.object(scan_runner, '_run_owned',
                              return_value=(returncode, stdout, b'')):
                cache = Path(parent) / 'diagnostics'
                with self.assertRaisesRegex(ValueError, expected):
                    scan_runner.run_scan('/tmp/root', debug_dir=cache)
                runs = [path for path in cache.iterdir()
                        if path.name.startswith('run-')]
                self.assertEqual(len(runs), 1)
                marker = json.loads((runs[0] / scan_diagnostics.RUN_MARKER).read_text())
                self.assertEqual(marker['status'], 'failed')

    def test_diagnostic_marker_retries_short_writes(self):
        with tempfile.TemporaryDirectory() as parent:
            path = Path(parent) / 'marker.json'
            value = {'owner': scan_diagnostics.DIAGNOSTIC_OWNER, 'version': 1}
            real_write = scan_diagnostics.os.write

            def short_write(descriptor, data):
                return real_write(descriptor, data[:5])

            with patch.object(scan_diagnostics.os, 'write', side_effect=short_write):
                scan_diagnostics._write_marker(path, value, create=True)
            self.assertEqual(json.loads(path.read_text()), value)

    def test_diagnostic_marker_write_failure_preserves_old_and_cleans_partial(self):
        with tempfile.TemporaryDirectory() as parent:
            parent = Path(parent)
            path = parent / 'marker.json'
            original = {'owner': scan_diagnostics.DIAGNOSTIC_OWNER, 'status': 'open'}
            scan_diagnostics._write_marker(path, original, create=True)
            real_write = scan_diagnostics.os.write

            def failed_write(descriptor, data):
                real_write(descriptor, data[:5])
                raise OSError('disk full')

            with patch.object(scan_diagnostics.os, 'write', side_effect=failed_write):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    scan_diagnostics._write_marker(path, dict(original, status='complete'))
            self.assertEqual(json.loads(path.read_text()), original)
            self.assertEqual(list(parent.iterdir()), [path])

            create = parent / 'new-marker.json'
            with patch.object(scan_diagnostics.os, 'write', side_effect=failed_write):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    scan_diagnostics._write_marker(create, original, create=True)
            self.assertFalse(create.exists())

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
            os.mkfifo(cache / scan_diagnostics.CACHE_MARKER)
            with patch.object(Path, 'read_text', side_effect=AssertionError('read FIFO')):
                with self.assertRaisesRegex(ValueError, 'not owned'):
                    scan_runner.prepare_diagnostic_run(cache)

    def test_diagnostic_pruning_preserves_open_and_unowned_directories(self):
        with tempfile.TemporaryDirectory() as parent, \
             patch.object(scan_diagnostics, 'MAX_DIAGNOSTIC_RUNS', 1):
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
             patch.object(scan_diagnostics, 'MAX_DIAGNOSTIC_RUNS', 1):
            cache = Path(parent) / 'diagnostics'
            abandoned = scan_runner.prepare_diagnostic_run(cache)
            marker_path = abandoned / scan_diagnostics.RUN_MARKER
            marker = json.loads(marker_path.read_text())
            marker['runner']['pid'] = 2_000_000_000
            scan_diagnostics._write_marker(marker_path, marker)
            live = scan_runner.prepare_diagnostic_run(cache)
            scan_runner.prune_diagnostics(cache)
            self.assertFalse(abandoned.exists())
            self.assertTrue(live.exists())

    def test_diagnostic_pruning_preserves_malformed_open_identity(self):
        with tempfile.TemporaryDirectory() as parent, \
             patch.object(scan_diagnostics, 'MAX_DIAGNOSTIC_RUNS', 0):
            cache = Path(parent) / 'diagnostics'
            run = scan_runner.prepare_diagnostic_run(cache)
            marker_path = run / scan_diagnostics.RUN_MARKER
            marker = json.loads(marker_path.read_text())
            marker['runner'] = {'pid': os.getpid(), 'boot_id': 'malformed',
                                'start_time': 'not-a-clock-tick'}
            scan_diagnostics._write_marker(marker_path, marker)
            scan_runner.prune_diagnostics(cache)
            self.assertTrue(run.exists())
            self.assertEqual(json.loads(marker_path.read_text())['status'], 'open')

    def test_diagnostic_pruning_preserves_open_run_when_proc_is_unreadable(self):
        with tempfile.TemporaryDirectory() as parent, \
             patch.object(scan_diagnostics, 'MAX_DIAGNOSTIC_RUNS', 0):
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
            self.assertEqual(json.loads((run / scan_diagnostics.RUN_MARKER).read_text())['status'],
                             'open')

    def test_diagnostic_failure_does_not_mask_cancellation(self):
        with patch.object(scan_runner, 'is_flatpak', return_value=False), \
             patch.object(scan_runner, 'check_scan_setup'), \
             patch.object(scan_runner, 'resolve_scanner_runtime',
                          return_value=self.runtime(profile='native')), \
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

    def test_run_scan_manages_diagnostics_through_runner_names(self):
        for returncode, stdout, outcome in (
                (0, json.dumps(VALID_REPORT).encode(), 'complete'),
                (0, b'not-json', 'failed')):
            with self.subTest(outcome=outcome), \
                 tempfile.TemporaryDirectory() as parent, \
                 patch.object(scan_runner, 'is_flatpak', return_value=False), \
                 patch.object(scan_runner, 'check_scan_setup'), \
                 patch.object(scan_runner, 'resolve_scanner_runtime',
                              return_value=self.runtime(profile='native')), \
                 patch.object(scan_runner, '_run_owned',
                              return_value=(returncode, stdout, b'')), \
                 patch.object(scan_runner, 'prepare_diagnostic_run',
                              return_value=Path('/tmp/owned-run')) as prepare, \
                 patch.object(scan_runner, 'close_diagnostic_run') as close, \
                 patch.object(scan_runner, 'prune_diagnostics') as prune:
                cache = Path(parent) / 'diagnostics'
                try:
                    scan_runner.run_scan('/tmp/root', debug_dir=cache)
                except ValueError:
                    self.assertEqual(outcome, 'failed')
                prepare.assert_called_once_with(cache)
                close.assert_called_once_with(Path('/tmp/owned-run'), outcome)
                prune.assert_called_once_with(cache)


if __name__ == '__main__':
    unittest.main()
