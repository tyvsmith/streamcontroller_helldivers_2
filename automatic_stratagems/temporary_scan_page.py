"""Owned temporary pages with explicit return navigation and cleanup."""

import json
import logging
from pathlib import Path
from uuid import uuid4

from .bounded_json import read_bounded_json
from .capture_source import (
    SCREENSHOT_SETTING_KEYS, screenshot_config, screenshot_source_identity)
from .streamcontroller_adapter import (
    discard_cached_page, register_page, unregister_page)

PLUGIN_ID = 'net_jslay_helldivers_2'
MARKER = 'hd2_temporary_scan'
MAX_PAGE_BYTES = 1024 * 1024
LEGACY_SCREENSHOT_TRIGGERS = ('none', 'hotkey', 'script')
LEGACY_IMAGE_SOURCE_HISTORY = (
    'image_source_last_fingerprint',
    'image_source_last_path',
    'image_source_last_mtime_ns',
)


def valid_source_action(value):
    return (isinstance(value, dict)
            and isinstance(value.get('input_type'), str) and bool(value['input_type'])
            and isinstance(value.get('identifier'), str) and bool(value['identifier'])
            and type(value.get('state')) is int and value['state'] >= 0
            and type(value.get('index')) is int and value['index'] >= 0)


def key_action(suffix, settings):
    return {'states': {'0': {'actions': [{'id': f'{PLUGIN_ID}::{suffix}', 'settings': settings}],
                            'image-control-action': 0, 'label-control-actions': [0, 0, 0],
                            'background-control-action': 0}}}


def image_source_identity(settings):
    backend = settings.get('capture_backend', 'screenshot')
    if backend not in ('auto', 'gamescope', 'screenshot'):
        raise ValueError('Invalid temporary page capture backend')
    if backend != 'gamescope':
        config = screenshot_config(settings)
        expected = {
            'screenshot_trigger': config['trigger'],
            'screenshot_hotkey': config['hotkey'],
            'screenshot_script': config['script'],
            'screenshot_folder': config['path'],
            'screenshot_delete': config['delete_after_scan'],
        }
        if any(settings.get(key) != value for key, value in expected.items()):
            raise ValueError('Invalid temporary page screenshot source')
    return screenshot_source_identity(backend, settings)


def _legacy_image_source_identity(settings):
    kind = settings.get('image_source_kind', 'live')
    if kind not in ('live', 'file', 'folder', 'path'):
        raise ValueError('Invalid temporary page image source')
    backend = settings.get('capture_backend', 'screenshot')
    if kind == 'live' and backend == 'gamescope':
        return None
    legacy_image = kind != 'live'
    if kind == 'live':
        kind = 'path'
    path = settings.get('image_source_path')
    if path is None:
        path = ''
    if not isinstance(path, str):
        raise ValueError('Invalid temporary page image source')
    trigger = settings.get('screenshot_trigger', 'none' if legacy_image else 'hotkey')
    hotkey = settings.get('screenshot_hotkey', 'KEY_F12')
    script = settings.get('screenshot_script', '')
    delete = settings.get('screenshot_delete', False)
    if (trigger not in LEGACY_SCREENSHOT_TRIGGERS
            or not isinstance(hotkey, str) or not hotkey.strip()
            or not isinstance(script, str)
            or type(delete) is not bool):
        raise ValueError('Invalid temporary page screenshot source')
    if trigger == 'none':
        delete = False
    return {
        'kind': kind, 'path': path.strip(), 'trigger': trigger,
        'hotkey': hotkey.strip(), 'script': script.strip(),
        'delete_after_scan': delete,
    }


def _valid_stored_identity(value):
    return (value is None or
            (isinstance(value, dict)
             and value.get('kind') in ('file', 'folder', 'path')
             and isinstance(value.get('path'), str)
             and value.get('trigger') in LEGACY_SCREENSHOT_TRIGGERS
             and isinstance(value.get('hotkey'), str) and bool(value['hotkey'])
             and isinstance(value.get('script'), str)
             and type(value.get('delete_after_scan')) is bool
             and (value['trigger'] != 'none' or not value['delete_after_scan'])))


def _legacy_stored_identity(value, settings):
    if value is None:
        return _legacy_image_source_identity(settings)
    legacy = dict(settings)
    legacy.update(image_source_kind=value['kind'], image_source_path=value['path'])
    for key in SCREENSHOT_SETTING_KEYS:
        legacy.pop(key, None)
    return _legacy_image_source_identity(legacy)


def _valid_current_identity(value):
    if not isinstance(value, dict) or value.get('backend') not in (
            'auto', 'gamescope', 'screenshot'):
        return False
    source = value.get('source')
    if value['backend'] == 'gamescope':
        return source is None
    return (isinstance(source, dict)
            and set(source) == {
                'kind', 'path', 'trigger', 'hotkey', 'script',
                'delete_after_scan'}
            and source.get('kind') == 'folder'
            and isinstance(source.get('path'), str)
            and source.get('trigger') in ('hotkey', 'script')
            and isinstance(source.get('hotkey'), str) and bool(source['hotkey'])
            and isinstance(source.get('script'), str)
            and type(source.get('delete_after_scan')) is bool)


class TemporaryScanPages:
    """Own registration, navigation, and deletion of versioned generated pages."""

    def __init__(self, directory, page_manager, state_store=None, owner=None):
        self.directory = Path(directory).resolve()
        self.manager = page_manager
        self.state_store = state_store
        self.owner = owner
        for path in self.directory.glob('*.json'):
            try:
                self.metadata(str(path))
                self._register(str(path))
            except Exception:
                logging.getLogger(__name__).warning('Skipping invalid temporary page: %s', path)

    def _register(self, path):
        register_page(self.owner, self.manager, path)

    def owns_location(self, path):
        return Path(path).parent.resolve() == self.directory

    def _unregister(self, path):
        unregister_page(self.owner, self.manager, path)

    @staticmethod
    def _metadata(data):
        meta = data.get(MARKER) if isinstance(data, dict) else None
        if (not isinstance(meta, dict) or meta.get('owner') != PLUGIN_ID
                or meta.get('version') not in (1, 2, 3, 4)
                or not all(isinstance(meta.get(key), str) and meta[key]
                           for key in ('source_page', 'deck', 'group'))):
            raise ValueError('Not an owned temporary scan page')
        if meta['version'] in (2, 3, 4) and not valid_source_action(meta.get('source_action')):
            raise ValueError('Invalid temporary page source action')
        image_source = meta.get('image_source')
        if (meta['version'] == 3 and not _valid_stored_identity(image_source)):
            raise ValueError('Invalid temporary page image source')
        if meta['version'] == 4 and not _valid_current_identity(image_source):
            raise ValueError('Invalid temporary page image source')
        if (meta['version'] in (1, 2) and image_source is not None
                and (not isinstance(image_source, dict)
                     or image_source.get('kind') not in ('file', 'folder', 'path')
                     or not isinstance(image_source.get('path'), str)
                     or not image_source['path'])):
            raise ValueError('Invalid temporary page image source')
        return meta

    def metadata(self, path):
        file = Path(path)
        if file.parent.resolve() != self.directory:
            raise ValueError('Not an owned temporary scan page')
        return self._metadata(read_bounded_json(file, max_bytes=MAX_PAGE_BYTES))

    @staticmethod
    def layout(controller):
        rows, columns = controller.deck.key_layout()
        if rows * columns < 3:
            raise ValueError('Temporary scan pages require at least three keys')
        return rows, columns

    def create(self, controller, source_page, group, backend, source_action,
               *, image_settings=None):
        if not valid_source_action(source_action):
            raise ValueError('Source action address unavailable')
        rows, columns = self.layout(controller)
        image_settings = dict(image_settings or {})
        for key in LEGACY_IMAGE_SOURCE_HISTORY:
            image_settings.pop(key, None)
        backend = backend if backend in ('auto', 'gamescope', 'screenshot') else 'screenshot'
        config = screenshot_config(image_settings)
        image_settings.update(
            screenshot_trigger=config['trigger'], screenshot_hotkey=config['hotkey'],
            screenshot_script=config['script'], screenshot_folder=config['path'],
            screenshot_delete=config['delete_after_scan'])
        scan_settings = {'group': group, 'scan_mode': 'update',
                         'capture_backend': backend, **image_settings}
        image_source = image_source_identity(scan_settings)
        actions = [key_action('TemporaryScanBack', {'group': group}),
                   key_action('ScanStratagems', scan_settings)]
        actions.extend(key_action('AutomaticStratagem', {'group': group, 'slot': slot})
                       for slot in range(1, rows * columns - 1))
        data = {MARKER: dict(owner=PLUGIN_ID, version=4, source_page=str(Path(source_page).absolute()),
                            deck=controller.serial_number(), group=group,
                            source_action=dict(source_action),
                            image_source=image_source),
                'keys': {f'{index % columns}x{index // columns}': action
                         for index, action in enumerate(actions)}}
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f'HD2 Scan {uuid4().hex}.json'
        with path.open('x') as stream:
            json.dump(data, stream, indent=2)
        try:
            self._register(str(path))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return str(path)

    def scan_settings(self, path, *, bind_image_source=False):
        file = Path(path)
        if file.parent.resolve() != self.directory:
            raise ValueError('Not an owned temporary scan page')
        data = read_bounded_json(file, max_bytes=MAX_PAGE_BYTES)
        metadata = self._metadata(data)
        keys = data.get('keys') if isinstance(data, dict) else None
        if not isinstance(keys, dict):
            raise ValueError('Invalid temporary scan page')
        action_id = f'{PLUGIN_ID}::ScanStratagems'
        for key in keys.values():
            try:
                actions = key['states']['0']['actions']
            except (KeyError, TypeError):
                continue
            for action in actions if isinstance(actions, list) else ():
                if isinstance(action, dict) and action.get('id') == action_id:
                    settings = action.get('settings')
                    if not isinstance(settings, dict):
                        raise ValueError('Invalid temporary scan settings')
                    if bind_image_source:
                        stored = metadata.get('image_source')
                        if metadata['version'] in (1, 2):
                            stored = _legacy_stored_identity(stored, settings)
                            expected = _legacy_image_source_identity(settings)
                        elif metadata['version'] == 3:
                            expected = _legacy_image_source_identity(settings)
                        else:
                            expected = image_source_identity(settings)
                        if expected != stored:
                            raise ValueError('Temporary page image source changed')
                    return dict(settings)
        raise ValueError('Temporary scan action unavailable')

    def find(self, controller, source_page, group, source_action):
        if not valid_source_action(source_action):
            raise ValueError('Source action address unavailable')
        identity = (controller.serial_number(), str(Path(source_page).absolute()),
                    group, source_action)
        for path in sorted(self.directory.glob('*.json')):
            try:
                meta = self.metadata(str(path))
            except (OSError, ValueError):
                continue
            if (meta['version'] in (2, 3, 4)
                    and (meta['deck'], meta['source_page'], meta['group'],
                         meta.get('source_action')) == identity):
                return str(path)
        return None

    def show(self, controller, path):
        page = self.manager.get_page(path, deck_controller=controller)
        if page is None:
            raise ValueError('Page is unavailable')
        controller.load_page(page)
        if getattr(controller.active_page, 'json_path', None) != path:
            raise RuntimeError('Unable to switch pages')

    def discard(self, path):
        meta = self.metadata(path)
        if any(getattr(controller.active_page, 'json_path', None) == path
               for controller in self.manager.pages):
            raise RuntimeError('Temporary page is still active on a deck')
        self._unregister(path)
        # StreamController beta.15 does not clear inactive cached pages when a
        # custom page is unregistered, so keep that private adaptation here.
        discard_cached_page(self.manager, path)
        if self.state_store:
            self.state_store.path(dict(deck=meta['deck'], page=path, group=meta['group'])).unlink(missing_ok=True)
        Path(path).unlink(missing_ok=True)

    def unregister_all(self):
        for path in self.directory.glob('*.json'):
            try:
                self.metadata(str(path))
                self._unregister(str(path))
            except Exception:
                logging.getLogger(__name__).warning(
                    'Unable to unregister temporary page: %s', path, exc_info=True)

    def cleanup(self):
        """Return active owned pages where safe, then remove converged artifacts."""
        for path in list(self.directory.glob('*.json')):
            try:
                meta = self.metadata(str(path))
                active = [controller for controller in self.manager.pages
                          if getattr(controller.active_page, 'json_path', None) == str(path)]
                for controller in active:
                    if controller.serial_number() != meta['deck']:
                        raise RuntimeError('Temporary page is active on another deck')
                    self.show(controller, meta['source_page'])
                self.discard(str(path))
            except Exception:
                logging.getLogger(__name__).warning(
                    'Unable to remove temporary page: %s', path, exc_info=True)

    def invalidate_screenshot_sources(self):
        """Remove pages whose source binding depends on global screenshot settings."""
        removed = set()
        for path in list(self.directory.glob('*.json')):
            try:
                meta = self.metadata(str(path))
                identity = meta.get('image_source')
                if (meta['version'] == 4
                        and identity.get('backend') == 'gamescope'):
                    continue
                if meta['version'] in (1, 2, 3) and identity is None:
                    continue
                active = [controller for controller in self.manager.pages
                          if getattr(controller.active_page, 'json_path', None) == str(path)]
                for controller in active:
                    if controller.serial_number() != meta['deck']:
                        raise RuntimeError('Temporary page is active on another deck')
                    self.show(controller, meta['source_page'])
                self.discard(str(path))
                removed.add(str(path))
            except Exception:
                logging.getLogger(__name__).warning(
                    'Unable to invalidate screenshot page: %s', path, exc_info=True)
        return removed

    def back(self, controller, path):
        meta = self.metadata(path)
        if meta['deck'] != controller.serial_number():
            raise ValueError('This temporary page belongs to another deck')
        self.show(controller, meta['source_page'])
