"""StreamController actions backed by persistent scan assignments."""
from dataclasses import dataclass
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


CAPTURE_BACKENDS = ('auto', 'gamescope', 'steam', 'desktop', 'portal', 'x11')
AUTOMATIC_ACTION_ID = 'net_jslay_helldivers_2::AutomaticStratagem'
SHUTDOWN_TIMEOUT_SECONDS = (FLATPAK_TERMINATE_GRACE_SECONDS
                            + TERMINATE_GRACE_SECONDS + 1)


@dataclass(frozen=True)
class ScanAttempt:
    id: int
    status: str
    message: str
    session: object = None
    token: int | None = None


def _configured_slot(settings):
    try:
        value = int(settings.get('slot', -1))
    except (ValueError, TypeError):
        return -1
    return min(value, 99) if value > 0 else -1


def _scan_group(settings):
    return str(settings.get('group', 'default')).strip() or 'default'


def _source_action_address(action):
    input_ident = getattr(action, 'input_ident', None)
    input_type = getattr(input_ident, 'input_type', None)
    identifier = getattr(input_ident, 'json_identifier', None)
    state = getattr(action, 'state', None)
    if (not isinstance(input_type, str) or not input_type
            or not isinstance(identifier, str) or not identifier):
        raise ValueError('Source action address unavailable')
    if type(state) is not int or state < 0:
        raise ValueError('Source action state unavailable')
    index = None
    objects = getattr(getattr(action, 'page', None), 'action_objects', None)
    if isinstance(objects, dict):
        state_actions = objects.get(input_type, {}).get(identifier, {}).get(state, {})
        if isinstance(state_actions, dict):
            index = next((candidate for candidate, value in state_actions.items()
                          if type(candidate) is int and candidate >= 0 and value is action), None)
    if index is None:
        get_index = getattr(action, 'get_own_action_index', None)
        candidate = get_index() if callable(get_index) else None
        if type(candidate) is int and candidate >= 0:
            index = candidate
    if index is None:
        raise ValueError('Source action index unavailable')
    return dict(input_type=input_type, identifier=identifier, state=state, index=index)


def _topology_index(value):
    if type(value) is int:
        return value if value >= 0 else None
    if (not isinstance(value, str) or not value.isascii()
            or not value.isdecimal()):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def _automatic_layout(action):
    """Return stable Auto action records from a beta.15 Page, if available."""
    page = getattr(action, 'page', None)
    data = getattr(page, 'dict', None)
    objects = getattr(page, 'action_objects', None)
    if not isinstance(data, dict) or not isinstance(objects, dict):
        return None
    keys = data.get('keys')
    object_keys = objects.get('keys')
    if not isinstance(keys, dict) or not isinstance(object_keys, dict):
        return None
    records = []
    for identifier, key in keys.items():
        parts = identifier.split('x', 1) if isinstance(identifier, str) else []
        coordinates = [_topology_index(value) for value in parts]
        if len(coordinates) != 2 or any(value is None for value in coordinates):
            return None
        column, row = coordinates
        if not isinstance(key, dict):
            return None
        states = key.get('states', {})
        if not isinstance(states, dict):
            return None
        for state_key, state_data in states.items():
            state = _topology_index(state_key)
            if state is None:
                return None
            if not isinstance(state_data, dict):
                return None
            actions = state_data.get('actions', [])
            if not isinstance(actions, list):
                return None
            for index, action_data in enumerate(actions):
                if not isinstance(action_data, dict):
                    return None
                if action_data.get('id') != AUTOMATIC_ACTION_ID:
                    continue
                settings = action_data.get('settings', {})
                if not isinstance(settings, dict):
                    return None
                input_objects = object_keys.get(identifier)
                if input_objects is not None and not isinstance(input_objects, dict):
                    return None
                state_objects = (input_objects.get(state)
                                 if isinstance(input_objects, dict) else None)
                if state_objects is not None and not isinstance(state_objects, dict):
                    return None
                action_object = (state_objects.get(index)
                                 if isinstance(state_objects, dict) else None)
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
    """Coordinate page sessions and input exclusion; queue worker results for the UI."""

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

    def _attached_actions(self):
        actions = list(self.actions)
        attached = []
        for action in actions:
            # StreamController can detach a cached page without an action removal callback.
            if getattr(action, 'page', None) is None:
                self.actions.discard(action)
            else:
                attached.append(action)
        return attached

    def disable_compatibility(self, message):
        if self.compatibility_error is None:
            self.compatibility_error = str(message)
            log.error(self.compatibility_error)
        self.cancel_all()
        for action in self._attached_actions():
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

    def loading(self, action):
        presentation = getattr(action, '__dict__', {}).get('_scan_presentation')
        if presentation is None:
            return False
        context, session, token = presentation
        try:
            active = self.context(action) == context and session.is_active(token)
        except Exception:
            active = False
        if not active:
            action._scan_presentation = None
        return active

    @staticmethod
    def begin_page_attempt(action):
        previous = getattr(action, '__dict__', {}).get('_scan_attempt')
        attempt = ScanAttempt((previous.id if isinstance(previous, ScanAttempt) else 0) + 1,
                              'scanning', 'Scanning')
        action._scan_attempt = attempt
        return attempt.id

    @staticmethod
    def bind_page_attempt(action, attempt_id, session, token):
        attempt = getattr(action, '__dict__', {}).get('_scan_attempt')
        if not isinstance(attempt, ScanAttempt) or attempt.id != attempt_id:
            return False
        action._scan_attempt = ScanAttempt(
            attempt.id, attempt.status, attempt.message, session, token)
        return True

    @staticmethod
    def finish_page_attempt(action, attempt_id, status, message,
                            *, session=None, token=None):
        attempt = getattr(action, '__dict__', {}).get('_scan_attempt')
        if not isinstance(attempt, ScanAttempt) or attempt.id != attempt_id:
            return False
        if session is not None and (attempt.session is not session or attempt.token != token):
            return False
        action._scan_attempt = ScanAttempt(
            attempt.id, status, str(message), attempt.session, attempt.token)
        return True

    def cancel_page_attempt(self, context, session):
        for action in self._attached_actions():
            attempt = getattr(action, '__dict__', {}).get('_scan_attempt')
            presentation = getattr(action, '__dict__', {}).get('_scan_presentation')
            if (isinstance(attempt, ScanAttempt) and attempt.status == 'scanning'
                    and attempt.session is session and presentation is not None
                    and presentation[:2] == (context, session)
                    and attempt.token == presentation[2]
                    and session.is_active(attempt.token)):
                self.finish_page_attempt(
                    action, attempt.id, 'cancelled', 'Scan cancelled',
                    session=session, token=attempt.token)

    def settings_changed(self):
        if not self.enabled:
            self.cancel_all()
        for action in self._attached_actions():
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

    def slot_filters(self, context, session):
        slots = [a for a in self._attached_actions() if isinstance(a, AutomaticStratagem)
                 and self.context(a) == context]
        if slots:
            return {slot: action.color_filter() for action in slots
                    if (slot := action.slot()) is not None}
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

    def clear(self, action):
        if not self.enabled or not action.get_is_present():
            return
        context = self.context(action)
        self.cancel_context(context)
        session = self.session_for(context)
        session.clear()
        self.persist_context(context, session)
        self.redraw(context)

    def page_result(self, action, report, colors):
        rows, columns = self.temporary_pages.layout(action.deck_controller)
        result = ScanSession()
        token = result.begin(range(1, rows * columns - 1))
        result.finish(token, report, self.plugin.stratagems, colors)
        if result.snapshot().recognized:
            self.open_temporary(action, result)
        return result.snapshot()

    def write_state(self, identity, session):
        colors = catalog_colors(self.plugin.PATH, self.plugin.stratagems)
        lm = getattr(self.plugin, 'lm', None)
        names = {key: lm.get(f'actions.{key}.name') for key in self.plugin.stratagems} if lm else {}
        self.store.save(identity, session, self.plugin.stratagems, colors, names)

    def open_temporary(self, action, session):
        deck, source, group = self.context(action)
        source_action = _source_action_address(action)
        path = self.temporary_pages.find(deck, source, group, source_action)
        if path is not None:
            self.session_for((deck, path, group))
            self.temporary_pages.show(deck, path)
            return
        path = self.temporary_pages.create(
            deck, source, group, capture_backend(action), source_action)
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
        deck, source, group = self.context(action)
        path = self.temporary_pages.find(
            deck, source, group, _source_action_address(action))
        if path is not None:
            self.session_for((deck, path, group))
        return path

    def _restore_cached_session(self, source, source_action):
        if self.temporary_pages is None:
            return None
        deck, page, group = source
        path = self.temporary_pages.find(deck, page, group, source_action)
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
        source = self.context(action)
        presentation = getattr(action, '__dict__', {}).get('_scan_presentation')
        if (presentation is not None and presentation[0] == source
                and presentation[1].is_active(presentation[2])):
            self.cancel_context(source)
        source_action = _source_action_address(action)
        path = self._restore_cached_session(source, source_action)
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
            transient = session.is_transient()
            if transient:
                self.cancel_page_attempt(context, session)
            session.cancel()
            if not transient:
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
        candidates = [candidate for candidate in self._attached_actions()
                      if isinstance(candidate, AutomaticStratagem)
                      and self.context(candidate) == context]
        for candidate in candidates:
            layout = _resolved_layout(candidate, context[2])
            if layout is not None:
                return ({slot: _settings_color_filter(settings)
                         for _, settings, _, slot in layout}, True)
        return ({slot: candidate.color_filter() for candidate in candidates
                 if candidate.get_is_present()
                 and (slot := candidate.slot()) is not None}, False)

    def reconcile_action(self, action, *, old_context=None, old_slot=None):
        context = self.context(action)
        action._scan_context = context
        session = self.session_for(context)
        slot = action.slot()
        if slot is None and old_slot is None:
            return
        affected = set() if slot is None else {slot}
        if old_context == context and old_slot is not None:
            affected.add(old_slot)
        filters, authoritative = self._configured_filters(context)
        if slot is not None:
            filters[slot] = action.color_filter()
        was_scanning = session.snapshot().status == 'scanning'
        reconcile_all = authoritative and slot is not None
        if session.reconcile(filters, affected=None if reconcile_all else affected):
            if was_scanning:
                self.cancel_context(context)
            self.persist_context(context, session)
            self.redraw(context)

    def redraw(self, context):
        for action in self._attached_actions():
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

    def start(self, action, *, replace=False, regenerate=False):
        if self.closed or not self.enabled or not action.get_is_present():
            return
        if not self.plugin.input_lock.acquire(blocking=False):
            self.show_action_error(action)
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
        attempt_id = None
        new_page = False
        try:
            context = self.context(action)
            session = self.session(action)
            new_page = scan_mode(action) == 'new_page'
            if new_page and session.snapshot().status == 'scanning':
                presentation = getattr(
                    action, '__dict__', {}).get('_scan_presentation')
                owns_scan = (presentation is not None and presentation[0] == context
                             and presentation[1] is session
                             and session.is_active(presentation[2]))
                if not regenerate or not owns_scan:
                    finalize()
                    self.show_action_error(action)
                    return
            if new_page:
                attempt_id = self.begin_page_attempt(action)
            automatic = [a for a in self._attached_actions()
                     if isinstance(a, AutomaticStratagem) and a.get_is_present()
                     and self.context(a) == context]
            slots = [a.slot() for a in automatic]
            filters = {slot: a.color_filter() for a, slot in zip(automatic, slots)
                       if slot is not None}
            if new_page:
                if self.temporary_pages is None:
                    raise ValueError('Temporary pages are unavailable')
                self.temporary_pages.layout(action.deck_controller)
                _source_action_address(action)
                filters = self.slot_filters(context, session)
                if regenerate:
                    if session.snapshot().status == 'scanning':
                        self.cancel_context(context)
                    self.delete_cached_page(action)
            elif regenerate:
                raise ValueError('Only page openers can regenerate a cache')
            if not new_page and any(slot is None for slot in slots):
                log.warning('Automatic slot position unavailable; choose an explicit slot')
                self.show_action_error(action)
                finalize()
                return
            token = session.begin(filters, replace=replace, transient=new_page)
            if token is None:
                if new_page:
                    self.finish_page_attempt(
                        action, attempt_id, 'cancelled', 'Scan already in progress')
                finalize()
                self.show_action_error(action)
                return
            if new_page:
                self.bind_page_attempt(action, attempt_id, session, token)
            action._scan_presentation = context, session, token
            operation = {'cancel': Event(), 'setup_done': Event(), 'started': False,
                         'thread': None, 'action': ref(action)}
            self.active_scans[context] = operation
            if not new_page:
                self.persist(action, session)
            self.redraw(context)
            if not new_page and (not slots or len(slots) != len(set(slots))):
                session.fail(token, 'Add uniquely numbered Automatic slots')
                self.persist(action, session)
                self.show_action_error(action)
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
                        if new_page:
                            self.finish_page_attempt(
                                action, attempt_id, 'cancelled', 'Scan cancelled',
                                session=session, token=token)
                        session.cancel()
                    elif error:
                        if session.fail(token, error):
                            if not new_page:
                                self.persist(action, session)
                            else:
                                self.finish_page_attempt(
                                    action, attempt_id, 'failed', error,
                                    session=session, token=token)
                        self.show_action_error(action)
                        log.warning('Stratagem scan failed: {}', error)
                    else:
                        if new_page:
                            accepted = session.finish_transient(token)
                        else:
                            accepted = session.finish(
                                token, report, self.plugin.stratagems, colors)
                        if accepted:
                            result = (self.page_result(action, report, colors)
                                      if new_page else session.snapshot())
                            if new_page:
                                self.finish_page_attempt(
                                    action, attempt_id, result.status, result.message,
                                    session=session, token=token)
                            if not new_page:
                                self.persist(action, session)
                            if result.status == 'failed':
                                self.show_action_error(action)
                            log.info('Stratagem scan: {}', result.message)
                    self.redraw(context)
                except Exception as error:
                    if new_page:
                        if session.is_active(token):
                            session.fail(token, str(error))
                        self.finish_page_attempt(
                            action, attempt_id, 'failed', error,
                            session=session, token=token)
                    else:
                        failure = token if session.is_active(token) else session.begin(
                            self.slot_filters(context, session))
                        session.fail(failure, str(error))
                        self.persist(action, session)
                    self.show_action_error(action)
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
                    active = session.is_active(token)
                    transient = session.is_transient(token)
                    if active:
                        if transient:
                            self.finish_page_attempt(
                                action, attempt_id, 'failed',
                                'Unable to queue scan result',
                                session=session, token=token)
                        session.cancel()
                    try:
                        if active and not transient:
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
                transient = session.is_transient(token)
                if transient:
                    self.finish_page_attempt(
                        action, attempt_id, 'cancelled', 'Scan cancelled',
                        session=session, token=token)
                session.cancel()
                if not transient:
                    self.persist_context(context, session)
            finalize()
        except Exception as error:
            if operation is not None:
                operation['cancel'].set()
                operation['setup_done'].set()
            if session is not None:
                if new_page:
                    if token is not None and session.is_active(token):
                        session.fail(token, str(error))
                    if attempt_id is not None:
                        self.finish_page_attempt(
                            action, attempt_id, 'failed', error,
                            session=session if token is not None else None,
                            token=token)
                else:
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
    """Connect StreamController action lifecycle and rendering to the coordinator."""

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
        group.set_show_apply_button(True)

        def commit_group(*_):
            value = group.get_text().strip() or 'default'
            if group.get_text() != value:
                group.set_text(value)
            if value != _scan_group(self.get_settings()):
                self.configure('group', value)

        group.connect('apply', commit_group)
        focus = Gtk.EventControllerFocus()
        focus.connect('leave', commit_group)
        group.add_controller(focus)
        return [group, Adw.ActionRow(
            title='Group scope',
            subtitle='Shared only with the same group on this deck and page')]

    def configure(self, key, value, *, remove=False):
        old_context = self.coordinator.context(self)
        old_slot = self.slot() if isinstance(self, AutomaticStratagem) else None
        settings = self.get_settings()
        if remove and key not in settings:
            return
        session = self.coordinator.session(self)
        if session.snapshot().status == 'scanning':
            self.coordinator.cancel_context(old_context)
        if remove:
            settings.pop(key)
        else:
            settings[key] = value
        self.set_settings(settings)
        if isinstance(self, AutomaticStratagem) and key in ('group', 'slot', 'color_filter'):
            self.coordinator.reconcile_action(self, old_context=old_context, old_slot=old_slot)
        self.displayed = None
        self.render()

    def artwork(self, filename, top, center, bottom, *, center_size=None):
        self.set_media(media_path=str(Path(self.plugin_base.PATH) / filename),
                       fps=30, loop=True)
        if Path(filename).name == 'scanning.mp4':
            media_player = getattr(self.deck_controller, 'media_player', None)
            # beta.15 can retain its idle cache until after a short scan ends.
            if isinstance(getattr(media_player, '_cached_needs_ticks', None), bool):
                media_player._cached_needs_ticks = True
                wake = getattr(media_player, '_wake_event', None)
                wake_set = getattr(wake, 'set', None)
                if callable(wake_set):
                    wake_set()
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
                self.coordinator.start(self, replace=True, regenerate=True)
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
        session = self.coordinator.session(self)
        snapshot = session.snapshot()
        loading = self.coordinator.loading(self)
        shown = snapshot.revision, cached, loading
        if self.displayed == shown:
            return
        filename = ('scan.png' if cached else 'scan-new-page.png') if mode == 'new_page' else 'scan-update.png'
        if loading:
            filename = 'scanning.mp4'
        self.artwork('automatic_stratagems/assets/icons/' + filename,
                     top, '', 'Stratagems')
        self.displayed = shown

    def get_config_rows(self):
        rows = super().get_config_rows()
        mode = scan_mode(self)
        backend = Adw.ComboRow(title='Capture backend',
                               subtitle=('Use plugin default follows plugin settings. '
                                         'Generated pages retain the effective setting used when created. '
                                         'Steam F12 saves a screenshot.'))
        backend.set_model(Gtk.StringList.new([
            'Use plugin default', 'Automatic', 'Gamescope', 'Steam F12', 'Hyprland desktop',
            'Portal window', 'X11 window']))
        settings = self.get_settings()
        backend.set_selected(
            CAPTURE_BACKENDS.index(capture_backend(self)) + 1
            if 'capture_backend' in settings else 0)

        def backend_changed(row, _):
            selected = row.get_selected()
            if selected == 0:
                self.configure('capture_backend', None, remove=True)
            else:
                self.configure('capture_backend', CAPTURE_BACKENDS[selected - 1])

        backend.connect('notify::selected', backend_changed)
        rows.append(backend)
        if mode == 'new_page':
            rows.append(Adw.ActionRow(title='Tap to open or create · Hold to recreate',
                                      subtitle="Tap opens this button's saved page, or scans to create it. Back keeps it. Hold deletes it and scans a new page."))
        else:
            rows.append(Adw.ActionRow(title='Tap to scan · Hold to clear',
                                      subtitle='Scan and clear change assignments only for this page and group. Slot and color settings stay.'))
        attempt = getattr(self, '__dict__', {}).get('_scan_attempt')
        if mode == 'new_page':
            message = attempt.message if isinstance(attempt, ScanAttempt) else ''
        else:
            message = self.coordinator.session(self).snapshot().message
        rows.append(Adw.ActionRow(title='Last scan', subtitle=message or 'No scan yet'))
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
        try:
            layout = _resolved_layout(self)
        except ValueError:
            return None
        if layout is not None:
            for _, _, action_object, slot in layout:
                if action_object is self:
                    self._resolved_slot = slot
                    return slot
        return None

    def color_filter(self):
        return _settings_color_filter(self.get_settings())

    def get_config_rows(self):
        rows = super().get_config_rows()
        configured = self.configured_slot()
        slot = Adw.SpinRow.new_with_range(-1, 99, 1)
        slot.set_title('Automatic slot')
        slot.set_subtitle('-1 chooses by button position; 1–99 pins the slot number')
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
        color = Adw.ComboRow(title='Color filter', subtitle='Only assign this icon color to this slot')
        color.set_model(Gtk.StringList.new(['Any', 'Red', 'Blue', 'Green', 'Yellow']))
        color.set_selected(SLOT_COLORS.index(self.color_filter()))
        color.connect('notify::selected', lambda row, _: self.configure(
            'color_filter', SLOT_COLORS[row.get_selected()]))
        rows.append(color)
        rows.append(Adw.ActionRow(title='Tap to execute or scan · Hold to rescan',
                                  subtitle='Tap an assigned slot to execute. Tap an empty slot or hold any Auto button to scan this group.'))
        resolved = self.slot()
        snapshot = self.coordinator.session(self).snapshot()
        key = snapshot.assignments.get(resolved)
        name = key
        get_text = getattr(getattr(self.plugin_base, 'lm', None), 'get', None)
        if key is not None and callable(get_text):
            name = get_text(f'actions.{key}.name', key) or key
        if resolved is None:
            status = 'Automatic position unavailable; choose an explicit positive slot'
        elif key is not None and resolved in snapshot.unconfirmed_slots:
            status = (f'{name} is unconfirmed; tap still executes it; '
                      'the latest scan did not see it')
        elif key is not None:
            status = f'{name} is assigned'
        elif resolved in snapshot.unknown_slots:
            status = 'Unknown result; rescan before use'
        else:
            status = 'Empty; no stratagem is assigned'
        rows.append(Adw.ActionRow(title='Assignment status', subtitle=status))
        rows.append(Adw.ActionRow(
            title='Last scan', subtitle=snapshot.message or 'No scan yet'))
        return rows

    def controllable(self):
        return (not self.has_custom_user_asset() and self.has_image_control()
                and all(self.has_label_controls()))

    def render(self):
        slot = self.slot()
        slot_label = str(slot) if slot is not None else '?'
        if not self.coordinator.enabled:
            shown = 'disabled', slot, self.color_filter()
            if self.displayed == shown:
                return
            self.artwork(f'automatic_stratagems/assets/icons/auto-{self.color_filter()}.png',
                         'AUTO', slot_label, 'Disabled', center_size=28)
            self.displayed = shown
            return
        session = self.coordinator.session(self)
        snapshot = session.snapshot()
        key = snapshot.assignments.get(slot)
        shown = snapshot.revision, slot, key
        if self.displayed == shown:
            return
        self.displayed = None
        if self.coordinator.loading(self):
            self.artwork('automatic_stratagems/assets/icons/scanning.mp4',
                         f'AUTO {slot_label}', '', 'Scanning')
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
                     'Stale' if key else 'Empty')
            filename = f'automatic_stratagems/assets/icons/auto-{self.color_filter()}.png'
            report = session.latest_report()
            completed_partial = (snapshot.status == 'partial' and report is not None
                                 and (report.get('status') == 'partial'
                                      or snapshot.unknown
                                      or snapshot.unconfirmed_slots))
            if completed_partial:
                path = str(Path(self.plugin_base.PATH) / filename)
                self.set_media(image=badged_icon(path).copy())
                self.set_top_label('AUTO')
                self.set_center_label(slot_label, font_size=28)
                self.set_bottom_label(label)
                self.set_background_color([26, 26, 26, 255])
            else:
                self.artwork(filename, 'AUTO', slot_label, label, center_size=28)
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
                or self.displayed != (snapshot.revision, slot, key)):
            return
        context = self.coordinator.context(self)
        if pressed != (context, snapshot.revision, slot, color_filter, key):
            return
        if snapshot.status == 'scanning':
            self.show_error(duration=3)
            return
        if snapshot.status not in ('idle', 'ready', 'partial', 'failed'):
            return
        if key is None:
            self.coordinator.start(self, replace=True)
            return
        if snapshot.status == 'idle':
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

        success = execute_stratagem(
            self.plugin_base, key, self.plugin_base.stratagems[key], guard=still_current)
        if not success and still_current():
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
        return [Adw.ActionRow(title='Return to source page',
                             subtitle=getattr(self, 'back_error', None) or
                             'Return to the source page. This temporary page and its scan state are kept.')]
