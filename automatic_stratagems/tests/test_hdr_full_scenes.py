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
        'b11249c46d2099e09791b5a7f21e748471785e08def334d3860b5ccbd0cf1af1',
        'd82823dec9c2259e8e741437acdb7fc260dfb32b7104f0b68b8b8ae5a39462d6',
    ),
    'screenshot-2026-09-06_22-14-47.png': (
        'mission',
        ['Reinforce', 'SOSBeacon', 'Resupply', None, 'EagleRearm',
         'OrbitalGatlingBarrage', 'Meltagun', 'WarpPack', 'RocketSentry'],
        (5120, 2160),
        'f64cfd7f8209718206dcfbb0026f05090b48751ff26dabeccd5ac6d22f1bba80',
        'cc0208a34f33e4e588fd8178ff01f2b4dba848fb4a1f131be106d2e9c43b693a',
    ),
    'screenshot-2026-09-08_11-52-36.png': (
        'mission',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'Stalwart',
         'HeavyMachineGun', 'MachineGun', 'Railgun'],
        (5120, 2160),
        '99bafc7caa0e0da7ee3e0db117bda8870fbdf227939b3808438fbd9df5afeceb',
        'c180c36d3cc9ec33b0b3f972c6fdc6b623d3fc155b16172a3a6a9df866e405a0',
    ),
    'screenshot-2026-09-08_12-20-34.png': (
        'mission', [], (5122, 2162),
        '424875627196aeb117bd7f529e381aba3df6d6042bb653a14ab3fb4084d7364f',
        '05215e3eba005a68bf267d9dbee42e3359be3215e6a9d18d44d9e12e189d84c6',
    ),
    WASHED_OUT_SELECTION: (
        'selection',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'Stalwart',
         'HeavyMachineGun', 'Railgun', 'Speargun'],
        (5122, 2162),
        'fbd80906282a0f2f63a4ede9264450822bb9981aa9bf7818c907e6faa3471da8',
        '05abf6df01c129db7bdf486787efdc97b7bd716b48d2623d1a648a5b7a740f84',
    ),
    'screenshot-2026-09-10_19-48-31.png': (
        'mission', [], (1433, 819),
        '3db1642631d054b461b1c2d4b4f35a64f9571f8dc70ee05cbe3dcbf3bb24b529',
        'dfcbf4e2da3c129c795fb3e6dbb214d885ad7ef4dc39934f83f95be5a8878887',
    ),
    'screenshot-2026-09-11_14-19-16.png': (
        'selection',
        ['Reinforce', 'SOSBeacon', 'Resupply', 'OrbitalGasStrike',
         'GrenadeLauncher', 'HellbombPortable', 'BreakthroughExosuit'],
        (5120, 2160),
        'd126756ab7ebeead80da9178465d89d4d770cc4eb11f37b0ad1bf14a6f62295f',
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

    def test_manifest_pins_reencoded_bytes_and_original_capture_digest(self):
        self.assertEqual({case.relative_path for case in self.cases}, set(EXPECTED))
        manifest = json.loads(MANIFEST.read_text())['cases']
        for case in self.cases:
            with self.subTest(path=case.relative_path):
                mode, identifiers, dimensions, digest, source_digest = (
                    EXPECTED[case.relative_path])
                self.assertEqual(case.split, 'calibration')
                self.assertEqual(case.expected_mode, mode)
                self.assertEqual(case.expected_ids, identifiers)
                self.assertEqual(case.dimensions, dimensions)
                self.assertEqual(case.sha256, digest)
                self.assertEqual(case.source_sha256, source_digest)
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
                mode, identifiers, *_ = EXPECTED[case['path']]
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
