"""Bounded, disposable storage for exact icon matches across scanner processes."""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock

from ..shared.bounded_json import loads_bounded_json


class RecognitionCache:
    """Callers provide a matcher/catalog fingerprint and cache only trusted results."""

    def __init__(self, directory, fingerprint, *, max_entries=512, max_entry_bytes=65536):
        self.path = Path(directory) / 'recognition.sqlite3'
        self.fingerprint = fingerprint
        self.max_entries = max(1, max_entries)
        self.max_entry_bytes = max(1, max_entry_bytes)
        self.lock = RLock()
        self.available = False
        self.hits = 0
        self.misses = 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection, connection:
                connection.execute('''CREATE TABLE IF NOT EXISTS entries (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    result TEXT NOT NULL)''')
            self.available = True
        except (OSError, sqlite3.Error):
            pass

    def _connect(self):
        return sqlite3.connect(self.path, timeout=.1)

    def key(self, input_bytes, context):
        """Context must include input shape, candidate IDs, mode and preprocessing."""
        metadata = json.dumps([self.fingerprint, context], sort_keys=True,
                              separators=(',', ':'), allow_nan=False).encode()
        digest = sha256(len(metadata).to_bytes(8, 'big') + metadata)
        digest.update(input_bytes)
        return digest.hexdigest()

    def info(self):
        with self.lock:
            return {'available': self.available, 'path': str(self.path),
                    'hits': self.hits, 'misses': self.misses}

    def get(self, key):
        with self.lock:
            result = self._get(key)
            if result is None:
                self.misses += 1
            else:
                self.hits += 1
            return result

    def _get(self, key):
        if not self.available:
            return None
        try:
            with self.lock, closing(self._connect()) as connection:
                row = connection.execute('SELECT result FROM entries WHERE key = ?', (key,)).fetchone()
            if row is None:
                return None
            result = loads_bounded_json(row[0], max_bytes=self.max_entry_bytes)
            return result if isinstance(result, dict) else None
        except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, RecursionError):
            return None

    def put(self, key, result):
        if not self.available or not isinstance(result, dict):
            return
        try:
            payload = json.dumps(result, separators=(',', ':'), allow_nan=False)
            if len(payload.encode()) > self.max_entry_bytes:
                return
            with self.lock, closing(self._connect()) as connection, connection:
                connection.execute('INSERT OR REPLACE INTO entries (key, result) VALUES (?, ?)',
                                   (key, payload))
                connection.execute('''DELETE FROM entries WHERE sequence NOT IN (
                    SELECT sequence FROM entries ORDER BY sequence DESC LIMIT ?)''',
                                   (self.max_entries,))
        except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
            pass


def scanner_cache(directory=None, *, root=None, matcher_paths=None):
    """Create the host scanner cache; unreadable fingerprint inputs disable it."""
    import cv2
    import numpy as np
    import PIL

    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    if directory is None:
        cache_home = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
        if not cache_home.is_absolute():
            cache_home = Path.home() / '.cache'
        directory = cache_home / 'net_jslay_helldivers_2' / 'recognition'
    if matcher_paths is None:
        matcher_paths = [root / 'automatic_stratagems' / 'scanner' / name for name in
                         ('stratagem_detection.py', 'icon_normalization.py',
                          'mission_references.py', 'colorless_icons.py', 'mission_layout.py',
                          'recognize/constants.py', 'recognize/tile.py')]
    paths = list(map(Path, matcher_paths)) + sorted((root / 'assets' / 'icons').glob('*.png'))
    paths += sorted((root / 'automatic_stratagems' / 'scanner' / 'references').glob('*.png'))
    paths += [root / 'assets' / 'data' / 'stratagems.json', root / 'locales' / 'en_US.json']
    fingerprint = sha256(json.dumps(['recognition-v1', cv2.__version__, np.__version__, PIL.__version__]).encode())
    try:
        for path in paths:
            name = str(path.relative_to(root) if path.is_relative_to(root) else path).encode()
            data = path.read_bytes()
            fingerprint.update(len(name).to_bytes(8, 'big') + name)
            fingerprint.update(len(data).to_bytes(8, 'big') + data)
    except OSError:
        return None
    return RecognitionCache(directory, fingerprint.hexdigest())
