"""Screenshot source configuration and Steam screenshot directory discovery."""

from concurrent.futures import CancelledError
import os
from pathlib import Path

from .screenshot_files import MAX_DIRECTORY_ENTRIES, _scan_error


MAX_CONFIG_STRING = 8192
SCREENSHOT_APP_ID = "553850"


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
