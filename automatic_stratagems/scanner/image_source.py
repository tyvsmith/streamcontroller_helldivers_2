"""Bounded, stable PNG/JPEG loading for configured file and folder sources."""

import hashlib
import io
import os
from pathlib import Path
import stat
import time

from PIL import Image

from .errors import ScanError
from .image_decode import (MAX_ENCODED_IMAGE_BYTES, MAX_IMAGE_DIMENSION,
                           MAX_IMAGE_PIXELS)
from ..shared.fs import check_cancel as _check_cancel
from ..shared.fs import typed_file_stamp as _snapshot
from ..shared.fs import check_deadline, poll_deadline, read_capped


MAX_DIRECTORY_ENTRIES = 4096
MAX_STABILITY_WAIT_SECONDS = 2.0
STABILITY_INTERVAL_SECONDS = 0.05
SUPPORTED_SUFFIXES = frozenset((".png", ".jpg", ".jpeg"))
DOCUMENT_PORTAL_ALIAS = Path("/run/user") / str(os.getuid()) / "doc"
DOCUMENT_PORTAL_TARGET = "../../flatpak/doc"
FLATPAK_INFO = Path("/.flatpak-info")
_UNSET = object()


def _check_deadline(deadline):
    check_deadline(deadline, lambda: ScanError("Scanner work deadline exhausted."))


def _absolute_path(path):
    try:
        return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    except (TypeError, ValueError, OSError) as error:
        raise ScanError("Image source path is invalid.") from error


def _reject_symlink_components(path, expected_portal_alias=_UNSET):
    current = Path(path.anchor)
    portal_alias = None
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise ScanError(f"Cannot inspect image source path: {error}") from error
        if stat.S_ISLNK(metadata.st_mode):
            try:
                target = os.readlink(current)
            except OSError as error:
                raise ScanError(
                    f"Cannot inspect image source symlink: {error}") from error
            if (not FLATPAK_INFO.exists() or current != DOCUMENT_PORTAL_ALIAS or
                    target != DOCUMENT_PORTAL_TARGET):
                raise ScanError("Image source path must not contain a symlink.")
            portal_alias = (_snapshot(metadata), target)
    if (expected_portal_alias is not _UNSET and
            portal_alias != expected_portal_alias):
        raise ScanError("Image source path changed while it was being read.")
    return portal_alias


def _lstat(path, description):
    try:
        return path.lstat()
    except OSError as error:
        raise ScanError(f"Cannot inspect {description}: {error}") from error


def _file_candidate(path):
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ScanError("Image source must be a PNG or JPEG file.")
    metadata = _lstat(path, "image source")
    return path, metadata


def _folder_identity(path):
    metadata = _lstat(path, "image source folder")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ScanError("Image source folder is not a directory.")
    return _snapshot(metadata)[:3]


def _folder_candidate(path, expected_folder, cancel_event=None, deadline=None):
    _check_cancel(cancel_event)
    _check_deadline(deadline)
    if _folder_identity(path) != expected_folder:
        raise ScanError("Image source folder changed while it was being read.")
    candidates = []
    try:
        with os.scandir(path) as entries:
            for count, entry in enumerate(entries, 1):
                _check_cancel(cancel_event)
                _check_deadline(deadline)
                if count > MAX_DIRECTORY_ENTRIES:
                    raise ScanError("Image source folder contains too many entries.")
                if Path(entry.name).suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise ScanError(
                        f"Cannot inspect image source entry: {error}") from error
                candidates.append((Path(entry.path), metadata))
    except ScanError:
        raise
    except OSError as error:
        raise ScanError(f"Cannot read image source folder: {error}") from error
    if _folder_identity(path) != expected_folder:
        raise ScanError("Image source folder changed while it was being read.")
    if not candidates:
        raise ScanError("Image source folder has no PNG or JPEG images.")
    newest_mtime = max(metadata.st_mtime_ns for _path, metadata in candidates)
    newest = [(candidate, metadata) for candidate, metadata in candidates
              if metadata.st_mtime_ns == newest_mtime]
    if len(newest) != 1:
        raise ScanError("Image source folder has multiple newest images.")
    _check_cancel(cancel_event)
    _check_deadline(deadline)
    return newest[0]


def _selection(kind, source, folder_identity, cancel_event, deadline):
    if kind == "file":
        return _file_candidate(source)
    return _folder_candidate(
        source, folder_identity, cancel_event=cancel_event, deadline=deadline)


def _stable_selection(kind, source, folder_identity, cancel_event, deadline):
    stability_deadline = poll_deadline(MAX_STABILITY_WAIT_SECONDS, deadline)
    candidate, metadata = _selection(
        kind, source, folder_identity, cancel_event, deadline)
    signature = (candidate, _snapshot(metadata))
    while True:
        _check_cancel(cancel_event)
        now = time.monotonic()
        if now >= stability_deadline:
            if deadline is not None and now >= deadline:
                _check_deadline(deadline)
            raise ScanError("Image source did not become stable within 2 seconds.")
        time.sleep(min(STABILITY_INTERVAL_SECONDS, stability_deadline - now))
        _check_cancel(cancel_event)
        _check_deadline(deadline)
        after, after_metadata = _selection(
            kind, source, folder_identity, cancel_event, deadline)
        after_signature = (after, _snapshot(after_metadata))
        if after_signature == signature:
            return after, after_metadata
        candidate, metadata = after, after_metadata
        signature = after_signature


def _read_bytes(path, expected, cancel_event, deadline):
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ScanError("Image source is not a regular file.")
        if _snapshot(opened) != _snapshot(expected):
            raise ScanError("Image source changed while it was being read.")

        def guard():
            _check_cancel(cancel_event)
            _check_deadline(deadline)

        encoded = read_capped(descriptor, MAX_ENCODED_IMAGE_BYTES, 64 * 1024,
                              before_read=guard)
        if len(encoded) > MAX_ENCODED_IMAGE_BYTES:
            raise ScanError("Image source encoded image is too large.")
        if _snapshot(os.fstat(descriptor)) != _snapshot(expected):
            raise ScanError("Image source changed while it was being read.")
        return encoded
    except ScanError:
        raise
    except OSError as error:
        raise ScanError(f"Cannot read image source: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _decode_image(encoded):
    try:
        with Image.open(io.BytesIO(encoded)) as image:
            if image.format not in ("PNG", "JPEG"):
                raise ScanError("Image source must contain PNG or JPEG data.")
            width, height = image.size
            if (width <= 0 or height <= 0 or
                    max(width, height) > MAX_IMAGE_DIMENSION or
                    width * height > MAX_IMAGE_PIXELS):
                raise ScanError("Image source image dimensions are unsupported.")
            image.load()
            return image.convert("RGB")
    except ScanError:
        raise
    except (Image.DecompressionBombError, OSError, ValueError) as error:
        raise ScanError("Image source returned an unreadable image.") from error


def _validate_after(kind, source, selected, expected, folder_identity,
                    portal_alias, cancel_event, deadline):
    _reject_symlink_components(
        selected, expected_portal_alias=portal_alias)
    if _snapshot(_lstat(selected, "image source")) != _snapshot(expected):
        raise ScanError("Image source changed while it was being read.")
    if kind == "folder":
        after, after_metadata = _folder_candidate(
            source, folder_identity, cancel_event=cancel_event,
            deadline=deadline)
        if after != selected or _snapshot(after_metadata) != _snapshot(expected):
            raise ScanError("Image source folder selection changed while it was being read.")


def _validated_fingerprint(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScanError("Previous image fingerprint is invalid.")
    if (len(value) != 64 or
            any(character not in "0123456789abcdefABCDEF" for character in value)):
        raise ScanError("Previous image fingerprint is invalid.")
    return value.lower()


def read_image_source(kind, path, *, previous_fingerprint=None,
                      allow_rescan=False, cancel_event=None, deadline=None):
    """Read one stable configured image without changing its source."""
    if kind not in ("file", "folder", "path"):
        raise ScanError("Image source kind must be file, folder or path.")
    if type(allow_rescan) is not bool:
        raise ScanError("Image source allow_rescan must be a boolean.")
    previous_fingerprint = _validated_fingerprint(previous_fingerprint)
    _check_cancel(cancel_event)
    _check_deadline(deadline)
    source = _absolute_path(path)
    portal_alias = _reject_symlink_components(source)
    if kind == "path":
        kind = "folder" if stat.S_ISDIR(_lstat(source, "image source").st_mode) else "file"
    folder_identity = _folder_identity(source) if kind == "folder" else None
    selected, metadata = _stable_selection(
        kind, source, folder_identity, cancel_event, deadline)
    if not stat.S_ISREG(metadata.st_mode):
        raise ScanError("Image source is not a regular file.")
    if metadata.st_size > MAX_ENCODED_IMAGE_BYTES:
        raise ScanError("Image source encoded image is too large.")
    encoded = _read_bytes(selected, metadata, cancel_event, deadline)
    _check_cancel(cancel_event)
    _check_deadline(deadline)
    image = _decode_image(encoded)
    _check_cancel(cancel_event)
    _check_deadline(deadline)
    _validate_after(kind, source, selected, metadata, folder_identity,
                    portal_alias, cancel_event, deadline)
    fingerprint = hashlib.sha256(encoded).hexdigest()
    _check_cancel(cancel_event)
    _check_deadline(deadline)
    if previous_fingerprint == fingerprint and not allow_rescan:
        raise ScanError(
            "Image source has not changed; pass --allow-image-rescan to scan it again.")
    return image, {
        "kind": "file",
        "selection": kind,
        "path": str(selected),
        "mtime_ns": metadata.st_mtime_ns,
        "size_bytes": metadata.st_size,
        "fingerprint": fingerprint,
    }
