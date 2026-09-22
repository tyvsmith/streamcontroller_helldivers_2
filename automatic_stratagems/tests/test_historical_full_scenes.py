import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from automatic_stratagems import fixture_evaluation


FIXTURES = Path(__file__).parent / 'fixtures' / 'historical-full-scenes'
MANIFEST = FIXTURES / 'manifest.json'
EXPECTED = {
    'gamescope-20260910-173122.png': [
        'Reinforce', 'SOSBeacon', 'Resupply', 'OrbitalGasStrike', 'Railgun',
        'SoloSilo', 'LaserSentry',
    ],
    'steam-20260910173549_1.jpg': [
        'Reinforce', 'SOSBeacon', 'Resupply', 'OrbitalGasStrike', 'Railgun',
        'SoloSilo', 'LaserSentry',
    ],
    'steam-20260908115655_1.jpg': [
        'Reinforce', 'SOSBeacon', 'Resupply', 'Stalwart', 'HeavyMachineGun',
        'Epoch', 'GrenadeLauncher',
    ],
}
MODES = {'steam-20260908115655_1.jpg': 'mission'}


class HistoricalFullSceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = fixture_evaluation.validate_manifest(MANIFEST)
        cls.temporary = TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.artifact = fixture_evaluation.evaluate_manifest(
            MANIFEST, Path(cls.temporary.name) / 'results.json',
            split='calibration', workers=1, interpreter=sys.executable)

    def test_manifest_preserves_independently_read_labels(self):
        self.assertEqual({case.relative_path for case in self.cases}, set(EXPECTED))
        manifest = json.loads(MANIFEST.read_text())
        for case in self.cases:
            with self.subTest(path=case.relative_path):
                self.assertEqual(case.split, 'calibration')
                self.assertEqual(case.dimensions, (5120, 2160))
                self.assertEqual(case.expected_mode,
                                 MODES.get(case.relative_path, 'selection'))
                self.assertEqual(case.expected_ids, EXPECTED[case.relative_path])
                item = next(item for item in manifest['cases']
                            if item['path'] == case.relative_path)
                self.assertEqual(item['provenance']['player_count'], 1)

    def test_full_pipeline_preserves_labels(self):
        self.assertEqual(self.artifact['workers'], 1)
        self.assertEqual(self.artifact['total'], len(EXPECTED))
        for case in self.artifact['cases']:
            with self.subTest(path=case['path']):
                self.assertIsNone(case['error'])
                self.assertEqual(case['actual_mode'],
                                 MODES.get(case['path'], 'selection'))
                self.assertEqual(case['actual_status'], 'matched')
                self.assertEqual(case['actual_ids'], EXPECTED[case['path']])


if __name__ == '__main__':
    unittest.main()
