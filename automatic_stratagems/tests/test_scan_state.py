import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

from automatic_stratagems.scan_session import ScanSession
from automatic_stratagems.scan_state import ScanStateStore

CATALOG = {'A': ['UP'], 'B': ['DOWN'], 'C': ['LEFT'], 'D': ['RIGHT']}
COLORS = dict.fromkeys(CATALOG, 'blue')
CONTEXT = {'deck': 'deck-1', 'page': '/pages/HD2.json', 'group': 'HD2'}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ScanStateStore(Path(self.tmp.name))
        self.session = ScanSession()

    def scan(self, *ids):
        token = self.session.begin({1: 'blue', 2: 'any', 3: 'any'})
        self.session.finish(token, {'status': 'matched', 'rows': [{'id': x} for x in ids]}, CATALOG, COLORS)

    def save(self):
        return self.store.save(CONTEXT, self.session, CATALOG, COLORS, {'A': 'Alpha'})

    def test_readable_export_and_restart_preserve_assignment_age(self):
        self.scan('A', 'B', 'C')
        self.scan('A', 'C')
        self.scan('C')
        path = self.save()
        data = json.loads(path.read_text())
        self.assertEqual(data['context'], CONTEXT)
        self.assertEqual(data['slots']['1']['name'], 'Alpha')
        self.assertEqual(data['slots']['1']['sequence'], ['UP'])
        self.assertEqual(data['slots']['1']['color'], 'blue')
        self.assertEqual(data['slots']['1']['filter'], 'blue')
        self.assertTrue(data['slots']['1']['unconfirmed'])
        self.assertIsNotNone(data['updated_at'])
        self.session = self.store.load(CONTEXT, CATALOG)
        self.scan('C', 'D')
        self.assertEqual(dict(self.session.snapshot().assignments), {1: 'A', 2: 'D', 3: 'C'})

    def test_failed_scan_keeps_assignments_in_export(self):
        self.scan('A')
        token = self.session.begin([1, 2, 3])
        self.session.fail(token, 'capture failed')
        data = json.loads(self.save().read_text())
        self.assertEqual(data['status'], 'failed')
        self.assertEqual(data['slots']['1']['id'], 'A')
        self.assertEqual(self.store.load(CONTEXT, CATALOG).snapshot().status, 'failed')

    def test_interrupted_scan_restores_usable_assignments(self):
        self.scan('A')
        self.session.begin([1, 2, 3])
        self.save()
        restored = self.store.load(CONTEXT, CATALOG).snapshot()
        self.assertEqual(restored.assignments[1], 'A')
        self.assertEqual(restored.status, 'partial')
        self.assertFalse(restored.replacing)

    def test_missing_file_returns_empty_session(self):
        self.assertFalse(self.store.load(CONTEXT, CATALOG).snapshot().assignments)

    def test_bad_file_is_rejected(self):
        self.scan('A')
        path = self.save()
        for payload in ['{', '[]', '{"schema_version": 99}', '{"schema_version": 1}']:
            with self.subTest(payload=payload):
                path.write_text(payload)
                with self.assertRaises(ValueError):
                    self.store.load(CONTEXT, CATALOG)

    def test_oversized_state_is_rejected_without_replacing_it(self):
        self.scan('A')
        path = self.save()
        data = json.loads(path.read_text())
        data['padding'] = 'x' * (1024 * 1024)
        path.write_text(json.dumps(data))
        before = path.read_bytes()

        with self.assertRaisesRegex(ValueError, 'too large'):
            self.store.load(CONTEXT, CATALOG)

        self.assertEqual(path.read_bytes(), before)

    def test_deep_state_is_rejected_before_restore_recursion(self):
        self.scan('A')
        path = self.save()
        data = json.loads(path.read_text())
        nested = {}
        for _ in range(500):
            nested = {'next': nested}
        data['last_report']['rows'][0]['extra'] = nested
        path.write_text(json.dumps(data))

        try:
            self.store.load(CONTEXT, CATALOG)
        except RecursionError:
            self.fail('deep state escaped as RecursionError')
        except ValueError as error:
            self.assertIn('nested', str(error))
        else:
            self.fail('deep state was accepted')

    def test_state_symlink_is_rejected_without_touching_target(self):
        self.scan('A')
        path = self.save()
        target = path.with_name('unrelated.json')
        target.write_bytes(path.read_bytes())
        before = target.read_bytes()
        path.unlink()
        path.symlink_to(target)

        with self.assertRaises(ValueError):
            self.store.load(CONTEXT, CATALOG)

        self.assertEqual(target.read_bytes(), before)

    def test_state_fifo_is_rejected_without_blocking(self):
        path = self.store.path(CONTEXT)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)
        done = Event()
        outcome = []

        def load():
            try:
                self.store.load(CONTEXT, CATALOG)
            except Exception as error:
                outcome.append(error)
            finally:
                done.set()

        thread = Thread(target=load, daemon=True)
        thread.start()
        completed_without_writer = done.wait(.2)
        if not completed_without_writer:
            writer = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            os.write(writer, b'{}')
            os.close(writer)
            done.wait(1)
        thread.join(1)

        self.assertTrue(completed_without_writer, 'state load blocked opening a FIFO')
        self.assertIsInstance(outcome[0], ValueError)

    def test_removed_catalog_id_is_not_restored_or_executed(self):
        self.scan('A', 'B')
        self.save()
        s = self.store.load(CONTEXT, {'B': ['RIGHT']}).snapshot()
        self.assertIsNone(s.assignments[1])
        self.assertEqual(s.assignments[2], 'B')
        self.assertIn(1, s.unknown_slots)

    def test_deck_page_group_isolation(self):
        self.scan('A')
        original = self.save()
        for field in CONTEXT:
            context = dict(CONTEXT, **{field: 'different'})
            self.assertFalse(self.store.load(context, CATALOG).snapshot().assignments)
            self.assertNotEqual(original, self.store.path(context))

    def test_failed_atomic_replace_preserves_previous_file(self):
        self.scan('A')
        path = self.save()
        previous = path.read_bytes()
        self.scan('B')
        with patch('automatic_stratagems.scan_state.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_tampered_sequence_is_not_used_on_restore(self):
        self.scan('A')
        path = self.save()
        data = json.loads(path.read_text())
        data['slots']['1']['sequence'] = ['TAMPERED']
        path.write_text(json.dumps(data))
        self.session = self.store.load(CONTEXT, CATALOG)
        data = json.loads(self.save().read_text())
        self.assertEqual(data['slots']['1']['sequence'], ['UP'])

    def test_invalid_slot_age_is_rejected(self):
        self.scan('A', 'B')
        self.scan('B')
        path = self.save()
        data = json.loads(path.read_text())
        data['slots']['1']['unconfirmed_since_scan'] = 'yesterday'
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.store.load(CONTEXT, CATALOG)

    def test_interrupted_first_scan_does_not_restore_as_scanning(self):
        self.session.begin([1, 2, 3])
        self.save()
        self.assertEqual(self.store.load(CONTEXT, CATALOG).snapshot().status, 'idle')

    def test_latest_observation_survives_restart_without_slot_truncation(self):
        self.scan('A', 'B', 'C', 'D')
        self.save()
        restored = self.store.load(CONTEXT, CATALOG)
        self.assertEqual(restored.latest_report()['rows'], [{'id': key} for key in ('A', 'B', 'C', 'D')])
        copy = restored.latest_report()
        copy['rows'].clear()
        self.assertEqual(len(restored.latest_report()['rows']), 4)

    def test_older_state_without_observation_loads_and_malformed_one_is_rejected(self):
        self.scan('A')
        path = self.save()
        data = json.loads(path.read_text())
        data.pop('last_report')
        path.write_text(json.dumps(data))
        self.assertIsNone(self.store.load(CONTEXT, CATALOG).latest_report())
        data['last_report'] = {'status': 'matched', 'rows': [42]}
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.store.load(CONTEXT, CATALOG)
