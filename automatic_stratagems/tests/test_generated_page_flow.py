import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, call, patch

from .package_loader import plugin_module

SOURCE = '/pages/source.json'
CACHED = '/pages/cached.json'
NEW = '/pages/new.json'


class Deck:
    """Identity-hashed like StreamController's DeckController."""

    def __init__(self):
        self.active_page = types.SimpleNamespace(json_path=SOURCE)

    def serial_number(self):
        return 'deck-one'


class Host:
    """The coordinator surface generated-page navigation reaches through."""

    def __init__(self, mod, pages=None):
        self.plugin = types.SimpleNamespace(
            stratagems={'A': ['UP']}, get_settings=lambda: {})
        self.sessions = {}
        self.state_errors = {}
        self.active_scans = {}
        self.closed = False
        self.enabled = True
        self.store = types.SimpleNamespace(directory=Path('/state/scan-state'))
        self.write_state = Mock()
        self.cancel_context = Mock()
        self.redraw = Mock()
        self.flow = mod.GeneratedPageFlow(self)
        self.flow.temporary_pages = pages

    def context(self, action):
        return action.context

    def session_for(self, context):
        return self.sessions.setdefault(context, 'restored')

    def cached_page(self, action):
        return self.flow.cached_page(action)

    def open_temporary(self, action, session, *, replace_path=None):
        return self.flow.open_temporary(action, session, replace_path=replace_path)

    def _cached_image_source_matches(self, action, path):
        return self.flow._cached_image_source_matches(action, path)

    def _restore_cached_session(self, source, source_action):
        return self.flow._restore_cached_session(source, source_action)


class GeneratedPageFlowTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(sys.modules, {
                'loguru': types.SimpleNamespace(logger=Mock())}):
            cls.mod = importlib.import_module(
                plugin_module('automatic_stratagems.generated_page_flow'))

    def setUp(self):
        self.deck = Deck()
        self.pages = Mock()
        self.pages.layout.return_value = (3, 5)
        self.pages.find.return_value = None
        self.pages.create.return_value = NEW
        self.host = Host(self.mod, self.pages)
        address = patch.object(self.mod, 'source_action_address', return_value='address')
        address.start()
        self.addCleanup(address.stop)

    def action(self, settings=None, page=SOURCE):
        return types.SimpleNamespace(
            context=(self.deck, page, 'HD2'), deck_controller=self.deck,
            page=types.SimpleNamespace(json_path=page), render=Mock(),
            get_is_present=lambda: True,
            get_settings=lambda: dict(settings or {'capture_backend': 'gamescope'}),
            plugin_base=types.SimpleNamespace(get_settings=lambda: {}))


class SourceSettingTests(GeneratedPageFlowTestCase):
    def test_action_settings_combine_with_the_plugin_screenshot_settings(self):
        action = self.action({'capture_backend': 'screenshot'})
        self.assertEqual(self.mod.capture_backend(action), 'screenshot')
        expected = self.mod.source_settings({'capture_backend': 'screenshot'}, {})
        self.assertEqual(self.mod.action_source_settings(action), expected)
        self.assertEqual(self.mod.action_source_settings(action, {'capture_backend': 'auto'}),
                         self.mod.source_settings({'capture_backend': 'auto'}, {}))
        self.assertEqual(self.mod.image_page_settings(action),
                         self.mod.page_source_settings(expected))

    def test_ordinary_actions_use_their_own_capture_settings(self):
        self.pages.owns_location.return_value = False
        action = self.action({'capture_backend': 'auto'})
        self.assertEqual(self.host.flow._operation_image_settings(action),
                         self.mod.action_source_settings(action))

    def test_generated_actions_use_their_stored_capture_settings(self):
        self.pages.owns_location.return_value = True
        stored = self.mod.source_settings({'capture_backend': 'gamescope'}, {})
        self.pages.scan_settings.return_value = stored
        action = self.action({'capture_backend': 'screenshot'}, page=CACHED)
        self.assertEqual(self.host.flow._operation_image_settings(action),
                         self.mod.action_source_settings(action, stored))
        self.pages.scan_settings.assert_called_once_with(CACHED, bind_image_source=True)

    def test_generated_actions_reject_changed_global_screenshot_settings(self):
        self.pages.owns_location.return_value = True
        self.pages.scan_settings.return_value = {'capture_backend': 'screenshot'}
        identities = iter(['stored', 'current'])
        with patch.object(self.mod, 'source_identity', side_effect=lambda _: next(identities)):
            with self.assertRaisesRegex(ValueError, 'Global screenshot settings changed'):
                self.host.flow._operation_image_settings(self.action(page=CACHED))

    def test_a_cached_page_matches_only_the_same_capture_identity(self):
        action = self.action()
        self.pages.scan_settings.return_value = self.mod.action_source_settings(action)
        self.assertTrue(self.host.flow._cached_image_source_matches(action, CACHED))
        self.pages.scan_settings.assert_called_once_with(CACHED, bind_image_source=True)
        self.pages.scan_settings.return_value = self.mod.action_source_settings(
            self.action({'capture_backend': 'screenshot'}))
        self.assertFalse(self.host.flow._cached_image_source_matches(action, CACHED))


class PageOwnershipTests(GeneratedPageFlowTestCase):
    def test_pages_are_enabled_beside_the_scan_state_directory(self):
        manager = object()
        with patch.object(self.mod, 'TemporaryScanPages') as pages:
            self.host.flow.enable_temporary_pages(manager)
        pages.assert_called_once_with(
            Path('/state/temporary-pages'), manager, self.host.store,
            owner=self.host.plugin)
        self.assertIs(self.host.flow.temporary_pages, pages.return_value)

    def test_only_owned_page_locations_are_temporary(self):
        action = self.action(page=CACHED)
        self.pages.owns_location.return_value = True
        self.assertTrue(self.host.flow.is_temporary_action(action))
        for error in (AttributeError('page'), OSError('unreadable'), ValueError('invalid')):
            with self.subTest(error=error):
                self.pages.owns_location.side_effect = error
                self.assertFalse(self.host.flow.is_temporary_action(action))
        self.host.flow.temporary_pages = None
        self.assertFalse(self.host.flow.is_temporary_action(action))


class CachedPageTests(GeneratedPageFlowTestCase):
    def test_no_cached_page_without_temporary_pages(self):
        self.host.flow.temporary_pages = None
        self.assertIsNone(self.host.flow.cached_page(self.action()))
        self.assertIsNone(self.host.flow._restore_cached_session(
            (self.deck, SOURCE, 'HD2'), 'address'))

    def test_a_matching_cached_page_is_found_and_its_session_restored(self):
        action = self.action()
        self.pages.find.return_value = CACHED
        self.pages.scan_settings.return_value = self.mod.action_source_settings(action)
        self.assertEqual(self.host.flow.cached_page(action), CACHED)
        self.pages.find.assert_called_once_with(self.deck, SOURCE, 'HD2', 'address')
        self.assertIn((self.deck, CACHED, 'HD2'), self.host.sessions)

    def test_a_cached_page_for_other_capture_settings_is_not_reused(self):
        action = self.action()
        self.pages.find.return_value = CACHED
        self.pages.scan_settings.return_value = self.mod.action_source_settings(
            self.action({'capture_backend': 'screenshot'}))
        self.assertIsNone(self.host.flow.cached_page(action))
        self.assertEqual(self.host.sessions, {})

    def test_restoring_a_cached_session_skips_the_capture_check(self):
        self.pages.find.return_value = CACHED
        self.assertEqual(self.host.flow._restore_cached_session(
            (self.deck, SOURCE, 'HD2'), 'address'), CACHED)
        self.pages.scan_settings.assert_not_called()
        self.assertIn((self.deck, CACHED, 'HD2'), self.host.sessions)

    def test_opening_a_cached_page_requires_an_enabled_present_action(self):
        action = self.action()
        self.host.cached_page = Mock(return_value=CACHED)
        for attribute, value in (('closed', True), ('enabled', False)):
            with self.subTest(attribute=attribute):
                setattr(self.host, attribute, value)
                self.assertFalse(self.host.flow.open_cached_page(action))
                setattr(self.host, attribute, not value)
        action.get_is_present = lambda: False
        self.assertFalse(self.host.flow.open_cached_page(action))
        self.host.cached_page.assert_not_called()

    def test_opening_a_cached_page_shows_it_through_the_coordinator_lookup(self):
        action = self.action()
        self.host.cached_page = Mock(return_value=None)
        self.assertFalse(self.host.flow.open_cached_page(action))
        self.host.cached_page.return_value = CACHED
        self.assertTrue(self.host.flow.open_cached_page(action))
        self.host.cached_page.assert_called_with(action)
        self.pages.show.assert_called_once_with(self.deck, CACHED)


class BackTests(GeneratedPageFlowTestCase):
    def test_back_returns_to_the_source_and_clears_the_error(self):
        action = self.action(page=CACHED)
        action.back_error = 'old'
        self.host.flow.back(action)
        self.pages.back.assert_called_once_with(self.deck, CACHED)
        self.assertIsNone(action.back_error)
        action.render.assert_not_called()

    def test_back_failure_is_logged_shown_and_kept_on_the_action(self):
        action = self.action(page=CACHED)
        self.pages.back.side_effect = RuntimeError('Source page missing')
        with patch.object(self.mod, 'log') as log:
            self.host.flow.back(action)
        log.exception.assert_called_once_with('Unable to leave temporary scan page')
        self.assertEqual(action.back_error, 'Source page missing')
        action.render.assert_called_once_with()

    def test_back_ignores_an_action_that_is_not_present(self):
        action = self.action(page=CACHED)
        action.get_is_present = lambda: False
        self.host.flow.back(action)
        self.pages.back.assert_not_called()


class OpenTemporaryTests(GeneratedPageFlowTestCase):
    def test_a_recognized_report_seeds_a_separate_page_session(self):
        action = self.action()
        self.host.open_temporary = Mock()
        report = {'status': 'matched', 'rows': [{'id': 'A'}]}
        snapshot = self.host.flow.page_result(action, report, {}, replace_path=CACHED)
        self.assertEqual(snapshot.assignments[1], 'A')
        self.assertEqual(len(snapshot.assignments), 13)
        session = self.host.open_temporary.call_args.args[1]
        self.assertIsInstance(session, self.mod.ScanSession)
        self.host.open_temporary.assert_called_once_with(action, session, replace_path=CACHED)

    def test_an_unrecognized_report_opens_no_page(self):
        self.host.open_temporary = Mock()
        snapshot = self.host.flow.page_result(
            self.action(), {'status': 'matched', 'rows': [{'id': None}]}, {})
        self.assertFalse(snapshot.recognized)
        self.host.open_temporary.assert_not_called()

    def test_a_matching_cached_page_is_reopened_without_replacing_its_session(self):
        action = self.action()
        self.pages.find.return_value = CACHED
        self.pages.scan_settings.return_value = self.mod.action_source_settings(action)
        self.host.flow.open_temporary(action, 'new session')
        self.pages.create.assert_not_called()
        self.pages.show.assert_called_once_with(self.deck, CACHED)
        self.assertEqual(self.host.sessions, {(self.deck, CACHED, 'HD2'): 'restored'})

    def test_a_new_page_gets_its_own_saved_session_and_is_shown(self):
        action = self.action({'capture_backend': 'auto'})
        self.host.flow.open_temporary(action, 'page session')
        self.pages.create.assert_called_once_with(
            self.deck, SOURCE, 'HD2', 'auto', 'address',
            image_settings=self.mod.image_page_settings(action))
        self.assertEqual(self.host.sessions, {(self.deck, NEW, 'HD2'): 'page session'})
        self.host.write_state.assert_called_once_with(
            dict(deck='deck-one', page=NEW, group='HD2'), 'page session')
        self.pages.show.assert_called_once_with(self.deck, NEW)
        self.pages.discard.assert_not_called()

    def test_a_stale_cached_page_is_replaced_after_the_new_page_is_shown(self):
        action = self.action()
        self.pages.find.return_value = CACHED
        self.pages.scan_settings.return_value = {'capture_backend': 'screenshot'}
        old = self.deck, CACHED, 'HD2'
        self.host.sessions[old] = 'old session'
        self.host.state_errors[old] = 'old error'
        manager = Mock()
        manager.attach_mock(self.pages.show, 'show')
        manager.attach_mock(self.pages.discard, 'discard')
        self.host.flow.open_temporary(action, 'page session')
        self.assertEqual(manager.mock_calls,
                         [call.show(self.deck, NEW), call.discard(CACHED)])
        self.assertEqual(self.host.sessions, {(self.deck, NEW, 'HD2'): 'page session'})
        self.assertEqual(self.host.state_errors, {})

    def test_replacement_requires_the_cached_page_it_was_planned_for(self):
        self.pages.find.return_value = CACHED
        with self.assertRaisesRegex(RuntimeError, 'Cached page changed during replacement'):
            self.host.flow.open_temporary(self.action(), 'page session', replace_path=NEW)
        self.pages.create.assert_not_called()

    def test_a_failed_new_page_is_removed_while_the_source_stays_active(self):
        self.host.write_state.side_effect = OSError('disk full')
        with self.assertRaisesRegex(OSError, 'disk full'):
            self.host.flow.open_temporary(self.action(), 'page session')
        self.assertEqual(self.host.sessions, {})
        self.pages.discard.assert_called_once_with(NEW)

    def test_failing_to_discard_the_old_page_returns_to_it_and_removes_the_new_one(self):
        action = self.action()
        self.pages.find.return_value = CACHED
        old = self.deck, CACHED, 'HD2'
        self.host.sessions[old] = 'old session'
        self.pages.discard.side_effect = [RuntimeError('discard failed'), None]
        with self.assertRaisesRegex(RuntimeError, 'discard failed'):
            self.host.flow.open_temporary(action, 'page session', replace_path=CACHED)
        self.assertEqual(self.pages.show.call_args_list,
                         [call(self.deck, NEW), call(self.deck, CACHED)])
        self.assertEqual(self.pages.discard.call_args_list, [call(CACHED), call(NEW)])
        self.assertEqual(self.host.sessions, {old: 'old session'})

    def test_a_shown_new_page_is_kept_when_a_later_step_fails(self):
        action = self.action()

        def show(deck, path):
            if path == CACHED:
                raise RuntimeError('reload failed')
            deck.active_page = types.SimpleNamespace(json_path=path)

        self.pages.show.side_effect = show
        self.pages.find.return_value = CACHED
        self.pages.discard.side_effect = RuntimeError('discard failed')
        with self.assertRaisesRegex(RuntimeError, 'reload failed'):
            self.host.flow.open_temporary(action, 'page session', replace_path=CACHED)
        self.assertIn((self.deck, NEW, 'HD2'), self.host.sessions)
        self.pages.discard.assert_called_once_with(CACHED)


class DeleteCachedPageTests(GeneratedPageFlowTestCase):
    def test_deletion_requires_an_enabled_present_action(self):
        action = self.action()
        for attribute, value in (('closed', True), ('enabled', False)):
            with self.subTest(attribute=attribute):
                setattr(self.host, attribute, value)
                self.assertFalse(self.host.flow.delete_cached_page(action))
                setattr(self.host, attribute, not value)
        action.get_is_present = lambda: False
        self.assertFalse(self.host.flow.delete_cached_page(action))
        self.pages.find.assert_not_called()

    def test_nothing_is_deleted_without_a_cached_page(self):
        self.assertFalse(self.host.flow.delete_cached_page(self.action()))
        self.host.cancel_context.assert_not_called()
        self.pages.discard.assert_not_called()

    def test_an_active_cached_page_returns_to_its_source_before_removal(self):
        action = self.action()
        source = self.deck, SOURCE, 'HD2'
        cached = self.deck, CACHED, 'HD2'
        self.pages.find.return_value = CACHED
        self.host.state_errors[cached] = 'old error'
        self.deck.active_page = types.SimpleNamespace(json_path=CACHED)
        manager = Mock()
        manager.attach_mock(self.pages.back, 'back')
        manager.attach_mock(self.pages.discard, 'discard')
        self.assertTrue(self.host.flow.delete_cached_page(action))
        self.host.cancel_context.assert_called_once_with(cached)
        self.assertEqual(manager.mock_calls,
                         [call.back(self.deck, CACHED), call.discard(CACHED)])
        self.assertEqual(self.host.sessions, {})
        self.assertEqual(self.host.state_errors, {})
        self.host.redraw.assert_called_once_with(source)

    def test_an_inactive_cached_page_is_removed_without_navigation(self):
        self.pages.find.return_value = CACHED
        self.assertTrue(self.host.flow.delete_cached_page(self.action()))
        self.pages.back.assert_not_called()
        self.pages.discard.assert_called_once_with(CACHED)

    def test_deletion_cancels_the_source_scan_unless_it_is_the_preserved_operation(self):
        action = self.action()
        source = self.deck, SOURCE, 'HD2'
        operation = object()
        self.host.active_scans[source] = operation
        with patch.object(self.mod.ScanOperation, 'action_presentation_is_active',
                          return_value=True):
            self.host.flow.delete_cached_page(action, preserve_operation=operation)
            self.host.cancel_context.assert_not_called()
            self.host.flow.delete_cached_page(action, preserve_operation=object())
            self.host.cancel_context.assert_called_once_with(source)
            self.host.cancel_context.reset_mock()
            self.host.flow.delete_cached_page(action)
            self.host.cancel_context.assert_called_once_with(source)


if __name__ == '__main__':
    unittest.main()
