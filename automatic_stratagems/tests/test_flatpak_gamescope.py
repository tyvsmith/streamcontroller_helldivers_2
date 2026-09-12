import json
import os
from concurrent.futures import CancelledError
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from automatic_stratagems import host_commands as hc
from automatic_stratagems.hostexec import gamescope_flatpak as gf
from automatic_stratagems.scanner.capture.gamescope import capture_into_shared_path
from automatic_stratagems.scanner.game_capture import ScanError
from automatic_stratagems.shared.gamescope_target import FlatpakGamescopeTarget


class FlatpakGamescopeTests(unittest.TestCase):
    def target(self, cache):
        metadata = cache.stat()
        return FlatpakGamescopeTarget(
            game_pid=200, game_start_time='1200', sandbox_pid=50,
            sandbox_start_time='4242', instance_id='steam-instance',
            socket='/run/user/1000/gamescope-7', socket_dev=1, socket_ino=2,
            host_cache=str(cache), sandbox_cache='/home/player/.cache',
            cache_dev=metadata.st_dev, cache_ino=metadata.st_ino,
            gamescopectl='/usr/lib/extensions/vulkan/gamescope/bin/gamescopectl')

    def namespace(self, root, target):
        proc = root / 'proc'
        sandbox = root / 'sandbox'
        (proc / str(target.sandbox_pid)).mkdir(parents=True)
        (sandbox / 'home' / 'player').mkdir(parents=True)
        os.symlink(sandbox, proc / str(target.sandbox_pid) / 'root')
        os.symlink(target.host_cache,
                   sandbox / 'home' / 'player' / '.cache')
        return proc

    def record_directory(self, job, target):
        output = job.path / 'gamescope-test' / 'capture.png'
        output.parent.mkdir(mode=0o700)
        directory = Path(tempfile.mkdtemp(
            prefix='hd2-gamescope-', dir=target.host_cache))
        directory.chmod(0o700)
        metadata = directory.stat()
        output_metadata = output.parent.stat()
        record = output.parent / gf.RECORD_NAME
        gf._write_record(record, {
            'version': 1, 'owner': 'net_jslay_helldivers_2',
            'job_nonce': job.nonce, 'cache': target.host_cache,
            'cache_dev': target.cache_dev, 'cache_ino': target.cache_ino,
            'directory': str(directory), 'directory_dev': metadata.st_dev,
            'directory_ino': metadata.st_ino,
            'output_dev': output_metadata.st_dev,
            'output_ino': output_metadata.st_ino,
            'uid': os.getuid(), 'mode': 0o700})
        return record, directory

    def test_host_capture_uses_exact_sandbox_and_copies_stable_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'steam-cache'
            cache.mkdir()
            target = self.target(cache)
            proc = self.namespace(root, target)

            def enter(argv, **_kwargs):
                capture = next(cache.glob('hd2-gamescope-*')) / 'capture.png'
                Image.new('RGB', (20, 10), 'red').save(capture)
                return subprocess.CompletedProcess(argv, 0)

            with hc.create_host_job(base=root / 'jobs', hard_timeout=3) as job:
                output = job.path / 'gamescope-test' / 'capture.png'
                output.parent.mkdir(mode=0o700)
                with patch.object(gf, 'validate_flatpak_gamescope_target',
                                  return_value=target) as validate, \
                     patch.object(gf.subprocess, 'run', side_effect=enter) as run:
                    gf.capture_on_host(
                        target, output, deadline=time.monotonic() + 1,
                        job=job, proc_root=proc)

                with Image.open(output) as image:
                    self.assertEqual(image.size, (20, 10))
                self.assertTrue(any(cache.iterdir()))
                gf._remove_recorded_directory(
                    output.parent / gf.RECORD_NAME, job)

            self.assertFalse(any(cache.iterdir()))
            argv = run.call_args.args[0]
            self.assertEqual(argv[:4], ['/usr/bin/flatpak', 'enter', '50',
                                       '/usr/bin/env'])
            self.assertIn('GAMESCOPE_WAYLAND_DISPLAY=/run/user/1000/gamescope-7',
                          argv)
            self.assertEqual(argv[-1], '1')
            self.assertGreaterEqual(validate.call_count, 2)

    def test_stable_wait_accepts_a_delayed_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.png'
            writer = threading.Timer(.02, path.write_bytes, args=(b'frame',))
            writer.start()
            try:
                stamp = gf._wait_stable(path, time.monotonic() + .3)
            finally:
                writer.join()
            self.assertEqual(stamp[2], 5)

    def test_stable_wait_rejects_symlink_and_oversized_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / 'real.png'
            real.write_bytes(b'frame')
            symlink = root / 'capture.png'
            symlink.symlink_to(real)
            with self.assertRaisesRegex(gf.FlatpakGamescopeError,
                                        'complete screenshot'):
                gf._wait_stable(symlink, time.monotonic() + .02)
            symlink.unlink()
            symlink.write_bytes(b'123456789')
            with patch.object(gf, 'MAX_ENCODED_IMAGE_BYTES', 8), \
                 self.assertRaisesRegex(gf.FlatpakGamescopeError,
                                             'encoded image limit'):
                gf._wait_stable(symlink, time.monotonic() + .02)

    def test_stable_wait_skips_empty_and_unreadable_observations(self):
        def metadata(size):
            return type('Metadata', (), {
                'st_mode': 0o100600, 'st_dev': 1, 'st_ino': 2,
                'st_size': size, 'st_mtime_ns': 10})()

        path = unittest.mock.MagicMock()
        path.is_symlink.return_value = False
        path.lstat.side_effect = [metadata(5), OSError('gone'), metadata(0),
                                  metadata(5)]
        with patch.object(gf.time, 'sleep'):
            stamp = gf._wait_stable(path, time.monotonic() + 5)
        self.assertEqual(stamp, (1, 2, 5, 10))
        self.assertEqual(path.lstat.call_count, 4)

    def test_frame_copy_handles_short_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.png'
            output = root / 'capture.png'
            source.write_bytes(b'abcdefghi')
            metadata = source.stat()
            expected = (metadata.st_dev, metadata.st_ino, metadata.st_size,
                        metadata.st_mtime_ns)
            real_write = os.write

            def short_write(descriptor, value):
                length = max(1, len(value) // 2)
                return real_write(descriptor, bytes(value[:length]))

            with patch.object(gf.os, 'write', side_effect=short_write):
                gf._copy_frame(source, output, expected)
            self.assertEqual(output.read_bytes(), b'abcdefghi')

    def test_frame_copy_rejects_source_changed_during_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.png'
            output = root / 'capture.png'
            source.write_bytes(b'first-frame')
            metadata = source.stat()
            expected = (metadata.st_dev, metadata.st_ino, metadata.st_size,
                        metadata.st_mtime_ns)
            real_read = os.read
            changed = False

            def change_after_read(descriptor, count):
                nonlocal changed
                value = real_read(descriptor, count)
                if value and not changed:
                    changed = True
                    source.write_bytes(b'replaced-frame-with-new-size')
                return value

            with patch.object(gf.os, 'read', side_effect=change_after_read), \
                 self.assertRaisesRegex(gf.FlatpakGamescopeError,
                                             'changed|ended'):
                gf._copy_frame(source, output, expected)
            self.assertFalse(output.exists())

    def test_masked_flatpak_enter_failure_times_out_without_stale_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'steam-cache'
            cache.mkdir()
            target = self.target(cache)
            proc = self.namespace(root, target)
            with hc.create_host_job(base=root / 'jobs', hard_timeout=3) as job:
                output = job.path / 'gamescope-test' / 'capture.png'
                output.parent.mkdir(mode=0o700)
                with patch.object(gf, 'validate_flatpak_gamescope_target',
                                  return_value=target), \
                     patch.object(gf.subprocess, 'run', return_value=
                                  subprocess.CompletedProcess([], 0)), \
                     self.assertRaisesRegex(gf.FlatpakGamescopeError,
                                                 'complete screenshot'):
                    gf.capture_on_host(
                        target, output, deadline=time.monotonic() + .03,
                        job=job, proc_root=proc)
                self.assertTrue(any(cache.iterdir()))
                gf._remove_recorded_directory(
                    output.parent / gf.RECORD_NAME, job)
            self.assertFalse(any(cache.iterdir()))

    def test_sandbox_wrapper_uses_bounded_host_runner_and_cancel_event(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'cache'
            cache.mkdir()
            target = self.target(cache)
            cancel = object()
            with hc.create_host_job(base=Path(directory) / 'jobs',
                                    hard_timeout=3) as job:
                output = job.path / 'gamescope-test' / 'capture.png'
                output.parent.mkdir(mode=0o700)
                with patch.dict(os.environ, hc.host_job_environment(job)), \
                     patch('automatic_stratagems.scanner.game_capture.run_command',
                           return_value=b'') as run:
                    capture_into_shared_path(
                        target, output, timeout=1.25, deadline=123.5,
                        cancel_event=cancel)
            capture = run.call_args_list[0]
            cleanup = run.call_args_list[1]
            self.assertEqual(capture.kwargs['timeout'], 1.25)
            self.assertEqual(capture.kwargs['operation'],
                             'gamescope-flatpak-capture')
            self.assertIs(capture.kwargs['cancel_event'], cancel)
            self.assertTrue(capture.kwargs['host'])
            self.assertIn('--deadline', capture.args[0])
            self.assertEqual(cleanup.kwargs['operation'],
                             'gamescope-flatpak-cleanup')
            self.assertNotIn('cancel_event', cleanup.kwargs)

    def test_sandbox_wrapper_derives_deadline_when_caller_has_none(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache'
            cache.mkdir()
            target = self.target(cache)
            with hc.create_host_job(base=root / 'jobs', hard_timeout=3) as job:
                output = job.path / 'gamescope-test' / 'capture.png'
                output.parent.mkdir(mode=0o700)
                with patch.dict(os.environ, hc.host_job_environment(job)), \
                     patch('automatic_stratagems.scanner.game_capture.run_command',
                           return_value=b'') as run, \
                     patch.object(gf.time, 'monotonic', return_value=100):
                    capture_into_shared_path(
                        target, output, timeout=1.25, deadline=None)
            capture_argv = run.call_args_list[0].args[0]
            self.assertEqual(capture_argv[capture_argv.index('--deadline') + 1],
                             '101.25')

    def test_confirmed_cancellation_runs_cleanup_without_cancel_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache'
            cache.mkdir()
            target = self.target(cache)
            with hc.create_host_job(base=root / 'jobs', hard_timeout=3) as job:
                output = job.path / 'gamescope-test' / 'capture.png'
                output.parent.mkdir(mode=0o700)
                with patch.dict(os.environ, hc.host_job_environment(job)), \
                     patch('automatic_stratagems.scanner.game_capture.run_command',
                           side_effect=[CancelledError(), b'']) as run, \
                     self.assertRaises(CancelledError):
                    capture_into_shared_path(
                        target, output, timeout=1, deadline=123.5,
                        cancel_event=object())
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[1].kwargs['operation'],
                             'gamescope-flatpak-cleanup')
            self.assertNotIn('cancel_event', run.call_args_list[1].kwargs)

    def test_unconfirmed_host_cleanup_preserves_record_for_later_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache'
            cache.mkdir()
            target = self.target(cache)
            with hc.create_host_job(base=root / 'jobs', hard_timeout=3) as job:
                output = job.path / 'gamescope-test' / 'capture.png'
                output.parent.mkdir(mode=0o700)
                with patch.dict(os.environ, hc.host_job_environment(job)), \
                     patch('automatic_stratagems.scanner.game_capture.run_command',
                           side_effect=hc.HostCleanupUnconfirmed(
                               'cleanup unconfirmed')) as run, \
                     self.assertRaises(hc.HostCleanupUnconfirmed):
                    capture_into_shared_path(
                        target, output, timeout=1, deadline=123.5)
            self.assertEqual(run.call_count, 1)

    def test_unconfirmed_record_cleanup_wins_over_capture_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache'
            cache.mkdir()
            target = self.target(cache)
            with hc.create_host_job(base=root / 'jobs', hard_timeout=3) as job:
                output = job.path / 'gamescope-test' / 'capture.png'
                output.parent.mkdir(mode=0o700)
                cleanup = hc.HostCleanupUnconfirmed('cleanup unconfirmed')
                with patch.dict(os.environ, hc.host_job_environment(job)), \
                     patch('automatic_stratagems.scanner.game_capture.run_command',
                           side_effect=[CancelledError(), cleanup]), \
                     self.assertRaises(hc.HostCleanupUnconfirmed) as raised:
                    capture_into_shared_path(
                        target, output, timeout=1, deadline=123.5)
            self.assertIsInstance(raised.exception.__cause__, CancelledError)

    def test_failed_record_cleanup_reports_retained_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache'
            cache.mkdir()
            target = self.target(cache)
            with hc.create_host_job(base=root / 'jobs', hard_timeout=3) as job:
                output = job.path / 'gamescope-test' / 'capture.png'
                output.parent.mkdir(mode=0o700)
                with patch.dict(os.environ, hc.host_job_environment(job)), \
                     patch('automatic_stratagems.scanner.game_capture.run_command',
                           side_effect=[CancelledError(),
                                        ScanError('cleanup failed')]), \
                     self.assertRaises(hc.HostCommandError) as raised:
                    capture_into_shared_path(
                        target, output, timeout=1, deadline=123.5)
            self.assertIn(str(output.parent / gf.RECORD_NAME),
                          str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, ScanError)
            self.assertIsInstance(raised.exception.__cause__.__cause__,
                                  CancelledError)

    def test_cleanup_rejects_unexpected_sibling_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache'
            cache.mkdir()
            target = self.target(cache)
            with hc.create_host_job(base=root / 'jobs', hard_timeout=3) as job:
                record, capture_dir = self.record_directory(job, target)
                sibling = capture_dir / 'unrelated'
                sibling.write_text('keep')
                with self.assertRaisesRegex(gf.FlatpakGamescopeError,
                                            'unexpected files'):
                    gf._remove_recorded_directory(record, job)
                self.assertEqual(sibling.read_text(), 'keep')
                sibling.unlink()
                gf._remove_recorded_directory(record, job)
            self.assertFalse(capture_dir.exists())

    def test_cleanup_rejects_replaced_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache'
            cache.mkdir()
            target = self.target(cache)
            with hc.create_host_job(base=root / 'jobs', hard_timeout=3) as job:
                record, capture_dir = self.record_directory(job, target)
                saved = capture_dir.with_name(capture_dir.name + '-saved')
                capture_dir.rename(saved)
                capture_dir.mkdir(mode=0o700)
                marker = capture_dir / 'replacement'
                marker.write_text('keep')
                with self.assertRaisesRegex(gf.FlatpakGamescopeError,
                                            'directory changed'):
                    gf._remove_recorded_directory(record, job)
                self.assertEqual(marker.read_text(), 'keep')
                marker.unlink()
                capture_dir.rmdir()
                saved.rename(capture_dir)
                gf._remove_recorded_directory(record, job)

    def test_sigkill_after_record_allows_exact_record_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache'
            cache.mkdir()
            target = self.target(cache)
            proc = self.namespace(root, target)
            with hc.create_host_job(base=root / 'jobs', hard_timeout=5) as job:
                output = job.path / 'gamescope-test' / 'capture.png'
                output.parent.mkdir(mode=0o700)
                script = '''
import json
from pathlib import Path
import time
from automatic_stratagems.hostexec import gamescope_flatpak as gf
from automatic_stratagems.shared.gamescope_target import FlatpakGamescopeTarget
from automatic_stratagems.shared.host_job import HostJob
target = FlatpakGamescopeTarget.from_dict(json.loads(__import__('sys').argv[1]))
job = HostJob(Path(__import__('sys').argv[2]), __import__('sys').argv[3], int(__import__('sys').argv[4]))
gf.validate_flatpak_gamescope_target = lambda value, **kwargs: value
gf.subprocess.run = lambda *args, **kwargs: time.sleep(60)
gf.capture_on_host(target, Path(__import__('sys').argv[5]), deadline=time.monotonic() + 60, job=job, proc_root=Path(__import__('sys').argv[6]))
'''
                process = subprocess.Popen([
                    sys.executable, '-c', script,
                    json.dumps(target.to_dict()), str(job.path), job.nonce,
                    str(job.hard_deadline), str(output), str(proc)])
                record = output.parent / gf.RECORD_NAME
                deadline = time.monotonic() + 2
                while not record.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(record.exists())
                process.kill()
                process.wait(timeout=2)
                self.assertLess(process.returncode, 0)
                value, _ = gf._read_record(record)
                capture_dir = Path(value['directory'])
                self.assertTrue(capture_dir.exists())
                gf._remove_recorded_directory(record, job)
                self.assertFalse(capture_dir.exists())


if __name__ == '__main__':
    unittest.main()
