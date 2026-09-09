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
                                         app=None, idle_error=None,
                                         signals_available=True,
                                         loading_tasks=None):
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
            plugin_module('automatic_stratagems.scan_actions'): None,
        }
        sys.modules.pop(main_name, None)
        with patch.dict(sys.modules, modules):
            module = importlib.import_module(main_name)
            if coordinator_factory is not None:
                module.ScanCoordinator = coordinator_factory
                module.ScanStratagems = type('ScanStratagems', (), {})
                module.AutoStratagems = type('AutoStratagems', (), {})
                module.AutomaticStratagem = type('AutomaticStratagem', (), {})
                module.TemporaryScanBack = type('TemporaryScanBack', (), {})
                module.ChooserCompatibilityError = RuntimeError
                module.update_visibility = Mock()
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
        plugin._automatic_closed = True

        with patch.dict(sys.modules, {'globals': ModuleType('globals')}), \
             patch.object(plugin, '_connect_automatic_action_visibility') as connect:
            self.assertFalse(callback())
        connect.assert_not_called()

        plugin_ref = ref(plugin)
        del plugin
        gc.collect()
        self.assertIsNone(plugin_ref())

    def test_toggle_chooser_mismatch_disables_automatic_integration(self):
        module, _ = self.import_without_automatic_actions()
        plugin = module.HellDiversPlugin.__new__(module.HellDiversPlugin)
        plugin.scan_coordinator = Mock(enabled=True, compatibility_error=None)
        plugin._automatic_settings_rows = [Mock()]
        plugin._automatic_chooser = object()
        plugin._save_setting = Mock()
        plugin._automatic_enable_row = Mock()
        row = Mock()
        module.ChooserCompatibilityError = RuntimeError
        module.update_visibility = Mock(side_effect=RuntimeError('chooser changed'))

        plugin._on_automatic_stratagems_changed(row, None)

        plugin.scan_coordinator.disable_compatibility.assert_called_once()
        plugin._automatic_enable_row.set_sensitive.assert_called_once_with(False)

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
                return self.compatibility_error is None

            def enable_temporary_pages(self, page_manager):
                pass

            def disable_compatibility(self, message):
                self.compatibility_error = str(message)

            def settings_changed(self):
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
        self.assertIs(opener.action_base, module.AutoStratagems)
        self.assertIs(scanner.action_base, module.ScanStratagems)

    def test_missing_optional_import_preserves_static_and_saved_action_holders(self):
        module, plugin = self.import_without_automatic_actions()
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

    def test_teardown_cancels_scans_releases_pages_and_disconnects_gtk(self):
        module, _ = self.import_without_automatic_actions()
        plugin = module.HellDiversPlugin.__new__(module.HellDiversPlugin)
        plugin._automatic_closed = False
        plugin.scan_coordinator = Mock()
        plugin.scan_coordinator.temporary_pages = Mock()
        chooser = Mock()
        plugin._automatic_chooser = chooser
        plugin._automatic_chooser_handler = 42
        plugin._teardown_automatic_scanning()
        plugin.scan_coordinator.shutdown.assert_called_once_with()
        plugin.scan_coordinator.temporary_pages.unregister_all.assert_called_once_with()
        chooser.disconnect.assert_called_once_with(42)

    def test_teardown_continues_after_scanner_shutdown_failure(self):
        module, _ = self.import_without_automatic_actions()
        plugin = module.HellDiversPlugin.__new__(module.HellDiversPlugin)
        plugin._automatic_closed = False
        plugin.scan_coordinator = Mock()
        plugin.scan_coordinator.shutdown.side_effect = RuntimeError('shutdown failed')
        plugin.scan_coordinator.temporary_pages = Mock()
        chooser = Mock()
        plugin._automatic_chooser = chooser
        plugin._automatic_chooser_handler = 42

        plugin._teardown_automatic_scanning()

        plugin.scan_coordinator.temporary_pages.unregister_all.assert_called_once_with()
        chooser.disconnect.assert_called_once_with(42)


if __name__ == '__main__':
    unittest.main()
