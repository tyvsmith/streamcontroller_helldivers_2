import json
from pathlib import Path
from tempfile import TemporaryDirectory
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
            self.deck, str(self.source), 'HD2', 'steam', address or self.address)

    def test_layout_reserves_back_and_scan_then_numbered_slots(self):
        path = self.create()
        keys = json.loads(Path(path).read_text())['keys']
        actions = [key['states']['0']['actions'][0] for key in keys.values()]
        self.assertEqual(len(actions), 15)
        self.assertTrue(actions[0]['id'].endswith('::TemporaryScanBack'))
        self.assertTrue(actions[1]['id'].endswith('::ScanStratagems'))
        self.assertEqual(actions[1]['settings']['capture_backend'], 'steam')
        self.assertEqual(actions[1]['settings']['scan_mode'], 'update')
        self.assertEqual([a['settings']['slot'] for a in actions[2:]], list(range(1, 14)))
        self.assertEqual(self.deck.active_page.json_path, str(self.source))

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

    def test_unreadable_cache_is_reported_instead_of_treated_as_missing(self):
        self.create()
        with patch.object(Path, 'read_text', side_effect=PermissionError('unreadable')):
            with self.assertRaises(PermissionError):
                self.pages.find(self.deck, str(self.source), 'HD2', self.address)

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
