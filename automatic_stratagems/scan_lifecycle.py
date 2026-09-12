"""Run scan workers, complete scans on GTK, and cancel or shut down scan work."""
from concurrent.futures import CancelledError
from functools import partial
from threading import current_thread
from time import monotonic

from gi.repository import GLib
from loguru import logger as log

from .capture_source import source_identity
from . import runtime_preparation
from .scan_artwork import catalog_colors
from .scan_runner import (FLATPAK_TERMINATE_GRACE_SECONDS,
                          TERMINATE_GRACE_SECONDS, ScanSetupError,
                          check_scan_setup, run_scan,
                          validate_image_source_report)
from .streamcontroller_adapter import source_action_address


SHUTDOWN_TIMEOUT_SECONDS = (FLATPAK_TERMINATE_GRACE_SECONDS
                            + TERMINATE_GRACE_SECONDS + 1)
MAIN_CONTEXT_TIMEOUT_SECONDS = 5


class ScanLifecycle:
    """Own active scan operations from worker execution to completion and shutdown.

    Everything else -- sessions, persistence, page attempts, generated pages,
    redraw, feature and shutdown state, and this lifecycle's own entry points --
    is looked up on the coordinator at each use, so callbacks queued for GTK and
    finalizers stay coordinator methods.
    """

    def __init__(self, coordinator, automatic_type):
        self.coordinator = coordinator
        self.automatic_type = automatic_type
        self.active_scans = {}

    def cancel_context(self, context):
        operation = self.active_scans.get(context)
        if operation is not None:
            operation.cancel.set()
        session = self.coordinator.sessions.get(context)
        if session is not None and session.snapshot().status == 'scanning':
            transient = session.is_transient()
            if transient:
                self.coordinator.cancel_page_attempt(context, session)
            session.cancel()
            if not transient:
                self.coordinator.persist_context(context, session)

    def cancel_all(self):
        contexts = set(self.active_scans)
        contexts.update(context for context, session in list(self.coordinator.sessions.items())
                        if session.snapshot().status == 'scanning')
        for context in contexts:
            self.coordinator.cancel_context(context)

    def deck_disconnected(self, deck):
        for context in [context for context in self.coordinator.sessions if context[0] is deck]:
            self.coordinator.cancel_context(context)
            self.coordinator.sessions.pop(context, None)

    def disconnect(self, action):
        self.coordinator.deck_disconnected(action.deck_controller)

    def remove_action(self, action):
        context = getattr(action, '_scan_context', None)
        if context is None:
            context = self.coordinator.context(action)
        slot = (getattr(action, '_resolved_slot', None)
                if isinstance(action, self.automatic_type) else None)
        if isinstance(action, self.automatic_type) and slot is None:
            slot = action.slot()
        self.coordinator.actions.discard(action)
        operation = self.active_scans.get(context)
        if (operation is not None and operation.initiator() is action) or slot is not None:
            self.coordinator.cancel_context(context)
        if slot is not None and context in self.coordinator.sessions:
            session = self.coordinator.sessions[context]
            filters, authoritative = self.coordinator._configured_filters(context)
            if session.reconcile(filters, affected=None if authoritative else {slot}):
                self.coordinator.persist_context(context, session)
                self.coordinator.redraw(context)

    def shutdown(self, timeout=SHUTDOWN_TIMEOUT_SECONDS):
        self.coordinator.closed = True
        operations = list(self.active_scans.values())
        self.coordinator.cancel_all()
        deadline = monotonic() + timeout
        for operation in operations:
            operation.setup_done.wait(max(0, deadline - monotonic()))
            thread = operation.worker
            if (operation.started and thread is not None
                    and thread is not current_thread()):
                thread.join(max(0, deadline - monotonic()))
        return all(operation.setup_done.is_set()
                   and not (operation.started and operation.worker is not None
                            and operation.worker.is_alive())
                   for operation in operations)

    def page_changed(self, controller, old_path, new_path):
        for (deck, path, group), session in list(self.coordinator.sessions.items()):
            if deck is controller and path == old_path and session.snapshot().status == 'scanning':
                self.coordinator.cancel_context((deck, path, group))
                self.coordinator.sessions.pop((deck, path, group), None)

    def _complete_scan_operation(self, operation):
        # A bound worker may finish before start() returns. The setup owner
        # still publishes started/setup_done before shutdown may return.
        if operation.worker is None:
            operation.setup_done.set()
        plan = operation.plan
        if (plan is not None
                and self.active_scans.get(plan.context) is operation):
            self.active_scans.pop(plan.context, None)
        self.coordinator.plugin.input_lock.release()

    def _apply_scan_result(self, action, operation, report, error, colors=None):
        plan = operation.plan
        try:
            if self.coordinator.closed or not plan.session.is_active(plan.token):
                return False
            if (not self.coordinator.enabled or not action.get_is_present()
                    or self.coordinator.context(action) != plan.context):
                if plan.new_page:
                    self.coordinator.finish_page_attempt(
                        action, plan.attempt_id, 'cancelled', 'Scan cancelled',
                        session=plan.session, token=plan.token)
                plan.session.cancel()
            elif source_identity(
                    self.coordinator._operation_image_settings(action)
                    ) != plan.source_snapshot:
                if plan.new_page:
                    self.coordinator.finish_page_attempt(
                        action, plan.attempt_id, 'cancelled', 'Scan cancelled',
                        session=plan.session, token=plan.token)
                plan.session.cancel()
            elif error:
                if plan.session.fail(plan.token, error):
                    if not plan.new_page:
                        self.coordinator.persist(action, plan.session)
                    else:
                        self.coordinator.finish_page_attempt(
                            action, plan.attempt_id, 'failed', error,
                            session=plan.session, token=plan.token)
                self.coordinator.show_action_error(action)
                log.warning('Stratagem scan failed: {}', error)
            else:
                if plan.image_source is not None:
                    validate_image_source_report(
                        report, plan.mutable_image_source(),
                        backend=plan.backend)
                if plan.new_page:
                    accepted = plan.session.finish_transient(plan.token)
                else:
                    accepted = plan.session.finish(
                        plan.token, report, self.coordinator.plugin.stratagems, colors)
                if accepted:
                    result = (self.coordinator.page_result(
                                  action, report, colors,
                                  replace_path=(plan.cached_path
                                                if plan.regenerate
                                                and plan.image_source is not None
                                                else None))
                              if plan.new_page else plan.session.snapshot())
                    if plan.new_page:
                        self.coordinator.finish_page_attempt(
                            action, plan.attempt_id,
                            result.status, result.message,
                            session=plan.session, token=plan.token)
                    if not plan.new_page:
                        self.coordinator.persist(action, plan.session)
                    if result.status == 'failed':
                        self.coordinator.show_action_error(action)
                    log.info('Stratagem scan: {}', result.message)
            self.coordinator.redraw(plan.context)
        except Exception as error:
            if plan.new_page:
                if plan.session.is_active(plan.token):
                    plan.session.fail(plan.token, str(error))
                self.coordinator.finish_page_attempt(
                    action, plan.attempt_id, 'failed', error,
                    session=plan.session, token=plan.token)
            else:
                failure = (plan.token if plan.session.is_active(plan.token)
                           else plan.session.begin(self.coordinator.slot_filters(
                               plan.context, plan.session)))
                plan.session.fail(failure, str(error))
                self.coordinator.persist(action, plan.session)
            self.coordinator.show_action_error(action)
            log.exception('Unable to finish stratagem scan')
            self.coordinator.redraw(plan.context)
        return False

    def _continue_after_preflight(self, action, operation):
        plan = operation.plan
        try:
            try:
                current_context = self.coordinator.context(action)
                current_source_action = source_action_address(action)
                current = (
                    current_context, current_source_action,
                    plan.session.snapshot().revision,
                    self.coordinator._restore_cached_session(
                        current_context, current_source_action),
                    source_identity(self.coordinator._operation_image_settings(action)))
            except Exception:
                current = None
            expected = (plan.context, plan.source_action,
                        plan.scan_revision, plan.cached_path,
                        plan.source_snapshot)
            valid = (
                self.active_scans.get(plan.context) is operation
                and not operation.cancel.is_set()
                and not self.coordinator.closed and self.coordinator.enabled
                and action.get_is_present()
                and plan.session.is_active(plan.token)
                and current == expected
            )
            if not valid:
                operation.cancel.set()
                if plan.session.is_active(plan.token):
                    self.coordinator.finish_page_attempt(
                        action, plan.attempt_id, 'cancelled', 'Scan cancelled',
                        session=plan.session, token=plan.token)
                    plan.session.cancel()
                self.coordinator.redraw(plan.context)
                return False
            if (plan.cached_path is not None and plan.image_source is None
                    and not self.coordinator.delete_cached_page(
                        action, preserve_operation=operation)):
                raise RuntimeError('Cached page changed during scan setup')
            operation.continue_scan = True
        except Exception as error:
            self.coordinator._apply_scan_result(action, operation, None, str(error))
        finally:
            operation.continuation.set()
        return False

    def _run_scan_worker(self, action, operation):
        plan = operation.plan
        try:
            try:
                if plan.regenerate:
                    setup_kwargs = dict(
                        backend=plan.backend, cancel_event=operation.cancel)
                    if plan.image_source is not None:
                        setup_kwargs['image_source'] = plan.mutable_image_source()
                    check_scan_setup(self.coordinator.plugin.PATH, **setup_kwargs)
                    GLib.idle_add(partial(
                        self.coordinator._continue_after_preflight, action, operation))
                    continuation_deadline = (
                        monotonic() + MAIN_CONTEXT_TIMEOUT_SECONDS)
                    while not operation.continuation.wait(.02):
                        if operation.cancel.is_set():
                            return
                        if monotonic() >= continuation_deadline:
                            message = (
                                'Unable to continue scan setup on the main thread')
                            operation.cancel.set()
                            if plan.session.is_active(plan.token):
                                plan.session.fail(plan.token, message)
                                self.coordinator.finish_page_attempt(
                                    action, plan.attempt_id, 'failed', message,
                                    session=plan.session, token=plan.token)
                            log.warning(message)
                            return
                    if operation.cancel.is_set() or not operation.continue_scan:
                        return
                colors = catalog_colors(
                    self.coordinator.plugin.PATH, self.coordinator.plugin.stratagems)
                scan_kwargs = dict(
                    backend=plan.backend, workers=plan.workers,
                    cancel_event=operation.cancel)
                if plan.image_source is not None:
                    scan_kwargs['image_source'] = plan.mutable_image_source()
                report = run_scan(self.coordinator.plugin.PATH, **scan_kwargs)
            except CancelledError:
                return
            except ScanSetupError as error:
                GLib.idle_add(
                    partial(self.coordinator._apply_scan_result, action, operation),
                    None, runtime_preparation.setup_failure_message(
                        self.coordinator.plugin, error))
            except Exception as error:
                GLib.idle_add(
                    partial(self.coordinator._apply_scan_result, action, operation),
                    None, str(error))
            else:
                GLib.idle_add(
                    partial(self.coordinator._apply_scan_result, action, operation),
                    report, None, colors)
        except Exception:
            active = plan.session.is_active(plan.token)
            transient = plan.session.is_transient(plan.token)
            if active:
                if transient:
                    self.coordinator.finish_page_attempt(
                        action, plan.attempt_id, 'failed',
                        'Unable to queue scan result',
                        session=plan.session, token=plan.token)
                plan.session.cancel()
            try:
                if active and not transient:
                    self.coordinator.persist_context(plan.context, plan.session)
            except Exception:
                log.exception('Unable to persist cancelled stratagem scan')
            log.exception('Unable to queue stratagem scan result')
        finally:
            operation.complete()
