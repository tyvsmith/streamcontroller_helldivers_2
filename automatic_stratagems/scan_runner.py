"""Launch, bound, cancel, and validate the scanner process."""

from concurrent.futures import CancelledError
from contextlib import nullcontext
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

from .capture_source import MAX_SOURCE_STRING, source_arguments
from .host_commands import (create_host_job, host_job_environment)
from .scanner_runtime import (ScanSetupError, resolve_scanner_runtime,
                              scanner_preflight_command)
from .shared.fs import read_bounded_stream, write_all


SCHEMA_VERSION = 1
SCAN_TIMEOUT_SECONDS = 120
SCANNER_BUDGET_SECONDS = 110
TERMINATE_GRACE_SECONDS = 1
FLATPAK_TERMINATE_GRACE_SECONDS = 10
OUTPUT_DRAIN_SECONDS = .1
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
MAX_REPORT_ROWS = 32
MAX_REPORT_STRING = MAX_SOURCE_STRING
MAX_DIAGNOSTIC_RUNS = 20
MAX_DIAGNOSTIC_BYTES = 512 * 1024 * 1024
DIAGNOSTIC_OWNER = "net_jslay_helldivers_2"
CACHE_MARKER = ".hd2-diagnostics-cache.json"
RUN_MARKER = ".hd2-scan-run.json"
log = logging.getLogger(__name__)


def scan_workers(value=2):
    try:
        return max(1, min(32, int(value))) if not isinstance(value, bool) else 2
    except (ValueError, TypeError, OverflowError):
        return 2


def is_flatpak():
    return bool(os.environ.get("FLATPAK_ID")) or Path("/.flatpak-info").exists()


def validate_image_source_report(report, source, *, backend="screenshot"):
    """Validate metadata against the configured source and return it."""
    metadata = report.get('source')
    if not isinstance(metadata, dict):
        raise ValueError('Scanner returned invalid image source metadata')
    if backend == 'auto' and metadata.get('kind') == 'live':
        socket = metadata.get('socket')
        if (metadata.get('backend') != 'gamescope' or not isinstance(socket, str)
                or not socket or len(socket) > MAX_REPORT_STRING):
            raise ValueError('Scanner returned invalid Gamescope source metadata')
        return None
    fingerprint = metadata.get('fingerprint')
    path = metadata.get('path')
    selection = metadata.get('selection')
    if (metadata.get('kind') != 'file' or selection not in ('file', 'folder')
            or (source['kind'] != 'path' and selection != source['kind'])
            or not isinstance(path, str) or len(path) > MAX_REPORT_STRING
            or not Path(path).is_absolute()
            or not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(c not in '0123456789abcdef' for c in fingerprint)
            or type(metadata.get('mtime_ns')) is not int or metadata['mtime_ns'] < 0
            or type(metadata.get('size_bytes')) is not int or metadata['size_bytes'] <= 0):
        raise ValueError('Scanner returned invalid image source metadata')
    if not source['path'].strip():
        from .scanner.screenshot_capture import resolve_source
        source = resolve_source(source)
    selected = Path(os.path.abspath(os.path.expanduser(source['path'])))
    reported = Path(path)
    if (path != os.path.abspath(path)
            or (reported if selection == 'file' else reported.parent) != selected):
        raise ValueError('Scanner returned image source outside the selected path')
    return metadata


def scan_command(root, *, flatpak=None, backend="auto", workers=2, debug_dir=None,
                 image_source=None, _runtime=None):
    root = Path(root)
    flatpak = is_flatpak() if flatpak is None else flatpak
    runtime = (_runtime if _runtime is not None else
               resolve_scanner_runtime(root, flatpak=flatpak))
    command = [str(runtime.interpreter), "-m",
               "automatic_stratagems.scanner", "--json", "--budget-seconds",
               str(SCANNER_BUDGET_SECONDS), "--workers",
               str(scan_workers(workers)), "--capture-backend", backend]
    command.extend(source_arguments(image_source, defer_trigger=backend == 'auto'))
    if debug_dir is not None:
        command.extend(["--debug-dir", str(debug_dir)])
    return command


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


def _cancelled(cancel_event):
    return cancel_event is not None and cancel_event.is_set()


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
               stdout_limit=MAX_STDOUT_BYTES, stderr_limit=MAX_STDERR_BYTES,
               env=None):
    if _cancelled(cancel_event):
        raise CancelledError()
    try:
        process = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, env=env)
    except FileNotFoundError as error:
        raise ScanSetupError(f"Missing scanner command: {command[0]}") from error

    stdout = bytearray()
    stderr = bytearray()
    overflow = []
    readers = [
        threading.Thread(target=read_bounded_stream,
                         args=(process.stdout, stdout_limit, "stdout", stdout, overflow),
                         daemon=True),
        threading.Thread(target=read_bounded_stream,
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


def _job_context(flatpak, hard_timeout):
    if not flatpak:
        return nullcontext(None)
    return create_host_job(hard_timeout=hard_timeout)


def _preflight_error(stderr, fallback):
    detail = stderr.decode(errors="replace").strip()
    return ScanSetupError(detail or fallback)


def check_scan_setup(root, *, flatpak=None, backend="auto", cancel_event=None,
                     image_source=None):
    """Check the child runtime and capture prerequisites without capturing."""
    image_arguments = source_arguments(image_source, defer_trigger=backend == 'auto')
    root = Path(root)
    flatpak = is_flatpak() if flatpak is None else flatpak
    runtime = resolve_scanner_runtime(root, flatpak=flatpak)
    code, _, stderr = _run_owned(
        scanner_preflight_command(root, runtime), cwd=root, timeout=15,
        cancel_event=cancel_event, stdout_limit=16 * 1024,
        stderr_limit=16 * 1024, env=dict(runtime.env))
    if code:
        raise _preflight_error(
            stderr, "Scanner runtime dependencies are unavailable")
    if _cancelled(cancel_event):
        raise CancelledError()
    with _job_context(flatpak and backend != 'auto' and (
            backend == 'gamescope' or
            (image_source is not None and image_source.get('trigger') == 'script')), 20) as job:
        environment = dict(runtime.env)
        if job is not None:
            environment.update(host_job_environment(job))
        command = [str(runtime.interpreter), "-m",
                   "automatic_stratagems.scanner", "--check-setup",
                   "--capture-backend", backend, *image_arguments]
        code, _, stderr = _run_owned(
            command, cwd=root, timeout=15, cancel_event=cancel_event,
            stdout_limit=1024, stderr_limit=16 * 1024, env=environment)
    if code:
        raise _preflight_error(stderr, "Capture prerequisites are unavailable")
    if _cancelled(cancel_event):
        raise CancelledError()


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


def run_scan(root, backend="auto", workers=2, *, cancel_event=None, debug_dir=None,
             image_source=None):
    """Return one validated report after all owned host work has exited."""
    if _cancelled(cancel_event):
        raise CancelledError()
    flatpak = is_flatpak()
    source_options = {"image_source": image_source} if image_source is not None else {}
    check_scan_setup(root, flatpak=flatpak, backend=backend,
                     cancel_event=cancel_event, **source_options)
    runtime = resolve_scanner_runtime(Path(root), flatpak=flatpak)
    diagnostic_run = prepare_diagnostic_run(debug_dir) if debug_dir else None
    outcome = "failed"
    try:
        command = scan_command(root, flatpak=flatpak, backend=backend,
                               workers=workers, debug_dir=diagnostic_run,
                               _runtime=runtime, **source_options)
        with _job_context(
            backend in ('auto', 'gamescope') or
            (flatpak and image_source is not None and
             image_source.get('trigger') == 'script'), SCAN_TIMEOUT_SECONDS) as job:
            environment = dict(runtime.env)
            if job is not None:
                environment.update(host_job_environment(job))
            code, stdout, stderr = _run_owned(
                command, cwd=Path(root), timeout=SCAN_TIMEOUT_SECONDS,
                cancel_event=cancel_event, stdout_limit=MAX_STDOUT_BYTES,
                stderr_limit=MAX_STDERR_BYTES, env=environment)
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
        if image_source is not None:
            validate_image_source_report(report, image_source, backend=backend)
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
    return report
