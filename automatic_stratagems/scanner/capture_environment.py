"""Classify the native display session without loading capture dependencies."""


def session_kind(environment):
    session = environment.get("XDG_SESSION_TYPE", "").lower()
    desktop = environment.get("XDG_CURRENT_DESKTOP", "").lower()
    if session == "x11" or (session != "wayland" and
                            environment.get("DISPLAY") and
                            not environment.get("WAYLAND_DISPLAY")):
        return "x11"
    if session == "wayland" or environment.get("WAYLAND_DISPLAY"):
        return "hyprland" if "hyprland" in desktop else "wayland"
    return "unknown"
