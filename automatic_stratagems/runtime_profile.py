"""Validate the immutable scanner runtime profile and its activation metadata."""

from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from types import MappingProxyType
from typing import Mapping


FLATPAK_INFO = Path("/.flatpak-info")
FLATPAK_PROFILE = "gnome-50-x86_64-cpython-313"
FLATPAK_RUNTIME = "org.gnome.Platform/x86_64/50"
MAX_ACTIVATION_BYTES = 4096
MAX_PROFILE_BYTES = 64 * 1024
PROFILE_DIRECTORY = re.compile(
    rf"profiles/{re.escape(FLATPAK_PROFILE)}-[0-9a-f]{{16,64}}"
)
SCANNER_VENV = "automatic_stratagems/.venv"


class ScanSetupError(RuntimeError):
    """The scanner child runtime is absent, corrupt, or incompatible."""


def _python_abi() -> str:
    return f"cpython-{sys.version_info.major}{sys.version_info.minor}"


def _read_json_document(path: Path, limit: int, description: str) -> tuple[dict, bytes]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                raise ScanSetupError(f"Invalid scanner runtime {description}: {path}")
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > limit:
                raise ScanSetupError(f"Invalid scanner runtime {description}: {path}")
        finally:
            os.close(descriptor)
        raw = bytes(data)
        value = json.loads(raw.decode("utf-8"))
    except ScanSetupError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise ScanSetupError(
            f"Invalid scanner runtime {description}: {path}"
        ) from error
    if not isinstance(value, dict):
        raise ScanSetupError(f"Invalid scanner runtime {description}: {path}")
    return value, raw


def read_json(path: Path, limit: int, description: str) -> dict:
    """Read one bounded, no-follow JSON object."""
    return _read_json_document(path, limit, description)[0]


def _flatpak_platform() -> tuple[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with FLATPAK_INFO.open(encoding="utf-8") as stream:
            parser.read_file(stream)
        runtime = parser.get("Application", "runtime")
        if runtime.startswith("runtime/"):
            runtime = runtime.removeprefix("runtime/")
        return runtime, parser.get("Instance", "arch")
    except (OSError, configparser.Error, KeyError) as error:
        raise ScanSetupError("Cannot read the Flatpak runtime from /.flatpak-info") from error


def validate_flatpak_platform() -> None:
    """Require the runtime and Python ABI used by the locked profile."""
    runtime, architecture = _flatpak_platform()
    if runtime != FLATPAK_RUNTIME or architecture != "x86_64":
        raise ScanSetupError(
            "Scanner runtime requires GNOME 50 x86_64 "
            f"({FLATPAK_RUNTIME}); found {runtime} on {architecture}"
        )
    abi = _python_abi()
    if abi != "cpython-313":
        raise ScanSetupError(
            f"Scanner runtime requires CPython 3.13; found {abi}"
        )


def _regular_file_hash(path: Path) -> str:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ScanSetupError(f"Scanner runtime payload contains a symlink: {path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise ScanSetupError(f"Scanner runtime payload is not a regular file: {path}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except ScanSetupError:
        raise
    except OSError as error:
        raise ScanSetupError(f"Scanner runtime payload cannot be read: {path}") from error


def validate_profile(profile_root: Path) -> tuple[dict, str]:
    """Validate one allowlisted immutable profile and return its manifest hash."""
    try:
        if profile_root.is_symlink() or not profile_root.is_dir():
            raise ScanSetupError(f"Invalid scanner runtime profile: {profile_root}")
        manifest, manifest_bytes = _read_json_document(
            profile_root / "profile.json", MAX_PROFILE_BYTES, "profile manifest"
        )
        if manifest.get("schema_version") != 1 or manifest.get("profile") != FLATPAK_PROFILE:
            raise ScanSetupError(f"Incompatible scanner runtime profile: {profile_root}")
        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries:
            raise ScanSetupError(f"Invalid scanner runtime file list: {profile_root}")
        expected = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ScanSetupError(f"Invalid scanner runtime file entry: {profile_root}")
            relative = entry.get("path")
            digest = entry.get("sha256")
            mode = entry.get("mode")
            if (
                not isinstance(relative, str)
                or not relative
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or relative in expected
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(mode, str)
                or not re.fullmatch(r"0[0-7]{3}", mode)
            ):
                raise ScanSetupError(f"Invalid scanner runtime file entry: {profile_root}")
            expected.add(relative)
            path = profile_root / relative
            if _regular_file_hash(path) != digest:
                raise ScanSetupError(f"Scanner runtime payload hash mismatch: {relative}")
            if stat.S_IMODE(path.stat().st_mode) != int(mode, 8):
                raise ScanSetupError(f"Scanner runtime payload mode mismatch: {relative}")

        actual = set()
        for path in profile_root.rglob("*"):
            if path.is_symlink():
                raise ScanSetupError(f"Scanner runtime payload contains a symlink: {path}")
            if path.is_file():
                actual.add(path.relative_to(profile_root).as_posix())
            elif not path.is_dir():
                raise ScanSetupError(f"Scanner runtime payload has a special file: {path}")
        if actual != expected | {"profile.json"}:
            raise ScanSetupError("Scanner runtime payload does not match its allowlist")
        for name in ("bin", "lib", "python", "tessdata"):
            path = profile_root / name
            if path.is_symlink() or not path.is_dir():
                raise ScanSetupError(f"Scanner runtime payload is missing {name}/")
        return manifest, hashlib.sha256(manifest_bytes).hexdigest()
    except FileNotFoundError as error:
        raise ScanSetupError(f"Scanner runtime payload is incomplete: {error.filename}") from error
    except OSError as error:
        raise ScanSetupError(f"Scanner runtime payload cannot be read: {profile_root}") from error


def validate_activation_pointer(value: dict, description: str) -> None:
    """Validate one active or previous profile pointer."""
    directory = value.get("directory")
    digest = value.get("manifest_sha256")
    if (
        value.get("profile") != FLATPAK_PROFILE
        or not isinstance(directory, str)
        or PROFILE_DIRECTORY.fullmatch(directory) is None
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ScanSetupError(f"Invalid scanner runtime {description}")


def flatpak_child_environment(
    profile_root: Path, environ: Mapping[str, str]
) -> Mapping[str, str]:
    """Build the isolated environment for a child using the locked profile."""
    child_env = dict(environ)
    original_path = child_env.get("PATH", "")
    for name in (
        "PYTHONHOME", "PYTHONPATH", "PYTHONPYCACHEPREFIX", "LD_LIBRARY_PATH"
    ):
        child_env.pop(name, None)
    child_env["PATH"] = f"{profile_root}/bin" + (f":{original_path}" if original_path else "")
    child_env["PYTHONPATH"] = str(profile_root / "python")
    child_env["LD_LIBRARY_PATH"] = str(profile_root / "lib")
    child_env["TESSDATA_PREFIX"] = str(profile_root / "tessdata")
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    return MappingProxyType(child_env)
