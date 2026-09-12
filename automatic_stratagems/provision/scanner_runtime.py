"""Resolve the scanner's child-only native or Flatpak runtime."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import MappingProxyType
from typing import Mapping

from .runtime_profile import (
    FLATPAK_INFO,
    FLATPAK_PROFILE,
    FLATPAK_RUNTIME,
    MAX_ACTIVATION_BYTES,
    MAX_PROFILE_BYTES,
    SCANNER_VENV,
    ScanSetupError,
    flatpak_child_environment,
    read_json,
    validate_activation_pointer,
    validate_flatpak_platform,
    validate_profile,
)

_PYTHON_PROFILES = {
    FLATPAK_PROFILE: (
        ("numpy", "numpy", "2.2.3"),
        ("opencv-python", "cv2", "4.11.0.86"),
        ("pillow", "PIL", "11.1.0"),
        ("evdev", "evdev", "1.9.1"),
    ),
    "native": (
        ("numpy", "numpy", "2.5.3"),
        ("opencv-python-headless", "cv2", "5.0.0.93"),
        ("pillow", "PIL", "12.3.0"),
        ("evdev", "evdev", "2.0.0"),
    ),
}


@dataclass(frozen=True)
class ScannerRuntime:
    interpreter: Path
    env: Mapping[str, str]
    profile: str
    runtime_root: Path | None


def _resolve_flatpak(root: Path, environ: Mapping[str, str]) -> ScannerRuntime:
    validate_flatpak_platform()
    runtime_dir = root / "automatic_stratagems/runtime"
    activation = read_json(
        runtime_dir / "active.json", MAX_ACTIVATION_BYTES, "activation record"
    )
    if activation.get("schema_version") != 1:
        raise ScanSetupError("Invalid scanner runtime activation record")
    validate_activation_pointer(activation, "activation record")
    directory = activation["directory"]
    manifest_digest = activation["manifest_sha256"]
    profile_root = runtime_dir / directory
    _, actual_manifest_digest = validate_profile(profile_root)
    if actual_manifest_digest != manifest_digest:
        raise ScanSetupError("Scanner runtime manifest hash mismatch")
    child_env = flatpak_child_environment(profile_root, environ)
    return ScannerRuntime(
        interpreter=Path(sys.executable),
        env=child_env,
        profile=FLATPAK_PROFILE,
        runtime_root=profile_root,
    )


def resolve_scanner_runtime(
    root: Path,
    *,
    flatpak: bool | None = None,
    environ: Mapping[str, str] | None = None,
) -> ScannerRuntime:
    """Resolve and statically validate the native or Flatpak child runtime."""
    root = Path(root)
    source_env = os.environ if environ is None else environ
    if flatpak is None:
        flatpak = bool(source_env.get("FLATPAK_ID")) or FLATPAK_INFO.exists()
    if flatpak:
        return _resolve_flatpak(root, source_env)

    interpreter = root / SCANNER_VENV / "bin/python"
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise ScanSetupError(
            f"Scanner Python is missing at {interpreter}; install "
            "automatic_stratagems/requirements.txt into that environment"
        )
    return ScannerRuntime(
        interpreter=interpreter,
        env=MappingProxyType(dict(source_env)),
        profile="native",
        runtime_root=None,
    )


def scanner_preflight_command(
    root: Path, runtime: ScannerRuntime
) -> tuple[str, ...]:
    """Return the bounded child command used by check_scan_setup."""
    return (
        str(runtime.interpreter),
        "-m",
        "automatic_stratagems.provision.scanner_runtime",
        "--preflight",
        "--root",
        str(Path(root)),
        f"--profile={runtime.profile}",
    )


def verify_python_dependencies(profile: str) -> dict[str, str]:
    """Import and verify the exact dependency set in the scanner child."""
    import importlib.metadata

    expected = _PYTHON_PROFILES.get(profile)
    if expected is None:
        raise ScanSetupError(f"Unknown scanner dependency profile: {profile}")
    versions = {}
    for distribution, module, wanted in expected:
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as error:
            raise ScanSetupError(f"Missing scanner dependency: {distribution}") from error
        if actual != wanted:
            raise ScanSetupError(
                f"Scanner dependency {distribution} requires {wanted}; found {actual}"
            )
        try:
            importlib.import_module(module)
        except Exception as error:
            raise ScanSetupError(
                f"Scanner dependency {distribution} cannot be imported: {error}"
            ) from error
        versions[distribution] = actual
    return versions


def run_captured(
    command: list[str] | tuple[str, ...],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float,
    limit: int = 64 * 1024,
) -> tuple[int, bytes, bytes]:
    """Run a trusted fixed child with bounded memory and bounded retained output."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout,
            check=False,
        )
        stdout_size = os.fstat(stdout.fileno()).st_size
        stderr_size = os.fstat(stderr.fileno()).st_size
        if stdout_size > limit or stderr_size > limit:
            raise ScanSetupError(
                f"Scanner runtime child output exceeded {limit // 1024} KiB"
            )
        stdout.seek(0)
        stderr.seek(0)
        return result.returncode, stdout.read(limit + 1), stderr.read(limit + 1)


def _run_tesseract(command: list[str]) -> str:
    try:
        returncode, stdout, stderr = run_captured(command, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ScanSetupError(f"Tesseract self-test failed: {error}") from error
    if returncode != 0:
        message = stderr[:4096].decode("utf-8", "replace").strip()
        raise ScanSetupError(f"Tesseract self-test failed: {message or returncode}")
    return stdout.decode("utf-8", "replace")


def verify_tesseract(
    executable: Path,
    image: Path,
    *,
    expected_version: str | None,
    require_only_english: bool = True,
) -> dict:
    """Execute the packaged OCR engine on a deterministic known image."""
    executable = Path(executable)
    version_output = _run_tesseract([str(executable), "--version"])
    first_line = version_output.splitlines()[0] if version_output.splitlines() else ""
    match = re.fullmatch(r"tesseract ([0-9]+(?:\.[0-9]+){2})", first_line.strip())
    if match is None:
        raise ScanSetupError("Tesseract self-test returned an invalid version")
    version = match.group(1)
    if expected_version is not None and version != expected_version:
        raise ScanSetupError(
            f"Tesseract requires {expected_version}; found {version}"
        )

    languages_output = _run_tesseract([str(executable), "--list-langs"])
    languages = sorted(
        line.strip() for line in languages_output.splitlines()[1:] if line.strip()
    )
    if (require_only_english and languages != ["eng"]) or "eng" not in languages:
        raise ScanSetupError(
            "Tesseract requires English trained data"
            + (" only" if require_only_english else "")
            + f"; found {languages}"
        )

    ocr_output = _run_tesseract([
        str(executable), str(Path(image)), "stdout", "--psm", "7", "-l", "eng"
    ])
    recognized = " ".join(ocr_output.upper().split())
    if recognized != "RESUPPLY":
        raise ScanSetupError(
            f"Tesseract OCR self-test expected RESUPPLY; found {recognized or 'no text'}"
        )
    return {"version": version, "languages": languages, "ocr": recognized}


def child_preflight(root: Path, profile: str) -> dict:
    """Run exact import and OCR validation inside the scanner child."""
    root = Path(root)
    versions = verify_python_dependencies(profile)
    executable = shutil.which("tesseract")
    if executable is None and profile == FLATPAK_PROFILE:
        raise ScanSetupError("Tesseract is missing from the scanner child PATH")
    if executable is None:
        ocr = None
    else:
        expected_tesseract = "5.5.0" if profile == FLATPAK_PROFILE else None
        try:
            ocr = verify_tesseract(
                Path(executable),
                root / "automatic_stratagems/runtime_profiles/preflight.png",
                expected_version=expected_tesseract,
                require_only_english=profile == FLATPAK_PROFILE,
            )
        except ScanSetupError:
            if profile == FLATPAK_PROFILE:
                raise
            ocr = None
    return {
        "schema_version": 1,
        "profile": profile,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "packages": versions,
        "tesseract": ocr,
    }


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Validate the scanner child runtime")
    parser.add_argument("--preflight", action="store_true", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    arguments = parser.parse_args(argv)
    try:
        report = child_preflight(arguments.root, arguments.profile)
    except ScanSetupError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
