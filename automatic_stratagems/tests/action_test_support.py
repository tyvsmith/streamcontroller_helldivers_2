import importlib
import json
from pathlib import Path
import sys
import threading
import time
import types
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from .package_loader import plugin_module


class ActionTestHarness:
    @classmethod
    def setUpClass(cls):
        gi = types.ModuleType('gi.repository')
        gi.GLib = Mock(); gi.Adw = Mock(); gi.Gtk = Mock(); gi.Gio = Mock()
        bases = types.ModuleType('src.backend.PluginManager.InputBases')
        bases.KeyAction = type('KeyAction', (), {})
        execution = types.ModuleType(plugin_module('stratagem_execution'))
        execution.execute_stratagem = Mock()
        with patch.dict(sys.modules, {'gi.repository':gi, 'src.backend.PluginManager.InputBases':bases,
                                     execution.__name__:execution, 'loguru':types.SimpleNamespace(logger=Mock())}):
            cls.mod = importlib.import_module(plugin_module('automatic_stratagems.scan_actions'))

    def setUp(self):
        self.plugin_settings = {'automatic_stratagems_enabled': True}
        self.plugin = types.SimpleNamespace(input_lock=threading.Lock(), stratagems={'A':['UP']},
                                           PATH='/tmp/plugin', get_settings=lambda:dict(self.plugin_settings))
        self.coordinator = self.mod.ScanCoordinator(self.plugin)
        self.deck = object(); self.page = types.SimpleNamespace(json_path='/tmp/HD2.json')

    def action(self):
        a = Mock()
        a.plugin_base=self.plugin; a.deck_controller=self.deck; a.page=self.page
        a.get_settings.return_value={'slot':1, 'capture_backend': 'gamescope'}
        a.get_is_present.return_value=True
        a.on_ready_called=True
        return a

    def operation_source(self, action, *, allow_rescan=False):
        settings = self.coordinator._operation_image_settings(action)
        return self.mod.scan_lifecycle.operation_source(
            settings, allow_rescan=allow_rescan)

    def wait_for_input_release(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.plugin.input_lock.locked():
            time.sleep(.01)
        self.assertFalse(self.plugin.input_lock.locked())

    def wait_for_queue(self, queued):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not queued:
            time.sleep(.01)
        self.assertTrue(queued)

    def rendering_action(self, action_type, settings=None):
        action = action_type.__new__(action_type)
        action.plugin_base = self.plugin
        self.plugin.scan_coordinator = self.coordinator
        action.deck_controller = self.deck
        action.page = self.page
        if settings is None:
            values = {'capture_backend': 'gamescope'}
            if action_type is self.mod.AutomaticStratagem:
                values['slot'] = 1
        else:
            values = dict(settings)
        action.get_settings = lambda: dict(values)
        action.set_settings = lambda updated: values.update(updated)
        action.get_is_present = lambda: True
        action.state = 0
        action.get_input = lambda: types.SimpleNamespace(state=0)
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
        action.get_settings.return_value = {'group': 'HD2', 'scan_mode': 'new_page', 'capture_backend': 'gamescope'}
        action.get_is_present.side_effect = lambda: self.deck.active_page.json_path == action.page.json_path
        action.input_ident = types.SimpleNamespace(input_type='keys', json_identifier='4x0')
        action.state = 0
        self.page.action_objects = {'keys': {'4x0': {0: {0: action}}}}
        return action, root

    def second_page_opener(self, identifier='3x1'):
        action = self.action()
        action.get_settings.return_value = {
            'group': 'HD2', 'scan_mode': 'new_page', 'capture_backend': 'gamescope'}
        action.get_is_present.side_effect = (
            lambda: self.deck.active_page.json_path == action.page.json_path)
        action.input_ident = types.SimpleNamespace(
            input_type='keys', json_identifier=identifier)
        action.state = 0
        self.page.action_objects['keys'][identifier] = {0: {0: action}}
        return action

    def real_page_opener(self, identifier='4x0', *, backend='auto'):
        action = self.rendering_action(
            self.mod.AutomaticStratagemPage,
            {'group': 'HD2', 'capture_backend': backend})
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
        with patch.object(self.mod.scan_lifecycle, 'Thread') as thread, \
             patch.object(self.mod.scan_lifecycle, 'check_scan_setup'), \
             patch.object(self.mod.scan_lifecycle, 'run_scan', return_value=report, side_effect=error), \
             patch.object(self.mod.scan_lifecycle.GLib, 'idle_add', side_effect=lambda f, *args: f(*args)):
            thread.side_effect = lambda **kw: types.SimpleNamespace(start=kw['target'])
            self.coordinator.start(action, replace=replace, regenerate=regenerate)

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
