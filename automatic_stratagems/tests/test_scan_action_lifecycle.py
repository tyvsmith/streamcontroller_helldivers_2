import json
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import ANY, Mock, patch

from .action_test_support import ActionTestHarness


class ActionLifecycleTests(ActionTestHarness, unittest.TestCase):
    def test_automatic_stratagems_require_explicit_opt_in(self):
        for settings in ({}, {'automatic_stratagems_enabled': False},
                         {'automatic_stratagems_enabled': 'true'}):
            self.plugin.get_settings = lambda: settings
            self.assertFalse(self.coordinator.enabled)
            with patch.object(self.mod, 'Thread') as thread:
                self.coordinator.start(self.action())
                thread.assert_not_called()
            self.assertFalse(self.plugin.input_lock.locked())

    def test_disabled_buttons_preserve_assignments_and_block_execution_and_clear(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        session = self.coordinator.session(action)
        token = session.begin([1])
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
        action.displayed = (session.snapshot().revision, 1, 'A')
        action.on_key_down()
        self.plugin.get_settings = lambda: {}
        self.coordinator.settings_changed()
        with patch.object(self.mod, 'execute_stratagem') as execute:
            action.on_key_short_up()
            execute.assert_not_called()
        self.coordinator.clear(action)
        self.assertEqual(session.snapshot().assignments[1], 'A')
        for cls in (self.mod.AutomaticStratagem, self.mod.AutomaticStratagemScanner):
            button = self.rendering_action(cls)
            button.render()
            button.set_bottom_label.assert_called_with('Disabled')
        self.plugin.get_settings = lambda: {'automatic_stratagems_enabled': True}
        self.coordinator.settings_changed()
        self.assertEqual(session.snapshot().assignments[1], 'A')

    def test_disabling_scan_rejects_pending_result_and_worker_releases_lock(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        self.coordinator.actions.add(action)
        action.on_ready_called = True
        session = self.coordinator.session(action)
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'catalog_colors', return_value={}), \
             patch.object(self.mod.scan_lifecycle, 'run_scan', return_value={'status':'matched', 'rows':[{'id':'A'}]}), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add', side_effect=lambda f,*args:f(*args)):
            self.coordinator.start(action, replace=True)
            worker = thread.call_args.kwargs['target']
            self.assertTrue(self.plugin.input_lock.locked())
            self.plugin.get_settings = lambda: {}
            self.coordinator.settings_changed()
            self.assertTrue(self.plugin.input_lock.locked())
            self.plugin.get_settings = lambda: {'automatic_stratagems_enabled': True}
            worker()
        self.assertFalse(self.plugin.input_lock.locked())
        self.assertNotIn('A', session.snapshot().assignments.values())

    def test_sessions_are_scoped_to_deck_page_and_group(self):
        a=self.action(); s=self.coordinator.session(a)
        self.assertIs(self.coordinator.session(a),s)
        a.get_settings.return_value={'group':'other'}
        self.assertIsNot(self.coordinator.session(a),s)

    def test_late_result_after_page_change_is_rejected(self):
        a=self.action(); s=self.coordinator.session(a); token=s.begin([1])
        self.coordinator.page_changed(self.deck,'/tmp/HD2.json','/tmp/Else.json')
        self.assertFalse(s.finish(token,{'status':'matched','rows':[{'id':'A'}]},self.plugin.stratagems))

    def test_busy_input_does_not_launch_scanner(self):
        a=self.action(); other=self.action(); self.plugin.input_lock.acquire()
        with patch.object(self.mod,'Thread') as thread:
            self.coordinator.start(a)
            thread.assert_not_called()
        a.show_error.assert_called_once_with(duration=3)
        other.show_error.assert_not_called()
        self.plugin.input_lock.release()

    def test_active_session_rejection_marks_only_initiator_busy(self):
        action = self.action()
        other = self.action()
        session = self.coordinator.session(action)
        session.begin({1: 'any'})

        with patch.object(self.mod, 'Thread') as thread:
            self.coordinator.start(action)

        thread.assert_not_called()
        action.show_error.assert_called_once_with(duration=3)
        other.show_error.assert_not_called()
        self.assertFalse(self.plugin.input_lock.locked())

    def test_setup_failures_release_input_and_allow_a_later_scan(self):
        action = self.action()
        slot = self.rendering_action(self.mod.AutomaticStratagem)
        self.coordinator.actions.add(slot)
        action.get_settings.side_effect = ValueError('invalid action settings')
        self.coordinator.start(action)
        self.assertFalse(self.plugin.input_lock.locked())

        action.get_settings.side_effect = None
        action.get_settings.return_value = {'slot': 1}
        with patch.object(self.mod, 'Thread') as thread:
            self.coordinator.start(action)
        thread.assert_called_once()
        self.assertTrue(self.plugin.input_lock.locked())
        self.plugin.input_lock.release()

    def test_persistence_failure_after_begin_releases_input(self):
        action = self.action()
        with patch.object(self.coordinator, 'persist', side_effect=RuntimeError('write failed')):
            self.coordinator.start(action)
        self.assertFalse(self.plugin.input_lock.locked())
        self.assertNotEqual(self.coordinator.session(action).snapshot().status, 'scanning')

    def test_thread_start_failure_releases_input(self):
        action = self.action()
        thread = Mock()
        thread.start.side_effect = RuntimeError('thread unavailable')
        with patch.object(self.mod, 'Thread', return_value=thread):
            self.coordinator.start(action)
        self.assertFalse(self.plugin.input_lock.locked())
        self.assertEqual(self.coordinator.session(action).snapshot().status, 'failed')

    def test_failed_main_context_queue_cancels_without_worker_render(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        action.render = Mock()
        action.on_ready_called = True
        self.coordinator.actions.add(action)
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan', return_value={'status': 'matched', 'rows': [{'id': 'A'}]}), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add', side_effect=RuntimeError('queue closed')):
            self.coordinator.start(action, replace=True)
            action.render.reset_mock()
            thread.call_args.kwargs['target']()
        self.assertFalse(self.plugin.input_lock.locked())
        self.assertEqual(self.coordinator.session(action).snapshot().status, 'idle')
        action.render.assert_not_called()

    def test_real_worker_process_finishes_through_queued_main_callback(self):
        from automatic_stratagems import scan_runner
        action = self.rendering_action(self.mod.AutomaticStratagem)
        action.on_ready_called = True
        self.coordinator.actions.add(action)
        report = {'schema_version': 1, 'status': 'matched',
                  'rows': [{'id': 'A', 'name': 'A', 'sequence': ['UP']}],
                  'warnings': []}
        command = [sys.executable, '-c', f'import json; print(json.dumps({report!r}))']
        queued = []
        with TemporaryDirectory() as directory, \
             patch.object(scan_runner, 'check_scan_setup'), \
             patch.object(scan_runner, 'resolve_scanner_runtime', return_value=Mock(
                 interpreter=Path(sys.executable), env={}, profile='native',
                 runtime_root=None)), \
             patch.object(scan_runner, 'scan_command', return_value=command), \
             patch.object(self.mod.scan_lifecycle, 'run_scan', side_effect=scan_runner.run_scan), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args: queued.append((callback, args)) or 1):
            self.plugin.PATH = directory
            self.coordinator.start(action, replace=True)
            self.wait_for_input_release()
        self.assertEqual(self.coordinator.session(action).snapshot().status, 'scanning')
        self.assertEqual(len(queued), 1)
        callback, args = queued.pop()
        callback(*args)
        self.assertEqual(self.coordinator.session(action).snapshot().assignments[1], 'A')

    def test_disable_cancels_and_reaps_real_worker_process(self):
        from automatic_stratagems import scan_runner
        action = self.rendering_action(self.mod.AutomaticStratagem)
        action.on_ready_called = True
        self.coordinator.actions.add(action)
        with TemporaryDirectory() as directory:
            child_pid = Path(directory) / 'scanner.pid'
            command = [sys.executable, '-c',
                       f'import os,pathlib,time; pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); time.sleep(60)']
            with patch.object(scan_runner, 'check_scan_setup'), \
                 patch.object(scan_runner, 'resolve_scanner_runtime', return_value=Mock(
                     interpreter=Path(sys.executable), env={}, profile='native',
                     runtime_root=None)), \
                 patch.object(scan_runner, 'scan_command', return_value=command), \
                 patch.object(self.mod.scan_lifecycle, 'run_scan', side_effect=scan_runner.run_scan), \
                 patch.object(self.mod.scan_lifecycle.GLib, 'idle_add', return_value=1):
                self.plugin.PATH = directory
                self.coordinator.start(action, replace=True)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not child_pid.exists():
                    time.sleep(.01)
                self.assertTrue(child_pid.exists())
                self.plugin.get_settings = lambda: {}
                self.coordinator.settings_changed()
                self.wait_for_input_release()
            pid = int(child_pid.read_text())
            self.assertFalse(Path(f'/proc/{pid}').exists())
            self.assertEqual(self.coordinator.session(action).snapshot().status, 'idle')

    def test_shutdown_cancels_joins_and_rejects_future_scans(self):
        from automatic_stratagems import scan_runner
        action = self.rendering_action(self.mod.AutomaticStratagem)
        action.on_ready_called = True
        self.coordinator.actions.add(action)
        with TemporaryDirectory() as directory:
            child_pid = Path(directory) / 'scanner.pid'
            command = [sys.executable, '-c',
                       f'import os,pathlib,time; pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); time.sleep(60)']
            with patch.object(scan_runner, 'check_scan_setup'), \
                 patch.object(scan_runner, 'resolve_scanner_runtime', return_value=Mock(
                     interpreter=Path(sys.executable), env={}, profile='native',
                     runtime_root=None)), \
                 patch.object(scan_runner, 'scan_command', return_value=command), \
                 patch.object(self.mod.scan_lifecycle, 'run_scan', side_effect=scan_runner.run_scan), \
                 patch.object(self.mod.scan_lifecycle.GLib, 'idle_add', return_value=1):
                self.plugin.PATH = directory
                self.coordinator.start(action, replace=True)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not child_pid.exists():
                    time.sleep(.01)
                self.assertTrue(child_pid.exists())
                self.assertTrue(self.coordinator.shutdown())
                self.assertFalse(self.plugin.input_lock.locked())
                with patch.object(self.mod, 'Thread') as thread:
                    self.coordinator.start(action, replace=True)
                thread.assert_not_called()
            pid = int(child_pid.read_text())
            self.assertFalse(Path(f'/proc/{pid}').exists())

    def test_shutdown_blocks_retained_assignment_execution(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        session = self.coordinator.session(action)
        token = session.begin({1: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems)
        action.displayed = (session.snapshot().revision, 1, 'A')
        self.coordinator.shutdown()
        with patch.object(self.mod, 'execute_stratagem') as execute:
            action.on_key_down()
            action.on_key_short_up()
        execute.assert_not_called()

    def test_disable_during_blocked_setup_prevents_worker_launch(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        action.on_ready_called = True
        self.coordinator.actions.add(action)
        entered = threading.Event()
        resume = threading.Event()
        original_persist = self.coordinator.persist

        def blocked_persist(*args):
            entered.set()
            self.assertTrue(resume.wait(3))
            original_persist(*args)

        scanner_thread = Mock()
        with patch.object(self.coordinator, 'persist', side_effect=blocked_persist), \
             patch.object(self.mod, 'Thread', return_value=scanner_thread):
            setup = threading.Thread(target=self.coordinator.start, args=(action,),
                                     kwargs={'replace': True})
            setup.start()
            self.assertTrue(entered.wait(3))
            self.assertIn(self.coordinator.context(action), self.coordinator.active_scans)
            self.plugin.get_settings = lambda: {}
            self.coordinator.settings_changed()
            resume.set()
            setup.join(3)
        self.assertFalse(setup.is_alive())
        scanner_thread.start.assert_not_called()
        self.assertFalse(self.plugin.input_lock.locked())

    def test_shutdown_waits_for_blocked_setup_to_abort(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        action.on_ready_called = True
        self.coordinator.actions.add(action)
        entered = threading.Event()
        resume = threading.Event()
        original_persist = self.coordinator.persist

        def blocked_persist(*args):
            entered.set()
            self.assertTrue(resume.wait(3))
            original_persist(*args)

        scanner_thread = Mock()
        result = []
        with patch.object(self.coordinator, 'persist', side_effect=blocked_persist), \
             patch.object(self.mod, 'Thread', return_value=scanner_thread):
            setup = threading.Thread(target=self.coordinator.start, args=(action,),
                                     kwargs={'replace': True})
            setup.start()
            self.assertTrue(entered.wait(3))
            shutdown = threading.Thread(
                target=lambda: result.append(self.coordinator.shutdown()))
            shutdown.start()
            operation = self.coordinator.active_scans[self.coordinator.context(action)]
            self.assertTrue(operation.cancel.wait(3))
            self.assertTrue(shutdown.is_alive())
            resume.set()
            setup.join(3)
            shutdown.join(3)
        self.assertEqual(result, [True])
        scanner_thread.start.assert_not_called()
        self.assertFalse(self.plugin.input_lock.locked())

    def test_disable_after_worker_exit_rejects_queued_completion_after_reenable(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        action.on_ready_called = True
        self.coordinator.actions.add(action)
        queued = []
        with patch.object(self.mod.scan_lifecycle, 'run_scan',
                          return_value={'status': 'matched', 'rows': [{'id': 'A'}]}), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args: queued.append((callback, args)) or 1):
            self.coordinator.start(action, replace=True)
            self.wait_for_input_release()
        self.assertEqual(self.coordinator.active_scans, {})
        self.plugin.get_settings = lambda: {}
        self.coordinator.settings_changed()
        self.plugin.get_settings = lambda: {'automatic_stratagems_enabled': True}
        self.coordinator.settings_changed()
        callback, args = queued.pop()
        callback(*args)
        self.assertNotIn('A', self.coordinator.session(action).snapshot().assignments.values())

    def test_automatic_ignores_stale_display_revision(self):
        a=self.mod.AutomaticStratagem.__new__(self.mod.AutomaticStratagem)
        a.plugin_base=self.plugin; self.plugin.scan_coordinator=self.coordinator
        a.deck_controller=self.deck; a.page=self.page
        a.get_settings=lambda:{'slot':1}; a.get_is_present=lambda:True
        a.has_custom_user_asset=lambda:False; a.has_image_control=lambda:True
        a.has_label_controls=lambda:[True]*3
        a.displayed=None
        s=self.coordinator.session(a); t=s.begin([1]); s.finish(t,{'status':'matched','rows':[{'id':'A'}]},self.plugin.stratagems)
        with patch.object(self.mod,'execute_stratagem') as execute:
            a.on_key_down(); a.on_key_short_up(); execute.assert_not_called()
            a.displayed=(s.snapshot().revision,1,'A'); a.on_key_down(); a.on_key_short_up()
            self.assertEqual(execute.call_args.args,(self.plugin,'A',['UP']))
            guard=execute.call_args.kwargs['guard']
            self.assertTrue(guard())
            s.cancel()
            self.assertFalse(guard())

    def test_host_update_forces_redraw_of_same_snapshot(self):
        a=self.mod.AutomaticStratagem.__new__(self.mod.AutomaticStratagem)
        a.displayed=(1,1,'A'); a.render=Mock()
        a.on_update()
        self.assertIsNone(a.displayed)
        a.render.assert_called_once()

    def test_scan_completion_updates_slots_and_releases_input(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.coordinator = self.mod.ScanCoordinator(self.plugin, state_dir=directory.name)
        self.deck = type('Deck', (), {'serial_number': lambda self: 'deck-one'})()
        a=self.action()
        a.get_settings.return_value={'capture_backend':'gamescope'}
        slot=self.mod.AutomaticStratagem.__new__(self.mod.AutomaticStratagem)
        slot.deck_controller=self.deck; slot.page=self.page; slot.plugin_base=self.plugin
        slot.get_settings=lambda:{'slot':1}; slot.get_is_present=lambda:True
        slot.on_ready_called=True; slot.render=Mock()
        self.coordinator.actions.add(slot)
        report={'status':'matched','rows':[{'id':'A'}]}
        with patch.object(self.mod,'Thread') as thread, patch.object(self.mod.scan_lifecycle,'run_scan',return_value=report) as scan, patch.object(self.mod.scan_lifecycle.GLib,'idle_add',side_effect=lambda f,*args:f(*args)):
            thread.side_effect=lambda **kw: types.SimpleNamespace(start=kw['target'])
            self.coordinator.start(a)
            scan.assert_called_once_with('/tmp/plugin', backend='gamescope', workers=2,
                                         cancel_event=ANY)
        self.assertEqual(self.coordinator.session(a).snapshot().assignments[1],'A')
        self.assertFalse(self.plugin.input_lock.locked())
        self.assertEqual(slot.render.call_count,2)
        saved = json.loads(self.coordinator.store.path(self.coordinator.identity(a)).read_text())
        self.assertEqual(saved['slots']['1']['id'], 'A')
        self.assertEqual(saved['status'], 'ready')
        self.assertIsNotNone(saved['last_scan_at'])

    def test_scanner_failure_releases_input_and_retains_old_results(self):
        a=self.action()
        slot=self.mod.AutomaticStratagem.__new__(self.mod.AutomaticStratagem)
        slot.deck_controller=self.deck; slot.page=self.page; slot.plugin_base=self.plugin
        slot.get_settings=lambda:{'slot':1}; slot.get_is_present=lambda:True
        slot.on_ready_called=True; slot.render=Mock()
        self.coordinator.actions.add(slot)
        s=self.coordinator.session(a); t=s.begin([1]); s.finish(t,{'status':'matched','rows':[{'id':'A'}]},self.plugin.stratagems)
        with patch.object(self.mod,'Thread') as thread, patch.object(self.mod.scan_lifecycle,'run_scan',side_effect=ValueError('Focus game')), patch.object(self.mod.scan_lifecycle.GLib,'idle_add',side_effect=lambda f,*args:f(*args)):
            thread.side_effect=lambda **kw: types.SimpleNamespace(start=kw['target'])
            self.coordinator.start(a)
        self.assertEqual(s.snapshot().status,'failed')
        self.assertEqual(s.snapshot().assignments[1],'A')
        self.assertFalse(self.plugin.input_lock.locked())

    def test_image_report_is_validated_without_persisting_history(self):
        action, root = self.temporary_setup()
        settings = {'group': 'HD2', 'scan_mode': 'new_page',
                    'capture_backend': 'screenshot'}
        self.plugin_settings['screenshot_folder'] = str(root / 'captures')

        def set_settings(updated):
            settings.clear()
            settings.update(updated)

        action.get_settings.side_effect = lambda: dict(settings)
        action.set_settings.side_effect = set_settings
        metadata = {
            'kind': 'file', 'selection': 'folder',
            'path': str(root / 'captures' / 'latest.png'),
            'mtime_ns': 123, 'size_bytes': 456, 'fingerprint': 'a' * 64,
        }
        report = {'status': 'no_detections', 'rows': [], 'source': metadata}

        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan', return_value=report) as scan, \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args: callback(*args)):
            thread.side_effect = lambda **kwargs: types.SimpleNamespace(
                start=kwargs['target'])
            self.coordinator.start(action, replace=True)

        scan.assert_called_once_with(
            self.plugin.PATH, backend='screenshot', workers=2,
            cancel_event=ANY,
            image_source={'kind': 'folder', 'path': str(root / 'captures'),
                          'trigger': 'hotkey', 'hotkey': 'KEY_F12',
                          'script': '', 'delete_after_scan': True,
                          'previous_fingerprint': None, 'allow_rescan': True})
        self.assertEqual(settings, {
            'group': 'HD2', 'scan_mode': 'new_page',
            'capture_backend': 'screenshot'})
        self.assertEqual(list((root / 'temporary-pages').glob('*.json')), [])

    def test_image_result_after_direct_source_change_is_rejected(self):
        action, root = self.temporary_setup()
        settings = {'group': 'HD2', 'scan_mode': 'new_page',
                    'capture_backend': 'screenshot'}
        self.plugin_settings['screenshot_folder'] = str(root)
        action.get_settings.side_effect = lambda: dict(settings)
        action.set_settings.side_effect = lambda updated: settings.update(updated)
        report = {
            'status': 'matched', 'rows': [{'id': 'A'}],
            'source': {'kind': 'file', 'selection': 'folder',
                       'path': str(root / 'first.png'), 'mtime_ns': 123,
                       'size_bytes': 456, 'fingerprint': 'a' * 64},
        }
        queued = []
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan', return_value=report), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                          side_effect=lambda callback, *args:
                          queued.append((callback, args)) or 1):
            self.coordinator.start(action, replace=True)
            thread.call_args.kwargs['target']()
            self.plugin_settings['screenshot_folder'] = str(root / 'other')
            callback, args = queued.pop()
            callback(*args)

        self.assertNotIn('image_source_last_fingerprint', settings)
        self.assertEqual(list((root / 'temporary-pages').glob('*.json')), [])
        self.assertFalse(self.plugin.input_lock.locked())

    def test_image_result_outside_selected_path_is_rejected(self):
        action, root = self.temporary_setup()
        settings = {
            'group': 'HD2', 'scan_mode': 'new_page',
            'capture_backend': 'steam', 'image_source_kind': 'folder',
            'image_source_path': str(root / 'selected'),
        }
        action.get_settings.side_effect = lambda: dict(settings)
        action.set_settings.side_effect = lambda updated: settings.update(updated)
        report = {
            'status': 'matched', 'rows': [{'id': 'A'}],
            'source': {'kind': 'file', 'selection': 'folder',
                       'path': str(root / 'other' / 'frame.png'),
                       'mtime_ns': 123, 'size_bytes': 456,
                       'fingerprint': 'a' * 64},
        }

        self.synchronous_scan(action, report)

        self.assertNotIn('image_source_last_fingerprint', settings)
        self.assertEqual(list((root / 'temporary-pages').glob('*.json')), [])
        self.assertEqual(action._scan_attempt.status, 'failed')


    def test_a_setup_failure_prepares_the_scanner_runtime_and_asks_for_a_retry(self):
        action = self.action()
        slot = self.rendering_action(self.mod.AutomaticStratagem)
        self.coordinator.actions.add(slot)
        prepared = {'state': 'installed', 'profile': 'native', 'error': None}

        with patch.object(self.mod.scan_lifecycle.runtime_preparation, 'ensure_scanner_runtime',
                          return_value=prepared) as ensure:
            self.synchronous_scan(
                action,
                error=self.mod.scan_lifecycle.ScanSetupError('Scanner Python is missing'))

        ensure.assert_called_once_with(
            self.plugin.PATH, {'automatic_stratagems_enabled': True})
        message = self.coordinator.session(action).snapshot().message
        self.assertIn('prepared', message)
        self.assertIn('again', message)

    def test_a_setup_failure_reports_a_failed_preparation(self):
        action = self.action()
        slot = self.rendering_action(self.mod.AutomaticStratagem)
        self.coordinator.actions.add(slot)
        prepared = {'state': 'error', 'profile': 'flatpak',
                    'error': 'Cannot download scanner runtime source'}

        with patch.object(self.mod.scan_lifecycle.runtime_preparation, 'ensure_scanner_runtime',
                          return_value=prepared):
            self.synchronous_scan(
                action, error=self.mod.scan_lifecycle.ScanSetupError('runtime is absent'))

        self.assertIn('Cannot download scanner runtime source',
                      self.coordinator.session(action).snapshot().message)

    def test_a_setup_failure_keeps_its_own_error_when_the_runtime_is_ready(self):
        action = self.action()
        slot = self.rendering_action(self.mod.AutomaticStratagem)
        self.coordinator.actions.add(slot)
        prepared = {'state': 'ready', 'profile': 'flatpak', 'error': None}

        with patch.object(self.mod.scan_lifecycle.runtime_preparation, 'ensure_scanner_runtime',
                          return_value=prepared):
            self.synchronous_scan(
                action,
                error=self.mod.scan_lifecycle.ScanSetupError(
                    'Capture prerequisites are unavailable'))

        self.assertEqual(self.coordinator.session(action).snapshot().message,
                         'Capture prerequisites are unavailable')

    def test_an_ordinary_scan_failure_does_not_prepare_the_scanner_runtime(self):
        action = self.action()
        slot = self.rendering_action(self.mod.AutomaticStratagem)
        self.coordinator.actions.add(slot)

        with patch.object(self.mod.scan_lifecycle.runtime_preparation, 'ensure_scanner_runtime') as ensure:
            self.synchronous_scan(action, error=RuntimeError('scanner crashed'))

        ensure.assert_not_called()
        self.assertEqual(self.coordinator.session(action).snapshot().message,
                         'scanner crashed')


if __name__ == '__main__':
    unittest.main()
