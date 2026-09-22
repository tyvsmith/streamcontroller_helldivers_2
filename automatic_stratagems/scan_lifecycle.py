"""Run scan workers, complete scans on GTK, and cancel or shut down scan work."""
from concurrent.futures import CancelledError
from functools import partial
from threading import Thread, current_thread
from time import monotonic

from gi.repository import GLib
from loguru import logger as log

from .capture_source import normalize_capture_backend, operation_source, source_identity
from . import page_attempts
from . import runtime_preparation
from .scan_artwork import catalog_colors
from .scan_operation import ScanOperation, ScanPlan
from .scan_runner import (FLATPAK_TERMINATE_GRACE_SECONDS,
                          TERMINATE_GRACE_SECONDS, ScanSetupError,
                          check_scan_setup, run_scan, scan_workers,
                          validate_image_source_report)
from .streamcontroller_adapter import source_action_address


SHUTDOWN_TIMEOUT_SECONDS = (FLATPAK_TERMINATE_GRACE_SECONDS
                            + TERMINATE_GRACE_SECONDS + 1)
MAIN_CONTEXT_TIMEOUT_SECONDS = 5


def scan_mode(action):
    return ('new_page' if (getattr(action, 'new_page_action', False) is True
                           or action.get_settings().get('scan_mode') == 'new_page')
            else 'update')


class ScanLifecycle:
    """Own active scans from start and preparation to completion and shutdown.

    Holds the active operation per context and runs each one through input
    locking, plan preparation, the worker, GTK completion, cancellation, deck
    and action removal, and the bounded shutdown join.
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
                page_attempts.cancel_page_attempt(
                    self.coordinator._attached_actions(), context, session)
            session.cancel()
            if not transient:
                self.coordinator.persist_context(context, session)

    def cancel_all(self):
        contexts = set(self.active_scans)
        contexts.update(context for context, session in list(self.coordinator.sessions.items())
                        if session.snapshot().status == 'scanning')
        for context in contexts:
            self.cancel_context(context)

    def deck_disconnected(self, deck):
        for context in [context for context in self.coordinator.sessions if context[0] is deck]:
            self.cancel_context(context)
            self.coordinator.sessions.pop(context, None)

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
            self.cancel_context(context)
        if slot is not None and context in self.coordinator.sessions:
            session = self.coordinator.sessions[context]
            filters, authoritative = self.coordinator.reconciler._configured_filters(context)
            if session.reconcile(filters, affected=None if authoritative else {slot}):
                self.coordinator.persist_context(context, session)
                self.coordinator.redraw(context)

    def shutdown(self, timeout=SHUTDOWN_TIMEOUT_SECONDS):
        self.coordinator.closed = True
        operations = list(self.active_scans.values())
        self.cancel_all()
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
                self.cancel_context((deck, path, group))
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

    def _prepare_scan_operation(self, action, operation, *, replace, regenerate):
        # A refused or failed preparation completes the operation even when
        # its cleanup raises; completion is idempotent.
        prepared = False
        try:
            prepared = self._prepare_scan_plan(
                action, operation, replace=replace, regenerate=regenerate)
            return prepared
        finally:
            if not prepared:
                operation.complete()

    def _prepare_scan_plan(self, action, operation, *, replace, regenerate):
        context = session = token = None
        source_action = cached_path = None
        attempt_id = None
        new_page = False
        try:
            context = self.coordinator.context(action)
            session = self.coordinator.session(action)
            new_page = scan_mode(action) == 'new_page'
            if new_page and session.snapshot().status == 'scanning':
                owns_scan = ScanOperation.action_presentation_is_active(
                    action, context, session=session)
                if not regenerate or not owns_scan:
                    operation.complete()
                    self.coordinator.show_action_error(action)
                    return False
            if new_page:
                attempt_id = page_attempts.begin_page_attempt(action)
            grouped = [a for a in self.coordinator._attached_actions()
                       if isinstance(a, self.automatic_type)
                       and self.coordinator.context(a) == context]
            automatic = [a for a in grouped if a.get_is_present()]
            slots = [a.slot() for a in automatic]
            filters = {slot: a.color_filter()
                       for a, slot in zip(automatic, slots)
                       if slot is not None}
            unusable = not slots or len(slots) != len(set(slots))
            if new_page:
                keep = ()
            elif unusable:
                # A scan that cannot run clears nothing.
                keep = set(session.snapshot().assignments)
            else:
                # The scan fills present slots; the group's other slots, such as
                # those on another key state, keep their assignments.
                keep = set(self.coordinator.reconciler._configured_filters(context)[0])
                keep.update(slot for a in grouped if a not in automatic
                            and (slot := a.slot()) is not None)
            if new_page:
                if self.coordinator.temporary_pages is None:
                    raise ValueError('Temporary pages are unavailable')
                self.coordinator.temporary_pages.layout(action.deck_controller)
                source_action = source_action_address(action)
                filters = self.coordinator.reconciler.slot_filters(context, session)
                if regenerate:
                    if session.snapshot().status == 'scanning':
                        self.cancel_context(context)
                    cached_path = self.coordinator.page_flow._restore_cached_session(
                        context, source_action)
            elif regenerate:
                raise ValueError('Only page openers can regenerate a cache')
            operation_image_settings = self.coordinator.page_flow._operation_image_settings(action)
            frozen_source_identity = source_identity(operation_image_settings)
            image_source = operation_source(
                operation_image_settings, allow_rescan=True)
            if not new_page and any(slot is None for slot in slots):
                log.warning(
                    'Automatic slot position unavailable; choose an explicit slot')
                self.coordinator.show_action_error(action)
                operation.complete()
                return False
            token = session.begin(filters, replace=replace, transient=new_page, keep=keep)
            if token is None:
                if new_page:
                    page_attempts.finish_page_attempt(
                        action, attempt_id, 'cancelled',
                        'Scan already in progress')
                operation.complete()
                self.coordinator.show_action_error(action)
                return False
            if new_page:
                page_attempts.bind_page_attempt(action, attempt_id, session, token)
            operation.bind_plan(ScanPlan(
                context=context, session=session, token=token,
                backend=normalize_capture_backend(operation_image_settings),
                workers=scan_workers(
                    self.coordinator.plugin.get_settings().get('scan_workers', 2)),
                source_snapshot=frozen_source_identity,
                image_source=image_source, source_action=source_action,
                cached_path=cached_path,
                scan_revision=session.snapshot().revision,
                new_page=new_page, regenerate=regenerate,
                attempt_id=attempt_id))
            plan = operation.plan
            operation.bind_presentation()
            self.active_scans[plan.context] = operation
            if not new_page:
                self.coordinator.registry.persist(action, session)
            self.coordinator.redraw(context)
            if not new_page and unusable:
                session.fail(token, 'Add uniquely numbered Automatic slots' if slots
                             else 'No Automatic slots to fill; add Automatic Stratagem buttons')
                self.coordinator.registry.persist(action, session)
                self.coordinator.show_action_error(action)
                operation.complete()
                self.coordinator.redraw(context)
                return False
            return True
        except CancelledError:
            operation.cancel.set()
            operation.setup_done.set()
            if session is not None and token is not None and session.is_active(token):
                transient = session.is_transient(token)
                if transient:
                    page_attempts.finish_page_attempt(
                        action, attempt_id, 'cancelled', 'Scan cancelled',
                        session=session, token=token)
                session.cancel()
                if not transient:
                    self.coordinator.persist_context(context, session)
            operation.complete()
        except Exception as error:
            operation.cancel.set()
            operation.setup_done.set()
            if session is not None:
                if new_page:
                    if token is not None and session.is_active(token):
                        session.fail(token, str(error))
                    if attempt_id is not None:
                        page_attempts.finish_page_attempt(
                            action, attempt_id, 'failed', error,
                            session=session if token is not None else None,
                            token=token)
                else:
                    if token is None and context is not None:
                        try:
                            token = session.begin(
                                self.coordinator.reconciler.slot_filters(context, session))
                        except Exception:
                            log.exception(
                                'Unable to initialize failed stratagem scan state')
                    if token is not None and session.is_active(token):
                        session.fail(token, str(error))
                    try:
                        self.coordinator.persist_context(context, session)
                    except Exception:
                        log.exception(
                            'Unable to persist failed stratagem scan setup')
            log.exception('Unable to start stratagem scan')
            operation.complete()
            if context is not None:
                self.coordinator.redraw(context)
            self.coordinator.show_action_error(action)
        return False

    def _apply_scan_result(self, action, operation, report, error, colors=None):
        plan = operation.plan
        try:
            if self.coordinator.closed or not plan.session.is_active(plan.token):
                return False
            if (not self.coordinator.enabled or not action.get_is_present()
                    or self.coordinator.context(action) != plan.context):
                if plan.new_page:
                    page_attempts.finish_page_attempt(
                        action, plan.attempt_id, 'cancelled', 'Scan cancelled',
                        session=plan.session, token=plan.token)
                plan.session.cancel()
            elif source_identity(
                    self.coordinator.page_flow._operation_image_settings(action)
                    ) != plan.source_snapshot:
                if plan.new_page:
                    page_attempts.finish_page_attempt(
                        action, plan.attempt_id, 'cancelled', 'Scan cancelled',
                        session=plan.session, token=plan.token)
                plan.session.cancel()
            elif error:
                if plan.session.fail(plan.token, error):
                    if not plan.new_page:
                        self.coordinator.registry.persist(action, plan.session)
                    else:
                        page_attempts.finish_page_attempt(
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
                    result = (self.coordinator.page_flow.page_result(
                                  action, report, colors,
                                  replace_path=(plan.cached_path
                                                if plan.regenerate
                                                and plan.image_source is not None
                                                else None))
                              if plan.new_page else plan.session.snapshot())
                    if plan.new_page:
                        page_attempts.finish_page_attempt(
                            action, plan.attempt_id,
                            result.status, result.message,
                            session=plan.session, token=plan.token)
                    if not plan.new_page:
                        self.coordinator.registry.persist(action, plan.session)
                    if result.status == 'failed':
                        self.coordinator.show_action_error(action)
                    log.info('Stratagem scan: {}', result.message)
            self.coordinator.redraw(plan.context)
        except Exception as error:
            if plan.new_page:
                if plan.session.is_active(plan.token):
                    plan.session.fail(plan.token, str(error))
                page_attempts.finish_page_attempt(
                    action, plan.attempt_id, 'failed', error,
                    session=plan.session, token=plan.token)
            else:
                failure = (plan.token if plan.session.is_active(plan.token)
                           else plan.session.begin(self.coordinator.reconciler.slot_filters(
                               plan.context, plan.session)))
                plan.session.fail(failure, str(error))
                self.coordinator.registry.persist(action, plan.session)
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
                    self.coordinator.page_flow._restore_cached_session(
                        current_context, current_source_action),
                    source_identity(self.coordinator.page_flow._operation_image_settings(action)))
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
                    page_attempts.finish_page_attempt(
                        action, plan.attempt_id, 'cancelled', 'Scan cancelled',
                        session=plan.session, token=plan.token)
                    plan.session.cancel()
                self.coordinator.redraw(plan.context)
                return False
            if (plan.cached_path is not None and plan.image_source is None
                    and not self.coordinator.page_flow.delete_cached_page(
                        action, preserve_operation=operation)):
                raise RuntimeError('Cached page changed during scan setup')
            operation.continue_scan = True
        except Exception as error:
            self._apply_scan_result(action, operation, None, str(error))
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
                        self._continue_after_preflight, action, operation))
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
                                page_attempts.finish_page_attempt(
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
                    partial(self._apply_scan_result, action, operation),
                    None, runtime_preparation.prepare_after_setup_failure(
                        self.coordinator.plugin, error))
            except Exception as error:
                GLib.idle_add(
                    partial(self._apply_scan_result, action, operation),
                    None, str(error))
            else:
                GLib.idle_add(
                    partial(self._apply_scan_result, action, operation),
                    report, None, colors)
        except Exception:
            active = plan.session.is_active(plan.token)
            transient = plan.session.is_transient(plan.token)
            if active:
                if transient:
                    page_attempts.finish_page_attempt(
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

    def start(self, action, *, replace=False, regenerate=False):
        if (self.coordinator.closed or not self.coordinator.enabled
                or not action.get_is_present()):
            return
        if not self.coordinator.plugin.input_lock.acquire(blocking=False):
            self.coordinator.show_action_error(action)
            return
        operation = ScanOperation(
            action, continue_scan=not regenerate,
            on_complete=self._complete_scan_operation)
        if not self._prepare_scan_operation(
                action, operation, replace=replace, regenerate=regenerate):
            return
        try:
            self._launch_scan_worker(action, operation)
        finally:
            # A started worker owns completion; completion is idempotent.
            if not operation.started:
                operation.complete()

    def _launch_scan_worker(self, action, operation):
        plan = operation.plan
        try:
            operation.bind_worker(Thread(
                target=partial(self._run_scan_worker, action, operation),
                name='hd2-scan', daemon=True))
            if (operation.cancel.is_set() or self.coordinator.closed
                    or not self.coordinator.enabled
                    or not action.get_is_present()
                    or self.coordinator.context(action) != plan.context
                    or not plan.session.is_active(plan.token)):
                raise CancelledError()
            operation.worker.start()
            operation.mark_started()
        except CancelledError:
            operation.cancel.set()
            operation.setup_done.set()
            if plan.session.is_active(plan.token):
                transient = plan.session.is_transient(plan.token)
                if transient:
                    page_attempts.finish_page_attempt(
                        action, plan.attempt_id, 'cancelled', 'Scan cancelled',
                        session=plan.session, token=plan.token)
                plan.session.cancel()
                if not transient:
                    self.coordinator.persist_context(plan.context, plan.session)
            operation.complete()
        except Exception as error:
            operation.cancel.set()
            operation.setup_done.set()
            if plan.new_page:
                if plan.session.is_active(plan.token):
                    plan.session.fail(plan.token, str(error))
                if plan.attempt_id is not None:
                    page_attempts.finish_page_attempt(
                        action, plan.attempt_id, 'failed', error,
                        session=plan.session, token=plan.token)
            else:
                if plan.session.is_active(plan.token):
                    plan.session.fail(plan.token, str(error))
                try:
                    self.coordinator.persist_context(plan.context, plan.session)
                except Exception:
                    log.exception('Unable to persist failed stratagem scan setup')
            log.exception('Unable to start stratagem scan')
            operation.complete()
            self.coordinator.redraw(plan.context)
            self.coordinator.show_action_error(action)
