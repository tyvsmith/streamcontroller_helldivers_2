import gc
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from weakref import ref

from .package_loader import ROOT, plugin_module


class OptionalIntegrationTests(unittest.TestCase):
    def import_without_automatic_actions(self, *, coordinator_factory=None,
                                         missing_optional=None,
                                         app=None, idle_error=None,
                                         signals_available=True,
                                         loading_tasks=None,
                                         automatic_install_error=None):
        main_name = plugin_module('main')
        holder_module = ModuleType('src.backend.PluginManager.ActionHolder')

        class ActionHolder:
            def __init__(self, **values):
                self.__dict__.update(values)

        holder_module.ActionHolder = ActionHolder
        base_module = ModuleType('src.backend.PluginManager.PluginBase')

        class PluginBase:
            def __init__(self):
                self.PATH = str(ROOT)
                self.registered_pages = []
                self.action_holders = {}
                self.locale_manager = SimpleNamespace(
                    set_to_os_default=lambda: None,
                    get=lambda key, default=None: default or key)

            def add_action_holder(self, holder):
                if (automatic_install_error is not None
                        and holder.action_id in {
                            'net_jslay_helldivers_2::ScanStratagems',
                            'net_jslay_helldivers_2::AutomaticStratagem',
                            'net_jslay_helldivers_2::TemporaryScanBack',
                            'net_jslay_helldivers_2::AutoStratagems',
                        }):
                    raise automatic_install_error
                self.action_holders[holder.action_id] = holder

            def register(self, **values):
                self.registration = values

            def get_plugin_id(self):
                return 'net_jslay_helldivers_2'

            def get_settings(self):
                return {}

            def on_uninstall(self):
                self.base_uninstalled = True

        base_module.PluginBase = PluginBase
        input_module = ModuleType('src.backend.PluginManager.InputBases')
        input_module.KeyAction = type('KeyAction', (), {
            'show_error': lambda self, duration=-1: None})
        execution = ModuleType(plugin_module('stratagem_execution'))
        execution.execute_stratagem = Mock()
        mapping = ModuleType(plugin_module('key_mapping'))
        mapping.DEFAULT_DIRECTION_KEY_LAYOUT = 'arrow_keys'
        mapping.get_direction_key = Mock()
        mapping.normalize_direction_key_layout = lambda value: 'arrow_keys'
        gi = ModuleType('gi')
        gi.require_version = Mock()
        repository = ModuleType('gi.repository')
        repository.Gtk = Mock()
        repository.Adw = Mock()
        repository.Gio = Mock()
        repository.GLib = Mock()
        if idle_error is not None:
            repository.GLib.idle_add.side_effect = idle_error
        evdev = ModuleType('evdev')
        evdev.ecodes = SimpleNamespace(EV_KEY=1, EV_REL=2, REL_X=0, REL_Y=1)
        evdev.UInput = Mock(return_value=object())
        signals = ModuleType('src.Signals')
        signals.Signals = SimpleNamespace(ChangePage=type('ChangePage', (), {}),
                                          AppQuit=type('AppQuit', (), {}))
        globals_module = ModuleType('globals')
        globals_module.DATA_PATH = '/tmp'
        globals_module.app = app
        globals_module.app_loading_finished_tasks = (loading_tasks if loading_tasks is not None
                                                      else [])
        globals_module.signal_manager = SimpleNamespace(connect_signal=Mock())
        globals_module.page_manager = Mock()
        scan_actions = ModuleType(plugin_module('automatic_stratagems.scan_actions'))
        scan_actions.ScanCoordinator = coordinator_factory or self.coordinator_type()
        scan_actions.AutomaticStratagemScanner = type(
            'AutomaticStratagemScanner', (), {})
        scan_actions.AutomaticStratagemPage = type(
            'AutomaticStratagemPage', (), {})
        scan_actions.AutomaticStratagem = type('AutomaticStratagem', (), {})
        scan_actions.TemporaryScanBack = type('TemporaryScanBack', (), {})
        scan_runner = ModuleType(plugin_module('automatic_stratagems.scan_runner'))
        scan_runner.scan_workers = lambda value=2: 2
        visibility = ModuleType(plugin_module('automatic_stratagems.visibility'))
        visibility.ChooserCompatibilityError = RuntimeError
        visibility.update_visibility = Mock()
        capture_source = ModuleType(
            plugin_module('automatic_stratagems.capture_source'))
        capture_source.DEFAULT_SCREENSHOT_HOTKEY = 'KEY_F12'
        capture_source.SCREENSHOT_TRIGGERS = ('hotkey', 'script')

        def screenshot_config(settings):
            settings = settings if isinstance(settings, dict) else {}
            trigger = settings.get('screenshot_trigger', 'hotkey')
            return {
                'kind': 'folder',
                'path': settings.get('screenshot_folder', ''),
                'trigger': trigger if trigger in ('hotkey', 'script') else 'hotkey',
                'hotkey': settings.get('screenshot_hotkey', 'KEY_F12'),
                'script': settings.get('screenshot_script', ''),
                'delete_after_scan': settings.get('screenshot_delete', True),
            }

        capture_source.screenshot_config = screenshot_config
        modules = {
            holder_module.__name__: holder_module,
            base_module.__name__: base_module,
            input_module.__name__: input_module,
            execution.__name__: execution,
            mapping.__name__: mapping,
            'gi': gi,
            'gi.repository': repository,
            'evdev': evdev,
            'src.Signals': signals if signals_available else None,
            'globals': globals_module,
            'loguru': SimpleNamespace(logger=Mock()),
            scan_actions.__name__: scan_actions,
            scan_runner.__name__: scan_runner,
            visibility.__name__: visibility,
            capture_source.__name__: capture_source,
        }
        if (missing_optional is not None
                and not (missing_optional == 'scan_actions'
                         and coordinator_factory is not None)):
            modules[plugin_module(
                f'automatic_stratagems.{missing_optional}')] = None
        sys.modules.pop(main_name, None)
        with patch.dict(sys.modules, modules):
            module = importlib.import_module(main_name)
            plugin = module.HellDiversPlugin()
        self.addCleanup(sys.modules.pop, main_name, None)
        return module, plugin

    def test_coordinator_constructor_failure_preserves_ordinary_actions(self):
        class BrokenCoordinator:
            def __init__(self, *args, **kwargs):
                raise PermissionError('state directory denied')

        module, plugin = self.import_without_automatic_actions(
            coordinator_factory=BrokenCoordinator)

        self.assertIn('net_jslay_helldivers_2::Railgun', plugin.action_holders)
        self.assertIn('net_jslay_helldivers_2::CustomStratagem', plugin.action_holders)
        self.assertIsInstance(plugin.scan_coordinator,
                              module.UnavailableScanCoordinator)

    def test_missing_lifecycle_api_preserves_ordinary_actions(self):
        coordinator = self.coordinator_type()

        module, plugin = self.import_without_automatic_actions(
            coordinator_factory=coordinator, signals_available=False)

        self.assertIn('net_jslay_helldivers_2::Railgun', plugin.action_holders)
        self.assertIsInstance(plugin.scan_coordinator,
                              module.UnavailableScanCoordinator)

    def test_visibility_scheduling_failure_disables_only_automatic_actions(self):
        coordinator = self.coordinator_type()

        _, plugin = self.import_without_automatic_actions(
            coordinator_factory=coordinator, app=object(),
            idle_error=RuntimeError('idle queue unavailable'))

        self.assertIn('net_jslay_helldivers_2::Railgun', plugin.action_holders)
        self.assertIn('idle queue unavailable',
                      plugin.scan_coordinator.compatibility_error)

    def test_deferred_visibility_callback_is_weak_and_ignores_closed_plugin(self):
        loading_tasks = []
        module, plugin = self.import_without_automatic_actions(
            coordinator_factory=self.coordinator_type(), loading_tasks=loading_tasks)
        callback = loading_tasks[0]
        plugin._automatic_integration.closed = True

        with patch.dict(sys.modules, {'globals': ModuleType('globals')}), \
             patch.object(plugin._automatic_integration,
                          'connect_visibility') as connect:
            self.assertFalse(callback())
        connect.assert_not_called()

        plugin_ref = ref(plugin)
        del plugin
        gc.collect()
        self.assertIsNone(plugin_ref())

    def test_toggle_chooser_mismatch_disables_automatic_integration(self):
        _, plugin = self.import_without_automatic_actions()
        integration = plugin._automatic_integration
        integration.coordinator = Mock(enabled=True, compatibility_error=None)
        plugin.scan_coordinator = integration.coordinator
        integration.settings_controls = [Mock()]
        integration.chooser = object()
        integration._save_setting = Mock()
        integration.enable_row = Mock()
        row = Mock()
        integration.update_visibility = Mock(
            side_effect=RuntimeError('chooser changed'))

        integration._automatic_changed(row, None)

        plugin.scan_coordinator.disable_compatibility.assert_called_once()
        integration.enable_row.set_sensitive.assert_called_once_with(False)

    def test_global_screenshot_setting_invalidates_only_on_effective_change(self):
        _, plugin = self.import_without_automatic_actions()
        values = {}
        plugin.get_settings = lambda: dict(values)
        plugin.set_settings = lambda updated: (values.clear(), values.update(updated))
        integration = plugin._automatic_integration
        integration.coordinator = Mock()
        plugin.scan_coordinator = integration.coordinator

        integration._save_screenshot_setting('screenshot_hotkey', 'KEY_F12')
        plugin.scan_coordinator.screenshot_settings_changed.assert_not_called()
        integration._save_screenshot_setting('screenshot_hotkey', 'KEY_F11')
        plugin.scan_coordinator.screenshot_settings_changed.assert_called_once_with()

    def test_global_screenshot_rows_default_to_hotkey_steam_folder_and_delete(self):
        module, plugin = self.import_without_automatic_actions()
        plugin.get_settings = lambda: {}
        plugin.scan_coordinator = Mock()
        section, trigger = Mock(), Mock()
        hotkey, script, folder = Mock(), Mock(), Mock()
        delete, browse, help_row = Mock(), Mock(), Mock()
        module.Gtk.StringList.new.reset_mock()
        with patch.object(module.Adw, 'ExpanderRow', return_value=section), \
             patch.object(module.Adw, 'ComboRow', return_value=trigger), \
             patch.object(module.Adw, 'EntryRow',
                          side_effect=[hotkey, script, folder]), \
             patch.object(module.Adw, 'SwitchRow', return_value=delete), \
             patch.object(module.Adw, 'ActionRow', return_value=help_row), \
             patch.object(module.Gtk, 'Button', return_value=browse):
            self.assertIs(plugin._automatic_integration._screenshot_row(), section)

        module.Gtk.StringList.new.assert_called_once_with(['Hotkey', 'Script'])
        trigger.set_selected.assert_called_once_with(0)
        hotkey.set_text.assert_called_once_with('KEY_F12')
        hotkey.set_visible.assert_called_once_with(True)
        script.set_text.assert_called_once_with('')
        script.set_visible.assert_called_once_with(False)
        folder.set_text.assert_called_once_with('')
        delete.set_active.assert_called_once_with(True)
        self.assertEqual(section.add_row.call_count, 6)

    def test_global_screenshot_rows_reopen_with_only_script_field_visible(self):
        module, plugin = self.import_without_automatic_actions()
        plugin.get_settings = lambda: {'screenshot_trigger': 'script'}
        plugin.scan_coordinator = Mock()
        hotkey, script, folder = Mock(), Mock(), Mock()
        with patch.object(module.Adw, 'ExpanderRow', return_value=Mock()), \
             patch.object(module.Adw, 'ComboRow', return_value=Mock()), \
             patch.object(module.Adw, 'EntryRow',
                          side_effect=[hotkey, script, folder]), \
             patch.object(module.Adw, 'SwitchRow', return_value=Mock()), \
             patch.object(module.Adw, 'ActionRow', return_value=Mock()), \
             patch.object(module.Gtk, 'Button', return_value=Mock()):
            plugin._automatic_integration._screenshot_row()
        hotkey.set_visible.assert_called_once_with(False)
        script.set_visible.assert_called_once_with(True)

    def test_ordinary_actions_keep_missing_input_feedback(self):
        module, _ = self.import_without_automatic_actions()
        plugin = SimpleNamespace(ui=None, executing=False, input_lock=Mock())
        for action in (self.static_action(module, plugin, ['UP']),
                       self.custom_action(module, plugin, ['UP'])):
            with self.subTest(action=type(action).__name__):
                module.log.reset_mock()
                module.execute_stratagem.reset_mock()
                action.on_key_down()
                module.log.error.assert_any_call(
                    'UInput not initialized! Check /dev/uinput permissions.')
                module.log.error.assert_any_call(
                    'Try: sudo usermod -aG input $USER (then logout/login)')
                module.execute_stratagem.assert_not_called()

    def test_ordinary_actions_keep_empty_sequence_feedback(self):
        module, _ = self.import_without_automatic_actions()
        plugin = SimpleNamespace(ui=object(), executing=False, input_lock=Mock())

        static = self.static_action(module, plugin, [])
        static.on_key_down()
        module.log.error.assert_called_with("No sequence for stratagem 'Railgun'!")
        module.execute_stratagem.assert_not_called()

        module.log.reset_mock()
        custom = self.custom_action(module, plugin, [])
        custom.on_key_down()
        module.log.warning.assert_called_with(
            "No sequence configured for custom stratagem 'Custom'!")
        module.execute_stratagem.assert_not_called()

    def test_ordinary_actions_keep_busy_feedback_before_executor(self):
        module, _ = self.import_without_automatic_actions()
        lock = Mock()
        lock.locked.return_value = True
        plugin = SimpleNamespace(ui=object(), executing=False, input_lock=lock)

        self.static_action(module, plugin, ['UP']).on_key_down()

        module.log.debug.assert_called_with(
            'Currently executing other stratagem! Aborting!')
        module.execute_stratagem.assert_not_called()

    @staticmethod
    def static_action(module, plugin, sequence):
        action = module.StratagemButton.__new__(module.StratagemButton)
        action.plugin_base = plugin
        action.stratagem_key = 'Railgun'
        action.stratagem = sequence
        return action

    @staticmethod
    def custom_action(module, plugin, sequence):
        action = module.CustomStratagemButton.__new__(module.CustomStratagemButton)
        action.plugin_base = plugin
        action.get_sequence = Mock(return_value=sequence)
        action.get_custom_name = Mock(return_value='Custom')
        return action

    @staticmethod
    def coordinator_type():
        class Coordinator:
            compatibility_error = None
            temporary_pages = None
            closed = False

            def __init__(self, plugin, state_dir=None):
                self.plugin = plugin

            @property
            def enabled(self):
                return (self.compatibility_error is None
                        and self.plugin.get_settings().get(
                            'automatic_stratagems_enabled', False) is True)

            def enable_temporary_pages(self, page_manager):
                pass

            def disable_compatibility(self, message):
                self.compatibility_error = str(message)

            def settings_changed(self):
                pass

            def screenshot_settings_changed(self):
                pass

            def shutdown(self, timeout=3):
                self.closed = True
                return True

        return Coordinator

    def test_page_opener_and_scanner_have_distinct_chooser_actions(self):
        module, plugin = self.import_without_automatic_actions(
            coordinator_factory=self.coordinator_type())
        opener = plugin.action_holders['net_jslay_helldivers_2::AutoStratagems']
        scanner = plugin.action_holders['net_jslay_helldivers_2::ScanStratagems']
        self.assertEqual(opener.action_name, 'Automatic Stratagem Page')
        self.assertEqual(scanner.action_name, 'Automatic Stratagem Scanner')
        self.assertEqual(opener.action_base.__name__, 'AutomaticStratagemPage')
        self.assertEqual(scanner.action_base.__name__,
                         'AutomaticStratagemScanner')
        classes = plugin._automatic_integration.action_classes
        self.assertIs(opener.action_base, classes[3])
        self.assertIs(scanner.action_base, classes[0])

    def test_available_integration_defaults_off(self):
        _, plugin = self.import_without_automatic_actions(
            coordinator_factory=self.coordinator_type())

        self.assertFalse(plugin.scan_coordinator.enabled)

    def test_missing_optional_import_preserves_static_and_saved_action_holders(self):
        module, plugin = self.import_without_automatic_actions(
            missing_optional='scan_actions')
        automatic_ids = {
            'net_jslay_helldivers_2::ScanStratagems',
            'net_jslay_helldivers_2::AutoStratagems',
            'net_jslay_helldivers_2::AutomaticStratagem',
            'net_jslay_helldivers_2::TemporaryScanBack',
        }
        self.assertTrue(automatic_ids.issubset(plugin.action_holders))
        self.assertIn('net_jslay_helldivers_2::Railgun', plugin.action_holders)
        self.assertIn('net_jslay_helldivers_2::CustomStratagem', plugin.action_holders)
        self.assertFalse(plugin.scan_coordinator.enabled)
        self.assertTrue(all(plugin.action_holders[action_id].action_base
                            is module.UnavailableAutomaticAction
                            for action_id in automatic_ids))

    def test_feature_fallback_registers_all_stable_actions_directly(self):
        module, plugin = self.import_without_automatic_actions(
            missing_optional='scan_actions')
        integration_type = module.create_integration.__globals__[
            'UnavailableAutomaticIntegration']
        for action_id in list(plugin.action_holders):
            if action_id.startswith('net_jslay_helldivers_2::'):
                plugin.action_holders.pop(action_id)

        integration_type(plugin, 'scan unavailable').install()

        self.assertEqual(set(plugin.action_holders), {
            'net_jslay_helldivers_2::ScanStratagems',
            'net_jslay_helldivers_2::AutomaticStratagem',
            'net_jslay_helldivers_2::TemporaryScanBack',
            'net_jslay_helldivers_2::AutoStratagems',
        })

    def test_each_optional_import_failure_preserves_base_plugin(self):
        for dependency in ('integration', 'scan_actions', 'scan_runner',
                           'visibility', 'capture_source'):
            with self.subTest(dependency=dependency):
                module, plugin = self.import_without_automatic_actions(
                    missing_optional=dependency)
                self.assertIn('net_jslay_helldivers_2::Railgun',
                              plugin.action_holders)
                self.assertIn('net_jslay_helldivers_2::CustomStratagem',
                              plugin.action_holders)
                self.assertFalse(plugin.scan_coordinator.enabled)
                self.assertTrue(all(
                    plugin.action_holders[action_id].action_base
                    is module.UnavailableAutomaticAction
                    for action_id in (
                        'net_jslay_helldivers_2::ScanStratagems',
                        'net_jslay_helldivers_2::AutoStratagems',
                        'net_jslay_helldivers_2::AutomaticStratagem',
                        'net_jslay_helldivers_2::TemporaryScanBack')))

    def test_automatic_install_failure_preserves_ordinary_actions(self):
        _, plugin = self.import_without_automatic_actions(
            missing_optional=None,
            automatic_install_error=RuntimeError('holder registration failed'))

        self.assertIn('net_jslay_helldivers_2::Railgun', plugin.action_holders)
        self.assertIn('net_jslay_helldivers_2::CustomStratagem',
                      plugin.action_holders)
        self.assertFalse(plugin.scan_coordinator.enabled)

    def feature_namespace(self, integration):
        return type(integration).__init__.__globals__

    def inline_thread(self):
        class InlineThread:
            def __init__(self, target=None, daemon=None, name=None):
                self.target = target

            def start(self):
                self.target()

        return InlineThread

    def prepared_integration(self):
        _, plugin = self.import_without_automatic_actions()
        values = {}
        plugin.get_settings = lambda: dict(values)
        plugin.set_settings = lambda updated: (values.clear(),
                                               values.update(updated))
        integration = plugin._automatic_integration
        integration.coordinator = Mock()
        plugin.scan_coordinator = integration.coordinator
        return plugin, integration, self.feature_namespace(integration)

    def test_enabling_the_feature_prepares_the_scanner_runtime(self):
        plugin, integration, feature = self.prepared_integration()
        row = Mock()
        row.get_active.return_value = True

        ensure = Mock()
        with patch.dict(feature, {'ensure_scanner_runtime': ensure,
                                  'Thread': self.inline_thread()}):
            integration._automatic_changed(row, None)

        ensure.assert_called_once_with(
            plugin.PATH, {'automatic_stratagems_enabled': True})

    def test_disabling_the_feature_does_not_prepare_the_scanner_runtime(self):
        _, integration, feature = self.prepared_integration()
        row = Mock()
        row.get_active.return_value = False

        ensure = Mock()
        with patch.dict(feature, {'ensure_scanner_runtime': ensure,
                                  'Thread': self.inline_thread()}):
            integration._automatic_changed(row, None)

        ensure.assert_not_called()

    def test_the_setup_status_reports_the_recorded_failure(self):
        _, integration, feature = self.prepared_integration()
        record = {'schema_version': 1, 'state': 'error', 'profile': 'flatpak',
                  'error': 'Cannot download scanner runtime source',
                  'updated_at': '2026-09-12T00:00:00Z'}

        with patch.dict(feature, {'read_status': lambda root: record}):
            text = integration.setup_status_text()

        self.assertIn('Cannot download scanner runtime source', text)

    def test_the_setup_status_reports_a_runtime_that_was_never_prepared(self):
        _, integration, feature = self.prepared_integration()

        with patch.dict(feature, {'read_status': lambda root: None}):
            text = integration.setup_status_text()

        self.assertIn('not been prepared', text)

    def test_the_setup_row_prepares_the_scanner_runtime_on_demand(self):
        plugin, integration, feature = self.prepared_integration()
        button = Mock()

        ensure = Mock()
        with patch.object(feature['Adw'], 'ActionRow', return_value=Mock()), \
             patch.object(feature['Gtk'], 'Button', return_value=button), \
             patch.dict(feature, {'read_status': lambda root: None}):
            integration._setup_row()
        handler = button.connect.call_args.args[1]

        with patch.dict(feature, {'ensure_scanner_runtime': ensure,
                                  'Thread': self.inline_thread()}):
            handler(button)

        ensure.assert_called_once_with(plugin.PATH, {})

    def test_a_second_preparation_is_ignored_while_one_is_running(self):
        _, integration, feature = self.prepared_integration()
        started = []

        class PendingThread:
            def __init__(self, target=None, daemon=None, name=None):
                self.target = target

            def start(self):
                started.append(self.target)

        with patch.dict(feature, {'Thread': PendingThread}):
            integration.prepare_runtime()
            integration.prepare_runtime()

        self.assertEqual(len(started), 1)

    def test_unavailable_integration_keeps_disabled_settings_rows(self):
        _, plugin = self.import_without_automatic_actions(
            missing_optional='capture_source')

        rows = plugin._automatic_integration.settings_rows()

        self.assertEqual(len(rows), 4)
        for row in rows:
            row.set_sensitive.assert_called_with(False)

    def test_settings_area_includes_rows_from_feature_facade(self):
        module, plugin = self.import_without_automatic_actions()
        group = Mock()
        feature_rows = [object(), object(), object()]
        plugin._automatic_integration.settings_rows = Mock(
            return_value=feature_rows)
        ordinary_row = object()

        with patch.object(module.Adw, 'PreferencesGroup', return_value=group), \
             patch.object(plugin, '_create_key_delay_row', return_value=ordinary_row), \
             patch.object(plugin, '_create_modifier_key_row', return_value=ordinary_row), \
             patch.object(plugin, '_create_direction_key_layout_row',
                          return_value=ordinary_row), \
             patch.object(plugin, '_create_hold_modifier_row',
                          return_value=ordinary_row), \
             patch.object(plugin, '_create_show_labels_row',
                          return_value=ordinary_row):
            self.assertIs(plugin.get_settings_area(), group)

        self.assertEqual(
            [call.args[0] for call in group.add.call_args_list[-3:]],
            feature_rows)

    def test_facade_shutdown_cancels_scans_releases_pages_and_disconnects_gtk(self):
        _, plugin = self.import_without_automatic_actions()
        integration = plugin._automatic_integration
        integration.coordinator = Mock()
        plugin.scan_coordinator = integration.coordinator
        plugin.scan_coordinator.temporary_pages = Mock()
        chooser = Mock()
        integration.chooser = chooser
        integration.chooser_handler = 42
        integration.shutdown()
        plugin.scan_coordinator.shutdown.assert_called_once_with()
        plugin.scan_coordinator.temporary_pages.unregister_all.assert_called_once_with()
        chooser.disconnect.assert_called_once_with(42)

    def test_facade_shutdown_continues_after_scanner_failure(self):
        _, plugin = self.import_without_automatic_actions()
        integration = plugin._automatic_integration
        integration.coordinator = Mock()
        plugin.scan_coordinator = integration.coordinator
        plugin.scan_coordinator.shutdown.side_effect = RuntimeError('shutdown failed')
        plugin.scan_coordinator.temporary_pages = Mock()
        chooser = Mock()
        integration.chooser = chooser
        integration.chooser_handler = 42

        integration.shutdown()

        plugin.scan_coordinator.temporary_pages.unregister_all.assert_called_once_with()
        chooser.disconnect.assert_called_once_with(42)

    def test_uninstall_delegates_feature_cleanup_before_base_uninstall(self):
        module, plugin = self.import_without_automatic_actions()
        integration = Mock()
        plugin._automatic_integration = integration

        plugin.on_uninstall()

        integration.shutdown.assert_called_once_with(uninstall=True)
        self.assertTrue(plugin.base_uninstalled)


if __name__ == '__main__':
    unittest.main()
