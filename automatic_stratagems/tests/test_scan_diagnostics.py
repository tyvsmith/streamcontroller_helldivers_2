import ast
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from automatic_stratagems import scan_diagnostics, scan_runner


class ScanDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        self.parent = Path(parent.name)
        self.cache = self.parent / 'diagnostics'

    def marker(self, run):
        return json.loads((run / scan_diagnostics.RUN_MARKER).read_text())

    def test_runner_names_are_the_diagnostics_objects(self):
        for name in ('prepare_diagnostic_run', 'close_diagnostic_run', 'prune_diagnostics'):
            self.assertIs(getattr(scan_runner, name), getattr(scan_diagnostics, name))
        # No stale runner copies that a patch could target without effect.
        for name in ('MAX_DIAGNOSTIC_RUNS', 'MAX_DIAGNOSTIC_BYTES', 'DIAGNOSTIC_OWNER',
                     'CACHE_MARKER', 'RUN_MARKER', '_reject_symlink', '_read_marker',
                     '_write_marker', '_process_identity', '_process_state', '_owned_run'):
            self.assertFalse(hasattr(scan_runner, name), name)
        tree = ast.parse(Path(scan_diagnostics.__file__).read_text())
        imported = [node.module or '' for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)]
        imported += [alias.name for node in ast.walk(tree)
                     if isinstance(node, (ast.Import, ast.ImportFrom))
                     for alias in node.names]
        self.assertFalse([name for name in imported if 'scan_runner' in name])

    def test_close_records_status_and_refuses_closed_or_unowned_runs(self):
        run = scan_diagnostics.prepare_diagnostic_run(self.cache)
        before = time.time()
        scan_diagnostics.close_diagnostic_run(run, 'failed')
        marker = self.marker(run)
        self.assertEqual(marker['status'], 'failed')
        self.assertGreaterEqual(marker['completed_at'], before)
        with self.assertRaisesRegex(ValueError, 'not open and owned'):
            scan_diagnostics.close_diagnostic_run(run, 'complete')
        self.assertEqual(self.marker(run)['status'], 'failed')

        foreign = scan_diagnostics.prepare_diagnostic_run(self.cache)
        scan_diagnostics._write_marker(foreign / scan_diagnostics.RUN_MARKER,
                                       dict(self.marker(foreign), owner='someone-else'))
        with self.assertRaisesRegex(ValueError, 'not open and owned'):
            scan_diagnostics.close_diagnostic_run(foreign, 'complete')
        with self.assertRaisesRegex(ValueError, 'not open and owned'):
            scan_diagnostics.close_diagnostic_run(self.parent / 'missing', 'complete')

    def test_prune_enforces_byte_limit_oldest_completed_first(self):
        runs = []
        for _ in range(3):
            run = scan_diagnostics.prepare_diagnostic_run(self.cache)
            (run / 'frame.bin').write_bytes(b'x' * 1000)
            scan_diagnostics.close_diagnostic_run(run, 'complete')
            runs.append(run)
            time.sleep(.01)
        live = scan_diagnostics.prepare_diagnostic_run(self.cache)
        (live / 'frame.bin').write_bytes(b'x' * 5000)
        # Markers add roughly 200 bytes per run: about 8,770 bytes in total.
        with patch.object(scan_diagnostics, 'MAX_DIAGNOSTIC_BYTES', 7000):
            scan_diagnostics.prune_diagnostics(self.cache)
        self.assertEqual([run.exists() for run in runs], [False, False, True])
        self.assertTrue(live.exists())

    def test_prune_never_removes_open_runs_even_over_the_limits(self):
        live = scan_diagnostics.prepare_diagnostic_run(self.cache)
        with patch.object(scan_diagnostics, 'MAX_DIAGNOSTIC_RUNS', 0), \
             patch.object(scan_diagnostics, 'MAX_DIAGNOSTIC_BYTES', 0):
            scan_diagnostics.prune_diagnostics(self.cache)
        self.assertTrue(live.exists())
        self.assertEqual(self.marker(live)['status'], 'open')

    def test_default_limits_are_twenty_runs_and_512_mib(self):
        self.assertEqual(scan_diagnostics.MAX_DIAGNOSTIC_RUNS, 20)
        self.assertEqual(scan_diagnostics.MAX_DIAGNOSTIC_BYTES, 512 * 1024 * 1024)

    def test_prune_refuses_unowned_cache_without_deleting(self):
        self.cache.mkdir()
        (self.cache / 'run-keep').mkdir()
        with self.assertRaisesRegex(ValueError, 'not owned'):
            scan_diagnostics.prune_diagnostics(self.cache)
        self.assertTrue((self.cache / 'run-keep').exists())

    def test_symlink_in_an_ancestor_is_rejected(self):
        (self.parent / 'real').mkdir()
        (self.parent / 'alias').symlink_to(self.parent / 'real', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            scan_diagnostics.prepare_diagnostic_run(self.parent / 'alias' / 'nested' / 'cache')
        self.assertEqual(list((self.parent / 'real').iterdir()), [])

    def test_marker_is_private_and_create_refuses_existing_paths(self):
        path = self.parent / 'marker.json'
        scan_diagnostics._write_marker(path, {'a': 1}, create=True)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            scan_diagnostics._write_marker(path, {'a': 2}, create=True)
        self.assertEqual(json.loads(path.read_text()), {'a': 1})
        link = self.parent / 'link.json'
        link.symlink_to(self.parent / 'elsewhere.json')
        with self.assertRaises(FileExistsError):
            scan_diagnostics._write_marker(link, {'a': 3}, create=True)
        self.assertFalse((self.parent / 'elsewhere.json').exists())

        scan_diagnostics._write_marker(path, {'b': 2})
        self.assertEqual(path.read_text(), '{"b": 2}\n')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(item.name for item in self.parent.iterdir()),
                         ['link.json', 'marker.json'])

    def test_marker_write_without_progress_reports_short_write(self):
        path = self.parent / 'marker.json'
        with patch.object(scan_diagnostics.os, 'write', return_value=0), \
             self.assertRaisesRegex(OSError, 'short write while updating diagnostic marker'):
            scan_diagnostics._write_marker(path, {'a': 1}, create=True)
        self.assertFalse(path.exists())

    def test_marker_fsyncs_only_the_file_not_the_directory(self):
        path = self.parent / 'marker.json'
        synced = []
        real_fsync = os.fsync

        def fsync(descriptor):
            synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
            return real_fsync(descriptor)

        with patch.object(scan_diagnostics.os, 'fsync', side_effect=fsync):
            scan_diagnostics._write_marker(path, {'a': 1}, create=True)
            scan_diagnostics._write_marker(path, {'a': 2})
        self.assertEqual(synced, [False, False])

    @unittest.skipUnless(Path('/proc/sys/kernel/random/boot_id').exists(), 'requires procfs')
    def test_process_identity_uses_boot_id_and_proc_start_time(self):
        identity = scan_diagnostics._process_identity()
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        start = Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()[19]
        self.assertEqual(identity, {'pid': os.getpid(), 'boot_id': boot_id,
                                    'start_time': start})
        self.assertEqual(scan_diagnostics._process_state(identity), 'match')
        self.assertEqual(scan_diagnostics._process_state(
            dict(identity, start_time=str(int(start) + 1))), 'mismatch')
        other_boot = str(uuid.uuid4())
        self.assertEqual(scan_diagnostics._process_state(
            dict(identity, boot_id=other_boot)), 'mismatch')
        self.assertEqual(scan_diagnostics._process_state(
            dict(identity, pid=2_000_000_000)), 'dead')
        for malformed in (None, {'pid': os.getpid()}, dict(identity, pid=True),
                          dict(identity, pid=0), dict(identity, boot_id='not-a-uuid'),
                          dict(identity, start_time='12a')):
            with self.subTest(identity=malformed):
                self.assertEqual(scan_diagnostics._process_state(malformed), 'unknown')

    def test_process_identity_without_proc_records_only_the_pid(self):
        with patch.object(Path, 'read_text', side_effect=PermissionError('no proc')):
            self.assertEqual(scan_diagnostics._process_identity(1234), {'pid': 1234})

    @unittest.skipUnless(Path('/proc/sys/kernel/random/boot_id').exists(), 'requires procfs')
    def test_prune_closes_open_run_from_another_boot(self):
        run = scan_diagnostics.prepare_diagnostic_run(self.cache)
        marker = self.marker(run)
        marker['runner'] = {'pid': 1, 'boot_id': str(uuid.uuid4()), 'start_time': '1'}
        scan_diagnostics._write_marker(run / scan_diagnostics.RUN_MARKER, marker)
        scan_diagnostics.prune_diagnostics(self.cache)
        marker = self.marker(run)
        self.assertEqual((marker['status'], marker['reason']), ('failed', 'abandoned runner'))
        self.assertTrue(run.exists())


if __name__ == '__main__':
    unittest.main()
