"""Delete a recognized screenshot, and its Steam thumbnail, only when owned."""

from concurrent.futures import CancelledError
import os
from pathlib import Path
import secrets
import stat
import threading
import time

from ...shared.fs import check_cancel as _check_cancel
from ...shared.fs import typed_file_stamp_with_ctime as _metadata_snapshot
from .screenshot_files import (
    _cancellable_sleep, _directory_snapshot, _fingerprint_descriptor,
    _stable_fingerprint)
from .screenshot_source import (
    _steam_screenshot_directory, is_steam_managed, resolve_source)


THUMBNAIL_WAIT_SECONDS = 1.0
_CLEANUP_LOCK = threading.Lock()
_CLEANUP_RECORDS = {}


def _steam_thumbnail_snapshot(directory, cancel_event=None, deadline=None):
    """Snapshot the standard thumbnail directory before the trigger runs."""
    directory = Path(directory)
    if _steam_screenshot_directory(directory / "capture.jpg") != directory:
        return None
    thumbnails = directory / "thumbnails"
    try:
        identity, entries = _directory_snapshot(
            thumbnails, cancel_event, deadline)
    except (CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return {"eligible": False,
                "reason": "Steam thumbnail directory could not be verified."}
    return {
        "eligible": True, "directory": str(directory),
        "thumbnail_directory": str(thumbnails),
        "thumbnail_parent_identity": identity,
        "thumbnail_entries": frozenset(entries),
    }


def _register_cleanup(config, metadata, identity, newly_created,
                      steam_snapshot=None):
    if not config["delete_after_scan"]:
        return
    if not newly_created:
        metadata["cleanup_skipped"] = (
            "Pre-existing screenshots are never deleted; use a dedicated folder.")
        return
    path = Path(metadata["path"])
    steam_directory = _steam_screenshot_directory(path)
    thumbnail = None
    if steam_directory is not None:
        if (not isinstance(steam_snapshot, dict) or
                not steam_snapshot.get("eligible") or
                steam_snapshot.get("directory") != str(steam_directory)):
            metadata["cleanup_skipped"] = (
                (steam_snapshot or {}).get("reason") or
                "Steam thumbnail state could not be verified before capture.")
            return
        if path.name in steam_snapshot["thumbnail_entries"]:
            metadata["cleanup_skipped"] = (
                "The matching Steam thumbnail existed before capture and was retained.")
            return
        thumbnail = {
            "path": str(Path(steam_snapshot["thumbnail_directory"]) / path.name),
            "parent_identity": steam_snapshot["thumbnail_parent_identity"],
        }
    elif is_steam_managed(path):
        metadata["cleanup_skipped"] = (
            "Only standard Steam screenshot pairs can be deleted safely.")
        return
    token = secrets.token_hex(32)
    record = {
        "source": (config["kind"], config["path"], config["trigger"]),
        "path": metadata["path"], "fingerprint": metadata["fingerprint"],
        "mtime_ns": metadata["mtime_ns"], "size_bytes": metadata["size_bytes"],
        # Renaming the claimed file may change ctime; inode, mode, size and
        # mtime remain sufficient to bind the cleanup receipt.
        "identity": identity[:5],
        "parent_identity": _metadata_snapshot(path.parent.lstat()),
        "thumbnail": thumbnail,
    }
    with _CLEANUP_LOCK:
        _CLEANUP_RECORDS[token] = record
    metadata["cleanup_token"] = token


def _wait_for_steam_thumbnail(record, cancel_event=None):
    """Return a stable new thumbnail receipt, or None after a bounded wait."""
    thumbnail = record.get("thumbnail")
    if not isinstance(thumbnail, dict):
        return None
    path = Path(thumbnail["path"])
    end = time.monotonic() + THUMBNAIL_WAIT_SECONDS
    stable = None
    while time.monotonic() < end:
        _check_cancel(cancel_event)
        try:
            parent, entries = _directory_snapshot(path.parent, cancel_event)
            if parent != thumbnail["parent_identity"]:
                return None
            identity = entries.get(path.name)
            if identity is not None and not stat.S_ISREG(identity[2]):
                return None
            if identity is not None and identity == stable:
                fingerprint = _stable_fingerprint(
                    path, identity, cancel_event=cancel_event)
                after_parent, after_entries = _directory_snapshot(
                    path.parent, cancel_event)
                if (after_parent == thumbnail["parent_identity"] and
                        after_entries.get(path.name) == identity):
                    return {"path": str(path), "identity": identity[:5],
                            "fingerprint": fingerprint,
                            "parent_identity": thumbnail["parent_identity"]}
            stable = identity
        except (CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            stable = None
        _cancellable_sleep(
            min(.05, max(0, end - time.monotonic())), cancel_event)
    return None


def _delete_cleanup_pairs(token, pairs, cancel_event=None):
    """Claim, verify and delete one main file and its optional thumbnail."""
    parent_descriptors = []
    quarantine_descriptor = None
    quarantine_created = False
    quarantine_name = ".hd2-screenshot-cleanup-" + token
    claims = []

    def restore():
        if quarantine_descriptor is None:
            if quarantine_created and parent_descriptors:
                try:
                    os.rmdir(quarantine_name, dir_fd=parent_descriptors[0])
                except OSError:
                    pass
            return
        for claim in reversed(claims):
            try:
                os.link(claim["held"], claim["path"].name,
                        src_dir_fd=quarantine_descriptor,
                        dst_dir_fd=claim["parent"], follow_symlinks=False)
                os.unlink(claim["held"], dir_fd=quarantine_descriptor)
            except OSError:
                pass
        if parent_descriptors:
            try:
                os.rmdir(quarantine_name, dir_fd=parent_descriptors[0])
            except OSError:
                pass

    try:
        for pair in pairs:
            path = pair["path"]
            descriptor = os.open(
                path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
            parent_descriptors.append(descriptor)
            expected = pair["parent_identity"]
            if _metadata_snapshot(os.fstat(descriptor))[:len(expected)] != expected:
                return False
            pair["parent"] = descriptor
        os.mkdir(quarantine_name, mode=0o700, dir_fd=parent_descriptors[0])
        quarantine_created = True
        quarantine_descriptor = os.open(
            quarantine_name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
            getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptors[0])
        for index, pair in enumerate(pairs):
            held = ("main" if index == 0 else "thumbnail") + pair["path"].suffix.lower()
            os.rename(pair["path"].name, held,
                      src_dir_fd=pair["parent"],
                      dst_dir_fd=quarantine_descriptor)
            claims.append({**pair, "held": held})
        for claim in claims:
            descriptor = os.open(
                claim["held"], os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) |
                getattr(os, "O_NOFOLLOW", 0), dir_fd=quarantine_descriptor)
            try:
                fingerprint = _fingerprint_descriptor(
                    descriptor, claim["identity"], cancel_event)
            finally:
                os.close(descriptor)
            if (fingerprint != claim["fingerprint"] or
                    _metadata_snapshot(os.stat(
                        claim["held"], dir_fd=quarantine_descriptor,
                        follow_symlinks=False))[:5] != claim["identity"]):
                restore()
                return False
        _check_cancel(cancel_event)
        for claim in claims:
            os.unlink(claim["held"], dir_fd=quarantine_descriptor)
        try:
            os.rmdir(quarantine_name, dir_fd=parent_descriptors[0])
        except OSError:
            pass
        return True
    except (CancelledError, KeyboardInterrupt, SystemExit):
        restore()
        raise
    except Exception:
        restore()
        return False
    finally:
        if quarantine_descriptor is not None:
            os.close(quarantine_descriptor)
        for descriptor in parent_descriptors:
            os.close(descriptor)


def cleanup_screenshot(config, metadata, cancel_event=None):
    """Delete unchanged files proven to have been created by this process."""
    try:
        source = resolve_source(config)
    except Exception:
        return False
    if (not source["delete_after_scan"] or source["trigger"] == "none" or
            not isinstance(metadata, dict)):
        return False
    token = metadata.get("cleanup_token")
    if not isinstance(token, str) or len(token) != 64:
        return False
    with _CLEANUP_LOCK:
        record = _CLEANUP_RECORDS.pop(token, None)
    if record is None or (source["kind"], source["path"], source["trigger"]) != record["source"]:
        return False
    _check_cancel(cancel_event)
    if any(metadata.get(key) != record[key] for key in (
            "path", "fingerprint", "mtime_ns", "size_bytes")):
        return False
    path = Path(record["path"])
    thumbnail = None
    if record.get("thumbnail") is not None:
        thumbnail = _wait_for_steam_thumbnail(record, cancel_event)
        if thumbnail is None:
            metadata["cleanup_skipped"] = (
                "Steam thumbnail did not become safely deletable; screenshot retained.")
            return False
    pairs = [{
        "path": path, "identity": record["identity"],
        "fingerprint": record["fingerprint"],
        "parent_identity": record["parent_identity"],
    }]
    if thumbnail is not None:
        pairs.append({**thumbnail, "path": Path(thumbnail["path"])})
    if not _delete_cleanup_pairs(token, pairs, cancel_event):
        metadata["cleanup_skipped"] = (
            "Screenshot cleanup ownership changed; files were retained.")
        return False
    metadata.pop("cleanup_token", None)
    metadata.pop("cleanup_skipped", None)
    return True
