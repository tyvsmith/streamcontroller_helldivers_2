import importlib
import hashlib
import json
from pathlib import Path
import threading
import time
import types
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import ANY, Mock, patch

from PIL import Image

from .package_loader import plugin_module

from .action_test_support import ActionTestHarness


class ActionPageTests(ActionTestHarness, unittest.TestCase):
    def test_persistent_session_restores_on_new_controller(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            self.coordinator = self.mod.ScanCoordinator(self.plugin, state_dir=directory)
            # Controllers are identity-keyed, like the host's DeckController.
            self.deck = type('Deck', (), {'serial_number': lambda self: 'deck-one'})()
            a = self.action()
            session = self.coordinator.session(a)
            token = session.begin([1])
            session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
            self.coordinator.persist(a, session)
            self.coordinator.disconnect(a)
            a.deck_controller = type(self.deck)()
            restored = self.coordinator.session(a).snapshot()
            self.assertEqual(restored.assignments[1], 'A')
            self.assertEqual(restored.status, 'ready')
            other = self.mod.ScanCoordinator(self.plugin, state_dir=directory)
            self.assertEqual(other.session(a).snapshot().assignments[1], 'A')

    def test_corrupt_state_does_not_break_action(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            self.coordinator = self.mod.ScanCoordinator(self.plugin, state_dir=directory)
            self.deck = type('Deck', (), {'serial_number': lambda self: 'deck-one'})()
            a = self.action()
            self.coordinator.store.path(self.coordinator.identity(a)).write_text('{')
            self.assertFalse(self.coordinator.session(a).snapshot().assignments)
            self.assertIn(self.coordinator.context(a), self.coordinator.state_errors)

    def test_storage_failure_preserves_live_assignments(self):
        with TemporaryDirectory() as directory:
            self.coordinator = self.mod.ScanCoordinator(self.plugin, state_dir=directory)
            self.deck = type('Deck', (), {'serial_number': lambda self: 'deck-one'})()
            a = self.action()
            session = self.coordinator.session(a)
            token = session.begin([1])
            session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
            with patch.object(self.coordinator.store, 'save', side_effect=OSError('disk full')):
                self.coordinator.persist(a, session)
            self.assertEqual(session.snapshot().assignments[1], 'A')
            self.assertEqual(session.snapshot().status, 'ready')
            self.assertIn('disk full', self.coordinator.state_errors[self.coordinator.context(a)])

    def test_new_page_scans_without_source_slots_and_preserves_source_session(self):
        action, root = self.temporary_setup()
        self.plugin.stratagems['B'] = ['DOWN']
        original = self.action()
        original.get_settings.return_value = {'group': 'HD2'}
        session = self.coordinator.session(original)
        token = session.begin([1])
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
        self.coordinator.persist(original, session)
        before = session.checkpoint()
        source_state = self.coordinator.store.path(self.coordinator.identity(original))
        source_bytes = source_state.read_bytes()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'B'}]})
        path = self.deck.active_page.json_path
        self.assertNotEqual(path, self.page.json_path)
        after = session.checkpoint()
        before.pop('scan_number'); after.pop('scan_number')
        self.assertEqual(after, before)
        self.assertEqual(source_state.read_bytes(), source_bytes)
        loaded = self.coordinator.sessions[(self.deck, path, 'HD2')].snapshot()
        self.assertEqual(loaded.assignments[1], 'B')
        self.assertEqual(len(loaded.assignments), 13)
        identity = dict(deck='deck-one', page=path, group='HD2')
        self.assertTrue(self.coordinator.store.path(identity).exists())
        self.assertFalse(self.plugin.input_lock.locked())

    def test_page_openers_have_independent_caches_across_restart_and_delete(self):
        first, root = self.temporary_setup()
        second = self.second_page_opener()
        self.plugin.stratagems['B'] = ['DOWN']
        self.synchronous_scan(first, {'status': 'matched', 'rows': [{'id': 'A'}]})
        first_path = self.deck.active_page.json_path
        first_back = self.action(); first_back.page = types.SimpleNamespace(json_path=first_path)
        first_back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(first_back)
        self.synchronous_scan(second, {'status': 'matched', 'rows': [{'id': 'B'}]})
        second_path = self.deck.active_page.json_path
        second_back = self.action(); second_back.page = types.SimpleNamespace(json_path=second_path)
        second_back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(second_back)

        self.assertNotEqual(first_path, second_path)
        self.assertEqual(self.coordinator.cached_page(first), first_path)
        self.assertEqual(self.coordinator.cached_page(second), second_path)
        manager = self.coordinator.temporary_pages.manager
        restarted = self.mod.ScanCoordinator(self.plugin, state_dir=root / 'scan-state')
        restarted.enable_temporary_pages(manager)
        self.coordinator = restarted
        self.plugin.scan_coordinator = restarted
        self.assertEqual(restarted.cached_page(first), first_path)
        self.assertEqual(restarted.cached_page(second), second_path)
        self.assertTrue(restarted.delete_cached_page(first))
        self.assertFalse(Path(first_path).exists())
        self.assertEqual(restarted.cached_page(second), second_path)
        self.assertTrue(Path(second_path).exists())

    def test_deleting_one_cache_does_not_cancel_other_queued_creation(self):
        first, root = self.temporary_setup()
        second = self.second_page_opener()
        self.plugin.stratagems['B'] = ['DOWN']
        self.synchronous_scan(first, {'status': 'matched', 'rows': [{'id': 'A'}]})
        first_path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=first_path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        queued = []
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan', return_value={
                 'status': 'matched', 'rows': [{'id': 'B'}]}), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args: queued.append((callback, args)) or 1):
            self.coordinator.start(second, replace=True)
            thread.call_args.kwargs['target']()
            self.assertNotIn(self.coordinator.context(second), self.coordinator.active_scans)
            presentation = second._scan_presentation
            self.assertTrue(presentation[1].is_active(presentation[2]))
            with patch.object(self.mod, 'Thread') as blocked:
                self.coordinator.start(first, replace=True, regenerate=True)
            blocked.assert_not_called()
            self.assertTrue(Path(first_path).exists())
            self.assertTrue(presentation[1].is_active(presentation[2]))
            self.assertTrue(self.coordinator.delete_cached_page(first))
            self.assertTrue(presentation[1].is_active(presentation[2]))
            callback, args = queued.pop()
            callback(*args)

        second_path = self.coordinator.cached_page(second)
        self.assertFalse(Path(first_path).exists())
        self.assertIsNotNone(second_path)
        self.assertTrue(Path(second_path).exists())

    def test_regenerate_replaces_only_own_cache_with_fresh_report(self):
        first, root = self.temporary_setup()
        second = self.second_page_opener()
        self.plugin.stratagems['B'] = ['DOWN']
        self.synchronous_scan(first, {'status': 'matched', 'rows': [{'id': 'A'}]})
        old_path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=old_path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        self.synchronous_scan(second, {'status': 'matched', 'rows': [{'id': 'A'}]})
        other_path = self.deck.active_page.json_path
        other_back = self.action(); other_back.page = types.SimpleNamespace(json_path=other_path)
        other_back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(other_back)
        source_before = self.coordinator.session(first).checkpoint()

        self.synchronous_scan(first, {'status': 'matched', 'rows': [{'id': 'B'}]},
                              replace=True, regenerate=True)

        new_path = self.deck.active_page.json_path
        self.assertNotEqual(new_path, old_path)
        self.assertFalse(Path(old_path).exists())
        self.assertTrue(Path(new_path).exists())
        self.assertEqual(self.coordinator.cached_page(second), other_path)
        self.assertTrue(Path(other_path).exists())
        current = self.temporary_scan_action(new_path)
        self.assertEqual(self.coordinator.session(current).snapshot().assignments[1], 'B')
        source_after = self.coordinator.session(first).checkpoint()
        source_before.pop('scan_number'); source_after.pop('scan_number')
        self.assertEqual(source_after, source_before)

    def test_busy_regenerate_preserves_existing_cache(self):
        action, root = self.temporary_setup()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        self.plugin.input_lock.acquire()
        try:
            with patch.object(self.mod, 'Thread') as thread:
                self.coordinator.start(action, replace=True, regenerate=True)
            thread.assert_not_called()
        finally:
            self.plugin.input_lock.release()
        self.assertEqual(self.coordinator.cached_page(action), path)
        self.assertTrue(Path(path).exists())

    def test_regenerate_setup_failure_preserves_cached_page_and_state_bytes(self):
        action, root = self.temporary_setup()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action()
        back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        state = self.coordinator.store.path(
            dict(deck='deck-one', page=path, group='HD2'))
        before = state.read_bytes()

        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'check_scan_setup', create=True,
                          side_effect=RuntimeError('missing sandbox OCR')), \
             patch.object(self.mod.scan_lifecycle, 'run_scan') as scan, \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args: callback(*args)):
            thread.side_effect = lambda **kwargs: types.SimpleNamespace(
                start=kwargs['target'])
            self.coordinator.start(action, replace=True, regenerate=True)

        scan.assert_not_called()
        self.assertTrue(Path(path).exists())
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(action._scan_attempt.status, 'failed')
        self.assertFalse(self.plugin.input_lock.locked())

    def test_regenerate_orders_preflight_delete_then_capture(self):
        action, _ = self.temporary_setup()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action()
        back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        events = []
        delete = self.coordinator.delete_cached_page

        def preflight(*args, **kwargs):
            events.append(('preflight', args, kwargs))

        def delete_cached(*args, **kwargs):
            events.append(('delete', args, kwargs))
            return delete(*args, **kwargs)

        def scan(*args, **kwargs):
            events.append(('scan', args, kwargs))
            return {'status': 'matched', 'rows': [{'id': 'A'}]}

        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'check_scan_setup', create=True,
                          side_effect=preflight), \
             patch.object(self.coordinator, 'delete_cached_page',
                          side_effect=delete_cached), \
             patch.object(self.mod.scan_lifecycle, 'run_scan', side_effect=scan), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args: callback(*args)):
            thread.side_effect = lambda **kwargs: types.SimpleNamespace(
                start=kwargs['target'])
            self.coordinator.start(action, replace=True, regenerate=True)

        self.assertEqual([event[0] for event in events],
                         ['preflight', 'delete', 'scan'])
        self.assertEqual(events[0][1], (self.plugin.PATH,))
        self.assertEqual(events[0][2]['backend'], 'gamescope')
        cancel = events[0][2]['cancel_event']
        self.assertIs(events[2][2]['cancel_event'], cancel)
        self.assertFalse(Path(path).exists())
        self.assertIsNotNone(self.coordinator.cached_page(action))

    def test_image_regenerate_keeps_cache_until_replacement_is_ready(self):
        action, root = self.temporary_setup()
        settings = {'group': 'HD2', 'scan_mode': 'new_page',
                    'capture_backend': 'screenshot'}
        self.plugin_settings['screenshot_folder'] = str(root)

        def set_settings(updated):
            settings.clear()
            settings.update(updated)

        action.get_settings.side_effect = lambda: dict(settings)
        action.set_settings.side_effect = set_settings
        first_source = {'kind': 'file', 'selection': 'folder',
                        'path': str(root / 'frame.png'), 'mtime_ns': 1,
                        'size_bytes': 20, 'fingerprint': 'a' * 64}
        self.synchronous_scan(action, {
            'status': 'matched', 'rows': [{'id': 'A'}], 'source': first_source})
        old_path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=old_path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        events = []
        discard = self.coordinator.temporary_pages.discard
        second_source = dict(first_source, mtime_ns=2, fingerprint='b' * 64)

        def preflight(*args, **kwargs):
            events.append(('preflight', Path(old_path).exists(), kwargs))

        def scan(*args, **kwargs):
            events.append(('scan', Path(old_path).exists(), kwargs))
            return {'status': 'matched', 'rows': [{'id': 'A'}],
                    'source': second_source}

        def discard_cached(path):
            if path == old_path:
                events.append(('delete', Path(old_path).exists(), {}))
            return discard(path)

        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'check_scan_setup', side_effect=preflight), \
             patch.object(self.mod.scan_lifecycle, 'run_scan', side_effect=scan), \
             patch.object(self.coordinator.temporary_pages, 'discard',
                          side_effect=discard_cached), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args: callback(*args)):
            thread.side_effect = lambda **kwargs: types.SimpleNamespace(
                start=kwargs['target'])
            self.coordinator.start(action, replace=True, regenerate=True)

        self.assertEqual([event[0] for event in events],
                         ['preflight', 'scan', 'delete'])
        self.assertTrue(all(event[1] for event in events))
        image = {'kind': 'folder', 'path': str(root),
                 'trigger': 'hotkey', 'hotkey': 'KEY_F12',
                 'script': '', 'delete_after_scan': True,
                 'previous_fingerprint': None, 'allow_rescan': True}
        self.assertEqual(events[0][2]['image_source'], image)
        self.assertEqual(events[1][2]['image_source'], image)
        self.assertFalse(Path(old_path).exists())
        self.assertNotEqual(self.deck.active_page.json_path, old_path)
        self.assertNotIn('image_source_last_fingerprint', settings)

    def test_new_page_hold_recaptures_after_previous_scan(self):
        _, root = self.temporary_setup()
        action = self.real_page_opener(backend='screenshot')
        image_path = root / 'frame.png'
        Image.new('RGB', (12, 8), 'red').save(image_path)
        self.plugin_settings['screenshot_folder'] = str(image_path.parent)
        source = {'kind': 'file', 'selection': 'folder', 'path': str(image_path),
                  'mtime_ns': image_path.stat().st_mtime_ns,
                  'size_bytes': image_path.stat().st_size,
                  'fingerprint': hashlib.sha256(image_path.read_bytes()).hexdigest()}
        report = {'status': 'matched', 'rows': [{'id': 'A'}], 'source': source}
        self.synchronous_scan(action, report)
        old_path = self.deck.active_page.json_path
        back = self.action()
        back.page = types.SimpleNamespace(json_path=old_path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        loader = importlib.import_module(
            plugin_module('automatic_stratagems.scanner.image_source'))

        def load_then_report(_root, **kwargs):
            selected = kwargs['image_source']
            loader.read_image_source(
                selected['kind'], selected['path'],
                previous_fingerprint=selected['previous_fingerprint'],
                allow_rescan=selected['allow_rescan'],
                cancel_event=kwargs['cancel_event'])
            return report

        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'check_scan_setup'), \
             patch.object(self.mod.scan_lifecycle, 'run_scan', side_effect=load_then_report), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args: callback(*args)):
            thread.side_effect = lambda **kwargs: types.SimpleNamespace(
                start=kwargs['target'])
            action.on_key_down()
            action.on_key_hold_start()

        self.assertFalse(Path(old_path).exists())
        self.assertNotEqual(self.deck.active_page.json_path, old_path)
        self.assertNotIn('image_source_last_fingerprint', action.get_settings())
        self.assertFalse(self.plugin.input_lock.locked())

    def test_image_replacement_discard_failure_restores_old_cache_only(self):
        action, root = self.temporary_setup()
        settings = {'group': 'HD2', 'scan_mode': 'new_page',
                    'capture_backend': 'screenshot'}
        self.plugin_settings['screenshot_folder'] = str(root)
        action.get_settings.side_effect = lambda: dict(settings)
        action.set_settings.side_effect = lambda updated: (
            settings.clear(), settings.update(updated))
        source = {'kind': 'file', 'selection': 'folder',
                  'path': str(root / 'frame.png'), 'mtime_ns': 1,
                  'size_bytes': 20, 'fingerprint': 'a' * 64}
        self.synchronous_scan(action, {
            'status': 'matched', 'rows': [{'id': 'A'}], 'source': source})
        old_path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=old_path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        discard = self.coordinator.temporary_pages.discard

        def fail_old(path):
            if path == old_path:
                raise RuntimeError('old cache busy')
            return discard(path)

        with patch.object(self.coordinator.temporary_pages, 'discard',
                          side_effect=fail_old):
            self.synchronous_scan(
                action,
                {'status': 'matched', 'rows': [{'id': 'A'}],
                 'source': dict(source, mtime_ns=2, fingerprint='b' * 64)},
                replace=True, regenerate=True)

        pages = list((root / 'temporary-pages').glob('*.json'))
        self.assertEqual(pages, [Path(old_path)])
        self.assertEqual(self.coordinator.cached_page(action), old_path)
        self.assertNotIn('image_source_last_fingerprint', settings)
        self.assertEqual(action._scan_attempt.status, 'failed')

    def test_image_regenerate_scan_failure_preserves_cache_and_assignments(self):
        action, root = self.temporary_setup()
        settings = {'group': 'HD2', 'scan_mode': 'new_page',
                    'capture_backend': 'screenshot'}
        self.plugin_settings['screenshot_folder'] = str(root)

        def set_settings(updated):
            settings.clear()
            settings.update(updated)

        action.get_settings.side_effect = lambda: dict(settings)
        action.set_settings.side_effect = set_settings
        source = {'kind': 'file', 'selection': 'folder',
                  'path': str(root / 'frame.png'), 'mtime_ns': 1,
                  'size_bytes': 20, 'fingerprint': 'a' * 64}
        self.synchronous_scan(action, {
            'status': 'matched', 'rows': [{'id': 'A'}], 'source': source})
        old_path = self.deck.active_page.json_path
        old_snapshot = self.coordinator.sessions[
            (self.deck, old_path, 'HD2')].snapshot()
        back = self.action(); back.page = types.SimpleNamespace(json_path=old_path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)

        self.synchronous_scan(
            action, error=RuntimeError('image changed during read'),
            replace=True, regenerate=True)

        self.assertTrue(Path(old_path).exists())
        self.assertEqual(self.coordinator.cached_page(action), old_path)
        self.assertEqual(
            self.coordinator.sessions[(self.deck, old_path, 'HD2')].snapshot(),
            old_snapshot)
        self.assertNotIn('image_source_last_fingerprint', settings)
        self.assertFalse(self.plugin.input_lock.locked())

    def test_image_no_detections_keeps_cached_page_without_history(self):
        action, root = self.temporary_setup()
        settings = {'group': 'HD2', 'scan_mode': 'new_page',
                    'capture_backend': 'screenshot'}
        self.plugin_settings['screenshot_folder'] = str(root / 'captures')

        def set_settings(updated):
            settings.clear()
            settings.update(updated)

        action.get_settings.side_effect = lambda: dict(settings)
        action.set_settings.side_effect = set_settings
        first = {'kind': 'file', 'selection': 'folder',
                 'path': str(root / 'captures' / 'first.png'), 'mtime_ns': 1,
                 'size_bytes': 20, 'fingerprint': 'a' * 64}
        self.synchronous_scan(action, {
            'status': 'matched', 'rows': [{'id': 'A'}], 'source': first})
        old_path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=old_path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        second = dict(
            first, path=str(root / 'captures' / 'second.png'),
            mtime_ns=2, fingerprint='b' * 64)

        self.synchronous_scan(
            action, {'status': 'no_detections', 'rows': [], 'source': second},
            replace=True, regenerate=True)

        self.assertTrue(Path(old_path).exists())
        self.assertNotIn('image_source_last_fingerprint', settings)
        self.assertEqual(self.coordinator.cached_page(action), old_path)

    def test_global_screenshot_change_invalidates_cache(self):
        _, root = self.temporary_setup()
        action = self.real_page_opener(backend='screenshot')
        values = action.get_settings()
        action.get_settings = lambda: dict(values)
        action.set_settings = lambda updated: (values.clear(), values.update(updated))
        self.plugin_settings['screenshot_folder'] = str(root)
        source = {'kind': 'file', 'selection': 'folder',
                  'path': str(root / 'first.png'), 'mtime_ns': 1,
                  'size_bytes': 20, 'fingerprint': 'a' * 64}
        self.synchronous_scan(action, {
            'status': 'matched', 'rows': [{'id': 'A'}], 'source': source})
        path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        self.plugin_settings['screenshot_folder'] = str(root / 'other')
        self.coordinator.screenshot_settings_changed()
        self.assertFalse(Path(path).exists())
        self.assertNotIn('image_source_last_fingerprint', action.get_settings())

    def test_global_screenshot_change_cancels_active_scan(self):
        action = self.rendering_action(
            self.mod.AutomaticStratagemScanner, {'capture_backend': 'screenshot'})
        self.coordinator.actions.add(action)
        context = self.coordinator.context(action)
        session = self.coordinator.session(action)
        session.begin({1: 'any'})
        operation = self.mod.ScanOperation(
            action, continue_scan=True, on_complete=lambda _operation: None)
        self.coordinator.active_scans[context] = operation

        self.coordinator.screenshot_settings_changed()

        self.assertTrue(operation.cancel.is_set())
        self.assertEqual(session.snapshot().status, 'idle')

    def test_global_screenshot_change_preserves_gamescope_cache_and_session(self):
        action, root = self.temporary_setup()
        pages = self.coordinator.temporary_pages
        address = self.mod._source_action_address(action)
        screenshot = pages.create(
            self.deck, self.page.json_path, 'HD2', 'auto', address)
        gamescope = pages.create(
            self.deck, self.page.json_path, 'other', 'gamescope', address)
        screenshot_context = self.deck, screenshot, 'HD2'
        gamescope_context = self.deck, gamescope, 'other'
        self.coordinator.sessions[screenshot_context] = self.mod.ScanSession()
        self.coordinator.sessions[gamescope_context] = self.mod.ScanSession()

        self.coordinator.screenshot_settings_changed()

        self.assertFalse(Path(screenshot).exists())
        self.assertNotIn(screenshot_context, self.coordinator.sessions)
        self.assertTrue(Path(gamescope).exists())
        self.assertIn(gamescope_context, self.coordinator.sessions)

    def test_stale_generated_page_rejects_changed_global_screenshot_source(self):
        _, root = self.temporary_setup()
        action = self.real_page_opener(backend='screenshot')
        self.plugin_settings['screenshot_folder'] = str(root)
        source = {'kind': 'file', 'selection': 'folder',
                  'path': str(root / 'first.png'), 'mtime_ns': 1,
                  'size_bytes': 20, 'fingerprint': 'a' * 64}
        self.synchronous_scan(action, {
            'status': 'matched', 'rows': [{'id': 'A'}], 'source': source})
        path = self.deck.active_page.json_path
        scan = self.temporary_scan_action(path)
        self.plugin_settings['screenshot_folder'] = str(root / 'other')

        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan') as run:
            self.coordinator.start(scan, replace=True)

        thread.assert_not_called()
        run.assert_not_called()
        self.assertFalse(self.plugin.input_lock.locked())

    def test_auto_gamescope_report_keeps_screenshot_metadata_empty(self):
        action, root = self.temporary_setup()
        action.get_settings.return_value = {
            'group': 'HD2', 'scan_mode': 'new_page',
            'capture_backend': 'auto'}
        report = {
            'status': 'matched', 'rows': [{'id': 'A'}],
            'source': {'kind': 'live', 'backend': 'gamescope',
                       'socket': '/run/gamescope.sock'}}

        self.synchronous_scan(action, report)

        path = self.deck.active_page.json_path
        self.assertNotEqual(path, str(root / 'original.json'))
        settings = self.coordinator.temporary_pages.scan_settings(
            path, bind_image_source=True)
        self.assertNotIn('image_source_last_fingerprint', settings)

    def test_regenerate_source_address_change_while_queued_preserves_cache(self):
        action, _ = self.temporary_setup()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action()
        back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        queued = []

        with patch.object(self.mod.scan_lifecycle, 'check_scan_setup', create=True), \
             patch.object(self.mod.scan_lifecycle, 'run_scan') as scan, \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args:
                          queued.append((callback, args)) or 1):
            self.coordinator.start(action, replace=True, regenerate=True)
            self.wait_for_queue(queued)
            self.page.action_objects['keys']['3x1'] = {0: {0: action}}
            action.input_ident.json_identifier = '3x1'
            callback, args = queued.pop(0)
            callback(*args)
            self.wait_for_input_release()

        scan.assert_not_called()
        self.assertTrue(Path(path).exists())

    def test_regenerate_context_change_while_queued_preserves_cache(self):
        action, _ = self.temporary_setup()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action()
        back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        queued = []

        with patch.object(self.mod.scan_lifecycle, 'check_scan_setup'), \
             patch.object(self.mod.scan_lifecycle, 'run_scan') as scan, \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args:
                          queued.append((callback, args)) or 1):
            self.coordinator.start(action, replace=True, regenerate=True)
            self.wait_for_queue(queued)
            action.get_settings.return_value = {
                'group': 'other', 'scan_mode': 'new_page'}
            callback, args = queued.pop(0)
            callback(*args)
            self.wait_for_input_release()

        scan.assert_not_called()
        self.assertTrue(Path(path).exists())

    def test_shutdown_during_queued_regenerate_preflight_preserves_cache(self):
        action, _ = self.temporary_setup()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action()
        back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        queued = []

        with patch.object(self.mod.scan_lifecycle, 'check_scan_setup', create=True), \
             patch.object(self.mod.scan_lifecycle, 'run_scan') as scan, \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args:
                          queued.append((callback, args)) or 1):
            self.coordinator.start(action, replace=True, regenerate=True)
            self.wait_for_queue(queued)
            self.assertTrue(self.coordinator.shutdown())

        scan.assert_not_called()
        self.assertTrue(Path(path).exists())
        self.assertFalse(self.plugin.input_lock.locked())

    def test_cancelled_regenerate_releases_lock_once_before_late_gtk_callback(self):
        class CountingLock:
            def __init__(self):
                self.lock = threading.Lock()
                self.releases = 0

            def acquire(self, *args, **kwargs):
                return self.lock.acquire(*args, **kwargs)

            def release(self):
                self.releases += 1
                self.lock.release()

            def locked(self):
                return self.lock.locked()

        action, _ = self.temporary_setup()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action()
        back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        lock = CountingLock()
        self.plugin.input_lock = lock
        queued = []

        with patch.object(self.mod.scan_lifecycle, 'check_scan_setup'), \
             patch.object(self.mod.scan_lifecycle, 'run_scan') as scan, \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args:
                          queued.append((callback, args)) or 1):
            self.coordinator.start(action, replace=True, regenerate=True)
            self.wait_for_queue(queued)
            self.coordinator.cancel_context(self.coordinator.context(action))
            self.wait_for_input_release()
            callback, args = queued.pop(0)
            callback(*args)

        scan.assert_not_called()
        self.assertEqual(lock.releases, 1)
        self.assertTrue(Path(path).exists())

    def test_queued_regenerate_preflight_has_bounded_gtk_wait(self):
        action, _ = self.temporary_setup()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action()
        back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        queued = []

        try:
            with patch.object(self.mod.scan_lifecycle, 'check_scan_setup'), \
                 patch.object(self.mod.scan_lifecycle, 'run_scan') as scan, \
                 patch.object(self.mod.scan_lifecycle, 'MAIN_CONTEXT_TIMEOUT_SECONDS',
                              .05, create=True), \
                 patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                              side_effect=lambda callback, *args:
                              queued.append((callback, args)) or 1):
                self.coordinator.start(action, replace=True, regenerate=True)
                self.wait_for_queue(queued)
                deadline = time.monotonic() + .5
                while (time.monotonic() < deadline
                       and self.plugin.input_lock.locked()):
                    time.sleep(.01)
                self.assertFalse(self.plugin.input_lock.locked())
                scan.assert_not_called()
        finally:
            self.coordinator.cancel_context(self.coordinator.context(action))
            self.wait_for_input_release()

        self.assertTrue(Path(path).exists())
        self.assertEqual(action._scan_attempt.status, 'failed')

    def test_regenerate_delete_and_capture_failures_do_not_leave_replacement(self):
        action, root = self.temporary_setup()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        with patch.object(self.coordinator.temporary_pages, 'discard',
                          side_effect=RuntimeError('delete failed')), \
             patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'check_scan_setup'), \
             patch.object(self.mod.scan_lifecycle, 'run_scan') as scan, \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args: callback(*args)):
            thread.side_effect = lambda **kwargs: types.SimpleNamespace(
                start=kwargs['target'])
            self.coordinator.start(action, replace=True, regenerate=True)
        thread.assert_called_once()
        scan.assert_not_called()
        action.show_error.assert_called()
        self.assertTrue(Path(path).exists())

        action.show_error.reset_mock()
        self.synchronous_scan(action, error=RuntimeError('capture failed'),
                              replace=True, regenerate=True)
        self.assertFalse(Path(path).exists())
        self.assertIsNone(self.coordinator.cached_page(action))
        action.show_error.assert_called()

    def test_regenerate_rejects_own_queued_result_before_restarting(self):
        action, root = self.temporary_setup()
        self.plugin.stratagems['B'] = ['DOWN']
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        old_path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=old_path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        queued = []
        report = {'status': 'matched', 'rows': [{'id': 'B'}]}
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'check_scan_setup'), \
             patch.object(self.mod.scan_lifecycle, 'run_scan', return_value=report), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args:
                          (queued.append((callback, args)) or 1)
                          if args else callback()):
            self.coordinator.start(action, replace=True, regenerate=True)
            first_worker = thread.call_args.kwargs['target']
            first_worker()
            stale_callback = queued.pop()
            self.coordinator.start(action, replace=True, regenerate=True)
            second_worker = thread.call_args.kwargs['target']
            self.assertEqual(thread.call_count, 2)
            stale_callback[0](*stale_callback[1])
            self.assertEqual(list((root / 'temporary-pages').glob('*.json')), [])
            second_worker()
            callback, args = queued.pop()
            callback(*args)

        new_path = self.coordinator.cached_page(action)
        self.assertFalse(Path(old_path).exists())
        self.assertIsNotNone(new_path)
        self.assertTrue(Path(new_path).exists())

    def test_failed_or_unknown_initial_scan_does_not_create_page(self):
        action, root = self.temporary_setup()
        self.synchronous_scan(action, error=RuntimeError('capture failed'))
        self.assertEqual(self.deck.active_page.json_path, self.page.json_path)
        self.assertEqual(self.coordinator.session(action).snapshot().status, 'idle')
        action.show_error.assert_called()
        self.synchronous_scan(action, {'status': 'partial', 'rows': [{'id': None}]})
        self.assertEqual(list((root / 'temporary-pages').glob('*.json')), [])
        self.assertFalse(self.plugin.input_lock.locked())

    def test_page_opener_last_attempt_isolated_from_source_and_other_opener(self):
        _, root = self.temporary_setup()
        first = self.real_page_opener()
        second = self.real_page_opener('3x1')
        source = self.coordinator.session(first)
        token = source.begin({1: 'any'})
        source.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                      self.plugin.stratagems)
        before = source.checkpoint()
        before.pop('scan_number')

        self.synchronous_scan(first, error=RuntimeError('capture failed'))
        first_attempt = first._scan_attempt
        self.assertEqual((first_attempt.status, first_attempt.message),
                         ('failed', 'capture failed'))
        first.show_error.assert_called_once_with(duration=3)
        self.mod.Adw.ActionRow.reset_mock()
        first.get_config_rows()
        self.mod.Adw.ActionRow.assert_any_call(
            title='Last scan', subtitle='capture failed')

        self.synchronous_scan(second, {
            'status': 'partial', 'rows': [{'id': None}], 'source': {'kind': 'live', 'backend': 'gamescope', 'socket': '/run/gamescope.sock'}})
        second_attempt = second._scan_attempt
        self.assertEqual(second_attempt.status, 'partial')
        self.assertIn('0 recognized', second_attempt.message)
        self.assertEqual(first._scan_attempt, first_attempt)
        second.show_error.assert_not_called()
        after = source.checkpoint()
        after.pop('scan_number')
        self.assertEqual(after, before)
        self.assertEqual(list((root / 'temporary-pages').glob('*.json')), [])

        self.mod.Adw.ActionRow.reset_mock()
        second.get_config_rows()
        self.mod.Adw.ActionRow.assert_any_call(
            title='Last scan', subtitle=second_attempt.message)

    def test_fresh_page_opener_does_not_show_source_last_scan(self):
        _, _ = self.temporary_setup()
        first = self.real_page_opener()
        source = self.coordinator.session(first)
        token = source.begin({1: 'any'})
        source.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                      self.plugin.stratagems)
        fresh = self.real_page_opener('3x1')

        self.mod.Adw.ActionRow.reset_mock()
        fresh.get_config_rows()

        self.mod.Adw.ActionRow.assert_any_call(
            title='Last scan', subtitle='No scan yet')

    def test_page_opener_records_no_detections_and_setup_failure(self):
        action = self.rendering_action(
            self.mod.AutomaticStratagemPage,
            {'group': 'HD2', 'capture_backend': 'screenshot'})
        action.show_error = Mock()
        with patch.object(self.mod, 'Thread') as thread:
            self.coordinator.start(action, replace=True)
        thread.assert_not_called()
        self.assertEqual(action._scan_attempt.status, 'failed')
        self.assertIn('Temporary pages are unavailable', action._scan_attempt.message)
        action.show_error.assert_called_once_with(duration=3)

        _, root = self.temporary_setup()
        action = self.real_page_opener()
        self.synchronous_scan(action, {'status': 'no_detections', 'rows': [], 'source': {'kind': 'live', 'backend': 'gamescope', 'socket': '/run/gamescope.sock'}})
        self.assertEqual(action._scan_attempt.status, 'failed')
        self.assertEqual(action._scan_attempt.message, 'No stratagems detected')
        action.show_error.assert_called_once_with(duration=3)
        self.assertEqual(list((root / 'temporary-pages').glob('*.json')), [])

    def test_page_opener_cancelled_attempt_and_stale_completion_do_not_replace_latest(self):
        _, _ = self.temporary_setup()
        action = self.real_page_opener()
        other = self.real_page_opener('3x1')
        other._scan_attempt = self.mod.ScanAttempt(1, 'failed', 'other failed')
        other_attempt = other._scan_attempt
        queued = []
        reports = [
            {'status': 'matched', 'rows': [{'id': 'A'}],
             'source': {'kind': 'live', 'backend': 'gamescope',
                        'socket': '/run/gamescope.sock'}},
            {'status': 'partial', 'rows': [{'id': None}],
             'source': {'kind': 'live', 'backend': 'gamescope',
                        'socket': '/run/gamescope.sock'}},
        ]
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan', side_effect=reports), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args:
                          queued.append((callback, args)) or 1):
            self.coordinator.start(action, replace=True)
            thread.call_args.kwargs['target']()
            stale_callback = queued.pop()
            first_id = action._scan_attempt.id
            self.coordinator.cancel_context(self.coordinator.context(action))
            self.assertEqual(action._scan_attempt.status, 'cancelled')
            self.assertEqual(other._scan_attempt, other_attempt)

            self.coordinator.start(action, replace=True)
            second_worker = thread.call_args.kwargs['target']
            self.assertGreater(action._scan_attempt.id, first_id)
            self.assertEqual(action._scan_attempt.status, 'scanning')
            stale_callback[0](*stale_callback[1])
            self.assertEqual(action._scan_attempt.status, 'scanning')
            second_worker()
            callback, args = queued.pop()
            callback(*args)

        self.assertEqual(action._scan_attempt.status, 'partial')
        self.assertIn('0 recognized', action._scan_attempt.message)

    def test_back_retains_temporary_page_state_and_session(self):
        action, root = self.temporary_setup()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action()
        back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        self.assertEqual(self.deck.active_page.json_path, self.page.json_path)
        self.assertTrue(Path(path).exists())
        self.assertEqual(len(list((root / 'scan-state').glob('*.json'))), 1)
        self.assertIn((self.deck, path, 'HD2'), self.coordinator.sessions)

    def test_page_open_failure_rolls_back_page_and_state(self):
        action, root = self.temporary_setup()
        with patch.object(self.coordinator.temporary_pages, 'show', side_effect=RuntimeError('load failed')):
            self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        self.assertEqual(self.coordinator.session(action).snapshot().status, 'idle')
        self.assertEqual(len(list((root / 'scan-state').glob('*.json'))), 0)
        self.assertEqual(list((root / 'temporary-pages').glob('*.json')), [])
        self.assertFalse(self.plugin.input_lock.locked())

    def test_scan_again_updates_temporary_page_without_creating_another(self):
        launcher, root = self.temporary_setup()
        self.plugin.stratagems['B'] = ['DOWN']
        self.synchronous_scan(launcher, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        scan = self.temporary_scan_action(path)
        self.synchronous_scan(scan, {'status': 'matched', 'rows': [{'id': 'B'}]})
        snapshot = self.coordinator.session(scan).snapshot()
        self.assertEqual(snapshot.assignments[1], 'A')
        self.assertEqual(snapshot.assignments[2], 'B')
        self.assertEqual(snapshot.unconfirmed_slots, frozenset({1}))
        self.assertEqual(self.deck.active_page.json_path, path)
        self.assertEqual(len(list((root / 'temporary-pages').glob('*.json'))), 1)

    def test_generated_scan_after_clear_recaptures(self):
        _, root = self.temporary_setup()
        launcher = self.real_page_opener(backend='screenshot')
        self.plugin_settings['screenshot_folder'] = str(root)
        image_path = root / 'frame.png'
        Image.new('RGB', (12, 8), 'red').save(image_path)
        source = {'kind': 'file', 'selection': 'folder',
                  'path': str(image_path), 'mtime_ns': image_path.stat().st_mtime_ns,
                  'size_bytes': image_path.stat().st_size,
                  'fingerprint': hashlib.sha256(image_path.read_bytes()).hexdigest()}
        report = {'status': 'matched', 'rows': [{'id': 'A'}], 'source': source}
        self.synchronous_scan(launcher, report)
        path = self.deck.active_page.json_path
        self.temporary_scan_action(path)
        settings = self.coordinator.temporary_pages.scan_settings(path)
        scan = self.rendering_action(self.mod.AutomaticStratagemScanner, settings)
        scan.page = types.SimpleNamespace(json_path=path)
        self.coordinator.actions.add(scan)
        self.coordinator.clear(scan)

        loader = importlib.import_module(
            plugin_module('automatic_stratagems.scanner.image_source'))

        def load_then_report(_root, **kwargs):
            selected = kwargs['image_source']
            loader.read_image_source(
                selected['kind'], selected['path'],
                previous_fingerprint=selected['previous_fingerprint'],
                allow_rescan=selected['allow_rescan'],
                cancel_event=kwargs['cancel_event'])
            return report

        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan', side_effect=load_then_report) as run, \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args: callback(*args)):
            thread.side_effect = lambda **kwargs: types.SimpleNamespace(
                start=kwargs['target'])
            scan.on_key_down()
            scan.on_key_short_up()

        self.assertEqual(run.call_args.kwargs['image_source'], {
            'kind': 'folder', 'path': str(root),
            'trigger': 'hotkey', 'hotkey': 'KEY_F12',
            'script': '', 'delete_after_scan': True,
            'previous_fingerprint': None, 'allow_rescan': True})
        self.assertEqual(self.coordinator.session(scan).snapshot().assignments[1], 'A')
        self.assertFalse(self.plugin.input_lock.locked())

    def test_generated_scan_does_not_persist_report_history(self):
        _, root = self.temporary_setup()
        launcher = self.real_page_opener(backend='screenshot')
        self.plugin_settings['screenshot_folder'] = str(root / 'captures')
        first_source = {'kind': 'file', 'selection': 'folder',
                        'path': str(root / 'captures' / 'first.png'),
                        'mtime_ns': 1, 'size_bytes': 20,
                        'fingerprint': 'a' * 64}
        self.synchronous_scan(launcher, {
            'status': 'matched', 'rows': [{'id': 'A'}], 'source': first_source})
        path = self.deck.active_page.json_path
        scan = self.temporary_scan_action(path)
        scan_settings = dict(scan.get_settings())

        def set_scan_settings(updated):
            scan_settings.clear()
            scan_settings.update(updated)

        scan.get_settings.side_effect = lambda: dict(scan_settings)
        scan.set_settings.side_effect = set_scan_settings
        second_source = dict(
            first_source, path=str(root / 'captures' / 'second.png'),
            mtime_ns=2, fingerprint='b' * 64)
        self.synchronous_scan(scan, {
            'status': 'partial', 'rows': [{'id': None}], 'source': second_source})

        self.assertNotIn('image_source_last_fingerprint', scan_settings)
        self.assertNotIn(
            'image_source_last_fingerprint', launcher.get_settings())

    def test_generated_auto_scan_uses_canonical_page_image_source(self):
        _, root = self.temporary_setup()
        launcher = self.real_page_opener(backend='screenshot')
        self.plugin_settings['screenshot_folder'] = str(root)
        first = {'kind': 'file', 'selection': 'folder',
                 'path': str(root / 'frame.png'), 'mtime_ns': 1,
                 'size_bytes': 20, 'fingerprint': 'a' * 64}
        self.synchronous_scan(launcher, {
            'status': 'matched', 'rows': [{'id': 'A'}], 'source': first})
        path = self.deck.active_page.json_path
        before = len(self.__dict__.get('page_actions', []))
        self.temporary_scan_action(path)
        automatic = self.page_actions[before]
        second = dict(first, mtime_ns=2, fingerprint='b' * 64)

        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan', return_value={
                 'status': 'matched', 'rows': [{'id': 'A'}],
                 'source': second}) as scan, \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args: callback(*args)):
            thread.side_effect = lambda **kwargs: types.SimpleNamespace(
                start=kwargs['target'])
            self.coordinator.start(automatic, replace=True)

        scan.assert_called_once_with(
            self.plugin.PATH, backend='screenshot', workers=2, cancel_event=ANY,
            image_source={'kind': 'folder', 'path': str(root),
                          'trigger': 'hotkey', 'hotkey': 'KEY_F12',
                          'script': '', 'delete_after_scan': True,
                          'previous_fingerprint': None,
                          'allow_rescan': True})
        page_settings = self.coordinator.temporary_pages.scan_settings(path)
        self.assertNotIn('image_source_last_fingerprint', page_settings)

    def test_generated_auto_fails_closed_without_canonical_scanner(self):
        launcher, root = self.temporary_setup()
        self.synchronous_scan(launcher, {
            'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        before = len(self.__dict__.get('page_actions', []))
        self.temporary_scan_action(path)
        automatic = self.page_actions[before]
        page = json.loads(Path(path).read_text())
        scan_key = list(page['keys'])[1]
        del page['keys'][scan_key]
        Path(path).write_text(json.dumps(page))

        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan') as scan:
            self.coordinator.start(automatic, replace=True)

        thread.assert_not_called()
        scan.assert_not_called()
        self.assertFalse(self.plugin.input_lock.locked())

    def test_generated_auto_rejects_marker_and_scanner_source_mismatch(self):
        launcher, _ = self.temporary_setup()
        self.synchronous_scan(launcher, {
            'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        before = len(self.__dict__.get('page_actions', []))
        self.temporary_scan_action(path)
        automatic = self.page_actions[before]
        page = json.loads(Path(path).read_text())
        scan_action = list(page['keys'].values())[1]['states']['0']['actions'][0]
        scan_action['settings']['capture_backend'] = 'screenshot'
        Path(path).write_text(json.dumps(page))

        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan') as scan:
            self.coordinator.start(automatic, replace=True)

        thread.assert_not_called()
        scan.assert_not_called()
        self.assertFalse(self.plugin.input_lock.locked())

    def test_generated_scan_rejects_backend_edits(self):
        action, root = self.temporary_setup()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        scan = self.temporary_scan_action(path)
        before = dict(scan.get_settings())
        scan.configure = self.rendering_action(
            self.mod.AutomaticStratagemScanner, before).configure
        scan.configure.__self__.page = scan.page
        scan.configure.__self__.plugin_base = self.plugin
        scan.configure('capture_backend', 'screenshot')
        self.assertEqual(scan.get_settings(), before)

    def test_back_during_scan_rejects_late_results_without_recreating_files(self):
        launcher, root = self.temporary_setup()
        report = {'status': 'matched', 'rows': [{'id': 'A'}]}
        self.synchronous_scan(launcher, report)
        path = self.deck.active_page.json_path
        scan = self.temporary_scan_action(path)
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan', return_value=report), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add', side_effect=lambda f, *args: f(*args)):
            self.coordinator.start(scan)
            self.assertTrue(self.plugin.input_lock.locked())
            back = self.action()
            back.page = scan.page
            back.get_settings.return_value = {'group': 'HD2'}
            self.coordinator.back(back)
            self.assertEqual(self.deck.active_page.json_path, self.page.json_path)
            thread.call_args.kwargs['target']()
        self.assertFalse(self.plugin.input_lock.locked())
        self.assertTrue(Path(path).exists())
        self.assertEqual(len(list((root / 'scan-state').glob('*.json'))), 1)
        saved_pages = {json.loads(saved.read_text())['context']['page']
                       for saved in (root / 'scan-state').glob('*.json')}
        self.assertIn(path, saved_pages)

    def test_cancelled_launch_does_not_open_page_when_result_arrives(self):
        launcher, root = self.temporary_setup()
        report = {'status': 'matched', 'rows': [{'id': 'A'}]}
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan', return_value=report), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add', side_effect=lambda f, *args: f(*args)):
            self.coordinator.start(launcher)
            self.coordinator.cancel_context(self.coordinator.context(launcher))
            thread.call_args.kwargs['target']()
        self.assertEqual(self.deck.active_page.json_path, self.page.json_path)
        self.assertEqual(list((root / 'temporary-pages').glob('*.json')), [])
        self.assertFalse(self.plugin.input_lock.locked())

    def test_missing_cached_page_always_starts_a_fresh_scan(self):
        launcher, root = self.temporary_setup()
        update = self.action()
        update.get_settings.return_value = {'group': 'HD2'}
        session = self.coordinator.session(update)
        token = session.begin({1: 'any'})
        report = {'status': 'partial', 'rows': [{'id': 'A'}, {'id': None}]}
        session.finish(token, report, self.plugin.stratagems)
        self.assertIs(self.coordinator.session(launcher), session)
        with patch.object(self.mod, 'Thread') as thread:
            self.coordinator.start(launcher)
            thread.assert_called_once()
        self.assertEqual(self.deck.active_page.json_path, self.page.json_path)
        self.plugin.input_lock.release()

    def test_temporary_scan_stays_local_and_source_survives_restart(self):
        launcher, root = self.temporary_setup()
        self.plugin.stratagems['B'] = ['DOWN']
        source = self.coordinator.session(launcher)
        token = source.begin({1: 'any'})
        source.finish(token, {'status': 'partial', 'rows': [{'id': 'A'}, {'id': None}]}, self.plugin.stratagems)
        self.coordinator.persist(launcher, source)
        self.synchronous_scan(launcher, {'status': 'partial', 'rows': [{'id': 'A'}, {'id': None}]})
        path = self.deck.active_page.json_path
        scan = self.temporary_scan_action(path)
        self.synchronous_scan(scan, {'status': 'matched', 'rows': [{'id': 'B'}]})
        self.assertEqual(source.snapshot().assignments[1], 'A')
        self.assertEqual(source.snapshot().unknown, 1)
        self.assertEqual(dict(self.coordinator.session(scan).snapshot().assignments),
                         {1: 'A', 2: 'B', **dict.fromkeys(range(3, 14))})
        back = self.action(); back.page = scan.page
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        other = self.mod.ScanCoordinator(self.plugin, state_dir=root/'scan-state')
        restored = other.session(launcher)
        self.assertEqual(restored.snapshot().assignments[1], 'A')
        self.assertEqual(restored.latest_report()['rows'], [{'id': 'A'}, {'id': None}])

    def test_source_and_cached_scans_are_independent_in_both_directions(self):
        launcher, root = self.temporary_setup()
        self.plugin.stratagems.update(B=['DOWN'], C=['LEFT'])
        colors = {'A': 'red', 'B': 'blue', 'C': 'blue'}
        source = self.coordinator.session(launcher)
        token = source.begin({1: 'red', 2: 'blue'})
        source.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}, {'id': 'B'}]}, self.plugin.stratagems, colors)
        unrelated = self.action(); unrelated.get_settings.return_value = {'group': 'other'}
        separate = self.coordinator.session(unrelated)
        before = separate.snapshot()
        with patch.object(self.mod.scan_lifecycle, 'catalog_colors', return_value=colors), \
             patch.object(self.mod.session_registry, 'catalog_colors',
                          return_value=colors):
            self.synchronous_scan(launcher, {
                'status': 'matched', 'rows': [{'id': 'A'}, {'id': 'B'}]})
            scan = self.temporary_scan_action(self.deck.active_page.json_path)
            self.synchronous_scan(scan, {'status': 'matched', 'rows': [{'id': 'C'}]},
                                  replace=True)
            cached_after_scan = self.coordinator.session(scan).snapshot()
            self.assertEqual(cached_after_scan.assignments[1], 'C')
            self.assertEqual(dict(source.snapshot().assignments), {1: 'A', 2: 'B'})
            back = self.action(); back.page = scan.page
            back.get_settings.return_value = {'group': 'HD2'}
            self.coordinator.back(back)
            self.synchronous_scan(launcher, {
                'status': 'matched', 'rows': [{'id': 'B'}]}, replace=True)
        self.assertEqual(source.snapshot().assignments[2], 'B')
        self.assertEqual(self.coordinator.session(scan).snapshot(), cached_after_scan)
        self.assertEqual(separate.snapshot(), before)

    def test_regenerate_without_cache_starts_one_fresh_scan(self):
        launcher, root = self.temporary_setup()
        source = self.coordinator.session(launcher)
        token = source.begin([1])
        source.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
        with patch.object(self.mod, 'Thread') as thread:
            self.coordinator.start(launcher, replace=True, regenerate=True)
            thread.assert_called_once()
        self.assertEqual(source.snapshot().status, 'scanning')
        self.assertEqual(self.deck.active_page.json_path, self.page.json_path)
        self.plugin.input_lock.release()

    def test_cached_page_open_failure_preserves_source_assignments(self):
        launcher, root = self.temporary_setup()
        source = self.coordinator.session(launcher)
        token = source.begin({1: 'any'})
        source.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
        self.coordinator.persist(launcher, source)
        self.synchronous_scan(launcher, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        cached_state = self.coordinator.store.path(
            dict(deck='deck-one', page=path, group='HD2')).read_bytes()
        with patch.object(self.coordinator.temporary_pages, 'show',
                          side_effect=RuntimeError('load failed')):
            with self.assertRaisesRegex(RuntimeError, 'load failed'):
                self.coordinator.open_cached_page(launcher)
        self.assertEqual(dict(source.snapshot().assignments), {1: 'A'})
        self.assertEqual(source.snapshot().status, 'ready')
        self.assertFalse(self.plugin.input_lock.locked())
        self.assertEqual(list((root/'temporary-pages').glob('*.json')), [Path(path)])
        self.assertEqual(self.coordinator.store.path(
            dict(deck='deck-one', page=path, group='HD2')).read_bytes(), cached_state)
        restored = self.coordinator.store.load(self.coordinator.identity(launcher), self.plugin.stratagems)
        self.assertEqual(dict(restored.snapshot().assignments), {1: 'A'})

    def test_auto_page_icon_tracks_cache_without_session_revision_change(self):
        launcher, root = self.temporary_setup()
        action = self.rendering_action(self.mod.AutomaticStratagemPage, {'group': 'HD2'})
        action.input_ident = types.SimpleNamespace(input_type='keys', json_identifier='3x1')
        action.state = 0
        self.page.action_objects['keys']['3x1'] = {0: {0: action}}
        action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith(
            '/automatic_stratagems/assets/icons/scan-new-page.png'))
        revision = self.coordinator.session(action).snapshot().revision

        path = self.coordinator.temporary_pages.create(
            self.deck, self.page.json_path, 'HD2', 'auto',
            self.mod._source_action_address(action),
            image_settings=self.mod._image_page_settings(action))
        action.render()
        self.assertEqual(self.coordinator.session(action).snapshot().revision, revision)
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith(
            '/automatic_stratagems/assets/icons/scan.png'))
        self.assertEqual(action.set_top_label.call_args.args[0], 'Auto')

        self.coordinator.temporary_pages.discard(path)
        action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith(
            '/automatic_stratagems/assets/icons/scan-new-page.png'))

    def test_delete_cached_page_preserves_source_assignments_and_state(self):
        launcher, root = self.temporary_setup()
        source = self.coordinator.session(launcher)
        token = source.begin({1: 'any'})
        source.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                      self.plugin.stratagems)
        self.coordinator.persist(launcher, source)
        self.synchronous_scan(launcher, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)

        self.assertTrue(self.coordinator.delete_cached_page(launcher))
        self.assertFalse(Path(path).exists())
        self.assertEqual(source.snapshot().assignments[1], 'A')
        self.assertTrue(self.coordinator.store.path(
            self.coordinator.identity(launcher)).exists())
        self.assertFalse(self.coordinator.store.path(
            dict(deck='deck-one', page=path, group='HD2')).exists())

    def test_deleted_page_actions_do_not_break_recreate_or_current_scan(self):
        launcher, root = self.temporary_setup()
        self.plugin.stratagems['B'] = ['DOWN']
        self.synchronous_scan(launcher, {'status': 'matched', 'rows': [{'id': 'A'}]})
        old_scan = self.temporary_scan_action(self.deck.active_page.json_path)
        back = self.action(); back.page = old_scan.page
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        stale = [action for action in self.page_actions if action.page is old_scan.page]
        for action in stale:
            action.page = None

        self.assertTrue(self.coordinator.delete_cached_page(launcher))
        self.synchronous_scan(launcher, {'status': 'matched', 'rows': [{'id': 'A'}]})
        current = self.temporary_scan_action(self.deck.active_page.json_path)
        self.synchronous_scan(current, {'status': 'matched', 'rows': [{'id': 'B'}]})

        self.assertTrue(all(action not in self.coordinator.actions for action in stale))
        self.assertEqual(self.coordinator.session(current).snapshot().assignments[2], 'B')

    def test_hold_delete_after_worker_finalize_rejects_queued_completion(self):
        launcher, root = self.temporary_setup()
        queued = []
        report = {'status': 'matched', 'rows': [{'id': 'A'}]}
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan', return_value=report), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args: queued.append((callback, args)) or 1):
            self.coordinator.start(launcher, replace=True)
            thread.call_args.kwargs['target']()
            self.assertNotIn(self.coordinator.context(launcher), self.coordinator.active_scans)
            self.assertTrue(queued)
            self.coordinator.delete_cached_page(launcher)
            callback, args = queued.pop()
            callback(*args)

        self.assertEqual(list((root / 'temporary-pages').glob('*.json')), [])
        self.assertEqual(self.deck.active_page.json_path, self.page.json_path)

    def test_unhydrated_cached_state_survives_source_clear_and_scan_after_restart(self):
        launcher, root = self.temporary_setup()
        self.plugin.stratagems['B'] = ['DOWN']
        self.synchronous_scan(launcher, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)

        manager = self.coordinator.temporary_pages.manager
        cached_identity = dict(deck='deck-one', page=path, group='HD2')
        cached_state_path = self.coordinator.store.path(cached_identity)
        cached_before = cached_state_path.read_bytes()
        restarted = self.mod.ScanCoordinator(self.plugin, state_dir=root / 'scan-state')
        restarted.enable_temporary_pages(manager)
        self.coordinator = restarted
        self.plugin.scan_coordinator = restarted
        restarted.clear(launcher)
        self.assertNotIn((self.deck, path, 'HD2'), restarted.sessions)
        self.assertEqual(cached_state_path.read_bytes(), cached_before)
        self.assertIsNone(restarted.session(launcher).latest_report())

        self.synchronous_scan(
            launcher, {'status': 'matched', 'rows': [{'id': 'B'}]}, replace=True)
        self.assertEqual(cached_state_path.read_bytes(), cached_before)
        cached = restarted.sessions[(self.deck, path, 'HD2')]
        self.assertEqual(cached.snapshot().assignments[1], 'A')

    def test_existing_cache_open_failure_does_not_replace_session_or_state(self):
        launcher, root = self.temporary_setup()
        self.synchronous_scan(launcher, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        context = self.deck, path, 'HD2'
        previous_session = self.coordinator.sessions[context]
        state_path = self.coordinator.store.path(
            dict(deck='deck-one', page=path, group='HD2'))
        previous_state = state_path.read_bytes()
        replacement = self.mod.ScanSession()

        with patch.object(self.coordinator.temporary_pages, 'show',
                          side_effect=RuntimeError('load failed')):
            with self.assertRaisesRegex(RuntimeError, 'load failed'):
                self.coordinator.open_temporary(launcher, replacement)

        self.assertTrue(Path(path).exists())
        self.assertIs(self.coordinator.sessions[context], previous_session)
        self.assertEqual(state_path.read_bytes(), previous_state)

    def test_clear_does_not_cancel_inactive_page_scan_token(self):
        launcher, root = self.temporary_setup()
        self.synchronous_scan(launcher, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        scan = self.temporary_scan_action(path)
        cached = self.coordinator.session(scan)
        back = self.action(); back.page = scan.page
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        token = cached.begin({1: 'any'}, replace=True)

        self.coordinator.clear(launcher)

        self.assertTrue(cached.is_active(token))



if __name__ == '__main__':
    unittest.main()
