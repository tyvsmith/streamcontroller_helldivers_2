"""Build and atomically activate the scanner's locked Flatpak payload."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import urllib.request
import uuid
import zipfile

from .runtime_profile import (
    FLATPAK_PROFILE,
    MAX_ACTIVATION_BYTES,
    MAX_PROFILE_BYTES,
    ScanSetupError,
    atomic_json,
    read_json,
    validate_activation_pointer,
    validate_profile,
)
from ..shared.fs import canonical_json, fsync_directory, write_all
from .verify import Preflight, _default_preflight, _require_profile_unchanged


DEFAULT_LOCK = Path(__file__).resolve().parents[1] / "runtime_profiles" / f"{FLATPAK_PROFILE}.json"
MAX_LOCK_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_lock(path: Path) -> tuple[dict, str]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_LOCK_BYTES:
            raise ScanSetupError(f"Invalid scanner runtime source lock: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ScanSetupError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise ScanSetupError(f"Invalid scanner runtime source lock: {path}") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("profile") != FLATPAK_PROFILE
        or not isinstance(value.get("sources"), list)
        or not isinstance(value.get("files"), list)
    ):
        raise ScanSetupError(f"Invalid scanner runtime source lock: {path}")
    canonical = canonical_json(value)
    return value, _sha256(canonical)


def _verified_source(path: Path, source: dict) -> Path:
    expected_size = source.get("bytes")
    expected_hash = source.get("sha256")
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ScanSetupError(f"Scanner runtime source is not a regular file: {path}")
        if metadata.st_size != expected_size or metadata.st_size > MAX_SOURCE_BYTES:
            raise ScanSetupError(f"Scanner runtime source hash/size mismatch: {path.name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except ScanSetupError:
        raise
    except OSError as error:
        raise ScanSetupError(f"Cannot read scanner runtime source: {path}") from error
    if not isinstance(expected_hash, str) or _HASH.fullmatch(expected_hash) is None:
        raise ScanSetupError(f"Invalid source hash in runtime lock: {path.name}")
    if digest != expected_hash:
        raise ScanSetupError(f"Scanner runtime source hash mismatch: {path.name}")
    return path


def _download(source: dict, destination: Path) -> Path:
    url = source.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ScanSetupError(f"Invalid scanner runtime source URL: {url}")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open("xb") as output:
            remaining = MAX_SOURCE_BYTES + 1
            while remaining:
                chunk = response.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                output.write(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if remaining == 0:
            raise ScanSetupError(f"Scanner runtime source exceeds size limit: {url}")
        _verified_source(temporary, source)
        os.replace(temporary, destination)
        return destination
    except ScanSetupError:
        raise
    except (OSError, urllib.error.URLError) as error:
        raise ScanSetupError(f"Cannot download scanner runtime source: {url}") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _source_path(
    runtime_dir: Path,
    source: dict,
    artifact_dir: Path | None,
    offline: bool,
) -> Path:
    filename = source.get("filename")
    if (
        not isinstance(filename, str)
        or not filename
        or filename != Path(filename).name
    ):
        raise ScanSetupError("Invalid source filename in runtime lock")
    if artifact_dir is not None:
        candidate = artifact_dir / filename
        if candidate.exists() or offline:
            return _verified_source(candidate, source)
    cache = runtime_dir / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    candidate = cache / filename
    if candidate.exists():
        return _verified_source(candidate, source)
    if offline:
        raise ScanSetupError(f"Offline scanner runtime source is missing: {filename}")
    return _download(source, candidate)


def _ar_member(data: bytes, wanted: str) -> bytes:
    if not data.startswith(b"!<arch>\n"):
        raise ScanSetupError("Invalid Debian package archive")
    offset = 8
    while offset < len(data):
        if offset + 60 > len(data):
            raise ScanSetupError("Truncated Debian package archive")
        header = data[offset:offset + 60]
        if header[58:60] != b"`\n":
            raise ScanSetupError("Invalid Debian package member header")
        name = header[:16].decode("ascii", "strict").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError as error:
            raise ScanSetupError("Invalid Debian package member size") from error
        start = offset + 60
        end = start + size
        if end > len(data):
            raise ScanSetupError("Truncated Debian package member")
        if name == wanted:
            return data[start:end]
        offset = end + (size % 2)
    raise ScanSetupError(f"Debian package is missing {wanted}")


def _deb_file(path: Path, member_name: str) -> bytes:
    try:
        payload = _ar_member(path.read_bytes(), "data.tar.xz")
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz") as archive:
            wanted = member_name.lstrip("./")
            matches = [
                member for member in archive.getmembers()
                if member.name.lstrip("./") == wanted
            ]
            if len(matches) != 1 or not matches[0].isfile():
                raise ScanSetupError(
                    f"Debian payload member is not one regular file: {member_name}"
                )
            extracted = archive.extractfile(matches[0])
            if extracted is None:
                raise ScanSetupError(f"Cannot read Debian payload member: {member_name}")
            return extracted.read(MAX_SOURCE_BYTES + 1)
    except ScanSetupError:
        raise
    except (OSError, tarfile.TarError, UnicodeError) as error:
        raise ScanSetupError(f"Cannot extract Debian payload: {path.name}") from error


def _wheel_file(path: Path, member_name: str) -> bytes:
    try:
        with zipfile.ZipFile(path) as archive:
            matches = [info for info in archive.infolist() if info.filename == member_name]
            if len(matches) != 1:
                raise ScanSetupError(f"Wheel payload member is missing: {member_name}")
            info = matches[0]
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if (
                info.is_dir()
                or stat.S_ISLNK(mode)
                or file_type not in (0, stat.S_IFREG)
            ):
                raise ScanSetupError(
                    f"Wheel payload member is not one regular file: {member_name}"
                )
            with archive.open(info) as stream:
                return stream.read(MAX_SOURCE_BYTES + 1)
    except ScanSetupError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise ScanSetupError(f"Cannot extract wheel payload: {path.name}") from error


def _payload_bytes(source_path: Path, kind: str, member: str) -> bytes:
    if kind == "deb":
        return _deb_file(source_path, member)
    if kind == "wheel":
        return _wheel_file(source_path, member)
    raise ScanSetupError(f"Unsupported scanner runtime source kind: {kind}")


def _write_payload(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        write_all(descriptor, data, "short write while building scanner runtime")
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _profile_manifest(lock: dict, lock_hash: str) -> dict:
    return {
        "schema_version": 1,
        "profile": FLATPAK_PROFILE,
        "source_lock_sha256": lock_hash,
        "compatibility": lock.get("compatibility"),
        "python_packages": lock.get("python_packages"),
        "licenses": lock.get("licenses"),
        "files": [
            {key: entry[key] for key in ("path", "sha256", "mode")}
            for entry in lock["files"]
        ],
    }


def _validate_file_entry(entry: dict, source_names: set[str], seen: set[str]) -> None:
    if not isinstance(entry, dict):
        raise ScanSetupError("Invalid scanner runtime output entry")
    path = entry.get("path")
    member = entry.get("member")
    source = entry.get("source")
    digest = entry.get("sha256")
    mode = entry.get("mode")
    if (
        not isinstance(path, str)
        or not path
        or Path(path).is_absolute()
        or ".." in Path(path).parts
        or path in seen
        or not isinstance(member, str)
        or not member
        or source not in source_names
        or not isinstance(digest, str)
        or _HASH.fullmatch(digest) is None
        or not isinstance(mode, str)
        or re.fullmatch(r"0[0-7]{3}", mode) is None
        or type(entry.get("bytes")) is not int
        or not 0 <= entry["bytes"] <= MAX_SOURCE_BYTES
    ):
        raise ScanSetupError("Invalid scanner runtime output entry")
    seen.add(path)


def _activation_for(profile_root: Path, manifest_hash: str, previous: dict | None) -> dict:
    value = {
        "schema_version": 1,
        "profile": FLATPAK_PROFILE,
        "directory": "profiles/" + profile_root.name,
        "manifest_sha256": manifest_hash,
    }
    if previous is not None:
        value["previous"] = {
            key: previous[key]
            for key in ("profile", "directory", "manifest_sha256")
        }
    return value


def _read_activation(path: Path, *, required: bool) -> dict | None:
    if not path.exists() and not required:
        return None
    value = read_json(path, MAX_ACTIVATION_BYTES, "activation record")
    validate_activation_pointer(value, "activation record")
    if "previous" in value:
        if not isinstance(value["previous"], dict):
            raise ScanSetupError("Invalid scanner runtime activation record")
        validate_activation_pointer(value["previous"], "previous activation")
    return value


STAGE_LOCK_SUFFIX = ".lock"


def _try_lock(descriptor: int) -> bool:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _claim_stage(staging_dir: Path) -> tuple[Path, int]:
    """Create a stage that its install owns for as long as it holds the lock.

    The lock file exists and is held before the stage directory does, so a
    stage without a lock file was abandoned. It is removed only after the stage.
    """
    while True:
        stage = staging_dir / f"{FLATPAK_PROFILE}-{uuid.uuid4().hex}"
        lock = staging_dir / f"{stage.name}{STAGE_LOCK_SUFFIX}"
        descriptor = os.open(
            lock, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        try:
            if _try_lock(descriptor):
                # A sweep may have unlinked the file between creation and flock.
                held = os.fstat(descriptor)
                try:
                    current = lock.lstat()
                except FileNotFoundError:
                    current = None
                if current is not None and (current.st_dev, current.st_ino) == (
                    held.st_dev, held.st_ino
                ):
                    stage.mkdir(mode=0o700)
                    return stage, descriptor
        except BaseException:
            os.close(descriptor)
            raise
        os.close(descriptor)


def _release_stage(stage: Path, descriptor: int) -> None:
    try:
        if stage.exists():
            shutil.rmtree(stage)
        (stage.parent / f"{stage.name}{STAGE_LOCK_SUFFIX}").unlink(missing_ok=True)
    finally:
        os.close(descriptor)


def _remove_entry(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        pass


def sweep_stale_staging(staging_dir: Path) -> None:
    """Remove stages and stage locks that no running install holds."""
    try:
        entries = list(staging_dir.iterdir())
    except FileNotFoundError:
        return
    for entry in entries:
        if entry.name.endswith(STAGE_LOCK_SUFFIX):
            stage = entry.with_name(entry.name[:-len(STAGE_LOCK_SUFFIX)])
            lock = entry
        else:
            stage = entry
            lock = entry.with_name(entry.name + STAGE_LOCK_SUFFIX)
        try:
            descriptor = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            descriptor = None
        except OSError:
            continue
        try:
            if descriptor is not None and not _try_lock(descriptor):
                continue
            if stage.exists() or stage.is_symlink():
                _remove_entry(stage)
            if descriptor is not None:
                lock.unlink(missing_ok=True)
        except OSError:
            continue
        finally:
            if descriptor is not None:
                os.close(descriptor)


def install_runtime(
    root: Path,
    *,
    lock_path: Path = DEFAULT_LOCK,
    artifact_dir: Path | None = None,
    offline: bool = False,
    preflight: Preflight | None = None,
) -> Path:
    """Build, validate, and activate one immutable scanner runtime profile."""
    root = Path(root)
    lock, lock_hash = _read_lock(Path(lock_path))
    runtime_dir = root / "automatic_stratagems/runtime"
    profiles_dir = runtime_dir / "profiles"
    staging_dir = runtime_dir / "staging"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    sweep_stale_staging(staging_dir)
    target = profiles_dir / f"{FLATPAK_PROFILE}-{lock_hash}"
    manifest = _profile_manifest(lock, lock_hash)
    manifest_bytes = canonical_json(manifest)
    if len(manifest_bytes) > MAX_PROFILE_BYTES:
        raise ScanSetupError("Scanner runtime profile manifest is too large")
    manifest_hash = _sha256(manifest_bytes)
    preflight = preflight or (lambda profile_root, value: _default_preflight(
        root, profile_root, value
    ))

    activation_path = runtime_dir / "active.json"
    previous = _read_activation(activation_path, required=False)
    if target.exists():
        existing_manifest, existing_hash = validate_profile(target)
        if existing_hash != manifest_hash:
            raise ScanSetupError("Existing scanner runtime manifest hash mismatch")
        preflight(target, existing_manifest)
        _, checked_hash = validate_profile(target)
        if checked_hash != manifest_hash:
            raise ScanSetupError("Scanner runtime changed during child preflight")
        if previous is None or previous.get("directory") != "profiles/" + target.name:
            atomic_json(
                activation_path, _activation_for(target, manifest_hash, previous)
            )
        return target

    sources = {}
    source_specs = {}
    for source in lock["sources"]:
        if not isinstance(source, dict) or not isinstance(source.get("name"), str):
            raise ScanSetupError("Invalid scanner runtime source entry")
        name = source["name"]
        if name in sources:
            raise ScanSetupError(f"Duplicate scanner runtime source: {name}")
        sources[name] = _source_path(
            runtime_dir,
            source,
            Path(artifact_dir) if artifact_dir is not None else None,
            offline,
        )
        source_specs[name] = source

    seen = set()
    for entry in lock["files"]:
        _validate_file_entry(entry, set(sources), seen)

    stage, stage_lock = _claim_stage(staging_dir)
    try:
        (stage / "python").mkdir()
        for entry in lock["files"]:
            source = source_specs[entry["source"]]
            data = _payload_bytes(
                sources[entry["source"]], source.get("kind"), entry["member"]
            )
            if len(data) != entry["bytes"] or _sha256(data) != entry["sha256"]:
                raise ScanSetupError(
                    f"Scanner runtime output hash mismatch: {entry['path']}"
                )
            _write_payload(stage / entry["path"], data, int(entry["mode"], 8))
        _write_payload(stage / "profile.json", manifest_bytes, 0o644)
        staged_manifest, staged_hash = validate_profile(stage)
        if staged_hash != manifest_hash:
            raise ScanSetupError("Staged scanner runtime manifest hash mismatch")
        preflight(stage, staged_manifest)
        _, checked_hash = validate_profile(stage)
        if checked_hash != manifest_hash:
            raise ScanSetupError("Scanner runtime changed during child preflight")
        os.replace(stage, target)
        fsync_directory(profiles_dir)
        atomic_json(activation_path, _activation_for(target, manifest_hash, previous))
        return target
    finally:
        _release_stage(stage, stage_lock)


def rollback_runtime(
    root: Path,
    *,
    preflight: Preflight | None = None,
) -> Path:
    """Atomically switch to the previously activated verified profile."""
    root = Path(root)
    runtime_dir = root / "automatic_stratagems/runtime"
    activation_path = runtime_dir / "active.json"
    current = _read_activation(activation_path, required=True)
    previous = current.get("previous")
    if not isinstance(previous, dict):
        raise ScanSetupError("No previous scanner runtime profile is available")
    directory = previous.get("directory")
    if (
        previous.get("profile") != FLATPAK_PROFILE
        or not isinstance(directory, str)
        or not directory.startswith("profiles/")
        or ".." in Path(directory).parts
        or not isinstance(previous.get("manifest_sha256"), str)
        or _HASH.fullmatch(previous["manifest_sha256"]) is None
    ):
        raise ScanSetupError("Invalid previous scanner runtime activation")
    profile_root = runtime_dir / directory
    manifest, manifest_hash = validate_profile(profile_root)
    if manifest_hash != previous["manifest_sha256"]:
        raise ScanSetupError("Previous scanner runtime manifest hash mismatch")
    if preflight is None:
        _default_preflight(root, profile_root, manifest)
    else:
        preflight(profile_root, manifest)
    _require_profile_unchanged(profile_root, previous["manifest_sha256"])
    new_activation = _activation_for(profile_root, previous["manifest_sha256"], current)
    atomic_json(activation_path, new_activation)
    return profile_root
