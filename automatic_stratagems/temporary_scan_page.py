"""Owned temporary pages with explicit return navigation and cleanup."""

import json
import logging
from pathlib import Path
from uuid import uuid4

PLUGIN_ID = 'net_jslay_helldivers_2'
MARKER = 'hd2_temporary_scan'


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


class TemporaryScanPages:
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
        if self.owner is not None and callable(getattr(self.owner, 'register_page', None)):
            self.owner.register_page(path)
        else:
            self.manager.register_page(path)

    def _unregister(self, path):
        registered = getattr(self.manager, 'custom_pages', None)
        if registered is None or path in registered:
            self.manager.unregister_page(path)
        owned = getattr(self.owner, 'registered_pages', None)
        if owned is not None:
            while path in owned:
                owned.remove(path)

    def metadata(self, path):
        file = Path(path)
        if file.resolve().parent != self.directory:
            raise ValueError('Not an owned temporary scan page')
        data = json.loads(file.read_text())
        meta = data.get(MARKER) if isinstance(data, dict) else None
        if (not isinstance(meta, dict) or meta.get('owner') != PLUGIN_ID
                or meta.get('version') not in (1, 2)
                or not all(isinstance(meta.get(key), str) and meta[key]
                           for key in ('source_page', 'deck', 'group'))):
            raise ValueError('Not an owned temporary scan page')
        if meta['version'] == 2 and not valid_source_action(meta.get('source_action')):
            raise ValueError('Invalid temporary page source action')
        return meta

    @staticmethod
    def layout(controller):
        rows, columns = controller.deck.key_layout()
        if rows * columns < 3:
            raise ValueError('Temporary scan pages require at least three keys')
        return rows, columns

    def create(self, controller, source_page, group, backend, source_action):
        if not valid_source_action(source_action):
            raise ValueError('Source action address unavailable')
        rows, columns = self.layout(controller)
        actions = [key_action('TemporaryScanBack', {'group': group}),
                   key_action('ScanStratagems', {'group': group, 'scan_mode': 'update',
                                               'capture_backend': backend})]
        actions.extend(key_action('AutomaticStratagem', {'group': group, 'slot': slot})
                       for slot in range(1, rows * columns - 1))
        data = {MARKER: dict(owner=PLUGIN_ID, version=2, source_page=str(Path(source_page).absolute()),
                            deck=controller.serial_number(), group=group,
                            source_action=dict(source_action)),
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

    def find(self, controller, source_page, group, source_action):
        if not valid_source_action(source_action):
            raise ValueError('Source action address unavailable')
        identity = (controller.serial_number(), str(Path(source_page).absolute()),
                    group, source_action)
        for path in sorted(self.directory.glob('*.json')):
            try:
                meta = self.metadata(str(path))
            except (ValueError, FileNotFoundError):
                continue
            if (meta['version'] == 2
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
        for cached in self.manager.pages.values():
            entry = cached.get(path)
            if entry:
                entry['page'].clear_action_objects()
                cached.pop(path, None)
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

    def back(self, controller, path):
        meta = self.metadata(path)
        if meta['deck'] != controller.serial_number():
            raise ValueError('This temporary page belongs to another deck')
        self.show(controller, meta['source_page'])
