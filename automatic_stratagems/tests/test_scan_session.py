import unittest
from dataclasses import FrozenInstanceError
from concurrent.futures import ThreadPoolExecutor

from automatic_stratagems.scan_session import ScanSession


CATALOG = {key: {} for key in ('A', 'B', 'C', 'D')}


def report(*ids):
    return {'status': 'partial' if None in ids else 'matched',
            'rows': [{'id': key} for key in ids]}


class ScanSessionTests(unittest.TestCase):
    def test_canonical_slot_colors_are_the_complete_accepted_vocabulary(self):
        expected = ('any', 'red', 'blue', 'green', 'yellow')
        for color in expected:
            with self.subTest(color=color):
                session = ScanSession()
                self.assertIsNotNone(session.begin({1: color}))
        with self.assertRaisesRegex(ValueError, 'Invalid slot color'):
            ScanSession().begin({1: 'cyan'})

    def test_clear_forgets_assignments_and_report_and_rejects_inflight_result(self):
        session = ScanSession()
        token = session.begin({1: 'red', 2: 'any'})
        session.finish(token, report('A', None), CATALOG, {'A': 'red'})
        old = session.begin({1: 'red', 2: 'any'}, replace=True)
        session.clear()
        snapshot = session.snapshot()
        self.assertEqual(dict(snapshot.assignments), {1: None, 2: None})
        self.assertEqual((snapshot.status, snapshot.recognized, snapshot.unknown, snapshot.overflow), ('idle', 0, 0, 0))
        self.assertFalse(snapshot.unknown_slots | snapshot.unconfirmed_slots)
        self.assertIsNone(session.latest_report())
        self.assertIsNone(snapshot.last_scan_at)
        self.assertEqual(session.checkpoint()['slots']['1']['filter'], 'red')
        self.assertFalse(session.finish(old, report('B'), CATALOG))

    def setUp(self):
        self.session = ScanSession()

    def scan(self, ids, slots=(1, 2, 3, 4)):
        token = self.session.begin(list(slots))
        self.assertTrue(self.session.finish(token, report(*ids), CATALOG))
        return self.session.snapshot()

    def test_survivors_keep_slots_and_absent_ids_are_badged(self):
        self.scan(['A', 'B', 'C'])
        snapshot = self.scan(['C', 'D', 'A'])
        self.assertEqual(dict(snapshot.assignments), {1: 'A', 2: 'B', 3: 'C', 4: 'D'})
        self.assertEqual(snapshot.unconfirmed_slots, frozenset({2}))
        self.assertEqual(snapshot.status, 'partial')

    def test_unknown_does_not_replace_existing_assignments(self):
        self.scan(['A', 'B', 'C'])
        snapshot = self.scan([None, 'C', 'D'])
        self.assertEqual(dict(snapshot.assignments), {1: 'A', 2: 'B', 3: 'C', 4: 'D'})
        self.assertEqual(snapshot.unknown_slots, frozenset())
        self.assertEqual((snapshot.recognized, snapshot.unknown, snapshot.overflow), (2, 1, 0))
        self.assertEqual(snapshot.status, 'partial')

    def test_uncatalogued_ids_are_unknown(self):
        snapshot = self.scan(['arbitrary', 'A', ['invalid']])
        self.assertEqual(dict(snapshot.assignments), {1: 'A', 2: None, 3: None, 4: None})
        self.assertEqual(snapshot.unknown_slots, frozenset({2, 3}))

    def test_known_items_take_priority_and_duplicates_are_not_repeated(self):
        snapshot = self.scan(['A', 'A', None, 'B', 'C'], slots=(1, 2))
        self.assertEqual(dict(snapshot.assignments), {1: 'A', 2: 'B'})
        self.assertEqual((snapshot.recognized, snapshot.unknown, snapshot.overflow), (3, 1, 1))
        self.assertEqual(snapshot.status, 'partial')

    def test_complete_recognition_remains_ready_with_limited_page_capacity(self):
        for slots in ((), (1,), (1, 2)):
            with self.subTest(slots=slots):
                session = ScanSession()
                token = session.begin(slots, replace=True)
                session.finish(token, report('A', 'B', 'C'), CATALOG)
                snapshot = session.snapshot()
                self.assertEqual(snapshot.status, 'ready')
                self.assertEqual(snapshot.overflow, 3 - len(slots))
                self.assertEqual(snapshot.recognized, 3)
                self.assertEqual(session.latest_report(), report('A', 'B', 'C'))

    def test_partial_recognition_stays_partial_without_vacancies(self):
        token = self.session.begin([1], replace=True)
        self.session.finish(token, report('A', 'B', None), CATALOG)
        self.assertEqual(self.session.snapshot().status, 'partial')
        self.assertEqual(self.session.snapshot().unknown, 1)
        self.assertEqual(self.session.snapshot().overflow, 1)

    def test_failure_retains_ids(self):
        self.scan(['A'])
        for failure in ({'status': 'no_detections', 'rows': []},
                        {'status': 'error', 'error': 'capture failed'}):
            token = self.session.begin([1, 2])
            self.assertEqual(self.session.snapshot().status, 'scanning')
            self.assertTrue(self.session.finish(token, failure, CATALOG))
            snapshot = self.session.snapshot()
            self.assertEqual(snapshot.status, 'failed')
            self.assertEqual(snapshot.assignments[1], 'A')
            self.assertTrue(snapshot.message)

    def test_fail_and_cancel_ignore_late_results(self):
        token = self.session.begin([1])
        self.assertIsNone(self.session.begin([2]))
        self.session.cancel()
        self.assertEqual(self.session.snapshot().status, 'idle')
        newer = self.session.begin([2])
        self.assertFalse(self.session.finish(token, report('A'), CATALOG))
        self.assertFalse(self.session.fail(token, 'old failure'))
        self.assertTrue(self.session.fail(newer, 'new failure'))
        self.assertEqual(self.session.snapshot().message, 'new failure')
        self.assertFalse(self.session.finish(newer, report('A'), CATALOG))

    def test_snapshot_is_immutable_and_detached(self):
        before = self.scan(['A'])
        with self.assertRaises(TypeError):
            before.assignments[1] = 'B'
        with self.assertRaises(FrozenInstanceError):
            before.status = 'failed'
        after = self.scan(['B'])
        self.assertEqual(before.assignments[1], 'A')
        self.assertGreater(after.revision, before.revision)

    def test_slot_changes_drop_removed_slots_and_use_numeric_order(self):
        self.scan(['A', 'B', 'C'])
        snapshot = self.scan(['A', 'C', 'D'], slots=(5, 3, 3))
        self.assertEqual(dict(snapshot.assignments), {3: 'C', 5: 'A'})
        self.assertEqual(snapshot.overflow, 1)

    def test_only_one_concurrent_begin_wins(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(lambda _: self.session.begin([1]), range(20)))
        self.assertEqual(sum(token is not None for token in tokens), 1)

    def test_transient_scan_restores_state_with_monotonic_revision_and_token(self):
        self.scan(['A', 'B'], slots=(1, 2, 3))
        before = self.scan(['A', None], slots=(1, 2, 3))
        report_before = self.session.latest_report()
        checkpoint_before = self.session.checkpoint()
        token = self.session.begin({1: 'any', 2: 'any'}, replace=True, transient=True)
        self.assertEqual(self.session.snapshot().status, 'scanning')
        self.assertEqual(self.session.checkpoint()['status'], before.status)

        self.assertTrue(self.session.finish_transient(token))

        after = self.session.snapshot()
        self.assertEqual(dict(after.assignments), dict(before.assignments))
        self.assertEqual(after.unknown_slots, before.unknown_slots)
        self.assertEqual(after.unconfirmed_slots, before.unconfirmed_slots)
        self.assertEqual((after.status, after.message, after.recognized, after.unknown,
                          after.overflow, after.last_scan_at),
                         (before.status, before.message, before.recognized, before.unknown,
                          before.overflow, before.last_scan_at))
        self.assertEqual(self.session.latest_report(), report_before)
        self.assertGreater(after.revision, before.revision)
        self.assertGreater(self.session.checkpoint()['scan_number'],
                           checkpoint_before['scan_number'])

    def test_noop_reconcile_keeps_transient_scan_active(self):
        token = self.session.begin({1: 'red'})
        self.session.finish(token, report('A'), CATALOG, {'A': 'red'})
        token = self.session.begin({1: 'red'}, transient=True)

        self.assertFalse(self.session.reconcile({1: 'red'}))
        self.assertTrue(self.session.is_active(token))
        with self.assertRaises(ValueError):
            self.session.reconcile({1: 'invalid'})
        self.assertTrue(self.session.is_active(token))
        self.assertFalse(self.session.finish(token, report('A'), CATALOG))
        self.assertTrue(self.session.is_active(token))
        self.assertTrue(self.session.finish_transient(token))

    def test_transient_fail_cancel_clear_and_reconcile_do_not_resurrect_state(self):
        for ending in ('fail', 'cancel'):
            with self.subTest(ending=ending):
                session = ScanSession()
                token = session.begin([1])
                session.finish(token, report('A'), CATALOG)
                before = session.snapshot()
                token = session.begin([1], transient=True)
                if ending == 'fail':
                    self.assertTrue(session.fail(token, 'capture failed'))
                else:
                    session.cancel()
                self.assertEqual(dict(session.snapshot().assignments),
                                 dict(before.assignments))
                self.assertEqual(session.snapshot().status, before.status)

        token = self.session.begin([1])
        self.session.finish(token, report('A'), CATALOG)
        self.session.begin([1], transient=True)
        self.session.clear()
        self.assertIsNone(self.session.snapshot().assignments[1])
        self.session.begin([1], transient=True)
        self.session.reconcile({2: 'any'})
        self.assertEqual(dict(self.session.snapshot().assignments), {2: None})


if __name__ == '__main__':
    unittest.main()
