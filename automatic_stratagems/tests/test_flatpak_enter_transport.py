"""Opt-in real lifecycle test for HostCommand-wrapped ``flatpak enter``.

Run on the host with::

    HD2_RUN_FLATPAK_ENTER_TRANSPORT=1 \
      python -m unittest automatic_stratagems.tests.test_flatpak_enter_transport

Only disposable command-mode StreamController Flatpaks are started.  All test
processes and operation names carry the ``dummytransport`` label.
"""

from concurrent.futures import CancelledError
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


APP_ID = "com.core447.StreamController"
ENABLE_ENV = "HD2_RUN_FLATPAK_ENTER_TRANSPORT"
ROOT = Path(__file__).resolve().parents[2]
THIS_FILE = Path(__file__).resolve()
SUCCESS_BYTES = b"dummytransport-success\0bytes\n"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _session_environment():
    environment = os.environ.copy()
    runtime = environment.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    environment.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    return environment


def _flatpak_ps(environment):
    result = subprocess.run(
        ["flatpak", "ps", "--columns=instance,pid,application"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True, env=environment, text=True, timeout=10,
    )
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 3:
            rows[fields[0]] = (int(fields[1]), fields[2])
    return rows


def _read_instance_id(descriptor, timeout=10):
    readable, _, _ = select.select([descriptor], [], [], timeout)
    if not readable:
        raise TimeoutError("dummytransport Flatpak did not publish an instance ID")
    value = os.read(descriptor, 128).decode().strip()
    if not value.isdigit():
        raise RuntimeError(f"invalid dummytransport instance ID: {value!r}")
    return value


def _wait_for_instance(instance, environment, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = _flatpak_ps(environment).get(instance)
        if row is not None:
            pid, application = row
            if application != APP_ID:
                raise RuntimeError(
                    f"dummytransport instance resolved to {application!r}")
            if pid > 1:
                return pid
        time.sleep(.02)
    raise TimeoutError("dummytransport Flatpak was absent from flatpak ps")


def _stop_process_group(process, timeout=5):
    def live_members():
        members = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                identity = _process_identity(int(entry.name))
            except (FileNotFoundError, OSError, ProcessLookupError, ValueError):
                continue
            if identity["pgid"] == process.pid and identity["state"] != "Z":
                members.append(identity["pid"])
        return members

    if process.poll() is None or live_members():
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + timeout
        while live_members() and time.monotonic() < deadline:
            time.sleep(.01)
        if live_members():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.wait(timeout=timeout)


def _process_identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    argv = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    return {
        "pid": pid,
        "state": fields[0],
        "ppid": int(fields[1]),
        "pgid": int(fields[2]),
        "sid": int(fields[3]),
        "start_time": fields[19],
        "argv": [item.decode(errors="replace") for item in argv],
    }


def _is_descendant(pid, ancestor):
    seen = set()
    while pid > 1 and pid not in seen:
        if pid == ancestor:
            return True
        seen.add(pid)
        try:
            pid = _process_identity(pid)["ppid"]
        except (FileNotFoundError, ProcessLookupError):
            return False
    return False


def _find_labeled_process(label, ancestor):
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            identity = _process_identity(int(entry.name))
            executable = Path(os.readlink(entry / "exe")).name
        except (FileNotFoundError, OSError, ProcessLookupError, ValueError):
            continue
        if (label in identity["argv"] and executable.startswith("python") and
                _is_descendant(identity["pid"], ancestor)):
            matches.append(identity)
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {label} process below {ancestor}, got {matches}")
    return matches[0]


def _same_process_is_live(identity):
    try:
        current = _process_identity(identity["pid"])
    except FileNotFoundError:
        return False
    return (current["start_time"] == identity["start_time"] and
            current["state"] != "Z")


def _wait_dead(identity, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _same_process_is_live(identity):
        time.sleep(.01)
    return not _same_process_is_live(identity)


def _start_target(directory, environment):
    readable, writable = os.pipe()
    try:
        command = [
            "flatpak", "run", f"--instance-id-fd={writable}",
            "--command=/usr/bin/python3", f"--filesystem={directory}",
            "--env=HD2_DUMMYTRANSPORT=dummytransport-target", APP_ID,
            "-c", DUMMYTRANSPORT_TARGET, "dummytransport-target",
        ]
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=environment, pass_fds=(writable,),
            start_new_session=True,
        )
    finally:
        os.close(writable)
    try:
        try:
            instance = _read_instance_id(readable)
        finally:
            os.close(readable)
        instance_pid = _wait_for_instance(instance, environment)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                identity = _find_labeled_process(
                    "dummytransport-target", instance_pid)
                return process, instance, identity
            except RuntimeError:
                time.sleep(.02)
        raise TimeoutError("dummytransport target process was not found")
    except BaseException:
        _stop_process_group(process)
        raise


DUMMYTRANSPORT_TARGET = r"""
import time
while True:
    time.sleep(60)
"""


DUMMYTRANSPORT_CHILD = r"""
import pathlib, signal, sys, time
pathlib.Path(sys.argv[1]).write_text('dummytransport\n')
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(60)
"""


DUMMYTRANSPORT_COMMAND = r"""
import os, pathlib, signal, subprocess, sys, time

root = pathlib.Path(sys.argv[1])
scenario = sys.argv[2]
root.mkdir(parents=True, exist_ok=True)
subprocess.Popen(
    [sys.executable, '-c', sys.argv[3], str(root / 'child-ready'),
     'dummytransport-child'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
deadline = time.monotonic() + 5
while not (root / 'child-ready').exists() and time.monotonic() < deadline:
    time.sleep(.01)
if not (root / 'child-ready').exists():
    raise RuntimeError('dummytransport child did not start')
(root / 'ready').write_text('dummytransport\n')
deadline = time.monotonic() + 5
while not (root / 'audit-release').exists() and time.monotonic() < deadline:
    time.sleep(.01)
if not (root / 'audit-release').exists():
    raise RuntimeError('dummytransport host identity was not audited')
if scenario == 'success':
    os.write(1, b'dummytransport-success\0bytes\n')
    raise SystemExit(0)
if scenario == 'overflow':
    os.write(1, b'dummytransport-overflow-' + b'x' * 8192)
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(60)
"""


HOST_IDENTITY_QUERY = r"""
import json, os, pathlib, sys

labels = set(sys.argv[1:])
matches = {label: [] for label in labels}
for entry in pathlib.Path('/proc').iterdir():
    if not entry.name.isdigit() or int(entry.name) == os.getpid():
        continue
    try:
        executable = pathlib.Path(os.readlink(entry / 'exe')).name
        argv = (entry / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
        stat = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
    except (FileNotFoundError, OSError, ValueError, IndexError):
        continue
    if not executable.startswith('python'):
        continue
    decoded = [item.decode(errors='replace') for item in argv]
    for label in labels.intersection(decoded):
        matches[label].append({
            'pid': int(entry.name),
            'state': stat[0],
            'ppid': int(stat[1]),
            'pgid': int(stat[2]),
            'sid': int(stat[3]),
            'start_time': stat[19],
            'argv': decoded,
        })
print(json.dumps(matches))
"""


def _query_host_identities(*labels):
    result = subprocess.run(
        ["flatpak-spawn", "--host", "/usr/bin/python3", "-c",
         HOST_IDENTITY_QUERY, *labels],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False, timeout=3,
    )
    if result.returncode:
        raise AssertionError(
            "dummytransport host identities were not queryable: "
            f"{result.stderr.decode(errors='replace')}")
    return json.loads(result.stdout)


def _one_host_identity(matches, label):
    identities = matches.get(label, [])
    if len(identities) != 1:
        raise AssertionError(
            f"expected one live {label} host process, got {identities}")
    identity = identities[0]
    if identity["state"] == "Z":
        raise AssertionError(f"{label} is a zombie: {identity}")
    return identity


def _entered_command(target_pid, scenario, directory):
    return [
        "/usr/bin/flatpak", "enter", str(target_pid), "/usr/bin/python3",
        "-c", DUMMYTRANSPORT_COMMAND, str(directory), scenario,
        DUMMYTRANSPORT_CHILD, "dummytransport-leader",
    ]


def _run_scenario(target, root, scenario):
    from automatic_stratagems import host_commands
    from automatic_stratagems.scanner.capture import command as capture_command
    from automatic_stratagems.scanner.errors import ScanError

    directory = root / scenario
    directory.mkdir()
    base = root / "host-jobs"
    cancel = threading.Event() if scenario == "cancel" else None
    auditor = None
    audit_errors = []
    identities = {}
    real_popen = capture_command.subprocess.Popen
    launched = []

    def audit_after_ready(action=None):
        deadline = time.monotonic() + 10
        while not (directory / "ready").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        if not (directory / "ready").exists():
            audit_errors.append(AssertionError(
                f"dummytransport {scenario} did not become ready"))
            return
        try:
            operations = list(host_commands._operations(job))
            if len(operations) != 1:
                raise AssertionError(
                    f"expected one dummytransport operation, got {operations}")
            ready = host_commands._validate_state(
                operations[0], host_commands.READY_FILE)
            matches = _query_host_identities(
                "dummytransport-leader", "dummytransport-child")
            for name in ("leader", "child"):
                identity = _one_host_identity(
                    matches, f"dummytransport-{name}")
                if (identity["pgid"] != ready["pgid"] or
                        identity["sid"] != ready["sid"]):
                    raise AssertionError(
                        f"dummytransport {name} escaped HostCommand: "
                        f"{identity}, ready={ready}")
                identities[name] = identity
            (directory / "audit-release").write_text("dummytransport\n")
            if action is not None:
                action()
        except BaseException as error:
            audit_errors.append(error)

    popen_patch = None
    if scenario == "wrapper-death":
        def recording_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            launched.append(process)
            return process
        popen_patch = patch.object(capture_command.subprocess, "Popen",
                                   side_effect=recording_popen)

    try:
        with host_commands.create_host_job(base=base, hard_timeout=4) as job:
            job_path = job.path
            environment = host_commands.host_job_environment(job)
            with patch.dict(os.environ, environment, clear=False):
                if popen_patch is not None:
                    popen_patch.start()
                if scenario == "cancel":
                    auditor = threading.Thread(
                        target=audit_after_ready, args=(cancel.set,), daemon=True)
                elif scenario == "wrapper-death":
                    def kill_wrapper():
                        if launched:
                            try:
                                os.kill(launched[0].pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                    auditor = threading.Thread(
                        target=audit_after_ready, args=(kill_wrapper,), daemon=True)
                else:
                    auditor = threading.Thread(
                        target=audit_after_ready, daemon=True)
                auditor.start()

                command = _entered_command(target["pid"], scenario, directory)
                if scenario == "success":
                    output = capture_command.run_command(
                        command, host=True, operation="dummytransport-success",
                        timeout=5,
                    )
                    if output != SUCCESS_BYTES:
                        raise AssertionError(
                            f"dummytransport bytes changed: {output!r}")
                    outcome = "success"
                elif scenario == "cancel":
                    try:
                        capture_command.run_command(
                            command, host=True,
                            operation="dummytransport-cancel", timeout=5,
                            cancel_event=cancel,
                        )
                    except CancelledError:
                        outcome = "cancelled"
                    else:
                        raise AssertionError("dummytransport cancellation returned")
                else:
                    try:
                        capture_command.run_command(
                            command, host=True,
                            operation=f"dummytransport-{scenario}",
                            timeout=.2 if scenario == "timeout" else 5,
                            stdout_limit=(1024 if scenario == "overflow"
                                          else None),
                        )
                    except ScanError as error:
                        expected = ("timed out" if scenario == "timeout"
                                    else "stdout exceeded"
                                    if scenario == "overflow" else "failed")
                        if expected not in str(error):
                            raise AssertionError(
                                f"unexpected dummytransport error: {error}") from error
                        outcome = expected
                    else:
                        raise AssertionError(
                            f"dummytransport {scenario} unexpectedly returned")
    finally:
        if popen_patch is not None:
            popen_patch.stop()
        if auditor is not None:
            auditor.join(10)
            if auditor.is_alive():
                raise AssertionError(f"dummytransport {scenario} helper remained live")
        if audit_errors:
            raise audit_errors[0]

    leader = identities["leader"]
    child = identities["child"]
    if leader["pgid"] != child["pgid"]:
        raise AssertionError("dummytransport descendant escaped the host group")
    if leader["sid"] != child["sid"]:
        raise AssertionError("dummytransport descendant escaped the host session")
    if leader["pid"] == leader["pgid"]:
        raise AssertionError("dummytransport command replaced the host guard")
    live_target = _one_host_identity(
        _query_host_identities("dummytransport-target"),
        "dummytransport-target")
    if (live_target["pid"] != target["pid"] or
            live_target["start_time"] != target["start_time"]):
        raise AssertionError(
            f"dummytransport target identity changed: {live_target} != {target}")
    return {
        "outcome": outcome,
        "identities": identities,
        "job_removed": not job_path.exists(),
    }


def _inside(target, root):
    report = {}
    for scenario in (
            "success", "overflow", "timeout", "cancel", "wrapper-death"):
        report[scenario] = _run_scenario(target, root, scenario)
    report["host_jobs_empty"] = not any((root / "host-jobs").iterdir())
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0


@unittest.skipUnless(
    os.environ.get(ENABLE_ENV) == "1",
    f"set {ENABLE_ENV}=1 to run the real dummytransport Flatpak test",
)
class FlatpakEnterTransportTests(unittest.TestCase):
    def test_entered_commands_remain_owned_and_are_reaped(self):
        self.assertIsNone(os.environ.get("FLATPAK_ID"),
                          "outer dummytransport test must run on the host")
        self.assertIsNotNone(shutil.which("flatpak"))
        environment = _session_environment()
        with tempfile.TemporaryDirectory(
                prefix="hd2-dummytransport-", dir="/tmp") as temporary:
            root = Path(temporary)
            target = None
            try:
                target, instance, target_identity = _start_target(root, environment)
                command = [
                    "flatpak", "run", "--command=/usr/bin/python3",
                    f"--filesystem={ROOT}:ro", f"--filesystem={root}",
                    "--env=HD2_DUMMYTRANSPORT=dummytransport-runner", APP_ID,
                    str(THIS_FILE), "--inside", json.dumps(target_identity),
                    str(root),
                ]
                result = subprocess.run(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, env=environment, timeout=60,
                    check=False,
                )
                self.assertEqual(
                    result.returncode, 0,
                    f"stdout:\n{result.stdout.decode(errors='replace')}\n"
                    f"stderr:\n{result.stderr.decode(errors='replace')}",
                )
                report = json.loads((root / "report.json").read_text())
                self.assertTrue(report.pop("host_jobs_empty"))
                self.assertEqual(set(report), {
                    "success", "overflow", "timeout", "cancel",
                    "wrapper-death",
                })
                for scenario, result_report in report.items():
                    with self.subTest(scenario=scenario):
                        self.assertTrue(result_report["job_removed"])
                        for identity in result_report["identities"].values():
                            self.assertTrue(
                                _wait_dead({
                                    "pid": identity["pid"],
                                    "start_time": identity["start_time"],
                                }),
                                f"dummytransport process remains live: {identity}",
                            )
                self.assertEqual(report["success"]["outcome"], "success")
                self.assertEqual(report["overflow"]["outcome"],
                                 "stdout exceeded")
                self.assertEqual(report["timeout"]["outcome"], "timed out")
                self.assertEqual(report["cancel"]["outcome"], "cancelled")
                self.assertEqual(report["wrapper-death"]["outcome"], "failed")
                self.assertIn(instance, _flatpak_ps(environment))
                self.assertTrue(_same_process_is_live(target_identity))
            finally:
                if target is not None:
                    _stop_process_group(target)


if __name__ == "__main__" and sys.argv[1:2] == ["--inside"]:
    raise SystemExit(_inside(json.loads(sys.argv[2]), Path(sys.argv[3])))
elif __name__ == "__main__":
    unittest.main()
