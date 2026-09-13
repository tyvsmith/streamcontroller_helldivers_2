"""Capture through Gamescope or a configured screenshot and recognize stratagems."""

import argparse
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

import cv2
from PIL import Image, ImageDraw

from automatic_stratagems.host_commands import HostCommandError

from .errors import ScanError
from .image_decode import decode_image
from .limits import MAX_ENCODED_IMAGE_BYTES
from .capture_backends import BACKENDS, scan_live
from .icon_normalization import normalize_icon
from .layout.geometry import SelectionGeometry, normalized_band
from .recognize.constants import TILE_PX
from .selection_layout import empty_tile, find_selection_band, selection_boxes
from .stratagem_detection import catalog, detect_icons, detect_mission_icons
from .recognition_cache import scanner_cache


HOST_PREFLIGHT = r'''
job=$1
required=$2
first=$3
second=$4
if [ ! -d "$job" ] || [ ! -w "$job" ]; then
  echo "Host command job is not shared with the sandbox" >&2
  exit 1
fi
for leaf in /usr/bin/grep /usr/bin/mv /usr/bin/rm /usr/bin/sh /usr/bin/sleep /usr/bin/sync; do
  if [ ! -x "$leaf" ]; then
    echo "Missing host transport helper: $leaf" >&2
    exit 1
  fi
done
if [ ! -r /proc/uptime ] || [ ! -r /proc/net/unix ] || \
   [ ! -r /proc/$$/stat ] || [ ! -r /proc/$$/cmdline ] || \
   [ ! -d /proc/$$/fd ]; then
  echo "Host process metadata is unavailable" >&2
  exit 1
fi
available_group() {
  for helper in $1; do
    if ! command -v "$helper" >/dev/null 2>&1; then return 1; fi
  done
}
for helper in $required; do
  if ! command -v "$helper" >/dev/null 2>&1; then
    echo "Missing capture helper: $helper" >&2
    exit 1
  fi
done
if [ -n "$first" ] && ! available_group "$first" && ! available_group "$second"; then
  echo "Missing capture fallback: $first or $second" >&2
  exit 1
fi
'''


def check_capture_setup(backend, cancel_event=None):
    """Check static Gamescope prerequisites without capture or input."""
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError()
    if backend == 'auto':
        return
    if backend != 'gamescope':
        raise ScanError('Choose Gamescope or configure a Screenshot source.')
    flatpak = bool(os.environ.get('FLATPAK_ID') or Path('/.flatpak-info').exists())
    required = '/usr/bin/python3'
    if not flatpak:
        for helper in ('/usr/bin/python3', '/usr/bin/setsid'):
            if shutil.which(helper) is None:
                raise ScanError(f'Missing capture helper: {helper}')
        if (shutil.which('gamescopectl') is None and
                shutil.which('/usr/bin/flatpak') is None):
            raise ScanError('Missing capture helper: gamescopectl or flatpak')
        return
    from automatic_stratagems.host_commands import JOB_DIRECTORY_ENV
    shared = os.environ.get(JOB_DIRECTORY_ENV, '')
    if not shared:
        raise ScanError('Host command job environment is unavailable.')
    from .capture.command import run_command
    run_command(['/usr/bin/sh', '-c', HOST_PREFLIGHT, 'hd2-capture-preflight',
                 shared, required, 'gamescopectl', '/usr/bin/flatpak'],
                timeout=5, host=True,
                operation='capture-preflight', cancel_event=cancel_event,
                stdout_limit=1024, stderr_limit=16 * 1024)


def rectangle(value):
    try:
        parts = [float(n) for n in value.split(",")]
        if len(parts) != 4 or not all(math.isfinite(n) for n in parts):
            raise ValueError()
        x, y, w, h = parts
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > 1 or y + h > 1:
            raise ValueError()
        return parts
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use normalized x,y,width,height within 0..1") from error


def _check_work_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise ScanError('Scanner work deadline exhausted.')


def pixel_rectangle(im, normalized):
    return [round(n * size) for n, size in zip(normalized, (im.width, im.height, im.width, im.height))]


def load_replay_image(path):
    try:
        if path.stat().st_size > MAX_ENCODED_IMAGE_BYTES:
            raise ScanError('Replay encoded image is too large.')
        return decode_image(path.read_bytes(), 'Replay')
    except OSError as error:
        raise ScanError(f'Cannot read replay image: {error}') from error


SELECTION_WARNING = "Selection layout uses calibrated Ready-bar ratios; four-player live coverage is unverified."
MISSION_WARNING = "Mission detection uses icons first, then names and complete arrow sequences for unknown rows; scrambled or obscured evidence can remain unknown."


@dataclass(frozen=True)
class ScanMode:
    """How one mode reads a capture: locate its layout, prepare the image, run stages in order."""

    locate: object  # (im, band) -> located layout
    prepare: object  # (im, located) -> image the stages read
    stages: tuple  # each (image, located, rows, **context) -> rows
    layout: object  # (im, located) -> report layout
    warning: str


def resolve_auto_mode(im):
    """Choose selection only when one Ready bar shows at least two occupied top tiles."""
    try:
        band = find_selection_band(im)
        top = selection_boxes(im, band)[:7]
        if sum(not empty_tile(im, box, frame_occupancy=True) for box in top) < 2:
            band = None
    except ScanError:
        band = None
    return ("selection", band) if band is not None else ("mission", None)


def _locate_selection(im, band):
    band = band or find_selection_band(im)
    boxes = selection_boxes(im, band)
    empty = [box for i, box in enumerate(boxes) if empty_tile(im, box, frame_occupancy=i < 7)]
    # Report boxes stay in source pixels; the matcher origin spans every tile, empty or not.
    return {"band": band, "empty": empty, "boxes": [box for box in boxes if box not in empty],
            "geometry": SelectionGeometry.for_band(band, boxes)}


def _match_selection_tiles(image, located, rows, *, entries, executor, cache, **_):
    boxes = located["boxes"]
    rows = detect_icons(image, entries, located["geometry"].to_local(boxes),
                        executor=executor, cache=cache)
    for row, box in zip(rows, boxes):
        row["box"] = box
    return rows


def _match_mission_icons(image, located, rows, *, entries, executor, cache, **_):
    return detect_mission_icons(image, entries, executor=executor, cache=cache)


def _apply_mission_fallbacks(image, located, rows, *, entries, deadline, cancel_event, **_):
    from .mission_fallbacks import apply_mission_fallbacks
    return apply_mission_fallbacks(image, rows, entries, deadline=deadline,
                                   cancel_event=cancel_event)


SELECTION = ScanMode(
    locate=_locate_selection,
    # Normalize the selected area to the calibrated tile size for matching.
    prepare=lambda im, located: located["geometry"].crop(im),
    stages=(_match_selection_tiles,),
    layout=lambda im, located: {"ready_bar": located["band"],
                                "normalized_ready_bar": normalized_band(im, located["band"]),
                                "empty_tiles": located["empty"], "calibrated": True},
    warning=SELECTION_WARNING)
MISSION = ScanMode(
    locate=lambda im, band: None,
    prepare=lambda im, located: im,
    stages=(_match_mission_icons, _apply_mission_fallbacks),
    layout=lambda im, located: {"icon_region": [0, 0, .15, .53], "calibrated": True},
    warning=MISSION_WARNING)


def detect(im, mode, band=None, *, executor=None, cache=None, deadline=None,
           cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError()
    _check_work_deadline(deadline)
    entries = catalog()
    if mode == "auto":
        mode, found = resolve_auto_mode(im)
        if found is not None:
            band = found
    scan = SELECTION if mode == "selection" else MISSION
    located = scan.locate(im, band)
    image = scan.prepare(im, located)
    rows = None
    for stage in scan.stages:
        rows = stage(image, located, rows, entries=entries, executor=executor, cache=cache,
                     deadline=deadline, cancel_event=cancel_event)
    layout = scan.layout(im, located)
    warnings = [scan.warning]
    _check_work_deadline(deadline)
    for row in rows:
        key = row["id"]
        row["name"] = entries[key]["name"] if key else row.get("text", "Unrecognized tile (possibly empty)")
        row["status"] = "matched" if key else "unknown"
        row["sequence"] = entries[key]["sequence"] if key else None
    status = "no_detections" if not rows else "partial" if any(r["id"] is None for r in rows) else "matched"
    return {"mode": mode, "status": status, "rows": rows, "layout": layout, "warnings": warnings}


def print_findings(report):
    print(f"{report['mode']} | {report['size'][0]}x{report['size'][1]} | "
          f"{report['status']} | {report['seconds']:.2f}s")
    if report.get("source", {}).get("backend"):
        print(f"  Capture: {report['source']['backend']}")
    for row in report["rows"]:
        if row["id"]:
            arrows = " ".join({"UP": "↑", "DOWN": "↓", "LEFT": "←", "RIGHT": "→"}[k]
                              for k in row["sequence"])
            via = {'mission-name': ' (via name)', 'mission-arrows': ' (via arrows)'}.get(row.get('method'), '')
            print(f"  MATCH    {row['name']} [{row['id']}]  {arrows}{via}")
        else:
            print(f"  UNKNOWN  {row['name']} (no sequence assigned)")
    if not report["rows"]:
        print("  No stratagems recognized. Open the expanded menu, or try --mode selection.")
    for warning in report["warnings"]:
        print(f"  Note: {warning}")


def annotation_box(im, report):
    layout = report.get('layout', {})
    boxes = [row['box'] for row in report.get('rows', [])]
    boxes.extend(layout.get('empty_tiles', []))
    if layout.get('ready_bar'):
        boxes.append(layout['ready_bar'])
    if report.get('mode') == 'mission':
        boxes.append(pixel_rectangle(im, layout.get('icon_region', [0, 0, .15, .53])))
        # Text width scales with HUD icons, not with the display's aspect ratio.
        boxes.extend([x, y, round(w * 7.4), h] for x, y, w, h in
                     (row['box'] for row in report.get('rows', [])))
    if not boxes:
        return None
    left = max(0, min(x for x, y, w, h in boxes) - 32)
    top = max(0, min(y for x, y, w, h in boxes) - 32)
    right = min(im.width, max(x + w for x, y, w, h in boxes) + 32)
    bottom = min(im.height, max(y + h for x, y, w, h in boxes) + 32)
    return [left, top, right - left, bottom - top]


def write_debug(directory, im, report):
    im.save(directory / "capture.png", compress_level=1)
    box = annotation_box(im, report)
    report['debug_annotation_box'] = box
    if box is not None:
        left, top, width, height = box
        annotated = im.crop((left, top, left + width, top + height))
        draw = ImageDraw.Draw(annotated)
        for i, row in enumerate(report.get("rows", []), 1):
            x, y, w, h = row["box"]
            tile = im.crop((x, y, x + w, y + h))
            if report.get('mode') == 'mission':
                tile = tile.resize((TILE_PX, TILE_PX))
            normalize_icon(tile, mission=report.get('mode') == 'mission').save(
                directory / f'normalized-{i:02d}.png', compress_level=1)
            color = "lime" if row["id"] else "orange"
            draw.rectangle((x-left, y-top, x+w-left, y+h-top), outline=color,
                           width=max(2, im.width // 1000))
            draw.text((x-left, max(0, y-top-30)), str(i), fill=color, font_size=24,
                      stroke_width=2, stroke_fill="black")
        annotated.save(directory / "annotated.png", compress_level=1)
    (directory / "findings.json").write_text(json.dumps(report, indent=2) + "\n")


def _reject_debug_symlinks(path):
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ScanError(f'Debug path contains a symlink: {current}')
    return path


def prepare_debug_directory(path):
    path = _reject_debug_symlinks(path)
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(path, 0o700)
        return path
    except FileExistsError:
        if path.is_symlink() or not path.is_dir():
            raise ScanError(f"Debug path is not a directory: {path}")
        marker = path / '.hd2-scan-run.json'
        try:
            metadata = marker.lstat()
            safe = stat.S_ISREG(metadata.st_mode) and metadata.st_size <= 4096
            value = json.loads(marker.read_text()) if safe else None
            owned = (safe and
                     isinstance(value, dict) and
                     value.get('owner') == 'net_jslay_helldivers_2' and
                     value.get('status') == 'open' and value.get('version') == 1)
        except (OSError, ValueError, UnicodeError):
            owned = False
        if owned:
            return path
        return Path(tempfile.mkdtemp(prefix="scan-", dir=path))


def reuse_debug_images(source, destination):
    """Preserve both debug layouts without encoding the selected images again."""
    for image in source.glob('*.png'):
        target = destination / image.name
        try:
            os.link(image, target)
        except OSError:
            shutil.copyfile(image, target)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-backend", choices=BACKENDS, default="auto")
    parser.add_argument('--workers', type=int, default=2, help='Parallel icon workers, 1–32 (default: 2; 1 disables parallel matching)')
    parser.add_argument('--budget-seconds', type=float, default=110,
                        help=argparse.SUPPRESS)
    parser.add_argument('--cache-dir', type=Path, help='Persistent icon cache directory (default: XDG cache directory)')
    parser.add_argument('--no-cache', action='store_true', help='Disable persistent icon-result caching')
    parser.add_argument("--image", type=Path, help="Replay an image instead of capturing")
    parser.add_argument('--image-source-kind', choices=['file', 'folder', 'path'],
                        help='Read a complete image from a file or the newest in a folder')
    parser.add_argument('--image-source-path')
    parser.add_argument('--previous-image-fingerprint', help=argparse.SUPPRESS)
    parser.add_argument('--allow-image-rescan', action='store_true',
                        help='Explicitly allow an unchanged image source')
    parser.add_argument('--screenshot-trigger', choices=['none', 'hotkey', 'script'])
    parser.add_argument('--screenshot-hotkey', default='KEY_F12')
    parser.add_argument('--screenshot-script', default='')
    parser.add_argument('--delete-screenshot', action='store_true')
    parser.add_argument("--mode", choices=["auto", "mission", "selection"], default="auto")
    parser.add_argument("--viewport", type=rectangle, help="Normalized game viewport x,y,w,h; trims letterboxing")
    parser.add_argument("--selection-band", type=rectangle, help="Normalized left player's complete Ready bar x,y,w,h within viewport")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable findings")
    parser.add_argument("--check-setup", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug-dir", type=Path, help="Save capture/findings; create a new child run if directory exists")
    args = parser.parse_args(argv)
    if bool(args.image_source_kind) != (args.image_source_path is not None):
        parser.error('--image-source-kind and --image-source-path are required together')
    if args.image_source_kind and args.image:
        parser.error('--image and --image-source-kind cannot be combined')
    if (args.capture_backend == 'gamescope' and not args.image_source_kind
            and (args.screenshot_trigger or args.delete_screenshot or args.screenshot_script)):
        parser.error('Screenshot trigger options require Screenshot capture')
    if args.image and (args.screenshot_trigger or args.delete_screenshot or args.screenshot_script):
        parser.error('Screenshot trigger options cannot be used with --image')
    if not args.image_source_kind and (args.previous_image_fingerprint or args.allow_image_rescan):
        parser.error('image rescan options require --image-source-kind')
    if not 1 <= args.workers <= 32:
        parser.error('--workers must be between 1 and 32')
    if not 1 <= args.budget_seconds <= 115:
        parser.error('--budget-seconds must be between 1 and 115')
    # Bound native parallelism too; otherwise each row worker can create more threads.
    cv2.setNumThreads(1)
    if args.selection_band and args.mode == "mission":
        parser.error("--selection-band cannot be used with --mode mission")
    start = time.monotonic()
    im = None
    debug = None
    cancel_event = threading.Event()
    previous_signals = {}
    try:
        def cancel_scan(_signum, _frame):
            cancel_event.set()
            raise CancelledError()
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_signals[signum] = signal.signal(signum, cancel_scan)
    except ValueError:
        pass
    try:
        screenshot = None
        if (args.capture_backend in ('auto', 'screenshot') and not args.image):
            screenshot = {
                'kind': args.image_source_kind or 'path',
                'path': str(args.image_source_path) if args.image_source_path is not None else '',
                'trigger': args.screenshot_trigger or ('none' if args.image_source_kind else 'hotkey'),
                'hotkey': args.screenshot_hotkey, 'script': args.screenshot_script,
                'delete_after_scan': args.delete_screenshot,
                'previous_fingerprint': args.previous_image_fingerprint,
                'allow_rescan': args.allow_image_rescan,
            }
        if args.check_setup:
            if args.capture_backend == 'screenshot':
                from .screenshot_capture import check_screenshot_setup
                check_screenshot_setup(screenshot, cancel_event=cancel_event,
                                       deadline=start + args.budget_seconds)
            else:
                check_capture_setup(args.capture_backend, cancel_event=cancel_event)
            return 0
        cache = None if args.no_cache else scanner_cache(args.cache_dir)
        if args.debug_dir:
            debug = prepare_debug_directory(args.debug_dir)
        def prepare(image):
            if args.viewport:
                x, y, w, h = pixel_rectangle(image, args.viewport)
                image = image.crop((x, y, x + w, y + h))
            if min(image.size) < 100:
                raise ScanError("Image is too small for detection.")
            return image

        def recognize(image):
            band = pixel_rectangle(image, args.selection_band) if args.selection_band else None
            pool = ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix='hd2-icon') if args.workers > 1 else nullcontext(None)
            with pool as executor:
                return detect(image, "selection" if band else args.mode, band,
                              executor=executor, cache=cache,
                              deadline=start + args.budget_seconds,
                              cancel_event=cancel_event)

        def save_attempt(name, image, findings):
            if debug:
                directory = debug / name
                directory.mkdir()
                write_debug(directory, image, findings)

        if args.image:
            im = prepare(load_replay_image(args.image))
            source_info = {"kind": "file", "path": str(args.image.resolve())}
            report = recognize(im)
        else:
            im, source_info, report = scan_live(
                args.capture_backend, recognize, save_attempt, prepare,
                cancel_event=cancel_event,
                deadline=start + args.budget_seconds, screenshot=screenshot)
        if args.viewport:
            source_info["normalized_viewport"] = args.viewport
        captured_at = datetime.now(timezone.utc).isoformat()
        report.update(source=source_info, size=im.size, captured_at=captured_at,
                      schema_version=1,
                      workers=args.workers, cache=cache.info() if cache is not None else {"available": False})
        if debug:
            report["debug_directory"] = str(debug.resolve())
        if debug:
            if args.image:
                write_debug(debug, im, report)
            else:
                reuse_debug_images(debug / source_info['backend'], debug)
        if (screenshot is not None and
                source_info.get('backend') == 'screenshot' and
                screenshot['delete_after_scan']
                and report['status'] in ('matched', 'partial')
                and any(row.get('id') for row in report['rows'])):
            from .screenshot_capture import cleanup_screenshot
            _check_work_deadline(start + args.budget_seconds)
            if cancel_event.is_set():
                raise CancelledError()
            if not cleanup_screenshot(screenshot, source_info, cancel_event=cancel_event):
                report['warnings'].append('Screenshot retained: safe deletion could not be confirmed.')
        report['seconds'] = round(time.monotonic() - start, 3)
        if debug:
            (debug / 'findings.json').write_text(json.dumps(report, indent=2) + '\n')
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_findings(report)
            if debug:
                print(f"  Debug files: {debug.resolve()}")
        return {"matched": 0, "partial": 3, "no_detections": 4}[report["status"]]
    except (CancelledError, ScanError, HostCommandError, OSError, ValueError,
            subprocess.SubprocessError, cv2.error) as error:
        detail = str(error)
        report = {"schema_version": 1, "status": "error", "error": detail,
                  "rows": [], "warnings": []}
        if debug:
            try:
                (debug / "findings.json").write_text(json.dumps(report, indent=2) + "\n")
                if im is not None:
                    im.save(debug / "capture.png")
            except OSError:
                pass
        if args.json:
            print(json.dumps(report))
        else:
            print(f"Scan failed: {detail}", file=sys.stderr)
        return 1
    finally:
        for signum, previous in previous_signals.items():
            signal.signal(signum, previous)


if __name__ == "__main__":
    sys.exit(main())
