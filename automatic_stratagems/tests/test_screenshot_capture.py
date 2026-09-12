import importlib
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time
import types
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

from .package_loader import plugin_module


class ScreenshotCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capture = importlib.import_module(
            plugin_module('automatic_stratagems.scanner.screenshot_capture'))
        cls.cleanup = importlib.import_module(
            plugin_module('automatic_stratagems.scanner.capture.screenshot_cleanup'))
        cls.files = importlib.import_module(
            plugin_module('automatic_stratagems.scanner.capture.screenshot_files'))
        cls.source = importlib.import_module(
            plugin_module('automatic_stratagems.scanner.capture.screenshot_source'))
        cls.trigger = importlib.import_module(
            plugin_module('automatic_stratagems.scanner.capture.screenshot_trigger'))
        cls.detect = importlib.import_module(
            plugin_module('automatic_stratagems.scanner.capture.screenshot_detect'))
        cls.game_capture = importlib.import_module(
            plugin_module('automatic_stratagems.scanner.game_capture'))

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def image(self, name='frame.png', color='red'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (12, 8), color).save(path)
        return path

    def config(self, **updates):
        value = {
            'kind': 'path', 'path': str(self.root), 'trigger': 'none',
            'hotkey': 'KEY_F12', 'script': '', 'delete_after_scan': False,
            'previous_fingerprint': None, 'allow_rescan': False,
        }
        value.update(updates)
        return value

    def steam_config(self, **updates):
        (self.root / 'thumbnails').mkdir(exist_ok=True)
        values = dict(kind='folder', path=str(self.root), trigger='script',
                      script=sys.executable, delete_after_scan=True)
        values.update(updates)
        return self.config(**values)

    def test_module_imports_without_pillow_or_evdev(self):
        source = Path(self.capture.__file__).read_text()
        tree = __import__('ast').parse(source)
        top_imports = {
            alias.name.split('.')[0]
            for node in tree.body if isinstance(node, (__import__('ast').Import,
                                                        __import__('ast').ImportFrom))
            for alias in node.names
        }
        self.assertNotIn('PIL', top_imports)
        self.assertNotIn('evdev', top_imports)

    def test_lower_capture_modules_never_import_the_facade(self):
        ast = __import__('ast')
        for module in (self.cleanup, self.files, self.source, self.trigger,
                       self.detect):
            tree = ast.parse(Path(module.__file__).read_text())
            imported = [
                node.module or '' for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            ] + [alias.name for node in ast.walk(tree)
                 if isinstance(node, ast.Import) for alias in node.names]
            top = {alias.name.split('.')[0] for node in tree.body
                   if isinstance(node, (ast.Import, ast.ImportFrom))
                   for alias in node.names}
            with self.subTest(module=module.__name__):
                self.assertFalse([name for name in imported
                                  if 'screenshot_capture' in name])
                self.assertFalse(top & {'PIL', 'evdev'})
        # Trigger and detect are siblings; neither needs the other.
        trigger_source = Path(self.trigger.__file__).read_text()
        detect_source = Path(self.detect.__file__).read_text()
        self.assertNotIn('screenshot_detect', trigger_source)
        self.assertNotIn('screenshot_trigger', detect_source)

    def test_facade_names_are_the_lower_module_objects(self):
        for name in ('cleanup_screenshot',):
            self.assertIs(getattr(self.capture, name), getattr(self.cleanup, name))
        for name in ('resolve_source', 'is_steam_managed'):
            self.assertIs(getattr(self.capture, name), getattr(self.source, name))
        for name in ('_source_kind', '_trigger_snapshot', '_wait_for_screenshot'):
            self.assertIs(getattr(self.capture, name), getattr(self.detect, name))
        for name in ('_key_codes', '_script_path', '_run_script', '_capture_hotkey'):
            self.assertIs(getattr(self.capture, name), getattr(self.trigger, name))
        # No stale facade copies that a patch could target without effect.
        for name in ('_CLEANUP_LOCK', '_CLEANUP_RECORDS', 'THUMBNAIL_WAIT_SECONDS',
                     '_steam_directories', '_wait_for_steam_thumbnail',
                     '_delete_cleanup_pairs', '_fingerprint_descriptor',
                     'MAX_DIRECTORY_ENTRIES', '_register_cleanup',
                     '_steam_thumbnail_snapshot', 'WAIT_SECONDS',
                     'MAX_CAPTURE_OUTPUT_BYTES', '_read_triggered_candidate',
                     '_cancellable_sleep', '_absolute', '_directory_snapshot',
                     '_stable_fingerprint', 'SUPPORTED_SUFFIXES'):
            self.assertFalse(hasattr(self.capture, name), name)

    def registered(self, config, name='new.png', newly_created=True, steam=None):
        path = self.image(name)
        metadata = {
            'path': str(path), 'size_bytes': path.stat().st_size,
            'mtime_ns': path.stat().st_mtime_ns,
            'fingerprint': __import__('hashlib').sha256(path.read_bytes()).hexdigest(),
        }
        identity = self.cleanup._metadata_snapshot(path.lstat())
        self.cleanup._register_cleanup(config, metadata, identity, newly_created,
                                       steam)
        return path, metadata, identity

    def test_register_cleanup_directly_records_only_new_non_steam_files(self):
        config = self.config(kind='folder', trigger='script', script=sys.executable,
                             delete_after_scan=True)
        records = self.cleanup._CLEANUP_RECORDS
        count = len(records)
        with patch.object(self.source, '_steam_directories', return_value=[]):
            _, disabled, _ = self.registered({**config, 'delete_after_scan': False})
            _, existing, _ = self.registered(config, newly_created=False)
            path, created, identity = self.registered(config)
        self.assertEqual(disabled.keys() & {'cleanup_token', 'cleanup_skipped'}, set())
        self.assertNotIn('cleanup_token', existing)
        self.assertIn('Pre-existing', existing['cleanup_skipped'])
        record = records[created['cleanup_token']]
        self.assertEqual(len(records), count + 1)
        self.assertEqual(record['source'], ('folder', str(self.root), 'script'))
        self.assertEqual(record['identity'], identity[:5])
        self.assertEqual(len(record['parent_identity']), 6)
        self.assertIsNone(record['thumbnail'])
        self.assertTrue(self.cleanup.cleanup_screenshot(config, created))
        self.assertFalse(path.exists())
        self.assertEqual(len(records), count)

        with patch.object(self.source, '_steam_directories',
                          return_value=[self.root.resolve().parent]):
            _, nested, _ = self.registered(config, 'nested.png')
        with patch.object(self.source, '_steam_directories',
                          return_value=[self.root.resolve()]):
            _, unverified, _ = self.registered(config, 'unverified.png')
        self.assertIn('Only standard Steam', nested['cleanup_skipped'])
        self.assertIn('could not be verified', unverified['cleanup_skipped'])
        self.assertNotIn('cleanup_token', nested)
        self.assertNotIn('cleanup_token', unverified)
        self.assertEqual(len(records), count)

    def test_cleanup_directly_consumes_record_when_source_differs(self):
        config = self.config(kind='folder', trigger='script', script=sys.executable,
                             delete_after_scan=True)
        with patch.object(self.source, '_steam_directories', return_value=[]):
            path, metadata, _ = self.registered(config)
        token = metadata['cleanup_token']
        self.assertFalse(self.cleanup.cleanup_screenshot(
            {**config, 'trigger': 'hotkey'}, metadata))
        self.assertNotIn(token, self.cleanup._CLEANUP_RECORDS)
        self.assertFalse(self.cleanup.cleanup_screenshot(config, metadata))
        self.assertTrue(path.exists())

    def test_resolve_source_normalizes_explicit_path_without_requiring_it(self):
        missing = self.root / 'missing' / '..' / 'capture.png'
        resolved = self.capture.resolve_source(self.config(kind='file', path=str(missing)))
        self.assertEqual(resolved['path'], str(self.root / 'capture.png'))
        self.assertEqual(resolved['kind'], 'file')

    def test_resolve_source_requires_one_steam_directory_for_blank_path(self):
        first = self.root / 'first'
        second = self.root / 'second'
        first.mkdir(); second.mkdir()
        config = self.config(path='')
        with patch.object(self.source, '_steam_directories', return_value=[first]):
            self.assertEqual(self.capture.resolve_source(config)['path'], str(first))
        for directories in ([], [first, second]):
            with self.subTest(count=len(directories)), \
                 patch.object(self.source, '_steam_directories', return_value=directories), \
                 self.assertRaises(self.game_capture.ScanError):
                self.capture.resolve_source(config)

    def test_public_steam_managed_predicate_uses_discovered_directories(self):
        with patch.object(self.source, '_steam_directories',
                          return_value=[self.root]):
            self.assertTrue(self.capture.is_steam_managed(self.root / 'shot.png'))
            self.assertFalse(self.capture.is_steam_managed(
                self.root.parent / 'shot.png'))

    def test_steam_discovery_bounds_userdata_enumeration(self):
        userdata = self.root / '.local/share/Steam/userdata'
        (userdata / 'one').mkdir(parents=True)
        (userdata / 'two').mkdir()
        with patch.object(self.source.Path, 'home', return_value=self.root), \
             patch.object(self.source, 'MAX_DIRECTORY_ENTRIES', 1), \
             self.assertRaisesRegex(self.game_capture.ScanError, 'too many'):
            self.source._steam_directories()

    def test_check_setup_rejects_invalid_config_without_triggering(self):
        invalid = (
            self.config(kind='live'),
            self.config(trigger='command'),
            self.config(delete_after_scan='yes'),
            self.config(allow_rescan='yes'),
            self.config(previous_fingerprint='bad'),
            self.config(path='relative/path'),
        )
        with patch.object(self.capture, '_run_script') as script, \
             patch.object(self.capture, '_capture_hotkey') as hotkey:
            for config in invalid:
                with self.subTest(config=config), self.assertRaises(
                        self.game_capture.ScanError):
                    self.capture.check_screenshot_setup(config)
        script.assert_not_called()
        hotkey.assert_not_called()

    def test_check_setup_accepts_future_triggered_file_but_not_missing_reuse(self):
        target = self.root / 'future.png'
        self.capture.check_screenshot_setup(
            self.config(kind='file', path=str(target), trigger='script',
                        script=sys.executable))
        with self.assertRaises(self.game_capture.ScanError):
            self.capture.check_screenshot_setup(
                self.config(kind='file', path=str(target), trigger='none'))

    def test_check_setup_rejects_unsupported_explicit_and_detected_files(self):
        unsupported = self.root / 'capture.txt'
        with self.assertRaisesRegex(self.game_capture.ScanError, 'PNG or JPEG'):
            self.capture.check_screenshot_setup(self.config(
                kind='file', path=str(unsupported), trigger='script',
                script=sys.executable))
        unsupported.write_text('not an image')
        with self.assertRaisesRegex(self.game_capture.ScanError, 'PNG or JPEG'):
            self.capture.check_screenshot_setup(self.config(
                kind='path', path=str(unsupported), trigger='script',
                script=sys.executable))

    def test_no_trigger_uses_existing_source_freshness_options(self):
        target = self.image()
        image, metadata = self.capture.capture_screenshot(
            self.config(kind='file', path=str(target)))
        self.assertEqual(image.size, (12, 8))
        self.assertEqual(metadata['selection'], 'file')
        repeated = self.config(kind='file', path=str(target),
                               previous_fingerprint=metadata['fingerprint'])
        with self.assertRaisesRegex(self.game_capture.ScanError, 'not changed'):
            self.capture.capture_screenshot(repeated)
        image, again = self.capture.capture_screenshot(
            {**repeated, 'allow_rescan': True})
        self.assertEqual(image.size, (12, 8))
        self.assertEqual(again['fingerprint'], metadata['fingerprint'])
        self.assertNotIn('cleanup_token', again)

    def test_script_trigger_accepts_one_new_folder_image(self):
        existing = self.image('old.png')
        config = self.config(kind='folder', trigger='script', script=sys.executable,
                             delete_after_scan=True)

        def trigger(*_args, **_kwargs):
            self.image('new.png', 'blue')

        with patch.object(self.capture, '_run_script', side_effect=trigger):
            image, metadata = self.capture.capture_screenshot(config)
        self.assertEqual(image.getpixel((0, 0)), (0, 0, 255))
        self.assertEqual(metadata['selection'], 'folder')
        self.assertEqual(metadata['path'], str(self.root / 'new.png'))
        self.assertIn('cleanup_token', metadata)
        self.assertTrue(existing.exists())

    def test_triggered_file_must_change_even_when_rescan_is_allowed(self):
        target = self.image()
        config = self.config(kind='file', path=str(target), trigger='script',
                             script=sys.executable, allow_rescan=True)
        with patch.object(self.capture, '_run_script'), \
             patch.object(self.detect, 'WAIT_SECONDS', .08), \
             self.assertRaisesRegex(self.game_capture.ScanError, 'new screenshot'):
            self.capture.capture_screenshot(config)

    def test_triggered_overwrite_is_accepted_but_never_deletable(self):
        target = self.image(color='red')
        config = self.config(kind='file', path=str(target), trigger='script',
                             script=sys.executable, delete_after_scan=True)
        with patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: Image.new(
                              'RGB', (12, 8), 'blue').save(target)):
            _, metadata = self.capture.capture_screenshot(config)
        self.assertEqual(metadata['fingerprint'],
                         __import__('hashlib').sha256(target.read_bytes()).hexdigest())
        self.assertNotIn('cleanup_token', metadata)
        self.assertIn('dedicated folder', metadata['cleanup_skipped'])
        self.assertFalse(self.capture.cleanup_screenshot(config, metadata))
        self.assertTrue(target.exists())

    def test_trigger_snapshot_hashes_preexisting_bytes_without_decoding(self):
        target = self.root / 'capture.png'
        target.write_bytes(b'not an image')
        config = self.capture.resolve_source(self.config(
            kind='file', path=str(target), trigger='script',
            script=sys.executable))
        before = self.capture._trigger_snapshot(config, 'file')
        self.assertEqual(before[3], __import__('hashlib').sha256(
            b'not an image').hexdigest())

    def test_fingerprint_rejects_growth_after_recorded_size_with_bounded_reads(self):
        target = self.root / 'capture.png'
        target.write_bytes(b'x')
        descriptor = os.open(target, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        expected = self.files._metadata_snapshot(os.fstat(descriptor))
        with patch.object(self.files.os, 'read', return_value=b'x') as read, \
             self.assertRaisesRegex(self.game_capture.ScanError, 'changed'):
            self.files._fingerprint_descriptor(descriptor, expected)
        self.assertEqual(read.call_count, 2)

    def test_candidate_replacement_after_decode_is_rejected(self):
        target = self.image(color='red')
        image_source = importlib.import_module(
            plugin_module('automatic_stratagems.scanner.image_source'))
        original = image_source.read_image_source

        def replace_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            replacement = self.root / 'replacement.png'
            Image.new('RGB', (12, 8), 'blue').save(replacement)
            replacement.replace(target)
            return result

        with patch.object(image_source, 'read_image_source',
                          side_effect=replace_after_read), \
             self.assertRaisesRegex(self.game_capture.ScanError, 'changed'):
            self.detect._read_triggered_candidate(
                target, 'file', self.config(), False, None, None)

    def test_trigger_rejects_multiple_new_images(self):
        config = self.config(kind='folder', trigger='script', script=sys.executable)

        def trigger(*_args, **_kwargs):
            self.image('one.png')
            self.image('two.jpg')

        with patch.object(self.capture, '_run_script', side_effect=trigger), \
             self.assertRaisesRegex(self.game_capture.ScanError, 'Multiple new'):
            self.capture.capture_screenshot(config)

    def test_trigger_rejects_new_symlink_candidate(self):
        outside = self.image('outside.png')
        config = self.config(kind='folder', trigger='script',
                             script=sys.executable)
        with patch.object(self.capture, '_run_script', side_effect=lambda *_a, **_k:
                          (self.root / 'linked.png').symlink_to(outside)), \
             patch.object(self.detect, 'WAIT_SECONDS', .05), \
             self.assertRaises(self.game_capture.ScanError):
            self.capture.capture_screenshot(config)

    def test_path_trigger_detects_folder_and_reports_folder_selection(self):
        config = self.config(kind='path', trigger='script', script=sys.executable)
        with patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: self.image('new.png')):
            _, metadata = self.capture.capture_screenshot(config)
        self.assertEqual(metadata['selection'], 'folder')

    def test_trigger_bounds_directory_enumeration(self):
        self.image('one.png')
        self.image('two.png')
        config = self.config(kind='folder', trigger='script', script=sys.executable)
        with patch.object(self.files, 'MAX_DIRECTORY_ENTRIES', 1), \
             self.assertRaisesRegex(self.game_capture.ScanError, 'too many'):
            self.capture.capture_screenshot(config)

    def test_cancellation_during_snapshot_prevents_trigger(self):
        self.image('old.png')
        cancelled = threading.Event()
        original = os.scandir

        class Entries:
            def __init__(self, path):
                self.entries = original(path)
                self.first = True

            def __enter__(self):
                self.entries.__enter__()
                return self

            def __exit__(self, *args):
                return self.entries.__exit__(*args)

            def __iter__(self): return self

            def __next__(self):
                entry = next(self.entries)
                if self.first:
                    self.first = False
                    cancelled.set()
                return entry

        trigger = Mock()
        with patch.object(self.capture.os, 'scandir', side_effect=Entries), \
             patch.object(self.capture, '_run_script', trigger), \
             self.assertRaises(__import__('concurrent.futures').futures.CancelledError):
            self.capture.capture_screenshot(
                self.config(kind='folder', trigger='script', script=sys.executable),
                cancel_event=cancelled)
        trigger.assert_not_called()

    def test_late_ambiguous_image_publishes_no_cleanup_provenance(self):
        config = self.config(kind='folder', trigger='script', script=sys.executable,
                             delete_after_scan=True)
        original = self.detect._read_triggered_candidate

        def read_then_publish_second(*args, **kwargs):
            result = original(*args, **kwargs)
            self.image('two.png')
            return result

        records = len(self.cleanup._CLEANUP_RECORDS)
        with patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: self.image('one.png')), \
             patch.object(self.detect, '_read_triggered_candidate',
                          side_effect=read_then_publish_second), \
             self.assertRaisesRegex(self.game_capture.ScanError, 'Multiple new'):
            self.capture.capture_screenshot(config)
        self.assertEqual(len(self.cleanup._CLEANUP_RECORDS), records)

    def test_trigger_wait_honors_timeout_and_cancellation(self):
        target = self.root / 'future.png'
        config = self.config(kind='file', path=str(target), trigger='script',
                             script=sys.executable)
        with patch.object(self.capture, '_run_script'), \
             patch.object(self.detect, 'WAIT_SECONDS', .05), \
             self.assertRaisesRegex(self.game_capture.ScanError, 'new screenshot'):
            self.capture.capture_screenshot(config)
        cancelled = threading.Event()
        with patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: cancelled.set()), \
             self.assertRaises(__import__('concurrent.futures').futures.CancelledError):
            self.capture.capture_screenshot(config, cancel_event=cancelled)

    def test_script_runs_as_one_host_executable_without_arguments(self):
        output = self.root / 'argv.json'
        script = self.root / 'writer'
        script.write_text(
            '#!/usr/bin/python3\nimport json,sys\n'
            f'open({str(output)!r}, "w").write(json.dumps(sys.argv))\n')
        script.chmod(0o700)
        config = self.config(kind='file', path=str(self.root / 'future.png'),
                             trigger='script', script=str(script))
        run_command = self.game_capture.run_command
        def run_locally(command, **kwargs):
            kwargs['host'] = False
            return run_command(command, **kwargs)
        # Exercise executable argv locally; real host transport has its own suite.
        with patch.object(self.game_capture, 'run_command', side_effect=run_locally):
            self.capture._run_script(config, None, time.monotonic() + 3)
        self.assertEqual(json.loads(output.read_text()), [str(script)])

    def test_script_uses_bounded_owned_host_runner_contract(self):
        script = self.root / 'capture'
        script.write_text('#!/bin/sh\nexit 0\n')
        script.chmod(0o700)
        config = self.config(kind='file', path=str(self.root / 'future.png'),
                             trigger='script', script=str(script))
        with patch.object(self.game_capture, 'run_command') as run:
            self.capture._run_script(config, None, time.monotonic() + 2)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [str(script)])
        self.assertTrue(run.call_args.kwargs['host'])
        self.assertEqual(run.call_args.kwargs['operation'], 'screenshot-script')
        self.assertLessEqual(run.call_args.kwargs['stdout_limit'], 16 * 1024)
        self.assertLessEqual(run.call_args.kwargs['stderr_limit'], 16 * 1024)

    def test_hotkey_releases_chord_in_reverse_and_holds_device_during_wait(self):
        target = self.root / 'new.png'
        events = []

        class Keyboard:
            def __enter__(self):
                events.append(('enter',))
                return self

            def __exit__(self, *_):
                events.append(('exit', target.exists()))

            def write(self, _kind, code, value):
                events.append(('write', code, value))

            def syn(self):
                events.append(('syn',))

        evdev = types.ModuleType('evdev')
        evdev.ecodes = types.SimpleNamespace(EV_KEY=1, KEY_LEFTCTRL=29, KEY_F12=88)
        evdev.UInput = Mock(return_value=Keyboard())
        config = self.config(kind='folder', trigger='hotkey',
                             hotkey='KEY_LEFTCTRL+KEY_F12')

        def wait(*_args, **_kwargs):
            self.image('new.png')
            return self.detect._read_triggered_candidate(
                target, 'folder', config, True, None, None)

        with patch.dict(sys.modules, {'evdev': evdev}), \
             patch.object(self.capture.os, 'access', return_value=True), \
             patch.object(self.trigger, '_cancellable_sleep'), \
             patch.object(self.capture, '_wait_for_screenshot', side_effect=wait):
            self.capture.capture_screenshot(config)
        writes = [event[1:] for event in events if event[0] == 'write']
        self.assertEqual(writes, [(29, 1), (88, 1), (88, 0), (29, 0)])
        self.assertEqual(events[-1], ('exit', True))
        self.assertEqual(evdev.UInput.call_args.args[0], {1: [29, 88]})

    def test_hotkey_supports_valid_codes_above_legacy_255_range(self):
        class Keyboard:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def write(self, *_): pass
            def syn(self): pass

        evdev = types.ModuleType('evdev')
        evdev.ecodes = types.SimpleNamespace(
            EV_KEY=1, KEY_MACRO1=656, KEY_MAX=767, KEY_CNT=768)
        evdev.UInput = Mock(return_value=Keyboard())
        with patch.dict(sys.modules, {'evdev': evdev}), \
             patch.object(self.trigger, '_cancellable_sleep'):
            self.capture._capture_hotkey(
                self.config(hotkey='KEY_MACRO1'), lambda: ('image', {}))
        self.assertEqual(evdev.UInput.call_args.args[0], {1: [656]})
        with patch.dict(sys.modules, {'evdev': evdev}), \
             self.assertRaises(self.game_capture.ScanError):
            self.capture._key_codes('KEY_MAX')

    def test_hotkey_partial_failure_still_attempts_every_reverse_release(self):
        events = []

        class Keyboard:
            def __enter__(self): return self
            def __exit__(self, *_): raise RuntimeError('exit failed')
            def syn(self): pass
            def write(self, _kind, code, value):
                events.append((code, value))
                if (code, value) == (88, 1):
                    raise OSError('write failed')

        evdev = types.ModuleType('evdev')
        evdev.ecodes = types.SimpleNamespace(EV_KEY=1, KEY_LEFTCTRL=29, KEY_F12=88)
        evdev.UInput = Mock(return_value=Keyboard())
        with patch.dict(sys.modules, {'evdev': evdev}), \
             patch.object(self.capture.os, 'access', return_value=True), \
             patch.object(self.trigger, '_cancellable_sleep'), \
             self.assertRaisesRegex(self.game_capture.ScanError, 'write failed'):
            self.capture.capture_screenshot(self.config(
                kind='folder', trigger='hotkey', hotkey='KEY_LEFTCTRL+KEY_F12'))
        self.assertEqual(events, [(29, 1), (88, 1), (88, 0), (29, 0)])

    def test_cleanup_deletes_only_registered_unchanged_new_file(self):
        target = self.root / 'new.png'
        config = self.config(kind='folder', trigger='script', script=sys.executable,
                             delete_after_scan=True)
        with patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: self.image('new.png')):
            _, metadata = self.capture.capture_screenshot(config)
        self.assertTrue(self.capture.cleanup_screenshot(config, metadata))
        self.assertFalse(target.exists())
        self.assertFalse(self.capture.cleanup_screenshot(config, metadata))

    def test_capture_registration_is_the_record_cleanup_consumes(self):
        cleanup = self.capture.cleanup_screenshot
        records = sys.modules[cleanup.__module__]._CLEANUP_RECORDS
        self.assertIs(cleanup.__globals__['_CLEANUP_RECORDS'], records)
        self.assertIs(
            self.cleanup._register_cleanup.__globals__['_CLEANUP_RECORDS'], records)
        self.assertIs(getattr(self.capture, '_CLEANUP_RECORDS', records), records)
        config = self.config(kind='folder', trigger='script', script=sys.executable,
                             delete_after_scan=True)
        with patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: self.image('new.png')):
            _, metadata = self.capture.capture_screenshot(config)
        token = metadata['cleanup_token']
        self.assertEqual(records[token]['path'], str(self.root / 'new.png'))
        self.assertTrue(cleanup(config, metadata))
        self.assertNotIn(token, records)
        self.assertNotIn('cleanup_token', metadata)
        self.assertFalse((self.root / 'new.png').exists())

    def test_no_trigger_and_delete_disabled_publish_no_cleanup_receipt(self):
        target = self.image()
        _, reused = self.capture.capture_screenshot(self.config(
            kind='file', path=str(target), delete_after_scan=True))
        self.assertNotIn('cleanup_token', reused)
        self.assertIn('without a trigger', reused['cleanup_skipped'])
        self.assertFalse(self.capture.cleanup_screenshot(
            self.config(kind='file', path=str(target), delete_after_scan=True),
            reused))

        target.unlink()
        config = self.config(kind='folder', trigger='script', script=sys.executable,
                             delete_after_scan=False)
        records = len(self.cleanup._CLEANUP_RECORDS)
        with patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: self.image('new.png')):
            _, created = self.capture.capture_screenshot(config)
        self.assertNotIn('cleanup_token', created)
        self.assertEqual(len(self.cleanup._CLEANUP_RECORDS), records)

    def test_cleanup_preserves_changed_replaced_and_steam_managed_files(self):
        config = self.config(kind='folder', trigger='script', script=sys.executable,
                             delete_after_scan=True)
        with patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: self.image('new.png')):
            _, changed = self.capture.capture_screenshot(config)
        self.image('new.png', 'blue')
        self.assertFalse(self.capture.cleanup_screenshot(config, changed))
        self.assertTrue((self.root / 'new.png').exists())

        (self.root / 'new.png').unlink()
        with patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: self.image('new.png')):
            _, replaced = self.capture.capture_screenshot(config)
        replacement = self.image('replacement.png')
        (self.root / 'new.png').unlink()
        (self.root / 'new.png').symlink_to(replacement)
        self.assertFalse(self.capture.cleanup_screenshot(config, replaced))
        self.assertTrue((self.root / 'new.png').is_symlink())

    def test_cleanup_quarantines_then_preserves_a_racing_replacement(self):
        target = self.root / 'new.png'
        config = self.config(kind='folder', trigger='script', script=sys.executable,
                             delete_after_scan=True)
        with patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: self.image('new.png')):
            _, metadata = self.capture.capture_screenshot(config)
        original_rename = os.rename

        def replace_before_claim(source, destination, **kwargs):
            target.unlink()
            Image.new('RGB', (12, 8), 'blue').save(target)
            return original_rename(source, destination, **kwargs)

        with patch.object(self.cleanup.os, 'rename',
                          side_effect=replace_before_claim):
            self.assertFalse(self.capture.cleanup_screenshot(config, metadata))
        self.assertEqual(Image.open(target).getpixel((0, 0)), (0, 0, 255))

    def test_cleanup_cancellation_after_claim_restores_file(self):
        target = self.root / 'new.png'
        config = self.config(kind='folder', trigger='script', script=sys.executable,
                             delete_after_scan=True)
        with patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: self.image('new.png')):
            _, metadata = self.capture.capture_screenshot(config)
        cancelled = threading.Event()
        original = self.cleanup._fingerprint_descriptor

        def cancel_after_claim(*args, **kwargs):
            cancelled.set()
            return original(*args, **kwargs)

        with patch.object(self.cleanup, '_fingerprint_descriptor',
                          side_effect=cancel_after_claim), \
             self.assertRaises(__import__('concurrent.futures').futures.CancelledError):
            self.capture.cleanup_screenshot(
                config, metadata, cancel_event=cancelled)
        self.assertTrue(target.exists())

    def test_cleanup_rejects_replaced_parent_directory(self):
        directory = self.root / 'shots'
        directory.mkdir()
        config = self.config(kind='folder', path=str(directory), trigger='script',
                             script=sys.executable, delete_after_scan=True)

        def create(*_args, **_kwargs):
            Image.new('RGB', (12, 8), 'red').save(directory / 'new.png')

        with patch.object(self.capture, '_run_script', side_effect=create):
            _, metadata = self.capture.capture_screenshot(config)
        moved = self.root / 'old-shots'
        directory.rename(moved)
        directory.mkdir()
        Image.new('RGB', (12, 8), 'blue').save(directory / 'new.png')
        self.assertFalse(self.capture.cleanup_screenshot(config, metadata))
        self.assertTrue((moved / 'new.png').exists())
        self.assertEqual(Image.open(directory / 'new.png').getpixel((0, 0)),
                         (0, 0, 255))

    def test_steam_cleanup_deletes_new_main_and_same_basename_thumbnail_only(self):
        config = self.steam_config()
        other_account = self.root / 'other-account-screenshots'
        (other_account / 'thumbnails').mkdir(parents=True)
        unrelated_main = self.image('old.jpg')
        unrelated_thumb = self.image('thumbnails/old.jpg')

        def trigger(*_args, **_kwargs):
            self.image('new.jpg', 'blue')
            self.image('thumbnails/new.jpg', 'green')

        with patch.object(self.source, '_steam_directories',
                          return_value=[self.root, other_account]), \
             patch.object(self.capture, '_run_script', side_effect=trigger):
            _, metadata = self.capture.capture_screenshot(config)
            self.assertTrue(self.capture.cleanup_screenshot(config, metadata))
        self.assertFalse((self.root / 'new.jpg').exists())
        self.assertFalse((self.root / 'thumbnails/new.jpg').exists())
        self.assertTrue(unrelated_main.exists())
        self.assertTrue(unrelated_thumb.exists())

    def test_steam_cleanup_waits_for_late_thumbnail(self):
        config = self.steam_config()
        with patch.object(self.source, '_steam_directories', return_value=[self.root]), \
             patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: self.image('new.jpg')):
            _, metadata = self.capture.capture_screenshot(config)
            timer = threading.Timer(
                .02, lambda: self.image('thumbnails/new.jpg', 'green'))
            timer.start()
            self.addCleanup(timer.join)
            with patch.object(self.cleanup, 'THUMBNAIL_WAIT_SECONDS', .2):
                self.assertTrue(self.capture.cleanup_screenshot(config, metadata))
        self.assertFalse((self.root / 'new.jpg').exists())
        self.assertFalse((self.root / 'thumbnails/new.jpg').exists())

    def test_steam_missing_thumbnail_retains_main_with_reason(self):
        config = self.steam_config()
        with patch.object(self.source, '_steam_directories', return_value=[self.root]), \
             patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: self.image('new.jpg')):
            _, metadata = self.capture.capture_screenshot(config)
            with patch.object(self.cleanup, 'THUMBNAIL_WAIT_SECONDS', .03):
                self.assertFalse(self.capture.cleanup_screenshot(config, metadata))
        self.assertTrue((self.root / 'new.jpg').exists())
        self.assertIn('thumbnail', metadata['cleanup_skipped'].lower())

    def test_steam_preexisting_same_name_thumbnail_disables_cleanup(self):
        config = self.steam_config()
        old = self.image('thumbnails/new.jpg')
        with patch.object(self.source, '_steam_directories', return_value=[self.root]), \
             patch.object(self.capture, '_run_script',
                          side_effect=lambda *_a, **_k: self.image('new.jpg')):
            _, metadata = self.capture.capture_screenshot(config)
            self.assertFalse(self.capture.cleanup_screenshot(config, metadata))
        self.assertNotIn('cleanup_token', metadata)
        self.assertTrue((self.root / 'new.jpg').exists())
        self.assertTrue(old.exists())

    def test_steam_overwrite_and_new_thumbnail_are_never_deleted(self):
        main = self.image('new.jpg')
        config = self.steam_config(kind='file', path=str(main))

        def trigger(*_args, **_kwargs):
            self.image('new.jpg', 'blue')
            self.image('thumbnails/new.jpg', 'green')

        with patch.object(self.source, '_steam_directories', return_value=[self.root]), \
             patch.object(self.capture, '_run_script', side_effect=trigger):
            _, metadata = self.capture.capture_screenshot(config)
            self.assertFalse(self.capture.cleanup_screenshot(config, metadata))
        self.assertTrue(main.exists())
        self.assertTrue((self.root / 'thumbnails/new.jpg').exists())

    def test_steam_symlink_thumbnail_never_enables_cleanup(self):
        config = self.steam_config()
        target = self.image('outside.jpg')

        def trigger(*_args, **_kwargs):
            self.image('new.jpg')
            (self.root / 'thumbnails/new.jpg').symlink_to(target)

        with patch.object(self.source, '_steam_directories', return_value=[self.root]), \
             patch.object(self.capture, '_run_script', side_effect=trigger):
            _, metadata = self.capture.capture_screenshot(config)
            with patch.object(self.cleanup, 'THUMBNAIL_WAIT_SECONDS', .03):
                self.assertFalse(self.capture.cleanup_screenshot(config, metadata))
        self.assertTrue((self.root / 'new.jpg').exists())
        self.assertTrue(target.exists())

    def test_steam_thumbnail_replacement_before_claim_retains_both(self):
        config = self.steam_config()

        def trigger(*_args, **_kwargs):
            self.image('new.jpg')
            self.image('thumbnails/new.jpg', 'green')

        with patch.object(self.source, '_steam_directories', return_value=[self.root]), \
             patch.object(self.capture, '_run_script', side_effect=trigger):
            _, metadata = self.capture.capture_screenshot(config)
            original = self.cleanup._wait_for_steam_thumbnail

            def replace_after_wait(*args, **kwargs):
                result = original(*args, **kwargs)
                self.image('replacement.jpg', 'blue').replace(
                    self.root / 'thumbnails/new.jpg')
                return result

            with patch.object(self.cleanup, '_wait_for_steam_thumbnail',
                              side_effect=replace_after_wait):
                self.assertFalse(self.capture.cleanup_screenshot(config, metadata))
        self.assertTrue((self.root / 'new.jpg').exists())
        pixel = Image.open(self.root / 'thumbnails/new.jpg').getpixel((0, 0))
        self.assertGreater(pixel[2], 240)
        self.assertLess(max(pixel[:2]), 10)

    def test_steam_cancellation_after_pair_claim_restores_both(self):
        config = self.steam_config()

        def trigger(*_args, **_kwargs):
            self.image('new.jpg')
            self.image('thumbnails/new.jpg')

        cancelled = threading.Event()
        with patch.object(self.source, '_steam_directories', return_value=[self.root]), \
             patch.object(self.capture, '_run_script', side_effect=trigger):
            _, metadata = self.capture.capture_screenshot(config)
            original = self.cleanup._fingerprint_descriptor
            calls = [0]

            def cancel_after_pair_claim(*args, **kwargs):
                calls[0] += 1
                if calls[0] == 1:
                    cancelled.set()
                return original(*args, **kwargs)

            with patch.object(self.cleanup, '_fingerprint_descriptor',
                              side_effect=cancel_after_pair_claim), \
                 self.assertRaises(__import__('concurrent.futures').futures.CancelledError):
                self.capture.cleanup_screenshot(
                    config, metadata, cancel_event=cancelled)
        self.assertTrue((self.root / 'new.jpg').exists())
        self.assertTrue((self.root / 'thumbnails/new.jpg').exists())


if __name__ == '__main__':
    unittest.main()
