"""Capture the focused Helldivers window through Hyprland and grim."""

import io
import json
import subprocess
import threading
import time

from PIL import Image


MAX_ENCODED_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 40_000_000
MAX_COMMAND_STDOUT_BYTES = MAX_ENCODED_IMAGE_BYTES
MAX_COMMAND_STDERR_BYTES = 256 * 1024


class ScanError(Exception):
    pass


def _read_bounded(stream, limit, name, output, overflow):
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            available = max(0, limit - len(output))
            output.extend(chunk[:available])
            if len(chunk) > available:
                overflow.append(name)
                return
    finally:
        stream.close()


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait()


def run_command(command, timeout=15, *, stdout_limit=None, stderr_limit=None):
    stdout_limit = MAX_COMMAND_STDOUT_BYTES if stdout_limit is None else stdout_limit
    stderr_limit = MAX_COMMAND_STDERR_BYTES if stderr_limit is None else stderr_limit
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as error:
        raise ScanError(f"Missing command: {command[0]}") from error
    stdout = bytearray()
    stderr = bytearray()
    overflow = []
    readers = [
        threading.Thread(target=_read_bounded,
                         args=(process.stdout, stdout_limit, "stdout", stdout, overflow),
                         daemon=True),
        threading.Thread(target=_read_bounded,
                         args=(process.stderr, stderr_limit, "stderr", stderr, overflow),
                         daemon=True),
    ]
    started = []
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        for reader in readers:
            reader.start()
            started.append(reader)
        while process.poll() is None and not overflow and time.monotonic() < deadline:
            time.sleep(.01)
        timed_out = process.poll() is None and not overflow
        if process.poll() is None:
            _stop(process)
    finally:
        if process.poll() is None:
            _stop(process)
        for reader, stream in zip(readers, (process.stdout, process.stderr)):
            if reader not in started:
                stream.close()
        for reader in started:
            reader.join(.5)
    if any(reader.is_alive() for reader in started):
        raise ScanError(f"{command[0]} did not close its output streams")
    if overflow:
        raise ScanError(f"{command[0]} {overflow[0]} exceeded its byte limit")
    if timed_out:
        raise ScanError(f"{command[0]} timed out after {timeout}s")
    if process.returncode:
        detail = stderr.decode(errors="replace").strip()
        raise ScanError(f"{command[0]} failed: {detail}")
    return bytes(stdout)


def decode_image(encoded, source):
    if len(encoded) > MAX_ENCODED_IMAGE_BYTES:
        raise ScanError(f"{source} encoded image is too large.")
    try:
        with Image.open(io.BytesIO(encoded)) as image:
            width, height = image.size
            if (width <= 0 or height <= 0 or
                    max(width, height) > MAX_IMAGE_DIMENSION or
                    width * height > MAX_IMAGE_PIXELS):
                raise ScanError(f"{source} image dimensions are unsupported.")
            image.load()
            return image.convert("RGB")
    except ScanError:
        raise
    except (Image.DecompressionBombError, OSError, ValueError) as error:
        raise ScanError(f"{source} returned an unreadable image.") from error


def query_active_window():
    try:
        window = json.loads(run_command(
            ["hyprctl", "-j", "activewindow"], stdout_limit=64 * 1024))
        if not isinstance(window, dict):
            raise ValueError("expected a window object")
        return window
    except (ValueError, UnicodeError) as error:
        raise ScanError(f"Invalid Hyprland window response: {error}") from error


def window_geometry(window):
    app = window.get("class", "").lower()
    native = app in {"steam_app_553850", "helldivers2.exe", "helldivers2"}
    gamescope = app == "gamescope" and window.get("title", "").upper().strip() in {
        "HELLDIVERS 2", "HELLDIVERS™ 2"}
    if not (native or gamescope) or not window.get("address"):
        raise ScanError("Focus Helldivers 2 before capture.")
    if window.get("hidden") or window.get("mapped") is False:
        raise ScanError("The game window is hidden or unmapped.")
    try:
        x, y = window["at"]
        width, height = window["size"]
        if not all(type(n) is int for n in [x, y, width, height]) or min(width, height) <= 0:
            raise ValueError("invalid dimensions")
    except (KeyError, TypeError, ValueError) as error:
        raise ScanError("The game window has invalid geometry.") from error
    return f"{x},{y} {width}x{height}"


def capture_game():
    before = query_active_window()
    geometry = window_geometry(before)
    pixels = run_command(["grim", "-g", geometry, "-t", "png", "-"])
    after = query_active_window()
    changed = [k for k in ("address", "class", "at", "size") if before.get(k) != after.get(k)]
    if changed:
        details = "; ".join(f"{k}: {before.get(k)!r} -> {after.get(k)!r}" for k in changed)
        raise ScanError(f"Game focus or geometry changed during capture; image discarded. {details}")
    window_geometry(after)
    return decode_image(pixels, "grim"), {
        "kind": "live", "geometry": geometry, "window_class": before["class"]}
