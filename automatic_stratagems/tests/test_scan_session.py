import unittest
from dataclasses import FrozenInstanceError
from concurrent.futures import ThreadPoolExecutor

from automatic_stratagems.scan_session import ScanSession


CATALOG = {key: {} for key in ('A', 'B', 'C', 'D')}


def report(*ids):
    return {'status': 'partial' if None in ids else 'matched',
            'rows': [{'id': key} for key in ids]}


class ScanSessionTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
