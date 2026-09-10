"""Launch, bound, cancel, and validate the host scanner process."""

from concurrent.futures import CancelledError
import json
import logging
import os
from pathlib import Path
import signal
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid

from .scanner.capture_environment import session_kind


SCHEMA_VERSION = 1
SCAN_TIMEOUT_SECONDS = 120
SCANNER_BUDGET_SECONDS = 110
TERMINATE_GRACE_SECONDS = 1
FLATPAK_TERMINATE_GRACE_SECONDS = 10
OUTPUT_DRAIN_SECONDS = .1
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
MAX_REPORT_ROWS = 32
MAX_REPORT_STRING = 4096
MAX_DIAGNOSTIC_RUNS = 20
MAX_DIAGNOSTIC_BYTES = 512 * 1024 * 1024
DIAGNOSTIC_OWNER = "net_jslay_helldivers_2"
CACHE_MARKER = ".hd2-diagnostics-cache.json"
RUN_MARKER = ".hd2-scan-run.json"
log = logging.getLogger(__name__)


class ScanSetupError(RuntimeError):
    """The optional host scanner environment is missing or incomplete."""


def scan_workers(value=2):
    try:
        return max(1, min(32, int(value))) if not isinstance(value, bool) else 2
    except (ValueError, TypeError, OverflowError):
        return 2


def is_flatpak():
    return bool(os.environ.get("FLATPAK_ID")) or Path("/.flatpak-info").exists()


def _host_command(root, command, flatpak):
    if flatpak:
        return ["flatpak-spawn", "--host", "--watch-bus",
                f"--directory={root}", *command]
    return command


def scan_command(root, *, flatpak=None, backend="auto", workers=2, debug_dir=None):
    root = Path(root)
    flatpak = is_flatpak() if flatpak is None else flatpak
    command = [str(root / ".venv/bin/python"), "-m",
               "automatic_stratagems.scanner", "--json", "--budget-seconds",
               str(SCANNER_BUDGET_SECONDS), "--workers",
               str(scan_workers(workers)), "--capture-backend", backend]
    if debug_dir is not None:
        command.extend(["--debug-dir", str(debug_dir)])
    return _host_command(root, command, flatpak)


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
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if not create:
        os.replace(target, path)


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


def _cancelled(cancel_event):
    return cancel_event is not None and cancel_event.is_set()


def _read_bounded(stream, limit, name, output, overflow):
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            available = max(0, limit - len(output))
            output.extend(chunk[:available])
            if len(chunk) > available:
                overflow.append(name)
                return
    finally:
        stream.close()


def _group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _group_has_live_members(pgid):
    proc = Path('/proc')
    if not proc.is_dir():
        return _group_exists(pgid)
    unknown = False
    try:
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) == pgid and fields[0] != 'Z':
                    return True
            except FileNotFoundError:
                continue
            except (OSError, IndexError, ValueError):
                unknown = True
    except OSError:
        unknown = True
    return _group_exists(pgid) if unknown else False


def _stop_and_reap(process, grace=TERMINATE_GRACE_SECONDS,
                   stop_signal=signal.SIGTERM):
    """Terminate and wait for confirmed process-group exit before returning."""
    signal_error = None
    try:
        os.killpg(process.pid, stop_signal)
    except ProcessLookupError:
        pass
    except OSError as error:
        signal_error = error
    deadline = time.monotonic() + grace
    while _group_has_live_members(process.pid) and time.monotonic() < deadline:
        time.sleep(.01)
    if _group_has_live_members(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            signal_error = signal_error or error
        deadline = time.monotonic() + grace
        while (_group_has_live_members(process.pid) and
               time.monotonic() < deadline):
            process.poll()
            time.sleep(.01)
        if _group_has_live_members(process.pid):
            log.error(
                "Scanner process group %s remains active after SIGKILL; "
                "input exclusion remains until exit is confirmed.", process.pid)
        while _group_has_live_members(process.pid):
            process.poll()
            time.sleep(.01)
    process.wait()
    if signal_error is not None:
        raise RuntimeError(f"Scanner process group signaling failed: {signal_error}") from signal_error


def _run_owned(command, *, cwd, timeout, cancel_event=None,
               stdout_limit=MAX_STDOUT_BYTES, stderr_limit=MAX_STDERR_BYTES):
    if _cancelled(cancel_event):
        raise CancelledError()
    try:
        process = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True)
    except FileNotFoundError as error:
        raise ScanSetupError(f"Missing scanner command: {command[0]}") from error

    stdout = bytearray()
    stderr = bytearray()
    overflow = []
    readers = [
        threading.Thread(target=_read_bounded,
                         args=(process.stdout, stdout_limit, "stdout", stdout, overflow),
                         daemon=True),
        threading.Thread(target=_read_bounded,
                         args=(process.stderr, stderr_limit, "stderr", stderr, overflow),
                         daemon=True),
    ]
    deadline = time.monotonic() + timeout
    failure = None
    started = []
    terminate_grace = (FLATPAK_TERMINATE_GRACE_SECONDS
                       if Path(command[0]).name == 'flatpak-spawn'
                       else TERMINATE_GRACE_SECONDS)
    # flatpak-spawn forwards SIGINT to the host process group. It forwards
    # SIGTERM only to the direct child, which can orphan capture/OCR helpers.
    stop_signal = (signal.SIGINT if Path(command[0]).name == 'flatpak-spawn'
                   else signal.SIGTERM)
    try:
        for reader in readers:
            reader.start()
            started.append(reader)
        while process.poll() is None:
            if _cancelled(cancel_event):
                failure = CancelledError()
                break
            if overflow:
                failure = ValueError(f"Scanner {overflow[0]} exceeded its byte limit")
                break
            if time.monotonic() >= deadline:
                failure = TimeoutError(f"Scanner timed out after {timeout:g} seconds")
                break
            time.sleep(.02)
    finally:
        cleanup_error = None
        try:
            if process.poll() is None or _group_has_live_members(process.pid):
                _stop_and_reap(process, terminate_grace, stop_signal)
        except BaseException as error:
            cleanup_error = error
        finally:
            for reader, stream in zip(readers, (process.stdout, process.stderr)):
                if reader not in started:
                    stream.close()
            for reader in started:
                reader.join(OUTPUT_DRAIN_SECONDS)
        if any(reader.is_alive() for reader in started):
            try:
                _stop_and_reap(process, terminate_grace, stop_signal)
            except BaseException as error:
                cleanup_error = cleanup_error or error
            for reader in started:
                reader.join(terminate_grace)
        if any(reader.is_alive() for reader in started) and cleanup_error is None:
            cleanup_error = RuntimeError(
                "Scanner process group did not close its output streams")
    if failure is not None:
        raise failure
    if cleanup_error is not None:
        raise cleanup_error
    if overflow:
        raise ValueError(f"Scanner {overflow[0]} exceeded its byte limit")
    return process.returncode, bytes(stdout), bytes(stderr)


def check_scan_setup(root, *, flatpak=None, backend="auto", cancel_event=None):
    """Check the optional host interpreter, Python modules, and fixed helper."""
    root = Path(root)
    flatpak = is_flatpak() if flatpak is None else flatpak
    if flatpak:
        raise ScanSetupError(
            "Automatic scanning is unavailable in Flatpak because forced "
            "host process-tree teardown is not confirmed safe.")
    interpreter = root / ".venv/bin/python"
    if not flatpak and not interpreter.is_file():
        raise ScanSetupError(
            f"Automatic scanning is not set up. Create {root / '.venv'} and install "
            f"{root / 'automatic_stratagems/requirements.txt'}.")
    platform = session_kind(os.environ)
    x11 = platform == "x11"
    portal = backend == "portal" or (backend == "auto" and
                                      platform == "wayland")
    if portal:
        helpers = ("gst-launch-1.0",)
    elif backend == "x11":
        helpers = ("xprop", "import")
    elif backend == "auto" and x11:
        helpers = ("xprop",)
    elif backend == "desktop":
        helpers = ("hyprctl", "grim")
    elif backend == "gamescope":
        helpers = (("xprop", "gamescopectl") if x11 else
                   ("hyprctl", "gamescopectl"))
    elif backend == "steam":
        helpers = ("xprop",) if x11 else ("hyprctl",)
    else:
        helpers = ("hyprctl",)
    optional_import = ",dbus_next" if portal else ""
    script = (
        f"import cv2,numpy,PIL,evdev,shutil,sys{optional_import};"
        f"missing=[x for x in {helpers!r} if shutil.which(x) is None];"
        "sys.stderr.write('Missing capture helper: '+', '.join(missing) if missing else '');"
        "sys.exit(bool(missing))")
    command = _host_command(root, [str(interpreter), "-c", script], flatpak)
    code, _, stderr = _run_owned(
        command, cwd=root, timeout=10, cancel_event=cancel_event,
        stdout_limit=1024, stderr_limit=16 * 1024)
    if code:
        detail = stderr.decode(errors="replace").strip()
        raise ScanSetupError(
            detail or
            f"Scanner dependencies are unavailable; install "
            f"{root / 'automatic_stratagems/requirements.txt'}.")


def _bounded_string(value, field):
    if not isinstance(value, str) or len(value) > MAX_REPORT_STRING:
        raise ValueError(f"Scanner returned an invalid {field}")


def validate_report(report):
    if not isinstance(report, dict):
        raise ValueError("Scanner returned an invalid report")
    if (type(report.get("schema_version")) is not int or
            report["schema_version"] != SCHEMA_VERSION):
        raise ValueError("Scanner returned an invalid schema version")
    if report.get("status") not in ("matched", "partial", "no_detections", "error"):
        raise ValueError("Scanner returned an invalid status")
    rows = report.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_REPORT_ROWS:
        raise ValueError("Scanner returned invalid rows")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Scanner returned invalid rows")
        key = row.get("id")
        if key is not None:
            _bounded_string(key, "row id")
        if "name" in row:
            _bounded_string(row["name"], "row name")
        sequence = row.get("sequence")
        if sequence is not None and (
                not isinstance(sequence, list) or len(sequence) > 16 or
                any(direction not in ("UP", "DOWN", "LEFT", "RIGHT")
                    for direction in sequence)):
            raise ValueError("Scanner returned an invalid sequence")
        box = row.get("box")
        if box is not None and (
                not isinstance(box, list) or len(box) != 4 or
                any(type(number) is not int or number < 0 for number in box)):
            raise ValueError("Scanner returned an invalid row box")
    warnings = report.get("warnings")
    if not isinstance(warnings, list) or len(warnings) > MAX_REPORT_ROWS or any(
            not isinstance(item, str) or len(item) > MAX_REPORT_STRING
            for item in warnings):
        raise ValueError("Scanner returned invalid warnings")
    return report


def run_scan(root, backend="auto", workers=2, *, cancel_event=None, debug_dir=None):
    """Return one validated report after all owned host work has exited."""
    if _cancelled(cancel_event):
        raise CancelledError()
    flatpak = is_flatpak()
    check_scan_setup(root, flatpak=flatpak, backend=backend,
                     cancel_event=cancel_event)
    diagnostic_run = prepare_diagnostic_run(debug_dir) if debug_dir else None
    outcome = "failed"
    try:
        command = scan_command(root, flatpak=flatpak, backend=backend,
                               workers=workers, debug_dir=diagnostic_run)
        code, stdout, stderr = _run_owned(
            command, cwd=Path(root), timeout=SCAN_TIMEOUT_SECONDS,
            cancel_event=cancel_event, stdout_limit=MAX_STDOUT_BYTES,
            stderr_limit=MAX_STDERR_BYTES)
        outcome = "complete"
    except BaseException:
        if diagnostic_run is not None:
            try:
                close_diagnostic_run(diagnostic_run, outcome)
            except Exception:
                log.warning("Unable to close scanner diagnostics after failure.",
                            exc_info=True)
            try:
                prune_diagnostics(debug_dir)
            except Exception:
                log.warning("Unable to prune scanner diagnostics after failure.",
                            exc_info=True)
        raise
    else:
        if diagnostic_run is not None:
            close_error = None
            try:
                close_diagnostic_run(diagnostic_run, outcome)
            except Exception as error:
                close_error = error
            try:
                prune_diagnostics(debug_dir)
            except Exception:
                if close_error is None:
                    raise
            if close_error is not None:
                raise close_error
    detail = stderr.decode(errors="replace").strip()
    try:
        report = json.loads(stdout.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise ValueError(detail or "Scanner returned invalid JSON") from error
    report = validate_report(report)
    if code not in (0, 3, 4) or report["status"] == "error":
        error = report.get("error")
        if isinstance(error, str) and len(error) <= MAX_REPORT_STRING:
            detail = error
        raise ValueError(detail or "Scanner failed")
    return report
