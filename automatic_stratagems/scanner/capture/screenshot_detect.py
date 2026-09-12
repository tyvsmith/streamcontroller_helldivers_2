"""Finding and accepting the new screenshot file after a trigger runs."""

from concurrent.futures import CancelledError
from pathlib import Path
import stat
import time

from ...shared.fs import check_cancel as _check_cancel
from ...shared.fs import typed_file_stamp_with_ctime as _metadata_snapshot
from ...shared.fs import poll_deadline
from .screenshot_cleanup import _register_cleanup, _steam_thumbnail_snapshot
from .screenshot_files import (
    SUPPORTED_SUFFIXES, _cancellable_sleep, _check_deadline, _directory_snapshot,
    _scan_error, _stable_fingerprint)


WAIT_SECONDS = 5.0


def _source_kind(source):
    path = Path(source["path"])
    kind = source["kind"]
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if kind == "file" and source["trigger"] != "none":
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                raise _scan_error("Screenshot file must be a PNG or JPEG.")
            try:
                parent = path.parent.lstat()
            except OSError as error:
                raise _scan_error(f"Cannot inspect screenshot path: {error}", error)
            if not stat.S_ISDIR(parent.st_mode):
                raise _scan_error("Screenshot file parent is not a directory.")
            return "file"
        raise _scan_error("Screenshot source does not exist.")
    except OSError as error:
        raise _scan_error(f"Cannot inspect screenshot path: {error}", error)
    if stat.S_ISLNK(metadata.st_mode):
        raise _scan_error("Screenshot source must not be a symlink.")
    if kind == "path":
        if stat.S_ISDIR(metadata.st_mode):
            return "folder"
        if stat.S_ISREG(metadata.st_mode):
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                raise _scan_error("Screenshot file must be a PNG or JPEG.")
            return "file"
        raise _scan_error("Screenshot path must be a regular file or directory.")
    expected = stat.S_ISREG(metadata.st_mode) if kind == "file" else stat.S_ISDIR(
        metadata.st_mode)
    if not expected:
        raise _scan_error(f"Screenshot {kind} has the wrong file type.")
    if kind == "file" and path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise _scan_error("Screenshot file must be a PNG or JPEG.")
    return kind


def _read_triggered_candidate(path, selection, config, newly_created,
                              cancel_event, deadline):
    try:
        before = _metadata_snapshot(Path(path).lstat())
    except OSError as error:
        raise _scan_error("Screenshot changed before it was read.", error)
    from ..image_source import read_image_source
    image, metadata = read_image_source(
        "file", path, previous_fingerprint=None, allow_rescan=True,
        cancel_event=cancel_event, deadline=deadline)
    metadata["selection"] = selection
    try:
        identity = _metadata_snapshot(Path(path).lstat())
    except OSError as error:
        raise _scan_error("Screenshot changed after it was read.", error)
    if (identity != before or identity[3] != metadata["size_bytes"] or
            identity[4] != metadata["mtime_ns"]):
        raise _scan_error("Screenshot changed after it was read.")
    return image, metadata


def _trigger_snapshot(config, selection, cancel_event=None, deadline=None):
    source = Path(config["path"])
    directory = source if selection == "folder" else source.parent
    root_identity, entries = _directory_snapshot(
        directory, cancel_event, deadline)
    before_fingerprint = None
    if selection == "file" and source.name in entries:
        before_fingerprint = _stable_fingerprint(
            source, entries[source.name], cancel_event, deadline)
    steam_snapshot = (_steam_thumbnail_snapshot(
        directory, cancel_event, deadline)
        if config["delete_after_scan"] else None)
    return (directory, root_identity, entries, before_fingerprint,
            steam_snapshot)


def _wait_for_screenshot(config, selection, before, cancel_event=None,
                         deadline=None):
    source = Path(config["path"])
    (directory, root_identity, old_entries, old_fingerprint,
     steam_snapshot) = before
    end = poll_deadline(WAIT_SECONDS, deadline)
    last_error = None
    while time.monotonic() < end:
        _check_cancel(cancel_event)
        _check_deadline(deadline)
        current_root, entries = _directory_snapshot(
            directory, cancel_event, deadline)
        if current_root != root_identity:
            raise _scan_error("Screenshot directory changed during capture.")
        if selection == "folder":
            names = sorted(set(entries) - set(old_entries))
            if len(names) > 1:
                raise _scan_error(
                    "Multiple new screenshots; cannot identify this capture.")
            candidate = directory / names[0] if names else None
            newly_created = candidate is not None
        else:
            current = entries.get(source.name)
            previous = old_entries.get(source.name)
            candidate = source if current is not None and current != previous else None
            newly_created = source.name not in old_entries and candidate is not None
        if candidate is not None:
            try:
                image, metadata = _read_triggered_candidate(
                    candidate, selection, config, newly_created,
                    cancel_event, deadline)
                if (selection == "file" and old_fingerprint is not None and
                        metadata["fingerprint"] == old_fingerprint):
                    last_error = _scan_error(
                        "Triggered screenshot contents did not change.")
                else:
                    after_root, after_entries = _directory_snapshot(
                        directory, cancel_event, deadline)
                    if after_root != root_identity:
                        raise _scan_error("Screenshot directory changed during capture.")
                    if selection == "folder":
                        after_names = sorted(set(after_entries) - set(old_entries))
                        if len(after_names) > 1:
                            raise _scan_error(
                                "Multiple new screenshots; cannot identify this capture.")
                        if after_names != [candidate.name]:
                            raise _scan_error("New screenshot changed during capture.")
                    if after_entries.get(candidate.name) != _metadata_snapshot(
                            candidate.lstat()):
                        raise _scan_error("Screenshot changed during capture.")
                    _register_cleanup(config, metadata,
                                      after_entries[candidate.name], newly_created,
                                      steam_snapshot)
                    return image, metadata
            except (CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception as error:
                from ..game_capture import ScanError
                if not isinstance(error, ScanError):
                    raise
                if "Multiple new screenshots" in str(error):
                    raise
                last_error = error
        _cancellable_sleep(min(.05, max(0, end - time.monotonic())),
                           cancel_event, deadline)
    detail = f" ({last_error})" if last_error is not None else ""
    raise _scan_error(f"No complete new screenshot received within 5 seconds{detail}.")
