"""Capture one user-selected window through the XDG ScreenCast portal."""

import asyncio
from concurrent.futures import CancelledError
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time

from .game_capture import ScanError, decode_image, run_command
from automatic_stratagems.bounded_json import read_bounded_json


PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_INTERFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
WINDOW_SOURCE = 2
PORTAL_SELECTION_TIMEOUT_SECONDS = 60
FRAME_TIMEOUT_SECONDS = 10
STATE_DIRECTORY = "streamcontroller-helldivers-2"
STATE_FILE = "portal-restore-token.json"
MAX_STATE_BYTES = 16 * 1024
MAX_TOKEN_LENGTH = 8 * 1024
MAX_METADATA_STRING = 1024


async def connect_bus():
    from dbus_next.aio import MessageBus
    return await MessageBus(negotiate_unix_fd=True).connect()


def _message(destination=PORTAL_SERVICE, **kwargs):
    from dbus_next import Message
    return Message(destination=destination, **kwargs)


def _variant(signature, value):
    from dbus_next import Variant
    return Variant(signature, value)


def _cancelled(cancel_event):
    return cancel_event is not None and cancel_event.is_set()


def _check_cancel(cancel_event):
    if _cancelled(cancel_event):
        raise CancelledError()


async def _wait(task, deadline, cancel_event):
    while True:
        if task.done():
            return task.result()
        if _cancelled(cancel_event):
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            raise CancelledError()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            raise ScanError("Portal capture timed out.")
        done, _ = await asyncio.wait((task,), timeout=min(.05, remaining))
        if done:
            return task.result()


def _close_descriptors(reply):
    for descriptor in getattr(reply, "unix_fds", ()):
        try:
            os.close(descriptor)
        except OSError:
            pass


async def _call(bus, message, deadline, cancel_event, *, allow_fds=False):
    from dbus_next import MessageType
    reply = await _wait(asyncio.create_task(bus.call(message)), deadline,
                        cancel_event)
    if reply is None or reply.message_type == MessageType.ERROR:
        if reply is not None:
            _close_descriptors(reply)
        name = getattr(reply, "error_name", "") if reply is not None else ""
        suffix = f" ({name[:MAX_METADATA_STRING]})" if isinstance(name, str) and name else ""
        raise ScanError(f"Portal request failed{suffix}.")
    if reply.unix_fds and not allow_fds:
        _close_descriptors(reply)
        raise ScanError("Portal returned an unexpected file descriptor.")
    if _cancelled(cancel_event):
        _close_descriptors(reply)
        raise CancelledError()
    if time.monotonic() >= deadline:
        _close_descriptors(reply)
        raise ScanError("Portal capture timed out.")
    return reply


async def _cleanup_call(bus, message):
    try:
        reply = await asyncio.wait_for(bus.call(message), timeout=1)
        if reply is not None:
            _close_descriptors(reply)
    except BaseException:
        pass


def _unwrap(value):
    while hasattr(value, "value") and hasattr(value, "signature"):
        value = value.value
    return value


async def _property(bus, name, deadline, cancel_event):
    reply = await _call(bus, _message(
        path=PORTAL_PATH, interface=PROPERTIES_INTERFACE, member="Get",
        signature="ss", body=[SCREENCAST_INTERFACE, name]), deadline,
        cancel_event)
    if len(reply.body) != 1:
        raise ScanError(f"Portal returned an invalid {name} property.")
    value = _unwrap(reply.body[0])
    if type(value) is not int or value < 0:
        raise ScanError(f"Portal returned an invalid {name} property.")
    return value


def _request_path(bus, token):
    unique = getattr(bus, "unique_name", "")
    if not isinstance(unique, str) or not unique.startswith(":"):
        raise ScanError("Portal session bus has no unique identity.")
    sender = unique[1:].replace(".", "_")
    return f"{PORTAL_PATH}/request/{sender}/{token}"


def _request_prefix(bus):
    return _request_path(bus, "").rsplit("/", 1)[0] + "/"


async def _subscribe_responses(bus, deadline, cancel_event):
    rule = ("type='signal',interface='org.freedesktop.portal.Request',"
            "member='Response'")
    reply = await _call(bus, _message(
        destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus", member="AddMatch", signature="s",
        body=[rule]), deadline, cancel_event)
    if reply.body:
        raise ScanError("Session bus rejected the portal response subscription.")


def _response(message):
    from dbus_next import MessageType
    return (message.message_type == MessageType.SIGNAL and
            message.interface == REQUEST_INTERFACE and
            message.member == "Response")


async def _request(bus, member, signature, body, options, deadline,
                   cancel_event):
    token = "hd2_" + secrets.token_hex(12)
    options = {**options, "handle_token": _variant("s", token)}
    expected = _request_path(bus, token)
    prefix = _request_prefix(bus)
    paths = {expected}
    early = {}
    response = asyncio.get_running_loop().create_future()

    def receive(message):
        if not _response(message):
            return
        if not isinstance(message.path, str) or not message.path.startswith(prefix):
            return
        if message.path in paths:
            if not response.done():
                response.set_result(message)
        else:
            if len(early) < 4:
                early[message.path] = message

    bus.add_message_handler(receive)
    handle = expected
    try:
        reply = await _call(bus, _message(
            path=PORTAL_PATH, interface=SCREENCAST_INTERFACE, member=member,
            signature=signature, body=[*body, options]), deadline,
            cancel_event)
        if (len(reply.body) != 1 or not isinstance(reply.body[0], str) or
                len(reply.body[0]) > MAX_METADATA_STRING):
            raise ScanError(f"Portal returned an invalid {member} request handle.")
        handle = reply.body[0]
        if not handle.startswith(prefix):
            raise ScanError(f"Portal returned an invalid {member} request handle.")
        paths.add(handle)
        if handle in early and not response.done():
            response.set_result(early[handle])
        signal = await _wait(response, deadline, cancel_event)
        if len(signal.body) != 2 or type(signal.body[0]) is not int:
            raise ScanError(f"Portal returned an invalid {member} response.")
        if signal.body[0] == 1:
            raise ScanError("Portal window selection was cancelled.")
        if signal.body[0] != 0 or not isinstance(signal.body[1], dict):
            raise ScanError(f"Portal could not {member.lower()} the capture session.")
        return {key: _unwrap(value) for key, value in signal.body[1].items()}
    except BaseException:
        await _cleanup_call(bus, _message(
            path=handle, interface=REQUEST_INTERFACE, member="Close"))
        raise
    finally:
        bus.remove_message_handler(receive)


def _valid_object_path(value, kind):
    if (not isinstance(value, str) or not value.startswith("/") or
            len(value) > MAX_METADATA_STRING):
        raise ScanError(f"Portal returned an invalid {kind} handle.")
    return value


def _stream_details(stream, version):
    if (not isinstance(stream, (list, tuple)) or len(stream) != 2 or
            type(stream[0]) is not int or stream[0] <= 0 or
            not isinstance(stream[1], dict)):
        raise ScanError("Portal returned an invalid PipeWire stream.")
    node, raw_properties = stream
    properties = {key: _unwrap(value) for key, value in raw_properties.items()}
    source_type = properties.get("source_type")
    if version >= 3 and source_type is None:
        raise ScanError("Portal stream has no source type; capture discarded.")
    if source_type is not None and source_type != WINDOW_SOURCE:
        raise ScanError("Portal returned a non-window stream; capture discarded.")
    stream_id = properties.get("id")
    if stream_id is not None and (not isinstance(stream_id, str) or
                                  len(stream_id) > MAX_METADATA_STRING):
        raise ScanError("Portal returned an invalid stream identity.")
    if version >= 6:
        serial = properties.get("pipewire-serial")
        if type(serial) is not int or serial <= 0:
            raise ScanError("Portal version 6 stream has no valid PipeWire serial.")
        selector = f"target-object={serial}"
    else:
        serial = None
        selector = f"path={node}"
    return node, serial, stream_id, source_type, selector


async def _pipewire_fd(bus, session, deadline, cancel_event):
    reply = await _call(bus, _message(
        path=PORTAL_PATH, interface=SCREENCAST_INTERFACE,
        member="OpenPipeWireRemote", signature="oa{sv}",
        body=[session, {}]), deadline, cancel_event, allow_fds=True)
    descriptors = list(reply.unix_fds)
    valid = (reply.signature == "h" and len(reply.body) == 1 and
             type(reply.body[0]) is int and len(descriptors) == 1 and
             reply.body[0] == 0)
    if not valid:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ScanError("Portal returned an invalid PipeWire file descriptor.")
    return descriptors[0]


async def _capture(bus, state, deadline, cancel_event):
    version = await _property(bus, "version", deadline, cancel_event)
    sources = await _property(bus, "AvailableSourceTypes", deadline,
                              cancel_event)
    if not sources & WINDOW_SOURCE:
        raise ScanError("The desktop portal does not support window capture.")
    await _subscribe_responses(bus, deadline, cancel_event)
    created = await _request(bus, "CreateSession", "a{sv}", [], {
        "session_handle_token": _variant("s", "hd2_" + secrets.token_hex(12))},
        deadline, cancel_event)
    session = _valid_object_path(created.get("session_handle"), "session")
    session_prefix = _request_prefix(bus).replace("/request/", "/session/")
    if not session.startswith(session_prefix):
        raise ScanError("Portal returned an invalid session handle.")
    try:
        options = {"types": _variant("u", WINDOW_SOURCE),
                   "multiple": _variant("b", False)}
        if version >= 4:
            options["persist_mode"] = _variant("u", 2)
            if state["restore_token"]:
                options["restore_token"] = _variant(
                    "s", state["restore_token"])
                state["used"] = True
        await _request(bus, "SelectSources", "oa{sv}", [session], options,
                       deadline, cancel_event)
        started = await _request(bus, "Start", "osa{sv}", [session, ""], {},
                                 deadline, cancel_event)
        streams = started.get("streams")
        if not isinstance(streams, (list, tuple)) or len(streams) != 1:
            raise ScanError("Portal did not return exactly one window stream.")
        node, serial, stream_id, source_type, selector = _stream_details(
            streams[0], version)
        restore_token = started.get("restore_token")
        if (version >= 4 and isinstance(restore_token, str) and
                0 < len(restore_token) <= MAX_TOKEN_LENGTH):
            state["next_token"] = restore_token
        descriptor = await _pipewire_fd(bus, session, deadline, cancel_event)
        try:
            _check_cancel(cancel_event)
            command = [
                "gst-launch-1.0", "-q", "pipewiresrc", f"fd={descriptor}",
                selector, "num-buffers=1", "do-timestamp=true", "!",
                "videoconvert", "!", "video/x-raw,format=RGB", "!",
                "pngenc", "snapshot=true", "!", "fdsink", "fd=1"]
            encoded = run_command(
                command, timeout=FRAME_TIMEOUT_SECONDS,
                pass_fds=(descriptor,), cancel_event=cancel_event)
            _check_cancel(cancel_event)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
        image = decode_image(encoded, "Portal capture")
        _check_cancel(cancel_event)
        return image, {
            "kind": "live", "platform": "portal", "stream_id": stream_id,
            "source_type": source_type, "pipewire_node": node,
            "pipewire_serial": serial}
    finally:
        await _cleanup_call(bus, _message(
            path=session, interface=SESSION_INTERFACE, member="Close"))


def _state_directory():
    root = os.environ.get("XDG_STATE_HOME")
    return Path(root) if root else Path.home() / ".local" / "state"


def _read_token(path):
    try:
        value = read_bounded_json(path, max_bytes=MAX_STATE_BYTES)
        if not isinstance(value, dict):
            return None
        token = value.get("restore_token") if value == {
            "owner": "streamcontroller-helldivers-2", "version": 1,
            "restore_token": value.get("restore_token")} else None
        return token if isinstance(token, str) and 0 < len(token) <= MAX_TOKEN_LENGTH else None
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


def _write_token(path, token):
    payload = json.dumps({"owner": "streamcontroller-helldivers-2",
                          "version": 1, "restore_token": token}) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".portal-token-",
                                              dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def _locked_state(deadline, cancel_event):
    directory = _state_directory() / STATE_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ScanError("Portal state directory is unsafe.")
    lock_path = directory / ".portal-token.lock"
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ScanError(f"Cannot open portal state lock: {error}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ScanError("Portal state lock is unsafe.")
        os.fchmod(descriptor, 0o600)
        while True:
            if _cancelled(cancel_event):
                raise CancelledError()
            if time.monotonic() >= deadline:
                raise ScanError("Portal capture timed out.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.05)
        path = directory / STATE_FILE
        state = {"restore_token": _read_token(path), "used": False,
                 "next_token": None}
        failure = None
        try:
            yield path, state
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                if state["next_token"]:
                    _write_token(path, state["next_token"])
                elif state["used"]:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            except OSError:
                if failure is None:
                    raise
    finally:
        os.close(descriptor)


def capture_portal(cancel_event=None):
    deadline = time.monotonic() + PORTAL_SELECTION_TIMEOUT_SECONDS
    with _locked_state(deadline, cancel_event) as (_, state):
        async def run():
            bus = await _wait(asyncio.create_task(connect_bus()), deadline,
                              cancel_event)
            try:
                return await _capture(bus, state, deadline, cancel_event)
            finally:
                try:
                    bus.disconnect()
                except BaseException:
                    pass
        return asyncio.run(run())
