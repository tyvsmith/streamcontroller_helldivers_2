"""Prepare the scanner runtime from the install hook, settings, or a button."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable, Mapping
import venv

from .runtime_profile import FLATPAK_INFO, SCANNER_VENV, ScanSetupError, atomic_json
from ..shared.bounded_json import read_bounded_json
from .verify import check_runtime


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


def install_runtime(root: Path) -> Path:
    """Install the locked Flatpak payload, importing its heavy build code lazily."""
    from . import build
    return build.install_runtime(root)


def feature_enabled(settings: Mapping) -> bool:
    """Report whether the optional feature is switched on."""
    return settings.get(FEATURE_SETTING, False) is True


def scanner_venv(root: Path) -> Path:
    """Return the virtual environment this feature owns and may rebuild."""
    return Path(root) / SCANNER_VENV


def _signal_group(process: subprocess.Popen, signum: int) -> None:
    # The leader is unreaped while returncode is None, so its pid still names
    # this install's group and cannot have been reused.
    if process.returncode is None:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass


class _InstallRegistry:
    """Track running pip installs so plugin shutdown can stop their groups."""

    def __init__(self):
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen] = set()
        self._stopped = False

    def start(self, command: list[str], **options) -> subprocess.Popen:
        with self._lock:
            if self._stopped:
                raise ScanSetupError(
                    "Scanner runtime install skipped: StreamController is shutting down")
            process = subprocess.Popen(command, start_new_session=True, **options)
            self._processes.add(process)
            return process

    def finish(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.discard(process)

    def terminate(self, grace: float) -> bool:
        with self._lock:
            self._stopped = True
            processes = list(self._processes)
            for process in processes:
                _signal_group(process, signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, grace)
        stopped = True
        for process in processes:
            try:
                process.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _signal_group(process, signal.SIGKILL)
                try:
                    process.wait(1)
                except subprocess.TimeoutExpired:
                    stopped = False
        return stopped


_INSTALLS = _InstallRegistry()


def terminate_runtime_installs(grace: float) -> bool:
    """Stop running pip installs and refuse new ones; True when every group exited.

    Each install's process group gets SIGTERM, then SIGKILL once ``grace``
    seconds pass.
    """
    return _INSTALLS.terminate(grace)


def _pip(command: list[str]) -> tuple[int, bytes]:
    """Run pip in its own session, so shutdown can stop it and its build children."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = _INSTALLS.start(
            command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        try:
            try:
                code = process.wait(PIP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                _signal_group(process, signal.SIGKILL)
                process.wait()
                raise
        finally:
            _INSTALLS.finish(process)
        if max(os.fstat(stdout.fileno()).st_size,
               os.fstat(stderr.fileno()).st_size) > MAX_PIP_OUTPUT_BYTES:
            raise ScanSetupError(
                f"Scanner runtime child output exceeded {MAX_PIP_OUTPUT_BYTES // 1024} KiB")
        stderr.seek(0)
        return code, stderr.read(MAX_PIP_OUTPUT_BYTES + 1)


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


class PluginSettingsError(Exception):
    """The plugin settings file exists but its values cannot be used."""


def installed_plugin_settings(root: Path) -> dict:
    """Read the plugin settings the install hook cannot request from the app.

    The plugin directory is normalized without resolving symlinks: a plugin
    directory symlinked into StreamController's plugin folder keeps the
    installed layout that holds its settings.

    StreamController 2.0 settings files wrap the values as
    ``{"file-version": "2.0", "settings": {...}}``; older files are flat.
    A missing file is a fresh install and reads as empty; a file that exists
    but cannot be read or understood raises ``PluginSettingsError``.
    """
    root = Path(os.path.abspath(root))
    path = root.parent.parent / "settings/plugins" / root.name / "settings.json"
    try:
        value = read_bounded_json(path, max_bytes=MAX_SETTINGS_BYTES)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise PluginSettingsError(error.strerror or str(error)) from error
    except ValueError as error:
        raise PluginSettingsError(str(error)) from error
    if not isinstance(value, dict):
        raise PluginSettingsError("the settings document is not an object")
    if "file-version" not in value:
        return value
    version = value["file-version"]
    if version != SETTINGS_FILE_VERSION:
        raise PluginSettingsError(f"unsupported file-version {version}")
    settings = value.get("settings")
    if not isinstance(settings, dict):
        raise PluginSettingsError("the settings envelope has no settings object")
    return settings


def record_settings_error(
    root: Path,
    error: PluginSettingsError,
    *,
    flatpak: bool | None = None,
) -> dict:
    """Record unusable plugin settings as a setup error instead of a disabled feature."""
    if flatpak is None:
        flatpak = _is_flatpak(os.environ)
    profile = "flatpak" if flatpak else "native"
    return _record(Path(root), _status(
        STATE_ERROR, profile, f"cannot read plugin settings: {error}"))


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


def _status(
    state: str,
    profile: str,
    error: object | None = None,
    previous_error: object | None = None,
) -> dict:
    status = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "state": state,
        "profile": profile,
        "error": None if error is None else str(error)[:MAX_ERROR_CHARACTERS],
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if previous_error is not None:
        # Why the first check failed and triggered the install; readers that do
        # not know this field ignore it.
        status["previous_error"] = str(previous_error)[:MAX_ERROR_CHARACTERS]
    return status


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
    except Exception as first:  # noqa: BLE001 - any unusable runtime is installed once
        try:
            install()
            check()
        except Exception as error:  # noqa: BLE001 - reported, never raised
            return _record(root, _status(STATE_ERROR, profile, error, first))
        return _record(root, _status(STATE_INSTALLED, profile, previous_error=first))
    return _record(root, _status(STATE_READY, profile))
