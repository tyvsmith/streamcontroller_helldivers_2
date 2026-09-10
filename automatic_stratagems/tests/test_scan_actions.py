import importlib
import json
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import ANY, Mock, patch

from .package_loader import plugin_module

class ActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gi = types.ModuleType('gi.repository')
        gi.GLib = Mock(); gi.Adw = Mock(); gi.Gtk = Mock()
        bases = types.ModuleType('src.backend.PluginManager.InputBases')
        bases.KeyAction = type('KeyAction', (), {})
        execution = types.ModuleType(plugin_module('stratagem_execution'))
        execution.execute_stratagem = Mock()
        with patch.dict(sys.modules, {'gi.repository':gi, 'src.backend.PluginManager.InputBases':bases,
                                     execution.__name__:execution, 'loguru':types.SimpleNamespace(logger=Mock())}):
            cls.mod = importlib.import_module(plugin_module('automatic_stratagems.scan_actions'))

    def setUp(self):
        self.plugin = types.SimpleNamespace(input_lock=threading.Lock(), stratagems={'A':['UP']},
                                           PATH='/tmp/plugin', get_settings=lambda:{'automatic_stratagems_enabled': True})
        self.coordinator = self.mod.ScanCoordinator(self.plugin)
        self.deck = object(); self.page = types.SimpleNamespace(json_path='/tmp/HD2.json')

    def action(self):
        a = Mock()
        a.plugin_base=self.plugin; a.deck_controller=self.deck; a.page=self.page
        a.get_settings.return_value={'slot':1}; a.get_is_present.return_value=True
        a.on_ready_called=True
        return a

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
        for cls in (self.mod.AutomaticStratagem, self.mod.ScanStratagems):
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
             patch.object(self.mod, 'catalog_colors', return_value={}), \
             patch.object(self.mod, 'run_scan', return_value={'status':'matched', 'rows':[{'id':'A'}]}), \
             patch.object(self.mod.GLib, 'idle_add', side_effect=lambda f,*args:f(*args)):
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
        a=self.action(); self.plugin.input_lock.acquire()
        with patch.object(self.mod,'Thread') as thread:
            self.coordinator.start(a)
            thread.assert_not_called()
        self.plugin.input_lock.release()

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
             patch.object(self.mod, 'run_scan', return_value={'status': 'matched', 'rows': [{'id': 'A'}]}), \
             patch.object(self.mod.GLib, 'idle_add', side_effect=RuntimeError('queue closed')):
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
        report = {'schema_version': 1, 'phase': 1, 'status': 'matched',
                  'rows': [{'id': 'A', 'name': 'A', 'sequence': ['UP']}],
                  'warnings': []}
        command = [sys.executable, '-c', f'import json; print(json.dumps({report!r}))']
        queued = []
        with TemporaryDirectory() as directory, \
             patch.object(scan_runner, 'check_scan_setup'), \
             patch.object(scan_runner, 'scan_command', return_value=command), \
             patch.object(self.mod, 'run_scan', side_effect=scan_runner.run_scan), \
             patch.object(self.mod.GLib, 'idle_add',
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
                 patch.object(scan_runner, 'scan_command', return_value=command), \
                 patch.object(self.mod, 'run_scan', side_effect=scan_runner.run_scan), \
                 patch.object(self.mod.GLib, 'idle_add', return_value=1):
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
                 patch.object(scan_runner, 'scan_command', return_value=command), \
                 patch.object(self.mod, 'run_scan', side_effect=scan_runner.run_scan), \
                 patch.object(self.mod.GLib, 'idle_add', return_value=1):
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
            self.assertTrue(operation['cancel'].wait(3))
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
        with patch.object(self.mod, 'run_scan',
                          return_value={'status': 'matched', 'rows': [{'id': 'A'}]}), \
             patch.object(self.mod.GLib, 'idle_add',
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

    def wait_for_input_release(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.plugin.input_lock.locked():
            time.sleep(.01)
        self.assertFalse(self.plugin.input_lock.locked())

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
        a.get_settings.return_value={'capture_backend':'steam'}
        slot=self.mod.AutomaticStratagem.__new__(self.mod.AutomaticStratagem)
        slot.deck_controller=self.deck; slot.page=self.page; slot.plugin_base=self.plugin
        slot.get_settings=lambda:{'slot':1}; slot.get_is_present=lambda:True
        slot.on_ready_called=True; slot.render=Mock()
        self.coordinator.actions.add(slot)
        report={'status':'matched','rows':[{'id':'A'}]}
        with patch.object(self.mod,'Thread') as thread, patch.object(self.mod,'run_scan',return_value=report) as scan, patch.object(self.mod.GLib,'idle_add',side_effect=lambda f,*args:f(*args)):
            thread.side_effect=lambda **kw: types.SimpleNamespace(start=kw['target'])
            self.coordinator.start(a)
            scan.assert_called_once_with('/tmp/plugin', backend='steam', workers=2,
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
        with patch.object(self.mod,'Thread') as thread, patch.object(self.mod,'run_scan',side_effect=ValueError('Focus game')), patch.object(self.mod.GLib,'idle_add',side_effect=lambda f,*args:f(*args)):
            thread.side_effect=lambda **kw: types.SimpleNamespace(start=kw['target'])
            self.coordinator.start(a)
        self.assertEqual(s.snapshot().status,'failed')
        self.assertEqual(s.snapshot().assignments[1],'A')
        self.assertFalse(self.plugin.input_lock.locked())

    def test_button_backend_overrides_plugin_default(self):
        a=self.action()
        self.plugin.get_settings=lambda:{'capture_backend':'gamescope'}
        self.assertEqual(self.mod.capture_backend(a), 'gamescope')
        for backend in ('auto','gamescope','steam','desktop','portal','x11'):
            a.get_settings.return_value={'capture_backend':backend}
            self.assertEqual(self.mod.capture_backend(a), backend)
        a.get_settings.return_value={'capture_backend':'invalid'}
        self.assertEqual(self.mod.capture_backend(a), 'auto')

    def test_dropdown_saves_os_as_desktop_per_button(self):
        a=self.mod.ScanStratagems.__new__(self.mod.ScanStratagems)
        a.plugin_base=self.plugin
        self.plugin.scan_coordinator=self.coordinator
        a.deck_controller=self.deck; a.page=self.page
        a.get_settings=lambda:{'capture_backend':'steam'}
        a.configure=Mock()
        row = Mock()
        with patch.object(self.mod.Adw,'ComboRow', return_value=row):
            rows=a.get_config_rows()
            row.set_selected.assert_called_once_with(2)
            self.assertIn(row, rows)
            callback=row.connect.call_args.args[1]
            row.get_selected.return_value=3
            callback(row,None)
            a.configure.assert_called_once_with('capture_backend','desktop')

    def test_scan_tap_and_hold_are_mutually_exclusive(self):
        a=self.mod.ScanStratagems.__new__(self.mod.ScanStratagems)
        a.plugin_base=self.plugin
        self.plugin.scan_coordinator=Mock()
        a.get_settings=lambda:{}
        start=self.plugin.scan_coordinator.start
        a.on_key_down()
        start.assert_not_called()
        a.on_key_short_up()
        start.assert_called_once_with(a, replace=True)
        start.reset_mock()
        a.on_key_down()
        a.on_key_hold_start()
        a.on_key_hold_start()
        a.on_key_up()
        a.on_key_short_up()
        a.on_key_hold_stop()
        start.assert_not_called()
        self.plugin.scan_coordinator.clear.assert_called_once_with(a)

    def test_auto_stratagems_tap_opens_cache_or_starts_fresh_scan(self):
        action = self.rendering_action(self.mod.AutoStratagems)
        action.on_key_down()
        self.coordinator.open_cached_page = Mock(return_value=False)
        self.coordinator.start = Mock()
        action.on_key_short_up()
        self.coordinator.open_cached_page.assert_called_once_with(action)
        self.coordinator.start.assert_called_once_with(action, replace=True)

        self.coordinator.open_cached_page.reset_mock()
        self.coordinator.open_cached_page.return_value = True
        self.coordinator.start.reset_mock()
        action.on_key_down()
        action.on_key_short_up()
        self.coordinator.open_cached_page.assert_called_once_with(action)
        self.coordinator.start.assert_not_called()

    def test_cache_lookup_error_does_not_start_another_scan(self):
        action = self.rendering_action(self.mod.AutoStratagems)
        action.show_error = Mock()
        self.coordinator.cached_page = Mock(side_effect=PermissionError('unreadable'))
        self.coordinator.start = Mock()
        action.on_key_down()
        action.on_key_short_up()
        self.coordinator.start.assert_not_called()
        action.show_error.assert_called_once()

    def test_auto_stratagems_hold_regenerates_once_without_clearing_source(self):
        action = self.rendering_action(self.mod.AutoStratagems)
        self.coordinator.start = Mock()
        self.coordinator.clear = Mock()
        action.on_key_down()
        action.on_key_hold_start()
        action.on_key_hold_start()
        action.on_key_short_up()
        self.coordinator.start.assert_called_once_with(
            action, replace=True, regenerate=True)
        self.coordinator.clear.assert_not_called()

    def test_legacy_new_page_scanner_dispatches_to_cached_behavior(self):
        action = self.rendering_action(
            self.mod.ScanStratagems, {'scan_mode': 'new_page'})
        self.coordinator.open_cached_page = Mock(return_value=True)
        self.coordinator.start = Mock()
        action.on_key_down()
        action.on_key_short_up()
        self.coordinator.open_cached_page.assert_called_once_with(action)
        self.coordinator.start.assert_not_called()
        action.on_key_down()
        action.on_key_hold_start()
        self.coordinator.start.assert_called_once_with(
            action, replace=True, regenerate=True)

    def test_uncertain_icon_is_badged_and_remains_executable_after_failure(self):
        a=self.mod.AutomaticStratagem.__new__(self.mod.AutomaticStratagem)
        a.plugin_base=self.plugin; self.plugin.scan_coordinator=self.coordinator
        self.plugin.lm=Mock(); self.plugin.lm.get.return_value=''
        self.plugin.get_show_labels=lambda:False
        a.deck_controller=self.deck; a.page=self.page
        a.get_settings=lambda:{'slot':1}; a.get_is_present=lambda:True
        a.has_custom_user_asset=lambda:False; a.has_image_control=lambda:True
        a.has_label_controls=lambda:[True]*3
        a.displayed=None
        for name in ['set_media','set_top_label','set_center_label','set_bottom_label','set_background_color']:
            setattr(a,name,Mock())
        s=self.coordinator.session(a)
        token=s.begin([1]); s.finish(token,{'status':'matched','rows':[{'id':'A'}]},self.plugin.stratagems)
        token=s.begin([1]); s.finish(token,{'status':'partial','rows':[{'id':None}]},self.plugin.stratagems)
        token=s.begin([1],replace=True); s.fail(token,'capture failed')
        with patch.object(self.mod,'badged_icon') as badge, patch.object(self.mod,'execute_stratagem') as execute:
            a.render()
            badge.assert_called_once_with('/tmp/plugin/assets/icons/A.png')
            a.on_key_down()
            a.on_key_short_up()
            execute.assert_called_once()
            self.assertTrue(execute.call_args.kwargs['guard']())

    def test_color_filter_is_per_slot(self):
        a=self.mod.AutomaticStratagem.__new__(self.mod.AutomaticStratagem)
        a.get_settings=lambda:{'color_filter':'red'}
        self.assertEqual(a.color_filter(),'red')
        a.get_settings=lambda:{'color_filter':'invalid'}
        self.assertEqual(a.color_filter(),'any')

    def rendering_action(self, action_type, settings=None):
        action = action_type.__new__(action_type)
        action.plugin_base = self.plugin
        self.plugin.scan_coordinator = self.coordinator
        action.deck_controller = self.deck
        action.page = self.page
        values = dict({'slot': 1}
                      if settings is None and action_type is self.mod.AutomaticStratagem
                      else settings or {})
        action.get_settings = lambda: dict(values)
        action.set_settings = lambda updated: values.update(updated)
        action.get_is_present = lambda: True
        action.has_custom_user_asset = lambda: False
        action.has_image_control = lambda: True
        action.has_label_controls = lambda: [True] * 3
        action.displayed = None
        for name in ('set_media', 'set_top_label', 'set_center_label',
                     'set_bottom_label', 'set_background_color'):
            setattr(action, name, Mock())
        return action

    def page_automatic_action(self, coordinate, settings=None, *, state=0, index=0):
        """Create an Auto action backed by the same topology as beta.15 Page."""
        if not hasattr(self.page, 'dict'):
            self.page.dict = {'keys': {}}
            self.page.action_objects = {'keys': {}}
        identifier = f'{coordinate[0]}x{coordinate[1]}'
        key = self.page.dict['keys'].setdefault(identifier, {'states': {}})
        state_data = key['states'].setdefault(str(state), {'actions': []})
        while len(state_data['actions']) <= index:
            state_data['actions'].append({'id': 'other::Action', 'settings': {}})
        values = dict(settings or {})
        state_data['actions'][index] = {
            'id': 'net_jslay_helldivers_2::AutomaticStratagem',
            'settings': values,
        }
        action = self.rendering_action(self.mod.AutomaticStratagem, values)
        action.input_ident = types.SimpleNamespace(
            input_type='keys', json_identifier=identifier, coords=coordinate)
        action.state = state
        action.action_id = 'net_jslay_helldivers_2::AutomaticStratagem'
        objects = self.page.action_objects['keys'].setdefault(identifier, {})
        objects.setdefault(state, {})[index] = action
        action.get_settings = lambda: dict(values)

        def set_settings(updated):
            values.update(updated)
            state_data['actions'][index]['settings'] = dict(values)

        action.set_settings = set_settings
        action.render = Mock()
        return action

    def test_two_default_auto_actions_scan_with_topology_allocated_slots(self):
        later = self.page_automatic_action((3, 1))
        earlier = self.page_automatic_action((4, 0))
        self.coordinator.register_action(later)
        self.coordinator.register_action(earlier)

        self.assertEqual(earlier.slot(), 1)
        self.assertEqual(later.slot(), 2)
        with patch.object(self.mod, 'Thread') as thread:
            self.coordinator.start(later, replace=True)
        thread.assert_called_once()
        self.assertEqual(
            set(self.coordinator.session(later).snapshot().assignments), {1, 2})
        self.plugin.input_lock.release()

    def test_default_auto_without_usable_topology_never_uses_slot_one(self):
        action = self.rendering_action(self.mod.AutomaticStratagem, {})
        action.show_error = Mock()
        action.on_ready_called = True
        self.coordinator.actions.add(action)
        session = self.coordinator.session(action)
        token = session.begin({1: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems)

        self.assertIsNone(action.slot())
        action.render()
        self.assertEqual(action.set_center_label.call_args.args[0], '?')
        with patch.object(self.mod, 'execute_stratagem') as execute, \
             patch.object(self.mod, 'Thread') as thread:
            action.on_key_down()
            action.on_key_short_up()

        execute.assert_not_called()
        thread.assert_not_called()
        action.show_error.assert_called_once_with(duration=3)
        self.assertEqual(session.snapshot().assignments[1], 'A')

    def test_current_page_scan_rejects_present_unresolved_default_auto(self):
        scanner = self.rendering_action(self.mod.ScanStratagems)
        scanner.show_error = Mock()
        unresolved = self.rendering_action(self.mod.AutomaticStratagem, {})
        unresolved.show_error = Mock()
        self.coordinator.actions.update((scanner, unresolved))

        with patch.object(self.mod, 'Thread') as thread:
            self.coordinator.start(scanner, replace=True)

        thread.assert_not_called()
        scanner.show_error.assert_called_once_with(duration=3)
        unresolved.show_error.assert_not_called()
        self.assertFalse(self.plugin.input_lock.locked())

    def test_default_auto_malformed_topology_fails_closed_without_render_error(self):
        malformed = (
            ({'keys': []}, {'keys': {}}),
            ({'keys': {'-1x0': {'states': {'0': {'actions': [
                {'id': self.mod.AUTOMATIC_ACTION_ID, 'settings': {}}]}}}}},
             {'keys': {'-1x0': {0: {0: None}}}}),
            ({'keys': {'0x0': {'states': {'-1': {'actions': [
                {'id': self.mod.AUTOMATIC_ACTION_ID, 'settings': {}}]}}}}},
             {'keys': {'0x0': {-1: {0: None}}}}),
            ({'keys': {'0x0': {'states': {'0': {'actions': [
                {'id': self.mod.AUTOMATIC_ACTION_ID, 'settings': {}}]}}}}},
             {'keys': []}),
        )
        for data, objects in malformed:
            with self.subTest(data=data, objects=objects):
                action = self.rendering_action(self.mod.AutomaticStratagem, {})
                action.page = types.SimpleNamespace(
                    json_path='/tmp/HD2.json', dict=data, action_objects=objects)
                action.render()
                self.assertIsNone(action.slot())
                self.assertEqual(action.set_center_label.call_args.args[0], '?')

    def test_malformed_topology_does_not_erase_saved_assignments(self):
        action = self.rendering_action(self.mod.AutomaticStratagem, {})
        action.page = types.SimpleNamespace(
            json_path='/tmp/HD2.json',
            dict={'keys': []},
            action_objects={'keys': {}},
        )
        session = self.coordinator.session(action)
        token = session.begin({1: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems)
        before = session.snapshot()

        self.coordinator.register_action(action)

        self.assertIsNone(action.slot())
        self.assertEqual(session.snapshot(), before)

    def test_malformed_sibling_keeps_explicit_slot_reconcile_non_authoritative(self):
        action = self.page_automatic_action((0, 0), {'slot': 1})
        self.page.dict['keys']['invalid-coordinate'] = {'states': {'0': {'actions': [{
            'id': self.mod.AUTOMATIC_ACTION_ID, 'settings': {'slot': 2}}]}}}
        self.page.action_objects['keys']['invalid-coordinate'] = {0: {0: Mock()}}
        self.plugin.stratagems['B'] = ['DOWN']
        session = self.coordinator.session(action)
        token = session.begin({1: 'any', 2: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}, {'id': 'B'}]},
                       self.plugin.stratagems)

        self.coordinator.register_action(action)

        self.assertEqual(action.slot(), 1)
        self.assertEqual(dict(session.snapshot().assignments), {1: 'A', 2: 'B'})

    def test_oversized_topology_number_keeps_explicit_reconcile_non_authoritative(self):
        action = self.page_automatic_action((0, 0), {'slot': 1})
        identifier = f'{"9" * 5000}x0'
        self.page.dict['keys'][identifier] = {'states': {'0': {'actions': [{
            'id': self.mod.AUTOMATIC_ACTION_ID, 'settings': {'slot': 2}}]}}}
        self.page.action_objects['keys'][identifier] = {0: {0: Mock()}}
        self.plugin.stratagems['B'] = ['DOWN']
        session = self.coordinator.session(action)
        token = session.begin({1: 'any', 2: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}, {'id': 'B'}]},
                       self.plugin.stratagems)

        self.coordinator.register_action(action)

        self.assertEqual(action.slot(), 1)
        self.assertEqual(dict(session.snapshot().assignments), {1: 'A', 2: 'B'})

    def test_default_auto_rejects_noncanonical_state_even_with_matching_object(self):
        action = self.rendering_action(self.mod.AutomaticStratagem, {})
        action.page = types.SimpleNamespace(
            json_path='/tmp/HD2.json',
            dict={'keys': {'0x0': {'states': {True: {'actions': [
                {'id': self.mod.AUTOMATIC_ACTION_ID, 'settings': {}}]}}}}},
            action_objects={'keys': {'0x0': {1: {0: action}}}})

        self.assertIsNone(action.slot())

    def test_explicit_slot_works_without_page_topology(self):
        action = self.rendering_action(self.mod.AutomaticStratagem, {'slot': 7})
        action.show_error = Mock()
        self.coordinator.actions.add(action)

        self.assertEqual(action.slot(), 7)
        with patch.object(self.mod, 'Thread') as thread:
            self.coordinator.start(action, replace=True)

        thread.assert_called_once()
        self.assertEqual(set(self.coordinator.session(action).snapshot().assignments), {7})
        self.plugin.input_lock.release()

    def test_automatic_allocation_reserves_explicit_slots(self):
        automatic_first = self.page_automatic_action((0, 0))
        explicit_later = self.page_automatic_action((1, 0), {'slot': 1})
        automatic_last = self.page_automatic_action((2, 0), {'slot': -1})

        self.assertEqual(explicit_later.configured_slot(), 1)
        self.assertEqual(explicit_later.slot(), 1)
        self.assertEqual(automatic_first.slot(), 2)
        self.assertEqual(automatic_last.slot(), 3)

    def test_automatic_allocation_is_independent_of_registration_and_json_order(self):
        bottom = self.page_automatic_action((3, 1))
        top = self.page_automatic_action((4, 0))
        self.page.dict['keys'] = dict(reversed(list(self.page.dict['keys'].items())))

        self.coordinator.register_action(bottom)
        self.coordinator.register_action(top)
        first = top.slot(), bottom.slot()
        self.coordinator.actions.clear()
        self.coordinator.register_action(top)
        self.coordinator.register_action(bottom)

        self.assertEqual(first, (1, 2))
        self.assertEqual((top.slot(), bottom.slot()), first)

    def test_automatic_allocation_orders_states_then_action_indexes(self):
        later_state = self.page_automatic_action((0, 0), state=1)
        later_action = self.page_automatic_action((0, 0), state=0, index=1)
        first_action = self.page_automatic_action((0, 0), state=0, index=0)

        self.assertEqual((first_action.slot(), later_action.slot(), later_state.slot()),
                         (1, 2, 3))

    def test_automatic_allocation_reacts_to_group_and_slot_configuration(self):
        first = self.page_automatic_action((0, 0), {'group': 'alpha'})
        second = self.page_automatic_action((1, 0), {'group': 'alpha'})
        other_group = self.page_automatic_action((2, 0), {'group': 'bravo'})
        self.coordinator.actions.update((first, second, other_group))

        self.assertEqual((first.slot(), second.slot(), other_group.slot()), (1, 2, 1))
        second.configure('slot', 1)
        self.assertEqual((first.slot(), second.slot()), (2, 1))
        second.configure('slot', -1)
        second.configure('group', 'bravo')
        self.assertEqual((first.slot(), second.slot(), other_group.slot()), (1, 1, 2))

    def test_slot_row_uses_minus_one_and_skips_zero(self):
        action = self.page_automatic_action((0, 0))
        action.configure = Mock()
        row = Mock()
        with patch.object(self.mod.Adw.SpinRow, 'new_with_range', return_value=row) as create:
            action.get_config_rows()

        create.assert_called_once_with(-1, 99, 1)
        row.set_value.assert_called_once_with(-1)
        callback = row.connect.call_args.args[1]
        row.get_value.return_value = 0
        callback(row, None)
        row.set_value.assert_called_with(1)
        action.configure.assert_called_with('slot', 1)
        callback(row, None)
        row.set_value.assert_called_with(-1)
        action.configure.assert_called_with('slot', -1)

    def test_reallocation_after_press_rejects_stale_execution(self):
        first = self.page_automatic_action((0, 0))
        second = self.page_automatic_action((1, 0))
        self.coordinator.actions.update((first, second))
        session = self.coordinator.session(first)
        token = session.begin({1: 'any', 2: 'any'})
        self.plugin.stratagems['B'] = ['DOWN']
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}, {'id': 'B'}]},
                       self.plugin.stratagems)
        first.displayed = (session.snapshot().revision, 1, 'A')
        first.on_key_down()
        second.configure('slot', 1)

        with patch.object(self.mod, 'execute_stratagem') as execute:
            first.on_key_short_up()
        execute.assert_not_called()

    def test_added_and_removed_auto_actions_reconcile_shifted_slots(self):
        later = self.page_automatic_action((1, 0))
        self.coordinator.register_action(later)
        session = self.coordinator.session(later)
        token = session.begin({1: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems)

        earlier = self.page_automatic_action((0, 0))
        self.coordinator.register_action(earlier)
        self.assertEqual((earlier.slot(), later.slot()), (1, 2))
        self.assertEqual(dict(session.snapshot().assignments), {1: 'A', 2: None})

        del self.page.dict['keys']['0x0']
        del self.page.action_objects['keys']['0x0']
        self.coordinator.remove_action(earlier)
        self.assertEqual(later.slot(), 1)
        self.assertEqual(dict(session.snapshot().assignments), {1: 'A'})

    def test_removed_explicit_action_uses_its_last_resolved_slot(self):
        action = self.page_automatic_action((0, 0), {'slot': 7, 'group': 'squad'})
        self.coordinator.register_action(action)
        session = self.coordinator.session(action)
        token = session.begin({7: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems)

        del self.page.dict['keys']['0x0']
        del self.page.action_objects['keys']['0x0']
        action.get_settings = lambda: {}
        self.coordinator.remove_action(action)

        self.assertEqual(dict(session.snapshot().assignments), {})

    def test_cached_off_page_auto_actions_supply_report_slot_filters(self):
        first = self.page_automatic_action((4, 0))
        second = self.page_automatic_action((3, 1), {'color_filter': 'blue'})
        first.get_is_present = lambda: False
        second.get_is_present = lambda: False
        self.coordinator.actions.update((second, first))
        context = self.coordinator.context(first)

        self.assertEqual(self.coordinator.slot_filters(
            context, self.coordinator.session(first)), {1: 'any', 2: 'blue'})

    def test_auto_hold_scans_once_without_executing_even_when_empty(self):
        for assigned in (False, True):
            action = self.rendering_action(self.mod.AutomaticStratagem)
            session = self.coordinator.session(action)
            if assigned:
                token = session.begin([1])
                session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
                action.displayed = (session.snapshot().revision, 1, 'A')
            with patch.object(self.coordinator, 'start') as scan, patch.object(self.mod, 'execute_stratagem') as execute:
                action.on_key_down()
                execute.assert_not_called()
                action.on_key_hold_start()
                action.on_key_hold_start()
                action.on_key_short_up()
                action.on_key_hold_stop()
                scan.assert_called_once_with(action, replace=True)
                execute.assert_not_called()

    def test_empty_auto_short_tap_scans_from_completed_states(self):
        for status in ('idle', 'ready', 'partial', 'failed'):
            with self.subTest(status=status):
                action = self.rendering_action(
                    self.mod.AutomaticStratagem,
                    {'slot': 1, 'group': status, 'color_filter': 'red'})
                action.on_ready_called = True
                self.coordinator.register_action(action)
                session = self.coordinator.session(action)
                if status != 'idle':
                    token = session.begin({1: 'red'})
                    if status == 'failed':
                        session.fail(token, 'capture failed')
                    else:
                        report = ({'status': 'matched', 'rows': [{'id': 'A'}]}
                                  if status == 'ready' else
                                  {'status': 'partial', 'rows': [{'id': None}]})
                        session.finish(token, report, self.plugin.stratagems,
                                       {'A': 'blue'})
                self.assertEqual(session.snapshot().status, status)
                with patch.object(self.mod, 'badged_icon') as badge:
                    badge.return_value.copy.return_value = object()
                    action.render()

                with patch.object(self.coordinator, 'start') as scan, \
                     patch.object(self.mod, 'execute_stratagem') as execute:
                    action.on_key_down()
                    action.on_key_short_up()
                scan.assert_called_once_with(action, replace=True)
                execute.assert_not_called()

    def test_empty_auto_short_tap_rejects_stale_or_unsafe_release(self):
        cases = ('revision', 'context', 'filter', 'disabled', 'not_present',
                 'uncontrollable', 'scanning')
        for case in cases:
            with self.subTest(case=case):
                settings = {'slot': 1, 'group': case, 'color_filter': 'any'}
                action = self.rendering_action(self.mod.AutomaticStratagem)
                action.get_settings = lambda: dict(settings)
                action.set_settings = lambda value: settings.update(value)
                action.on_ready_called = True
                self.coordinator.register_action(action)
                session = self.coordinator.session(action)
                action.render()
                action.on_key_down()
                if case == 'revision':
                    token = session.begin({1: 'any'})
                    session.fail(token, 'changed')
                elif case == 'context':
                    settings['group'] = 'changed'
                elif case == 'filter':
                    settings['color_filter'] = 'red'
                elif case == 'disabled':
                    self.plugin.get_settings = lambda: {}
                elif case == 'not_present':
                    action.get_is_present = lambda: False
                elif case == 'uncontrollable':
                    action.has_image_control = lambda: False
                elif case == 'scanning':
                    session.begin({1: 'any'})

                with patch.object(self.coordinator, 'start') as scan, \
                     patch.object(self.mod, 'execute_stratagem') as execute:
                    action.on_key_short_up()
                scan.assert_not_called()
                execute.assert_not_called()
                self.plugin.get_settings = lambda: {
                    'automatic_stratagems_enabled': True}

    def test_auto_release_rejects_assignment_changed_since_press(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        session = self.coordinator.session(action)
        token = session.begin([1])
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
        action.displayed = (session.snapshot().revision, 1, 'A')
        with patch.object(self.mod, 'execute_stratagem') as execute:
            action.on_key_down()
            token = session.begin([1], replace=True)
            session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
            action.displayed = (session.snapshot().revision, 1, 'A')
            action.on_key_short_up()
            execute.assert_not_called()

    def test_automatic_execution_failure_shows_action_error(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        action.show_error = Mock()
        session = self.coordinator.session(action)
        token = session.begin({1: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems)
        action.displayed = (session.snapshot().revision, 1, 'A')
        with patch.object(self.mod, 'execute_stratagem', return_value=False):
            action.on_key_down()
            action.on_key_short_up()
        action.show_error.assert_called_once_with(duration=3)

    def test_busy_automatic_execution_does_not_show_failure(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        action.show_error = Mock()
        session = self.coordinator.session(action)
        token = session.begin({1: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems)
        action.displayed = (session.snapshot().revision, 1, 'A')
        action.on_key_down()
        self.plugin.input_lock.acquire()
        with patch.object(self.mod, 'execute_stratagem', return_value=False):
            action.on_key_short_up()
        self.plugin.input_lock.release()
        action.show_error.assert_not_called()

    def test_noninitiating_auto_keeps_assignment_during_scan(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        self.plugin.get_show_labels = lambda: False
        session = self.coordinator.session(action)
        token = session.begin([1])
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
        token = session.begin([1], replace=True)
        action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('/A.png'))
        action.on_tick()
        action.set_media.assert_called_once()
        session.fail(token, 'Capture failed')
        action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('/A.png'))

    def test_only_initiating_scanner_animates_and_auto_slots_stay_normal(self):
        initiator = self.rendering_action(self.mod.ScanStratagems)
        other = self.rendering_action(self.mod.ScanStratagems)
        automatic = self.rendering_action(self.mod.AutomaticStratagem)
        for action in (initiator, other, automatic):
            action.on_ready_called = True
            self.coordinator.actions.add(action)

        with patch.object(self.mod, 'Thread'):
            self.coordinator.start(initiator, replace=True)
        initiator.render(); other.render(); automatic.render()

        self.assertTrue(initiator.set_media.call_args.kwargs['media_path'].endswith(
            '/scanning.mp4'))
        self.assertTrue(other.set_media.call_args.kwargs['media_path'].endswith(
            '/scan-update.png'))
        self.assertTrue(automatic.set_media.call_args.kwargs['media_path'].endswith(
            '/auto-any.png'))
        self.plugin.input_lock.release()

    def test_auto_hold_initiator_animates_while_other_auto_stays_normal(self):
        initiator = self.rendering_action(self.mod.AutomaticStratagem, {'slot': 1})
        other = self.rendering_action(self.mod.AutomaticStratagem, {'slot': 2})
        for action in (initiator, other):
            action.on_ready_called = True
            self.coordinator.actions.add(action)

        with patch.object(self.mod, 'Thread'):
            self.coordinator.start(initiator, replace=True)
        initiator.render(); other.render()

        self.assertTrue(initiator.set_media.call_args.kwargs['media_path'].endswith(
            '/scanning.mp4'))
        self.assertTrue(other.set_media.call_args.kwargs['media_path'].endswith(
            '/auto-any.png'))
        self.plugin.input_lock.release()

    def test_loading_owner_survives_worker_finalizer_until_queued_finish(self):
        action = self.rendering_action(self.mod.ScanStratagems)
        automatic = self.rendering_action(self.mod.AutomaticStratagem)
        for registered in (action, automatic):
            registered.on_ready_called = True
            self.coordinator.actions.add(registered)
        queued = []
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod, 'run_scan',
                          return_value={'status': 'matched', 'rows': [{'id': 'A'}]}), \
             patch.object(self.mod.GLib, 'idle_add',
                          side_effect=lambda callback, *args: queued.append((callback, args)) or 1):
            self.coordinator.start(action, replace=True)
            thread.call_args.kwargs['target']()
            self.assertNotIn(self.coordinator.context(action), self.coordinator.active_scans)
            action.render()
            self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith(
                '/scanning.mp4'))
            callback, args = queued.pop()
            callback(*args)
            action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith(
            '/scan-update.png'))

    def test_scanner_center_stays_blank_for_failed_and_partial_results(self):
        action = self.rendering_action(self.mod.ScanStratagems)
        session = self.coordinator.session(action)
        for report in ({'status': 'no_detections', 'rows': []},
                       {'status': 'partial', 'rows': [{'id': None}]}):
            token = session.begin([1], replace=True)
            session.finish(token, report, self.plugin.stratagems)
            action.render()
            self.assertEqual(action.set_center_label.call_args.args[0], '')

    def test_hard_scan_failure_marks_only_initiating_button(self):
        initiator = self.rendering_action(self.mod.ScanStratagems)
        other = self.rendering_action(self.mod.ScanStratagems)
        automatic = self.rendering_action(self.mod.AutomaticStratagem)
        for action in (initiator, other, automatic):
            action.show_error = Mock()
            action.on_ready_called = True
            self.coordinator.actions.add(action)
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod, 'run_scan', return_value={
                 'status': 'no_detections', 'rows': []}), \
             patch.object(self.mod.GLib, 'idle_add', side_effect=lambda f, *args: f(*args)):
            self.coordinator.start(initiator, replace=True)
            thread.call_args.kwargs['target']()

        initiator.show_error.assert_called_once_with(duration=3)
        other.show_error.assert_not_called()
        automatic.show_error.assert_not_called()

    def test_duplicate_slots_mark_only_initiating_scanner(self):
        initiator = self.rendering_action(self.mod.ScanStratagems)
        other = self.rendering_action(self.mod.ScanStratagems)
        first = self.rendering_action(self.mod.AutomaticStratagem, {'slot': 1})
        second = self.rendering_action(self.mod.AutomaticStratagem, {'slot': 1})
        for action in (initiator, other, first, second):
            action.show_error = Mock()
            action.on_ready_called = True
            self.coordinator.actions.add(action)
        self.coordinator.start(initiator, replace=True)

        initiator.show_error.assert_called_once_with(duration=3)
        other.show_error.assert_not_called()
        first.show_error.assert_not_called()
        second.show_error.assert_not_called()
        self.assertFalse(self.plugin.input_lock.locked())

    def test_empty_partial_slots_badge_colored_placeholder_but_complete_do_not(self):
        action = self.rendering_action(self.mod.AutomaticStratagem, {'slot': 1})
        session = self.coordinator.session(action)
        with patch.object(self.mod, 'badged_icon') as badge:
            badge.return_value.copy.return_value = object()
            for color in ('any', 'red', 'blue', 'green', 'yellow'):
                action.get_settings = lambda color=color: {'slot': 1, 'color_filter': color}
                token = session.begin({1: color})
                session.finish(token, {'status': 'partial', 'rows': [{'id': None}]},
                               self.plugin.stratagems)
                action.render()
                self.assertTrue(badge.call_args.args[0].endswith(
                    f'/automatic_stratagems/assets/icons/auto-{color}.png'))
                self.assertEqual(action.set_center_label.call_args.args[0], '1')

            badge.reset_mock()
            token = session.begin({1: 'any', 2: 'any'}, replace=True)
            session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                           self.plugin.stratagems)
            action.get_settings = lambda: {'slot': 2, 'color_filter': 'any'}
            action.render()
            badge.assert_not_called()
            self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith(
                '/auto-any.png'))

    def test_configuration_change_after_complete_scan_does_not_add_badge(self):
        action = self.rendering_action(self.mod.AutomaticStratagem, {'slot': 2})
        session = self.coordinator.session(action)
        token = session.begin({1: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems)
        session.reconcile({1: 'any', 2: 'any'})
        with patch.object(self.mod, 'badged_icon') as badge:
            action.render()
        badge.assert_not_called()

    def test_cached_clear_preserves_source_and_other_group(self):
        launcher, root = self.temporary_setup()
        source = self.coordinator.session(launcher)
        token = source.begin([1])
        source.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                      self.plugin.stratagems)
        self.coordinator.persist(launcher, source)
        self.synchronous_scan(launcher, {'status': 'matched', 'rows': [{'id': 'A'}]})
        scan = self.temporary_scan_action(self.deck.active_page.json_path)
        other = self.action()
        other.get_settings.return_value = {'group': 'other'}
        separate = self.coordinator.session(other)
        token = separate.begin([1])
        separate.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
        before = separate.snapshot()
        self.coordinator.clear(scan)
        self.assertEqual(source.snapshot().assignments[1], 'A')
        self.assertIsNotNone(source.latest_report())
        self.assertFalse(any(self.coordinator.session(scan).snapshot().assignments.values()))
        self.assertEqual(separate.snapshot(), before)
        restored = self.mod.ScanCoordinator(self.plugin, state_dir=root / 'scan-state').session(launcher)
        self.assertEqual(restored.snapshot().assignments[1], 'A')
        self.assertIsNotNone(restored.latest_report())

    def test_scan_mode_selects_artwork_and_configuration_redraws(self):
        action = self.rendering_action(self.mod.ScanStratagems)
        action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('/automatic_stratagems/assets/icons/scan-update.png'))
        action.configure('scan_mode', 'new_page')
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('/automatic_stratagems/assets/icons/scan-new-page.png'))
        self.assertEqual(action.set_top_label.call_args.args[0], 'Auto')
        self.assertEqual(action.set_bottom_label.call_args.args[0], 'Stratagems')

    def test_clear_during_scan_rejects_result_without_unlocking_worker_early(self):
        action = self.rendering_action(self.mod.ScanStratagems)
        slot = self.rendering_action(self.mod.AutomaticStratagem)
        slot.on_ready_called = True
        self.coordinator.actions.add(slot)
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod, 'run_scan', return_value={'status': 'matched', 'rows': [{'id': 'A'}]}), \
             patch.object(self.mod.GLib, 'idle_add', side_effect=lambda f, *args: f(*args)):
            self.coordinator.start(action, replace=True)
            self.coordinator.clear(action)
            self.assertTrue(self.plugin.input_lock.locked())
            thread.call_args.kwargs['target']()
        session = self.coordinator.session(action)
        self.assertEqual(session.snapshot().status, 'idle')
        self.assertFalse(any(session.snapshot().assignments.values()))
        self.assertIsNone(session.latest_report())
        self.assertFalse(self.plugin.input_lock.locked())

    def test_scanning_loops_without_restarting_on_ticks_and_stops_on_failure(self):
        action = self.rendering_action(self.mod.ScanStratagems)
        session = self.coordinator.session(action)
        token = session.begin([1])
        action._scan_presentation = self.coordinator.context(action), session, token
        action.render()
        media = action.set_media.call_args.kwargs
        self.assertTrue(media['media_path'].endswith('/automatic_stratagems/assets/icons/scanning.mp4'))
        self.assertEqual((media['fps'], media['loop']), (30, True))
        action.on_tick()
        action.on_tick()
        action.set_media.assert_called_once()
        session.fail(token, 'Capture failed')
        action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('/automatic_stratagems/assets/icons/scan-update.png'))
        self.assertEqual(action.set_top_label.call_args.args[0], 'Scan')
        self.assertEqual(action.set_bottom_label.call_args.args[0], 'Stratagems')
        self.assertEqual(action.set_center_label.call_args.args[0], '')

    def test_scanning_video_wakes_cached_native_media_ticks(self):
        action = self.rendering_action(self.mod.ScanStratagems)
        wake = Mock()
        media_player = types.SimpleNamespace(_cached_needs_ticks=False, _wake_event=wake)
        action.deck_controller = types.SimpleNamespace(media_player=media_player)

        action.artwork('automatic_stratagems/assets/icons/scanning.mp4',
                       'Scan', '', 'Scanning')

        self.assertIs(media_player._cached_needs_ticks, True)
        wake.set.assert_called_once_with()

    def test_static_artwork_and_missing_media_tick_api_are_unchanged(self):
        action = self.rendering_action(self.mod.ScanStratagems)
        wake = Mock()
        media_player = types.SimpleNamespace(_cached_needs_ticks=False, _wake_event=wake)
        action.deck_controller = types.SimpleNamespace(media_player=media_player)

        action.artwork('automatic_stratagems/assets/icons/scan-update.png',
                       'Scan', '', 'Stratagems')
        self.assertIs(media_player._cached_needs_ticks, False)
        wake.set.assert_not_called()

        for media_player in (types.SimpleNamespace(),
                             types.SimpleNamespace(_cached_needs_ticks='false')):
            action.deck_controller = types.SimpleNamespace(media_player=media_player)
            action.artwork('automatic_stratagems/assets/icons/scanning.mp4',
                           'Scan', '', 'Scanning')

    def test_scan_completion_and_cancel_restore_static_artwork(self):
        for outcome in ('matched', 'partial', 'cancel'):
            with self.subTest(outcome=outcome):
                action = self.rendering_action(self.mod.ScanStratagems, {'group': outcome})
                session = self.coordinator.session(action)
                token = session.begin([1])
                action.render()
                if outcome == 'cancel':
                    session.cancel()
                else:
                    session.finish(token, {'status': outcome, 'rows': [{'id': 'A' if outcome == 'matched' else None}]}, self.plugin.stratagems)
                action.render()
                self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('.png'))

    def test_empty_auto_artwork_tracks_filter_and_keeps_slot_number(self):
        action = self.rendering_action(self.mod.AutomaticStratagem, {'slot': 12})
        for color in ('any', 'red', 'blue', 'green', 'yellow', 'invalid'):
            with self.subTest(color=color):
                action.configure('color_filter', color)
                expected = 'any' if color == 'invalid' else color
                self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith(f'/automatic_stratagems/assets/icons/auto-{expected}.png'))
                self.assertEqual(action.set_center_label.call_args.args[0], '12')

    def test_back_action_uses_existing_navigation_artwork(self):
        action = self.rendering_action(self.mod.TemporaryScanBack)
        action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('/_stepbakcward.png'))

    def test_back_remains_available_while_automatic_scanning_is_disabled(self):
        action = self.rendering_action(self.mod.TemporaryScanBack)
        self.plugin.get_settings = lambda: {}
        self.coordinator.temporary_pages = Mock()
        self.coordinator.back(action)
        self.coordinator.temporary_pages.back.assert_called_once_with(
            action.deck_controller, action.page.json_path)

    def test_back_failure_keeps_navigation_artwork_and_explains_error(self):
        action = self.rendering_action(self.mod.TemporaryScanBack)
        self.coordinator.temporary_pages = Mock()
        self.coordinator.temporary_pages.back.side_effect = RuntimeError('Source page missing')
        self.coordinator.back(action)
        self.assertEqual(action.back_error, 'Source page missing')
        self.assertEqual(action.set_top_label.call_args.args[0], 'Back failed')
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('/_stepbakcward.png'))
        self.coordinator.temporary_pages.back.side_effect = None
        self.coordinator.back(action)
        action.render()
        self.assertIsNone(action.back_error)
        self.assertEqual(action.set_bottom_label.call_args.args[0], 'Back')

    def test_assignment_replaces_auto_artwork_and_slot_number(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        self.plugin.get_show_labels = lambda: True
        self.plugin.lm = Mock()
        self.plugin.lm.get.return_value = 'Stratagem'
        action.render()
        session = self.coordinator.session(action)
        token = session.begin([1])
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
        action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('/icons/A.png'))
        action.set_center_label.assert_called_with('Stratagem')

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

    def temporary_setup(self):
        from .test_temporary_scan_page import Deck, PageManager
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / 'original.json'
        source.write_text('{"keys": {}}')
        self.page = types.SimpleNamespace(json_path=str(source))
        self.deck = Deck(source)
        self.coordinator = self.mod.ScanCoordinator(self.plugin, state_dir=root / 'scan-state')
        self.coordinator.enable_temporary_pages(PageManager())
        load = self.deck.load_page
        def switch(page):
            old_path = self.deck.active_page.json_path
            load(page)
            self.coordinator.page_changed(self.deck, old_path, page.json_path)
        self.deck.load_page = switch
        action = self.action()
        action.get_settings.return_value = {'group': 'HD2', 'scan_mode': 'new_page', 'capture_backend': 'steam'}
        action.get_is_present.side_effect = lambda: self.deck.active_page.json_path == action.page.json_path
        action.input_ident = types.SimpleNamespace(input_type='keys', json_identifier='4x0')
        action.state = 0
        self.page.action_objects = {'keys': {'4x0': {0: {0: action}}}}
        return action, root

    def second_page_opener(self, identifier='3x1'):
        action = self.action()
        action.get_settings.return_value = {
            'group': 'HD2', 'scan_mode': 'new_page', 'capture_backend': 'steam'}
        action.get_is_present.side_effect = (
            lambda: self.deck.active_page.json_path == action.page.json_path)
        action.input_ident = types.SimpleNamespace(
            input_type='keys', json_identifier=identifier)
        action.state = 0
        self.page.action_objects['keys'][identifier] = {0: {0: action}}
        return action

    def real_page_opener(self, identifier='4x0'):
        action = self.rendering_action(
            self.mod.AutoStratagems,
            {'group': 'HD2', 'capture_backend': 'steam'})
        action.show_error = Mock()
        action.input_ident = types.SimpleNamespace(
            input_type='keys', json_identifier=identifier)
        action.state = 0
        action.get_is_present = (
            lambda: self.deck.active_page.json_path == action.page.json_path)
        self.page.action_objects['keys'][identifier] = {0: {0: action}}
        self.coordinator.actions.add(action)
        return action

    def synchronous_scan(self, action, report=None, error=None, *, replace=False,
                         regenerate=False):
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod, 'run_scan', return_value=report, side_effect=error), \
             patch.object(self.mod.GLib, 'idle_add', side_effect=lambda f, *args: f(*args)):
            thread.side_effect = lambda **kw: types.SimpleNamespace(start=kw['target'])
            self.coordinator.start(action, replace=replace, regenerate=regenerate)

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
             patch.object(self.mod, 'run_scan', return_value={
                 'status': 'matched', 'rows': [{'id': 'B'}]}), \
             patch.object(self.mod.GLib, 'idle_add',
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

    def test_regenerate_delete_and_capture_failures_do_not_leave_replacement(self):
        action, root = self.temporary_setup()
        self.synchronous_scan(action, {'status': 'matched', 'rows': [{'id': 'A'}]})
        path = self.deck.active_page.json_path
        back = self.action(); back.page = types.SimpleNamespace(json_path=path)
        back.get_settings.return_value = {'group': 'HD2'}
        self.coordinator.back(back)
        with patch.object(self.coordinator.temporary_pages, 'discard',
                          side_effect=RuntimeError('delete failed')), \
             patch.object(self.mod, 'Thread') as thread:
            self.coordinator.start(action, replace=True, regenerate=True)
        thread.assert_not_called()
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
             patch.object(self.mod, 'run_scan', return_value=report), \
             patch.object(self.mod.GLib, 'idle_add',
                          side_effect=lambda callback, *args: queued.append((callback, args)) or 1):
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
            'status': 'partial', 'rows': [{'id': None}]})
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
            self.mod.AutoStratagems,
            {'group': 'HD2', 'capture_backend': 'steam'})
        action.show_error = Mock()
        with patch.object(self.mod, 'Thread') as thread:
            self.coordinator.start(action, replace=True)
        thread.assert_not_called()
        self.assertEqual(action._scan_attempt.status, 'failed')
        self.assertIn('Temporary pages are unavailable', action._scan_attempt.message)
        action.show_error.assert_called_once_with(duration=3)

        _, root = self.temporary_setup()
        action = self.real_page_opener()
        self.synchronous_scan(action, {'status': 'no_detections', 'rows': []})
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
            {'status': 'matched', 'rows': [{'id': 'A'}]},
            {'status': 'partial', 'rows': [{'id': None}]},
        ]
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod, 'run_scan', side_effect=reports), \
             patch.object(self.mod.GLib, 'idle_add',
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

    def test_scan_mode_selector_is_hidden_but_legacy_setting_is_preserved(self):
        a = self.mod.ScanStratagems.__new__(self.mod.ScanStratagems)
        a.plugin_base = self.plugin
        self.plugin.scan_coordinator = self.coordinator
        a.deck_controller = self.deck
        a.page = self.page
        a.get_settings = lambda: {}
        a.configure = Mock()
        backend_row = Mock()
        with patch.object(self.mod.Adw, 'ComboRow', return_value=backend_row) as create:
            a.get_config_rows()
        create.assert_called_once()
        self.assertEqual(self.mod.scan_mode(a), 'update')
        a.get_settings = lambda: {'scan_mode': 'new_page'}
        self.assertEqual(self.mod.scan_mode(a), 'new_page')

    def temporary_scan_action(self, path):
        data = json.loads(Path(path).read_text())
        entries = [value['states']['0']['actions'][0] for value in data['keys'].values()]
        scan = self.action()
        scan.page = types.SimpleNamespace(json_path=path)
        scan.get_settings.return_value = entries[1]['settings']
        scan.get_is_present.side_effect = lambda: self.deck.active_page.json_path == path
        for entry in entries[2:]:
            slot = self.mod.AutomaticStratagem.__new__(self.mod.AutomaticStratagem)
            slot.deck_controller = self.deck
            slot.page = scan.page
            slot.plugin_base = self.plugin
            slot.get_settings = lambda settings=entry['settings']: settings
            slot.get_is_present = lambda: self.deck.active_page.json_path == path
            slot.on_ready_called = True
            slot.render = Mock()
            # The real Page owns its actions; retain them while exercising its equivalent here.
            self.__dict__.setdefault('page_actions', []).append(slot)
            self.coordinator.actions.add(slot)
        return scan

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

    def test_back_during_scan_rejects_late_results_without_recreating_files(self):
        launcher, root = self.temporary_setup()
        report = {'status': 'matched', 'rows': [{'id': 'A'}]}
        self.synchronous_scan(launcher, report)
        path = self.deck.active_page.json_path
        scan = self.temporary_scan_action(path)
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod, 'run_scan', return_value=report), \
             patch.object(self.mod.GLib, 'idle_add', side_effect=lambda f, *args: f(*args)):
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

    def test_changing_mode_preserves_existing_source_assignments(self):
        action = self.mod.ScanStratagems.__new__(self.mod.ScanStratagems)
        action.plugin_base = self.plugin
        self.plugin.scan_coordinator = self.coordinator
        action.deck_controller = self.deck
        action.page = self.page
        settings = {}
        action.get_settings = lambda: dict(settings)
        action.set_settings = lambda value: settings.update(value)
        action.render = Mock()
        session = self.coordinator.session(action)
        token = session.begin([1])
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]}, self.plugin.stratagems)
        before = session.snapshot()
        action.configure('scan_mode', 'new_page')
        self.assertEqual(session.snapshot(), before)
        self.assertIs(self.coordinator.session(action), session)

    def test_cancelled_launch_does_not_open_page_when_result_arrives(self):
        launcher, root = self.temporary_setup()
        report = {'status': 'matched', 'rows': [{'id': 'A'}]}
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod, 'run_scan', return_value=report), \
             patch.object(self.mod.GLib, 'idle_add', side_effect=lambda f, *args: f(*args)):
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
        with patch.object(self.mod, 'catalog_colors', return_value=colors):
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

    def test_open_status_row_is_not_retained_for_worker_or_tick_mutation(self):
        action = self.rendering_action(self.mod.ScanStratagems)
        session = self.coordinator.session(action)
        token = session.begin([1])
        session.finish(token, {'status': 'partial', 'rows': [{'id': None}]}, self.plugin.stratagems)
        with patch.object(self.mod.Adw, 'ActionRow') as row:
            action.get_config_rows()
        self.assertNotIn('last_scan_row', action.__dict__)
        self.assertEqual(row.call_args.kwargs['subtitle'], session.snapshot().message)

    def test_disabled_ticks_reuse_static_artwork(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        self.plugin.get_settings = lambda: {}
        action.render()
        action.on_tick()
        action.on_tick()
        action.set_media.assert_called_once()

    def test_restored_assignment_is_cleared_when_filter_changes(self):
        with TemporaryDirectory() as directory:
            self.coordinator = self.mod.ScanCoordinator(self.plugin, state_dir=directory)
            self.deck = type('Deck', (), {'serial_number': lambda self: 'deck-one'})()
            action = self.rendering_action(
                self.mod.AutomaticStratagem, {'slot': 1, 'color_filter': 'red'})
            self.coordinator.actions.add(action)
            session = self.coordinator.session(action)
            token = session.begin({1: 'red'})
            session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                           self.plugin.stratagems, {'A': 'red'})
            self.coordinator.persist(action, session)

            restored = self.mod.ScanCoordinator(self.plugin, state_dir=directory)
            self.plugin.scan_coordinator = restored
            settings = {'slot': 1, 'color_filter': 'blue'}
            action.get_settings = lambda: dict(settings)
            action.set_settings = lambda value: settings.update(value)
            action.render = Mock()
            action.on_ready()
            self.assertIsNone(restored.session(action).snapshot().assignments[1])
            saved = json.loads(restored.store.path(restored.identity(action)).read_text())
            self.assertIsNone(saved['slots']['1']['id'])
            self.assertEqual(saved['slots']['1']['filter'], 'blue')

    def test_first_restored_action_does_not_discard_later_saved_slots(self):
        with TemporaryDirectory() as directory:
            self.coordinator = self.mod.ScanCoordinator(self.plugin, state_dir=directory)
            self.deck = type('Deck', (), {'serial_number': lambda self: 'deck-one'})()
            first = self.rendering_action(
                self.mod.AutomaticStratagem, {'slot': 1, 'color_filter': 'red'})
            second = self.rendering_action(
                self.mod.AutomaticStratagem, {'slot': 2, 'color_filter': 'blue'})
            session = self.coordinator.session(first)
            self.plugin.stratagems['B'] = ['DOWN']
            token = session.begin({1: 'red', 2: 'blue'})
            session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}, {'id': 'B'}]},
                           self.plugin.stratagems, {'A': 'red', 'B': 'blue'})
            self.coordinator.persist(first, session)

            restored = self.mod.ScanCoordinator(self.plugin, state_dir=directory)
            self.plugin.scan_coordinator = restored
            first.render = Mock()
            first.on_ready()
            self.assertEqual(dict(restored.session(first).snapshot().assignments),
                             {1: 'A', 2: 'B'})
            second.render = Mock()
            second.on_ready()
            self.assertEqual(dict(restored.session(second).snapshot().assignments),
                             {1: 'A', 2: 'B'})

    def test_page_setting_edit_is_reconciled_before_execution(self):
        settings = {'slot': 1, 'color_filter': 'red'}
        action = self.rendering_action(self.mod.AutomaticStratagem)
        action.get_settings = lambda: dict(settings)
        action.render = Mock()
        self.coordinator.register_action(action)
        session = self.coordinator.session(action)
        token = session.begin({1: 'red'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems, {'A': 'red'})
        action.displayed = (session.snapshot().revision, 1, 'A')
        settings['color_filter'] = 'blue'
        with patch.object(self.mod, 'execute_stratagem') as execute:
            action.on_key_down()
            action.on_key_short_up()
        execute.assert_not_called()
        self.assertIsNone(session.snapshot().assignments[1])

    def test_slot_change_preserves_unrelated_assignment(self):
        settings = {'slot': 1, 'color_filter': 'red'}
        action = self.rendering_action(self.mod.AutomaticStratagem)
        action.get_settings = lambda: dict(settings)
        action.set_settings = lambda value: settings.update(value)
        other = self.rendering_action(
            self.mod.AutomaticStratagem, {'slot': 2, 'color_filter': 'blue'})
        action.render = Mock()
        other.render = Mock()
        self.coordinator.actions.update((action, other))
        self.plugin.stratagems['B'] = ['DOWN']
        session = self.coordinator.session(action)
        token = session.begin({1: 'red', 2: 'blue'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}, {'id': 'B'}]},
                       self.plugin.stratagems, {'A': 'red', 'B': 'blue'})
        action.configure('slot', 3)
        self.assertEqual(dict(session.snapshot().assignments), {2: 'B', 3: None})

    def test_shared_slot_removal_cancels_scan_and_preserves_other_binding(self):
        self.plugin.stratagems['B'] = ['DOWN']
        first = self.rendering_action(
            self.mod.AutomaticStratagem, {'slot': 1, 'color_filter': 'any'})
        second = self.rendering_action(
            self.mod.AutomaticStratagem, {'slot': 2, 'color_filter': 'any'})
        scanner = self.rendering_action(self.mod.ScanStratagems)
        self.coordinator.actions.update((first, second, scanner))
        session = self.coordinator.session(scanner)
        token = session.begin({1: 'any', 2: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}, {'id': 'B'}]},
                       self.plugin.stratagems)
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod, 'run_scan', side_effect=self.mod.CancelledError):
            self.coordinator.start(scanner, replace=True)
            operation = self.coordinator.active_scans[self.coordinator.context(scanner)]
            self.coordinator.remove_action(first)
            self.assertTrue(operation['cancel'].is_set())
            self.assertEqual(dict(session.snapshot().assignments), {2: 'B'})
            thread.call_args.kwargs['target']()
        self.assertFalse(self.plugin.input_lock.locked())

    def test_deck_disconnect_preserves_other_deck_session(self):
        first_deck = self.deck
        first = self.action()
        first_session = self.coordinator.session(first)
        token = first_session.begin({1: 'any'})
        first_session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                             self.plugin.stratagems)
        second_deck = object()
        second = self.action()
        second.deck_controller = second_deck
        second_session = self.coordinator.session(second)
        token = second_session.begin({1: 'any'})
        second_session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                              self.plugin.stratagems)
        self.coordinator.deck_disconnected(first_deck)
        self.assertNotIn(self.coordinator.context(first), self.coordinator.sessions)
        self.assertEqual(second_session.snapshot().assignments[1], 'A')

    def test_returning_to_group_restores_only_matching_binding(self):
        with TemporaryDirectory() as directory:
            self.coordinator = self.mod.ScanCoordinator(self.plugin, state_dir=directory)
            self.deck = type('Deck', (), {'serial_number': lambda self: 'deck-one'})()
            settings = {'slot': 1, 'color_filter': 'red', 'group': 'red-team'}
            action = self.rendering_action(self.mod.AutomaticStratagem)
            action.get_settings = lambda: dict(settings)
            action.set_settings = lambda value: settings.update(value)
            action.render = Mock()
            self.coordinator.actions.add(action)
            session = self.coordinator.session(action)
            token = session.begin({1: 'red'})
            session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                           self.plugin.stratagems, {'A': 'red'})
            self.coordinator.persist(action, session)

            action.configure('group', 'other')
            action.configure('group', 'red-team')
            self.assertEqual(self.coordinator.session(action).snapshot().assignments[1], 'A')

            action.configure('group', 'other')
            action.configure('color_filter', 'blue')
            action.configure('group', 'red-team')
            self.assertIsNone(self.coordinator.session(action).snapshot().assignments[1])

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
        action = self.rendering_action(self.mod.AutoStratagems, {'group': 'HD2'})
        action.input_ident = types.SimpleNamespace(input_type='keys', json_identifier='3x1')
        action.state = 0
        self.page.action_objects['keys']['3x1'] = {0: {0: action}}
        action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith(
            '/automatic_stratagems/assets/icons/scan-new-page.png'))
        revision = self.coordinator.session(action).snapshot().revision

        path = self.coordinator.temporary_pages.create(
            self.deck, self.page.json_path, 'HD2', 'steam',
            self.mod._source_action_address(action))
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
             patch.object(self.mod, 'run_scan', return_value=report), \
             patch.object(self.mod.GLib, 'idle_add',
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
