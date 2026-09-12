"""Configured screenshot acquisition without live-window capture."""

from concurrent.futures import CancelledError
import os
from pathlib import Path
import stat
import time

from ..shared.fs import check_cancel as _check_cancel
from ..shared.fs import typed_file_stamp_with_ctime as _metadata_snapshot
from ..shared.fs import poll_deadline
from .capture.screenshot_cleanup import (
    _register_cleanup, _steam_thumbnail_snapshot, cleanup_screenshot)
from .capture.screenshot_files import (
    SUPPORTED_SUFFIXES, _cancellable_sleep, _check_deadline, _directory_snapshot,
    _scan_error, _stable_fingerprint)
from .capture.screenshot_source import _absolute, is_steam_managed, resolve_source


MAX_CAPTURE_OUTPUT_BYTES = 16 * 1024
WAIT_SECONDS = 5.0


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
