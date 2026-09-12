"""Durable writes and bounded reads shared by every execution context.

Callers keep their own open flags, identity checks and error types; these
helpers own only the loops that must behave identically everywhere.
"""

from concurrent.futures import CancelledError
import json
import os
import stat
import time
import uuid


STREAM_CHUNK_BYTES = 64 * 1024


def check_cancel(cancel_event):
    """Raise CancelledError once the caller's cancel event is set."""
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError()


def check_deadline(deadline, expired):
    """Raise expired() once the monotonic work deadline has passed."""
    if deadline is not None and time.monotonic() >= deadline:
        raise expired()


def poll_deadline(seconds, deadline):
    """End a bounded wait after seconds, or at the work deadline if sooner."""
    end = time.monotonic() + seconds
    if deadline is not None:
        end = min(end, deadline)
    return end


def file_stamp(metadata):
    """Device, inode, size and mtime of one stat result."""
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns)


def typed_file_stamp(metadata):
    """file_stamp plus the file type, placed before the size."""
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode),
            metadata.st_size, metadata.st_mtime_ns)


def typed_file_stamp_with_ctime(metadata):
    """typed_file_stamp plus ctime, which chmod, chown and link also change."""
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode),
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def wait_for_stable_stamp(probe, end, *, interval, cancel_event=None, accept=None):
    """Poll until a ready stamp repeats, or return None once end passes.

    Each round checks cancellation, then calls probe(). probe returns None
    when it observed nothing, leaving remembered stamps alone, or
    (key, stamp, ready). Every observed stamp is remembered for its key; a
    ready stamp equal to the one remembered for its key is stable, and
    accept(key, stamp) turns it into the result (the stamp by default). An
    accept result of None keeps polling. Probe and accept errors propagate,
    so each caller decides which failures mean "not yet".
    """
    remembered = {}
    while time.monotonic() < end:
        check_cancel(cancel_event)
        observation = probe()
        if observation is not None:
            key, stamp, ready = observation
            if ready and remembered.get(key) == stamp:
                result = stamp if accept is None else accept(key, stamp)
                if result is not None:
                    return result
            remembered[key] = stamp
        remaining = end - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))
    return None


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
