"""Test-only commands used by the isolated Flatpak lifecycle harness.

The ``host-cli`` path intentionally imports only the Python standard library.
It runs through the production host-command transport and substitutes bytes at
the acquisition boundary; recognition remains in the scanner child.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import signal
import shutil
import subprocess
import sys
import threading
import time


AUDIT_ENV = "HD2_SANDBOX_AUDIT"
FIXTURE_ENV = "HD2_SANDBOX_FIXTURE"
GEOMETRY_ENV = "HD2_SANDBOX_GEOMETRY"
SCENARIO_ENV = "HD2_SANDBOX_SCENARIO"
HOST_STARTED_ENV = "HD2_SANDBOX_HOST_STARTED"
EXPECTED_ENV = "HD2_SANDBOX_EXPECTED_IDS"
DEFAULT_OUTPUT_LIMIT = 16 * 1024 * 1024
OUTPUT_OVERFLOW_RETURN_CODE = 125
LINGERING_PROCESS_RETURN_CODE = 126

_GEOMETRY = re.compile(r"^(-?\d+),(-?\d+) ([1-9]\d*)x([1-9]\d*)$")


def _write_all(descriptor, data):
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("audit write made no progress")
        view = view[written:]


def _append_json(path, value):
    if not path:
        return
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        _write_all(descriptor, payload)
    finally:
        os.close(descriptor)


def _process_location():
    try:
        root = os.readlink("/proc/self/root")
    except OSError as error:
        root = f"unavailable: {error}"
    try:
        mount_namespace = os.readlink("/proc/self/ns/mnt")
    except OSError as error:
        mount_namespace = f"unavailable: {error}"
    try:
        executable = os.readlink("/proc/self/exe")
    except OSError as error:
        executable = f"unavailable: {error}"
    try:
        start_time = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        start_time = None
    return {
        "executable": executable,
        "flatpak_info": Path("/.flatpak-info").exists(),
        "mount_namespace": mount_namespace,
        "root": root,
        "start_time": start_time,
    }


def _audit(event, argv):
    _append_json(os.environ.get(AUDIT_ENV), {
        "argv": list(argv),
        "command": argv[0] if argv else None,
        "event": event,
        "location": _process_location(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "time_ns": time.time_ns(),
    })


def _ignore_shutdown():
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, signal.SIG_IGN)


def _linger_child():
    _audit("linger-child-started", ["linger-child"])
    _ignore_shutdown()
    while True:
        time.sleep(60)


def _host_screenshot(argv):
    if len(argv) != 2 or not Path(argv[1]).is_absolute():
        raise ValueError('Expected screenshot output path')
    if os.environ.get(SCENARIO_ENV, 'complete') == 'cancel':
        started = os.environ.get(HOST_STARTED_ENV)
        if started:
            Path(started).write_text(f'{os.getpid()}\n')
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), 'linger-child'],
            stdin=subprocess.DEVNULL)
        _ignore_shutdown()
        _audit('screenshot-hanging', argv)
        while True:
            time.sleep(60)
    fixture = os.environ.get(FIXTURE_ENV)
    if not fixture:
        raise ValueError(f'Missing {FIXTURE_ENV}')
    shutil.copyfile(fixture, argv[1])


def _host_probe(argv):
    scenario = os.environ.get(SCENARIO_ENV, "complete")
    if scenario == "stdout-overflow":
        sys.stdout.buffer.write(b"o" * (512 * 1024))
        return 0
    if scenario == "stderr-overflow":
        sys.stderr.buffer.write(b"e" * (512 * 1024))
        return 0
    if scenario == "nonzero":
        print("deliberate probe failure", file=sys.stderr)
        return 23
    if scenario in {"cancel", "timeout"}:
        started = os.environ.get(HOST_STARTED_ENV)
        if started:
            Path(started).write_text(f"{os.getpid()}\n")
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "linger-child"],
            stdin=subprocess.DEVNULL,
        )
        _ignore_shutdown()
        _audit("probe-hanging", argv)
        while True:
            time.sleep(60)
    sys.stdout.write(json.dumps(argv, separators=(",", ":")))
    return 0


def _host_cli(argv):
    if not argv:
        raise ValueError("host-cli requires a command")
    _audit("host-cli-started", argv)
    if argv[0] == "screenshot":
        _host_screenshot(argv)
    elif argv[0] == "probe":
        status = _host_probe(argv)
        if status:
            return status
    else:
        raise ValueError(f"unexpected host command: {argv[0]}")
    _audit("host-cli-complete", argv)
    return 0


def _scanner_wrapper(argv):
    """Run the real scanner with only its live acquisition commands replaced."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from automatic_stratagems.scanner import scan_game
    from automatic_stratagems.scanner.capture import command as capture_command

    _append_json(os.environ.get(AUDIT_ENV), {
        "event": "scanner-wrapper-started",
        "location": _process_location(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "tesseract": shutil.which("tesseract"),
        "time_ns": time.time_ns(),
    })
    original = capture_command.run_command
    support = str(Path(__file__).resolve())
    forwarded = {
        name: os.environ[name]
        for name in (AUDIT_ENV, FIXTURE_ENV, GEOMETRY_ENV, SCENARIO_ENV,
                     HOST_STARTED_ENV, EXPECTED_ENV)
        if name in os.environ
    }

    def run_command(command, *args, **kwargs):
        if command and Path(command[0]).name == "fixture-trigger":
            kwargs["host"] = True
            kwargs["host_env"] = {**kwargs.get("host_env", {}), **forwarded}
            output = Path(command[0]).parent / "captures/frame.png"
            command = ["/usr/bin/python3", support, "host-cli", "screenshot", str(output)]
        return original(command, *args, **kwargs)

    capture_command.run_command = run_command
    try:
        return scan_game.main(argv)
    finally:
        capture_command.run_command = original


def _descendants(root_pid):
    processes = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            processes[int(entry.name)] = (int(fields[1]), int(fields[21]))
        except (FileNotFoundError, OSError, ValueError, IndexError):
            continue
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _rss) in processes.items():
            if parent in selected and pid not in selected:
                selected.add(pid)
                changed = True
    return selected, processes


def _live_group_members(process_group):
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z" and int(fields[2]) == process_group:
                members.append(int(entry.name))
        except (FileNotFoundError, OSError, ValueError, IndexError):
            continue
    return members


def _terminate_group(process, grace=10):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    while _live_group_members(process.pid) and time.monotonic() < deadline:
        time.sleep(.01)
    if _live_group_members(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.wait()
    deadline = time.monotonic() + 1
    while _live_group_members(process.pid) and time.monotonic() < deadline:
        time.sleep(.01)
    return not _live_group_members(process.pid)


def _drain_output(stream, path, limit, state, name, overflow):
    try:
        with path.open("wb") as destination:
            retained = 0
            read = getattr(stream, "read1", stream.read)
            while chunk := read(64 * 1024):
                state[f"{name}_total_bytes"] += len(chunk)
                available = max(0, limit - retained)
                if available:
                    written = chunk[:available]
                    destination.write(written)
                    retained += len(written)
                if len(chunk) > available:
                    state[f"{name}_overflow"] = True
                    overflow.set()
    except BaseException as error:
        state[f"{name}_error"] = error
        overflow.set()
    finally:
        stream.close()
        state[f"{name}_done"].set()


def run_validation_process(command, *, stdout_path, stderr_path, timeout,
                           output_limit=DEFAULT_OUTPUT_LIMIT, environment=None,
                           process_record=None):
    """Run one harness command with bounded retained output and group cleanup."""
    if output_limit < 1:
        raise ValueError("output limit must be positive")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    process = subprocess.Popen(
        command, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    state = {
        "stdout_done": threading.Event(),
        "stderr_done": threading.Event(),
        "stdout_total_bytes": 0,
        "stderr_total_bytes": 0,
        "stdout_overflow": False,
        "stderr_overflow": False,
    }
    overflow = threading.Event()
    readers = [
        threading.Thread(
            target=_drain_output,
            args=(process.stdout, stdout_path, output_limit, state, "stdout", overflow),
            daemon=True),
        threading.Thread(
            target=_drain_output,
            args=(process.stderr, stderr_path, output_limit, state, "stderr", overflow),
            daemon=True),
    ]
    for reader in readers:
        reader.start()
    peak_rss = 0
    peak_members = 0
    observed = {}
    stop = threading.Event()

    def sample():
        nonlocal peak_rss, peak_members
        pagesize = os.sysconf("SC_PAGE_SIZE")
        while not stop.wait(.01):
            try:
                selected, processes = _descendants(process.pid)
                rss = sum(processes[pid][1] * pagesize for pid in selected
                          if pid in processes)
                if process_record is not None:
                    for pid in selected:
                        if pid in processes:
                            parent, pages = processes[pid]
                            record = process_record(pid, parent, pages)
                            observed[(pid, record.get("start_time"))] = record
                if rss > peak_rss:
                    peak_rss = rss
                    peak_members = len(selected)
            except OSError:
                pass

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    timed_out = False
    output_overflow = False
    lingering_process_group = False
    cleanup_confirmed = False
    deadline = started + timeout
    try:
        while True:
            if overflow.is_set():
                output_overflow = True
                cleanup_confirmed = _terminate_group(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                cleanup_confirmed = _terminate_group(process)
                break
            if (process.poll() is not None and state["stdout_done"].is_set()
                    and state["stderr_done"].is_set()):
                lingering_process_group = bool(_live_group_members(process.pid))
                cleanup_confirmed = (_terminate_group(process)
                                     if lingering_process_group else True)
                break
            time.sleep(.01)
    except BaseException:
        _terminate_group(process)
        raise
    finally:
        stop.set()
        sampler.join(1)
        for reader in readers:
            reader.join(1)
    for name in ("stdout", "stderr"):
        error = state.get(f"{name}_error")
        if error is not None:
            raise error
        if not state[f"{name}_done"].is_set():
            raise RuntimeError(f"{name} reader did not stop")
    if process.poll() is None:
        process.wait()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "argv": command,
        "seconds": round(time.monotonic() - started, 6),
        "returncode": process.returncode,
        "timed_out": timed_out,
        "output_overflow": output_overflow,
        "lingering_process_group": lingering_process_group,
        "stdout_overflow": state["stdout_overflow"],
        "stderr_overflow": state["stderr_overflow"],
        "output_limit_bytes": output_limit,
        "stdout_total_bytes": state["stdout_total_bytes"],
        "stderr_total_bytes": state["stderr_total_bytes"],
        "process_group_cleanup_confirmed": cleanup_confirmed,
        "peak_descendant_rss_bytes": peak_rss,
        "peak_descendant_members": peak_members,
        "child_user_seconds": round(after.ru_utime - before.ru_utime, 6),
        "child_system_seconds": round(after.ru_stime - before.ru_stime, 6),
        "observed_processes": sorted(observed.values(), key=lambda item: (
            item["pid"], item.get("start_time") or "")),
    }


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _measure(argv):
    parser = argparse.ArgumentParser(prog="sandbox_support.py measure")
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--output-limit", type=int, default=DEFAULT_OUTPUT_LIMIT)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    command = (arguments.command[1:] if arguments.command[:1] == ["--"]
               else arguments.command)
    if not command:
        parser.error("a command is required after --")
    prefix = arguments.output_prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = prefix.with_suffix(".stdout")
    stderr_path = prefix.with_suffix(".stderr")
    metrics = run_validation_process(
        command, stdout_path=stdout_path, stderr_path=stderr_path,
        timeout=arguments.timeout, output_limit=arguments.output_limit)
    metrics.pop("observed_processes")
    metrics.update({
        "schema_version": 1,
        "sampler": "sum /proc PPID-tree RSS at 10 ms",
        "stdout_sha256": _file_hash(stdout_path),
        "stderr_sha256": _file_hash(stderr_path),
    })
    prefix.with_suffix(".metrics.json").write_text(json.dumps(
        metrics, indent=2) + "\n")
    if metrics["timed_out"]:
        return 124
    if metrics["output_overflow"]:
        return OUTPUT_OVERFLOW_RETURN_CODE
    if metrics["lingering_process_group"]:
        return LINGERING_PROCESS_RETURN_CODE
    return metrics["returncode"]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] == ["host-cli"]:
            return _host_cli(argv[1:])
        elif argv[:1] == ["scanner-wrapper"]:
            return _scanner_wrapper(argv[1:])
        elif argv[:1] == ["measure"]:
            return _measure(argv[1:])
        elif argv == ["linger-child"]:
            _linger_child()
        else:
            raise ValueError("expected host-cli or linger-child")
    except (OSError, ValueError) as error:
        print(f"sandbox support: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
