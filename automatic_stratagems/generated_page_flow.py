"""Open, reuse, leave, and delete generated scan pages for their source actions."""
from loguru import logger as log

from .capture_source import (
    normalize_capture_backend, page_source_settings, source_identity, source_settings,
)
from .scan_operation import ScanOperation
from .scan_session import ScanSession
from .streamcontroller_adapter import source_action_address
from .temporary_scan_page import TemporaryScanPages


def capture_backend(action):
    return normalize_capture_backend(action.get_settings())


def action_source_settings(action, settings=None):
    return source_settings(
        action.get_settings() if settings is None else settings,
        action.plugin_base.get_settings())


def image_page_settings(action):
    return page_source_settings(action_source_settings(action))


class GeneratedPageFlow:
    """Navigate and reuse generated pages on top of ``TemporaryScanPages``.

    Owns the page store. Sessions, scan state, cancellation, and redraw belong
    to the coordinator and are looked up on it at each use.
    """

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.temporary_pages = None

    def enable_temporary_pages(self, page_manager):
        self.temporary_pages = TemporaryScanPages(
            self.coordinator.store.directory.parent / 'temporary-pages', page_manager,
            self.coordinator.store, owner=self.coordinator.plugin)

    def page_result(self, action, report, colors, *, replace_path=None):
        rows, columns = self.temporary_pages.layout(action.deck_controller)
        result = ScanSession()
        token = result.begin(range(1, rows * columns - 1))
        result.finish(token, report, self.coordinator.plugin.stratagems, colors)
        if result.snapshot().recognized:
            self.open_temporary(action, result, replace_path=replace_path)
        return result.snapshot()

    def _cached_image_source_matches(self, action, path):
        settings = self.temporary_pages.scan_settings(
            path, bind_image_source=True)
        return source_identity(settings) == source_identity(
            action_source_settings(action))

    def is_temporary_action(self, action):
        if self.temporary_pages is None:
            return False
        try:
            return self.temporary_pages.owns_location(action.page.json_path)
        except (AttributeError, OSError, ValueError):
            return False

    def _operation_image_settings(self, action):
        settings = action.get_settings()
        if (self.temporary_pages is not None
                and self.temporary_pages.owns_location(action.page.json_path)):
            stored = self.temporary_pages.scan_settings(
                action.page.json_path, bind_image_source=True)
            current = action_source_settings(action, stored)
            if source_identity(stored) != source_identity(current):
                raise ValueError('Global screenshot settings changed')
            return current
        return action_source_settings(action, settings)

    def open_temporary(self, action, session, *, replace_path=None):
        deck, source, group = self.coordinator.context(action)
        source_action = source_action_address(action)
        path = self.temporary_pages.find(deck, source, group, source_action)
        if (path is not None and replace_path is None
                and self._cached_image_source_matches(action, path)):
            self.coordinator.session_for((deck, path, group))
            self.temporary_pages.show(deck, path)
            return
        if path is not None and replace_path is None:
            replace_path = path
        if replace_path is not None and path != replace_path:
            raise RuntimeError('Cached page changed during replacement')
        path = self.temporary_pages.create(
            deck, source, group, capture_backend(action), source_action,
            image_settings=image_page_settings(action))
        context = deck, path, group
        try:
            self.coordinator.sessions[context] = session
            self.coordinator.registry.write_state(
                dict(deck=deck.serial_number(), page=path, group=group), session)
            self.temporary_pages.show(deck, path)
            if replace_path is not None:
                old_context = deck, replace_path, group
                try:
                    self.temporary_pages.discard(replace_path)
                except Exception:
                    self.temporary_pages.show(deck, replace_path)
                    raise
                self.coordinator.sessions.pop(old_context, None)
                self.coordinator.state_errors.pop(old_context, None)
        except Exception:
            if getattr(deck.active_page, 'json_path', None) != path:
                self.coordinator.sessions.pop(context, None)
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
        deck, source, group = self.coordinator.context(action)
        path = self.temporary_pages.find(
            deck, source, group, source_action_address(action))
        if path is not None and not self._cached_image_source_matches(action, path):
            return None
        if path is not None:
            self.coordinator.session_for((deck, path, group))
        return path

    def _restore_cached_session(self, source, source_action):
        if self.temporary_pages is None:
            return None
        deck, page, group = source
        path = self.temporary_pages.find(deck, page, group, source_action)
        if path is not None:
            self.coordinator.session_for((deck, path, group))
        return path

    def open_cached_page(self, action):
        if self.coordinator.closed or not self.coordinator.enabled or not action.get_is_present():
            return False
        path = self.cached_page(action)
        if path is None:
            return False
        self.temporary_pages.show(action.deck_controller, path)
        return True

    def delete_cached_page(self, action, *, preserve_operation=None):
        if self.coordinator.closed or not self.coordinator.enabled or not action.get_is_present():
            return False
        source = self.coordinator.context(action)
        if (ScanOperation.action_presentation_is_active(action, source)
                and (preserve_operation is None
                     or self.coordinator.active_scans.get(source) is not preserve_operation)):
            self.coordinator.cancel_context(source)
        source_action = source_action_address(action)
        path = self._restore_cached_session(source, source_action)
        if path is None:
            return False
        context = source[0], path, source[2]
        self.coordinator.cancel_context(context)
        if getattr(action.deck_controller.active_page, 'json_path', None) == path:
            self.temporary_pages.back(action.deck_controller, path)
        self.temporary_pages.discard(path)
        self.coordinator.sessions.pop(context, None)
        self.coordinator.state_errors.pop(context, None)
        self.coordinator.redraw(source)
        return True
