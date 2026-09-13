import json
from contextlib import closing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from automatic_stratagems.scanner.recognition_cache import RecognitionCache, scanner_cache


class RecognitionCacheTests(unittest.TestCase):
    def test_exact_input_context_and_fingerprint_control_reuse(self):
        with TemporaryDirectory() as directory:
            cache = RecognitionCache(directory, 'catalog-v1')
            key = cache.key(b'pixels', {'mode': 'mission', 'keys': ['A', 'B']})
            self.assertIsNone(cache.get(key))
            result = {'id': 'A', 'scores': [[.99, 'A'], [.5, 'B']]}
            cache.put(key, result)
            reopened = RecognitionCache(directory, 'catalog-v1')
            self.assertEqual(reopened.get(key), result)
            self.assertEqual(key, reopened.key(b'pixels', {'keys': ['A', 'B'], 'mode': 'mission'}))
            for other in (cache.key(b'pixelS', {'mode': 'mission', 'keys': ['A', 'B']}),
                          cache.key(b'pixels', {'mode': 'selection', 'keys': ['A', 'B']}),
                          cache.key(b'pixels', {'mode': 'mission', 'keys': ['A']}),
                          RecognitionCache(directory, 'catalog-v2').key(
                              b'pixels', {'mode': 'mission', 'keys': ['A', 'B']})):
                self.assertNotEqual(key, other)
                self.assertIsNone(cache.get(other))

    def test_hit_and_miss_counts(self):
        with TemporaryDirectory() as directory:
            cache = RecognitionCache(directory, 'v1')
            key = cache.key(b'pixels', {})
            cache.get(key)
            cache.put(key, {'id': 'A'})
            cache.get(key)
            self.assertEqual(cache.info(), {'available': True,
                             'path': str(Path(directory) / 'recognition.sqlite3'),
                             'hits': 1, 'misses': 1})

    def test_factory_uses_xdg_and_invalidates_changed_sources_and_assets(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'repo'
            paths = ('automatic_stratagems/scanner/stratagem_detection.py', 'automatic_stratagems/scanner/icon_normalization.py',
                     'automatic_stratagems/scanner/mission_references.py', 'automatic_stratagems/scanner/colorless_icons.py',
                     'automatic_stratagems/scanner/mission_layout.py', 'automatic_stratagems/scanner/recognize/constants.py',
                     'automatic_stratagems/scanner/recognize/tile.py',
                     'automatic_stratagems/scanner/recognize/match_result.py',
                     'automatic_stratagems/scanner/references/A.png',
                     'assets/icons/A.png', 'assets/data/stratagems.json', 'locales/en_US.json')
            for name in paths:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'original')
            with patch.dict('os.environ', {'XDG_CACHE_HOME': directory}):
                cache = scanner_cache(root=root)
            self.assertEqual(cache.path, Path(directory) / 'net_jslay_helldivers_2' /
                             'recognition' / 'recognition.sqlite3')
            fingerprint = cache.fingerprint
            for name in paths:
                path = root / name
                path.write_bytes(b'changed')
                self.assertNotEqual(scanner_cache(directory, root=root).fingerprint, fingerprint)
                path.write_bytes(b'original')
            self.assertEqual(scanner_cache(directory, root=root).fingerprint, fingerprint)
            with patch('cv2.__version__', 'new-version'):
                self.assertNotEqual(scanner_cache(directory, root=root).fingerprint, fingerprint)
            with patch('PIL.__version__', 'new-version'):
                self.assertNotEqual(scanner_cache(directory, root=root).fingerprint, fingerprint)
            self.assertIsNotNone(scanner_cache(directory, root=root,
                                               matcher_paths=[root / paths[0]]))
            (root / paths[0]).unlink()
            self.assertIsNone(scanner_cache(directory, root=root))

    def test_oldest_written_entries_are_bounded(self):
        with TemporaryDirectory() as directory:
            cache = RecognitionCache(directory, 'v1', max_entries=2)
            keys = [cache.key(bytes([index]), {}) for index in range(3)]
            for index, key in enumerate(keys):
                cache.put(key, {'id': str(index)})
            self.assertIsNone(cache.get(keys[0]))
            self.assertEqual(cache.get(keys[1]), {'id': '1'})
            self.assertEqual(cache.get(keys[2]), {'id': '2'})

    def test_corrupt_database_and_unwritable_location_are_misses(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'recognition.sqlite3'
            path.write_bytes(b'broken sqlite database')
            cache = RecognitionCache(directory, 'v1')
            key = cache.key(b'pixels', {})
            cache.put(key, {'id': 'A'})
            self.assertIsNone(cache.get(key))
            self.assertEqual(path.read_bytes(), b'broken sqlite database')
            cache = RecognitionCache(path / 'child', 'v1')
            cache.put(key, {'id': 'A'})
            self.assertIsNone(cache.get(key))

    def test_corrupt_payload_and_large_entries_are_misses(self):
        with TemporaryDirectory() as directory:
            cache = RecognitionCache(directory, 'v1', max_entry_bytes=64)
            key = cache.key(b'pixels', {})
            cache.put(key, {'id': 'A'})
            with closing(sqlite3.connect(Path(directory) / 'recognition.sqlite3')) as connection, connection:
                connection.execute('UPDATE entries SET result = ?', ('broken json',))
            self.assertIsNone(cache.get(key))
            cache.put(key, {'id': 'x' * 100})
            self.assertIsNone(cache.get(key))

    def test_recursively_nested_payload_is_a_miss(self):
        with TemporaryDirectory() as directory:
            cache = RecognitionCache(directory, 'v1')
            key = cache.key(b'pixels', {})
            payload = '{"id":"A","extra":' + '[' * 1200 + '0' + ']' * 1200 + '}'
            with closing(sqlite3.connect(Path(directory) / 'recognition.sqlite3')) as connection, connection:
                connection.execute('INSERT INTO entries (key, result) VALUES (?, ?)',
                                   (key, payload))

            self.assertIsNone(cache.get(key))

    def test_failed_eviction_rolls_back_the_whole_write(self):
        with TemporaryDirectory() as directory:
            cache = RecognitionCache(directory, 'v1', max_entries=1)
            old_key, new_key = cache.key(b'old', {}), cache.key(b'new', {})
            cache.put(old_key, {'id': 'A'})
            with closing(sqlite3.connect(Path(directory) / 'recognition.sqlite3')) as connection, connection:
                connection.execute("""CREATE TRIGGER reject_eviction BEFORE DELETE ON entries
                    BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
            cache.put(new_key, {'id': 'B'})
            self.assertEqual(cache.get(old_key), {'id': 'A'})
            self.assertIsNone(cache.get(new_key))

    def test_concurrent_writes_leave_complete_results_within_limit(self):
        with TemporaryDirectory() as directory:
            caches = [RecognitionCache(directory, 'v1', max_entries=8) for _ in range(2)]
            def write(index):
                cache = caches[index % 2]
                key = cache.key(str(index).encode(), {})
                cache.put(key, {'id': str(index)})
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(write, range(24)))
            with closing(sqlite3.connect(Path(directory) / 'recognition.sqlite3')) as connection, connection:
                rows = connection.execute('SELECT result FROM entries').fetchall()
            self.assertEqual(len(rows), 8)
            self.assertTrue(all('id' in json.loads(row[0]) for row in rows))


if __name__ == '__main__':
    unittest.main()
