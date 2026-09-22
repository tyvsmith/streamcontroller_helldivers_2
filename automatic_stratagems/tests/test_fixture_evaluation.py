import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from automatic_stratagems import fixture_evaluation as fixtures


class FixtureEvaluationTests(unittest.TestCase):
    def image(self, path, size=(320, 200), color='black'):
        Image.new('RGB', size, color).save(path)
        return path

    def provenance(self, capture_id='capture-one'):
        return {
            'capture_id': capture_id,
            'source': 'private user capture',
            'captured_at': '2026-09-09T12:00:00+00:00',
            'backend': 'portal',
            'platform': 'linux',
            'desktop': 'gnome',
            'resolution': [320, 200],
            'hdr': 'off',
            'language': 'English',
            'game_build': 'example-build',
            'player_count': 1,
            'hud_scale': '100%',
            'safe_area': '100%',
        }

    def case(self, path, *, split='heldout', ids=None, mode='mission',
             capture_id='capture-one'):
        with Image.open(path) as image:
            dimensions = list(image.size)
        return {
            'path': path.name,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'dimensions': dimensions,
            'split': split,
            'provenance': self.provenance(capture_id),
            'expected': {'mode': mode, 'ids': [] if ids is None else ids},
        }

    def write_manifest(self, directory, cases):
        path = Path(directory) / 'fixtures.json'
        path.write_text(json.dumps({'schema_version': 1, 'cases': cases}))
        return path

    def test_prepare_records_integrity_and_incomplete_metadata_without_copying(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = self.image(root / 'scene.png')
            output = root / 'fixtures.json'

            manifest = fixtures.prepare_manifest(root, output)

            self.assertEqual(manifest['cases'][0]['path'], 'scene.png')
            self.assertEqual(manifest['cases'][0]['dimensions'], [320, 200])
            self.assertEqual(
                manifest['cases'][0]['sha256'], hashlib.sha256(image.read_bytes()).hexdigest())
            self.assertIsNone(manifest['cases'][0]['split'])
            self.assertEqual(manifest['cases'][0]['provenance']['game_build'], 'unknown')
            self.assertIsNone(manifest['cases'][0]['expected']['ids'])
            self.assertTrue(image.exists())
            with self.assertRaisesRegex(ValueError, 'already exists'):
                fixtures.prepare_manifest(root, output)

    def test_prepare_rejects_output_that_needs_parent_escape(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'images'
            images.mkdir()
            self.image(images / 'scene.png')
            with self.assertRaisesRegex(ValueError, 'inside the manifest directory'):
                fixtures.prepare_manifest(images, root / 'other' / 'fixtures.json')

    def test_prepare_rejects_symlink_root_and_bounded_traversal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'images'
            images.mkdir()
            self.image(images / 'scene.png')
            link = root / 'linked-images'
            link.symlink_to(images, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'non-symlink directory'):
                fixtures.prepare_manifest(link, root / 'fixtures.json')

            (images / 'extra.txt').touch()
            (images / 'another.txt').touch()
            with patch.object(fixtures, 'MAX_DISCOVERY_ENTRIES', 2):
                with self.assertRaisesRegex(ValueError, 'entry limit'):
                    fixtures.prepare_manifest(images, root / 'fixtures.json')

            nested = images
            for index in range(fixtures.MAX_DISCOVERY_DEPTH + 1):
                nested = nested / str(index)
                nested.mkdir()
            with self.assertRaisesRegex(ValueError, 'depth limit'):
                fixtures.prepare_manifest(images, root / 'fixtures.json')

    def test_validation_rejects_escape_symlink_integrity_and_missing_labels(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = self.image(root / 'scene.png')
            valid = self.case(image, ids=['Reinforce', None])
            mutations = (
                ('path', '../scene.png', 'relative path'),
                ('sha256', '0' * 64, 'SHA-256'),
                ('dimensions', [1, 1], 'dimensions'),
                ('expected', {'mode': 'mission', 'ids': None}, 'labels'),
                ('expected', {'mode': 'mission', 'ids': ['NotInCatalog']}, 'catalog'),
            )
            for field, value, message in mutations:
                with self.subTest(field=field, value=value):
                    case = {**valid, field: value}
                    manifest = self.write_manifest(root, [case])
                    with self.assertRaisesRegex(ValueError, message):
                        fixtures.validate_manifest(manifest, builtin_manifest=None)

            target = root / 'target.png'
            image.rename(target)
            image.symlink_to(target.name)
            case = self.case(target, ids=[])
            case['path'] = image.name
            manifest = self.write_manifest(root, [case])
            with self.assertRaisesRegex(ValueError, 'symlink'):
                fixtures.validate_manifest(manifest, builtin_manifest=None)

    def test_validation_rejects_boolean_schema_and_nontext_heldout_metadata(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = self.image(root / 'scene.png')
            case = self.case(image, ids=[])
            manifest = self.write_manifest(root, [case])
            value = json.loads(manifest.read_text())
            value['schema_version'] = True
            manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'schema version'):
                fixtures.validate_manifest(manifest, builtin_manifest=None)

            case['provenance']['backend'] = 7
            manifest = self.write_manifest(root, [case])
            with self.assertRaisesRegex(ValueError, 'backend'):
                fixtures.validate_manifest(manifest, builtin_manifest=None)

            case = self.case(image, split='calibration', ids=[])
            case['provenance']['backend'] = 7
            manifest = self.write_manifest(root, [case])
            with self.assertRaisesRegex(ValueError, 'backend'):
                fixtures.validate_manifest(manifest, builtin_manifest=None)

    def test_heldout_requires_independence_metadata_but_not_rights_approval(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = self.image(root / 'scene.png')
            case = self.case(image, ids=[])
            case['provenance']['game_build'] = 'unknown'
            manifest = self.write_manifest(root, [case])
            with self.assertRaisesRegex(ValueError, 'game_build'):
                fixtures.validate_manifest(manifest, builtin_manifest=None)

            case['provenance']['game_build'] = 'example-build'
            case['provenance']['rights'] = 'unknown'
            case['provenance']['privacy_reviewed'] = False
            manifest = self.write_manifest(root, [case])
            validated = fixtures.validate_manifest(manifest, builtin_manifest=None)
            self.assertEqual(validated[0].expected_ids, [])

    def test_validation_rejects_hash_and_capture_identity_crossing_splits(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.image(root / 'first.png', color='red')
            second = root / 'second.png'
            second.write_bytes(first.read_bytes())
            calibration = self.case(first, split='calibration', ids=[],
                                    capture_id='capture-a')
            calibration['provenance'] = {
                key: ('unknown' if key not in ('capture_id', 'resolution', 'player_count')
                      else value)
                for key, value in calibration['provenance'].items()
            }
            heldout = self.case(second, ids=[], capture_id='capture-b')
            manifest = self.write_manifest(root, [calibration, heldout])
            with self.assertRaisesRegex(ValueError, 'content.*splits'):
                fixtures.validate_manifest(manifest, builtin_manifest=None)

            self.image(second, color='blue')
            heldout = self.case(second, ids=[], capture_id='capture-a')
            manifest = self.write_manifest(root, [calibration, heldout])
            with self.assertRaisesRegex(ValueError, 'capture_id.*splits'):
                fixtures.validate_manifest(manifest, builtin_manifest=None)

    def test_validation_rejects_known_builtin_capture_in_heldout(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = self.image(root / 'scene.png', color='purple')
            case = self.case(image, ids=[], capture_id='18n2fofu')
            manifest = self.write_manifest(root, [case])
            with self.assertRaisesRegex(ValueError, 'capture_id.*splits'):
                fixtures.validate_manifest(manifest)

            case = self.case(image, ids=[], capture_id=' 18n2fofu ')
            manifest = self.write_manifest(root, [case])
            with self.assertRaisesRegex(ValueError, 'capture_id'):
                fixtures.validate_manifest(manifest)

            builtin = (Path(__file__).parent / 'fixtures' / 'mission-panels' /
                       '18n2fofu.png')
            image.write_bytes(builtin.read_bytes())
            case = self.case(image, ids=[], capture_id='new-private-capture')
            case['provenance']['resolution'] = case['dimensions']
            manifest = self.write_manifest(root, [case])
            with self.assertRaisesRegex(ValueError, 'content.*splits'):
                fixtures.validate_manifest(manifest)

    def test_source_digest_is_optional_validated_and_checked_for_overlap(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = self.image(root / 'scene.png')
            original = hashlib.sha256(b'original capture bytes').hexdigest()
            case = self.case(image, split='calibration', ids=[])
            case['source_sha256'] = original
            manifest = self.write_manifest(root, [case])
            validated = fixtures.validate_manifest(manifest, builtin_manifest=None)
            self.assertEqual(validated[0].source_sha256, original)
            self.assertEqual(validated[0].sha256, case['sha256'])

            for value in ('0' * 63, 'A' * 64, None, 7):
                with self.subTest(source_sha256=value):
                    manifest = self.write_manifest(root, [{**case, 'source_sha256': value}])
                    with self.assertRaisesRegex(ValueError, 'source SHA-256'):
                        fixtures.validate_manifest(manifest, builtin_manifest=None)

            # A held-out copy of the original bytes still overlaps the re-encoded case.
            other = self.image(root / 'other.png', color='white')
            heldout = self.case(other, ids=[], capture_id='capture-two')
            case['source_sha256'] = heldout['sha256']
            manifest = self.write_manifest(root, [case, heldout])
            with self.assertRaisesRegex(ValueError, 'content.*splits'):
                fixtures.validate_manifest(manifest, builtin_manifest=None)

            builtin = root / 'builtin.json'
            builtin.write_text(json.dumps({'inventory': {'panel.png': {
                'sha256': 'a' * 64, 'source_sha256': heldout['sha256'],
                'split': 'calibration', 'capture_id': 'builtin-capture'}}}))
            manifest = self.write_manifest(root, [heldout])
            with self.assertRaisesRegex(ValueError, 'content.*splits'):
                fixtures.validate_manifest(manifest, builtin_manifest=builtin)

    def test_evaluation_is_serial_no_cache_exact_and_split_specific(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = self.image(root / 'scene.png')
            manifest = self.write_manifest(root, [
                self.case(image, split='calibration', ids=[]),
            ])
            output = root / 'results.json'
            report = {
                'schema_version': 1, 'mode': 'mission',
                'status': 'no_detections', 'rows': [], 'warnings': [],
            }
            with patch.object(fixtures, '_run_owned',
                              return_value=(4, json.dumps(report).encode(), b'')) as run:
                artifact = fixtures.evaluate_manifest(
                    manifest, output, split='calibration', workers=1,
                    builtin_manifest=None)

            command = run.call_args.args[0]
            self.assertIn('--no-cache', command)
            self.assertEqual(command[command.index('--mode') + 1], 'auto')
            self.assertEqual(command[command.index('--workers') + 1], '1')
            self.assertEqual(artifact['split'], 'calibration')
            self.assertEqual(artifact['passed'], 1)
            self.assertEqual(artifact['cases'][0]['actual_ids'], [])
            self.assertEqual(json.loads(output.read_text()), artifact)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                fixtures.evaluate_manifest(
                    manifest, output, split='calibration', builtin_manifest=None)

    def test_evaluation_requires_split_for_a_mixed_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            calibration_image = self.image(root / 'calibration.png', color='red')
            heldout_image = self.image(root / 'heldout.png', color='blue')
            calibration = self.case(
                calibration_image, split='calibration', ids=[],
                capture_id='calibration-capture')
            calibration['provenance'] = {
                field: 'unknown' for field in calibration['provenance']}
            heldout = self.case(
                heldout_image, ids=[], capture_id='heldout-capture')
            manifest = self.write_manifest(root, [calibration, heldout])
            with self.assertRaisesRegex(ValueError, 'Mixed manifests require --split'):
                fixtures.evaluate_manifest(
                    manifest, root / 'results.json', builtin_manifest=None)

    def test_evaluation_detects_file_change_after_scanner(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = self.image(root / 'scene.png')
            manifest = self.write_manifest(root, [self.case(image, ids=[])])
            output = root / 'results.json'
            report = {'schema_version': 1, 'mode': 'mission',
                      'status': 'no_detections', 'rows': [], 'warnings': []}

            def replace(*args, **kwargs):
                self.image(image, color='white')
                return 4, json.dumps(report).encode(), b''

            with patch.object(fixtures, '_run_owned', side_effect=replace):
                artifact = fixtures.evaluate_manifest(
                    manifest, output, split='heldout', builtin_manifest=None)
            self.assertEqual(artifact['failed'], 1)
            self.assertIn('changed during evaluation', artifact['cases'][0]['error'])

    def test_evaluation_rejects_manifest_change_during_run(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = self.image(root / 'scene.png')
            manifest = self.write_manifest(root, [self.case(image, ids=[])])
            output = root / 'results.json'
            report = {'schema_version': 1, 'mode': 'mission',
                      'status': 'no_detections', 'rows': [], 'warnings': []}

            def replace(*args, **kwargs):
                manifest.write_text(manifest.read_text() + ' ')
                return 4, json.dumps(report).encode(), b''

            with patch.object(fixtures, '_run_owned', side_effect=replace):
                with self.assertRaisesRegex(ValueError, 'changed during evaluation'):
                    fixtures.evaluate_manifest(
                        manifest, output, split='heldout', builtin_manifest=None)
            self.assertFalse(output.exists())

    def test_evaluation_rejects_scanner_error_and_exit_status_mismatch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = self.image(root / 'scene.png')
            manifest = self.write_manifest(root, [self.case(image, ids=[])])
            reports = (
                (4, {'schema_version': 1, 'status': 'error',
                     'error': 'recognition failed', 'rows': [], 'warnings': []},
                 'recognition failed'),
                (0, {'schema_version': 1, 'mode': 'mission',
                     'status': 'no_detections', 'rows': [], 'warnings': []},
                 'exit status'),
            )
            for index, (code, report, message) in enumerate(reports):
                with self.subTest(code=code, status=report['status']):
                    output = root / f'results-{index}.json'
                    with patch.object(
                            fixtures, '_run_owned',
                            return_value=(code, json.dumps(report).encode(), b'')):
                        artifact = fixtures.evaluate_manifest(
                            manifest, output, split='heldout', builtin_manifest=None)
                    self.assertEqual(artifact['failed'], 1)
                    self.assertRegex(artifact['cases'][0]['error'], message)

    def test_real_scanner_smoke_on_existing_panel_in_temporary_full_scene(self):
        fixture_root = Path(__file__).parent / 'fixtures'
        source = fixture_root / 'mission-panels' / '18n2fofu.png'
        legacy = json.loads((fixture_root / 'manifest.json').read_text())
        expected = next(item for item in legacy['datasets']
                        if item['id'] == 'mission-panels')['expected_ids'][source.name]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scene = Image.new('RGB', (5120, 2160))
            with Image.open(source) as panel:
                scene.paste(panel, (0, 0))
            image = root / 'scene.png'
            scene.save(image)
            case = self.case(image, split='calibration', ids=expected)
            case['dimensions'] = [5120, 2160]
            case['provenance']['resolution'] = [5120, 2160]
            case['provenance'] = {key: 'unknown' for key in case['provenance']}
            manifest = self.write_manifest(root, [case])

            artifact = fixtures.evaluate_manifest(
                manifest, root / 'results.json', split='calibration',
                workers=1, builtin_manifest=None, interpreter=sys.executable)

            self.assertEqual((artifact['passed'], artifact['failed']), (1, 0))


if __name__ == '__main__':
    unittest.main()
