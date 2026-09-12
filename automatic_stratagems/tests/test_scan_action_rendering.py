import types
import unittest
from unittest.mock import Mock, patch

from .action_test_support import ActionTestHarness


class ActionRenderingTests(ActionTestHarness, unittest.TestCase):
    def test_scan_tap_and_hold_are_mutually_exclusive(self):
        a=self.mod.AutomaticStratagemScanner.__new__(self.mod.AutomaticStratagemScanner)
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
        action = self.rendering_action(self.mod.AutomaticStratagemPage)
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
        action = self.rendering_action(self.mod.AutomaticStratagemPage)
        action.show_error = Mock()
        self.coordinator.cached_page = Mock(side_effect=PermissionError('unreadable'))
        self.coordinator.start = Mock()
        action.on_key_down()
        action.on_key_short_up()
        self.coordinator.start.assert_not_called()
        action.show_error.assert_called_once()

    def test_auto_stratagems_hold_regenerates_once_without_clearing_source(self):
        action = self.rendering_action(self.mod.AutomaticStratagemPage)
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
            self.mod.AutomaticStratagemScanner, {'scan_mode': 'new_page'})
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
                action.show_error = Mock()
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
                action.show_error.assert_not_called()
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

    def test_busy_automatic_execution_marks_only_pressed_action(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        other = self.rendering_action(self.mod.AutomaticStratagem)
        action.show_error = Mock()
        other.show_error = Mock()
        session = self.coordinator.session(action)
        token = session.begin({1: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems)
        action.displayed = (session.snapshot().revision, 1, 'A')
        action.on_key_down()
        self.plugin.input_lock.acquire()
        with patch.object(self.mod, 'execute_stratagem', return_value=False) as execute:
            action.on_key_short_up()
        self.plugin.input_lock.release()
        execute.assert_called_once()
        action.show_error.assert_called_once_with(duration=3)
        other.show_error.assert_not_called()

    def test_current_automatic_press_during_scan_marks_only_pressed_action(self):
        action = self.rendering_action(self.mod.AutomaticStratagem)
        other = self.rendering_action(self.mod.AutomaticStratagem)
        action.show_error = Mock()
        other.show_error = Mock()
        session = self.coordinator.session(action)
        token = session.begin({1: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems)
        session.begin({1: 'any'}, replace=True)
        snapshot = session.snapshot()
        action.displayed = (snapshot.revision, 1, 'A')

        action.on_key_down()
        with patch.object(self.coordinator, 'start') as start, \
             patch.object(self.mod, 'execute_stratagem') as execute:
            action.on_key_short_up()

        start.assert_not_called()
        execute.assert_not_called()
        action.show_error.assert_called_once_with(duration=3)
        other.show_error.assert_not_called()

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
        initiator = self.rendering_action(self.mod.AutomaticStratagemScanner)
        other = self.rendering_action(self.mod.AutomaticStratagemScanner)
        automatic = self.rendering_action(self.mod.AutomaticStratagem)
        for action in (initiator, other, automatic):
            action.on_ready_called = True
            self.coordinator.actions.add(action)

        with patch.object(self.mod, 'Thread'):
            self.coordinator.start(initiator, replace=True)
        initiator.render(); other.render(); automatic.render()

        self.assertEqual(initiator.set_media.call_args.kwargs['image'].mode, 'RGBA')
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

        self.assertEqual(initiator.set_media.call_args.kwargs['image'].mode, 'RGBA')
        self.assertTrue(other.set_media.call_args.kwargs['media_path'].endswith(
            '/auto-any.png'))
        self.plugin.input_lock.release()

    def test_loading_owner_survives_worker_finalizer_until_queued_finish(self):
        action = self.rendering_action(self.mod.AutomaticStratagemScanner)
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
            self.assertEqual(action.set_media.call_args.kwargs['image'].mode, 'RGBA')
            callback, args = queued.pop()
            callback(*args)
            action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith(
            '/scan-update.png'))

    def test_scanner_center_stays_blank_for_failed_and_partial_results(self):
        action = self.rendering_action(self.mod.AutomaticStratagemScanner)
        session = self.coordinator.session(action)
        for report in ({'status': 'no_detections', 'rows': []},
                       {'status': 'partial', 'rows': [{'id': None}]}):
            token = session.begin([1], replace=True)
            session.finish(token, report, self.plugin.stratagems)
            action.render()
            self.assertEqual(action.set_center_label.call_args.args[0], '')

    def test_hard_scan_failure_marks_only_initiating_button(self):
        initiator = self.rendering_action(self.mod.AutomaticStratagemScanner)
        other = self.rendering_action(self.mod.AutomaticStratagemScanner)
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
        initiator = self.rendering_action(self.mod.AutomaticStratagemScanner)
        other = self.rendering_action(self.mod.AutomaticStratagemScanner)
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

    def test_unknown_slot_badges_but_filtered_and_complete_empty_slots_do_not(self):
        action = self.rendering_action(self.mod.AutomaticStratagem, {'slot': 1})
        session = self.coordinator.session(action)
        with patch.object(self.mod, 'badged_icon') as badge:
            badge.return_value.copy.return_value = object()
            token = session.begin({1: 'any'})
            session.finish(token, {'status': 'partial', 'rows': [{'id': None}]},
                           self.plugin.stratagems)
            action.render()
            self.assertTrue(badge.call_args.args[0].endswith(
                '/automatic_stratagems/assets/icons/auto-any.png'))
            self.assertEqual(action.set_center_label.call_args.args[0], '1')

            for color in ('red', 'blue', 'green', 'yellow'):
                badge.reset_mock()
                action.get_settings = lambda color=color: {'slot': 1, 'color_filter': color}
                token = session.begin({1: color}, replace=True)
                session.finish(token, {'status': 'partial', 'rows': [{'id': None}]},
                               self.plugin.stratagems)
                action.render()
                badge.assert_not_called()
                self.assertTrue(action.set_media.call_args.kwargs[
                    'media_path'].endswith(
                        f'/automatic_stratagems/assets/icons/auto-{color}.png'))

            badge.reset_mock()
            token = session.begin({1: 'any', 2: 'any'}, replace=True)
            session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                           self.plugin.stratagems)
            action.get_settings = lambda: {'slot': 2, 'color_filter': 'any'}
            action.render()
            badge.assert_not_called()
            self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith(
                '/auto-any.png'))

    def test_partial_scan_badges_only_slots_bound_to_unknown_rows(self):
        known = (
            'Reinforce', 'SOSBeacon', 'Resupply', 'Hellbomb', 'Flamethrower',
            'FlameSentry', 'OrbitalAirburstStrike', 'AntiTankMines',
        )
        self.plugin.stratagems = {key: ['UP'] for key in known}
        unknown = self.rendering_action(
            self.mod.AutomaticStratagem, {'slot': 9, 'color_filter': 'any'})
        empty = self.rendering_action(
            self.mod.AutomaticStratagem, {'slot': 10, 'color_filter': 'any'})
        session = self.coordinator.session(unknown)
        token = session.begin(dict.fromkeys(range(1, 14), 'any'), replace=True)
        session.finish(
            token,
            {'status': 'partial', 'rows': [
                {'id': 'Reinforce'}, {'id': 'SOSBeacon'}, {'id': 'Resupply'},
                {'id': 'Hellbomb'}, {'id': None}, {'id': 'Flamethrower'},
                {'id': 'FlameSentry'}, {'id': 'OrbitalAirburstStrike'},
                {'id': 'AntiTankMines'},
            ]},
            self.plugin.stratagems,
        )
        self.assertEqual(session.snapshot().unknown_slots, frozenset({9}))

        with patch.object(self.mod, 'badged_icon') as badge:
            badge.return_value.copy.return_value = object()
            unknown.render()
            badge.assert_called_once()
            badge.reset_mock()

            empty.render()
            badge.assert_not_called()
            self.assertTrue(empty.set_media.call_args.kwargs['media_path'].endswith(
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
        action = self.rendering_action(self.mod.AutomaticStratagemScanner)
        action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('/automatic_stratagems/assets/icons/scan-update.png'))
        action.configure('scan_mode', 'new_page')
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('/automatic_stratagems/assets/icons/scan-new-page.png'))
        self.assertEqual(action.set_top_label.call_args.args[0], 'Auto')
        self.assertEqual(action.set_bottom_label.call_args.args[0], 'Stratagems')

    def test_clear_during_scan_rejects_result_without_unlocking_worker_early(self):
        action = self.rendering_action(self.mod.AutomaticStratagemScanner)
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




    def test_scan_completion_and_cancel_restore_static_artwork(self):
        for outcome in ('matched', 'partial', 'cancel'):
            with self.subTest(outcome=outcome):
                action = self.rendering_action(self.mod.AutomaticStratagemScanner, {'group': outcome})
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

    def test_open_status_row_is_not_retained_for_worker_or_tick_mutation(self):
        action = self.rendering_action(self.mod.AutomaticStratagemScanner)
        session = self.coordinator.session(action)
        token = session.begin([1])
        session.finish(token, {'status': 'partial', 'rows': [{'id': None}]}, self.plugin.stratagems)
        with patch.object(self.mod.Adw, 'ActionRow') as row:
            action.get_config_rows()
        self.assertNotIn('last_scan_row', action.__dict__)
        self.assertEqual(row.call_args.kwargs['subtitle'], session.snapshot().message)



if __name__ == '__main__':
    unittest.main()

    def test_scanning_loops_without_restarting_on_ticks_and_stops_on_failure(self):
        action = self.rendering_action(self.mod.AutomaticStratagemScanner)
        session = self.coordinator.session(action)
        token = session.begin([1])
        action._scan_presentation = self.coordinator.context(action), session, token
        action.render()
        media = action.set_media.call_args.kwargs
        self.assertEqual(media['image'].mode, 'RGBA')
        self.assertEqual(media['image'].getpixel((0, 0))[3], 0)
        action.on_tick()
        action.on_tick()
        action.set_media.assert_called_once()
        session.fail(token, 'Capture failed')
        action.render()
        self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('/automatic_stratagems/assets/icons/scan-update.png'))
        self.assertEqual(action.set_top_label.call_args.args[0], 'Scan')
        self.assertEqual(action.set_bottom_label.call_args.args[0], 'Stratagems')
        self.assertEqual(action.set_center_label.call_args.args[0], '')


    def test_loading_preserves_background_and_removal_cancels_timer(self):
        action = self.rendering_action(self.mod.AutomaticStratagemScanner)
        session = self.coordinator.session(action)
        token = session.begin([1])
        action._scan_presentation = self.coordinator.context(action), session, token
        with patch.object(self.mod.GLib, 'timeout_add', return_value=71) as schedule, \
             patch.object(self.mod.GLib, 'source_remove') as remove:
            action.render()
            self.assertEqual(action.set_media.call_args.kwargs['image'].mode, 'RGBA')
            action.set_background_color.assert_not_called()
            callback = schedule.call_args.args[1]
            action.on_remove()
            remove.assert_called_once_with(71)
            self.assertFalse(callback())


    def test_loading_stops_before_assigned_icon_is_restored(self):
        self.plugin.get_show_labels = lambda: False
        action = self.rendering_action(self.mod.AutomaticStratagem)
        session = self.coordinator.session(action)
        token = session.begin([1])
        action._scan_presentation = self.coordinator.context(action), session, token
        with patch.object(self.mod.GLib, 'timeout_add', return_value=72) as schedule, \
             patch.object(self.mod.GLib, 'source_remove') as remove:
            action.render()
            callback = schedule.call_args.args[1]
            session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                           self.plugin.stratagems)
            action.render()
            remove.assert_called_once_with(72)
            self.assertFalse(callback())
            self.assertTrue(action.set_media.call_args.kwargs['media_path'].endswith('/A.png'))


    def test_loading_pauses_for_hidden_key_state_and_resumes_on_return(self):
        action = self.rendering_action(self.mod.AutomaticStratagemScanner)
        current = types.SimpleNamespace(state=0)
        action.get_input = lambda: current
        session = self.coordinator.session(action)
        token = session.begin([1])
        action._scan_presentation = self.coordinator.context(action), session, token
        with patch.object(self.mod.GLib, 'timeout_add', side_effect=[81, 82]) as schedule:
            action.render()
            callback = schedule.call_args.args[1]
            current.state = 1
            self.assertFalse(callback())
            action.on_tick()
            self.assertEqual(schedule.call_count, 1)
            current.state = 0
            action.on_tick()
            self.assertEqual(schedule.call_count, 2)


    def test_loading_first_render_while_hidden_starts_on_visible_return(self):
        action = self.rendering_action(self.mod.AutomaticStratagemScanner)
        current = types.SimpleNamespace(state=1)
        action.get_input = lambda: current
        session = self.coordinator.session(action)
        token = session.begin([1])
        action._scan_presentation = self.coordinator.context(action), session, token
        with patch.object(self.mod.GLib, 'timeout_add', return_value=83) as schedule:
            action.render()
            schedule.assert_not_called()
            current.state = 0
            action.on_tick()
            schedule.assert_called_once()
            self.assertEqual(action.set_media.call_args.kwargs['image'].mode, 'RGBA')
