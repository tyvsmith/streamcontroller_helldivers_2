import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from automatic_stratagems.temporary_scan_page import TemporaryScanPages


class PageManager:
    def __init__(self):
        self.custom_pages = []
        self.pages = {}

    def register_page(self, path):
        self.custom_pages.append(path)

    def unregister_page(self, path):
        self.custom_pages.remove(path)

    def get_page(self, path, deck_controller):
        if not Path(path).is_file():
            return None
        page = SimpleNamespace(json_path=path, clear_action_objects=lambda: None)
        self.pages.setdefault(deck_controller, {})[path] = {'page': page}
        return page

    def remove_page(self, path):
        Path(path).unlink()


class Deck:
    deck = SimpleNamespace(key_layout=lambda: (3, 5))
    def __init__(self, source):
        self.active_page = SimpleNamespace(json_path=str(source))

    def serial_number(self):
        return 'deck-one'

    def load_page(self, page):
        self.active_page = page


class TemporaryPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'original.json'
        self.source.write_text('{"keys": {}}')
        self.deck = Deck(self.source)
        self.manager = PageManager()
        self.pages = TemporaryScanPages(self.root / 'temporary-pages', self.manager)
        self.address = dict(input_type='keys', identifier='4x0', state=0, index=0)

    def create(self, address=None):
        return self.pages.create(
            self.deck, str(self.source), 'HD2', 'gamescope', address or self.address)

    def test_layout_reserves_back_and_scan_then_numbered_slots(self):
        path = self.create()
        keys = json.loads(Path(path).read_text())['keys']
        actions = [key['states']['0']['actions'][0] for key in keys.values()]
        self.assertEqual(len(actions), 15)
        self.assertTrue(actions[0]['id'].endswith('::TemporaryScanBack'))
        self.assertTrue(actions[1]['id'].endswith('::ScanStratagems'))
        self.assertEqual(actions[1]['settings']['capture_backend'], 'gamescope')
        self.assertEqual(actions[1]['settings']['scan_mode'], 'update')
        self.assertEqual([a['settings']['slot'] for a in actions[2:]], list(range(1, 14)))
        self.assertEqual(self.deck.active_page.json_path, str(self.source))

    def test_generated_scan_button_omits_legacy_image_source_history(self):
        image_settings = {
            'screenshot_trigger': 'script',
            'screenshot_hotkey': 'KEY_F11',
            'screenshot_script': '/tmp/save-screenshot',
            'screenshot_folder': '/captures',
            'screenshot_delete': True,
            'image_source_last_fingerprint': 'a' * 64,
            'image_source_last_path': '/captures/latest.png',
            'image_source_last_mtime_ns': 123,
        }
        path = self.pages.create(
            self.deck, str(self.source), 'HD2', 'auto', self.address,
            image_settings=image_settings)
        data = json.loads(Path(path).read_text())
        scan = list(data['keys'].values())[1]['states']['0']['actions'][0]['settings']
        for key, value in image_settings.items():
            if key.startswith('image_source_last_'):
                self.assertNotIn(key, scan)
            else:
                self.assertEqual(scan[key], value)
        self.assertEqual(scan['capture_backend'], 'auto')
        self.assertEqual(data['hd2_temporary_scan']['image_source'], {
            'backend': 'auto',
            'source': {'kind': 'folder', 'path': '/captures',
                       'trigger': 'script', 'hotkey': 'KEY_F11',
                       'script': '/tmp/save-screenshot',
                       'delete_after_scan': True}})

    def test_version_four_tolerates_legacy_image_source_history_extras(self):
        path = self.pages.create(
            self.deck, str(self.source), 'HD2', 'screenshot', self.address,
            image_settings={'screenshot_folder': '/captures'})
        data = json.loads(Path(path).read_text())
        scan = list(data['keys'].values())[1]['states']['0']['actions'][0]
        scan['settings'].update(
            image_source_last_fingerprint='a' * 64,
            image_source_last_path='/captures/frame.png',
            image_source_last_mtime_ns=1)
        Path(path).write_text(json.dumps(data))

        settings = self.pages.scan_settings(path, bind_image_source=True)

        self.assertEqual(settings['image_source_last_fingerprint'], 'a' * 64)
    def test_generated_page_accepts_unified_path_source_binding(self):
        path = self.pages.create(
            self.deck, str(self.source), 'HD2', 'screenshot', self.address,
            image_settings={'screenshot_folder': '/captures'})
        metadata = self.pages.metadata(path)
        settings = self.pages.scan_settings(path, bind_image_source=True)
        self.assertEqual(metadata['image_source'], {
            'backend': 'screenshot',
            'source': {'kind': 'folder', 'path': '/captures',
                       'trigger': 'hotkey', 'hotkey': 'KEY_F12',
                       'script': '', 'delete_after_scan': True}})
        self.assertEqual(settings['screenshot_folder'], '/captures')

    def test_version_four_binds_backend_separately_from_public_source(self):
        auto = self.pages.create(
            self.deck, str(self.source), 'HD2', 'auto', self.address,
            image_settings={'screenshot_folder': '/captures'})
        screenshot = self.pages.create(
            self.deck, str(self.source), 'other', 'screenshot', self.address,
            image_settings={'screenshot_folder': '/captures'})
        gamescope = self.pages.create(
            self.deck, str(self.source), 'third', 'gamescope', self.address,
            image_settings={'screenshot_folder': '/captures'})

        self.assertNotEqual(
            self.pages.metadata(auto)['image_source'],
            self.pages.metadata(screenshot)['image_source'])
        self.assertEqual(self.pages.metadata(gamescope)['image_source'], {
            'backend': 'gamescope', 'source': None})

    def test_version_four_rejects_corrupt_canonical_global_snapshot(self):
        path = self.pages.create(
            self.deck, str(self.source), 'HD2', 'screenshot', self.address)
        data = json.loads(Path(path).read_text())
        scan = list(data['keys'].values())[1]['states']['0']['actions'][0]
        scan['settings']['screenshot_trigger'] = 'none'
        Path(path).write_text(json.dumps(data))

        with self.assertRaisesRegex(ValueError, 'screenshot source'):
            self.pages.scan_settings(path, bind_image_source=True)

    def test_global_screenshot_invalidation_preserves_gamescope_cache(self):
        screenshot = self.pages.create(
            self.deck, str(self.source), 'HD2', 'auto', self.address)
        gamescope = self.pages.create(
            self.deck, str(self.source), 'other', 'gamescope', self.address)

        removed = self.pages.invalidate_screenshot_sources()

        self.assertEqual(removed, {screenshot})
        self.assertFalse(Path(screenshot).exists())
        self.assertTrue(Path(gamescope).exists())
    def test_legacy_page_source_is_normalized_when_binding(self):
        path = self.pages.create(
            self.deck, str(self.source), 'HD2', 'steam', self.address,
            image_settings={
                'image_source_kind': 'file',
                'image_source_path': '/captures/frame.png'})
        data = json.loads(Path(path).read_text())
        data['hd2_temporary_scan']['version'] = 2
        data['hd2_temporary_scan']['image_source'] = {
            'kind': 'file', 'path': '/captures/frame.png'}
        scan = list(data['keys'].values())[1]['states']['0']['actions'][0]
        for key in ('screenshot_trigger', 'screenshot_hotkey',
                    'screenshot_script', 'screenshot_delete'):
            scan['settings'].pop(key, None)
        Path(path).write_text(json.dumps(data))

        settings = self.pages.scan_settings(path, bind_image_source=True)

        self.assertEqual(settings['image_source_kind'], 'file')

    def test_version_three_binds_all_screenshot_source_settings(self):
        path = self.pages.create(
            self.deck, str(self.source), 'HD2', 'screenshot', self.address,
            image_settings={
                'image_source_kind': 'path',
                'image_source_path': '/captures',
                'screenshot_trigger': 'script',
                'screenshot_script': '/tmp/save-screenshot',
                'screenshot_delete': True,
            })
        data = json.loads(Path(path).read_text())
        scan = list(data['keys'].values())[1]['states']['0']['actions'][0]
        scan['settings']['screenshot_script'] = '/tmp/other-script'
        Path(path).write_text(json.dumps(data))

        with self.assertRaisesRegex(ValueError, 'source changed'):
            self.pages.scan_settings(path, bind_image_source=True)

    def test_version_three_rejects_delete_with_no_trigger(self):
        path = self.pages.create(
            self.deck, str(self.source), 'HD2', 'screenshot', self.address)
        data = json.loads(Path(path).read_text())
        data['hd2_temporary_scan']['version'] = 3
        data['hd2_temporary_scan']['image_source'] = {
            'kind': 'path', 'path': '', 'trigger': 'none',
            'hotkey': 'KEY_F12', 'script': '', 'delete_after_scan': True}
        Path(path).write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'image source'):
            self.pages.metadata(path)
    def test_incomplete_script_setting_remains_editable_and_bound(self):
        path = self.pages.create(
            self.deck, str(self.source), 'HD2', 'screenshot', self.address,
            image_settings={'screenshot_trigger': 'script',
                            'screenshot_script': ''})
        settings = self.pages.scan_settings(path, bind_image_source=True)
        self.assertEqual(settings['screenshot_trigger'], 'script')
        self.assertEqual(settings['screenshot_script'], '')
    def test_back_switches_to_source_and_retains_cached_page(self):
        path = self.create()
        self.pages.show(self.deck, path)
        self.assertEqual(self.deck.active_page.json_path, path)
        self.pages.back(self.deck, path)
        self.assertEqual(self.deck.active_page.json_path, str(self.source))
        self.assertTrue(Path(path).exists())
        self.assertIn(path, self.manager.custom_pages)
        self.assertIn(path, self.manager.pages[self.deck])
        self.assertTrue(self.source.exists())

    def test_find_returns_only_matching_deck_source_and_group_cache(self):
        path = self.create()
        self.assertEqual(self.pages.find(self.deck, str(self.source), 'HD2', self.address), path)
        self.assertIsNone(self.pages.find(self.deck, str(self.source), 'other', self.address))
        other_source = self.root / 'other.json'
        other_source.write_text('{"keys": {}}')
        self.assertIsNone(self.pages.find(self.deck, str(other_source), 'HD2', self.address))

    def test_direct_metadata_reports_unreadable_cache(self):
        path = self.create()
        with patch('automatic_stratagems.temporary_scan_page.read_bounded_json',
                   side_effect=PermissionError('unreadable')):
            with self.assertRaises(PermissionError):
                self.pages.metadata(path)

    def test_missing_original_keeps_temporary_page(self):
        path = self.create()
        self.pages.show(self.deck, path)
        self.source.unlink()
        with self.assertRaises(ValueError):
            self.pages.back(self.deck, path)
        self.assertTrue(Path(path).exists())
        self.assertEqual(self.deck.active_page.json_path, path)

    def test_restart_registers_owned_page_and_back_still_works(self):
        path = self.create()
        manager = PageManager()
        pages = TemporaryScanPages(self.pages.directory, manager)
        self.assertIn(path, manager.custom_pages)
        pages.show(self.deck, path)
        pages.back(self.deck, path)
        self.assertTrue(Path(path).exists())
        self.assertEqual(pages.find(self.deck, str(self.source), 'HD2', self.address), path)

    def test_same_source_group_uses_exact_source_action_address(self):
        other = dict(input_type='keys', identifier='3x1', state=0, index=0)
        first = self.create()
        second = self.create(other)

        self.assertEqual(self.pages.find(
            self.deck, str(self.source), 'HD2', self.address), first)
        self.assertEqual(self.pages.find(
            self.deck, str(self.source), 'HD2', other), second)

        restarted = TemporaryScanPages(self.pages.directory, PageManager())
        self.assertEqual(restarted.find(
            self.deck, str(self.source), 'HD2', self.address), first)
        self.assertEqual(restarted.find(
            self.deck, str(self.source), 'HD2', other), second)

    def test_ownerless_v1_cache_is_retained_but_never_bound(self):
        path = self.create()
        data = json.loads(Path(path).read_text())
        data['hd2_temporary_scan']['version'] = 1
        data['hd2_temporary_scan'].pop('source_action')
        Path(path).write_text(json.dumps(data))

        restarted = TemporaryScanPages(self.pages.directory, PageManager())

        self.assertTrue(Path(path).exists())
        self.assertIsNone(restarted.find(
            self.deck, str(self.source), 'HD2', self.address))

    def test_malformed_v2_source_action_is_rejected(self):
        path = self.create()
        data = json.loads(Path(path).read_text())
        data['hd2_temporary_scan']['source_action']['index'] = -1
        Path(path).write_text(json.dumps(data))

        with self.assertRaises(ValueError):
            self.pages.metadata(path)

    def test_oversized_owned_page_is_rejected_and_preserved(self):
        path = Path(self.create())
        data = json.loads(path.read_text())
        data['padding'] = 'x' * (1024 * 1024)
        path.write_text(json.dumps(data))
        before = path.read_bytes()

        with self.assertRaisesRegex(ValueError, 'too large'):
            self.pages.metadata(path)

        self.assertEqual(path.read_bytes(), before)

    def test_deep_owned_page_is_rejected_and_preserved(self):
        path = Path(self.create())
        data = json.loads(path.read_text())
        nested = {}
        for _ in range(500):
            nested = {'next': nested}
        data['extra'] = nested
        path.write_text(json.dumps(data))

        with self.assertRaisesRegex(ValueError, 'nested'):
            self.pages.metadata(path)
        self.assertTrue(path.exists())

    def test_owned_page_symlink_is_rejected_without_touching_target(self):
        target = Path(self.create())
        alias = target.with_name('alias.json')
        before = target.read_bytes()
        alias.symlink_to(target)

        with self.assertRaises(ValueError):
            self.pages.metadata(alias)

        self.assertEqual(target.read_bytes(), before)

    def test_owned_page_fifo_is_rejected_without_blocking(self):
        path = self.pages.directory / 'pipe.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)
        done = Event()
        outcome = []

        def metadata():
            try:
                self.pages.metadata(path)
            except Exception as error:
                outcome.append(error)
            finally:
                done.set()

        thread = Thread(target=metadata, daemon=True)
        thread.start()
        completed_without_writer = done.wait(.2)
        if not completed_without_writer:
            writer = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            os.write(writer, b'{}')
            os.close(writer)
            done.wait(1)
        thread.join(1)

        self.assertTrue(completed_without_writer, 'metadata blocked opening a FIFO')
        self.assertIsInstance(outcome[0], ValueError)

    def test_find_skips_unreadable_entry_without_deleting_it(self):
        unreadable = self.pages.directory / '000-unreadable.json'
        unreadable.parent.mkdir(parents=True, exist_ok=True)
        unreadable.write_text('{}')
        unreadable.chmod(0)
        try:
            try:
                found = self.pages.find(
                    self.deck, str(self.source), 'HD2', self.address)
            except PermissionError:
                self.fail('one unreadable entry aborted cache discovery')
            self.assertIsNone(found)
            self.assertTrue(unreadable.exists())
        finally:
            unreadable.chmod(0o600)

    def test_cannot_delete_arbitrary_page(self):
        with self.assertRaises(ValueError):
            self.pages.back(self.deck, str(self.source))
        self.assertTrue(self.source.exists())

    def test_layout_adapts_to_small_deck(self):
        self.deck.deck = SimpleNamespace(key_layout=lambda: (2, 3))
        data = json.loads(Path(self.create()).read_text())
        self.assertEqual(len(data['keys']), 6)
        self.assertIn('2x1', data['keys'])

    def test_no_navigation_means_no_delete(self):
        path = self.create()
        self.pages.show(self.deck, path)
        self.deck.load_page = lambda page: None
        with self.assertRaises(RuntimeError):
            self.pages.back(self.deck, path)
        self.assertTrue(Path(path).exists())

    def test_unregister_failure_keeps_owned_file_for_retry(self):
        path = self.create()
        original = self.manager.unregister_page
        self.manager.unregister_page = Mock(side_effect=RuntimeError('registry busy'))
        with self.assertRaisesRegex(RuntimeError, 'registry busy'):
            self.pages.discard(path)
        self.assertTrue(Path(path).exists())
        self.manager.unregister_page = original
        self.pages.discard(path)
        self.assertFalse(Path(path).exists())

    def test_cache_clear_failure_keeps_entry_and_file_for_retry(self):
        path = self.create()
        page = Mock()
        page.clear_action_objects.side_effect = RuntimeError('cache busy')
        self.manager.pages[self.deck] = {path: {'page': page}}
        with self.assertRaisesRegex(RuntimeError, 'cache busy'):
            self.pages.discard(path)
        self.assertIn(path, self.manager.pages[self.deck])
        self.assertTrue(Path(path).exists())
        page.clear_action_objects.side_effect = None
        self.pages.discard(path)
        self.assertNotIn(path, self.manager.pages[self.deck])
        self.assertFalse(Path(path).exists())

    def test_cleanup_does_not_call_host_delete_before_adapter_steps(self):
        path = self.create()
        self.manager.remove_page = Mock(side_effect=AssertionError('host deleted early'))
        self.pages.discard(path)
        self.manager.remove_page.assert_not_called()
        self.assertFalse(Path(path).exists())

    def test_active_page_on_any_deck_blocks_cleanup(self):
        path = self.create()
        other = Deck(self.source)
        other.active_page = SimpleNamespace(json_path=path)
        self.manager.pages[other] = {}
        with self.assertRaisesRegex(RuntimeError, 'still active'):
            self.pages.discard(path)
        self.assertTrue(Path(path).exists())

    def test_plugin_owner_tracks_registration_and_release(self):
        owner = SimpleNamespace(registered_pages=[], register_page=Mock())
        pages = TemporaryScanPages(self.root / 'owned-pages', self.manager, owner=owner)
        path = pages.create(
            self.deck, str(self.source), 'HD2', 'steam', self.address)
        owner.register_page.assert_called_once_with(path)
        owner.registered_pages.append(path)
        pages.discard(path)
        self.assertNotIn(path, owner.registered_pages)

    def test_cleanup_returns_from_active_owned_page_before_deletion(self):
        path = self.create()
        self.pages.show(self.deck, path)
        self.pages.cleanup()
        self.assertEqual(self.deck.active_page.json_path, str(self.source))
        self.assertFalse(Path(path).exists())

    def test_cleanup_preserves_active_page_when_source_is_missing(self):
        path = self.create()
        self.pages.show(self.deck, path)
        self.source.unlink()
        with self.assertLogs('automatic_stratagems.temporary_scan_page', level='WARNING'):
            self.pages.cleanup()
        self.assertEqual(self.deck.active_page.json_path, path)
        self.assertTrue(Path(path).exists())

    def test_unregister_all_releases_registration_without_deleting_page(self):
        path = self.create()
        self.pages.unregister_all()
        self.assertTrue(Path(path).exists())
        self.assertNotIn(path, self.manager.custom_pages)

    def test_unregister_all_continues_after_unexpected_sdk_failure(self):
        self.create()
        self.create()
        self.pages._unregister = Mock(side_effect=[TypeError('SDK changed'), None])

        with self.assertLogs('automatic_stratagems.temporary_scan_page',
                             level='WARNING'):
            self.pages.unregister_all()

        self.assertEqual(self.pages._unregister.call_count, 2)
