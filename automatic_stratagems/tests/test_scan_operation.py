from dataclasses import FrozenInstanceError
import threading
import unittest

from automatic_stratagems.scan_operation import ScanOperation, ScanPlan


class Subject:
    def is_active(self, token):
        return token == 3


class ScanOperationTests(unittest.TestCase):
    def plan(self):
        return ScanPlan(
            context=('deck', '/page', 'group'), session=Subject(), token=3,
            backend='screenshot', workers=2, source_snapshot=('source',),
            image_source={'kind': 'folder', 'path': '/captures'},
            source_action=None, cached_path=None, scan_revision=8,
            new_page=False, regenerate=False, attempt_id=None)

    def test_owns_events_weak_initiator_and_immutable_plan(self):
        action = Subject()
        operation = ScanOperation(
            action, continue_scan=True, on_complete=lambda _operation: None)
        self.assertIs(operation.initiator(), action)
        self.assertIsInstance(operation.cancel, threading.Event)
        self.assertIsInstance(operation.setup_done, threading.Event)
        self.assertIsInstance(operation.continuation, threading.Event)
        operation.bind_plan(self.plan())
        with self.assertRaises(FrozenInstanceError):
            operation.plan.backend = 'gamescope'
        with self.assertRaises(TypeError):
            operation.plan.image_source['path'] = '/other'
        with self.assertRaises(RuntimeError):
            operation.bind_plan(self.plan())

    def test_completion_callback_is_required_and_must_be_callable(self):
        with self.assertRaises(TypeError):
            ScanOperation(Subject(), continue_scan=True)
        with self.assertRaisesRegex(TypeError, 'completion callback'):
            ScanOperation(Subject(), continue_scan=True, on_complete=None)

    def test_completion_is_exactly_once(self):
        calls = []
        operation = ScanOperation(
            Subject(), continue_scan=True,
            on_complete=lambda completed: calls.append(completed))
        self.assertTrue(operation.complete())
        self.assertFalse(operation.complete())
        self.assertEqual(calls, [operation])

    def test_concurrent_completion_runs_release_once(self):
        barrier = threading.Barrier(8)
        calls = []
        operation = ScanOperation(
            Subject(), continue_scan=True,
            on_complete=lambda _operation: calls.append(threading.get_ident()))

        def finish():
            barrier.wait()
            operation.complete()

        threads = [threading.Thread(target=finish) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(calls), 1)

    def test_failed_completion_callback_is_not_reentered(self):
        calls = []

        def fail(_operation):
            calls.append('attempt')
            raise RuntimeError('release failed')

        operation = ScanOperation(Subject(), continue_scan=True, on_complete=fail)
        with self.assertRaisesRegex(RuntimeError, 'release failed'):
            operation.complete()
        self.assertFalse(operation.complete())
        self.assertEqual(calls, ['attempt'])

    def test_completion_callback_can_observe_duplicate_without_deadlock(self):
        results = []
        operation = ScanOperation(
            Subject(), continue_scan=True,
            on_complete=lambda completed: results.append(completed.complete()))
        thread = threading.Thread(
            target=operation.complete,
            daemon=True)
        thread.start()
        thread.join(.2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [False])

    def test_worker_setup_state_is_explicit(self):
        operation = ScanOperation(
            Subject(), continue_scan=False, on_complete=lambda _operation: None)
        worker = Subject()
        operation.bind_worker(worker)
        operation.mark_started()
        self.assertIs(operation.worker, worker)
        self.assertTrue(operation.started)
        self.assertTrue(operation.setup_done.is_set())

    def test_presentation_binding_is_owned_and_stale_binding_clears(self):
        action = Subject()
        plan = self.plan()
        operation = ScanOperation(
            action, continue_scan=True, on_complete=lambda _operation: None)
        operation.bind_plan(plan)
        operation.bind_presentation()
        self.assertTrue(operation.action_presentation_is_active(
            action, plan.context))
        self.assertFalse(operation.action_presentation_is_active(
            action, ('other',), clear_stale=True))
        self.assertIsNone(action._scan_presentation)


if __name__ == '__main__':
    unittest.main()
