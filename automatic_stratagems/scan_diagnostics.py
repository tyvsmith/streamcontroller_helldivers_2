"""Prepare, close, and prune runner-managed scanner diagnostic runs."""

import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
import uuid

from .shared.fs import write_all


MAX_DIAGNOSTIC_RUNS = 20
MAX_DIAGNOSTIC_BYTES = 512 * 1024 * 1024
DIAGNOSTIC_OWNER = "net_jslay_helldivers_2"
CACHE_MARKER = ".hd2-diagnostics-cache.json"
RUN_MARKER = ".hd2-scan-run.json"


def _reject_symlink(path):
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"Diagnostic path contains a symlink: {current}")
    return path


def _read_marker(path):
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
            return None
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _write_marker(path, value, *, create=False):
    payload = (json.dumps(value, sort_keys=True) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    if create:
        flags |= os.O_EXCL
        target = path
    else:
        target = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
        flags |= os.O_EXCL
    descriptor = os.open(target, flags, 0o600)
    try:
        try:
            write_all(descriptor, payload,
                      "short write while updating diagnostic marker")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if not create:
            os.replace(target, path)
    except BaseException:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise


def _process_identity(pid=None):
    pid = os.getpid() if pid is None else pid
    try:
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        start_time = fields[19]
        return {"pid": pid, "boot_id": boot_id, "start_time": start_time}
    except (OSError, IndexError, ValueError):
        return {"pid": pid}


def _process_state(identity):
    """Return match, dead, mismatch, or unknown for a durable process identity."""
    if (not isinstance(identity, dict) or type(identity.get("pid")) is not int or
            identity["pid"] <= 0 or not isinstance(identity.get("boot_id"), str) or
            not identity["boot_id"] or not isinstance(identity.get("start_time"), str) or
            not identity["start_time"].isdigit()):
        return "unknown"
    try:
        if str(uuid.UUID(identity["boot_id"])) != identity["boot_id"].lower():
            return "unknown"
    except ValueError:
        return "unknown"
    try:
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return "unknown"
    if boot_id != identity["boot_id"]:
        return "mismatch"
    try:
        fields = Path(f'/proc/{identity["pid"]}/stat').read_text().rsplit(')', 1)[1].split()
        start_time = fields[19]
    except FileNotFoundError:
        return "dead"
    except (OSError, IndexError, ValueError):
        return "unknown"
    return "match" if start_time == identity["start_time"] else "mismatch"


def prepare_diagnostic_run(cache_path):
    cache = _reject_symlink(cache_path)
    if cache.exists():
        marker = _read_marker(cache / CACHE_MARKER)
        if marker != {"owner": DIAGNOSTIC_OWNER, "version": 1}:
            raise ValueError(f"Diagnostic cache is not owned by this plugin: {cache}")
    else:
        cache.mkdir(parents=True, mode=0o700)
        _write_marker(cache / CACHE_MARKER,
                      {"owner": DIAGNOSTIC_OWNER, "version": 1}, create=True)
    os.chmod(cache, 0o700)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=cache))
    os.chmod(run, 0o700)
    _write_marker(run / RUN_MARKER,
                  {"owner": DIAGNOSTIC_OWNER, "version": 1,
                   "status": "open", "started_at": time.time(),
                   "runner": _process_identity()}, create=True)
    return run


def close_diagnostic_run(run, status):
    marker_path = Path(run) / RUN_MARKER
    marker = _read_marker(marker_path)
    if (not marker or marker.get("owner") != DIAGNOSTIC_OWNER or
            marker.get("status") != "open"):
        raise ValueError(f"Diagnostic run is not open and owned: {run}")
    marker.update(status=status, completed_at=time.time())
    _write_marker(marker_path, marker)


def _owned_run(path):
    if path.is_symlink() or not path.is_dir():
        return None
    marker = _read_marker(path / RUN_MARKER)
    if not marker or marker.get("owner") != DIAGNOSTIC_OWNER:
        return None
    process_state = _process_state(marker.get("runner"))
    if marker.get("status") == "open" and process_state in ("dead", "mismatch"):
        marker.update(status="failed", completed_at=time.time(),
                      reason="abandoned runner")
        _write_marker(path / RUN_MARKER, marker)
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        directories[:] = [name for name in directories
                          if not (Path(root) / name).is_symlink()]
        for name in files:
            item = Path(root) / name
            if not item.is_symlink():
                total += item.stat().st_size
    closed = marker.get("status") in ("complete", "failed")
    return marker.get("completed_at", float("inf")), total, closed


def prune_diagnostics(cache_path):
    cache = _reject_symlink(cache_path)
    if _read_marker(cache / CACHE_MARKER) != {
            "owner": DIAGNOSTIC_OWNER, "version": 1}:
        raise ValueError(f"Diagnostic cache is not owned by this plugin: {cache}")
    runs = []
    count = 0
    total = 0
    for path in cache.iterdir():
        owned = _owned_run(path)
        if owned is not None:
            completed_at, size, closed = owned
            count += 1
            total += size
            if closed:
                runs.append((completed_at, path, size))
    runs.sort()
    while runs and (count > MAX_DIAGNOSTIC_RUNS or
                    total > MAX_DIAGNOSTIC_BYTES):
        _, path, size = runs.pop(0)
        shutil.rmtree(path)
        count -= 1
        total -= size
