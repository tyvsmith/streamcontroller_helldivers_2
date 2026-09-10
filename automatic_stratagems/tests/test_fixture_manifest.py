import json
import hashlib
from pathlib import Path
import unittest

from PIL import Image


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

    def test_inventory_pins_every_image_content_dimension_and_label_source(self):
        manifest = json.loads((FIXTURES / 'manifest.json').read_text())
        datasets = {dataset['id']: dataset for dataset in manifest['datasets']}
        live_labels = {
            item['file'] for item in
            json.loads((FIXTURES / 'live-tiles/labels.json').read_text())}
        retained = sorted(FIXTURES.glob('*/*.png'))
        inventory = manifest['inventory']
        self.assertEqual(set(inventory), {
            path.relative_to(FIXTURES).as_posix() for path in retained})
        for path in retained:
            relative = path.relative_to(FIXTURES).as_posix()
            with self.subTest(path=relative), Image.open(path) as image:
                item = inventory[relative]
                self.assertEqual(
                    set(item),
                    {'sha256', 'dimensions', 'label_ref', 'split', 'capture_id'})
                self.assertEqual(item['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(item['dimensions'], list(image.size))
                self.assertEqual(item['split'], 'calibration')
                self.assertIsInstance(item['capture_id'], str)
                label_ref = item['label_ref']
                if label_ref.startswith('manifest.json#dataset='):
                    reference = label_ref.removeprefix('manifest.json#dataset=')
                    dataset_id, field, *key = reference.split('/')
                    self.assertIn(dataset_id, datasets)
                    self.assertIn(field, datasets[dataset_id])
                    if key:
                        self.assertIn(key[0], datasets[dataset_id][field])
                elif label_ref.startswith('live-tiles/labels.json#file='):
                    self.assertIn(label_ref.rsplit('=', 1)[1], live_labels)
                elif label_ref == 'mission-fallbacks/resupply-cooldown.json#expected_ids':
                    labels = json.loads(
                        (FIXTURES / 'mission-fallbacks/resupply-cooldown.json').read_text())
                    self.assertIsInstance(labels['expected_ids'], list)
                else:
                    self.fail(f'Unresolvable label reference: {label_ref}')


if __name__ == '__main__':
    unittest.main()
