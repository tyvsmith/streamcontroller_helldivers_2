import asyncio
from concurrent.futures import CancelledError
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from dbus_next import Message, MessageType, Variant
from PIL import Image

from automatic_stratagems.scanner.game_capture import ScanError
from automatic_stratagems.scanner import portal_capture as portal


class FakeBus:
    unique_name = ":1.42"

    def __init__(self, *, version=4, source_types=2, stream=None, fd=71,
                 responses=None, restore_token="rotated-token"):
        self.version = version
        self.source_types = source_types
        self.stream = stream or [77, {
            "source_type": Variant("u", 2), "id": Variant("s", "window-one")}]
        self.fd = fd
        self.responses = responses or {}
        self.restore_token = restore_token
        self.handlers = []
        self.calls = []
        self.closed_paths = []
        self.disconnected = False

    def add_message_handler(self, handler):
        self.handlers.append(handler)

    def remove_message_handler(self, handler):
        self.handlers.remove(handler)

    def disconnect(self):
        self.disconnected = True

    async def call(self, message):
        self.calls.append(message)
        message.serial = len(self.calls)
        if message.interface == "org.freedesktop.DBus.Properties":
            value = self.version if message.body[1] == "version" else self.source_types
            return Message.new_method_return(message, "v", [Variant("u", value)])
        if message.destination == "org.freedesktop.DBus":
            return Message.new_method_return(message)
        if message.interface == portal.SESSION_INTERFACE:
            self.closed_paths.append(message.path)
            return Message.new_method_return(message)
        if message.interface == portal.REQUEST_INTERFACE:
            self.closed_paths.append(message.path)
            return Message.new_method_return(message)
        if message.member == "OpenPipeWireRemote":
            return Message.new_method_return(message, "h", [0], unix_fds=[self.fd])
        handle = f"/org/freedesktop/portal/desktop/request/1_42/{message.body[-1]['handle_token'].value}"
        reply = Message.new_method_return(message, "o", [handle])
        loop = asyncio.get_running_loop()
        if message.member == "CreateSession":
            results = {"session_handle": Variant("s", "/org/freedesktop/portal/desktop/session/1_42/hd2")}
        elif message.member == "SelectSources":
            results = {}
        else:
            results = {"streams": Variant("a(ua{sv})", [self.stream])}
            if self.restore_token is not None:
                results["restore_token"] = Variant("s", self.restore_token)
        signal = Message(path=handle, interface=portal.REQUEST_INTERFACE,
                         member="Response", message_type=MessageType.SIGNAL,
                         signature="ua{sv}",
                         body=[self.responses.get(message.member, 0), results])
        loop.call_soon(lambda: [handler(signal) for handler in list(self.handlers)])
        return reply


class PortalCaptureTests(unittest.TestCase):
    def png(self):
        output = io.BytesIO()
        Image.new("RGB", (100, 75), "blue").save(output, "PNG")
        return output.getvalue()

    def run_capture(self, bus, **kwargs):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus), \
             patch.object(portal, "run_command", return_value=self.png()) as run:
            image, source = portal.capture_portal(**kwargs)
            token_path = Path(directory, portal.STATE_DIRECTORY,
                              portal.STATE_FILE)
            token = token_path.read_text() if token_path.exists() else None
        return image, source, run, token

    def test_window_only_session_reads_one_pipewire_frame_and_closes(self):
        bus = FakeBus(version=4)
        image, source, run, token = self.run_capture(bus)
        self.assertEqual(image.size, (100, 75))
        select = next(call for call in bus.calls if call.member == "SelectSources")
        self.assertEqual(select.body[1]["types"].value, portal.WINDOW_SOURCE)
        self.assertFalse(select.body[1]["multiple"].value)
        self.assertLess(next(index for index, call in enumerate(bus.calls)
                             if call.member == "AddMatch"),
                        next(index for index, call in enumerate(bus.calls)
                             if call.member == "CreateSession"))
        self.assertIn("num-buffers=1", run.call_args.args[0])
        self.assertIn("path=77", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["pass_fds"], (71,))
        self.assertEqual(source["stream_id"], "window-one")
        self.assertIn("rotated-token", token)
        self.assertIn("/org/freedesktop/portal/desktop/session/1_42/hd2",
                      bus.closed_paths)
        self.assertTrue(bus.disconnected)

    def test_completed_wait_result_transfers_ownership_before_cancellation(self):
        cancel = threading.Event()
        marker = object()

        async def scenario():
            async def complete():
                return marker
            task = asyncio.create_task(complete())
            await asyncio.sleep(0)
            cancel.set()
            return await portal._wait(
                task, portal.time.monotonic() + 1, cancel)

        self.assertIs(asyncio.run(scenario()), marker)

    def test_v6_requires_serial_and_targets_it(self):
        stream = [77, {"source_type": Variant("u", 2),
                       "pipewire-serial": Variant("t", 9001)}]
        bus = FakeBus(version=6, stream=stream)
        _, _, run, _ = self.run_capture(bus)
        self.assertIn("target-object=9001", run.call_args.args[0])
        self.assertNotIn("path=77", run.call_args.args[0])

    def test_v6_missing_serial_fails_closed(self):
        bus = FakeBus(version=6)
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus), \
             patch.object(portal, "run_command") as run:
            with self.assertRaisesRegex(ScanError, "serial"):
                portal.capture_portal()
        run.assert_not_called()
        self.assertTrue(bus.disconnected)

    def test_rejects_portal_without_window_sources(self):
        bus = FakeBus(source_types=1)
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus):
            with self.assertRaisesRegex(ScanError, "window capture"):
                portal.capture_portal()
        self.assertFalse(any(call.member == "CreateSession" for call in bus.calls))

    def test_rejects_monitor_stream_even_after_window_only_request(self):
        bus = FakeBus(stream=[77, {"source_type": Variant("u", 1)}])
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus), \
             patch.object(portal, "run_command") as run:
            with self.assertRaisesRegex(ScanError, "non-window"):
                portal.capture_portal()
        run.assert_not_called()

    def test_v3_and_newer_require_window_source_metadata(self):
        bus = FakeBus(version=4, stream=[77, {}])
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus), \
             patch.object(portal, "run_command") as run:
            with self.assertRaisesRegex(ScanError, "source type"):
                portal.capture_portal()
        run.assert_not_called()

    def test_pre_v3_accepts_missing_source_metadata_after_window_only_request(self):
        bus = FakeBus(version=2, stream=[77, {}])
        image, source, _, _ = self.run_capture(bus)
        self.assertEqual(image.size, (100, 75))
        self.assertIsNone(source["source_type"])

    def test_external_cancellation_closes_pending_request(self):
        bus = FakeBus()
        cancel = threading.Event()

        async def stalled_call(message):
            bus.calls.append(message)
            message.serial = len(bus.calls)
            if message.interface == "org.freedesktop.DBus.Properties":
                return Message.new_method_return(message, "v", [Variant("u", 4 if message.body[1] == "version" else 2)])
            if message.destination == "org.freedesktop.DBus":
                return Message.new_method_return(message)
            if message.interface in (portal.REQUEST_INTERFACE, portal.SESSION_INTERFACE):
                bus.closed_paths.append(message.path)
                return Message.new_method_return(message)
            cancel.set()
            await asyncio.sleep(10)

        bus.call = stalled_call
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus):
            with self.assertRaises(CancelledError):
                portal.capture_portal(cancel_event=cancel)
        self.assertTrue(any("request" in path for path in bus.closed_paths))
        self.assertTrue(bus.disconnected)

    def test_bad_pipewire_fd_index_closes_received_fds(self):
        bus = FakeBus()

        original = bus.call
        async def bad_fd(message):
            if message.member == "OpenPipeWireRemote":
                bus.calls.append(message)
                message.serial = len(bus.calls)
                return Message.new_method_return(message, "h", [2], unix_fds=[91])
            return await original(message)
        bus.call = bad_fd
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus), \
             patch.object(portal, "run_command", return_value=self.png()), \
             patch.object(portal.os, "close") as close:
            with self.assertRaisesRegex(ScanError, "file descriptor"):
                portal.capture_portal()
        close.assert_any_call(91)

    def test_unexpected_fd_on_property_reply_is_closed(self):
        bus = FakeBus()
        original = bus.call

        async def unexpected_fd(message):
            reply = await original(message)
            if (message.interface == "org.freedesktop.DBus.Properties" and
                    message.body[1] == "version"):
                reply.unix_fds = [92]
            return reply
        bus.call = unexpected_fd
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus), \
             patch.object(portal, "run_command", return_value=self.png()), \
             patch.object(portal.os, "close") as close:
            with self.assertRaisesRegex(ScanError, "unexpected file descriptor"):
                portal.capture_portal()
        close.assert_any_call(92)

    def test_cleanup_reply_file_descriptors_are_closed(self):
        class CleanupBus:
            async def call(self, message):
                message.serial = 1
                return Message.new_method_return(
                    message, "h", [0], unix_fds=[93])

        async def cleanup():
            await portal._cleanup_call(CleanupBus(), portal._message(
                path="/org/freedesktop/portal/desktop/session/1_42/test",
                interface=portal.SESSION_INTERFACE, member="Close"))

        with patch.object(portal.os, "close") as close:
            asyncio.run(cleanup())
        close.assert_any_call(93)

    def test_frame_cancellation_closes_session(self):
        bus = FakeBus()
        cancel = threading.Event()

        def stop(*args, **kwargs):
            cancel.set()
            raise CancelledError()

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus), \
             patch.object(portal, "run_command", side_effect=stop):
            with self.assertRaises(CancelledError):
                portal.capture_portal(cancel_event=cancel)
        self.assertIn("/org/freedesktop/portal/desktop/session/1_42/hd2",
                      bus.closed_paths)
        self.assertTrue(bus.disconnected)

    def test_cancellation_as_frame_finishes_discards_image(self):
        bus = FakeBus()
        cancel = threading.Event()

        def finish(*args, **kwargs):
            cancel.set()
            return self.png()

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus), \
             patch.object(portal, "run_command", side_effect=finish):
            with self.assertRaises(CancelledError):
                portal.capture_portal(cancel_event=cancel)
        self.assertIn("/org/freedesktop/portal/desktop/session/1_42/hd2",
                      bus.closed_paths)

    def test_consumed_restore_token_is_removed_after_capture_failure(self):
        bus = FakeBus(version=4, restore_token=None)
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus), \
             patch.object(portal, "run_command", side_effect=ScanError("frame failed")):
            state = Path(directory, portal.STATE_DIRECTORY)
            state.mkdir()
            token = state / portal.STATE_FILE
            token.write_text('{"owner":"streamcontroller-helldivers-2",'
                             '"version":1,"restore_token":"old-token"}\n')
            with self.assertRaisesRegex(ScanError, "frame failed"):
                portal.capture_portal()
            self.assertFalse(token.exists())

    def test_picker_denial_closes_session_without_capturing(self):
        bus = FakeBus(responses={"Start": 1})
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus), \
             patch.object(portal, "run_command") as run:
            with self.assertRaisesRegex(ScanError, "selection was cancelled"):
                portal.capture_portal()
        run.assert_not_called()
        self.assertIn("/org/freedesktop/portal/desktop/session/1_42/hd2",
                      bus.closed_paths)

    def test_dbus_error_does_not_expose_restore_token(self):
        bus = FakeBus()
        original = bus.call

        async def fail_select(message):
            if message.member == "SelectSources":
                bus.calls.append(message)
                message.serial = len(bus.calls)
                return Message.new_error(
                    message, "org.freedesktop.portal.Error.Failed",
                    "secret old-token leaked by service")
            return await original(message)
        bus.call = fail_select
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_STATE_HOME": directory}, clear=False), \
             patch.object(portal, "connect_bus", return_value=bus):
            state = Path(directory, portal.STATE_DIRECTORY)
            state.mkdir()
            (state / portal.STATE_FILE).write_text(
                '{"owner":"streamcontroller-helldivers-2","version":1,'
                '"restore_token":"old-token"}\n')
            with self.assertRaises(ScanError) as caught:
                portal.capture_portal()
        self.assertNotIn("old-token", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
