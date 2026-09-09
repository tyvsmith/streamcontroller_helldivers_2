"""StreamController actions backed by persistent scan assignments."""
from pathlib import Path
from concurrent.futures import CancelledError
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from weakref import WeakSet, ref

from gi.repository import Adw, GLib, Gtk
from loguru import logger as log
from src.backend.PluginManager.InputBases import KeyAction

from .scan_runner import (FLATPAK_TERMINATE_GRACE_SECONDS,
                          TERMINATE_GRACE_SECONDS, run_scan, scan_workers)
from .scan_session import ScanSession
from .scan_state import ScanStateStore
from .scan_artwork import SLOT_COLORS, catalog_colors, badged_icon
from .temporary_scan_page import TemporaryScanPages
from ..stratagem_execution import execute_stratagem


CAPTURE_BACKENDS = ('auto', 'gamescope', 'steam', 'desktop')
AUTOMATIC_ACTION_ID = 'net_jslay_helldivers_2::AutomaticStratagem'
SHUTDOWN_TIMEOUT_SECONDS = (FLATPAK_TERMINATE_GRACE_SECONDS
                            + TERMINATE_GRACE_SECONDS + 1)


def _configured_slot(settings):
    try:
        value = int(settings.get('slot', -1))
    except (ValueError, TypeError):
        return -1
    return min(value, 99) if value > 0 else -1


def _scan_group(settings):
    return str(settings.get('group', 'default')).strip() or 'default'


def _automatic_layout(action):
    """Return stable Auto action records from a beta.15 Page, if available."""
    page = getattr(action, 'page', None)
    data = getattr(page, 'dict', None)
    objects = getattr(page, 'action_objects', None)
    if not isinstance(data, dict) or not isinstance(objects, dict):
        return None
    records = []
    for identifier, key in data.get('keys', {}).items():
        try:
            column, row = (int(value) for value in identifier.split('x', 1))
        except (AttributeError, TypeError, ValueError):
            continue
        states = key.get('states', {}) if isinstance(key, dict) else {}
        for state_key, state_data in states.items():
            try:
                state = int(state_key)
            except (TypeError, ValueError):
                continue
            actions = state_data.get('actions', []) if isinstance(state_data, dict) else []
            if not isinstance(actions, list):
                continue
            for index, action_data in enumerate(actions):
                if (not isinstance(action_data, dict)
                        or action_data.get('id') != AUTOMATIC_ACTION_ID):
                    continue
                settings = action_data.get('settings', {})
                if not isinstance(settings, dict):
                    settings = {}
                action_object = objects.get('keys', {}).get(identifier, {}).get(
                    state, {}).get(index)
                records.append(((row, column, state, index), settings, action_object))
    return sorted(records, key=lambda record: record[0])


def _resolved_layout(action, group=None):
    records = _automatic_layout(action)
    if records is None:
        return None
    group = _scan_group(action.get_settings()) if group is None else group
    records = [record for record in records if _scan_group(record[1]) == group]
    reserved = {_configured_slot(settings) for _, settings, _ in records}
    reserved.discard(-1)
    available = (slot for slot in range(1, 100) if slot not in reserved)
    resolved = []
    for position, settings, action_object in records:
        slot = _configured_slot(settings)
        if slot == -1:
            try:
                slot = next(available)
            except StopIteration:
                raise ValueError('No automatic slots remain') from None
        resolved.append((position, settings, action_object, slot))
    return resolved


def _settings_color_filter(settings):
    value = settings.get('color_filter', 'any')
    return value if value in SLOT_COLORS else 'any'


def scan_mode(action):
    return ('new_page' if (getattr(action, 'new_page_action', False) is True
                           or action.get_settings().get('scan_mode') == 'new_page')
            else 'update')


def capture_backend(action):
    default = action.plugin_base.get_settings().get('capture_backend', 'auto')
    value = action.get_settings().get('capture_backend', default)
    return value if value in CAPTURE_BACKENDS else 'auto'


class ScanCoordinator:
    def __init__(self, plugin, state_dir=None):
        self.plugin = plugin
        self.sessions = {}
        self.actions = WeakSet()
        self.store = ScanStateStore(state_dir) if state_dir is not None else None
        self.state_errors = {}
        self.temporary_pages = None
        self.active_scans = {}
        self.closed = False
        self.compatibility_error = None

    @property
    def enabled(self):
        return (not self.closed
                and self.compatibility_error is None
                and self.plugin.get_settings().get('automatic_stratagems_enabled', False) is True)

    def disable_compatibility(self, message):
        if self.compatibility_error is None:
            self.compatibility_error = str(message)
            log.error(self.compatibility_error)
        self.cancel_all()
        for action in list(self.actions):
            action._pressed = None
            if action.get_is_present():
                action.show()

    @staticmethod
    def show_action_error(action):
        try:
            if action.get_is_present():
                action.show_error(duration=3)
        except Exception:
            log.exception('Unable to show automatic stratagem error')

    def settings_changed(self):
        if not self.enabled:
            self.cancel_all()
        for action in list(self.actions):
            action._pressed = None
            if action.get_is_present():
                action.show()

    def enable_temporary_pages(self, page_manager):
        self.temporary_pages = TemporaryScanPages(
            self.store.directory.parent / 'temporary-pages', page_manager, self.store,
            owner=self.plugin)

    def identity(self, action):
        _, page, group = self.context(action)
        serial = action.deck_controller.serial_number()
        if not isinstance(serial, str) or not serial:
            raise ValueError('Deck serial number unavailable')
        return dict(deck=serial, page=str(Path(page).absolute()), group=group)

    def context(self, action):
        group = _scan_group(action.get_settings())
        return action.deck_controller, action.page.json_path, group

    def session(self, action):
        return self.session_for(self.context(action))

    def session_for(self, context):
        if context not in self.sessions:
            session = ScanSession()
            if self.store:
                try:
                    session = self.store.load(self.context_identity(context), self.plugin.stratagems)
                except (OSError, ValueError) as error:
                    self.state_errors[context] = f'Unable to restore scan state: {error}'
                    log.warning(self.state_errors[context])
            self.sessions[context] = session
        return self.sessions[context]

    def context_identity(self, context):
        deck, page, group = context
        serial = deck.serial_number()
        if not isinstance(serial, str) or not serial:
            raise ValueError('Deck serial number unavailable')
        return dict(deck=serial, page=str(Path(page).absolute()), group=group)

    def source_context(self, context):
        deck, page, group = context
        if self.temporary_pages and Path(page).resolve().parent == self.temporary_pages.directory:
            meta = self.temporary_pages.metadata(page)
            if meta['deck'] == deck.serial_number() and meta['group'] == group:
                return deck, meta['source_page'], group
        return context

    def slot_filters(self, context, session):
        slots = [a for a in list(self.actions) if isinstance(a, AutomaticStratagem)
                 and self.context(a) == context]
        if slots:
            return {a.slot(): a.color_filter() for a in slots}
        return {int(slot): row['filter'] for slot, row in session.checkpoint()['slots'].items()}

    def persist(self, action, session):
        self.persist_context(self.context(action), session)

    def persist_context(self, context, session):
        if not self.store:
            return
        try:
            self.write_state(self.context_identity(context), session)
            self.state_errors.pop(context, None)
        except Exception as error:
            self.state_errors[context] = f'Unable to save scan state: {error}'
            log.warning(self.state_errors[context])

    def share_report(self, context, report, colors, *, replace=False):
        source = self.source_context(context)
        self.session_for(source)
        self._restore_cached_session(source)
        for linked, session in list(self.sessions.items()):
            if linked == context or linked[0] is not source[0] or linked[2] != source[2]:
                continue
            try:
                if self.source_context(linked) != source:
                    continue
            except (OSError, ValueError):
                # A removed cached page must not invalidate a current scan.
                continue
            token = session.begin(self.slot_filters(linked, session), replace=replace)
            if token is not None:
                session.finish(token, report, self.plugin.stratagems, colors)
                self.persist_context(linked, session)
                self.redraw(linked)

    def clear(self, action):
        if not self.enabled or not action.get_is_present():
            return
        context = self.context(action)
        source = self.source_context(context)
        self.session_for(context)
        self.session_for(source)
        self._restore_cached_session(source)
        for linked, session in list(self.sessions.items()):
            if linked[0] is not source[0] or linked[2] != source[2]:
                continue
            try:
                if self.source_context(linked) != source:
                    continue
            except (OSError, ValueError):
                continue
            self.cancel_context(linked)
            session.clear()
            self.persist_context(linked, session)
            self.redraw(linked)

    def page_result(self, action, report, colors):
        rows, columns = self.temporary_pages.layout(action.deck_controller)
        result = ScanSession()
        token = result.begin(range(1, rows * columns - 1))
        result.finish(token, report, self.plugin.stratagems, colors)
        if result.snapshot().recognized:
            self.open_temporary(action, result)

    def write_state(self, identity, session):
        colors = catalog_colors(self.plugin.PATH, self.plugin.stratagems)
        lm = getattr(self.plugin, 'lm', None)
        names = {key: lm.get(f'actions.{key}.name') for key in self.plugin.stratagems} if lm else {}
        self.store.save(identity, session, self.plugin.stratagems, colors, names)

    def open_temporary(self, action, session):
        deck, source, group = self.source_context(self.context(action))
        path = self.temporary_pages.find(deck, source, group)
        if path is not None:
            self.session_for((deck, path, group))
            self.temporary_pages.show(deck, path)
            return
        path = self.temporary_pages.create(deck, source, group, capture_backend(action))
        context = deck, path, group
        try:
            self.sessions[context] = session
            self.write_state(dict(deck=deck.serial_number(), page=path, group=group), session)
            self.temporary_pages.show(deck, path)
        except Exception:
            if getattr(deck.active_page, 'json_path', None) != path:
                self.sessions.pop(context, None)
                self.temporary_pages.discard(path)
            raise

    def back(self, action):
        if not action.get_is_present():
            return
        try:
            self.temporary_pages.back(action.deck_controller, action.page.json_path)
            action.back_error = None
        except Exception as error:
            log.exception('Unable to leave temporary scan page')
            action.back_error = str(error)
            action.render()

    def cached_page(self, action):
        if self.temporary_pages is None:
            return None
        deck, source, group = self.source_context(self.context(action))
        path = self.temporary_pages.find(deck, source, group)
        if path is not None:
            self.session_for((deck, path, group))
        return path

    def _restore_cached_session(self, source):
        if self.temporary_pages is None:
            return None
        deck, page, group = source
        path = self.temporary_pages.find(deck, page, group)
        if path is not None:
            self.session_for((deck, path, group))
        return path

    def open_cached_page(self, action):
        if self.closed or not self.enabled or not action.get_is_present():
            return False
        path = self.cached_page(action)
        if path is None:
            return False
        self.temporary_pages.show(action.deck_controller, path)
        return True

    def delete_cached_page(self, action):
        if self.closed or not self.enabled or not action.get_is_present():
            return False
        source = self.source_context(self.context(action))
        self.cancel_context(source)
        path = self._restore_cached_session(source)
        if path is None:
            return False
        context = source[0], path, source[2]
        self.cancel_context(context)
        if getattr(action.deck_controller.active_page, 'json_path', None) == path:
            self.temporary_pages.back(action.deck_controller, path)
        self.temporary_pages.discard(path)
        self.sessions.pop(context, None)
        self.state_errors.pop(context, None)
        self.redraw(source)
        return True

    def cancel_context(self, context):
        operation = self.active_scans.get(context)
        if operation is not None:
            operation['cancel'].set()
        session = self.sessions.get(context)
        if session is not None and session.snapshot().status == 'scanning':
            session.cancel()
            self.persist_context(context, session)

    def cancel_all(self):
        contexts = set(self.active_scans)
        contexts.update(context for context, session in list(self.sessions.items())
                        if session.snapshot().status == 'scanning')
        for context in contexts:
            self.cancel_context(context)

    def deck_disconnected(self, deck):
        for context in [context for context in self.sessions if context[0] is deck]:
            self.cancel_context(context)
            self.sessions.pop(context, None)

    def disconnect(self, action):
        self.deck_disconnected(action.deck_controller)

    def remove_action(self, action):
        context = getattr(action, '_scan_context', None)
        if context is None:
            context = self.context(action)
        slot = (getattr(action, '_resolved_slot', None)
                if isinstance(action, AutomaticStratagem) else None)
        if isinstance(action, AutomaticStratagem) and slot is None:
            slot = action.slot()
        self.actions.discard(action)
        operation = self.active_scans.get(context)
        if (operation is not None and operation['action']() is action) or slot is not None:
            self.cancel_context(context)
        if slot is not None and context in self.sessions:
            session = self.sessions[context]
            filters, authoritative = self._configured_filters(context)
            if session.reconcile(filters, affected=None if authoritative else {slot}):
                self.persist_context(context, session)
                self.redraw(context)

    def shutdown(self, timeout=SHUTDOWN_TIMEOUT_SECONDS):
        self.closed = True
        operations = list(self.active_scans.values())
        self.cancel_all()
        deadline = monotonic() + timeout
        for operation in operations:
            operation['setup_done'].wait(max(0, deadline - monotonic()))
            thread = operation.get('thread')
            if (operation.get('started') and thread is not None
                    and thread is not current_thread()):
                thread.join(max(0, deadline - monotonic()))
        return all(operation['setup_done'].is_set()
                   and not (operation.get('started') and operation.get('thread') is not None
                            and operation['thread'].is_alive())
                   for operation in operations)

    def register_action(self, action):
        self.actions.add(action)
        if isinstance(action, AutomaticStratagem):
            self.reconcile_action(action)

    def configured_filters(self, context):
        filters, _ = self._configured_filters(context)
        return filters

    def _configured_filters(self, context):
        candidates = [candidate for candidate in list(self.actions)
                      if isinstance(candidate, AutomaticStratagem)
                      and self.context(candidate) == context]
        for candidate in candidates:
            layout = _resolved_layout(candidate, context[2])
            if layout is not None:
                return ({slot: _settings_color_filter(settings)
                         for _, settings, _, slot in layout}, True)
        return ({candidate.slot(): candidate.color_filter()
                 for candidate in candidates if candidate.get_is_present()}, False)

    def reconcile_action(self, action, *, old_context=None, old_slot=None):
        context = self.context(action)
        action._scan_context = context
        session = self.session_for(context)
        affected = {action.slot()}
        if old_context == context and old_slot is not None:
            affected.add(old_slot)
        filters, authoritative = self._configured_filters(context)
        filters[action.slot()] = action.color_filter()
        was_scanning = session.snapshot().status == 'scanning'
        if session.reconcile(filters, affected=None if authoritative else affected):
            if was_scanning:
                self.cancel_context(context)
            self.persist_context(context, session)
            self.redraw(context)

    def redraw(self, context):
        for action in list(self.actions):
            if (getattr(action, 'on_ready_called', False) and action.get_is_present()
                    and self.context(action) == context):
                try:
                    action.render()
                except Exception:
                    log.exception('Unable to render scan action')

    def page_changed(self, controller, old_path, new_path):
        for (deck, path, group), session in list(self.sessions.items()):
            if deck is controller and path == old_path and session.snapshot().status == 'scanning':
                self.cancel_context((deck, path, group))
                self.sessions.pop((deck, path, group), None)

    def start(self, action, *, replace=False):
        if (self.closed or not self.enabled or not action.get_is_present()
                or not self.plugin.input_lock.acquire(blocking=False)):
            return
        finalizer_lock = Lock()
        finalized = False

        def finalize():
            nonlocal finalized
            with finalizer_lock:
                if finalized:
                    return
                finalized = True
                if operation is not None and not operation['started']:
                    operation['setup_done'].set()
                if context is not None and self.active_scans.get(context) is operation:
                    self.active_scans.pop(context, None)
                self.plugin.input_lock.release()

        context = session = token = operation = None
        try:
            context = self.context(action)
            session = self.session(action)
            new_page = scan_mode(action) == 'new_page'
            automatic = [a for a in list(self.actions)
                     if isinstance(a, AutomaticStratagem) and a.get_is_present()
                     and self.context(a) == context]
            slots = [a.slot() for a in automatic]
            filters = {a.slot(): a.color_filter() for a in automatic}
            if new_page:
                if self.temporary_pages is None:
                    raise ValueError('Temporary pages are unavailable')
                self.temporary_pages.layout(action.deck_controller)
                filters = self.slot_filters(context, session)
            token = session.begin(filters, replace=replace)
            if token is None:
                finalize()
                return
            operation = {'cancel': Event(), 'setup_done': Event(), 'started': False,
                         'thread': None, 'action': ref(action)}
            self.active_scans[context] = operation
            self.persist(action, session)
            self.redraw(context)
            if not new_page and (not slots or len(slots) != len(set(slots))):
                session.fail(token, 'Add uniquely numbered Automatic slots')
                self.persist(action, session)
                finalize()
                self.redraw(context)
                return
            root = self.plugin.PATH
            backend = capture_backend(action)
            workers = scan_workers(self.plugin.get_settings().get('scan_workers', 2))

            def finish(report, error, colors=None):
                try:
                    if self.closed or not session.is_active(token):
                        return False
                    if not self.enabled or not action.get_is_present() or self.context(action) != context:
                        session.cancel()
                    elif error:
                        if session.fail(token, error):
                            self.persist(action, session)
                        self.show_action_error(action)
                        log.warning('Stratagem scan failed: {}', error)
                    else:
                        if session.finish(token, report, self.plugin.stratagems, colors):
                            self.persist(action, session)
                            if session.snapshot().status in ('ready', 'partial'):
                                self.share_report(context, report, colors, replace=replace)
                                if new_page:
                                    self.page_result(action, report, colors)
                        log.info('Stratagem scan: {}', session.snapshot().message)
                    self.redraw(context)
                except Exception as error:
                    failure = token if session.is_active(token) else session.begin(
                        self.slot_filters(context, session))
                    session.fail(failure, str(error))
                    self.persist(action, session)
                    log.exception('Unable to finish stratagem scan')
                    self.redraw(context)
                return False

            def worker():
                try:
                    try:
                        colors = catalog_colors(self.plugin.PATH, self.plugin.stratagems)
                        report = run_scan(root, backend=backend, workers=workers,
                                          cancel_event=operation['cancel'])
                    except CancelledError:
                        return
                    except Exception as error:
                        GLib.idle_add(finish, None, str(error))
                    else:
                        GLib.idle_add(finish, report, None, colors)
                except Exception:
                    session.cancel()
                    try:
                        self.persist_context(context, session)
                    except Exception:
                        log.exception('Unable to persist cancelled stratagem scan')
                    log.exception('Unable to queue stratagem scan result')
                finally:
                    finalize()

            operation['thread'] = Thread(target=worker, name='hd2-scan', daemon=True)
            if (operation['cancel'].is_set() or self.closed or not self.enabled
                    or not action.get_is_present() or self.context(action) != context
                    or not session.is_active(token)):
                raise CancelledError()
            operation['thread'].start()
            operation['started'] = True
            operation['setup_done'].set()
        except CancelledError:
            if operation is not None:
                operation['cancel'].set()
                operation['setup_done'].set()
            if session is not None and token is not None and session.is_active(token):
                session.cancel()
                self.persist_context(context, session)
            finalize()
        except Exception as error:
            if operation is not None:
                operation['cancel'].set()
                operation['setup_done'].set()
            if session is not None:
                if token is None and context is not None:
                    try:
                        token = session.begin(self.slot_filters(context, session))
                    except Exception:
                        log.exception('Unable to initialize failed stratagem scan state')
                if token is not None and session.is_active(token):
                    session.fail(token, str(error))
                try:
                    self.persist_context(context, session)
                except Exception:
                    log.exception('Unable to persist failed stratagem scan setup')
            log.exception('Unable to start stratagem scan')
            finalize()
            if context is not None:
                self.redraw(context)
            self.show_action_error(action)


class ScanActionBase(KeyAction):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True
        self.displayed = None

    @property
    def coordinator(self):
        return self.plugin_base.scan_coordinator

    def on_ready(self):
        self.coordinator.register_action(self)
        self.show()

    def show(self):
        self.displayed = None
        self.render()

    def on_update(self):
        self.show()

    def on_tick(self):
        if self.get_is_present():
            self.render()

    def on_disconnect(self):
        self.coordinator.deck_disconnected(self.deck_controller)
        self.coordinator.actions.discard(self)
        self.displayed = None

    def on_remove(self):
        self.coordinator.remove_action(self)
        self.displayed = None

    def on_removed_from_cache(self):
        self.on_remove()

    def on_key_up(self, data=None):
        pass

    def on_key_short_up(self, data=None):
        pass

    def get_config_rows(self):
        group = Adw.EntryRow(title='Scan group')
        group.set_text(str(self.get_settings().get('group', 'default')))
        group.connect('changed', lambda row: self.configure('group', row.get_text().strip() or 'default'))
        return [group]

    def configure(self, key, value):
        old_context = self.coordinator.context(self)
        old_slot = self.slot() if isinstance(self, AutomaticStratagem) else None
        session = self.coordinator.session(self)
        if session.snapshot().status == 'scanning':
            self.coordinator.cancel_context(old_context)
        settings = self.get_settings()
        settings[key] = value
        self.set_settings(settings)
        if isinstance(self, AutomaticStratagem) and key in ('group', 'slot', 'color_filter'):
            self.coordinator.reconcile_action(self, old_context=old_context, old_slot=old_slot)
        self.displayed = None
        self.render()

    def artwork(self, filename, top, center, bottom, *, center_size=None):
        self.set_media(media_path=str(Path(self.plugin_base.PATH) / filename),
                       fps=30, loop=True)
        self.set_top_label(top)
        self.set_center_label(center, font_size=center_size)
        self.set_bottom_label(bottom)
        self.set_background_color([26, 26, 26, 255])


class ScanStratagems(ScanActionBase):
    def on_key_down(self, data=None):
        self._held = False

    def on_key_short_up(self, data=None):
        if not getattr(self, '_held', False):
            if scan_mode(self) == 'new_page':
                try:
                    if self.coordinator.open_cached_page(self):
                        return
                except Exception:
                    log.exception('Unable to open cached stratagem page')
                    self.coordinator.show_action_error(self)
                    return
            self.coordinator.start(self, replace=True)

    def on_key_hold_start(self, data=None):
        if not getattr(self, '_held', False):
            self._held = True
            if scan_mode(self) == 'new_page':
                try:
                    self.coordinator.delete_cached_page(self)
                except Exception:
                    log.exception('Unable to delete cached stratagem page')
                    self.coordinator.show_action_error(self)
            else:
                self.coordinator.clear(self)

    def on_key_hold_stop(self, data=None):
        pass

    def render(self):
        mode = scan_mode(self)
        try:
            cached = bool(self.coordinator.cached_page(self)) if mode == 'new_page' else False
        except Exception:
            cached = False
            log.exception('Unable to inspect cached stratagem page')
        top = 'Auto' if mode == 'new_page' else 'Scan'
        if not self.coordinator.enabled:
            shown = 'disabled', mode, cached
            if self.displayed == shown:
                return
            filename = 'scan.png' if cached else ('scan-new-page.png' if mode == 'new_page'
                                                  else 'scan-update.png')
            self.artwork('automatic_stratagems/assets/icons/' + filename,
                         top, '', 'Disabled')
            self.displayed = shown
            return
        snapshot = self.coordinator.session(self).snapshot()
        shown = snapshot.revision, cached
        if self.displayed == shown:
            return
        filename = ('scan.png' if cached else 'scan-new-page.png') if mode == 'new_page' else 'scan-update.png'
        if snapshot.status == 'scanning':
            filename = 'scanning.mp4'
        status = snapshot.status.title() if snapshot.status in ('failed', 'partial') else ''
        self.artwork('automatic_stratagems/assets/icons/' + filename,
                     top, status, 'Stratagems')
        self.displayed = shown

    def get_config_rows(self):
        rows = super().get_config_rows()
        backend = Adw.ComboRow(title='Capture backend',
                               subtitle='Auto tries Gamescope, Steam F12, then OS')
        backend.set_model(Gtk.StringList.new(['Auto', 'Gamescope', 'Steam', 'OS']))
        backend.set_selected(CAPTURE_BACKENDS.index(capture_backend(self)))
        backend.connect('notify::selected', lambda row, _: self.configure(
            'capture_backend', CAPTURE_BACKENDS[row.get_selected()]))
        rows.append(backend)
        if scan_mode(self) == 'new_page':
            rows.append(Adw.ActionRow(title='Tap to open or scan · Hold to delete',
                                      subtitle='Tap reopens the cached page, or scans to create it. Back retains it. Hold deletes the cached page.'))
        else:
            rows.append(Adw.ActionRow(title='Tap to scan · Hold to clear',
                                      subtitle='Scan rebuilds this group. Clear resets its Auto selections on this page and linked cached pages; slot numbers and color filters stay.'))
        rows.append(Adw.ActionRow(title='Last scan',
                                  subtitle=self.coordinator.session(self).snapshot().message or 'No scan yet'))
        if self.coordinator.store and scan_mode(self) != 'new_page':
            error = self.coordinator.state_errors.get(self.coordinator.context(self))
            try:
                location = str(self.coordinator.store.path(self.coordinator.identity(self)))
            except ValueError as identity_error:
                location = str(identity_error)
            rows.append(Adw.ActionRow(title='Scan state file', subtitle=error or location))
        return rows


class AutoStratagems(ScanStratagems):
    new_page_action = True


class AutomaticStratagem(ScanActionBase):
    def configured_slot(self):
        return _configured_slot(self.get_settings())

    def slot(self):
        configured = self.configured_slot()
        if configured != -1:
            self._resolved_slot = configured
            return configured
        layout = _resolved_layout(self)
        if layout is not None:
            for _, _, action_object, slot in layout:
                if action_object is self:
                    self._resolved_slot = slot
                    return slot
        # Preserve compatibility with incomplete Page implementations. Native
        # beta.15 exposes both page.dict and page.action_objects.
        self._resolved_slot = 1
        return self._resolved_slot

    def color_filter(self):
        return _settings_color_filter(self.get_settings())

    def get_config_rows(self):
        rows = super().get_config_rows()
        configured = self.configured_slot()
        slot = Adw.SpinRow.new_with_range(-1, 99, 1)
        slot.set_title('Automatic slot')
        slot.set_subtitle('-1 assigns a slot from this button position')
        slot.set_value(configured)
        previous = [configured]
        updating = [False]

        def slot_changed(row, _):
            if updating[0]:
                return
            value = int(row.get_value())
            if value == 0:
                value = 1 if previous[0] == -1 else -1
                updating[0] = True
                row.set_value(value)
                updating[0] = False
            previous[0] = value
            self.configure('slot', value)

        slot.connect('notify::value', slot_changed)
        rows.append(slot)
        color = Adw.ComboRow(title='Color filter', subtitle='Only fill vacancies with this icon color')
        color.set_model(Gtk.StringList.new(['Any', 'Red', 'Blue', 'Green', 'Yellow']))
        color.set_selected(SLOT_COLORS.index(self.color_filter()))
        color.connect('notify::selected', lambda row, _: self.configure(
            'color_filter', SLOT_COLORS[row.get_selected()]))
        rows.append(color)
        rows.append(Adw.ActionRow(title='Tap to execute · Hold to scan',
                                  subtitle='Hold any Auto button, including an empty slot, to rescan and update this group.'))
        return rows

    def controllable(self):
        return (not self.has_custom_user_asset() and self.has_image_control()
                and all(self.has_label_controls()))

    def render(self):
        if not self.coordinator.enabled:
            shown = 'disabled', self.slot(), self.color_filter()
            if self.displayed == shown:
                return
            self.artwork(f'automatic_stratagems/assets/icons/auto-{self.color_filter()}.png',
                         'AUTO', str(self.slot()), 'Disabled', center_size=28)
            self.displayed = shown
            return
        snapshot = self.coordinator.session(self).snapshot()
        slot = self.slot()
        key = snapshot.assignments.get(slot)
        shown = snapshot.revision, slot, key
        if self.displayed == shown:
            return
        self.displayed = None
        if snapshot.status == 'scanning':
            self.artwork('automatic_stratagems/assets/icons/scanning.mp4', f'AUTO {slot}', '', 'Scanning')
        elif key:
            path = str(Path(self.plugin_base.PATH) / 'assets/icons' / (key + '.png'))
            if slot in snapshot.unconfirmed_slots:
                self.set_media(image=badged_icon(path).copy())
            else:
                self.set_media(media_path=path)
            for position in ('top', 'center', 'bottom'):
                label = self.plugin_base.lm.get(f'actions.{key}.labels.{position}', '') if self.plugin_base.get_show_labels() else ''
                getattr(self, f'set_{position}_label')(label)
            self.set_background_color([0, 0, 0, 255])
        else:
            label = ('Unknown' if slot in snapshot.unknown_slots else
                     'Scanning' if snapshot.status == 'scanning' else
                     'Stale' if key else 'Empty')
            self.artwork(f'automatic_stratagems/assets/icons/auto-{self.color_filter()}.png', 'AUTO', str(slot), label,
                         center_size=28)
        self.displayed = shown

    def on_key_down(self, data=None):
        self._held = False
        self.coordinator.reconcile_action(self)
        snapshot = self.coordinator.session(self).snapshot()
        slot = self.slot()
        self._pressed = (self.coordinator.context(self), snapshot.revision, slot,
                         self.color_filter(),
                         snapshot.assignments.get(slot))

    def on_key_hold_start(self, data=None):
        if not getattr(self, '_held', False):
            self._held = True
            self._pressed = None
            self.coordinator.start(self, replace=True)

    def on_key_hold_stop(self, data=None):
        pass

    def on_key_short_up(self, data=None):
        pressed = getattr(self, '_pressed', None)
        self._pressed = None
        if not self.coordinator.enabled or getattr(self, '_held', False) or pressed is None:
            return
        self.coordinator.reconcile_action(self)
        snapshot = self.coordinator.session(self).snapshot()
        slot = self.slot()
        color_filter = self.color_filter()
        key = snapshot.assignments.get(slot)
        if (not self.get_is_present() or not self.controllable()
                or snapshot.status not in ('ready', 'partial', 'failed') or key is None
                or self.displayed != (snapshot.revision, slot, key)):
            return
        context = self.coordinator.context(self)
        if pressed != (context, snapshot.revision, slot, color_filter, key):
            return

        def still_current():
            current = self.coordinator.session(self).snapshot()
            return (self.coordinator.enabled and self.coordinator.context(self) == context
                    and self.get_is_present() and self.controllable() and self.slot() == slot
                    and self.color_filter() == color_filter
                    and current.status in ('ready', 'partial', 'failed')
                    and current.revision == snapshot.revision
                    and current.assignments.get(slot) == key
                    and self.displayed == (current.revision, slot, key))

        busy = self.plugin_base.input_lock.locked()
        success = execute_stratagem(
            self.plugin_base, key, self.plugin_base.stratagems[key], guard=still_current)
        if not success and not busy and still_current():
            self.show_error(duration=3)


class TemporaryScanBack(ScanActionBase):
    def render(self):
        if getattr(self, 'back_error', None):
            self.artwork('assets/icons/_stepbakcward.png', 'Back failed', '', 'See settings')
        else:
            self.artwork('assets/icons/_stepbakcward.png', '', '', 'Back')

    def on_key_down(self, data=None):
        pass

    def on_key_short_up(self, data=None):
        self.coordinator.back(self)

    def get_config_rows(self):
        return [Adw.ActionRow(title='Return to previous page',
                             subtitle=getattr(self, 'back_error', None) or
                             'Return first, then delete this temporary page and its scan state.')]
