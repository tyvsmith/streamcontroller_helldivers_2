"""Bounded command execution for scanner capture."""

import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from concurrent.futures import CancelledError

from ..errors import ScanError

# Readers look this name up at start so tests can replace the drain.
from ...shared.fs import read_bounded_stream as _read_bounded


# Command stdout can carry an encoded capture, so the image byte cap lives here
# and image_decode imports it; defining it there would pull Pillow into commands.
MAX_ENCODED_IMAGE_BYTES = 64 * 1024 * 1024
MAX_COMMAND_STDOUT_BYTES = MAX_ENCODED_IMAGE_BYTES
MAX_COMMAND_STDERR_BYTES = 256 * 1024
NATIVE_SETSID = "/usr/bin/setsid"


def remaining_timeout(deadline, maximum):
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ScanError('Scanner work deadline exhausted.')
    return min(maximum, remaining)


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait()


def run_command(command, timeout=15, *, stdout_limit=None, stderr_limit=None,
                pass_fds=(), cancel_event=None, host=False, operation=None,
                host_env=None):
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError()
    command_name = command[0]
    host_operation = None
    flatpak = bool(os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").exists())
    host_env = {} if host_env is None else dict(host_env)
    if any(not isinstance(name, str) or not name or "=" in name or "\0" in name or
           not isinstance(value, str) or "\0" in value
           for name, value in host_env.items()):
        raise ScanError("Invalid host command environment")
    child_env = None
    if host and flatpak:
        if shutil.which("flatpak-spawn") is None:
            raise ScanError("Cannot start host command: flatpak-spawn is unavailable")
        from automatic_stratagems.host_commands import guard_command, launcher_command
        guarded, host_operation = guard_command(
            command, operation=operation or Path(command[0]).name)
        host_command = ["flatpak-spawn", "--host", "--watch-bus",
                        *(f"--env={name}={value}" for name, value in host_env.items()),
                        "--directory=/", *guarded]
        command = launcher_command(host_command, host_operation)
    elif host:
        from automatic_stratagems.host_commands import (JOB_DIRECTORY_ENV,
                                                         guard_command,
                                                         launcher_command)
        if os.environ.get(JOB_DIRECTORY_ENV):
            if (not os.path.isfile(NATIVE_SETSID) or
                    not os.access(NATIVE_SETSID, os.X_OK)):
                raise ScanError(
                    "Cannot start guarded host command: setsid is unavailable")
            guarded, host_operation = guard_command(
                command, operation=operation or Path(command[0]).name)
            command = launcher_command([NATIVE_SETSID, *guarded], host_operation)
        if host_env:
            child_env = {**os.environ, **host_env}
    elif host_env:
        child_env = {**os.environ, **host_env}
    stdout_limit = MAX_COMMAND_STDOUT_BYTES if stdout_limit is None else stdout_limit
    stderr_limit = MAX_COMMAND_STDERR_BYTES if stderr_limit is None else stderr_limit
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   pass_fds=tuple(pass_fds), env=child_env)
    except OSError as error:
        if host_operation is not None:
            from automatic_stratagems.host_commands import mark_not_started
            mark_not_started(host_operation)
        raise ScanError(f"Cannot start command {command_name}: {error}") from error
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
    cancelled = False
    completion = None
    try:
        for reader in readers:
            reader.start()
            started.append(reader)
        while process.poll() is None and not overflow and time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
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
        if host_operation is not None:
            from automatic_stratagems.host_commands import (reconcile_operation,
                                                             wait_operation)
            status = process.returncode if type(process.returncode) is int else 125
            if status < 0:
                status = min(255, 128 - status)
            reconcile_operation(host_operation, max(0, min(255, status)))
            completion = wait_operation(host_operation)
    if any(reader.is_alive() for reader in started):
        raise ScanError(f"{command_name} did not close its output streams")
    if cancelled:
        raise CancelledError()
    if overflow:
        raise ScanError(f"{command_name} {overflow[0]} exceeded its byte limit")
    if timed_out:
        raise ScanError(f"{command_name} timed out after {timeout}s")
    command_status = (completion.command_status
                      if completion is not None and
                      type(completion.command_status) is int else process.returncode)
    if command_status:
        detail = stderr.decode(errors="replace").strip()
        raise ScanError(f"{command_name} failed: {detail}")
    return bytes(stdout)
