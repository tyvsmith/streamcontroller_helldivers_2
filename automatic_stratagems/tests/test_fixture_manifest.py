import json
from pathlib import Path
import unittest


FIXTURES = Path(__file__).parent / 'fixtures'


class FixtureManifestTests(unittest.TestCase):
    def test_manifest_accounts_for_every_retained_image_once(self):
        manifest = json.loads((FIXTURES / 'manifest.json').read_text())
        accounted = []
        required = {'source', 'capture_id', 'geometry', 'resolution', 'layout',
                    'backend', 'hdr', 'language', 'purpose'}
        for dataset in manifest['datasets']:
            with self.subTest(dataset=dataset['id']):
                self.assertFalse(required - dataset.keys())
                matched = sorted(FIXTURES.glob(dataset['files']))
                self.assertTrue(matched)
                accounted.extend(matched)
                if 'expectations_file' in dataset:
                    self.assertTrue((FIXTURES / dataset['expectations_file']).is_file())
        retained = sorted(FIXTURES.glob('*/*.png'))
        self.assertEqual(sorted(accounted), retained)
        self.assertEqual(len(accounted), len(set(accounted)))
        self.assertEqual(manifest['evidence']['split'], 'calibration')
        self.assertEqual(manifest['evidence']['held_out_captures'], [])

    def test_manifest_expectations_cover_mission_panels_and_live_tiles(self):
        manifest = json.loads((FIXTURES / 'manifest.json').read_text())
        datasets = {dataset['id']: dataset for dataset in manifest['datasets']}
        mission_files = {path.name for path in (FIXTURES / 'mission-panels').glob('*.png')}
        self.assertEqual(set(datasets['mission-panels']['expected_ids']), mission_files)
        labels = json.loads((FIXTURES / 'live-tiles/labels.json').read_text())
        live_files = {path.name for path in (FIXTURES / 'live-tiles').glob('*.png')}
        self.assertEqual({case['file'] for case in labels}, live_files)


if __name__ == '__main__':
    unittest.main()
