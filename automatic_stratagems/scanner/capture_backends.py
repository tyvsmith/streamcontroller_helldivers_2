"""Capture Helldivers through its uniquely associated Gamescope socket."""

from concurrent.futures import CancelledError
from itertools import islice
import os
from pathlib import Path
import stat
import time

from .game_capture import (MAX_ENCODED_IMAGE_BYTES, ScanError, decode_image,
                           remaining_timeout)


BACKENDS = ('auto', 'gamescope', 'screenshot')


def read_frame(path):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ScanError('Screenshot is not a regular file.')
        chunks = []
        remaining = MAX_ENCODED_IMAGE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b''.join(chunks)
        if len(encoded) > MAX_ENCODED_IMAGE_BYTES:
            raise ScanError('Screenshot encoded image is too large.')
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ScanError('Screenshot changed while it was being read.')
        return decode_image(encoded, 'Screenshot')
    except OSError as error:
        raise ScanError(f'Cannot read screenshot: {error}') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def wait_frame(paths, timeout=5, cancel_event=None, deadline=None):
    frame_deadline = time.monotonic() + timeout
    if deadline is not None:
        frame_deadline = min(frame_deadline, deadline)
    stable = {}
    while time.monotonic() < frame_deadline:
        _check_cancel(cancel_event)
        candidates = list(islice(iter(paths()), 2))
        if len(candidates) > 1:
            raise ScanError('Multiple new screenshots; cannot identify this capture.')
        for path in candidates:
            try:
                metadata = path.stat()
                stamp = (metadata.st_dev, metadata.st_ino,
                         metadata.st_size, metadata.st_mtime_ns)
                if stable.get(path) == stamp and metadata.st_size:
                    return read_frame(path), path
                stable[path] = stamp
            except (OSError, ValueError):
                pass
        remaining = frame_deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(.1, remaining))
    _check_cancel(cancel_event)
    if deadline is not None and time.monotonic() >= deadline:
        raise ScanError('Scanner work deadline exhausted while waiting for screenshot.')
    raise ScanError('No complete screenshot received within 5 seconds.')


def gamescope_capture_target(deadline=None, cancel_event=None):
    from .capture.gamescope import HostMetadataError, resolve_gamescope_capture_target
    try:
        return resolve_gamescope_capture_target(
            timeout=remaining_timeout(deadline, 5), cancel_event=cancel_event)
    except HostMetadataError as error:
        raise ScanError(str(error)) from error


def capture_gamescope(cancel_event=None, deadline=None):
    from automatic_stratagems.host_commands import shared_host_directory
    from .game_capture import run_command

    _check_cancel(cancel_event)
    _check_deadline(deadline)
    target = gamescope_capture_target(deadline=deadline, cancel_event=cancel_event)
    _check_cancel(cancel_event)
    info = {'kind': 'live', 'backend': 'gamescope'}
    directory_options = ({'retain_on_error': True}
                         if target['kind'] == 'steam-flatpak' else {})
    with shared_host_directory(prefix='gamescope-', **directory_options) as directory:
        path = Path(directory) / 'capture.png'
        if target['kind'] == 'steam-flatpak':
            from .capture.gamescope import capture_into_shared_path
            capture_into_shared_path(
                target['target'], path, timeout=remaining_timeout(deadline, 5),
                deadline=deadline, cancel_event=cancel_event)
            info.update(socket=target['target'].socket, transport='flatpak-enter',
                        compatibility='unverified')
        else:
            socket = target['socket']
            run_command(['gamescopectl', 'screenshot', str(path), '1'],
                        timeout=remaining_timeout(deadline, 5),
                        host=True, operation='gamescope-capture', host_env={
                            'GAMESCOPE_WAYLAND_DISPLAY': socket},
                        cancel_event=cancel_event)
            info['socket'] = socket
        image, _ = wait_frame(
            lambda: [path] if path.exists() else [],
            cancel_event=cancel_event, deadline=deadline)
    return image, info


def capture_issue(image, backend):
    """Reject uniform frames and the known Steam false-color artifact."""
    import numpy as np
    preview = image.copy()
    preview.thumbnail((512, 216))
    rgb = np.asarray(preview.convert('RGB'))
    if float(rgb.std(axis=(0, 1)).max()) < .5:
        return 'Capture is a uniform blank frame.'
    if backend == 'steam':
        h, s, _ = np.asarray(preview.convert('HSV')).transpose(2, 0, 1)
        saturated = s > 180
        magenta = saturated & (h > 190) & (h < 235)
        green = saturated & (h > 50) & (h < 145)
        hud = image.crop((round(image.width * .018), round(image.height * .07),
                          round(image.width * .12), round(image.height * .42))).convert('HSV')
        pixels = np.asarray(hud)
        pale = ((pixels[:, :, 1] < 50) & (pixels[:, :, 2] > 180)).mean()
        if (saturated.mean() > .30 and magenta.mean() > .06 and
                green.mean() > .06 and pale < .025):
            return 'Steam capture shows extreme false-color artifacts.'
    return None


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError()


def _check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise ScanError('Scanner work deadline exhausted.')


def _capture_attempt(backend, recognize, screenshot, save_attempt, prepare,
                     cancel_event, deadline):
    if backend == 'gamescope':
        image, info = capture_gamescope(
            cancel_event=cancel_event, deadline=deadline)
        _check_cancel(cancel_event)
        _check_deadline(deadline)
        image = prepare(image)
        issue_backend = 'gamescope'
    else:
        if screenshot is None:
            raise ScanError('Screenshot capture is not configured.')
        from .screenshot_capture import capture_screenshot, is_steam_managed
        image, info = capture_screenshot(
            screenshot, cancel_event=cancel_event, deadline=deadline)
        _check_cancel(cancel_event)
        _check_deadline(deadline)
        issue_backend = ('steam' if is_steam_managed(info['path'])
                         else 'screenshot')
        if screenshot.get('trigger', 'none') == 'none':
            issue_backend = None

    _check_cancel(cancel_event)
    _check_deadline(deadline)
    if issue_backend is not None:
        issue = capture_issue(image, issue_backend)
        if issue:
            if save_attempt:
                save_attempt(backend, image, {
                    'status': 'capture_rejected', 'rows': [], 'reason': issue})
            raise ScanError(issue)
    if backend == 'screenshot':
        image = prepare(image)
    _check_cancel(cancel_event)
    _check_deadline(deadline)
    report = recognize(image)
    _check_cancel(cancel_event)
    _check_deadline(deadline)
    attempt = {
        'backend': backend,
        'status': report['status'],
        'matched': sum(bool(row['id']) for row in report['rows']),
        'icon_matches': sum(
            bool(row['id']) and row.get('method') not in
            ('mission-name', 'mission-arrows') for row in report['rows']),
    }
    if save_attempt:
        save_attempt(backend, image, report)
    info.update(kind=info.get('kind', 'file'), backend=backend)
    return image, info, report, attempt


def _attempt_warning(attempt):
    name = attempt['backend'].capitalize()
    if attempt['status'] == 'error':
        return f"Capture {name}: {attempt['reason']}"
    return (f"Capture {name}: {attempt['status']} "
            f"({attempt['matched']} matched)")


def scan_live(backend, recognize, save_attempt=None, prepare=lambda image: image,
              cancel_event=None, deadline=None, screenshot=None):
    """Capture once per configured alternative and keep the best recognition."""
    if backend not in BACKENDS:
        raise ScanError('Capture backend must be Auto, Gamescope or Screenshot.')
    names = ('gamescope', 'screenshot') if backend == 'auto' else (backend,)
    attempts = []
    best = None
    last_error = None

    for name in names:
        _check_cancel(cancel_event)
        _check_deadline(deadline)
        try:
            current = _capture_attempt(
                name, recognize, screenshot, save_attempt, prepare,
                cancel_event, deadline)
        except (ScanError, OSError, ValueError) as error:
            last_error = error
            attempts.append({'backend': name, 'status': 'error',
                             'reason': str(error)})
            _check_cancel(cancel_event)
            _check_deadline(deadline)
            if backend != 'auto':
                break
            continue

        attempts.append(current[3])
        if best is None:
            best = current
        elif current[3]['matched'] > best[3]['matched']:
            best = current
        if backend != 'auto' or current[2]['status'] == 'matched':
            break

    if best is None:
        detail = '; '.join(
            f"{attempt['backend']}: {attempt.get('reason', attempt['status'])}"
            for attempt in attempts)
        raise ScanError(f'No usable capture. {detail}') from last_error

    image, info, report, chosen = best
    info['attempts'] = attempts
    report['warnings'].extend(
        _attempt_warning(attempt) for attempt in attempts
        if attempt is not chosen)
    return image, info, report
