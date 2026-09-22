"""Configured screenshot acquisition without live-window capture."""

import os
from pathlib import Path

from ..shared.fs import check_cancel as _check_cancel
from .capture.screenshot_cleanup import cleanup_screenshot
from .capture.screenshot_detect import (
    _source_kind, _trigger_snapshot, _wait_for_screenshot)
from .capture.screenshot_files import _check_deadline, _scan_error
from .capture.screenshot_source import is_steam_managed, resolve_source
from .capture.screenshot_trigger import (
    _capture_hotkey, _key_codes, _run_script, _script_path)


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
