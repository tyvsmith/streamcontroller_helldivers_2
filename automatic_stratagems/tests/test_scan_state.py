import json
from pathlib import Path
from tempfile import TemporaryDirectory
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
