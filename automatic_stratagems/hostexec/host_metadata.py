"""Bounded host lookup for Helldivers' uniquely associated Gamescope socket."""

import configparser
import io
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from automatic_stratagems.shared.fs import read_capped
from automatic_stratagems.shared.gamescope_target import (
    FlatpakGamescopeTarget, GAMESCOPECTL_PATH, HostMetadataError)


STEAM_APP_ID = b'553850'
GAME_NAMES = {'helldivers2', 'helldivers2.exe'}
MAX_PROC_ENTRIES = 32_768
MAX_ENVIRON_BYTES = 128 * 1024
MAX_PROCESS_TEXT_BYTES = 16 * 1024
MAX_STATUS_BYTES = 64 * 1024
MAX_FDS_PER_PROCESS = 16_384
MAX_UNIX_SOCKET_BYTES = 4 * 1024 * 1024
MAX_FLATPAK_INFO_BYTES = 64 * 1024
STEAM_FLATPAK_ID = 'com.valvesoftware.Steam'
GAMESCOPE_LIBRARY_PATH = '/usr/lib/extensions/vulkan/gamescope/lib'


def _read_bounded(path, limit):
    descriptor = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, 'O_CLOEXEC'):
            flags |= os.O_CLOEXEC
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        value = read_capped(descriptor, limit, 64 * 1024)
        if len(value) > limit:
            raise HostMetadataError(f'Host metadata file is too large: {path}')
        return value
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _optional_bounded(path, limit):
    try:
        return _read_bounded(path, limit)
    except (HostMetadataError, OSError):
        return None


def _process_ids(proc_root):
    pids = []
    count = 0
    try:
        with os.scandir(proc_root) as entries:
            for entry in entries:
                count += 1
                if count > MAX_PROC_ENTRIES:
                    raise HostMetadataError('Too many host processes to inspect safely.')
                if not entry.name.isdigit():
                    continue
                pids.append(int(entry.name))
    except HostMetadataError:
        raise
    except OSError as error:
        raise HostMetadataError(f'Cannot inspect host processes: {error}') from error
    return sorted(pids)


def _has_steam_app_id(environment):
    return any(entry in (b'SteamAppId=' + STEAM_APP_ID,
                         b'SteamGameId=' + STEAM_APP_ID)
               for entry in environment.split(b'\0'))


def _environment_value(environment, name):
    prefix = name.encode() + b'='
    values = [entry[len(prefix):] for entry in environment.split(b'\0')
              if entry.startswith(prefix)]
    if len(values) != 1:
        raise HostMetadataError(f'Host process {name} is unavailable.')
    try:
        value = values[0].decode()
    except UnicodeError as error:
        raise HostMetadataError(f'Host process {name} is invalid.') from error
    if not value or '\0' in value:
        raise HostMetadataError(f'Host process {name} is invalid.')
    return value


def _game_process_name(directory):
    names = set()
    comm = _optional_bounded(directory / 'comm', MAX_PROCESS_TEXT_BYTES)
    if comm is not None:
        names.add(comm.decode(errors='replace').strip().casefold())
    cmdline = _optional_bounded(directory / 'cmdline', MAX_PROCESS_TEXT_BYTES)
    if cmdline is not None:
        executable = cmdline.split(b'\0', 1)[0]
        names.add(PureWindowsPath(executable.decode(errors='replace')).name.casefold())
    try:
        names.add(Path(os.readlink(directory / 'exe')).name.casefold())
    except OSError:
        pass
    return bool(names & GAME_NAMES)


def helldivers_process_pid(*, proc_root=Path('/proc')):
    """Return the one process carrying the app ID and game executable identity."""
    proc_root = Path(proc_root)
    matches = []
    for pid in _process_ids(proc_root):
        directory = proc_root / str(pid)
        environment = _optional_bounded(directory / 'environ',
                                        MAX_ENVIRON_BYTES)
        if (environment is not None and _has_steam_app_id(environment) and
                _game_process_name(directory)):
            matches.append(pid)
    if not matches:
        raise HostMetadataError('No Helldivers 2 game process is running.')
    if len(matches) != 1:
        raise HostMetadataError('Multiple Helldivers 2 game processes are running.')
    return matches[0]


def _parent_pid(proc_root, pid):
    status = _read_bounded(proc_root / str(pid) / 'status', MAX_STATUS_BYTES)
    match = re.search(rb'^PPid:\s+(\d+)', status, re.M)
    return int(match.group(1)) if match is not None else None


def _ancestry(proc_root, pid):
    owners = set()
    for _ in range(64):
        if not isinstance(pid, int) or pid <= 1 or pid in owners:
            break
        owners.add(pid)
        try:
            pid = _parent_pid(proc_root, pid)
        except (HostMetadataError, OSError, ValueError):
            break
    return owners


def _socket_inodes(proc_root, owners):
    inodes = set()
    for owner in owners:
        try:
            with os.scandir(proc_root / str(owner) / 'fd') as descriptors:
                count = 0
                for descriptor in descriptors:
                    count += 1
                    if count > MAX_FDS_PER_PROCESS:
                        raise HostMetadataError(
                            'Too many host process descriptors to inspect safely.')
                    try:
                        target = os.readlink(descriptor.path)
                    except OSError:
                        continue
                    match = re.fullmatch(r'socket:\[(\d+)\]', target)
                    if match:
                        inodes.add(match.group(1))
        except HostMetadataError:
            raise
        except OSError:
            continue
    return inodes


def _socket_inodes_by_owner(proc_root, owners):
    return {owner: _socket_inodes(proc_root, {owner}) for owner in owners}


def process_start_time(pid, *, proc_root=Path('/proc')):
    try:
        value = _read_bounded(
            Path(proc_root) / str(pid) / 'stat', MAX_PROCESS_TEXT_BYTES)
        fields = value.decode().rsplit(')', 1)[1].split()
        start_time = fields[19]
    except (HostMetadataError, OSError, UnicodeError, IndexError) as error:
        raise HostMetadataError('Cannot read Helldivers process identity.') from error
    if not start_time.isdigit():
        raise HostMetadataError('Helldivers process identity is invalid.')
    return start_time


def _flatpak_info(pid, proc_root):
    path = Path(proc_root) / str(pid) / 'root' / '.flatpak-info'
    try:
        text = _read_bounded(path, MAX_FLATPAK_INFO_BYTES).decode()
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_file(io.StringIO(text))
        app_id = parser.get('Application', 'name')
        instance_id = parser.get('Instance', 'instance-id')
        instance_path = parser.get('Instance', 'instance-path')
    except (HostMetadataError, OSError, UnicodeError, configparser.Error,
            KeyError) as error:
        raise HostMetadataError(
            'Helldivers Steam Flatpak identity is unavailable.') from error
    if (app_id != STEAM_FLATPAK_ID or
            not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', instance_id) or
            not Path(instance_path).is_absolute()):
        raise HostMetadataError('Helldivers is not in the expected Steam Flatpak.')
    return instance_id, Path(instance_path)


def _flatpak_info_presence(pid, proc_root):
    try:
        (Path(proc_root) / str(pid) / 'root' / '.flatpak-info').stat()
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        raise HostMetadataError(
            'Helldivers sandbox identity cannot be inspected.') from error


def _sandbox_path(proc_root, pid, path):
    path = Path(path)
    if not path.is_absolute():
        raise HostMetadataError('Steam sandbox path is invalid.')
    return Path(proc_root) / str(pid) / 'root' / path.relative_to('/')


def _directory_identity(path, message):
    try:
        metadata = Path(path).stat()
    except OSError as error:
        raise HostMetadataError(message) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise HostMetadataError(message)
    return metadata.st_dev, metadata.st_ino


def _flatpak_socket_target(game_pid, proc_root, runtime_dir):
    owners = _ancestry(proc_root, game_pid)
    by_owner = _socket_inodes_by_owner(proc_root, owners)
    all_inodes = set().union(*by_owner.values()) if by_owner else set()
    try:
        socket_table = _read_bounded(
            proc_root / 'net' / 'unix', MAX_UNIX_SOCKET_BYTES)
        lines = socket_table.decode(errors='replace').splitlines()[1:]
    except OSError as error:
        raise HostMetadataError(
            f'Cannot inspect host Unix sockets: {error}') from error

    candidates = set()
    for line in lines:
        fields = line.split(maxsplit=7)
        if len(fields) != 8 or fields[6] not in all_inodes:
            continue
        socket = Path(fields[7])
        if (socket.parent != runtime_dir or
                not re.fullmatch(r'gamescope-\d+', socket.name)):
            continue
        for owner, inodes in by_owner.items():
            if fields[6] not in inodes:
                continue
            try:
                metadata = _sandbox_path(proc_root, owner, socket).stat()
            except OSError:
                continue
            if stat.S_ISSOCK(metadata.st_mode):
                candidates.add((owner, str(socket), metadata.st_dev,
                                metadata.st_ino))
    if len(candidates) != 1:
        raise HostMetadataError(
            'Cannot uniquely associate a Steam-sandbox Gamescope socket '
            'with Helldivers 2.')
    return candidates.pop()


def flatpak_gamescope_target(*, proc_root=Path('/proc'), runtime_dir=None,
                             host_home=None):
    """Prove the exact Steam sandbox, socket, extension and shared cache."""
    proc_root = Path(proc_root)
    game_pid = helldivers_process_pid(proc_root=proc_root)
    game_start_time = process_start_time(game_pid, proc_root=proc_root)
    runtime_dir = (Path(f'/run/user/{os.getuid()}') if runtime_dir is None
                   else Path(runtime_dir))
    sandbox_pid, socket, socket_dev, socket_ino = _flatpak_socket_target(
        game_pid, proc_root, runtime_dir)
    sandbox_start_time = process_start_time(sandbox_pid, proc_root=proc_root)
    instance_id, instance_path = _flatpak_info(sandbox_pid, proc_root)
    if host_home is not None:
        expected = (Path(host_home) / '.var' / 'app' / STEAM_FLATPAK_ID)
        if instance_path != expected:
            raise HostMetadataError('Steam Flatpak instance path is invalid.')

    environment = _read_bounded(
        proc_root / str(sandbox_pid) / 'environ', MAX_ENVIRON_BYTES)
    home = Path(_environment_value(environment, 'HOME'))
    try:
        sandbox_cache = Path(_environment_value(environment, 'XDG_CACHE_HOME'))
    except HostMetadataError:
        sandbox_cache = home / '.cache'
    if not home.is_absolute() or not sandbox_cache.is_absolute():
        raise HostMetadataError('Steam sandbox cache path is invalid.')

    host_cache = instance_path / 'cache'
    host_identity = _directory_identity(
        host_cache, 'Steam host cache is unavailable.')
    sandbox_identity = _directory_identity(
        _sandbox_path(proc_root, sandbox_pid, sandbox_cache),
        'Steam sandbox cache mapping is unavailable.')
    if host_identity != sandbox_identity:
        raise HostMetadataError('Steam cache mapping identity does not match.')

    gamescopectl = _sandbox_path(proc_root, sandbox_pid, GAMESCOPECTL_PATH)
    library_path = _sandbox_path(proc_root, sandbox_pid, GAMESCOPE_LIBRARY_PATH)
    if not gamescopectl.is_file() or not os.access(gamescopectl, os.X_OK):
        raise HostMetadataError(
            'Steam Gamescope extension gamescopectl is unavailable.')
    if not library_path.is_dir():
        raise HostMetadataError(
            'Steam Gamescope extension library path is unavailable.')

    return FlatpakGamescopeTarget(
        game_pid=game_pid, game_start_time=game_start_time,
        sandbox_pid=sandbox_pid, sandbox_start_time=sandbox_start_time,
        instance_id=instance_id, socket=socket, socket_dev=socket_dev,
        socket_ino=socket_ino, host_cache=str(host_cache),
        sandbox_cache=str(sandbox_cache), cache_dev=host_identity[0],
        cache_ino=host_identity[1], gamescopectl=GAMESCOPECTL_PATH)


def gamescope_capture_target(*, proc_root=Path('/proc'), runtime_dir=None):
    """Classify the exact game-associated socket owner without guessing."""
    proc_root = Path(proc_root)
    game_pid = helldivers_process_pid(proc_root=proc_root)
    runtime_dir = (Path(f'/run/user/{os.getuid()}') if runtime_dir is None
                   else Path(runtime_dir))
    sandbox_pid, socket, socket_dev, socket_ino = _flatpak_socket_target(
        game_pid, proc_root, runtime_dir)
    if _flatpak_info_presence(sandbox_pid, proc_root):
        target = flatpak_gamescope_target(
            proc_root=proc_root, runtime_dir=runtime_dir)
        return {'kind': 'steam-flatpak', 'target': target.to_dict()}
    try:
        metadata = Path(socket).stat()
    except OSError as error:
        raise HostMetadataError(
            'Native Gamescope socket is unavailable on the host.') from error
    if ((metadata.st_dev, metadata.st_ino) != (socket_dev, socket_ino) or
            not stat.S_ISSOCK(metadata.st_mode)):
        raise HostMetadataError('Native Gamescope socket identity does not match.')
    return {'kind': 'native', 'socket': socket}


def validate_flatpak_gamescope_target(target, *, proc_root=Path('/proc')):
    if (process_start_time(target.game_pid, proc_root=proc_root) !=
            target.game_start_time or
            process_start_time(target.sandbox_pid, proc_root=proc_root) !=
            target.sandbox_start_time):
        raise HostMetadataError('Helldivers Steam Flatpak target changed.')
    actual = flatpak_gamescope_target(proc_root=proc_root)
    if actual != target:
        raise HostMetadataError('Helldivers Steam Flatpak target changed.')
    return target


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv != ['--capture-target']:
            raise HostMetadataError('Expected --capture-target.')
        value = gamescope_capture_target()
        print(json.dumps(value, separators=(',', ':')))
        return 0
    except HostMetadataError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
