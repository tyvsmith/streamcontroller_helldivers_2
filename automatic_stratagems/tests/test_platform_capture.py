import io
import os
import unittest
from unittest.mock import patch

from PIL import Image

from automatic_stratagems.scanner.game_capture import ScanError
from automatic_stratagems.scanner import platform_capture as pc


class PlatformCaptureTests(unittest.TestCase):
    def png(self):
        output = io.BytesIO()
        Image.new("RGB", (80, 60), "red").save(output, "PNG")
        return output.getvalue()

    def x11_window(self, **changes):
        window = {
            "platform": "x11",
            "address": "0x4a00007",
            "pid": 553850,
            "class": "steam_app_553850",
            "title": "HELLDIVERS 2",
        }
        return {**window, **changes}

    def test_session_kind_preserves_hyprland_and_detects_x11(self):
        self.assertEqual(pc.session_kind({
            "XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "Hyprland"}),
            "hyprland")
        self.assertEqual(pc.session_kind({
            "XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}), "x11")
        self.assertEqual(pc.session_kind({
            "XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"}),
            "wayland")

    def test_xprop_query_uses_active_xid_and_parses_identity(self):
        root = b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x4a00007\n"
        detail = (b'_NET_WM_PID(CARDINAL) = 553850\n'
                  b'WM_CLASS(STRING) = "steam_app_553850", "steam_app_553850"\n'
                  b'_NET_WM_NAME(UTF8_STRING) = "HELLDIVERS\\342\\204\\242 2"\n'
                  b'WM_NAME(STRING) = "HELLDIVERS 2"\n')
        with patch.object(pc, "run_command", side_effect=[root, detail]) as run:
            window = pc.query_x11_window()
        self.assertEqual(window["address"], "0x4a00007")
        self.assertEqual(window["pid"], 553850)
        self.assertEqual(window["class"], "steam_app_553850")
        self.assertEqual(window["title"], "HELLDIVERS™ 2")
        self.assertEqual(run.call_args_list[1].args[0][0:3],
                         ["xprop", "-id", "0x4a00007"])

    def test_xprop_rejects_no_active_window(self):
        with patch.object(pc, "run_command", return_value=(
                b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n")):
            with self.assertRaisesRegex(ScanError, "active X11 window"):
                pc.query_x11_window()

    def test_x11_capture_targets_exact_window_and_rechecks_identity(self):
        before = self.x11_window()
        with patch.object(pc, "query_x11_window", return_value=before), \
             patch.object(pc, "run_command", return_value=self.png()) as run:
            image, source = pc.capture_x11(before)
        self.assertEqual(image.size, (80, 60))
        self.assertEqual(run.call_args.args[0],
                         ["import", "-window", "0x4a00007", "png:-"])
        self.assertEqual(source["window_id"], "0x4a00007")

    def test_x11_capture_discards_changed_window(self):
        before = self.x11_window()
        after = self.x11_window(address="0x4a00008")
        with patch.object(pc, "query_x11_window", return_value=after), \
             patch.object(pc, "run_command", return_value=self.png()):
            with self.assertRaisesRegex(ScanError, "changed"):
                pc.capture_x11(before)

    def test_x11_capture_rejects_non_game_before_running_import(self):
        with patch.object(pc, "run_command") as run:
            with self.assertRaisesRegex(ScanError, "Focus Helldivers"):
                pc.capture_x11(self.x11_window(**{"class": "terminal"}))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
