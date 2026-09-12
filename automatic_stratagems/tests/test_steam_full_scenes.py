import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from automatic_stratagems import fixture_evaluation


FIXTURES = Path(__file__).parent / 'fixtures' / 'steam-full-scenes'
MANIFEST = FIXTURES / 'manifest.json'
ROVER_SELECTION = '20260911164727_1.jpg'

EXPECTED = {
    '20260911164029_1.jpg': (
        'selection',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'EagleNapalmAirstrike',
         'Autocannon', 'GuardDog', 'EMSMortarSentry'],
        1,
    ),
    '20260911164130_1.jpg': (
        'mission',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'EagleNapalmAirstrike',
         'Autocannon', 'GuardDog', 'EMSMortarSentry'],
        1,
    ),
    '20260911164153_1.jpg': ('mission', [], 1),
    '20260911164450_1.jpg': (
        'selection',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'Hellbomb', None,
         'Flamethrower', 'FlameSentry', 'OrbitalAirburstStrike',
         'AntiTankMines'],
        4,
    ),
    '20260911164548_1.jpg': (
        'mission',
        ['Reinforce', 'Resupply', 'Hellbomb', 'Flamethrower', 'FlameSentry',
         'OrbitalAirburstStrike', 'AntiTankMines'],
        4,
    ),
    '20260911164727_1.jpg': (
        'selection',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'Hellbomb', 'GuardDogRover',
         'Railgun', 'HMGEmplacement', 'OrbitalGasStrike'],
        2,
    ),
    '20260911164808_1.jpg': ('mission', [], 2),
    '20260911164816_1.jpg': (
        'mission',
        ['Reinforce', 'Resupply', 'GuardDogRover', 'Railgun',
         'HMGEmplacement', 'OrbitalGasStrike'],
        2,
    ),
}


class SteamFullSceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = fixture_evaluation.validate_manifest(MANIFEST)
        cls.temporary = TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.artifact = fixture_evaluation.evaluate_manifest(
            MANIFEST, Path(cls.temporary.name) / 'results.json',
            split='calibration', workers=1, interpreter=sys.executable)

    def test_manifest_preserves_the_independently_labeled_calibration_set(self):
        self.assertEqual({case.relative_path for case in self.cases}, set(EXPECTED))
        for case in self.cases:
            with self.subTest(path=case.relative_path):
                mode, identifiers, players = EXPECTED[case.relative_path]
                self.assertEqual(case.split, 'calibration')
                self.assertEqual(case.dimensions, (5120, 2160))
                self.assertEqual(case.expected_mode, mode)
                self.assertEqual(case.expected_ids, identifiers)
                manifest_case = next(
                    item for item in json.loads(MANIFEST.read_text())['cases']
                    if item['path'] == case.relative_path)
                self.assertEqual(manifest_case['provenance']['player_count'], players)

    def test_full_pipeline_preserves_labels(self):
        self.assertEqual(self.artifact['workers'], 1)
        self.assertEqual(self.artifact['total'], len(EXPECTED))
        self.assertEqual(
            [case['path'] for case in self.artifact['cases']
             if case['status'] == 'failed'], [])
        for case in self.artifact['cases']:
            with self.subTest(path=case['path']):
                mode, identifiers, _ = EXPECTED[case['path']]
                expected = list(identifiers)
                status = ('no_detections' if not expected else
                          'partial' if None in expected else 'matched')
                self.assertIsNone(case['error'])
                self.assertEqual(case['actual_mode'], mode)
                self.assertEqual(case['actual_status'], status)
                self.assertEqual(case['actual_ids'], expected)

    def test_selection_rover_meets_visual_accuracy_label(self):
        case = next(case for case in self.artifact['cases']
                    if case['path'] == ROVER_SELECTION)
        self.assertEqual(case['actual_ids'][4], 'GuardDogRover')

    def test_closed_menus_do_not_turn_compact_hud_icons_into_rows(self):
        results = {case['path']: case for case in self.artifact['cases']}
        for path in ('20260911164153_1.jpg', '20260911164808_1.jpg'):
            with self.subTest(path=path):
                self.assertEqual(results[path]['actual_mode'], 'mission')
                self.assertEqual(results[path]['actual_status'], 'no_detections')
                self.assertEqual(results[path]['actual_ids'], [])

    def test_multiplayer_selection_returns_only_the_local_left_panel(self):
        results = {case['path']: case for case in self.artifact['cases']}
        for path in ('20260911164450_1.jpg', '20260911164727_1.jpg'):
            with self.subTest(path=path):
                self.assertEqual(results[path]['actual_mode'], 'selection')
                expected = list(EXPECTED[path][1])
                self.assertEqual(results[path]['actual_ids'], expected)


if __name__ == '__main__':
    unittest.main()
