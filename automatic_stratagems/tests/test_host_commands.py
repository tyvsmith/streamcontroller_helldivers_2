import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from automatic_stratagems import host_commands as hc
from automatic_stratagems.tests.process_support import process_is_live


class HostCommandTests(unittest.TestCase):
    def setUp(self):
        # Guards spawned by these unit tests share this process namespace.
        flatpak = patch.object(hc, '_flatpak', return_value=False)
        flatpak.start()
        self.addCleanup(flatpak.stop)

    def test_parent_job_is_private_and_exported_to_child(self):
        with tempfile.TemporaryDirectory() as base:
            with hc.create_host_job(base=Path(base), hard_timeout=3) as job:
                self.assertTrue(stat.S_ISDIR(job.path.lstat().st_mode))
                self.assertEqual(stat.S_IMODE(job.path.stat().st_mode), 0o700)
                child = hc.host_job_environment(job)
                self.assertEqual(child[hc.JOB_DIRECTORY_ENV], str(job.path))
                self.assertEqual(child[hc.JOB_NONCE_ENV], job.nonce)
            self.assertFalse(job.path.exists())

    def test_failed_artifact_cleanup_preserves_record_after_job_exits(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaises(hc.HostArtifactCleanupError):
                with hc.create_host_job(base=Path(base), hard_timeout=3) as job, \
                     patch.dict(os.environ, hc.host_job_environment(job)), \
                     hc.shared_host_directory(retain_on_error=True) as directory:
                    record = directory / 'recovery.json'
                    record.write_text('owned capture identity')
                    raise hc.HostArtifactCleanupError('cleanup denied')
            self.assertEqual(record.read_text(), 'owned capture identity')
            self.assertTrue((job.path / hc.REGISTRY_FILE).exists())

    def test_killed_scanner_preserves_recovery_record_after_job_exits(self):
        script = """
import os, signal
from automatic_stratagems.host_commands import shared_host_directory
with shared_host_directory(retain_on_error=True) as directory:
    (directory / 'recovery.json').write_text('owned capture identity')
    print(directory, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
"""
        with tempfile.TemporaryDirectory() as base:
            with hc.create_host_job(base=Path(base), hard_timeout=3) as job:
                result = subprocess.run(
                    [sys.executable, '-c', script],
                    env={**os.environ, **hc.host_job_environment(job)},
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, -signal.SIGKILL)
                record = Path(result.stdout.strip()) / 'recovery.json'
            self.assertEqual(record.read_text(), 'owned capture identity')
            self.assertTrue((job.path / hc.RETAIN_FILE).exists())

    def test_normal_cancel_removes_shared_directory_and_job(self):
        from concurrent.futures import CancelledError
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaises(CancelledError):
                with hc.create_host_job(base=Path(base), hard_timeout=3) as job, \
                     patch.dict(os.environ, hc.host_job_environment(job)), \
                     hc.shared_host_directory(retain_on_error=True) as directory:
                    self.assertEqual(directory.parent, job.path)
                    raise CancelledError()
            self.assertFalse(job.path.exists())

    def test_guard_preserves_argv_stdout_and_exit_status(self):
        with tempfile.TemporaryDirectory() as base:
            with hc.create_host_job(base=Path(base), hard_timeout=3) as job, \
                 patch.dict(os.environ, hc.host_job_environment(job), clear=False):
                command, operation = hc.guard_command(
                    [sys.executable, '-c',
                     'import sys;print(sys.argv[1]);sys.exit(7)',
                     'space ; $()'], operation='test-status')
                result = subprocess.run(command, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True,
                                        timeout=5, start_new_session=True)
                completion = hc.wait_operation(operation)
            self.assertEqual(result.stdout, 'space ; $()\n')
            self.assertEqual(result.returncode, -signal.SIGKILL)
            self.assertEqual(completion.command_status, 7)
            self.assertTrue(completion.cleanup_confirmed)

    def test_guard_preserves_empty_non_executable_arguments(self):
        with tempfile.TemporaryDirectory() as base:
            with hc.create_host_job(base=Path(base), hard_timeout=3) as job, \
                 patch.dict(os.environ, hc.host_job_environment(job), clear=False):
                command, operation = hc.guard_command(
                    [sys.executable, '-c', 'import json,sys;print(json.dumps(sys.argv[1:]))',
                     '', 'tail'], operation='test-empty-argument')
                result = subprocess.run(command, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True,
                                        timeout=5, start_new_session=True)
                completion = hc.wait_operation(operation)
            self.assertEqual(json.loads(result.stdout), ['', 'tail'])
            self.assertEqual(completion.command_status, 0)

    def test_guard_uses_fixed_lifecycle_helpers(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            empty_path = root / 'empty-path'
            empty_path.mkdir()
            with hc.create_host_job(base=root, hard_timeout=3) as job, \
                 patch.dict(os.environ, hc.host_job_environment(job), clear=False):
                command, operation = hc.guard_command(
                    ['/bin/true'], operation='test-fixed-helpers')
                result = subprocess.run(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env={**os.environ, 'PATH': str(empty_path)}, timeout=5,
                    start_new_session=True)
                completion = hc.wait_operation(operation)
            self.assertEqual(result.returncode, -signal.SIGKILL)
            self.assertEqual(completion.command_status, 0)
            self.assertEqual(result.stderr, b'')

    def test_reservation_rejects_empty_executable_and_nul_arguments(self):
        with tempfile.TemporaryDirectory() as base:
            with hc.create_host_job(base=Path(base), hard_timeout=.1) as job:
                for command in ([''], ['true', 'bad\0value']):
                    with self.subTest(command=command), \
                         self.assertRaisesRegex(hc.HostCommandError, 'argv'):
                        hc.reserve_operation(job, command, 'test-invalid-argv')

    def test_guard_kills_descendant_after_command_leader_exits(self):
        with tempfile.TemporaryDirectory() as base:
            pid_file = Path(base) / 'descendant.pid'
            descendant = (
                'import os,pathlib,signal,time;'
                'signal.signal(signal.SIGINT,signal.SIG_IGN);'
                'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));'
                'time.sleep(30)')
            leader = (f'import subprocess,sys,time;subprocess.Popen('
                      f'[sys.executable,"-c",{descendant!r}],'
                      'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,'
                      'stderr=subprocess.DEVNULL);time.sleep(.1)')
            with hc.create_host_job(base=Path(base), hard_timeout=3) as job, \
                 patch.dict(os.environ, hc.host_job_environment(job), clear=False):
                command, operation = hc.guard_command(
                    [sys.executable, '-c', leader], operation='test-descendant')
                result = subprocess.run(command, timeout=5, start_new_session=True)
                completion = hc.wait_operation(operation)
                pid = int(pid_file.read_text())
                self.assertFalse(process_is_live(pid))
            self.assertEqual(result.returncode, -signal.SIGKILL)
            self.assertTrue(completion.cleanup_confirmed)

    def test_guard_term_cleans_ignoring_child_group(self):
        with tempfile.TemporaryDirectory() as base:
            pid_file = Path(base) / 'leader.pid'
            source = (
                'import os,pathlib,signal,time;'
                'signal.signal(signal.SIGINT,signal.SIG_IGN);'
                'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));'
                'time.sleep(30)')
            with hc.create_host_job(base=Path(base), hard_timeout=4) as job, \
                 patch.dict(os.environ, hc.host_job_environment(job), clear=False):
                command, operation = hc.guard_command(
                    [sys.executable, '-c', source], operation='test-term')
                process = subprocess.Popen(command, start_new_session=True)
                deadline = time.monotonic() + 2
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                process.terminate()
                process.wait(timeout=4)
                completion = hc.wait_operation(operation)
                self.assertFalse(process_is_live(int(pid_file.read_text())))
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertTrue(completion.cleanup_confirmed)

    def test_guard_cleanup_survives_repeated_cancel_signals(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            pid_file = root / 'leader.pid'
            linger_file = root / 'linger.pid'
            entered = root / 'result-move-entered'
            release = root / 'result-move-release'
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            mv = bin_dir / 'mv'
            mv.write_text(
                '#!/bin/sh\n'
                "trap '' HUP INT TERM\n"
                'target=\n'
                'for target do :; done\n'
                f'case "$target" in */result.json) : > {str(entered)!r}; '
                f'while [ ! -e {str(release)!r} ]; do sleep .001; done;; esac\n'
                'exec /usr/bin/mv "$@"\n')
            mv.chmod(0o700)
            linger = (
                'import os,pathlib,signal,time;'
                'signal.signal(signal.SIGINT,signal.SIG_IGN);'
                'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                f'pathlib.Path({str(linger_file)!r}).write_text(str(os.getpid()));'
                'time.sleep(30)')
            source = (
                'import os,pathlib,subprocess,sys,time;'
                f'subprocess.Popen([sys.executable,"-c",{linger!r}],'
                'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,'
                'stderr=subprocess.DEVNULL);'
                f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));'
                'time.sleep(30)')
            manager = hc.create_host_job(base=root, hard_timeout=4)
            job = manager.__enter__()
            environment = hc.host_job_environment(job)
            operation = None
            process = None
            guard = hc.HOST_GUARD.replace('/usr/bin/mv', str(mv))
            with patch.dict(os.environ, environment, clear=False), \
                 patch.object(hc, 'HOST_GUARD', guard):
                command, operation = hc.guard_command(
                    [sys.executable, '-c', source], operation='test-repeat-term')
            try:
                process = subprocess.Popen(
                    command, start_new_session=True, env={**os.environ, **environment})
                deadline = time.monotonic() + 2
                while (not pid_file.exists() or not linger_file.exists()) and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertTrue(pid_file.exists())
                self.assertTrue(linger_file.exists())
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
                deadline = time.monotonic() + 2
                while not entered.exists() and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertTrue(entered.exists())
                os.killpg(process.pid, signal.SIGHUP)
                release.touch()
                process.wait(timeout=4)
                completion = hc.wait_operation(operation, timeout=.5)
                self.assertFalse(process_is_live(int(linger_file.read_text())))
                self.assertTrue((operation.path / hc.RESULT_FILE).exists())
                self.assertEqual(list(operation.path.glob('.result.json.*')), [])
                self.assertEqual(process.returncode, -signal.SIGKILL)
                self.assertEqual(completion.command_status, 143)
            finally:
                release.touch(exist_ok=True)
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process is not None and process.poll() is None:
                    process.wait(timeout=2)
                if operation is not None and not (
                        operation.path / hc.COMPLETE_FILE).exists():
                    hc.write_state(operation, hc.COMPLETE_FILE, {
                        'command_status': 125, 'cleanup_confirmed': True})
                manager.__exit__(None, None, None)

    def test_invalid_or_incomplete_completion_is_never_confirmed(self):
        with tempfile.TemporaryDirectory() as base:
            manager = hc.create_host_job(base=Path(base), hard_timeout=.1)
            job = manager.__enter__()
            try:
                operation = hc.reserve_operation(job, ['true'], 'test-invalid')
                (operation.path / hc.COMPLETE_FILE).write_text('{}')
                with self.assertRaises(hc.HostCleanupUnconfirmed):
                    hc.wait_operation(operation, timeout=.1)
            finally:
                hc.write_state(operation, hc.COMPLETE_FILE, {
                    'command_status': 125, 'cleanup_confirmed': True})
                manager.__exit__(None, None, None)

    def test_ready_without_active_or_complete_is_unknown(self):
        with tempfile.TemporaryDirectory() as base:
            manager = hc.create_host_job(base=Path(base), hard_timeout=.1)
            job = manager.__enter__()
            try:
                operation = hc.reserve_operation(job, ['true'], 'test-ready')
                hc.write_state(operation, hc.READY_FILE, {
                    'guard_pid': os.getpid(), 'pgid': os.getpid(), 'sid': os.getpid(),
                    'guard_start_time': hc.process_start_time(os.getpid())})
                with self.assertRaises(hc.HostCleanupUnconfirmed):
                    hc.wait_operation(operation, timeout=.1)
            finally:
                hc.write_state(operation, hc.COMPLETE_FILE, {
                    'command_status': 125, 'cleanup_confirmed': True})
                manager.__exit__(None, None, None)

    def test_result_marker_alone_does_not_confirm_live_group_cleanup(self):
        with tempfile.TemporaryDirectory() as base:
            manager = hc.create_host_job(base=Path(base), hard_timeout=.1)
            job = manager.__enter__()
            operation = hc.reserve_operation(job, ['true'], 'test-live-result')
            hc.write_state(operation, hc.READY_FILE, {
                'guard_pid': os.getpid(), 'pgid': os.getpid(), 'sid': os.getpid(),
                'guard_start_time': hc.process_start_time(os.getpid())})
            hc.write_state(operation, hc.RESULT_FILE, {'command_status': 0})
            with patch.object(hc, '_host_group_empty', return_value=False), \
                 self.assertRaises(hc.HostCleanupUnconfirmed):
                hc.wait_operation(operation, timeout=.05)
            hc.write_state(operation, hc.COMPLETE_FILE, {
                'command_status': 125, 'cleanup_confirmed': True})
            manager.__exit__(None, None, None)

    def test_local_spawn_failure_is_terminal_without_host_work(self):
        with tempfile.TemporaryDirectory() as base:
            with hc.create_host_job(base=Path(base), hard_timeout=.1) as job:
                operation = hc.reserve_operation(job, ['missing'], 'test-missing')
                self.assertTrue(hc.mark_not_started(operation))
                completion = hc.wait_operation(operation, timeout=.1)
            self.assertEqual(completion.command_status, 125)
            self.assertTrue(completion.cleanup_confirmed)

    def test_launcher_publishes_submitted_before_exec(self):
        with tempfile.TemporaryDirectory() as base:
            with hc.create_host_job(base=Path(base), hard_timeout=.1) as job:
                operation = hc.reserve_operation(job, ['missing'], 'test-launcher')
                command = hc.launcher_command(['/definitely/missing'], operation)
                result = subprocess.run(command, stderr=subprocess.DEVNULL)
                self.assertEqual(result.returncode, 127)
                submitted = hc._validate_state(operation, hc.SUBMITTED_FILE)
                self.assertIs(type(submitted['launcher_pid']), int)
                hc.mark_not_started(operation)

    def test_launcher_uses_fixed_lifecycle_helpers(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            empty_path = root / 'empty-path'
            empty_path.mkdir()
            manager = hc.create_host_job(base=root, hard_timeout=.1)
            job = manager.__enter__()
            operation = hc.reserve_operation(job, ['/bin/true'], 'test-fixed-launcher')
            try:
                command = hc.launcher_command(['/bin/true'], operation)
                result = subprocess.run(
                    command, stderr=subprocess.PIPE,
                    env={**os.environ, 'PATH': str(empty_path)})
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, b'')
                self.assertTrue((operation.path / hc.SUBMITTED_FILE).exists())
            finally:
                hc.write_state(operation, hc.COMPLETE_FILE, {
                    'command_status': 125, 'cleanup_confirmed': True})
                manager.__exit__(None, None, None)

    def test_operation_query_removes_temporary_with_restricted_path(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            empty_path = root / 'empty-path'
            empty_path.mkdir()
            operation = root / 'operation'
            operation.mkdir()
            result = subprocess.run(
                ['/usr/bin/sh', '-c', hc.HOST_OPERATION_QUERY,
                 'test-operation-query', str(operation)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, 'PATH': str(empty_path),
                     'HD2_OPERATION_QUERY': 'hd2-no-such-query-token'})
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, b'')
            self.assertEqual(list(operation.iterdir()), [])

    def _query_fake_proc(self, root, token, *, vanish=None):
        """Run the operation query over a fake /proc tree under root.

        Entry 100 has a FIFO for cmdline, so grep blocks there after the glob has
        expanded; vanish is then removed, as if that process exited mid-query.
        """
        proc = root / 'proc'
        (proc / '100').mkdir(parents=True)
        os.mkfifo(proc / '100' / 'cmdline')
        operation = root / 'operation'
        operation.mkdir()
        script = hc.HOST_OPERATION_QUERY.replace('/proc/', f'{proc}/')
        self.assertNotEqual(script, hc.HOST_OPERATION_QUERY)
        process = subprocess.Popen(
            ['/usr/bin/sh', '-c', script, 'test-operation-query', str(operation)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, 'HD2_OPERATION_QUERY': token})
        self.addCleanup(process.kill)
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            try:
                writer = os.open(proc / '100' / 'cmdline', os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                time.sleep(.005)
                continue
            if vanish is not None:
                for path in sorted(vanish.iterdir()):
                    path.unlink()
                vanish.rmdir()
                vanish = None
            os.close(writer)
        return process.wait(timeout=1)

    def _fake_process(self, root, pid, cmdline):
        directory = root / 'proc' / str(pid)
        directory.mkdir(parents=True)
        (directory / 'cmdline').write_bytes(cmdline)
        return directory

    def test_operation_query_ignores_process_that_exits_during_query(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            vanished = self._fake_process(root, 200, b'hd2-other\0')
            self._fake_process(root, 300, b'sleep\0')
            self.assertEqual(self._query_fake_proc(root, 'hd2-token', vanish=vanished), 0)

    def test_operation_query_finds_match_after_process_exits_during_query(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            vanished = self._fake_process(root, 200, b'hd2-other\0')
            self._fake_process(root, 300, b'sh\0hd2-token\0')
            self.assertEqual(self._query_fake_proc(root, 'hd2-token', vanish=vanished), 1)

    def test_operation_query_fails_closed_on_unreadable_live_process(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            vanished = self._fake_process(root, 200, b'hd2-other\0')
            unreadable = self._fake_process(root, 300, b'hd2-token\0')
            (unreadable / 'cmdline').chmod(0)
            self.assertEqual(self._query_fake_proc(root, 'hd2-token', vanish=vanished), 2)

    def test_reservation_failure_never_publishes_operation(self):
        with tempfile.TemporaryDirectory() as base:
            with hc.create_host_job(base=Path(base), hard_timeout=.1) as job, \
                 patch.object(hc, '_atomic_json', side_effect=OSError('write failed')), \
                 self.assertRaisesRegex(OSError, 'write failed'):
                hc.reserve_operation(job, ['true'], 'failed-reservation')
            self.assertEqual(list(Path(base).glob('scan-*/operation-*')), [])
            self.assertEqual(list(Path(base).glob('scan-*/.operation-stage-*')), [])

    def test_parent_removes_interrupted_staging_reservation(self):
        with tempfile.TemporaryDirectory() as base:
            manager = hc.create_host_job(base=Path(base), hard_timeout=.1)
            job = manager.__enter__()
            staging = job.path / '.operation-stage-interrupted'
            staging.mkdir(mode=0o700)
            (staging / 'partial-intent').write_text('{')
            manager.__exit__(None, None, None)
            self.assertFalse(job.path.exists())

    def test_parent_reconciles_submitted_operation_only_after_expiry(self):
        with tempfile.TemporaryDirectory() as base:
            manager = hc.create_host_job(base=Path(base), hard_timeout=.1)
            job = manager.__enter__()
            operation = hc.reserve_operation(job, ['true'], 'test-denied')
            hc.write_state(operation, hc.SUBMITTED_FILE, {
                'launcher_pid': 99, 'launcher_start_time': '1'})
            with patch.object(hc, '_host_operation_exists', return_value=False) as query:
                manager.__exit__(None, None, None)
            query.assert_called()
            self.assertFalse(job.path.exists())

    def test_parent_rechecks_submitted_operation_killed_before_ready(self):
        with tempfile.TemporaryDirectory() as base:
            manager = hc.create_host_job(base=Path(base), hard_timeout=.1)
            job = manager.__enter__()
            operation = hc.reserve_operation(job, ['true'], 'test-pre-ready-kill')
            hc.write_state(operation, hc.SUBMITTED_FILE, {
                'launcher_pid': 99, 'launcher_start_time': '1'})
            with patch.object(hc, '_host_operation_exists',
                              side_effect=[True, False]) as exists:
                manager.__exit__(None, None, None)
            self.assertGreaterEqual(exists.call_count, 2)
            self.assertFalse(job.path.exists())

    def test_submitted_operation_is_not_completed_before_expiry(self):
        with tempfile.TemporaryDirectory() as base:
            manager = hc.create_host_job(base=Path(base), hard_timeout=.1)
            job = manager.__enter__()
            operation = hc.reserve_operation(job, ['true'], 'test-host-race')
            hc.write_state(operation, hc.SUBMITTED_FILE, {
                'launcher_pid': 99, 'launcher_start_time': '1'})
            with patch.object(hc, '_uptime_ticks',
                              return_value=job.hard_deadline - 1), \
                 patch.object(hc, '_host_operation_exists') as query:
                self.assertFalse(hc.reconcile_operation(operation))
            self.assertFalse((operation.path / hc.COMPLETE_FILE).exists())
            query.assert_not_called()
            with patch.object(hc, '_uptime_ticks',
                              return_value=job.hard_deadline), \
                 patch.object(hc, '_host_operation_exists', return_value=False):
                self.assertTrue(hc.reconcile_operation(operation))
            manager.__exit__(None, None, None)

    def test_submitted_operation_is_not_completed_on_query_error(self):
        with tempfile.TemporaryDirectory() as base:
            manager = hc.create_host_job(base=Path(base), hard_timeout=.1)
            job = manager.__enter__()
            operation = hc.reserve_operation(job, ['true'], 'test-query-error')
            hc.write_state(operation, hc.SUBMITTED_FILE, {
                'launcher_pid': 99, 'launcher_start_time': '1'})
            with patch.object(hc, '_uptime_ticks',
                              return_value=job.hard_deadline), \
                 patch.object(hc, '_host_operation_exists', return_value=None):
                self.assertFalse(hc.reconcile_operation(operation))
            self.assertFalse((operation.path / hc.COMPLETE_FILE).exists())
            with patch.object(hc, '_host_operation_exists', return_value=False):
                hc.write_state(operation, hc.COMPLETE_FILE, {
                    'command_status': 125, 'cleanup_confirmed': True})
            manager.__exit__(None, None, None)

    def test_delayed_guard_after_negative_query_cannot_launch_child(self):
        with tempfile.TemporaryDirectory() as base:
            child_marker = Path(base) / 'child-started'
            manager = hc.create_host_job(base=Path(base), hard_timeout=.05)
            job = manager.__enter__()
            with patch.dict(os.environ, hc.host_job_environment(job), clear=False):
                command, operation = hc.guard_command(
                    ['/bin/sh', '-c', f': > {str(child_marker)!r}'],
                    operation='test-delayed-guard')
            hc.write_state(operation, hc.SUBMITTED_FILE, {
                'launcher_pid': 99, 'launcher_start_time': '1'})
            while hc._uptime_ticks() < job.hard_deadline:
                time.sleep(.005)
            with patch.object(hc, '_host_operation_exists', return_value=False):
                self.assertTrue(hc.reconcile_operation(operation))
            result = subprocess.run(command, timeout=3, start_new_session=True)
            self.assertEqual(result.returncode, -signal.SIGKILL)
            self.assertFalse(child_marker.exists())
            self.assertFalse((operation.path / hc.READY_FILE).exists())
            manager.__exit__(None, None, None)

    def test_nonce_query_finds_pre_ready_guard_identity(self):
        with tempfile.TemporaryDirectory() as base:
            with hc.create_host_job(base=Path(base), hard_timeout=3) as job:
                operation = hc.reserve_operation(job, ['true'], 'test-query-live')
                process = subprocess.Popen(
                    [sys.executable, '-c', 'import time;time.sleep(3)',
                     operation.nonce])
                try:
                    self.assertTrue(hc._host_operation_exists(operation))
                finally:
                    process.terminate()
                    process.wait(timeout=2)
                hc.mark_not_started(operation)

    def test_uptime_reader_rejects_malformed_clock(self):
        with patch.object(hc.Path, 'read_text', return_value='invalid\n'), \
             self.assertRaisesRegex(hc.HostCommandError, 'uptime'):
            hc._uptime_ticks()

    def test_context_waits_through_corrupt_registry_state(self):
        with tempfile.TemporaryDirectory() as base:
            manager = hc.create_host_job(base=Path(base), hard_timeout=.1)
            job = manager.__enter__()
            (job.path / hc.REGISTRY_FILE).write_text('{}')
            errors = []
            def close():
                try:
                    manager.__exit__(None, None, None)
                except BaseException as error:
                    errors.append(error)
            waiter = threading.Thread(target=close, daemon=True)
            waiter.start()
            time.sleep(.05)
            self.assertTrue(waiter.is_alive())
            self.assertTrue(job.path.exists())
            hc._atomic_json(job.path / hc.REGISTRY_FILE, hc._registry_value(job))
            waiter.join(2)
            self.assertFalse(waiter.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse(job.path.exists())

    def test_context_waits_through_corrupt_intent_state(self):
        with tempfile.TemporaryDirectory() as base:
            manager = hc.create_host_job(base=Path(base), hard_timeout=.1)
            job = manager.__enter__()
            operation = hc.reserve_operation(job, ['true'], 'corrupt-intent')
            intent = hc._read_json(operation.path / hc.INTENT_FILE)
            (operation.path / hc.INTENT_FILE).write_text('{}')
            errors = []
            waiter = threading.Thread(
                target=lambda: self._close_manager(manager, errors), daemon=True)
            waiter.start()
            time.sleep(.05)
            self.assertTrue(waiter.is_alive())
            self.assertTrue(job.path.exists())
            hc._atomic_json(operation.path / hc.INTENT_FILE, intent)
            waiter.join(2)
            self.assertFalse(waiter.is_alive())
            self.assertEqual(errors, [])

    @staticmethod
    def _close_manager(manager, errors):
        try:
            manager.__exit__(None, None, None)
        except BaseException as error:
            errors.append(error)

    def test_atomic_json_retries_short_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            real_write = os.write
            def short_write(descriptor, payload):
                return real_write(descriptor, payload[:max(1, len(payload) // 2)])
            with patch.object(hc.os, 'write', side_effect=short_write):
                hc._atomic_json(path, {'value': 'x' * 100})
            self.assertEqual(hc._read_json(path), {'value': 'x' * 100})

    def test_atomic_json_removes_partial_file_after_write_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with patch.object(hc.os, 'write', side_effect=OSError('short failure')):
                with self.assertRaisesRegex(OSError, 'short failure'):
                    hc._atomic_json(path, {'value': 1}, create=True)
            self.assertFalse(path.exists())

    def test_state_reader_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target.json'
            target.write_text('{}')
            link = root / 'link.json'
            link.symlink_to(target)
            with self.assertRaises((OSError, hc.HostCommandError)):
                hc._read_json(link)

    def test_context_waits_and_preserves_registry_until_confirmed(self):
        with tempfile.TemporaryDirectory() as base:
            manager = hc.create_host_job(base=Path(base), hard_timeout=.1)
            job = manager.__enter__()
            operation = hc.reserve_operation(job, ['true'], 'never-started')
            hc.write_state(operation, hc.READY_FILE, {
                'guard_pid': os.getpid(), 'pgid': os.getpid(), 'sid': os.getpid(),
                'guard_start_time': hc.process_start_time(os.getpid())})
            waiter = threading.Thread(
                target=lambda: manager.__exit__(None, None, None), daemon=True)
            waiter.start()
            time.sleep(.05)
            self.assertTrue(waiter.is_alive())
            self.assertTrue(job.path.exists())
            hc.write_state(operation, hc.COMPLETE_FILE, {
                'command_status': 125, 'cleanup_confirmed': True})
            waiter.join(2)
            self.assertFalse(waiter.is_alive())
            self.assertFalse(job.path.exists())


if __name__ == '__main__':
    unittest.main()
