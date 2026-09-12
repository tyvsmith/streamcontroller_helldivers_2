"""Causing a configured screenshot: the hotkey chord or the launcher script."""

from concurrent.futures import CancelledError
import os
import stat

from ...shared.fs import check_cancel as _check_cancel
from .screenshot_files import _cancellable_sleep, _check_deadline, _scan_error
from .screenshot_source import _absolute


MAX_CAPTURE_OUTPUT_BYTES = 16 * 1024


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


def _run_script(config, cancel_event=None, deadline=None):
    from ..game_capture import remaining_timeout, run_command
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
        from ..game_capture import ScanError
        if isinstance(error, ScanError):
            raise
        raise _scan_error(f"Screenshot hotkey failed: {error}", error)
