"""Bounded host-command ownership shared by the scanner and its parent."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
import uuid


OWNER = "net_jslay_helldivers_2"
VERSION = 1
JOB_DIRECTORY_ENV = "HD2_HOST_JOB_DIRECTORY"
JOB_NONCE_ENV = "HD2_HOST_JOB_NONCE"
JOB_DEADLINE_ENV = "HD2_HOST_JOB_DEADLINE"
REGISTRY_FILE = "registry.json"
INTENT_FILE = "intent.json"
READY_FILE = "ready.json"
SUBMITTED_FILE = "submitted.json"
RESULT_FILE = "result.json"
COMPLETE_FILE = "complete.json"
RETAIN_FILE = "retain-artifacts.json"
STATE_LIMIT = 4096
DEFAULT_CLEANUP_SECONDS = 3.0


class HostCommandError(RuntimeError):
    pass


class HostCleanupUnconfirmed(HostCommandError):
    pass


class HostArtifactCleanupError(HostCommandError):
    """Commands stopped, but owned capture files could not be removed safely."""


@dataclass(frozen=True)
class HostJob:
    path: Path
    nonce: str
    hard_deadline: int


@dataclass(frozen=True)
class HostOperation:
    job: HostJob
    path: Path
    nonce: str
    operation: str


@dataclass(frozen=True)
class HostCompletion:
    command_status: int
    cleanup_confirmed: bool


def _uptime_ticks():
    """Return kernel uptime in centiseconds, shared by Flatpak and host."""
    try:
        value = Path("/proc/uptime").read_text().split(maxsplit=1)[0]
        seconds, fraction = value.split(".", 1)
        fraction = (fraction + "00")[:2]
        if not seconds.isdigit() or not fraction.isdigit():
            raise ValueError
        return int(seconds) * 100 + int(fraction)
    except (OSError, ValueError, IndexError) as error:
        raise HostCommandError("Kernel uptime is unavailable") from error


def _cache_root():
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "net_jslay_helldivers_2" / "host-jobs"


def _atomic_json(path, value, *, create=False):
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(payload) > STATE_LIMIT:
        raise HostCommandError("Host command state is too large")
    target = path if create else path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    descriptor = None
    created = False
    try:
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("Host command state write made no progress")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if not create:
            os.replace(target, path)
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        raise


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or
                metadata.st_size > STATE_LIMIT):
            raise HostCommandError(f"Unsafe host command state: {path}")
        chunks = []
        remaining = STATE_LIMIT + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > STATE_LIMIT:
            raise HostCommandError(f"Unsafe host command state: {path}")
    finally:
        os.close(descriptor)
    value = json.loads(payload.decode())
    if not isinstance(value, dict):
        raise HostCommandError(f"Invalid host command state: {path}")
    return value


def _registry_value(job):
    return {"version": VERSION, "owner": OWNER, "nonce": job.nonce,
            "hard_deadline": job.hard_deadline}


def _validate_job(job):
    metadata = job.path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or
            stat.S_IMODE(metadata.st_mode) != 0o700):
        raise HostCommandError(f"Unsafe host job directory: {job.path}")
    if _read_json(job.path / REGISTRY_FILE) != _registry_value(job):
        raise HostCommandError("Host job registry identity changed")


@contextmanager
def create_host_job(*, base=None, hard_timeout=120):
    base = _cache_root() if base is None else Path(base)
    base.mkdir(parents=True, exist_ok=True)
    timeout_ticks = max(0, math.ceil(float(hard_timeout) * 100))
    hard_deadline = _uptime_ticks() + timeout_ticks
    nonce = uuid.uuid4().hex
    path = base / f"scan-{nonce}"
    path.mkdir(mode=0o700)
    job = HostJob(path=path, nonce=nonce, hard_deadline=hard_deadline)
    _atomic_json(path / REGISTRY_FILE, _registry_value(job), create=True)
    try:
        yield job
    finally:
        wait_host_job(job)
        if os.path.lexists(path / RETAIN_FILE):
            logging.getLogger(__name__).warning(
                "Capture cleanup records retained at %s", path)
        else:
            shutil.rmtree(path)


def host_job_environment(job):
    _validate_job(job)
    return {JOB_DIRECTORY_ENV: str(job.path), JOB_NONCE_ENV: job.nonce,
            JOB_DEADLINE_ENV: repr(job.hard_deadline)}


@contextmanager
def shared_host_directory(prefix="capture-", *, retain_on_error=False):
    """Create a private artifact directory visible to sandbox and host."""
    if not (os.environ.get(JOB_DIRECTORY_ENV) or os.environ.get("FLATPAK_ID")
            or Path("/.flatpak-info").exists()):
        with __import__("tempfile").TemporaryDirectory(prefix=prefix) as directory:
            yield Path(directory)
        return
    job = _job_from_environment()
    path = job.path / f"{prefix}{uuid.uuid4().hex}"
    path.mkdir(mode=0o700)
    retain = False
    if retain_on_error:
        _atomic_json(job.path / RETAIN_FILE, {
            "owner": OWNER, "job_nonce": job.nonce,
            "directory": path.name})
    try:
        yield path
    except (HostCleanupUnconfirmed, HostArtifactCleanupError):
        if retain_on_error:
            retain = True
        raise
    finally:
        if not retain:
            shutil.rmtree(path)
            if retain_on_error:
                (job.path / RETAIN_FILE).unlink()


def _job_from_environment(environ=None):
    environ = os.environ if environ is None else environ
    try:
        job = HostJob(Path(environ[JOB_DIRECTORY_ENV]), environ[JOB_NONCE_ENV],
                      int(environ[JOB_DEADLINE_ENV]))
    except (KeyError, TypeError, ValueError) as error:
        raise HostCommandError("Host command job environment is unavailable") from error
    _validate_job(job)
    return job


def reserve_operation(job, command, operation):
    _validate_job(job)
    if (not command or not isinstance(command[0], str) or not command[0] or
            "\0" in command[0] or
            any(not isinstance(item, str) or "\0" in item
                for item in command[1:])):
        raise HostCommandError("Host command argv is invalid")
    if not isinstance(operation, str) or not operation or len(operation) > 64:
        raise HostCommandError("Host operation name is invalid")
    nonce = uuid.uuid4().hex
    staging = job.path / f".operation-stage-{nonce}"
    path = job.path / f"operation-{nonce}"
    staging.mkdir(mode=0o700)
    item = HostOperation(job=job, path=staging, nonce=nonce, operation=operation)
    digest = hashlib.sha256(b"\0".join(part.encode() for part in command)).hexdigest()
    try:
        _atomic_json(staging / INTENT_FILE, {
            "version": VERSION, "owner": OWNER, "job_nonce": job.nonce,
            "operation_nonce": nonce, "operation": operation,
            "argv_sha256": digest, "hard_deadline": job.hard_deadline,
        }, create=True)
        os.replace(staging, path)
        _fsync_directory(job.path)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return HostOperation(job=job, path=path, nonce=nonce, operation=operation)


def write_state(operation, filename, values):
    value = {"version": VERSION, "owner": OWNER,
             "job_nonce": operation.job.nonce,
             "operation_nonce": operation.nonce, **values}
    _atomic_json(operation.path / filename, value)


def process_start_time(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[19]


HOST_GUARD = r'''
operation_dir=$1
job_nonce=$2
operation_nonce=$3
hard_deadline=$4
shift 4
watchdog=0
status=125
pgid=0
umask 077
atomic_state() {
  name=$1; body=$2; temporary="$operation_dir/.$name.$$"
  ( set -C; printf '%s\n' "$body" > "$temporary" ) || return 1
  /usr/bin/mv -f -- "$temporary" "$operation_dir/$name"
  /usr/bin/sync -d "$operation_dir/$name" "$operation_dir" || return 1
}
uptime_ticks() {
  IFS=' ' read -r uptime ignored < /proc/uptime || return 1
  seconds=${uptime%%.*}
  fraction=${uptime#*.}
  case "$seconds:$fraction" in
    *[!0-9:]*|:*) return 1 ;;
  esac
  fraction="${fraction}00"
  fraction=${fraction%"${fraction#??}"}
  tens=${fraction%?}
  ones=${fraction#?}
  UPTIME_TICKS=$((seconds * 100 + tens * 10 + ones))
}
cleanup() {
  trap - EXIT
  trap '' HUP INT TERM
  if [ "$watchdog" -gt 0 ] 2>/dev/null; then kill "$watchdog" 2>/dev/null || :; fi
  uptime_ticks || UPTIME_TICKS=0
  completed=$UPTIME_TICKS
  body="{\"version\":1,\"owner\":\"net_jslay_helldivers_2\",\"job_nonce\":\"$job_nonce\",\"operation_nonce\":\"$operation_nonce\",\"command_status\":$status,\"completed_at\":$completed}"
  atomic_state result.json "$body" || status=125
  if [ "$pgid" -gt 0 ] 2>/dev/null; then kill -KILL -- "-$pgid" 2>/dev/null || :; fi
  exit 125
}
cancel_int() { status=130; cleanup; }
cancel_term() { status=143; cleanup; }
trap cleanup EXIT
trap cancel_int INT HUP
trap cancel_term TERM
IFS= read -r guard_stat < "/proc/$$/stat" || exit 125
guard_fields=${guard_stat##*) }
read -r state parent pgid sid tty_nr tpgid flags minflt cminflt majflt cmajflt utime stime cutime cstime priority nice threads interval guard_start ignored <<EOF
$guard_fields
EOF
if [ "$pgid" != "$$" ] || [ "$sid" != "$$" ]; then exit 125; fi
case "$hard_deadline" in ''|*[!0-9]*) exit 125 ;; esac
uptime_ticks || exit 125
now=$UPTIME_TICKS
if [ "$now" -ge "$hard_deadline" ]; then exit 125; fi
body="{\"version\":1,\"owner\":\"net_jslay_helldivers_2\",\"job_nonce\":\"$job_nonce\",\"operation_nonce\":\"$operation_nonce\",\"guard_pid\":$$,\"guard_start_time\":\"$guard_start\",\"pgid\":$pgid,\"sid\":$sid,\"ready_at\":$now}"
atomic_state ready.json "$body" || exit 125
uptime_ticks || exit 125
now=$UPTIME_TICKS
if [ "$now" -ge "$hard_deadline" ]; then exit 125; fi
remaining=$((hard_deadline - now))
delay=$(((remaining + 99) / 100))
( /usr/bin/sleep "$delay"; kill -TERM $$ 2>/dev/null ) &
watchdog=$!
"$@" &
child=$!
wait "$child"
status=$?
cleanup
'''


HOST_GROUP_QUERY = r'''
target=$1
for process_stat in /proc/[0-9]*/stat; do
  IFS= read -r line < "$process_stat" || continue
  fields=${line##*) }
  set -- $fields
  if [ "$3" = "$target" ] && [ "$1" != Z ]; then exit 1; fi
done
exit 0
'''


LOCAL_LAUNCHER = r'''
operation_dir=$1
job_nonce=$2
operation_nonce=$3
shift 3
umask 077
temporary="$operation_dir/.submitted.json.$$"
IFS= read -r launcher_stat < "/proc/$$/stat" || exit 125
launcher_fields=${launcher_stat##*) }
read -r state parent pgid sid tty_nr tpgid flags minflt cminflt majflt cmajflt utime stime cutime cstime priority nice threads interval launcher_start ignored <<EOF
$launcher_fields
EOF
body="{\"version\":1,\"owner\":\"net_jslay_helldivers_2\",\"job_nonce\":\"$job_nonce\",\"operation_nonce\":\"$operation_nonce\",\"launcher_pid\":$$,\"launcher_start_time\":\"$launcher_start\"}"
( set -C; printf '%s\n' "$body" > "$temporary" ) || exit 125
/usr/bin/mv -f -- "$temporary" "$operation_dir/submitted.json" || exit 125
/usr/bin/sync -d "$operation_dir/submitted.json" "$operation_dir" || exit 125
exec "$@"
'''


HOST_OPERATION_QUERY = r'''
operation_dir=$1
pattern="$operation_dir/.nonce-query.$$"
trap '/usr/bin/rm -f -- "$pattern"' EXIT HUP INT TERM
printf '%s\n' "$HD2_OPERATION_QUERY" > "$pattern" || exit 2
/usr/bin/grep -z -F -x -q -f "$pattern" /proc/[0-9]*/cmdline 2>/dev/null
status=$?
if [ "$status" -eq 0 ]; then exit 1; fi
if [ "$status" -eq 1 ]; then exit 0; fi
exit 2
'''


def guard_command(command, *, operation):
    job = _job_from_environment()
    item = reserve_operation(job, command, operation)
    argv = ["/usr/bin/sh", "-c", HOST_GUARD, "hd2-host-command",
            str(item.path), job.nonce, item.nonce, str(job.hard_deadline), *command]
    return argv, item


def launcher_command(command, operation):
    return ["/bin/sh", "-c", LOCAL_LAUNCHER, "hd2-host-launcher",
            str(operation.path), operation.job.nonce, operation.nonce, *command]


def _validate_state(operation, filename):
    value = _read_json(operation.path / filename)
    expected = {"version": VERSION, "owner": OWNER,
                "job_nonce": operation.job.nonce,
                "operation_nonce": operation.nonce}
    if any(value.get(key) != wanted for key, wanted in expected.items()):
        raise HostCommandError(f"Host operation {filename} identity changed")
    return value


def _flatpak():
    return bool(os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").exists())


def _host_group_empty(ready):
    pid = ready.get("guard_pid")
    pgid = ready.get("pgid")
    sid = ready.get("sid")
    start_time = ready.get("guard_start_time")
    if (type(pid) is not int or pid <= 1 or pgid != pid or sid != pid or
            not isinstance(start_time, str) or not start_time.isdigit()):
        return False
    command = ["/usr/bin/sh", "-c", HOST_GROUP_QUERY,
               "hd2-host-group-query", str(pgid)]
    if _flatpak():
        command = ["flatpak-spawn", "--host", "--watch-bus", "--directory=/",
                   *command]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _host_operation_exists(operation):
    command = ["/usr/bin/sh", "-c", HOST_OPERATION_QUERY,
               "hd2-host-operation-query", str(operation.path)]
    environment = {**os.environ, "HD2_OPERATION_QUERY": operation.nonce}
    if _flatpak():
        command = ["flatpak-spawn", "--host", "--watch-bus",
                   f"--env=HD2_OPERATION_QUERY={operation.nonce}",
                   "--directory=/", *command]
        environment = None
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=2, check=False, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    return None


def mark_not_started(operation, status=125):
    try:
        _validate_state(operation, READY_FILE)
    except FileNotFoundError:
        pass
    else:
        return False
    write_state(operation, COMPLETE_FILE, {
        "command_status": status, "cleanup_confirmed": True,
        "not_started": True, "completed_at": _uptime_ticks()})
    return True


def reconcile_operation(operation, status=125):
    """Confirm a reserved operation could not have started host work."""
    try:
        _validate_state(operation, COMPLETE_FILE)
        return True
    except FileNotFoundError:
        pass
    try:
        _validate_state(operation, READY_FILE)
        return False
    except FileNotFoundError:
        pass
    try:
        _validate_state(operation, SUBMITTED_FILE)
    except FileNotFoundError:
        return mark_not_started(operation, status)
    if _uptime_ticks() < operation.job.hard_deadline:
        return False
    exists = _host_operation_exists(operation)
    if exists is not False:
        return False
    return mark_not_started(operation, status)


def wait_operation(operation, *, timeout=None):
    deadline = (time.monotonic() + timeout if timeout is not None else
                time.monotonic() +
                max(0, operation.job.hard_deadline - _uptime_ticks()) / 100 +
                DEFAULT_CLEANUP_SECONDS)
    while time.monotonic() < deadline:
        try:
            reconcile_operation(operation)
        except (OSError, ValueError, json.JSONDecodeError, HostCommandError):
            pass
        if (operation.path / COMPLETE_FILE).exists():
            try:
                value = _validate_state(operation, COMPLETE_FILE)
                status = value.get("command_status")
                confirmed = value.get("cleanup_confirmed") is True
                if type(status) is int and 0 <= status <= 255 and confirmed:
                    return HostCompletion(status, True)
            except (OSError, ValueError, json.JSONDecodeError, HostCommandError):
                pass
        if ((operation.path / READY_FILE).exists() and
                (operation.path / RESULT_FILE).exists()):
            try:
                ready = _validate_state(operation, READY_FILE)
                result = _validate_state(operation, RESULT_FILE)
                status = result.get("command_status")
                if type(status) is int and 0 <= status <= 255 and _host_group_empty(ready):
                    write_state(operation, COMPLETE_FILE, {
                        "command_status": status, "cleanup_confirmed": True,
                        "completed_at": _uptime_ticks()})
                    return HostCompletion(status, True)
            except (OSError, ValueError, json.JSONDecodeError, HostCommandError):
                pass
        time.sleep(.01)
    raise HostCleanupUnconfirmed(
        f"Host operation cleanup is unconfirmed: {operation.operation}")


def _operations(job):
    for path in sorted(job.path.glob("operation-*")):
        metadata = path.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or path.is_symlink() or
                metadata.st_uid != os.getuid() or
                stat.S_IMODE(metadata.st_mode) != 0o700):
            raise HostCommandError(f"Unsafe host operation path: {path}")
        intent = _read_json(path / INTENT_FILE)
        nonce = path.name.removeprefix("operation-")
        if (intent.get("owner") != OWNER or intent.get("version") != VERSION or
                intent.get("job_nonce") != job.nonce or
                intent.get("operation_nonce") != nonce):
            raise HostCommandError("Host operation intent identity changed")
        yield HostOperation(job, path, nonce, intent.get("operation", "unknown"))


def _remove_staging(job):
    for path in sorted(job.path.glob(".operation-stage-*")):
        metadata = path.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or path.is_symlink() or
                metadata.st_uid != os.getuid() or
                stat.S_IMODE(metadata.st_mode) != 0o700):
            raise HostCommandError(f"Unsafe staged host operation: {path}")
        shutil.rmtree(path)
    _fsync_directory(job.path)


def wait_host_job(job):
    warned = time.monotonic()
    while True:
        try:
            _validate_job(job)
            _remove_staging(job)
            operations = list(_operations(job))
            break
        except (OSError, ValueError, json.JSONDecodeError, HostCommandError):
            now = time.monotonic()
            if now - warned >= 30:
                logging.getLogger(__name__).warning(
                    "Waiting for valid host job ownership state")
                warned = now
            time.sleep(.01)
    for operation in operations:
        warned = time.monotonic()
        while True:
            try:
                reconcile_operation(operation)
                wait_operation(operation, timeout=1)
                break
            except (OSError, ValueError, json.JSONDecodeError,
                    HostCommandError, HostCleanupUnconfirmed):
                now = time.monotonic()
                if now - warned >= 30:
                    logging.getLogger(__name__).warning(
                        "Waiting for confirmed cleanup of host operation %s",
                        operation.operation)
                    warned = now
