import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from automatic_stratagems import fixture_evaluation
from automatic_stratagems.scanner.capture_backends import capture_issue


FIXTURES = Path(__file__).parent / 'fixtures' / 'hdr-full-scenes'
MANIFEST = FIXTURES / 'manifest.json'
WASHED_OUT_SELECTION = 'screenshot-2026-09-08_12-21-37.png'

EXPECTED = {
    'screenshot-2026-05-10_15-18-30.png': (
        'selection',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'Hellbomb',
         'AntiMaterialRifle', 'RocketSentry', 'WarpPack',
         'OrbitalGatlingBarrage'],
        (4943, 1936),
        'd82823dec9c2259e8e741437acdb7fc260dfb32b7104f0b68b8b8ae5a39462d6',
    ),
    'screenshot-2026-09-06_22-14-47.png': (
        'mission',
        ['Reinforce', 'SOSBeacon', 'Resupply', None, 'EagleRearm',
         'OrbitalGatlingBarrage', 'Meltagun', 'WarpPack', 'RocketSentry'],
        (5120, 2160),
        'cc0208a34f33e4e588fd8178ff01f2b4dba848fb4a1f131be106d2e9c43b693a',
    ),
    'screenshot-2026-09-08_11-52-36.png': (
        'mission',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'Stalwart',
         'HeavyMachineGun', 'MachineGun', 'Railgun'],
        (5120, 2160),
        'c180c36d3cc9ec33b0b3f972c6fdc6b623d3fc155b16172a3a6a9df866e405a0',
    ),
    'screenshot-2026-09-08_11-57-00.png': (
        'mission',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'Stalwart',
         'HeavyMachineGun', 'Epoch', 'GrenadeLauncher'],
        (5120, 2160),
        '9af9007a156b13712611cf222e9c447637a700c0c3826a7f4733a2c85077ac58',
    ),
    'screenshot-2026-09-08_12-20-34.png': (
        'mission', [], (5122, 2162),
        '05215e3eba005a68bf267d9dbee42e3359be3215e6a9d18d44d9e12e189d84c6',
    ),
    WASHED_OUT_SELECTION: (
        'selection',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'Stalwart',
         'HeavyMachineGun', 'Railgun', 'Speargun'],
        (5122, 2162),
        '05abf6df01c129db7bdf486787efdc97b7bd716b48d2623d1a648a5b7a740f84',
    ),
    'screenshot-2026-09-08_12-22-27.png': (
        'mission',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'Stalwart',
         'HeavyMachineGun', 'Railgun', 'Speargun'],
        (5122, 2162),
        'dc0db2965e6ceec0441b07af2868ab4c04f95543ab713b172c6f5241778cd1b6',
    ),
    'screenshot-2026-09-10_19-48-31.png': (
        'mission', [], (1433, 819),
        'dfcbf4e2da3c129c795fb3e6dbb214d885ad7ef4dc39934f83f95be5a8878887',
    ),
    'screenshot-2026-09-11_14-19-16.png': (
        'selection',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'OrbitalGasStrike',
         'GrenadeLauncher', 'HellbombPortable', 'BreakthroughExosuit'],
        (5120, 2160),
        '6a153bc196efa9f15ddbbebde3f03f0407fc1d244af2558d329b7a85d8a7f332',
    ),
}


class HdrFullSceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = fixture_evaluation.validate_manifest(MANIFEST)
        cls.temporary = TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.artifact = fixture_evaluation.evaluate_manifest(
            MANIFEST, Path(cls.temporary.name) / 'results.json',
            split='calibration', workers=1, interpreter=sys.executable)

    def test_manifest_preserves_independently_labeled_original_bytes(self):
        self.assertEqual({case.relative_path for case in self.cases}, set(EXPECTED))
        manifest = json.loads(MANIFEST.read_text())['cases']
        for case in self.cases:
            with self.subTest(path=case.relative_path):
                mode, identifiers, dimensions, digest = EXPECTED[case.relative_path]
                self.assertEqual(case.split, 'calibration')
                self.assertEqual(case.expected_mode, mode)
                self.assertEqual(case.expected_ids, identifiers)
                self.assertEqual(case.dimensions, dimensions)
                self.assertEqual(case.sha256, digest)
                source = next(item for item in manifest
                              if item['path'] == case.relative_path)
                self.assertEqual(source['provenance']['source'],
                                 'user-provided original HDR screenshot')
                self.assertEqual(source['provenance']['hdr'], 'user-reported')

    def test_raw_offline_pipeline_matches_visual_labels(self):
        self.assertEqual(self.artifact['workers'], 1)
        self.assertEqual(self.artifact['total'], len(EXPECTED))
        self.assertEqual(
            [case['path'] for case in self.artifact['cases']
             if case['status'] == 'failed'], [])
        for case in self.artifact['cases']:
            with self.subTest(path=case['path']):
                self.assertIsNone(case['error'])
                mode, identifiers, _, _ = EXPECTED[case['path']]
                status = ('no_detections' if not identifiers else
                          'partial' if None in identifiers else 'matched')
                self.assertEqual(case['actual_mode'], mode)
                self.assertEqual(case['actual_status'], status)
                self.assertEqual(case['actual_ids'], identifiers)

    def test_negative_scenes_produce_no_rows(self):
        results = {case['path']: case for case in self.artifact['cases']}
        for path in ('screenshot-2026-09-08_12-20-34.png',
                     'screenshot-2026-09-10_19-48-31.png'):
            with self.subTest(path=path):
                self.assertEqual(results[path]['actual_status'], 'no_detections')
                self.assertEqual(results[path]['actual_ids'], [])

    def test_screenshot_capture_quality_gate_accepts_selected_images(self):
        for case in self.cases:
            with self.subTest(path=case.relative_path), Image.open(case.path) as image:
                self.assertIsNone(capture_issue(image, 'screenshot'))


if __name__ == '__main__':
    unittest.main()
