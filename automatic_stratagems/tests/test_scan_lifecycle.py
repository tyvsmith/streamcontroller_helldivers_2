import contextlib
import importlib
import inspect
import sys
import threading
import time
import types
import unittest
from concurrent.futures import CancelledError
from unittest.mock import Mock, patch

from .package_loader import plugin_module

REPORT = {'status': 'matched', 'rows': [{'id': 'A'}]}
CACHED = '/pages/cached.json'


class Deck:
    pass


class Action:
    def __init__(self, context):
        self.context = context
        self.deck_controller = None if context is None else context[0]
        self.present = True
        self.settings = {}

    def get_is_present(self):
        return self.present

    def get_settings(self):
        return self.settings


class Automatic(Action):
    def __init__(self, context, slot):
        super().__init__(context)
        self._slot = slot

    def slot(self):
        return self._slot

    def color_filter(self):
        return 'any'


class InputLock:
    def __init__(self, events):
        self.events = events
        self.busy = False

    def acquire(self, blocking=True):
        self.events.append('busy' if self.busy else 'acquire')
        return not self.busy

    def release(self):
        self.events.append('release')


class Worker:
    def __init__(self, alive):
        self.alive = alive
        self.joins = []

    def is_alive(self):
        return self.alive

    def join(self, timeout):
        self.joins.append(timeout)


class Host:
    """The coordinator surface the scan lifecycle reaches through."""

    def __init__(self, mod, settings, events):
        self.plugin = types.SimpleNamespace(
            PATH='/plugin', stratagems={'A': ['UP'], 'B': ['DOWN']},
            input_lock=InputLock(events), get_settings=lambda: {})
        self.sessions = {}
        self.actions = set()
        self.closed = False
        self.enabled = True
        for name in ('persist_context', 'redraw', 'show_action_error'):
            setattr(self, name, Mock())
        self.registry = types.SimpleNamespace(persist=Mock())
        self.reconciler = types.SimpleNamespace(
            slot_filters=Mock(return_value={1: 'any'}),
            _configured_filters=Mock(return_value=({}, False)))
        self.page_flow = types.SimpleNamespace(
            page_result=Mock(), delete_cached_page=Mock(return_value=True),
            _operation_image_settings=Mock(return_value=settings),
            _restore_cached_session=Mock(return_value=None))
        self.temporary_pages = Mock()
        self.automatic = []
        self.lifecycle = mod.ScanLifecycle(self, Automatic)

    def context(self, action):
        return action.context

    def session(self, action):
        return self.sessions[action.context]

    def _attached_actions(self):
        return list(self.automatic)


class ScanLifecycleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repository = types.ModuleType('gi.repository')
        repository.GLib = Mock()
        with patch.dict(sys.modules, {
                'gi.repository': repository,
                'loguru': types.SimpleNamespace(logger=Mock())}):
            cls.mod = importlib.import_module(
                plugin_module('automatic_stratagems.scan_lifecycle'))
            cls.ops = importlib.import_module(
                plugin_module('automatic_stratagems.scan_operation'))
            cls.scan_session = importlib.import_module(
                plugin_module('automatic_stratagems.scan_session'))
            cls.capture = importlib.import_module(
                plugin_module('automatic_stratagems.capture_source'))

    def setUp(self):
        self.events = []
        self.queued = []
        self.settings = self.capture.source_settings({'capture_backend': 'gamescope'}, {})
        self.host = Host(self.mod, self.settings, self.events)
        self.lifecycle = self.host.lifecycle
        self.context = (Deck(), '/pages/source.json', 'HD2')
        self.action = Action(self.context)
        for patcher in (
                patch.object(self.mod, 'source_action_address', return_value='address'),
                patch.object(self.mod, 'log'),
                patch.object(self.mod.GLib, 'idle_add', side_effect=self.queue)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.attempts = types.SimpleNamespace()
        for name, returned in (('begin_page_attempt', {'return_value': 7}),
                               ('bind_page_attempt', {'return_value': True}),
                               ('finish_page_attempt', {}), ('cancel_page_attempt', {})):
            patcher = patch.object(self.mod.page_attempts, name, **returned)
            setattr(self.attempts, name, patcher.start())
            self.addCleanup(patcher.stop)

    def queue(self, callback, *args):
        self.events.append('queue')
        self.queued.append((callback, args))
        return 1

    def assigned_session(self, context=None):
        session = self.scan_session.ScanSession()
        token = session.begin({1: 'any'})
        session.finish(token, REPORT, self.host.plugin.stratagems)
        self.host.sessions[context or self.context] = session
        return session

    def scan(self, *, new_page=False, regenerate=False, cached_path=None,
             revision_offset=0):
        session = self.host.sessions.get(self.context) or self.assigned_session()
        token = session.begin({1: 'any'}, replace=True, transient=new_page)
        operation = self.ops.ScanOperation(
            self.action, continue_scan=not regenerate,
            on_complete=self.lifecycle._complete_scan_operation)
        operation.bind_plan(self.ops.ScanPlan(
            context=self.context, session=session, token=token, backend='gamescope',
            workers=2, source_snapshot=self.mod.source_identity(self.settings),
            image_source=None, source_action='address', cached_path=cached_path,
            scan_revision=session.snapshot().revision + revision_offset,
            new_page=new_page, regenerate=regenerate,
            attempt_id=7 if new_page else None))
        self.lifecycle.active_scans[self.context] = operation
        return session, token, operation

    def reset_host(self):
        self.host.sessions.clear()
        self.lifecycle.active_scans.clear()
        self.host.closed = False
        self.host.enabled = True
        self.action.present = True
        self.action.context = self.context
        for mock in (self.host.registry.persist, self.host.persist_context,
                     self.host.redraw, self.host.show_action_error,
                     self.attempts.finish_page_attempt, self.host.page_flow.page_result,
                     self.host.page_flow._operation_image_settings):
            mock.reset_mock()


class WorkerTests(ScanLifecycleTestCase):
    def test_the_result_is_queued_before_the_operation_finalizes(self):
        session, token, operation = self.scan()
        with patch.object(self.mod, 'run_scan', return_value=REPORT) as run, \
             patch.object(self.mod, 'catalog_colors', return_value={'A': 'red'}):
            self.lifecycle._run_scan_worker(self.action, operation)

        run.assert_called_once_with(
            '/plugin', backend='gamescope', workers=2, cancel_event=operation.cancel)
        self.assertEqual(self.events, ['queue', 'release'])
        self.assertNotIn(self.context, self.lifecycle.active_scans)
        callback, args = self.queued.pop()
        self.assertEqual(callback.func, self.lifecycle._apply_scan_result)
        self.assertEqual(args, (REPORT, None, {'A': 'red'}))
        self.assertTrue(session.is_active(token))

        callback(*args)

        self.assertEqual(session.snapshot().assignments[1], 'A')
        self.assertFalse(session.is_active(token))
        self.host.registry.persist.assert_called_once_with(self.action, session)
        self.assertEqual(self.events, ['queue', 'release'])

    def test_setup_and_scan_failures_are_queued_as_messages(self):
        error = self.mod.ScanSetupError('runtime is absent')
        for failure, expected in ((error, 'prepared'), (RuntimeError('crashed'), 'crashed')):
            with self.subTest(failure=failure):
                self.reset_host()
                self.queued.clear()
                _, _, operation = self.scan()
                with patch.object(self.mod, 'catalog_colors', return_value={}), \
                     patch.object(self.mod, 'run_scan', side_effect=failure), \
                     patch.object(self.mod.runtime_preparation, 'prepare_after_setup_failure',
                                  return_value='prepared') as message:
                    self.lifecycle._run_scan_worker(self.action, operation)
                self.assertEqual(self.queued[0][1], (None, expected))
                if failure is error:
                    message.assert_called_once_with(self.host.plugin, error)
                else:
                    message.assert_not_called()

    def test_a_cancelled_worker_queues_nothing_and_still_finalizes_once(self):
        _, _, operation = self.scan()
        with patch.object(self.mod, 'catalog_colors', return_value={}), \
             patch.object(self.mod, 'run_scan', side_effect=CancelledError()):
            self.lifecycle._run_scan_worker(self.action, operation)
        self.assertEqual(self.events, ['release'])
        self.assertFalse(operation.complete())
        self.assertEqual(self.events, ['release'])

    def test_a_queue_failure_cancels_without_clearing_assignments(self):
        session, token, operation = self.scan()
        with patch.object(self.mod, 'catalog_colors', return_value={}), \
             patch.object(self.mod, 'run_scan', return_value=REPORT), \
             patch.object(self.mod.GLib, 'idle_add', side_effect=RuntimeError('queue closed')):
            self.lifecycle._run_scan_worker(self.action, operation)
        self.assertFalse(session.is_active(token))
        self.assertEqual(session.snapshot().assignments[1], 'A')
        self.host.persist_context.assert_called_once_with(self.context, session)
        self.attempts.finish_page_attempt.assert_not_called()
        self.mod.log.exception.assert_called_with('Unable to queue stratagem scan result')
        self.assertEqual(self.events, ['release'])

    def test_a_generated_page_queue_failure_finishes_its_attempt(self):
        session, token, operation = self.scan(new_page=True)
        with patch.object(self.mod, 'catalog_colors', return_value={}), \
             patch.object(self.mod, 'run_scan', return_value=REPORT), \
             patch.object(self.mod.GLib, 'idle_add', side_effect=RuntimeError('queue closed')):
            self.lifecycle._run_scan_worker(self.action, operation)
        self.attempts.finish_page_attempt.assert_called_once_with(
            self.action, 7, 'failed', 'Unable to queue scan result',
            session=session, token=token)
        self.host.persist_context.assert_not_called()
        self.assertEqual(self.events, ['release'])

    def test_a_preflight_that_never_continues_fails_at_the_main_context_deadline(self):
        session, token, operation = self.scan(new_page=True, regenerate=True)
        with patch.object(self.mod, 'check_scan_setup') as check, \
             patch.object(self.mod, 'run_scan') as run, \
             patch.object(self.mod, 'MAIN_CONTEXT_TIMEOUT_SECONDS', .05):
            self.lifecycle._run_scan_worker(self.action, operation)
        message = 'Unable to continue scan setup on the main thread'
        check.assert_called_once_with('/plugin', backend='gamescope',
                                      cancel_event=operation.cancel)
        run.assert_not_called()
        self.assertEqual(self.queued[0][0].func, self.lifecycle._continue_after_preflight)
        self.assertTrue(operation.cancel.is_set())
        self.assertFalse(session.is_active(token))
        self.attempts.finish_page_attempt.assert_called_once_with(
            self.action, 7, 'failed', message, session=session, token=token)
        self.mod.log.warning.assert_called_once_with(message)
        self.assertEqual(self.events, ['queue', 'release'])

    def test_a_preflight_cancelled_while_waiting_stops_without_scanning(self):
        _, _, operation = self.scan(new_page=True, regenerate=True)

        def queue_then_cancel(callback, *args):
            self.queue(callback, *args)
            operation.cancel.set()
            return 1

        with patch.object(self.mod, 'check_scan_setup'), \
             patch.object(self.mod, 'run_scan') as run, \
             patch.object(self.mod.GLib, 'idle_add', side_effect=queue_then_cancel):
            self.lifecycle._run_scan_worker(self.action, operation)
        run.assert_not_called()
        self.assertEqual(self.events, ['queue', 'release'])


class ResultTests(ScanLifecycleTestCase):
    def test_stale_or_closed_results_are_inert(self):
        for case in ('stale', 'closed'):
            with self.subTest(case=case):
                self.reset_host()
                session, token, operation = self.scan()
                if case == 'stale':
                    session.cancel()
                    session.begin({1: 'any'})
                else:
                    self.host.closed = True
                before = session.snapshot()
                self.assertFalse(self.lifecycle._apply_scan_result(
                    self.action, operation, {'status': 'matched', 'rows': [{'id': 'B'}]},
                    None))
                self.assertEqual(session.snapshot(), before)
                self.host.redraw.assert_not_called()
                self.host.registry.persist.assert_not_called()
                self.host.page_flow._operation_image_settings.assert_not_called()

    def test_feature_presence_and_context_are_rechecked_before_capture_identity(self):
        for case in ('disabled', 'absent', 'moved'):
            with self.subTest(case=case):
                self.reset_host()
                session, token, operation = self.scan(new_page=True)
                if case == 'disabled':
                    self.host.enabled = False
                elif case == 'absent':
                    self.action.present = False
                else:
                    self.action.context = (Deck(), '/pages/other.json', 'HD2')
                self.lifecycle._apply_scan_result(self.action, operation, REPORT, None)
                self.assertFalse(session.is_active(token))
                self.assertEqual(session.snapshot().assignments[1], 'A')
                self.attempts.finish_page_attempt.assert_called_once_with(
                    self.action, 7, 'cancelled', 'Scan cancelled',
                    session=session, token=token)
                self.host.page_flow._operation_image_settings.assert_not_called()
                self.host.page_flow.page_result.assert_not_called()
                self.host.redraw.assert_called_once_with(self.context)

    def test_a_changed_capture_identity_cancels_the_result(self):
        session, token, operation = self.scan()
        self.host.page_flow._operation_image_settings.return_value = self.capture.source_settings(
            {'capture_backend': 'screenshot'}, {})
        self.lifecycle._apply_scan_result(
            self.action, operation, {'status': 'matched', 'rows': [{'id': 'B'}]}, None)
        self.assertFalse(session.is_active(token))
        self.assertEqual(session.snapshot().assignments[1], 'A')
        self.host.registry.persist.assert_not_called()

    def test_a_scan_error_fails_the_session_without_releasing_input(self):
        session, token, operation = self.scan()
        self.lifecycle._apply_scan_result(self.action, operation, None, 'capture failed')
        self.assertEqual(session.snapshot().status, 'failed')
        self.assertEqual(session.snapshot().message, 'capture failed')
        self.host.registry.persist.assert_called_once_with(self.action, session)
        self.host.show_action_error.assert_called_once_with(self.action)
        self.mod.log.warning.assert_called_once_with('Stratagem scan failed: {}', 'capture failed')
        self.host.redraw.assert_called_once_with(self.context)
        self.assertEqual(self.events, [])

    def test_a_generated_page_overflow_fails_its_attempt_on_the_opener(self):
        session, token, operation = self.scan(new_page=True)
        message = '2 stratagems did not fit; add more Automatic slots'
        self.host.page_flow.page_result.return_value = types.SimpleNamespace(
            status='failed', message=message)
        self.lifecycle._apply_scan_result(self.action, operation, REPORT, None, {})
        self.attempts.finish_page_attempt.assert_called_once_with(
            self.action, 7, 'failed', message, session=session, token=token)
        self.host.show_action_error.assert_called_once_with(self.action)
        self.assertEqual(session.snapshot().assignments[1], 'A')

    def test_a_generated_page_result_opens_its_page_through_the_coordinator(self):
        session, token, operation = self.scan(new_page=True)
        self.host.page_flow.page_result.return_value = types.SimpleNamespace(
            status='ready', message='1 recognized')
        self.lifecycle._apply_scan_result(self.action, operation, REPORT, None, {'A': 'red'})
        self.host.page_flow.page_result.assert_called_once_with(
            self.action, REPORT, {'A': 'red'}, replace_path=None)
        self.attempts.finish_page_attempt.assert_called_once_with(
            self.action, 7, 'ready', '1 recognized', session=session, token=token)
        self.host.registry.persist.assert_not_called()
        self.assertFalse(session.is_active(token))


class ContinuationTests(ScanLifecycleTestCase):
    def test_a_changed_revision_rejects_the_preflight_continuation(self):
        session, token, operation = self.scan(new_page=True, regenerate=True,
                                              revision_offset=-1)
        self.assertFalse(self.lifecycle._continue_after_preflight(self.action, operation))
        self.assertTrue(operation.cancel.is_set())
        self.assertFalse(session.is_active(token))
        self.attempts.finish_page_attempt.assert_called_once_with(
            self.action, 7, 'cancelled', 'Scan cancelled', session=session, token=token)
        self.host.page_flow.delete_cached_page.assert_not_called()
        self.assertFalse(operation.continue_scan)
        self.assertTrue(operation.continuation.is_set())

    def test_a_valid_continuation_deletes_the_cache_through_the_coordinator(self):
        _, _, operation = self.scan(new_page=True, regenerate=True, cached_path=CACHED)
        self.host.page_flow._restore_cached_session.return_value = CACHED
        self.assertFalse(self.lifecycle._continue_after_preflight(self.action, operation))
        self.host.page_flow.delete_cached_page.assert_called_once_with(
            self.action, preserve_operation=operation)
        self.assertTrue(operation.continue_scan)
        self.assertFalse(operation.cancel.is_set())
        self.assertTrue(operation.continuation.is_set())

    def test_a_cache_that_changed_during_setup_fails_the_scan(self):
        session, token, operation = self.scan(new_page=True, regenerate=True,
                                              cached_path=CACHED)
        self.host.page_flow._restore_cached_session.return_value = CACHED
        self.host.page_flow.delete_cached_page.return_value = False
        self.lifecycle._continue_after_preflight(self.action, operation)
        message = 'Cached page changed during scan setup'
        self.attempts.finish_page_attempt.assert_called_once_with(
            self.action, 7, 'failed', message, session=session, token=token)
        self.assertFalse(operation.continue_scan)
        self.assertTrue(operation.continuation.is_set())


class CancellationTests(ScanLifecycleTestCase):
    def test_cancelling_a_context_stops_its_scan_without_clearing_assignments(self):
        for new_page in (False, True):
            with self.subTest(new_page=new_page):
                self.reset_host()
                self.attempts.cancel_page_attempt.reset_mock()
                session, token, operation = self.scan(new_page=new_page)
                self.lifecycle.cancel_context(self.context)
                self.assertTrue(operation.cancel.is_set())
                self.assertFalse(session.is_active(token))
                self.assertEqual(session.snapshot().assignments[1], 'A')
                if new_page:
                    self.attempts.cancel_page_attempt.assert_called_once_with([], self.context, session)
                    self.host.persist_context.assert_not_called()
                else:
                    self.attempts.cancel_page_attempt.assert_not_called()
                    self.host.persist_context.assert_called_once_with(self.context, session)

    def test_cancel_all_reaches_active_operations_and_scanning_sessions(self):
        _, token, operation = self.scan()
        other = (Deck(), '/pages/other.json', 'HD2')
        idle = self.assigned_session(other)
        scanning = self.scan_session.ScanSession()
        other_token = scanning.begin({1: 'any'})
        third = (Deck(), '/pages/third.json', 'HD2')
        self.host.sessions[third] = scanning
        self.lifecycle.cancel_all()
        self.assertTrue(operation.cancel.is_set())
        self.assertFalse(self.host.sessions[self.context].is_active(token))
        self.assertFalse(scanning.is_active(other_token))
        self.assertEqual(idle.snapshot().status, 'ready')

    def test_a_disconnected_deck_forgets_its_sessions_after_cancelling(self):
        session, token, operation = self.scan()
        sibling = (self.context[0], '/pages/sibling.json', 'HD2')
        self.assigned_session(sibling)
        other = (Deck(), '/pages/source.json', 'HD2')
        kept = self.assigned_session(other)
        self.lifecycle.deck_disconnected(self.action.deck_controller)
        self.assertTrue(operation.cancel.is_set())
        self.assertFalse(session.is_active(token))
        self.assertEqual(self.host.sessions, {other: kept})

    def test_a_page_change_forgets_only_scanning_sessions_on_the_old_page(self):
        session, token, operation = self.scan()
        other_group = (self.context[0], self.context[1], 'other')
        idle = self.assigned_session(other_group)
        self.lifecycle.page_changed(self.context[0], '/pages/unrelated.json', CACHED)
        self.assertTrue(session.is_active(token))
        self.lifecycle.page_changed(self.context[0], self.context[1], CACHED)
        self.assertTrue(operation.cancel.is_set())
        self.assertEqual(self.host.sessions, {other_group: idle})

    def test_removing_the_initiator_cancels_its_scan(self):
        session, token, operation = self.scan()
        bystander = Action(self.context)
        self.host.actions.update((self.action, bystander))
        self.lifecycle.remove_action(bystander)
        self.assertTrue(session.is_active(token))
        self.lifecycle.remove_action(self.action)
        self.assertEqual(self.host.actions, set())
        self.assertTrue(operation.cancel.is_set())
        self.assertFalse(session.is_active(token))

    def test_removing_an_automatic_slot_reconciles_its_session(self):
        session = self.assigned_session()
        slot = Automatic(None, 1)
        slot._scan_context = self.context
        self.host.actions.add(slot)
        self.lifecycle.remove_action(slot)
        self.host.reconciler._configured_filters.assert_called_once_with(self.context)
        self.assertEqual(dict(session.snapshot().assignments), {})
        self.host.persist_context.assert_called_with(self.context, session)
        self.host.redraw.assert_called_once_with(self.context)


class CompletionAndShutdownTests(ScanLifecycleTestCase):
    def test_finalizing_releases_input_once_and_forgets_only_its_own_operation(self):
        _, _, operation = self.scan()
        stray = self.ops.ScanOperation(
            self.action, continue_scan=True, on_complete=self.lifecycle._complete_scan_operation)
        stray.bind_plan(operation.plan)
        self.assertTrue(stray.complete())
        self.assertTrue(stray.setup_done.is_set())
        self.assertIs(self.lifecycle.active_scans[self.context], operation)
        self.assertTrue(operation.complete())
        self.assertFalse(operation.complete())
        self.assertNotIn(self.context, self.lifecycle.active_scans)
        self.assertEqual(self.events, ['release', 'release'])

    def test_shutdown_closes_cancels_joins_and_leaves_queued_results_inert(self):
        session, token, operation = self.scan()
        worker = Worker(alive=False)
        operation.bind_worker(worker)
        operation.mark_started()
        self.assertTrue(self.lifecycle.shutdown(timeout=1))
        self.assertTrue(self.host.closed)
        self.assertTrue(operation.cancel.is_set())
        self.assertFalse(session.is_active(token))
        self.assertEqual(len(worker.joins), 1)
        self.assertTrue(0 <= worker.joins[0] <= 1)
        before = session.snapshot()
        self.host.redraw.reset_mock()
        self.assertFalse(self.lifecycle._apply_scan_result(self.action, operation, REPORT, None))
        self.assertEqual(session.snapshot(), before)
        self.host.redraw.assert_not_called()
        self.assertEqual(self.events, [])

    def test_shutdown_reports_unfinished_cleanup_within_its_deadline(self):
        _, _, alive = self.scan()
        alive.bind_worker(Worker(alive=True))
        alive.mark_started()
        self.assertFalse(self.lifecycle.shutdown(timeout=.05))
        self.reset_host()
        self.scan()
        started = time.monotonic()
        self.assertFalse(self.lifecycle.shutdown(timeout=.05))
        self.assertLess(time.monotonic() - started, 1)

    def test_shutdown_never_joins_its_own_thread(self):
        _, _, operation = self.scan()
        operation.bind_worker(threading.current_thread())
        operation.mark_started()
        self.assertFalse(self.lifecycle.shutdown(timeout=.05))

    def test_the_default_shutdown_deadline_covers_both_terminate_grace_periods(self):
        self.assertEqual(self.mod.SHUTDOWN_TIMEOUT_SECONDS,
                         self.mod.FLATPAK_TERMINATE_GRACE_SECONDS
                         + self.mod.TERMINATE_GRACE_SECONDS + 1)
        self.assertEqual(inspect.signature(self.mod.ScanLifecycle.shutdown)
                         .parameters['timeout'].default, self.mod.SHUTDOWN_TIMEOUT_SECONDS)
        self.assertEqual(self.mod.MAIN_CONTEXT_TIMEOUT_SECONDS, 5)


class EntryTestCase(ScanLifecycleTestCase):
    def update_setup(self):
        session = self.assigned_session()
        self.host.automatic = [Automatic(self.context, 1)]
        return session

    def page_setup(self):
        session = self.assigned_session()
        self.action.settings = {'scan_mode': 'new_page'}
        return session

    def page_token(self):
        return self.attempts.bind_page_attempt.call_args.args[3]

    def exception_messages(self):
        return [call.args[0] for call in self.mod.log.exception.call_args_list]

    def launch_cancelled(self):
        thread = Mock()

        def create(**_kwargs):
            self.host.enabled = False
            return thread

        with patch.object(self.mod, 'Thread', side_effect=create):
            self.lifecycle.start(self.action, replace=True)
        return thread

    def launch_failure(self):
        thread = Mock()
        thread.start.side_effect = RuntimeError('thread unavailable')
        with patch.object(self.mod, 'Thread', return_value=thread):
            self.lifecycle.start(self.action, replace=True)
        return thread


class EntryTests(EntryTestCase):
    """Scan start and preparation: worker launch, and input released once after a refused
    page token, regenerating an update button, cancellation while preparing or launching,
    or a setup or worker-start failure, including failures that cannot be saved."""

    def test_a_launch_binds_a_named_daemon_worker_before_starting_it(self):
        session = self.update_setup()
        thread = Mock()
        with patch.object(self.mod, 'Thread', return_value=thread) as create:
            self.lifecycle.start(self.action, replace=True)
        target = create.call_args.kwargs['target']
        self.assertEqual((create.call_args.kwargs['name'], create.call_args.kwargs['daemon']),
                         ('hd2-scan', True))
        self.assertEqual(target.func, self.lifecycle._run_scan_worker)
        self.assertIs(target.args[0], self.action)
        operation = target.args[1]
        self.assertIs(operation.worker, thread)
        thread.start.assert_called_once_with()
        self.assertTrue(operation.started)
        self.assertEqual(operation._on_complete, self.lifecycle._complete_scan_operation)
        self.assertIs(self.lifecycle.active_scans[self.context], operation)
        self.assertTrue(session.is_active(operation.plan.token))
        self.host.registry.persist.assert_called_once_with(self.action, session)
        self.assertEqual(self.events, ['acquire'])

    def test_regenerating_an_update_button_fails_its_setup(self):
        session = self.update_setup()
        with patch.object(self.mod, 'Thread') as create:
            self.lifecycle.start(self.action, replace=True, regenerate=True)
        create.assert_not_called()
        self.assertEqual((session.snapshot().status, session.snapshot().message),
                         ('failed', 'Only page openers can regenerate a cache'))
        self.assertEqual(session.snapshot().assignments[1], 'A')
        self.host.persist_context.assert_called_once_with(self.context, session)
        self.host.show_action_error.assert_called_once_with(self.action)
        self.assertEqual(self.exception_messages(), ['Unable to start stratagem scan'])
        self.assertEqual(self.lifecycle.active_scans, {})
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_a_page_opener_refused_a_session_token_cancels_its_attempt(self):
        session = self.page_setup()
        with patch.object(session, 'begin', return_value=None), \
             patch.object(self.mod, 'Thread') as create:
            self.lifecycle.start(self.action, replace=True)
        create.assert_not_called()
        self.attempts.finish_page_attempt.assert_called_once_with(
            self.action, 7, 'cancelled', 'Scan already in progress')
        self.attempts.bind_page_attempt.assert_not_called()
        self.host.show_action_error.assert_called_once_with(self.action)
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_cancellation_while_saving_a_prepared_scan_keeps_assignments(self):
        session = self.update_setup()
        self.host.registry.persist.side_effect = CancelledError()
        with patch.object(self.mod, 'Thread') as create:
            self.lifecycle.start(self.action, replace=True)
        create.assert_not_called()
        self.assertNotEqual(session.snapshot().status, 'scanning')
        self.assertEqual(session.snapshot().assignments[1], 'A')
        self.host.persist_context.assert_called_once_with(self.context, session)
        self.host.show_action_error.assert_not_called()
        self.host.redraw.assert_not_called()
        self.assertEqual(self.lifecycle.active_scans, {})
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_cancellation_while_drawing_a_prepared_page_scan_cancels_its_attempt(self):
        session = self.page_setup()
        self.host.redraw.side_effect = CancelledError()
        with patch.object(self.mod, 'Thread') as create:
            self.lifecycle.start(self.action, replace=True)
        create.assert_not_called()
        token = self.page_token()
        self.assertFalse(session.is_active(token))
        self.attempts.finish_page_attempt.assert_called_once_with(
            self.action, 7, 'cancelled', 'Scan cancelled', session=session, token=token)
        self.host.persist_context.assert_not_called()
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_a_setup_failure_without_a_failure_token_is_still_saved_and_shown(self):
        session = self.update_setup()
        self.host.page_flow._operation_image_settings.side_effect = ValueError(
            'Global screenshot settings changed')
        self.host.reconciler.slot_filters.side_effect = RuntimeError('filters unavailable')
        with patch.object(self.mod, 'Thread') as create:
            self.lifecycle.start(self.action, replace=True)
        create.assert_not_called()
        self.assertEqual(session.snapshot().status, 'ready')
        self.assertEqual(self.exception_messages(),
                         ['Unable to initialize failed stratagem scan state',
                          'Unable to start stratagem scan'])
        self.host.persist_context.assert_called_once_with(self.context, session)
        self.host.redraw.assert_called_once_with(self.context)
        self.host.show_action_error.assert_called_once_with(self.action)
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_a_setup_failure_that_cannot_be_saved_is_logged_and_shown(self):
        session = self.update_setup()
        self.host.page_flow._operation_image_settings.side_effect = ValueError(
            'Global screenshot settings changed')
        self.host.persist_context.side_effect = OSError('disk full')
        with patch.object(self.mod, 'Thread'):
            self.lifecycle.start(self.action, replace=True)
        self.assertEqual((session.snapshot().status, session.snapshot().message),
                         ('failed', 'Global screenshot settings changed'))
        self.assertEqual(self.exception_messages(),
                         ['Unable to persist failed stratagem scan setup',
                          'Unable to start stratagem scan'])
        self.host.show_action_error.assert_called_once_with(self.action)
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_a_launch_cancelled_before_start_cancels_an_update_scan(self):
        session = self.update_setup()
        thread = self.launch_cancelled()
        thread.start.assert_not_called()
        self.assertNotEqual(session.snapshot().status, 'scanning')
        self.assertEqual(session.snapshot().assignments[1], 'A')
        self.host.persist_context.assert_called_once_with(self.context, session)
        self.attempts.finish_page_attempt.assert_not_called()
        self.host.show_action_error.assert_not_called()
        self.assertEqual(self.lifecycle.active_scans, {})
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_a_launch_cancelled_before_start_cancels_a_page_attempt(self):
        session = self.page_setup()
        thread = self.launch_cancelled()
        thread.start.assert_not_called()
        token = self.page_token()
        self.assertFalse(session.is_active(token))
        self.attempts.finish_page_attempt.assert_called_once_with(
            self.action, 7, 'cancelled', 'Scan cancelled', session=session, token=token)
        self.host.persist_context.assert_not_called()
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_a_worker_that_cannot_start_fails_an_update_scan(self):
        session = self.update_setup()
        self.launch_failure()
        self.assertEqual((session.snapshot().status, session.snapshot().message),
                         ('failed', 'thread unavailable'))
        self.host.persist_context.assert_called_once_with(self.context, session)
        self.assertEqual(self.exception_messages(), ['Unable to start stratagem scan'])
        self.host.show_action_error.assert_called_once_with(self.action)
        self.assertEqual(self.host.redraw.call_args_list[-1].args, (self.context,))
        self.assertEqual(self.lifecycle.active_scans, {})
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_a_worker_that_cannot_start_fails_a_page_attempt(self):
        session = self.page_setup()
        self.launch_failure()
        token = self.page_token()
        self.assertFalse(session.is_active(token))
        attempt = self.attempts.finish_page_attempt.call_args
        self.assertEqual(attempt.args[:3], (self.action, 7, 'failed'))
        self.assertEqual(str(attempt.args[3]), 'thread unavailable')
        self.assertEqual(attempt.kwargs, {'session': session, 'token': token})
        self.host.persist_context.assert_not_called()
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_a_worker_start_failure_that_cannot_be_saved_is_logged(self):
        self.update_setup()
        self.host.persist_context.side_effect = OSError('disk full')
        self.launch_failure()
        self.assertEqual(self.exception_messages(),
                         ['Unable to persist failed stratagem scan setup',
                          'Unable to start stratagem scan'])
        self.assertEqual(self.events, ['acquire', 'release'])


class EntryExitTests(EntryTestCase):
    """Presses refused before a scan starts leave no lock, operation, or worker."""

    def start_refused(self, **kwargs):
        with patch.object(self.mod, 'Thread') as create:
            self.lifecycle.start(self.action, **kwargs)
        create.assert_not_called()
        self.assertEqual(self.lifecycle.active_scans, {})

    def test_a_closed_disabled_or_absent_coordinator_ignores_the_press(self):
        for name, change in (('closed', lambda: setattr(self.host, 'closed', True)),
                             ('disabled', lambda: setattr(self.host, 'enabled', False)),
                             ('absent', lambda: setattr(self.action, 'present', False))):
            with self.subTest(name):
                self.reset_host()
                self.events.clear()
                session = self.update_setup()
                revision = session.snapshot().revision
                change()
                self.start_refused(replace=True)
                self.assertEqual(self.events, [])
                self.host.show_action_error.assert_not_called()
                self.assertEqual(session.snapshot().revision, revision)

    def test_busy_input_marks_the_initiator_without_touching_its_session(self):
        session = self.update_setup()
        revision = session.snapshot().revision
        self.host.plugin.input_lock.busy = True
        self.start_refused(replace=True)
        self.assertEqual(self.events, ['busy'])
        self.host.show_action_error.assert_called_once_with(self.action)
        self.assertEqual(session.snapshot().revision, revision)

    def test_a_page_opener_already_scanning_is_refused_without_a_new_attempt(self):
        for regenerate in (False, True):
            with self.subTest(regenerate=regenerate):
                self.reset_host()
                self.events.clear()
                session = self.page_setup()
                token = session.begin({1: 'any'}, transient=True)
                self.start_refused(replace=True, regenerate=regenerate)
                self.assertEqual(self.events, ['acquire', 'release'])
                self.host.show_action_error.assert_called_once_with(self.action)
                self.attempts.begin_page_attempt.assert_not_called()
                self.assertTrue(session.is_active(token))

    def test_an_unresolved_slot_is_refused_before_the_session_begins(self):
        session = self.assigned_session()
        self.host.automatic = [Automatic(self.context, 1), Automatic(self.context, None)]
        revision = session.snapshot().revision
        self.start_refused(replace=True)
        self.mod.log.warning.assert_called_once_with(
            'Automatic slot position unavailable; choose an explicit slot')
        self.host.show_action_error.assert_called_once_with(self.action)
        self.assertEqual(session.snapshot().revision, revision)
        self.assertEqual(session.snapshot().assignments[1], 'A')
        self.host.registry.persist.assert_not_called()
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_duplicate_or_missing_slots_fail_the_scan_without_clearing(self):
        hidden = Automatic(self.context, 2)
        hidden.present = False
        for automatic, message in (
                ([Automatic(self.context, 1), Automatic(self.context, 1)],
                 'Add uniquely numbered Automatic slots'),
                ([], 'No Automatic slots to fill; add Automatic Stratagem buttons'),
                ([hidden], 'No Automatic slots to fill; add Automatic Stratagem buttons')):
            with self.subTest(slots=len(automatic)):
                self.reset_host()
                self.events.clear()
                session = self.assigned_session()
                token = session.begin({1: 'any', 2: 'red'})
                session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}, {'id': 'B'}]},
                               self.host.plugin.stratagems, {'B': 'red'})
                self.host.automatic = automatic
                self.start_refused(replace=True)
                self.assertEqual((session.snapshot().status, session.snapshot().message),
                                 ('failed', message))
                self.assertEqual(dict(session.snapshot().assignments), {1: 'A', 2: 'B'})
                self.assertEqual(session.checkpoint()['slots']['2']['filter'], 'red')
                self.host.registry.persist.assert_called_with(self.action, session)
                self.host.show_action_error.assert_called_once_with(self.action)
                self.assertEqual(self.host.redraw.call_args_list[-1].args, (self.context,))
                self.assertEqual(self.events, ['acquire', 'release'])


class ScanSlotSetTests(EntryTestCase):
    """An update scan fills present slots and leaves the group's other slots alone."""

    def test_slots_outside_the_scan_keep_assignments_and_removed_slots_drop(self):
        session = self.scan_session.ScanSession()
        token = session.begin([1, 2, 3, 4])
        session.finish(token, {'status': 'matched', 'rows': [
            {'id': 'A'}, {'id': 'B'}, {'id': 'C'}, {'id': 'D'}]},
            dict.fromkeys('ABCD'))
        self.host.sessions[self.context] = session
        hidden = Automatic(self.context, 2)
        hidden.present = False
        # Slot 3 exists only in the page topology, on a state never attached.
        self.host.reconciler._configured_filters.return_value = (
            {1: 'any', 2: 'any', 3: 'any'}, True)
        self.host.automatic = [Automatic(self.context, 1), hidden]
        with patch.object(self.mod, 'Thread'):
            self.lifecycle.start(self.action, replace=True)
        plan = self.lifecycle.active_scans[self.context].plan
        self.assertEqual(dict(session.snapshot().assignments), {1: 'A', 2: 'B', 3: 'C'})
        session.finish(plan.token, {'status': 'matched', 'rows': [{'id': 'E'}, {'id': 'F'}]},
                       dict.fromkeys('ABCDEF'))
        snapshot = session.snapshot()
        self.assertEqual(dict(snapshot.assignments), {1: 'E', 2: 'B', 3: 'C'})
        self.assertEqual((snapshot.status, snapshot.message),
                         ('failed', '1 stratagem did not fit; add more Automatic slots'))


class CleanupFailureTests(EntryTestCase):
    """A raising cleanup step still completes the operation and frees input."""

    def assert_released(self):
        self.assertEqual(self.lifecycle.active_scans, {})
        self.assertEqual(self.events, ['acquire', 'release'])

    def start_suppressing(self, error_type, thread=None):
        with patch.object(self.mod, 'Thread', return_value=thread or Mock()), \
             contextlib.suppress(error_type):
            self.lifecycle.start(self.action, replace=True)

    def test_a_refused_page_token_whose_attempt_cannot_finish_releases_input(self):
        session = self.page_setup()
        self.attempts.finish_page_attempt.side_effect = RuntimeError('attempt')
        with patch.object(session, 'begin', return_value=None):
            self.start_suppressing(RuntimeError)
        self.assert_released()

    def test_a_cancelled_preparation_that_cannot_be_saved_releases_input(self):
        self.update_setup()
        self.host.registry.persist.side_effect = CancelledError()
        self.host.persist_context.side_effect = OSError('disk full')
        self.start_suppressing(OSError)
        self.assert_released()

    def test_a_cancelled_page_preparation_whose_attempt_cannot_finish_releases_input(self):
        self.page_setup()
        self.host.redraw.side_effect = CancelledError()
        self.attempts.finish_page_attempt.side_effect = RuntimeError('attempt')
        self.start_suppressing(RuntimeError)
        self.assert_released()

    def test_a_failed_page_preparation_whose_attempt_cannot_finish_releases_input(self):
        self.page_setup()
        self.host.page_flow._operation_image_settings.side_effect = ValueError('settings')
        self.attempts.finish_page_attempt.side_effect = RuntimeError('attempt')
        self.start_suppressing(RuntimeError)
        self.assert_released()

    def test_a_cancelled_launch_that_cannot_be_saved_releases_input(self):
        self.update_setup()
        self.host.persist_context.side_effect = OSError('disk full')
        with contextlib.suppress(OSError):
            self.launch_cancelled()
        self.assert_released()

    def test_a_failed_page_launch_whose_attempt_cannot_finish_releases_input(self):
        self.page_setup()
        self.attempts.finish_page_attempt.side_effect = RuntimeError('attempt')
        thread = Mock()
        thread.start.side_effect = RuntimeError('thread unavailable')
        self.start_suppressing(RuntimeError, thread)
        self.assert_released()

    def test_a_started_worker_keeps_input_until_it_finalizes(self):
        self.update_setup()
        thread = Mock()
        with patch.object(self.mod, 'Thread', return_value=thread):
            self.lifecycle.start(self.action, replace=True)
        thread.start.assert_called_once_with()
        self.assertEqual(self.events, ['acquire'])
        self.assertEqual(len(self.lifecycle.active_scans), 1)


if __name__ == '__main__':
    unittest.main()
