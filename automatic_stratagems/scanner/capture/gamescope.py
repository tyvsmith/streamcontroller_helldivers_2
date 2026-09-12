"""Sandbox-side callers that launch the hostexec Gamescope scripts."""

import json
from pathlib import Path
import re
import time

from automatic_stratagems import hostexec
from automatic_stratagems.shared.gamescope_target import (
    FlatpakGamescopeTarget, HostMetadataError, RECORD_NAME)


def resolve_gamescope_capture_target(timeout=5, cancel_event=None):
    """Classify the host-native or Steam-Flatpak Gamescope capture target."""
    from automatic_stratagems.host_commands import HostCleanupUnconfirmed
    from ..game_capture import ScanError, run_command
    command = ['/usr/bin/python3', str(hostexec.script('host_metadata.py')),
               '--capture-target']
    try:
        output = run_command(
            command, timeout=timeout, stdout_limit=4096, stderr_limit=16 * 1024,
            host=True, operation='gamescope-capture-target',
            cancel_event=cancel_event)
        value = json.loads(output.decode())
        if not isinstance(value, dict):
            raise HostMetadataError(
                'Host Gamescope capture target response is invalid.')
        if value.get('kind') == 'native' and set(value) == {'kind', 'socket'}:
            socket = value['socket']
            if (not isinstance(socket, str) or '\0' in socket or
                    not re.fullmatch(r'/run/user/\d+/gamescope-\d+', socket)):
                raise HostMetadataError(
                    'Host Gamescope capture target response is invalid.')
            return value
        if (value.get('kind') == 'steam-flatpak' and
                set(value) == {'kind', 'target'}):
            return {'kind': 'steam-flatpak',
                    'target': FlatpakGamescopeTarget.from_dict(value['target'])}
        raise HostMetadataError(
            'Host Gamescope capture target response is invalid.')
    except HostCleanupUnconfirmed:
        raise
    except (OSError, UnicodeError, ValueError, RuntimeError, ScanError,
            HostMetadataError) as error:
        raise HostMetadataError(
            f'Cannot inspect host Gamescope capture target: {error}') from error


def capture_into_shared_path(target, output, *, timeout, deadline,
                             cancel_event=None):
    from automatic_stratagems.host_commands import (HostArtifactCleanupError,
                                                    HostCleanupUnconfirmed,
                                                    _job_from_environment)
    from ..game_capture import run_command
    job = _job_from_environment()
    if deadline is None:
        deadline = time.monotonic() + timeout
    job_args = [str(job.path), job.nonce, str(job.hard_deadline)]
    script = str(hostexec.script('gamescope_flatpak.py'))
    capture = ['/usr/bin/python3', script, '--capture',
               json.dumps(target.to_dict(), separators=(',', ':')), str(output),
               '--deadline', repr(deadline), '--job', *job_args]
    primary = None
    result = None
    try:
        result = run_command(
            capture, timeout=timeout, stdout_limit=1024,
            stderr_limit=16 * 1024, host=True,
            operation='gamescope-flatpak-capture', cancel_event=cancel_event)
    except HostCleanupUnconfirmed:
        raise
    except BaseException as error:
        primary = error
    cleanup = ['/usr/bin/python3', script, '--cleanup',
               str(Path(output).parent / RECORD_NAME), '--job', *job_args]
    try:
        run_command(cleanup, timeout=2, stdout_limit=1024,
                    stderr_limit=16 * 1024, host=True,
                    operation='gamescope-flatpak-cleanup')
    except HostCleanupUnconfirmed as cleanup_error:
        if primary is not None:
            raise cleanup_error from primary
        raise
    except BaseException as cleanup_error:
        artifact_error = HostArtifactCleanupError(
                f'Gamescope artifact cleanup failed; record retained at '
                f'{Path(output).parent / RECORD_NAME}')
        if primary is not None:
            cleanup_error.__cause__ = primary
        raise artifact_error from cleanup_error
    if primary is not None:
        raise primary
    return result
