"""Bounded, identity-checked screenshot file reads and the scan guards they share."""

from concurrent.futures import CancelledError
import hashlib
import os
from pathlib import Path
import stat
import time

from ...shared.fs import check_cancel as _check_cancel
from ...shared.fs import typed_file_stamp_with_ctime as _metadata_snapshot
from ...shared.fs import check_deadline, iter_capped_chunks


MAX_DIRECTORY_ENTRIES = 4096
SUPPORTED_SUFFIXES = frozenset((".png", ".jpg", ".jpeg"))


def _scan_error(message, cause=None):
    from ..errors import ScanError
    error = ScanError(message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _check_deadline(deadline):
    check_deadline(deadline, lambda: _scan_error("Scanner work deadline exhausted."))


def _cancellable_sleep(seconds, cancel_event=None, deadline=None):
    end = time.monotonic() + seconds
    while True:
        _check_cancel(cancel_event)
        _check_deadline(deadline)
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.01, remaining))


def _directory_snapshot(directory, cancel_event=None, deadline=None):
    _check_cancel(cancel_event)
    _check_deadline(deadline)
    try:
        root = directory.lstat()
        if not stat.S_ISDIR(root.st_mode):
            raise _scan_error("Screenshot directory is not a directory.")
        entries = {}
        with os.scandir(directory) as iterator:
            for count, entry in enumerate(iterator, 1):
                _check_cancel(cancel_event)
                _check_deadline(deadline)
                if count > MAX_DIRECTORY_ENTRIES:
                    raise _scan_error(
                        "Screenshot directory contains too many entries.")
                if Path(entry.name).suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                entries[entry.name] = _metadata_snapshot(
                    entry.stat(follow_symlinks=False))
        if _metadata_snapshot(directory.lstat())[:3] != _metadata_snapshot(root)[:3]:
            raise _scan_error("Screenshot directory changed during capture.")
        _check_cancel(cancel_event)
        _check_deadline(deadline)
        return _metadata_snapshot(root)[:3], entries
    except Exception as error:
        from ..errors import ScanError
        if isinstance(error, ScanError):
            raise
        if isinstance(error, OSError):
            raise _scan_error(f"Cannot inspect screenshot directory: {error}", error)
        raise


def _stable_fingerprint(path, expected, cancel_event=None, deadline=None):
    """Hash one exact regular file without requiring it to decode as an image."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            digest = _fingerprint_descriptor(
                descriptor, expected, cancel_event, deadline)
        finally:
            os.close(descriptor)
        if _metadata_snapshot(Path(path).lstat()) != expected:
            raise _scan_error("Existing screenshot changed before capture.")
        return digest
    except (CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        from ..errors import ScanError
        if isinstance(error, ScanError):
            raise
        raise _scan_error(f"Cannot snapshot existing screenshot: {error}", error)


def _fingerprint_descriptor(descriptor, expected, cancel_event=None,
                            deadline=None):
    from .command import MAX_ENCODED_IMAGE_BYTES
    before = os.fstat(descriptor)
    if (_metadata_snapshot(before)[:len(expected)] != expected or
            not stat.S_ISREG(before.st_mode) or
            before.st_size > MAX_ENCODED_IMAGE_BYTES):
        raise _scan_error("Screenshot identity changed.")

    def guard():
        _check_cancel(cancel_event)
        _check_deadline(deadline)

    digest = hashlib.sha256()
    read_size = 0
    for chunk in iter_capped_chunks(descriptor, before.st_size, 1024 * 1024,
                                    before_read=guard):
        digest.update(chunk)
        read_size += len(chunk)
    if read_size != before.st_size:
        raise _scan_error("Screenshot identity changed.")
    if _metadata_snapshot(os.fstat(descriptor))[:len(expected)] != expected:
        raise _scan_error("Screenshot identity changed.")
    return digest.hexdigest()
