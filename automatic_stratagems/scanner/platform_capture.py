"""Identify and capture the active game window on native Linux desktops."""

import re

from .game_capture import ScanError, decode_image, run_command
from .capture_environment import session_kind


GAME_CLASSES = {"steam_app_553850", "helldivers2.exe", "helldivers2"}
GAME_TITLES = {"HELLDIVERS 2", "HELLDIVERS™ 2"}


def validate_game_window(window):
    window_class = str(window.get("class", "")).lower()
    title = str(window.get("title", "")).upper().strip()
    if (window_class not in GAME_CLASSES and
            not (window_class == "gamescope" and title in GAME_TITLES)):
        raise ScanError("Focus Helldivers 2 before capture.")
    if not window.get("address"):
        raise ScanError("The focused game window has no stable identity.")


def same_window(before, after):
    return all(before.get(key) == after.get(key)
               for key in ("platform", "address", "pid", "class", "title"))


def _property(output, name):
    match = re.search(rf"^{re.escape(name)}(?:\([^\n]*\))?\s*=\s*(.*)$",
                      output, re.MULTILINE)
    return match.group(1).strip() if match else None


def _quoted(value):
    if value is None:
        return []
    def unescape(item):
        output = bytearray()
        index = 0
        while index < len(item):
            if (item[index] == "\\" and index + 3 < len(item) and
                    all(character in "01234567" for character in
                        item[index + 1:index + 4])):
                output.append(int(item[index + 1:index + 4], 8))
                index += 4
            elif item[index:index + 2] in ('\\"', "\\\\"):
                output.extend(item[index + 1].encode("utf-8"))
                index += 2
            else:
                output.extend(item[index].encode("utf-8"))
                index += 1
        return output.decode("utf-8", errors="replace")
    return [unescape(item)
            for item in re.findall(r'"((?:\\.|[^"\\])*)"', value)]


def query_x11_window():
    root = run_command(["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                       stdout_limit=64 * 1024).decode("utf-8", errors="replace")
    match = re.search(r"#\s*(0x[0-9a-fA-F]+)\b", root)
    if not match or int(match.group(1), 16) == 0:
        raise ScanError("No active X11 window is available.")
    window_id = match.group(1).lower()
    details = run_command([
        "xprop", "-id", window_id, "_NET_WM_PID", "WM_CLASS",
        "_NET_WM_NAME", "WM_NAME"], stdout_limit=64 * 1024
    ).decode("utf-8", errors="replace")
    pid_value = _property(details, "_NET_WM_PID")
    classes = _quoted(_property(details, "WM_CLASS"))
    titles = (_quoted(_property(details, "_NET_WM_NAME")) or
              _quoted(_property(details, "WM_NAME")))
    try:
        pid = int(pid_value) if pid_value is not None else None
    except ValueError:
        pid = None
    return {"platform": "x11", "address": window_id, "pid": pid,
            "class": classes[-1] if classes else "",
            "title": titles[0] if titles else ""}


def capture_x11(before, cancel_event=None):
    validate_game_window(before)
    window_id = before["address"]
    encoded = run_command(["import", "-window", window_id, "png:-"],
                          timeout=10, cancel_event=cancel_event)
    after = query_x11_window()
    validate_game_window(after)
    if not same_window(before, after):
        raise ScanError("Game focus changed during X11 capture; image discarded.")
    return decode_image(encoded, "X11 capture"), {
        "kind": "live", "platform": "x11", "window_id": window_id,
        "window_class": before["class"]}
