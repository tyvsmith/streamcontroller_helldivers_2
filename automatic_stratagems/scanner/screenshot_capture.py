"""Configured screenshot acquisition without live-window capture."""

from concurrent.futures import CancelledError
import hashlib
import os
from pathlib import Path
import secrets
import stat
import threading
import time

from ..shared.fs import check_cancel as _check_cancel
from ..shared.fs import typed_file_stamp_with_ctime as _metadata_snapshot
from ..shared.fs import check_deadline, iter_capped_chunks, poll_deadline


MAX_CONFIG_STRING = 8192
MAX_DIRECTORY_ENTRIES = 4096
MAX_CAPTURE_OUTPUT_BYTES = 16 * 1024
SCREENSHOT_APP_ID = "553850"
SUPPORTED_SUFFIXES = frozenset((".png", ".jpg", ".jpeg"))
WAIT_SECONDS = 5.0
THUMBNAIL_WAIT_SECONDS = 1.0
_CLEANUP_LOCK = threading.Lock()
_CLEANUP_RECORDS = {}


def _scan_error(message, cause=None):
    from .game_capture import ScanError
    error = ScanError(message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _check_deadline(deadline):
    check_deadline(deadline, lambda: _scan_error("Scanner work deadline exhausted."))


def _absolute(value, name):
    if (not isinstance(value, str) or len(value) > MAX_CONFIG_STRING or
            "\0" in value):
        raise _scan_error(f"Screenshot {name} is invalid.")
    expanded = os.path.expanduser(value.strip())
    if not expanded or not os.path.isabs(expanded):
        raise _scan_error(f"Screenshot {name} must be an absolute path.")
    return Path(os.path.abspath(expanded))


def _steam_directories():
    roots = (
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    )
    found = set()
    for root in roots:
        try:
            userdata = root / "userdata"
            with os.scandir(userdata) as users:
                entries = []
                for count, entry in enumerate(users, 1):
                    if count > MAX_DIRECTORY_ENTRIES:
                        raise _scan_error(
                            "Steam userdata contains too many entries.")
                    entries.append(entry.name)
            for name in entries:
                candidate = (userdata / name / "760/remote" /
                             SCREENSHOT_APP_ID / "screenshots")
                if candidate.is_dir():
                    found.add(candidate.resolve())
        except (CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except OSError:
            continue
    return sorted(found)


def _validated_config(config):
    if not isinstance(config, dict):
        raise _scan_error("Screenshot configuration is invalid.")
    kind = config.get("kind")
    trigger = config.get("trigger", "none")
    if kind not in ("file", "folder", "path"):
        raise _scan_error("Screenshot source kind must be file, folder or path.")
    if trigger not in ("none", "hotkey", "script"):
        raise _scan_error("Screenshot trigger must be none, hotkey or script.")
    delete_after = config.get("delete_after_scan", False)
    allow_rescan = config.get("allow_rescan", False)
    if type(delete_after) is not bool or type(allow_rescan) is not bool:
        raise _scan_error("Screenshot boolean option is invalid.")
    fingerprint = config.get("previous_fingerprint")
    if fingerprint is not None and (
            not isinstance(fingerprint, str) or len(fingerprint) != 64 or
            any(character not in "0123456789abcdefABCDEF"
                for character in fingerprint)):
        raise _scan_error("Previous screenshot fingerprint is invalid.")
    path = config.get("path")
    if not isinstance(path, str) or len(path) > MAX_CONFIG_STRING or "\0" in path:
        raise _scan_error("Screenshot path is invalid.")
    result = dict(config)
    result.update(kind=kind, trigger=trigger, path=path.strip(),
                  hotkey=config.get("hotkey", "KEY_F12"),
                  script=config.get("script", ""), delete_after_scan=delete_after,
                  previous_fingerprint=(fingerprint.lower()
                                        if isinstance(fingerprint, str) else None),
                  allow_rescan=allow_rescan)
    return result


def resolve_source(config):
    """Return a validated copy with one normalized absolute source path."""
    result = _validated_config(config)
    if result["path"]:
        result["path"] = str(_absolute(result["path"], "path"))
        return result
    directories = _steam_directories()
    if len(directories) != 1:
        raise _scan_error(
            "Choose a screenshot path; automatic Steam discovery did not find exactly one Helldivers 2 screenshot directory.")
    result["path"] = str(directories[0])
    return result


def _key_codes(value):
    if (not isinstance(value, str) or not value or len(value) > 512 or
            "\0" in value):
        raise _scan_error("Screenshot hotkey is invalid.")
    try:
        from evdev import ecodes
    except ImportError as error:
        raise _scan_error("Screenshot hotkey requires Python evdev.", error)
    names = value.split("+")
    if (len(names) > 8 or len(set(names)) != len(names) or
            any(not name.startswith("KEY_") or not name.isascii() for name in names)):
        raise _scan_error("Screenshot hotkey is invalid.")
    codes = []
    for name in names:
        code = getattr(ecodes, name, None)
        if (type(code) is not int or code <= 0 or
                name in ("KEY_MAX", "KEY_CNT") or
                code > getattr(ecodes, "KEY_MAX", code)):
            raise _scan_error(f"Unknown screenshot hotkey key: {name}")
        codes.append(code)
    return tuple(codes)


def _script_path(config):
    path = _absolute(config.get("script", ""), "script")
    try:
        metadata = path.stat()
    except OSError as error:
        raise _scan_error(f"Cannot inspect screenshot script: {error}", error)
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise _scan_error("Screenshot script must be an executable file.")
    return path


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


def check_screenshot_setup(config, cancel_event=None, deadline=None):
    """Validate the configured source and trigger without running the trigger."""
    _check_cancel(cancel_event)
    _check_deadline(deadline)
    source = resolve_source(config)
    from .image_source import _reject_symlink_components
    _reject_symlink_components(Path(source["path"]))
    selection = _source_kind(source)
    if source["trigger"] == "hotkey":
        _key_codes(source["hotkey"])
        if not os.access("/dev/uinput", os.W_OK):
            raise _scan_error("Screenshot hotkey requires writable /dev/uinput.")
    elif source["trigger"] == "script":
        _script_path(source)
    _check_cancel(cancel_event)
    _check_deadline(deadline)


def _run_script(config, cancel_event=None, deadline=None):
    from .game_capture import remaining_timeout, run_command
    script = _script_path(config)
    run_command(
        [str(script)], timeout=remaining_timeout(deadline, 15),
        stdout_limit=MAX_CAPTURE_OUTPUT_BYTES,
        stderr_limit=MAX_CAPTURE_OUTPUT_BYTES, host=True,
        operation="screenshot-script", cancel_event=cancel_event)


def _cancellable_sleep(seconds, cancel_event=None, deadline=None):
    end = time.monotonic() + seconds
    while True:
        _check_cancel(cancel_event)
        _check_deadline(deadline)
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.01, remaining))


def _capture_hotkey(config, receive, cancel_event=None, deadline=None):
    try:
        from evdev import UInput, ecodes
    except ImportError as error:
        raise _scan_error("Screenshot hotkey requires Python evdev.", error)
    codes = _key_codes(config["hotkey"])
    try:
        manager = UInput({ecodes.EV_KEY: list(codes)},
                         name="HD2 Configured Screenshot Keyboard")
        keyboard = manager.__enter__()
        primary_error = None
        try:
            _cancellable_sleep(.3, cancel_event, deadline)
            pressed = []
            failure = None
            try:
                for code in codes:
                    _check_cancel(cancel_event)
                    pressed.append(code)
                    keyboard.write(ecodes.EV_KEY, code, 1)
                keyboard.syn()
                _cancellable_sleep(.03, cancel_event, deadline)
            except BaseException as error:
                failure = error
            finally:
                release_error = None
                for code in reversed(pressed):
                    try:
                        keyboard.write(ecodes.EV_KEY, code, 0)
                    except BaseException as error:
                        release_error = release_error or error
                try:
                    keyboard.syn()
                except BaseException as error:
                    release_error = release_error or error
            if failure is not None:
                raise failure
            if release_error is not None:
                raise release_error
            _check_cancel(cancel_event)
            _check_deadline(deadline)
            return receive()
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                manager.__exit__(
                    type(primary_error), primary_error,
                    primary_error.__traceback__ if primary_error else None)
            except BaseException:
                if primary_error is None:
                    raise
    except (CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        from .game_capture import ScanError
        if isinstance(error, ScanError):
            raise
        raise _scan_error(f"Screenshot hotkey failed: {error}", error)


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
        from .game_capture import ScanError
        if isinstance(error, ScanError):
            raise
        if isinstance(error, OSError):
            raise _scan_error(f"Cannot inspect screenshot directory: {error}", error)
        raise


def is_steam_managed(path):
    """Return whether a screenshot belongs to Steam's managed directory."""
    path = Path(path)
    try:
        candidate = path.resolve(strict=False)
    except OSError:
        candidate = path.absolute()
    for directory in _steam_directories():
        try:
            if candidate == directory or candidate.is_relative_to(directory):
                return True
        except (OSError, ValueError):
            continue
    return False


def _steam_screenshot_directory(path):
    """Return the discovered Steam screenshot directory containing one file."""
    path = Path(path)
    try:
        parent = path.parent.resolve()
    except OSError:
        return None
    for directory in _steam_directories():
        if parent == directory:
            return directory
    return None


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


def _read_triggered_candidate(path, selection, config, newly_created,
                              cancel_event, deadline):
    try:
        before = _metadata_snapshot(Path(path).lstat())
    except OSError as error:
        raise _scan_error("Screenshot changed before it was read.", error)
    from .image_source import read_image_source
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
        from .game_capture import ScanError
        if isinstance(error, ScanError):
            raise
        raise _scan_error(f"Cannot snapshot existing screenshot: {error}", error)


def _fingerprint_descriptor(descriptor, expected, cancel_event=None,
                            deadline=None):
    from .game_capture import MAX_ENCODED_IMAGE_BYTES
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
                from .game_capture import ScanError
                if not isinstance(error, ScanError):
                    raise
                if "Multiple new screenshots" in str(error):
                    raise
                last_error = error
        _cancellable_sleep(min(.05, max(0, end - time.monotonic())),
                           cancel_event, deadline)
    detail = f" ({last_error})" if last_error is not None else ""
    raise _scan_error(f"No complete new screenshot received within 5 seconds{detail}.")


def capture_screenshot(config, cancel_event=None, deadline=None):
    """Read or trigger one configured screenshot and return trusted metadata."""
    check_screenshot_setup(config, cancel_event=cancel_event, deadline=deadline)
    source = resolve_source(config)
    selection = _source_kind(source)
    if source["trigger"] == "none":
        from .image_source import read_image_source
        image, metadata = read_image_source(
            source["kind"], source["path"],
            previous_fingerprint=source["previous_fingerprint"],
            allow_rescan=source["allow_rescan"], cancel_event=cancel_event,
            deadline=deadline)
        if source["delete_after_scan"]:
            metadata["cleanup_skipped"] = (
                "Existing screenshots are never deleted without a trigger; "
                "use a dedicated folder.")
        return image, metadata
    before = _trigger_snapshot(source, selection, cancel_event, deadline)
    _check_cancel(cancel_event)
    _check_deadline(deadline)
    receive = lambda: _wait_for_screenshot(
        source, selection, before, cancel_event, deadline)
    if source["trigger"] == "hotkey":
        return _capture_hotkey(source, receive, cancel_event, deadline)
    _run_script(source, cancel_event, deadline)
    return receive()


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
