import json
import types
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from .action_test_support import ActionTestHarness


class ActionConfigurationTests(ActionTestHarness, unittest.TestCase):
    def test_capture_backend_normalizes_legacy_sources(self):
        action = self.action()
        action.get_settings.return_value = {}
        self.assertEqual(self.mod.capture_backend(action), 'auto')
        for backend in ('auto', 'gamescope', 'screenshot'):
            action.get_settings.return_value = {'capture_backend': backend}
            self.assertEqual(self.mod.capture_backend(action), backend)
        for backend in ('steam', 'desktop', 'portal', 'x11', 'invalid'):
            action.get_settings.return_value = {'capture_backend': backend}
            self.assertEqual(self.mod.capture_backend(action), 'screenshot')
        action.get_settings.return_value = {'image_source_kind': 'folder'}
        self.assertEqual(self.mod.capture_backend(action), 'screenshot')
        action.get_settings.return_value = {
            'capture_backend': 'gamescope', 'image_source_kind': 'folder'}
        self.assertEqual(self.mod.capture_backend(action), 'gamescope')
        action.get_settings.return_value = {'image_source_kind': 'invalid'}
        self.assertEqual(self.mod.capture_backend(action), 'auto')

    def test_capture_backend_display_matches_runtime_for_current_and_legacy_values(self):
        action = self.action()
        expected = {
            'auto': ('auto', 0), 'gamescope': ('gamescope', 1),
            'screenshot': ('screenshot', 2), 'steam': ('screenshot', 2),
            'desktop': ('screenshot', 2), 'portal': ('screenshot', 2),
            'x11': ('screenshot', 2), 'invalid': ('screenshot', 2),
        }
        for backend, (runtime, selected) in expected.items():
            with self.subTest(backend=backend):
                settings = {'capture_backend': backend}
                action.get_settings.return_value = settings
                self.assertEqual(self.mod.capture_backend(action), runtime)
                self.assertEqual(self.mod.capture_source_index(settings), selected)

    def test_group_edit_commits_only_on_apply_or_focus_leave(self):
        a=self.mod.AutomaticStratagemScanner.__new__(self.mod.AutomaticStratagemScanner)
        settings = {'group': 'alpha'}
        a.get_settings = lambda: dict(settings)
        a.configure = Mock(side_effect=lambda key, value: settings.update({key: value}))
        row, trigger = Mock(), Mock()
        focus = Mock()
        self.mod.Adw.ActionRow.reset_mock()
        with patch.object(self.mod.Adw, 'EntryRow', return_value=row), \
             patch.object(self.mod.Gtk, 'EventControllerFocus', return_value=focus):
            rows = self.mod.ScanActionBase.get_config_rows(a)

        self.assertIn(row, rows)
        row.set_show_apply_button.assert_called_once_with(True)
        self.mod.Adw.ActionRow.assert_any_call(
            title='Group scope',
            subtitle='Shared only with the same group on this deck and page')
        self.assertEqual(
            [call.args[0] for call in row.connect.call_args_list],
            ['apply', 'entry-activated'])
        focus.connect.assert_called_once()
        self.assertEqual(focus.connect.call_args.args[0], 'leave')
        row.get_text.return_value = ' bravo '
        row.connect.call_args_list[1].args[1](row)
        focus.connect.call_args.args[1](focus)
        a.configure.assert_called_once_with('group', 'bravo')

        row.get_text.return_value = '   '
        row.connect.call_args_list[0].args[1](row)
        a.configure.assert_called_with('group', 'default')
        row.set_text.assert_called_with('default')

    def test_capture_source_offers_automatic_gamescope_and_screenshot(self):
        action = self.mod.AutomaticStratagemScanner.__new__(self.mod.AutomaticStratagemScanner)
        action.plugin_base = self.plugin
        self.plugin.scan_coordinator = self.coordinator
        action.deck_controller = self.deck
        action.page = self.page
        action.get_settings = lambda: {}
        action.configure = Mock()
        row = Mock()
        self.mod.Gtk.StringList.new.reset_mock()
        with patch.object(self.mod.Adw, 'ComboRow', return_value=row):
            rows = action.get_config_rows()
        self.assertIn(row, rows)
        self.mod.Gtk.StringList.new.assert_called_once_with(
            ['Automatic', 'Gamescope', 'Screenshot'])
        row.set_selected.assert_called_once_with(0)
        row.get_selected.return_value = 2
        row.connect.call_args.args[1](row, None)
        action.configure.assert_called_once_with('capture_backend', 'screenshot')

    def test_generated_capture_source_is_read_only_without_screenshot_rows(self):
        action = self.mod.AutomaticStratagemScanner.__new__(self.mod.AutomaticStratagemScanner)
        action.plugin_base = self.plugin
        coordinator = Mock(store=None)
        coordinator.is_temporary_action.return_value = True
        self.plugin.scan_coordinator = coordinator
        action.deck_controller = self.deck
        action.page = self.page
        action.get_settings = lambda: {'capture_backend': 'screenshot'}
        action.configure = Mock()
        source = Mock()
        with patch.object(self.mod.Adw, 'ComboRow', return_value=source), \
             patch.object(self.mod.Adw, 'EntryRow') as entries:
            rows = action.get_config_rows()
        source.set_selected.assert_called_once_with(2)
        source.set_sensitive.assert_called_once_with(False)
        self.assertEqual(entries.call_count, 1)
        self.assertTrue(any(call.kwargs.get('title') == 'Capture source is inherited'
                            for call in self.mod.Adw.ActionRow.call_args_list))
        action.configure.assert_not_called()

    def test_backend_configuration_removes_inherited_override_and_persists_explicit_auto(self):
        settings = {'group': 'alpha', 'capture_backend': 'steam'}
        action = self.rendering_action(self.mod.AutomaticStratagemScanner, settings)

        def replace_settings(updated):
            settings.clear()
            settings.update(updated)

        action.get_settings = lambda: dict(settings)
        action.set_settings = replace_settings
        action.render = Mock()

        action.configure('capture_backend', None, remove=True)
        self.assertEqual(settings, {'group': 'alpha'})
        action.configure('capture_backend', 'auto')
        self.assertEqual(settings, {'group': 'alpha', 'capture_backend': 'auto'})

    def test_removed_global_backend_does_not_override_action_default(self):
        action = self.rendering_action(self.mod.AutomaticStratagemScanner, {})
        self.plugin.get_settings = lambda: {'capture_backend': 'gamescope'}
        self.assertEqual(self.mod.capture_backend(action), 'auto')
        action.configure = Mock()
        source = Mock()
        with patch.object(self.mod.Adw, 'ComboRow', return_value=source):
            self.mod.AutomaticStratagemScanner.get_config_rows(action)
        source.set_selected.assert_called_once_with(0)
        self.plugin.get_settings = lambda: {'capture_backend': 'portal'}
        self.assertEqual(self.mod.capture_backend(action), 'auto')

    def test_capture_source_maps_legacy_images_and_defaults_to_auto(self):
        for kind in ('file', 'folder', 'path'):
            with self.subTest(kind=kind):
                self.assertEqual(self.mod.capture_source_index({
                    'image_source_kind': kind}), 2)
        self.assertEqual(self.mod.capture_source_index({}), 0)
        self.assertEqual(self.mod.capture_source_index({
            'capture_backend': 'gamescope'}), 1)

    def test_new_backend_selection_overrides_legacy_image_source_keys(self):
        values = {'image_source_kind': 'file',
                  'image_source_path': '/legacy/frame.png'}
        action = self.rendering_action(self.mod.AutomaticStratagemScanner, values)
        action.get_settings = lambda: dict(values)
        action.set_settings = lambda updated: (values.clear(), values.update(updated))
        self.plugin_settings['screenshot_folder'] = '/global'

        action.configure('capture_backend', 'auto')
        self.assertEqual(self.mod.capture_backend(action), 'auto')
        self.assertEqual(self.mod.capture_source_index(action.get_settings()), 0)
        self.assertEqual(self.operation_source(action)['path'], '/global')

        action.configure('capture_backend', 'gamescope')
        self.assertEqual(self.mod.capture_backend(action), 'gamescope')
        self.assertEqual(self.mod.capture_source_index(action.get_settings()), 1)
        self.assertIsNone(self.operation_source(action))

    def test_image_source_config_uses_only_global_screenshot_settings(self):
        action = self.action()
        action.get_settings.return_value = {
            'capture_backend': 'screenshot', 'screenshot_folder': '/ignored',
            'screenshot_trigger': 'script'}
        self.plugin_settings.update(
            screenshot_folder='/captures', screenshot_trigger='script',
            screenshot_script='/tmp/save', screenshot_delete=True)
        self.assertEqual(self.operation_source(action), {
            'kind': 'folder', 'path': '/captures', 'trigger': 'script',
            'hotkey': 'KEY_F12', 'script': '/tmp/save',
            'delete_after_scan': True, 'previous_fingerprint': None,
            'allow_rescan': False})
        self.assertTrue(self.operation_source(
            action, allow_rescan=True)['allow_rescan'])

    def test_image_source_config_ignores_legacy_history(self):
        action = self.action()
        action.get_settings.return_value = {
            'capture_backend': 'screenshot',
            'image_source_last_fingerprint': 'a' * 64,
            'image_source_last_path': '/old/frame.png',
            'image_source_last_mtime_ns': 1}
        self.plugin_settings['screenshot_folder'] = '/old'
        self.assertIsNone(self.operation_source(action)['previous_fingerprint'])

    def test_incomplete_global_script_is_preserved_for_preflight(self):
        action = self.action()
        action.get_settings.return_value = {'capture_backend': 'screenshot'}
        self.plugin_settings['screenshot_trigger'] = 'script'
        self.assertEqual(self.operation_source(action)['script'], '')

    def test_invalid_explicit_backend_normalizes_to_screenshot(self):
        action = self.action()
        action.get_settings.return_value = {'capture_backend': 'invalid'}
        self.assertEqual(self.mod.capture_backend(action), 'screenshot')

    def test_old_per_action_screenshot_settings_are_ignored(self):
        action = self.action()
        action.get_settings.return_value = {
            'capture_backend': 'screenshot', 'screenshot_folder': '/old',
            'screenshot_trigger': 'script', 'screenshot_delete': False}
        self.plugin_settings['screenshot_folder'] = '/global'
        config = self.operation_source(action)
        self.assertEqual(config['path'], '/global')
        self.assertEqual(config['trigger'], 'hotkey')
        self.assertTrue(config['delete_after_scan'])

    def test_backend_change_tolerates_legacy_history(self):
        values = {
            'capture_backend': 'auto',
            'image_source_last_fingerprint': 'a' * 64,
            'image_source_last_path': '/captures/frame.png',
            'image_source_last_mtime_ns': 123}
        action = self.rendering_action(self.mod.AutomaticStratagemScanner, values)
        action.get_settings = lambda: dict(values)
        action.set_settings = lambda updated: (values.clear(), values.update(updated))
        action.configure('capture_backend', 'gamescope')
        self.assertEqual(values['image_source_last_fingerprint'], 'a' * 64)
        self.assertIsNone(self.operation_source(action))

    def test_action_configuration_has_no_screenshot_path_entry(self):
        action = self.mod.AutomaticStratagemScanner.__new__(self.mod.AutomaticStratagemScanner)
        action.plugin_base = self.plugin
        self.plugin.scan_coordinator = self.coordinator
        action.deck_controller = self.deck
        action.page = self.page
        action.get_settings = lambda: {'capture_backend': 'screenshot'}
        action.configure = Mock()
        with patch.object(self.mod.Adw, 'EntryRow') as entries:
            action.get_config_rows()
        self.assertEqual(entries.call_count, 1)

    def test_action_configuration_does_not_create_file_dialog(self):
        action = self.mod.AutomaticStratagemScanner.__new__(self.mod.AutomaticStratagemScanner)
        action.plugin_base = self.plugin
        self.plugin.scan_coordinator = self.coordinator
        action.deck_controller = self.deck
        action.page = self.page
        action.get_settings = lambda: {'capture_backend': 'screenshot'}
        action.configure = Mock()
        with patch.object(self.mod.Gtk.FileDialog, 'new') as dialog:
            action.get_config_rows()
        dialog.assert_not_called()

    def test_color_filter_is_per_slot(self):
        a=self.mod.AutomaticStratagem.__new__(self.mod.AutomaticStratagem)
        a.get_settings=lambda:{'color_filter':'red'}
        self.assertEqual(a.color_filter(),'red')
        a.get_settings=lambda:{'color_filter':'invalid'}
        self.assertEqual(a.color_filter(),'any')

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
        scanner = self.rendering_action(self.mod.AutomaticStratagemScanner)
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

    def test_scan_mode_selector_is_hidden_but_legacy_setting_is_preserved(self):
        action = self.mod.AutomaticStratagemScanner.__new__(self.mod.AutomaticStratagemScanner)
        action.plugin_base = self.plugin
        self.plugin.scan_coordinator = self.coordinator
        action.deck_controller = self.deck
        action.page = self.page
        action.get_settings = lambda: {}
        action.configure = Mock()
        with patch.object(self.mod.Adw, 'ComboRow') as create:
            action.get_config_rows()
        self.assertEqual(create.call_count, 1)
        self.assertEqual(self.mod.scan_mode(action), 'update')
        action.get_settings = lambda: {'scan_mode': 'new_page'}
        self.assertEqual(self.mod.scan_mode(action), 'new_page')

    def test_changing_mode_preserves_existing_source_assignments(self):
        action = self.mod.AutomaticStratagemScanner.__new__(self.mod.AutomaticStratagemScanner)
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

    def test_automatic_config_explains_unconfirmed_assignment_and_last_scan(self):
        action = self.rendering_action(self.mod.AutomaticStratagem, {'slot': 1})
        action.plugin_base.lm = Mock()
        action.plugin_base.lm.get.return_value = 'Alpha Stratagem'
        session = self.coordinator.session(action)
        token = session.begin({1: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems)
        token = session.begin({1: 'any'})
        session.finish(token, {'status': 'partial', 'rows': [{'id': None}]},
                       self.plugin.stratagems)
        self.assertEqual(session.snapshot().unconfirmed_slots, frozenset({1}))

        self.mod.Adw.ActionRow.reset_mock()
        action.get_config_rows()

        self.mod.Adw.ActionRow.assert_any_call(
            title='Assignment status',
            subtitle=('Alpha Stratagem is unconfirmed; tap still executes it; '
                      'the latest scan did not see it'))
        action.plugin_base.lm.get.assert_called_with('actions.A.name', 'A')
        self.mod.Adw.ActionRow.assert_any_call(
            title='Last scan', subtitle=session.snapshot().message)

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
        scanner = self.rendering_action(self.mod.AutomaticStratagemScanner)
        self.coordinator.actions.update((first, second, scanner))
        session = self.coordinator.session(scanner)
        token = session.begin({1: 'any', 2: 'any'})
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}, {'id': 'B'}]},
                       self.plugin.stratagems)
        with patch.object(self.mod, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'run_scan', side_effect=self.mod.CancelledError):
            self.coordinator.start(scanner, replace=True)
            operation = self.coordinator.active_scans[self.coordinator.context(scanner)]
            self.coordinator.remove_action(first)
            self.assertTrue(operation.cancel.is_set())
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



if __name__ == '__main__':
    unittest.main()
