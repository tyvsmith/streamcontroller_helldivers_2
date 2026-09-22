"""Capture one Gamescope frame through an identified Steam Flatpak."""

import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from automatic_stratagems.hostexec.host_metadata import (
    validate_flatpak_gamescope_target)
from automatic_stratagems.shared.fs import (
    file_stamp, fsync_directory, read_capped, wait_for_stable_stamp, write_all)
from automatic_stratagems.shared.gamescope_target import (
    FlatpakGamescopeTarget, HostMetadataError, RECORD_NAME)
from automatic_stratagems.shared.host_job import HostJob, validate_job

MAX_ENCODED_IMAGE_BYTES = 64 * 1024 * 1024
RECORD_LIMIT = 4096


class FlatpakGamescopeError(RuntimeError):
    pass


class _Interrupted(Exception):
    pass


def _validate_cache(target):
    cache = Path(target.host_cache)
    try:
        metadata = cache.stat()
    except OSError as error:
        raise FlatpakGamescopeError('Steam capture cache is unavailable.') from error
    if (not stat.S_ISDIR(metadata.st_mode) or
            (metadata.st_dev, metadata.st_ino) !=
            (target.cache_dev, target.cache_ino)):
        raise FlatpakGamescopeError('Steam capture cache identity changed.')
    return cache


def _validate_capture_directory(target, directory, proc_root):
    directory = Path(directory)
    cache = _validate_cache(target)
    if (directory.parent != cache or
            not directory.name.startswith('hd2-gamescope-')):
        raise FlatpakGamescopeError('Steam capture directory is invalid.')
    try:
        metadata = directory.lstat()
        alias = (Path(proc_root) / str(target.sandbox_pid) / 'root' /
                 Path(target.sandbox_cache).relative_to('/') /
                 directory.name).stat()
    except (OSError, ValueError) as error:
        raise FlatpakGamescopeError(
            'Steam capture directory mapping is unavailable.') from error
    if (not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink() or
            metadata.st_uid != os.getuid() or
            stat.S_IMODE(metadata.st_mode) != 0o700 or
            (metadata.st_dev, metadata.st_ino) != (alias.st_dev, alias.st_ino)):
        raise FlatpakGamescopeError(
            'Steam capture directory mapping identity changed.')
    return metadata


def _validate_job_output(job, output):
    validate_job(job)
    output = Path(output)
    if output.name != 'capture.png' or output.parent.parent != job.path:
        raise FlatpakGamescopeError('Gamescope output is outside its host job.')
    try:
        parent = output.parent.lstat()
    except OSError as error:
        raise FlatpakGamescopeError('Gamescope output directory is unavailable.') from error
    if (not stat.S_ISDIR(parent.st_mode) or output.parent.is_symlink() or
            parent.st_uid != os.getuid() or
            stat.S_IMODE(parent.st_mode) != 0o700 or output.exists()):
        raise FlatpakGamescopeError('Gamescope output path is unsafe.')
    return parent


def _write_record(path, value):
    encoded = (json.dumps(value, separators=(',', ':')) + '\n').encode()
    if len(encoded) > RECORD_LIMIT:
        raise FlatpakGamescopeError('Gamescope cleanup record is too large.')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW, 0o600)
    try:
        write_all(descriptor, encoded, 'short write')
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(Path(path).parent)


def _read_record(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        value = read_capped(descriptor, RECORD_LIMIT, 4096)
    finally:
        os.close(descriptor)
    if len(value) > RECORD_LIMIT:
        raise FlatpakGamescopeError('Gamescope cleanup record is too large.')
    result = json.loads(value)
    keys = {'version', 'owner', 'job_nonce', 'cache', 'cache_dev',
            'cache_ino', 'directory', 'directory_dev', 'directory_ino',
            'output_dev', 'output_ino', 'uid', 'mode'}
    if (not isinstance(result, dict) or set(result) != keys or
            result.get('version') != 1 or
            result.get('owner') != 'net_jslay_helldivers_2' or
            any(type(result.get(name)) is not int or result[name] < 0
                for name in ('cache_dev', 'cache_ino', 'directory_dev',
                             'directory_ino', 'output_dev', 'output_ino',
                             'uid', 'mode')) or
            any(not isinstance(result.get(name), str) or
                not result[name] or '\0' in result[name]
                for name in ('job_nonce', 'cache', 'directory'))):
        raise FlatpakGamescopeError('Gamescope cleanup record is invalid.')
    return result, identity


def _wait_stable(path, deadline):
    def probe():
        try:
            metadata = path.lstat()
            if (stat.S_ISREG(metadata.st_mode) and not path.is_symlink() and
                    metadata.st_size > MAX_ENCODED_IMAGE_BYTES):
                raise FlatpakGamescopeError(
                    'Gamescope screenshot exceeds the encoded image limit.')
            if (stat.S_ISREG(metadata.st_mode) and not path.is_symlink() and
                    metadata.st_size > 0):
                return path, file_stamp(metadata), True
        except OSError:
            pass
        return None

    stamp = wait_for_stable_stamp(probe, deadline, interval=.05)
    if stamp is None:
        raise FlatpakGamescopeError(
            'Gamescope did not create a complete screenshot before the deadline.')
    return stamp


def _copy_frame(source, output, expected):
    source_fd = output_fd = None
    temporary = Path(output).parent / f'.capture-{uuid.uuid4().hex}.tmp'
    published = False
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        metadata = os.fstat(source_fd)
        if (file_stamp(metadata) != expected or
                not stat.S_ISREG(metadata.st_mode)):
            raise FlatpakGamescopeError('Gamescope screenshot identity changed.')
        output_fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600)
        remaining = MAX_ENCODED_IMAGE_BYTES + 1
        copied = 0
        while remaining:
            chunk = os.read(source_fd, min(64 * 1024, remaining))
            if not chunk:
                break
            write_all(output_fd, chunk, 'short write')
            remaining -= len(chunk)
            copied += len(chunk)
        if remaining == 0:
            raise FlatpakGamescopeError(
                'Gamescope screenshot exceeds the encoded image limit.')
        if copied != expected[2]:
            raise FlatpakGamescopeError(
                'Gamescope screenshot ended before its recorded size.')
        after = os.fstat(source_fd)
        if file_stamp(after) != expected:
            raise FlatpakGamescopeError(
                'Gamescope screenshot changed while it was copied.')
        os.fsync(output_fd)
        os.close(output_fd)
        output_fd = None
        os.link(temporary, output, follow_symlinks=False)
        temporary.unlink()
        fsync_directory(Path(output).parent)
        published = True
    finally:
        if output_fd is not None:
            os.close(output_fd)
        if source_fd is not None:
            os.close(source_fd)
        if not published:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _remove_recorded_directory(record, job):
    validate_job(job)
    record = Path(record)
    if record.name != RECORD_NAME or record.parent.parent != job.path:
        raise FlatpakGamescopeError('Gamescope cleanup record path is unsafe.')
    try:
        output_parent = record.parent.lstat()
    except OSError as error:
        raise FlatpakGamescopeError(
            'Gamescope cleanup output directory is unavailable.') from error
    if (not stat.S_ISDIR(output_parent.st_mode) or record.parent.is_symlink() or
            output_parent.st_uid != os.getuid() or
            stat.S_IMODE(output_parent.st_mode) != 0o700):
        raise FlatpakGamescopeError(
            'Gamescope cleanup output directory is unsafe.')
    if not record.exists():
        return
    value, record_identity = _read_record(record)
    if value['job_nonce'] != job.nonce or value['uid'] != os.getuid():
        raise FlatpakGamescopeError('Gamescope cleanup ownership changed.')
    if (output_parent.st_dev, output_parent.st_ino) != (
            value['output_dev'], value['output_ino']):
        raise FlatpakGamescopeError(
            'Gamescope cleanup output directory changed.')
    cache = Path(value['cache'])
    directory = Path(value['directory'])
    if directory.parent != cache or not directory.name.startswith('hd2-gamescope-'):
        raise FlatpakGamescopeError('Gamescope cleanup target is unsafe.')
    cache_fd = directory_fd = None
    try:
        cache_fd = os.open(cache, os.O_RDONLY | os.O_DIRECTORY)
        cache_stat = os.fstat(cache_fd)
        if (cache_stat.st_dev, cache_stat.st_ino) != (
                value['cache_dev'], value['cache_ino']):
            raise FlatpakGamescopeError('Gamescope cleanup cache changed.')
        directory_fd = os.open(directory.name, os.O_RDONLY | os.O_DIRECTORY |
                               os.O_NOFOLLOW, dir_fd=cache_fd)
        directory_stat = os.fstat(directory_fd)
        if ((directory_stat.st_dev, directory_stat.st_ino) !=
                (value['directory_dev'], value['directory_ino']) or
                directory_stat.st_uid != value['uid'] or
                stat.S_IMODE(directory_stat.st_mode) != value['mode']):
            raise FlatpakGamescopeError('Gamescope cleanup directory changed.')
        children = os.listdir(directory_fd)
        if any(name != 'capture.png' for name in children):
            raise FlatpakGamescopeError(
                'Gamescope cleanup directory has unexpected files.')
        if children:
            child = os.stat('capture.png', dir_fd=directory_fd,
                            follow_symlinks=False)
            if not stat.S_ISREG(child.st_mode):
                raise FlatpakGamescopeError('Gamescope cleanup file is unsafe.')
            os.unlink('capture.png', dir_fd=directory_fd)
        os.close(directory_fd)
        directory_fd = None
        os.rmdir(directory.name, dir_fd=cache_fd)
        current_record = record.lstat()
        if (current_record.st_dev, current_record.st_ino) != record_identity:
            raise FlatpakGamescopeError('Gamescope cleanup record changed.')
        record.unlink()
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        if cache_fd is not None:
            os.close(cache_fd)


def capture_on_host(target, output, *, deadline, job, proc_root=Path('/proc')):
    """Run inside the bounded host worker; no image decoding happens here."""
    if not math.isfinite(deadline) or deadline <= time.monotonic():
        raise FlatpakGamescopeError('Gamescope capture deadline exhausted.')
    output = Path(output)
    output_parent = _validate_job_output(job, output)
    cache = _validate_cache(target)
    directory = Path(tempfile.mkdtemp(prefix='hd2-gamescope-', dir=cache))
    directory.chmod(0o700)
    directory_stat = directory.stat()
    try:
        _write_record(output.parent / RECORD_NAME, {
            'version': 1, 'owner': 'net_jslay_helldivers_2',
            'job_nonce': job.nonce, 'cache': str(cache),
            'cache_dev': target.cache_dev, 'cache_ino': target.cache_ino,
            'directory': str(directory), 'directory_dev': directory_stat.st_dev,
            'directory_ino': directory_stat.st_ino,
            'output_dev': output_parent.st_dev,
            'output_ino': output_parent.st_ino,
            'uid': os.getuid(), 'mode': 0o700})
    except BaseException:
        directory.rmdir()
        raise
    old_handlers = {}

    def interrupted(_signum, _frame):
        raise _Interrupted()

    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            old_handlers[number] = signal.signal(number, interrupted)
    try:
        validate_flatpak_gamescope_target(target, proc_root=proc_root)
        _validate_capture_directory(target, directory, proc_root)
        validate_flatpak_gamescope_target(target, proc_root=proc_root)
        sandbox_output = (Path(target.sandbox_cache) / directory.name /
                          'capture.png')
        command = ['/usr/bin/flatpak', 'enter', str(target.sandbox_pid),
                   '/usr/bin/env',
                   f'GAMESCOPE_WAYLAND_DISPLAY={target.socket}',
                   target.gamescopectl, 'screenshot', str(sandbox_output), '1']
        result = subprocess.run(command, stdin=subprocess.DEVNULL, check=False)
        if result.returncode:
            raise FlatpakGamescopeError(
                f'flatpak enter failed with status {result.returncode}.')
        validate_flatpak_gamescope_target(target, proc_root=proc_root)
        _validate_capture_directory(target, directory, proc_root)
        expected = _wait_stable(directory / 'capture.png', deadline)
        _copy_frame(directory / 'capture.png', output, expected)
    except _Interrupted as error:
        raise FlatpakGamescopeError('Gamescope Flatpak capture interrupted.') from error
    finally:
        for number, handler in old_handlers.items():
            signal.signal(number, handler)


def _job_from_args(values):
    if len(values) != 3:
        raise FlatpakGamescopeError('Invalid host job arguments.')
    return HostJob(Path(values[0]), values[1], int(values[2]))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if '--job' not in argv:
            raise FlatpakGamescopeError('Missing host job arguments.')
        split = argv.index('--job')
        command = argv[:split]
        job = _job_from_args(argv[split + 1:])
        if len(command) == 2 and command[0] == '--cleanup':
            _remove_recorded_directory(Path(command[1]), job)
            return 0
        if (len(command) != 5 or command[0] != '--capture' or
                command[3] != '--deadline'):
            raise FlatpakGamescopeError('Invalid Flatpak Gamescope arguments.')
        target = FlatpakGamescopeTarget.from_dict(json.loads(command[1]))
        capture_on_host(target, Path(command[2]), deadline=float(command[4]),
                        job=job)
        return 0
    except (FlatpakGamescopeError, HostMetadataError, OSError, ValueError,
            json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
