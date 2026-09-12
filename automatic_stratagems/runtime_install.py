"""Prepare the scanner runtime from the install hook, settings, or a button."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Callable, Mapping
import venv

from .bounded_json import read_bounded_json
from .runtime_profile import FLATPAK_INFO, SCANNER_VENV, ScanSetupError
from .runtime_setup import atomic_json, check_runtime, install_runtime
from .scanner_runtime import run_captured


FEATURE_SETTING = "automatic_stratagems_enabled"

STATE_DISABLED = "disabled"
STATE_READY = "ready"
STATE_INSTALLED = "installed"
STATE_ERROR = "error"

STATUS_SCHEMA_VERSION = 1
STATUS_PATH = "automatic_stratagems/runtime/setup-status.json"
MAX_STATUS_BYTES = 4096
MAX_SETTINGS_BYTES = 64 * 1024
SETTINGS_FILE_VERSION = "2.0"
MAX_ERROR_CHARACTERS = 1024

REQUIREMENTS = "automatic_stratagems/requirements.txt"
PIP_TIMEOUT_SECONDS = 1800
MAX_PIP_OUTPUT_BYTES = 1024 * 1024


def feature_enabled(settings: Mapping) -> bool:
    """Report whether the optional feature is switched on."""
    return settings.get(FEATURE_SETTING, False) is True


def scanner_venv(root: Path) -> Path:
    """Return the virtual environment this feature owns and may rebuild."""
    return Path(root) / SCANNER_VENV


def _pip(command: list[str]) -> tuple[int, bytes]:
    code, _stdout, stderr = run_captured(
        command, timeout=PIP_TIMEOUT_SECONDS, limit=MAX_PIP_OUTPUT_BYTES)
    return code, stderr


def install_scanner_venv(
    root: Path,
    *,
    runner: Callable[[list[str]], tuple[int, bytes]] | None = None,
) -> Path:
    """Rebuild the owned scanner environment from the pinned requirements."""
    root = Path(root)
    requirements = root / REQUIREMENTS
    if not requirements.is_file():
        raise ScanSetupError(f"Scanner requirements are missing at {requirements}")
    target = scanner_venv(root)
    try:
        venv.create(target, with_pip=True, clear=True)
    except Exception as error:
        raise ScanSetupError(
            f"Cannot create the scanner environment at {target}: {error}"
        ) from error
    command = [
        str(target / "bin/python"), "-m", "pip", "install", "--no-input",
        "--disable-pip-version-check", "--quiet", "-r", str(requirements),
    ]
    try:
        code, stderr = (runner or _pip)(command)
    except Exception as error:
        raise ScanSetupError(
            f"Cannot install the scanner dependencies: {error}") from error
    if code:
        detail = stderr.decode("utf-8", "replace").strip()
        raise ScanSetupError(
            "Cannot install the scanner dependencies: "
            f"{detail[:MAX_ERROR_CHARACTERS] or code}")
    return target


def installed_plugin_settings(root: Path) -> dict:
    """Read the plugin settings the install hook cannot request from the app.

    The plugin directory is normalized without resolving symlinks: a plugin
    directory symlinked into StreamController's plugin folder keeps the
    installed layout that holds its settings.

    StreamController 2.0 settings files wrap the values as
    ``{"file-version": "2.0", "settings": {...}}``; older files are flat.
    """
    root = Path(os.path.abspath(root))
    path = root.parent.parent / "settings/plugins" / root.name / "settings.json"
    try:
        value = read_bounded_json(path, max_bytes=MAX_SETTINGS_BYTES)
    except (OSError, ValueError):
        return {}
    if isinstance(value, dict) and value.get("file-version") == SETTINGS_FILE_VERSION:
        value = value.get("settings")
    return value if isinstance(value, dict) else {}


def status_path(root: Path) -> Path:
    """Return the setup record written by every preparation attempt."""
    return Path(root) / STATUS_PATH


def read_status(root: Path) -> dict | None:
    """Return the last recorded setup outcome, or None when absent."""
    try:
        value = read_bounded_json(status_path(root), max_bytes=MAX_STATUS_BYTES)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _is_flatpak(environ: Mapping[str, str]) -> bool:
    return bool(environ.get("FLATPAK_ID")) or FLATPAK_INFO.exists()


def _status(state: str, profile: str, error: object | None = None) -> dict:
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "state": state,
        "profile": profile,
        "error": None if error is None else str(error)[:MAX_ERROR_CHARACTERS],
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _record(root: Path, status: dict) -> dict:
    try:
        atomic_json(status_path(root), status)
    except Exception:  # noqa: BLE001 - the hook must never fail on reporting
        pass
    return status


def ensure_scanner_runtime(
    root: Path,
    settings: Mapping,
    *,
    flatpak: bool | None = None,
    environ: Mapping[str, str] | None = None,
    check: Callable[[], object] | None = None,
    install: Callable[[], object] | None = None,
) -> dict:
    """Check the scanner runtime and install it once when it is unusable."""
    root = Path(root)
    if flatpak is None:
        flatpak = _is_flatpak(os.environ if environ is None else environ)
    profile = "flatpak" if flatpak else "native"
    if not feature_enabled(settings):
        return _record(root, _status(STATE_DISABLED, profile))
    if check is None:
        def check():
            check_runtime(root, flatpak=flatpak)
    if install is None:
        def install():
            if flatpak:
                install_runtime(root)
            else:
                install_scanner_venv(root)
    try:
        check()
    except Exception:  # noqa: BLE001 - any unusable runtime is installed once
        try:
            install()
            check()
        except Exception as error:  # noqa: BLE001 - reported, never raised
            return _record(root, _status(STATE_ERROR, profile, error))
        return _record(root, _status(STATE_INSTALLED, profile))
    return _record(root, _status(STATE_READY, profile))
