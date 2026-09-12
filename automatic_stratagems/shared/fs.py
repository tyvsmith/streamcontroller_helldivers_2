"""Durable writes and bounded reads shared by every execution context.

Callers keep their own open flags, identity checks and error types; these
helpers own only the loops that must behave identically everywhere.
"""

import json
import os
import uuid


STREAM_CHUNK_BYTES = 64 * 1024


def write_all(descriptor, data, message):
    """Write every byte, raising OSError(message) when a write makes no progress."""
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(message)
        view = view[written:]


def fsync_directory(path):
    """Flush a directory entry change such as a rename or new file."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def canonical_json(value):
    """Encode JSON with sorted keys, compact separators and a newline."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def atomic_json(path, value, *, max_bytes, too_large, short_write_message):
    """Replace one small canonical JSON record atomically and durably.

    too_large() builds the caller's error for a record over max_bytes; nothing
    touches the disk in that case.
    """
    data = canonical_json(value)
    if len(data) > max_bytes:
        raise too_large()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            write_all(descriptor, data, short_write_message)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_bounded_stream(stream, limit, name, output, overflow):
    """Drain a pipe into output, recording name in overflow past limit bytes."""
    try:
        while True:
            chunk = stream.read(STREAM_CHUNK_BYTES)
            if not chunk:
                return
            available = max(0, limit - len(output))
            output.extend(chunk[:available])
            if len(chunk) > available:
                overflow.append(name)
                return
    finally:
        stream.close()
