"""Live capture alternatives; each returns a fresh frame from the focused game."""
from concurrent.futures import CancelledError
import os
from pathlib import Path
import re
import tempfile
import time

from PIL import Image

from .game_capture import (MAX_ENCODED_IMAGE_BYTES, ScanError, capture_game,
                           decode_image, query_active_window, window_geometry)
from .platform_capture import (capture_x11, query_x11_window, same_window,
                               session_kind, validate_game_window)

BACKENDS = ('auto', 'gamescope', 'steam', 'desktop', 'portal', 'x11')


class FocusChanged(ScanError):
    pass


def check_focus(before):
    if before is None:
        return
    if before.get('platform') == 'x11':
        after = query_x11_window()
        validate_game_window(after)
        if not same_window(before, after):
            raise FocusChanged('Game focus changed; scan discarded.')
        return
    after = query_active_window()
    window_geometry(after)
    if any(before.get(k) != after.get(k) for k in ('address', 'pid', 'class', 'at', 'size')):
        raise FocusChanged('Game focus or geometry changed; scan discarded.')


def read_frame(path):
    try:
        if path.stat().st_size > MAX_ENCODED_IMAGE_BYTES:
            raise ScanError('Screenshot encoded image is too large.')
        return decode_image(path.read_bytes(), 'Screenshot')
    except OSError as error:
        raise ScanError(f'Cannot read screenshot: {error}') from error


def wait_frame(paths, before, timeout=5):
    deadline = time.monotonic() + timeout
    stable = {}
    while time.monotonic() < deadline:
        check_focus(before)
        candidates = list(paths())
        if len(candidates) > 1:
            raise ScanError('Multiple new screenshots; cannot identify this capture.')
        for path in candidates:
            try:
                stat = path.stat()
                stamp = (stat.st_size, stat.st_mtime_ns)
                if stable.get(path) == stamp and stat.st_size:
                    return read_frame(path), path
                stable[path] = stamp
            except (OSError, ValueError):
                pass
        time.sleep(.1)
    raise ScanError('No complete screenshot received within 5 seconds.')


def gamescope_socket(before):
    if before.get('class', '').lower() != 'gamescope':
        raise ScanError('Focused game is not running in Gamescope.')
    # Bind the socket to the focused window process or one of its ancestors.
    owners = set()
    pid = before.get('pid')
    for _ in range(64):
        if not isinstance(pid, int) or pid <= 1 or pid in owners:
            break
        owners.add(pid)
        try:
            status = Path(f'/proc/{pid}/status').read_text()
            pid = int(re.search(r'^PPid:\s+(\d+)', status, re.M)[1])
        except (OSError, TypeError):
            break
    inodes = set()
    for pid in owners:
        try:
            for fd in Path(f'/proc/{pid}/fd').iterdir():
                try:
                    target = os.readlink(fd)
                    if target.startswith('socket:['):
                        inodes.add(target[8:-1])
                except OSError:
                    pass
        except OSError:
            pass
    matches = set()
    for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) == 8 and fields[6] in inodes:
            path = Path(fields[7])
            if re.fullmatch(r'gamescope-\d+', path.name) and path.is_socket():
                matches.add(path)
    if len(matches) != 1:
        raise ScanError('Cannot uniquely associate a Gamescope socket with the focused game.')
    return str(matches.pop())


def steam_directories():
    roots = [Path.home()/'.local/share/Steam', Path.home()/'.steam/steam',
             Path.home()/'.var/app/com.valvesoftware.Steam/.local/share/Steam']
    return sorted({p.resolve() for root in roots for p in
                   root.glob('userdata/*/760/remote/553850/screenshots') if p.is_dir()})


def steam_frame(before, paths):
    try:
        from evdev import UInput, ecodes
    except ImportError as error:
        raise ScanError("Steam capture requires Python evdev; install automatic_stratagems/requirements.txt.") from error
    with UInput({ecodes.EV_KEY: range(1, 256)}, name="HD2 Screenshot Keyboard") as keyboard:
        # Allow the compositor to discover the new keyboard before sending F12.
        time.sleep(.3)
        check_focus(before)
        try:
            keyboard.write(ecodes.EV_KEY, ecodes.KEY_F12, 1)
            keyboard.syn()
            time.sleep(.03)
        finally:
            keyboard.write(ecodes.EV_KEY, ecodes.KEY_F12, 0)
            keyboard.syn()
        # Keep the device alive until Steam has consumed the input.
        return wait_frame(paths, before)


def capture(backend, before, cancel_event=None):
    from .game_capture import run_command
    check_focus(before)
    if backend == 'desktop':
        im, info = capture_game()
    elif backend == 'portal':
        from .portal_capture import capture_portal
        im, info = capture_portal(cancel_event=cancel_event)
    elif backend == 'x11':
        im, info = capture_x11(before, cancel_event=cancel_event)
    elif backend == 'gamescope':
        socket = gamescope_socket(before)
        with tempfile.TemporaryDirectory(prefix='hd2-gamescope-') as directory:
            path = Path(directory)/'capture.png'
            run_command(['env', f'GAMESCOPE_WAYLAND_DISPLAY={socket}',
                         'gamescopectl', 'screenshot', str(path), '1'], timeout=5)
            im, _ = wait_frame(lambda: [path] if path.exists() else [], before)
        info = {'kind': 'live', 'socket': socket}
    elif backend == 'steam':
        directories = steam_directories()
        if not directories:
            raise ScanError('No Helldivers Steam screenshot directory found.')
        def screenshots():
            return {p for directory in directories for p in directory.iterdir()
                    if p.is_file() and p.suffix.lower() in ('.jpg', '.jpeg', '.png')}
        existing = screenshots()
        check_focus(before)
        im, path = steam_frame(before, lambda: screenshots() - existing)
        info = {'kind': 'live', 'steam_path': str(path)}
    else:
        raise ScanError(f'Unknown capture backend: {backend}')
    check_focus(before)
    return im, {**info, 'backend': backend}


def capture_issue(image, backend):
    """Reject blank frames and a narrow false-color Steam artifact, not unknown rows."""
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
        if saturated.mean() > .30 and magenta.mean() > .06 and green.mean() > .06 and pale < .025:
            return 'Steam capture shows extreme false-color artifacts.'
    return None


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError()


def scan_live(backend, recognize, save_attempt=None, prepare=lambda image: image,
              cancel_event=None):
    _check_cancel(cancel_event)
    platform = session_kind(os.environ)
    if backend == 'x11' and platform != 'x11':
        raise ScanError('X11 capture requires a native X11 session.')
    if backend == 'portal' or (backend == 'auto' and platform == 'wayland'):
        before = None
        names = ('portal',)
    elif (backend == 'x11' or
          (platform == 'x11' and backend in ('auto', 'gamescope', 'steam'))):
        before = query_x11_window()
        validate_game_window(before)
        names = ('gamescope', 'steam', 'x11') if backend == 'auto' else (backend,)
    else:
        if backend == 'auto' and platform not in ('hyprland',):
            raise ScanError('Cannot choose a capture backend for this desktop session.')
        if (backend in ('gamescope', 'steam', 'desktop') and
                platform == 'wayland'):
            raise ScanError(f'{backend} capture is unsupported on this Wayland desktop.')
        before = query_active_window()
        window_geometry(before)
        names = ('gamescope', 'steam', 'desktop') if backend == 'auto' else (backend,)
    attempts = []
    for name in names:
        _check_cancel(cancel_event)
        check_focus(before)
        try:
            im, info = capture(name, before, cancel_event=cancel_event)
            im = prepare(im)
            issue = capture_issue(im, name)
            if issue:
                if save_attempt:
                    save_attempt(name, im, {'status': 'capture_rejected', 'rows': [], 'reason': issue})
                raise ScanError(issue)
        except FocusChanged:
            raise
        except (ScanError, OSError, ValueError) as error:
            attempts.append({'backend': name, 'status': 'error', 'reason': str(error)})
            _check_cancel(cancel_event)
            check_focus(before)
            continue
        # Recognition uncertainty is not a capture failure. Keep the first usable frame.
        _check_cancel(cancel_event)
        report = recognize(im)
        _check_cancel(cancel_event)
        attempts.append({'backend': name, 'status': report['status'],
                         'matched': sum(bool(r['id']) for r in report['rows']),
                         'icon_matches': sum(bool(r['id']) and r.get('method') not in
                                            ('mission-name', 'mission-arrows') for r in report['rows'])})
        if save_attempt:
            save_attempt(name, im, report)
        check_focus(before)
        info['attempts'] = attempts
        report['warnings'].extend(f"Capture {a['backend']}: {a['reason']}" for a in attempts[:-1])
        return im, info, report
    raise ScanError('No usable capture. ' + '; '.join(
        f"{a['backend']}: {a.get('reason', a['status'])}" for a in attempts))
