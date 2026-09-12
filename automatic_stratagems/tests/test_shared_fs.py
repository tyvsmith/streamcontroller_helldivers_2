"""Shared stdlib-only filesystem primitives used by every execution context."""

from concurrent.futures import CancelledError
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from automatic_stratagems.shared import fs


class WriteAllTests(unittest.TestCase):
    def test_retries_short_writes_until_every_byte_is_written(self):
        real_write = os.write
        lengths = []

        def short_write(descriptor, data):
            lengths.append(len(data))
            return real_write(descriptor, bytes(data[:3]))

        with tempfile.TemporaryFile() as stream, \
                patch.object(fs.os, 'write', side_effect=short_write):
            fs.write_all(stream.fileno(), b'abcdefgh', 'unused')
            stream.seek(0)
            self.assertEqual(stream.read(), b'abcdefgh')
        self.assertEqual(lengths, [8, 5, 2])

    def test_non_positive_write_raises_the_callers_message(self):
        for result in (0, -1):
            with self.subTest(result=result), \
                    patch.object(fs.os, 'write', return_value=result), \
                    self.assertRaises(OSError) as caught:
                fs.write_all(99, b'x', 'short write while testing')
            self.assertEqual(caught.exception.args, ('short write while testing',))

    def test_empty_data_makes_no_write(self):
        with patch.object(fs.os, 'write') as write:
            fs.write_all(99, b'', 'unused')
        write.assert_not_called()


class FsyncDirectoryTests(unittest.TestCase):
    def test_fsyncs_a_read_only_directory_descriptor_and_closes_it(self):
        real_open = os.open
        opened = []
        synced = []

        def track_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            opened.append((Path(path), flags, descriptor))
            return descriptor

        def record_fsync(descriptor):
            synced.append((descriptor, stat.S_ISDIR(os.fstat(descriptor).st_mode)))

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(fs.os, 'open', side_effect=track_open), \
                patch.object(fs.os, 'fsync', side_effect=record_fsync), \
                patch.object(fs.os, 'close', wraps=os.close) as close:
            fs.fsync_directory(Path(directory))
        self.assertEqual(len(opened), 1)
        path, flags, descriptor = opened[0]
        self.assertEqual(path, Path(directory))
        self.assertEqual(flags, os.O_RDONLY | os.O_DIRECTORY)
        self.assertEqual(synced, [(descriptor, True)])
        close.assert_called_once_with(descriptor)

    def test_closes_the_descriptor_when_fsync_fails(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(fs.os, 'fsync', side_effect=OSError('fsync failed')), \
                patch.object(fs.os, 'close', wraps=os.close) as close, \
                self.assertRaisesRegex(OSError, 'fsync failed'):
            fs.fsync_directory(directory)
        close.assert_called_once()

    def test_rejects_a_regular_file(self):
        with tempfile.NamedTemporaryFile() as stream, \
                self.assertRaises(NotADirectoryError):
            fs.fsync_directory(stream.name)


class TooLarge(Exception):
    pass


class AtomicJsonTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, path, value, max_bytes=4096):
        fs.atomic_json(path, value, max_bytes=max_bytes,
                       too_large=lambda: TooLarge('record too large'),
                       short_write_message='short write while testing')

    def test_writes_canonical_bytes_with_private_mode_and_creates_parents(self):
        path = self.root / 'nested' / 'record.json'
        self.write(path, {'b': [1, 2], 'a': 'é'})
        self.assertEqual(path.read_bytes(), b'{"a":"\\u00e9","b":[1,2]}\n')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(os.listdir(path.parent), ['record.json'])

    def test_canonical_json_matches_the_record_bytes(self):
        self.assertEqual(fs.canonical_json({'z': None, 'a': {'y': 1, 'b': True}}),
                         b'{"a":{"b":true,"y":1},"z":null}\n')

    def test_oversized_record_raises_the_factory_error_before_touching_disk(self):
        path = self.root / 'nested' / 'record.json'
        with self.assertRaisesRegex(TooLarge, 'record too large'):
            self.write(path, {'a': 'x' * 10}, max_bytes=8)
        self.assertFalse(path.parent.exists())

    def test_record_exactly_at_the_limit_is_written(self):
        path = self.root / 'record.json'
        value = {'a': 'x'}
        self.write(path, value, max_bytes=len(fs.canonical_json(value)))
        self.assertEqual(path.read_bytes(), b'{"a":"x"}\n')

    def test_temporary_is_exclusive_private_and_no_follow(self):
        path = self.root / 'record.json'
        real_open = os.open
        opened = []

        def track_open(target, flags, *args, **kwargs):
            opened.append((Path(target), flags, args))
            return real_open(target, flags, *args, **kwargs)

        with patch.object(fs.os, 'open', side_effect=track_open):
            self.write(path, {'a': 1})
        temporary, flags, args = opened[0]
        self.assertEqual(temporary.parent, self.root)
        self.assertTrue(temporary.name.startswith('.record.json.'))
        self.assertEqual(len(temporary.name), len('.record.json.') + 32)
        self.assertEqual(flags, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW)
        self.assertEqual(args, (0o600,))

    def test_file_is_fsynced_before_replace_and_parent_after(self):
        path = self.root / 'record.json'
        events = []
        real_fsync = os.fsync
        real_replace = os.replace

        def fsync(descriptor):
            events.append('fsync-file')
            real_fsync(descriptor)

        def replace(source, target):
            events.append('replace')
            real_replace(source, target)

        def fsync_directory(directory):
            events.append(('fsync-directory', Path(directory)))

        with patch.object(fs.os, 'fsync', side_effect=fsync), \
                patch.object(fs.os, 'replace', side_effect=replace), \
                patch.object(fs, 'fsync_directory', side_effect=fsync_directory):
            self.write(path, {'a': 1})
        self.assertEqual(events, ['fsync-file', 'replace',
                                  ('fsync-directory', self.root)])

    def test_short_writes_are_retried(self):
        path = self.root / 'record.json'
        real_write = os.write
        with patch.object(fs.os, 'write',
                          side_effect=lambda fd, data: real_write(fd, data[:2])):
            self.write(path, {'value': 'x' * 40})
        self.assertEqual(path.read_bytes(),
                         fs.canonical_json({'value': 'x' * 40}))

    def test_stalled_write_raises_short_write_message_and_removes_temporary(self):
        path = self.root / 'record.json'
        with patch.object(fs.os, 'write', return_value=0), \
                self.assertRaises(OSError) as caught:
            self.write(path, {'a': 1})
        self.assertEqual(caught.exception.args, ('short write while testing',))
        self.assertEqual(os.listdir(self.root), [])

    def test_write_failure_preserves_previous_record_and_removes_temporary(self):
        path = self.root / 'record.json'
        self.write(path, {'a': 1})
        real_write = os.write

        def failed_write(descriptor, data):
            real_write(descriptor, data[:3])
            raise OSError('disk full')

        with patch.object(fs.os, 'write', side_effect=failed_write), \
                self.assertRaisesRegex(OSError, 'disk full'):
            self.write(path, {'a': 2})
        self.assertEqual(path.read_bytes(), b'{"a":1}\n')
        self.assertEqual(os.listdir(self.root), ['record.json'])

    def test_directory_fsync_failure_after_replace_keeps_the_new_record(self):
        path = self.root / 'record.json'
        with patch.object(fs, 'fsync_directory', side_effect=OSError('sync failed')), \
                self.assertRaisesRegex(OSError, 'sync failed'):
            self.write(path, {'a': 1})
        self.assertEqual(path.read_bytes(), b'{"a":1}\n')
        self.assertEqual(os.listdir(self.root), ['record.json'])


class Stream:
    def __init__(self, chunks, error=None):
        self.chunks = list(chunks)
        self.error = error
        self.sizes = []
        self.closed = False

    def read(self, size):
        self.sizes.append(size)
        if self.error is not None and not self.chunks:
            raise self.error
        return self.chunks.pop(0) if self.chunks else b''

    def close(self):
        self.closed = True


class ReadBoundedStreamTests(unittest.TestCase):
    def test_collects_every_chunk_until_eof_then_closes(self):
        stream = Stream([b'abc', b'def'])
        output = bytearray()
        overflow = []
        fs.read_bounded_stream(stream, 10, 'stdout', output, overflow)
        self.assertEqual(bytes(output), b'abcdef')
        self.assertEqual(overflow, [])
        self.assertEqual(stream.sizes, [64 * 1024] * 3)
        self.assertTrue(stream.closed)

    def test_output_exactly_at_the_limit_is_not_overflow(self):
        stream = Stream([b'abc', b'def'])
        output = bytearray()
        overflow = []
        fs.read_bounded_stream(stream, 6, 'stdout', output, overflow)
        self.assertEqual((bytes(output), overflow), (b'abcdef', []))

    def test_overflow_keeps_the_limit_records_the_name_and_stops_reading(self):
        stream = Stream([b'abcd', b'efgh', b'never read'])
        output = bytearray()
        overflow = []
        fs.read_bounded_stream(stream, 6, 'stderr', output, overflow)
        self.assertEqual(bytes(output), b'abcdef')
        self.assertEqual(overflow, ['stderr'])
        self.assertEqual(stream.chunks, [b'never read'])
        self.assertTrue(stream.closed)

    def test_limit_counts_bytes_already_in_the_output(self):
        stream = Stream([b'abc'])
        output = bytearray(b'xy')
        overflow = []
        fs.read_bounded_stream(stream, 4, 'stdout', output, overflow)
        self.assertEqual((bytes(output), overflow), (b'xyab', ['stdout']))

    def test_closes_the_stream_when_a_read_fails(self):
        stream = Stream([b'abc'], error=OSError('pipe broke'))
        with self.assertRaisesRegex(OSError, 'pipe broke'):
            fs.read_bounded_stream(stream, 10, 'stdout', bytearray(), [])
        self.assertTrue(stream.closed)


def stat_metadata(**changes):
    values = {'st_dev': 1, 'st_ino': 2, 'st_mode': stat.S_IFREG | 0o644,
              'st_size': 3, 'st_mtime_ns': 4, 'st_ctime_ns': 5}
    values.update(changes)
    return SimpleNamespace(**values)


class FileStampTests(unittest.TestCase):
    def test_file_stamp_is_device_inode_size_and_mtime(self):
        self.assertEqual(fs.file_stamp(stat_metadata()), (1, 2, 3, 4))

    def test_typed_file_stamp_adds_the_file_type_before_size(self):
        self.assertEqual(fs.typed_file_stamp(stat_metadata()),
                         (1, 2, stat.S_IFREG, 3, 4))

    def test_typed_file_stamp_with_ctime_adds_ctime_last(self):
        self.assertEqual(fs.typed_file_stamp_with_ctime(stat_metadata()),
                         (1, 2, stat.S_IFREG, 3, 4, 5))

    def test_each_stamp_ignores_exactly_the_fields_it_omits(self):
        base = stat_metadata()
        chmodded = stat_metadata(st_mode=stat.S_IFREG | 0o600, st_ctime_ns=6)
        retyped = stat_metadata(st_mode=stat.S_IFDIR | 0o644)
        self.assertEqual(fs.file_stamp(retyped), fs.file_stamp(base))
        self.assertEqual(fs.typed_file_stamp(chmodded), fs.typed_file_stamp(base))
        self.assertNotEqual(fs.typed_file_stamp(retyped), fs.typed_file_stamp(base))
        self.assertNotEqual(fs.typed_file_stamp_with_ctime(chmodded),
                            fs.typed_file_stamp_with_ctime(base))
        for field, value in (('st_dev', 9), ('st_ino', 9), ('st_size', 9),
                             ('st_mtime_ns', 9)):
            changed = stat_metadata(**{field: value})
            with self.subTest(field=field):
                self.assertNotEqual(fs.file_stamp(changed), fs.file_stamp(base))

    def test_stamps_accept_real_stat_results(self):
        with tempfile.NamedTemporaryFile() as stream:
            metadata = os.stat(stream.name)
        self.assertEqual(fs.typed_file_stamp_with_ctime(metadata), (
            metadata.st_dev, metadata.st_ino, stat.S_IFREG, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns))


class Expired(Exception):
    pass


class GuardTests(unittest.TestCase):
    def test_cancel_check_raises_only_for_a_set_event(self):
        event = threading.Event()
        fs.check_cancel(None)
        fs.check_cancel(event)
        event.set()
        with self.assertRaises(CancelledError):
            fs.check_cancel(event)

    def test_deadline_check_raises_the_factory_error_at_or_after_the_deadline(self):
        error = Expired('deadline exhausted')
        with patch.object(fs.time, 'monotonic', return_value=5.0):
            for deadline in (4.0, 5.0):
                with self.subTest(deadline=deadline), \
                        self.assertRaises(Expired) as caught:
                    fs.check_deadline(deadline, lambda: error)
                self.assertIs(caught.exception, error)

    def test_deadline_check_never_builds_the_error_before_the_deadline(self):
        expired = Mock(side_effect=AssertionError('built too early'))
        with patch.object(fs.time, 'monotonic', return_value=5.0) as monotonic:
            fs.check_deadline(None, expired)
            monotonic.assert_not_called()
            fs.check_deadline(5.5, expired)
        expired.assert_not_called()

    def test_poll_deadline_caps_the_wait_at_the_work_deadline(self):
        with patch.object(fs.time, 'monotonic', return_value=10.0) as monotonic:
            self.assertEqual(fs.poll_deadline(5, None), 15.0)
            self.assertEqual(fs.poll_deadline(5, 12.0), 12.0)
            self.assertEqual(fs.poll_deadline(5, 20.0), 15.0)
        self.assertEqual(monotonic.call_count, 3)


class Clock:
    def __init__(self):
        self.now = 0
        self.reads = 0
        self.sleeps = []

    def monotonic(self):
        self.reads += 1
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class WaitForStableStampTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for name in ('monotonic', 'sleep'):
            patcher = patch.object(fs.time, name, side_effect=getattr(self.clock, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def probe(*observations):
        return Mock(side_effect=list(observations))

    def test_returns_the_stamp_after_two_equal_ready_observations(self):
        probe = self.probe(('path', 'A', True), ('path', 'A', True))
        self.assertEqual(fs.wait_for_stable_stamp(probe, 100, interval=10), 'A')
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(self.clock.sleeps, [10])
        self.assertEqual(self.clock.reads, 3)

    def test_changing_stamp_keeps_waiting(self):
        probe = self.probe(('path', 'A', True), ('path', 'B', True),
                           ('path', 'B', True))
        self.assertEqual(fs.wait_for_stable_stamp(probe, 100, interval=10), 'B')
        self.assertEqual(probe.call_count, 3)

    def test_unready_stamps_are_remembered_but_never_accepted(self):
        probe = self.probe(('path', 'A', True), ('path', 'B', False),
                           ('path', 'B', False), ('path', 'A', True),
                           ('path', 'A', True))
        self.assertEqual(fs.wait_for_stable_stamp(probe, 100, interval=10), 'A')
        self.assertEqual(probe.call_count, 5)

    def test_no_observation_keeps_the_remembered_stamp(self):
        probe = self.probe(('path', 'A', True), None, ('path', 'A', True))
        self.assertEqual(fs.wait_for_stable_stamp(probe, 100, interval=10), 'A')
        self.assertEqual(probe.call_count, 3)

    def test_stamps_are_remembered_per_key(self):
        probe = self.probe(('a', 'S', True), ('b', 'S', True), ('a', 'S', True))
        self.assertEqual(
            fs.wait_for_stable_stamp(probe, 100, interval=10,
                                     accept=lambda key, stamp: (key, stamp)),
            ('a', 'S'))
        self.assertEqual(probe.call_count, 3)

    def test_accept_result_is_returned_and_none_keeps_polling(self):
        probe = self.probe(*[('path', 'A', True)] * 3)
        accept = Mock(side_effect=[None, 'frame'])
        self.assertEqual(
            fs.wait_for_stable_stamp(probe, 100, interval=10, accept=accept),
            'frame')
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(accept.call_args_list, [(('path', 'A'),)] * 2)

    def test_returns_none_at_the_end_without_sleeping_past_it(self):
        probe = self.probe(('path', 'A', True), ('path', 'B', True),
                           ('path', 'C', True))
        self.assertIsNone(fs.wait_for_stable_stamp(probe, 25, interval=10))
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(self.clock.sleeps, [10, 10, 5])

    def test_expired_end_never_probes(self):
        self.clock.now = 30
        probe = self.probe()
        self.assertIsNone(fs.wait_for_stable_stamp(probe, 25, interval=10))
        probe.assert_not_called()
        self.assertEqual(self.clock.sleeps, [])

    def test_cancel_is_checked_before_every_probe(self):
        event = threading.Event()
        event.set()
        probe = self.probe()
        with self.assertRaises(CancelledError):
            fs.wait_for_stable_stamp(probe, 100, interval=10, cancel_event=event)
        probe.assert_not_called()

        event.clear()
        probe = Mock(side_effect=lambda: event.set())
        with self.assertRaises(CancelledError):
            fs.wait_for_stable_stamp(probe, 100, interval=10, cancel_event=event)
        self.assertEqual(probe.call_count, 1)

    def test_probe_and_accept_errors_propagate(self):
        with self.assertRaisesRegex(ValueError, 'probe failed'):
            fs.wait_for_stable_stamp(Mock(side_effect=ValueError('probe failed')),
                                     100, interval=10)
        probe = self.probe(('path', 'A', True), ('path', 'A', True))
        with self.assertRaisesRegex(OSError, 'accept failed'):
            fs.wait_for_stable_stamp(
                probe, 100, interval=10,
                accept=Mock(side_effect=OSError('accept failed')))


if __name__ == '__main__':
    unittest.main()
