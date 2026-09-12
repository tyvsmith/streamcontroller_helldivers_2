"""StreamController actions backed by persistent scan assignments."""
from pathlib import Path
from weakref import WeakSet

from gi.repository import Adw, GLib, Gtk
from loguru import logger as log
from src.backend.PluginManager.InputBases import KeyAction

from .capture_source import CAPTURE_BACKENDS, normalize_capture_backend
from .provision.runtime_install import feature_enabled
from . import generated_page_flow
from .generated_page_flow import (
    capture_backend, image_page_settings as _image_page_settings,
)
from . import page_attempts
from .page_attempts import ScanAttempt
from . import scan_lifecycle
from .scan_lifecycle import SHUTDOWN_TIMEOUT_SECONDS, scan_mode
from .scan_session import SLOT_COLORS, ScanSession
from .scan_operation import ScanOperation
from . import session_registry
from .session_registry import scan_group as _scan_group
from . import slot_reconciliation
from .slot_reconciliation import (
    AUTOMATIC_ACTION_ID, configured_slot as _configured_slot,
    resolved_layout as _resolved_layout,
    settings_color_filter as _settings_color_filter,
)
from .scan_artwork import badged_icon
from .loading_animation import LoadingAnimation
from ..stratagem_execution import execute_stratagem


SOURCE_SETTING_KEYS = (
    'capture_backend',
)


def capture_source_index(settings, default_backend='auto'):
    return CAPTURE_BACKENDS.index(
        normalize_capture_backend(settings, default_backend))


class ScanCoordinator:
    """Coordinate page sessions and input exclusion; queue worker results for the UI."""

    def __init__(self, plugin, state_dir=None):
        self.plugin = plugin
        self.registry = session_registry.SessionRegistry(plugin, state_dir)
        self.reconciler = slot_reconciliation.SlotReconciler(self, AutomaticStratagem)
        self.actions = WeakSet()
        self.page_flow = generated_page_flow.GeneratedPageFlow(self)
        self.lifecycle = scan_lifecycle.ScanLifecycle(self, AutomaticStratagem)
        self.closed = False
        self.compatibility_error = None

    @property
    def sessions(self):
        return self.registry.sessions

    @property
    def store(self):
        return self.registry.store

    @property
    def state_errors(self):
        return self.registry.state_errors

    @property
    def temporary_pages(self):
        return self.page_flow.temporary_pages

    @temporary_pages.setter
    def temporary_pages(self, pages):
        self.page_flow.temporary_pages = pages

    @property
    def active_scans(self):
        return self.lifecycle.active_scans

    @property
    def enabled(self):
        return (not self.closed
                and self.compatibility_error is None
                and feature_enabled(self.plugin.get_settings()))

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
        try:
            context = self.context(action)
        except Exception:
            ScanOperation.clear_presentation(action)
            return False
        return ScanOperation.action_presentation_is_active(
            action, context, clear_stale=True)

    @staticmethod
    def begin_page_attempt(action):
        return page_attempts.begin_page_attempt(action)

    @staticmethod
    def bind_page_attempt(action, attempt_id, session, token):
        return page_attempts.bind_page_attempt(action, attempt_id, session, token)

    @staticmethod
    def finish_page_attempt(action, attempt_id, status, message,
                            *, session=None, token=None):
        return page_attempts.finish_page_attempt(
            action, attempt_id, status, message, session=session, token=token)

    def cancel_page_attempt(self, context, session):
        page_attempts.cancel_page_attempt(self._attached_actions(), context, session)

    def settings_changed(self):
        if not self.enabled:
            self.cancel_all()
        for action in self._attached_actions():
            action._pressed = None
            if action.get_is_present():
                action.show()

    def screenshot_settings_changed(self):
        """Cancel scans and retire caches bound to the old global source."""
        self.cancel_all()
        for action in self._attached_actions():
            action.displayed = None
        pages = self.temporary_pages
        if pages is not None:
            removed = pages.invalidate_screenshot_sources()
            for context in list(self.sessions):
                if context[1] in removed:
                    self.sessions.pop(context, None)
                    self.state_errors.pop(context, None)
        for action in self._attached_actions():
            if action.get_is_present():
                action.show()

    def enable_temporary_pages(self, page_manager):
        return self.page_flow.enable_temporary_pages(page_manager)

    def identity(self, action):
        return self.registry.identity(action)

    def context(self, action):
        return self.registry.context(action)

    def session(self, action):
        return self.registry.session(action)

    def session_for(self, context):
        return self.registry.session_for(context)

    def context_identity(self, context):
        return self.registry.context_identity(context)

    def slot_filters(self, context, session):
        return self.reconciler.slot_filters(context, session)

    def persist(self, action, session):
        self.registry.persist(action, session)

    def persist_context(self, context, session):
        self.registry.persist_context(context, session)

    def clear(self, action):
        if not self.enabled or not action.get_is_present():
            return
        context = self.context(action)
        self.cancel_context(context)
        session = self.session_for(context)
        session.clear()
        self.persist_context(context, session)
        self.redraw(context)

    def page_result(self, action, report, colors, *, replace_path=None):
        return self.page_flow.page_result(
            action, report, colors, replace_path=replace_path)

    def write_state(self, identity, session):
        self.registry.write_state(identity, session)

    def _cached_image_source_matches(self, action, path):
        return self.page_flow._cached_image_source_matches(action, path)

    def is_temporary_action(self, action):
        return self.page_flow.is_temporary_action(action)

    def _operation_image_settings(self, action):
        return self.page_flow._operation_image_settings(action)

    def open_temporary(self, action, session, *, replace_path=None):
        return self.page_flow.open_temporary(action, session, replace_path=replace_path)

    def back(self, action):
        return self.page_flow.back(action)

    def cached_page(self, action):
        return self.page_flow.cached_page(action)

    def _restore_cached_session(self, source, source_action):
        return self.page_flow._restore_cached_session(source, source_action)

    def open_cached_page(self, action):
        return self.page_flow.open_cached_page(action)

    def delete_cached_page(self, action, *, preserve_operation=None):
        return self.page_flow.delete_cached_page(
            action, preserve_operation=preserve_operation)

    def cancel_context(self, context):
        return self.lifecycle.cancel_context(context)

    def cancel_all(self):
        return self.lifecycle.cancel_all()

    def deck_disconnected(self, deck):
        return self.lifecycle.deck_disconnected(deck)

    def disconnect(self, action):
        return self.lifecycle.disconnect(action)

    def remove_action(self, action):
        return self.lifecycle.remove_action(action)

    def shutdown(self, timeout=SHUTDOWN_TIMEOUT_SECONDS):
        return self.lifecycle.shutdown(timeout)

    def register_action(self, action):
        self.actions.add(action)
        if isinstance(action, AutomaticStratagem):
            self.reconcile_action(action)

    def configured_filters(self, context):
        return self.reconciler.configured_filters(context)

    def _configured_filters(self, context):
        return self.reconciler._configured_filters(context)

    def reconcile_action(self, action, *, old_context=None, old_slot=None):
        self.reconciler.reconcile_action(
            action, old_context=old_context, old_slot=old_slot)

    def redraw(self, context):
        self.reconciler.redraw(context)

    def page_changed(self, controller, old_path, new_path):
        return self.lifecycle.page_changed(controller, old_path, new_path)

    def _complete_scan_operation(self, operation):
        return self.lifecycle._complete_scan_operation(operation)

    def _prepare_scan_operation(self, action, operation, *, replace, regenerate):
        return self.lifecycle._prepare_scan_operation(
            action, operation, replace=replace, regenerate=regenerate)

    def _apply_scan_result(self, action, operation, report, error, colors=None):
        return self.lifecycle._apply_scan_result(action, operation, report, error, colors)

    def _continue_after_preflight(self, action, operation):
        return self.lifecycle._continue_after_preflight(action, operation)

    def _run_scan_worker(self, action, operation):
        return self.lifecycle._run_scan_worker(action, operation)

    def start(self, action, *, replace=False, regenerate=False):
        return self.lifecycle.start(action, replace=replace, regenerate=regenerate)


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
            animation = getattr(self, '_loading_animation', None)
            if ((animation is None or animation.source is None)
                    and self.loading_animation_active()):
                self.displayed = None
            self.render()

    def on_disconnect(self):
        self.stop_loading_animation()
        self.coordinator.deck_disconnected(self.deck_controller)
        self.coordinator.actions.discard(self)
        self.displayed = None

    def on_remove(self):
        self.stop_loading_animation()
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
        group.connect('entry-activated', commit_group)
        focus = Gtk.EventControllerFocus()
        focus.connect('leave', commit_group)
        group.add_controller(focus)
        return [group, Adw.ActionRow(
            title='Group scope',
            subtitle='Shared only with the same group on this deck and page')]

    def configure(self, key, value, *, remove=False):
        if (key in SOURCE_SETTING_KEYS
                and self.coordinator.is_temporary_action(self) is True):
            self.coordinator.show_action_error(self)
            return
        old_context = self.coordinator.context(self)
        old_slot = self.slot() if isinstance(self, AutomaticStratagem) else None
        settings = self.get_settings()
        old_source_value = settings.get(key)
        if remove and key not in settings:
            return
        session = self.coordinator.session(self)
        if session.snapshot().status == 'scanning':
            self.coordinator.cancel_context(old_context)
        if remove:
            settings.pop(key)
        else:
            settings[key] = value
        new_source_value = settings.get(key)
        source_changed = (key in SOURCE_SETTING_KEYS
                          and old_source_value != new_source_value)
        self.set_settings(settings)
        if source_changed and scan_mode(self) == 'new_page':
            try:
                self.coordinator.delete_cached_page(self)
            except Exception:
                log.exception('Unable to discard stale image scan page')
                self.coordinator.show_action_error(self)
        if isinstance(self, AutomaticStratagem) and key in ('group', 'slot', 'color_filter'):
            self.coordinator.reconcile_action(self, old_context=old_context, old_slot=old_slot)
        self.displayed = None
        self.render()

    def stop_loading_animation(self):
        animation = getattr(self, '_loading_animation', None)
        if animation is not None:
            animation.stop()

    def loading_animation_active(self):
        return (self.coordinator.enabled and not self.coordinator.closed
                and self.get_is_present() and self.has_image_control()
                and not self.has_custom_user_asset()
                and self.get_input().state == self.state
                and self.coordinator.loading(self))

    def artwork(self, filename, top, center, bottom, *, center_size=None):
        if Path(filename).name == 'scanning.png':
            if not self.loading_animation_active():
                return
            if getattr(self, '_loading_animation', None) is None:
                self._loading_animation = LoadingAnimation(
                    GLib, lambda image: self.set_media(image=image),
                    self.loading_animation_active)
            self._loading_animation.start()
        else:
            self.stop_loading_animation()
            self.set_media(media_path=str(Path(self.plugin_base.PATH) / filename),
                           fps=30, loop=True)
            self.set_background_color([26, 26, 26, 255])
        self.set_top_label(top)
        self.set_center_label(center, font_size=center_size)
        self.set_bottom_label(bottom)


class AutomaticStratagemScanner(ScanActionBase):
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
            filename = 'scanning.png'
        self.artwork('automatic_stratagems/assets/icons/' + filename,
                     top, '', 'Stratagems')
        self.displayed = shown

    def get_config_rows(self):
        rows = super().get_config_rows()
        mode = scan_mode(self)
        settings = self.get_settings()
        generated = self.coordinator.is_temporary_action(self) is True
        source_descriptions = (
            'Try Gamescope first, then use the configured screenshot source.',
            'Capture the game through its associated Gamescope session.',
            'Use the plugin-wide screenshot settings.',
        )
        source_index = capture_source_index(settings)
        source = Adw.ComboRow(
            title='Capture source', subtitle=source_descriptions[source_index])
        source.set_model(Gtk.StringList.new(['Automatic', 'Gamescope', 'Screenshot']))
        source.set_selected(source_index)
        source.set_sensitive(not generated)

        def source_changed(row, _):
            if generated:
                return
            choice = row.get_selected()
            row.set_subtitle(source_descriptions[choice])
            self.configure('capture_backend', CAPTURE_BACKENDS[choice])

        source.connect('notify::selected', source_changed)
        rows.append(source)
        if generated:
            rows.append(Adw.ActionRow(
                title='Capture source is inherited',
                subtitle='Change the source on the button that created this page'))
        if mode == 'new_page':
            rows.append(Adw.ActionRow(title='Tap to open or create · Hold to recreate',
                                      subtitle=(
                                          "Tap opens this button's saved page, or scans to create it. "
                                          "Back keeps it. Hold scans a replacement; image failures keep the saved page."
                                          if source_index != 1 else
                                          "Tap opens this button's saved page, or scans to create it. "
                                          "Back keeps it. Hold deletes it and scans a new page.")))
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


class AutomaticStratagemPage(AutomaticStratagemScanner):
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
        if not self.coordinator.enabled or not self.coordinator.loading(self):
            self.stop_loading_animation()
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
        loading = self.coordinator.loading(self)
        if self.displayed == shown and getattr(self, '_displayed_loading', None) == loading:
            return
        self.displayed = None
        self._displayed_loading = loading
        if loading:
            self.artwork('automatic_stratagems/assets/icons/scanning.png',
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
            if slot in snapshot.unknown_slots:
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
