"""Read local JSON through a bounded regular-file descriptor."""

import errno
import json
import os
from pathlib import Path
import stat


DEFAULT_MAX_DEPTH = 64
READ_CHUNK_BYTES = 64 * 1024


def loads_bounded_json(value, *, max_bytes, max_depth=DEFAULT_MAX_DEPTH):
    """Decode bounded UTF-8 JSON and reject deeply nested containers."""
    if not isinstance(value, (str, bytes)):
        raise ValueError('Invalid JSON document')
    if len(value) > max_bytes:
        raise ValueError('JSON document is too large')
    try:
        encoded = value.encode('utf-8') if isinstance(value, str) else value
    except UnicodeEncodeError as error:
        raise ValueError('Invalid JSON document') from error
    if len(encoded) > max_bytes:
        raise ValueError('JSON document is too large')
    try:
        data = json.loads(encoded.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid JSON document') from error

    stack = [(data, 0)]
    while stack:
        value, depth = stack.pop()
        if not isinstance(value, (dict, list)):
            continue
        if depth >= max_depth:
            raise ValueError('JSON document is too deeply nested')
        stack.extend((item, depth + 1) for item in
                     (value.values() if isinstance(value, dict) else value))
    return data


def read_bounded_json(path, *, max_bytes, max_depth=DEFAULT_MAX_DEPTH):
    """Open one non-symlink regular file and read no more than the given cap."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(Path(path), flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError('JSON path must be a regular file') from error
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('JSON path must be a regular file')
        if metadata.st_size > max_bytes:
            raise ValueError('JSON document is too large')
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b''.join(chunks)
        if len(encoded) > max_bytes:
            raise ValueError('JSON document is too large')
    finally:
        os.close(descriptor)
    return loads_bounded_json(encoded, max_bytes=max_bytes, max_depth=max_depth)
