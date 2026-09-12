"""Revalidate the scanner's active runtime and run its child preflight."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import subprocess
import sys

from .runtime_profile import (
    FLATPAK_PROFILE,
    ScanSetupError,
    flatpak_child_environment,
    validate_flatpak_platform,
    validate_profile,
)
from .scanner_runtime import (
    ScannerRuntime,
    resolve_scanner_runtime,
    run_captured,
    scanner_preflight_command,
)


Preflight = Callable[[Path, dict], None]


def _run_preflight(root: Path, runtime: ScannerRuntime) -> None:
    command = scanner_preflight_command(root, runtime)
    try:
        returncode, _stdout, stderr = run_captured(
            command,
            cwd=root,
            env=runtime.env,
            timeout=30,
        )
    except ScanSetupError:
        raise
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ScanSetupError(f"Scanner runtime child preflight failed: {error}") from error
    if returncode != 0:
        message = stderr[:4096].decode("utf-8", "replace").strip()
        raise ScanSetupError(
            f"Scanner runtime child preflight failed: {message or returncode}"
        )


def _default_preflight(root: Path, profile_root: Path, _manifest: dict) -> None:
    validate_flatpak_platform()
    child_env = flatpak_child_environment(profile_root, os.environ)
    runtime = ScannerRuntime(
        interpreter=Path(sys.executable),
        env=child_env,
        profile=FLATPAK_PROFILE,
        runtime_root=profile_root,
    )
    _run_preflight(root, runtime)


def _require_profile_unchanged(profile_root: Path, expected_hash: str) -> None:
    try:
        _, checked_hash = validate_profile(profile_root)
    except ScanSetupError as error:
        raise ScanSetupError(
            "Scanner runtime changed during child preflight"
        ) from error
    if checked_hash != expected_hash:
        raise ScanSetupError("Scanner runtime changed during child preflight")


def check_runtime(
    root: Path,
    *,
    flatpak: bool | None = None,
    environ: dict[str, str] | None = None,
    preflight: Preflight | None = None,
) -> Path | None:
    """Revalidate the active profile and execute its child preflight."""
    root = Path(root)
    runtime = resolve_scanner_runtime(
        root, flatpak=flatpak, environ=environ
    )
    if runtime.runtime_root is None:
        manifest = {"schema_version": 1, "profile": "native"}
        manifest_hash = None
    else:
        manifest, manifest_hash = validate_profile(runtime.runtime_root)
    if preflight is None:
        _run_preflight(root, runtime)
    else:
        preflight(runtime.runtime_root, manifest)
    if runtime.runtime_root is not None:
        _require_profile_unchanged(runtime.runtime_root, manifest_hash)
    return runtime.runtime_root
