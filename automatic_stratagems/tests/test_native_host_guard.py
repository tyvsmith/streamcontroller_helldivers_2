import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import CancelledError
from unittest.mock import patch

from automatic_stratagems import host_commands as hc
from automatic_stratagems.scanner import game_capture as gc
from automatic_stratagems.tests.process_support import process_is_live


class NativeHostGuardTests(unittest.TestCase):
    def setUp(self):
        if Path('/.flatpak-info').exists():
            self.skipTest('native host guard requires a native test process')

    def _job_environment(self, job):
        return patch.dict(
            os.environ, {**hc.host_job_environment(job), 'FLATPAK_ID': ''},
            clear=False)

    def _operation(self, job):
        operations = list(job.path.glob('operation-*'))
        self.assertEqual(len(operations), 1)
        return operations[0]

    def _identity(self, path):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                return json.loads(path.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(.01)
        self.fail(f'process identity was not written: {path}')

    def _same_process_is_live(self, identity):
        try:
            return (hc.process_start_time(identity['pid']) == identity['start_time']
                    and process_is_live(identity['pid']))
        except FileNotFoundError:
            return False

    def _kill_exact(self, identity):
        pid = identity['pid']
        if self._same_process_is_live(identity):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _assert_dead(self, identity):
        self.addCleanup(self._kill_exact, identity)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self._same_process_is_live(identity):
            time.sleep(.01)
        self.assertFalse(self._same_process_is_live(identity))

    def _child_script(self, marker):
        return (
            'import json,os,signal,time;'
            'from automatic_stratagems.host_commands import process_start_time;'
            'signal.signal(signal.SIGINT,signal.SIG_IGN);'
            'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
            f'open({str(marker)!r},"w").write(json.dumps('
            '{"pid":os.getpid(),"start_time":process_start_time(os.getpid()),'
            '"pgid":os.getpgrp(),"sid":os.getsid(0)}));'
            'time.sleep(60)'
        )

    def _success_command(self, child_marker):
        child_script = self._child_script(child_marker)
        return [sys.executable, '-c', (
            'import os,pathlib,subprocess,sys,time;'
            f'subprocess.Popen([sys.executable,"-c",{child_script!r}],'
            'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,'
            'stderr=subprocess.DEVNULL);'
            f'marker=pathlib.Path({str(child_marker)!r});'
            'deadline=time.monotonic()+2;'
            'exec("while not marker.exists() and time.monotonic() < deadline:\\n'
            ' time.sleep(.01)");'
            'assert marker.exists();'
            'os.write(1,(os.environ["HD2_NATIVE_GUARD_VALUE"]+"\\n").encode())'
        )]

    def _hanging_command(self, leader_marker, child_marker, *, noisy=False):
        child_script = self._child_script(child_marker)
        output = 'os.write(1,b"x"*131072);' if noisy else ''
        return [sys.executable, '-c', (
            'import json,os,pathlib,signal,subprocess,sys,time;'
            'from automatic_stratagems.host_commands import process_start_time;'
            'signal.signal(signal.SIGINT,signal.SIG_IGN);'
            'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
            f'child=subprocess.Popen([sys.executable,"-c",{child_script!r}],'
            'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,'
            'stderr=subprocess.DEVNULL);'
            f'child_marker=pathlib.Path({str(child_marker)!r});'
            'deadline=time.monotonic()+2;'
            'exec("while not child_marker.exists() and time.monotonic() < deadline:\\n'
            ' time.sleep(.01)");'
            'assert child_marker.exists();'
            f'open({str(leader_marker)!r},"w").write(json.dumps('
            '{"pid":os.getpid(),"start_time":process_start_time(os.getpid()),'
            '"pgid":os.getpgrp(),"sid":os.getsid(0)}));'
            f'{output}'
            'time.sleep(60)'
        )]

    def test_success_returns_bytes_and_reaps_descendant(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            child_marker = base / 'child.json'
            with hc.create_host_job(base=base, hard_timeout=5) as job, \
                    self._job_environment(job):
                output = gc.run_command(
                    self._success_command(child_marker), host=True,
                    operation='native-guard-success',
                    host_env={'HD2_NATIVE_GUARD_VALUE': 'space ; $()'})
                child = self._identity(child_marker)
                ready = hc._read_json(self._operation(job) / hc.READY_FILE)
                self.assertEqual(output, b'space ; $()\n')
                self.assertEqual(ready['guard_pid'], ready['pgid'])
                self.assertEqual(ready['guard_pid'], ready['sid'])
                self.assertEqual(child['pgid'], ready['pgid'])
                self.assertEqual(child['sid'], ready['sid'])
                self._assert_dead(child)
            self.assertEqual(list(base.glob('scan-*')), [])

    def test_timeout_cancel_and_overflow_reap_command_tree(self):
        for mode in ('timeout', 'cancel', 'overflow'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                leader_marker = base / 'leader.json'
                child_marker = base / 'child.json'
                cancelled = threading.Event()
                setter = None
                if mode == 'cancel':
                    def cancel_after_start():
                        while not leader_marker.exists():
                            time.sleep(.01)
                        cancelled.set()
                    setter = threading.Thread(target=cancel_after_start, daemon=True)
                    setter.start()
                with hc.create_host_job(base=base, hard_timeout=5) as job, \
                        self._job_environment(job):
                    command = self._hanging_command(
                        leader_marker, child_marker, noisy=mode == 'overflow')
                    error = (CancelledError if mode == 'cancel' else gc.ScanError)
                    pattern = None if mode == 'cancel' else (
                        'stdout exceeded' if mode == 'overflow' else 'timed out')
                    context = self.assertRaises(error) if pattern is None else \
                        self.assertRaisesRegex(error, pattern)
                    with context:
                        gc.run_command(
                            command, host=True, operation=f'native-guard-{mode}',
                            timeout=.3 if mode == 'timeout' else 2,
                            stdout_limit=1024 if mode == 'overflow' else None,
                            cancel_event=cancelled if mode == 'cancel' else None)
                    leader = self._identity(leader_marker)
                    child = self._identity(child_marker)
                    ready = hc._read_json(self._operation(job) / hc.READY_FILE)
                    self.assertEqual(ready['guard_pid'], ready['pgid'])
                    self.assertEqual(ready['guard_pid'], ready['sid'])
                    self.assertEqual(leader['pgid'], ready['pgid'])
                    self.assertEqual(child['pgid'], ready['pgid'])
                    self._assert_dead(leader)
                    self._assert_dead(child)
                if setter is not None:
                    setter.join(1)
                    self.assertFalse(setter.is_alive())
                self.assertEqual(list(base.glob('scan-*')), [])

    def test_missing_setsid_fails_before_operation_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            with hc.create_host_job(base=base, hard_timeout=2) as job, \
                    self._job_environment(job), \
                    patch.object(gc, 'NATIVE_SETSID',
                                 '/missing/dummytransport-setsid'):
                with self.assertRaisesRegex(gc.ScanError, 'setsid is unavailable'):
                    gc.run_command(['/usr/bin/true'], host=True,
                                   operation='native-guard-missing-setsid')
                self.assertEqual(list(job.path.glob('operation-*')), [])
            self.assertEqual(list(base.glob('scan-*')), [])


if __name__ == '__main__':
    unittest.main()
