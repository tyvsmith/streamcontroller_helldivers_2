"""Prepare, validate, and evaluate private full-scene screenshot manifests."""

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import time

from PIL import Image

from .scan_runner import SCAN_TIMEOUT_SECONDS, _run_owned, validate_report
from .shared.bounded_json import read_bounded_json


ROOT = Path(__file__).resolve().parent.parent
BUILTIN_MANIFEST = ROOT / 'automatic_stratagems' / 'tests' / 'fixtures' / 'manifest.json'
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CASES = 256
MAX_DISCOVERY_ENTRIES = 4096
MAX_DISCOVERY_DEPTH = 8
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 40_000_000
SPLITS = ('calibration', 'heldout')
MODES = ('mission', 'selection')
IMAGE_SUFFIXES = ('.jpg', '.jpeg', '.png')
PROVENANCE_FIELDS = (
    'capture_id', 'source', 'captured_at', 'backend', 'platform', 'desktop',
    'resolution', 'hdr', 'language', 'game_build', 'player_count', 'hud_scale',
    'safe_area',
)
TEXT_PROVENANCE_FIELDS = (
    'capture_id', 'source', 'captured_at', 'backend', 'platform', 'desktop',
    'hdr', 'language', 'game_build', 'hud_scale', 'safe_area',
)
UNKNOWN = 'unknown'


@dataclass(frozen=True)
class ValidatedCase:
    relative_path: str
    path: Path
    sha256: str
    dimensions: tuple[int, int]
    split: str
    capture_id: str
    expected_mode: str
    expected_ids: list[str | None]


def _relative_image_path(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('Fixture path must be a relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in path.parts):
        raise ValueError('Fixture path must be a relative path without parent escape')
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError('Fixture path must name a PNG or JPEG image')
    return path


def _reject_symlink_components(root, relative):
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise ValueError(f'Fixture path contains a symlink: {relative}')
        except FileNotFoundError as error:
            raise ValueError(f'Fixture image is missing: {relative}') from error


def _inspect_image(path):
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f'Cannot open fixture image: {path.name}') from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f'Fixture image is not a regular file: {path.name}')
        if metadata.st_size > MAX_IMAGE_BYTES:
            raise ValueError(f'Fixture image exceeds the byte limit: {path.name}')
        chunks = []
        remaining = MAX_IMAGE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b''.join(chunks)
    finally:
        os.close(descriptor)
    if len(encoded) > MAX_IMAGE_BYTES:
        raise ValueError(f'Fixture image exceeds the byte limit: {path.name}')
    try:
        with Image.open(io.BytesIO(encoded)) as image:
            dimensions = image.size
            if image.format not in ('JPEG', 'PNG'):
                raise ValueError('unsupported image format')
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ValueError(f'Fixture image is unreadable: {path.name}') from error
    width, height = dimensions
    if (min(width, height) <= 0 or max(width, height) > MAX_IMAGE_DIMENSION or
            width * height > MAX_IMAGE_PIXELS):
        raise ValueError(f'Fixture image dimensions are unsupported: {path.name}')
    return hashlib.sha256(encoded).hexdigest(), dimensions


def _bounded_file_sha256(path, max_bytes):
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('Manifest path must be a regular file')
        if metadata.st_size > max_bytes:
            raise ValueError('Manifest exceeds the byte limit')
        digest = hashlib.sha256()
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            raise ValueError('Manifest exceeds the byte limit')
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _write_json_exclusive(path, value):
    path = Path(path)
    if os.path.lexists(path):
        raise ValueError(f'Output already exists: {path}')
    if not path.parent.is_dir():
        raise ValueError(f'Output directory does not exist: {path.parent}')
    payload = (json.dumps(value, indent=2) + '\n').encode('utf-8')
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ValueError(f'Output already exists: {path}') from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _unknown_provenance():
    value = {field: UNKNOWN for field in PROVENANCE_FIELDS}
    value.update(rights=UNKNOWN, privacy_reviewed=False)
    return value


def _discover_images(root):
    images = []
    stack = [(root, 0)]
    entries_seen = 0
    while stack:
        directory, depth = stack.pop()
        try:
            entries = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries_seen += 1
                    if entries_seen > MAX_DISCOVERY_ENTRIES:
                        raise ValueError('Image discovery exceeds the entry limit')
                    entries.append(entry)
        except OSError as error:
            raise ValueError(f'Cannot inspect image directory: {directory}') from error
        for entry in sorted(entries, key=lambda item: item.name, reverse=True):
            path = Path(entry.path)
            if entry.is_symlink():
                raise ValueError(f'Image root contains a symlink: {path.name}')
            if entry.is_dir(follow_symlinks=False):
                if depth >= MAX_DISCOVERY_DEPTH:
                    raise ValueError('Image discovery exceeds the depth limit')
                stack.append((path, depth + 1))
            elif entry.is_file(follow_symlinks=False) and path.suffix.lower() in IMAGE_SUFFIXES:
                images.append(path)
                if len(images) > MAX_CASES:
                    raise ValueError(
                        f'Manifest cannot contain more than {MAX_CASES} cases')
    return sorted(images)


def prepare_manifest(image_root, output):
    image_root = Path(image_root).absolute()
    if image_root.is_symlink():
        raise ValueError('Image root must be a non-symlink directory')
    if not image_root.is_dir():
        raise ValueError('Image root must be a non-symlink directory')
    image_root = image_root.resolve()
    output = Path(output).absolute()
    output_parent = output.parent.resolve(strict=False)
    try:
        image_root.relative_to(output_parent)
    except ValueError as error:
        raise ValueError('Image root must be inside the manifest directory') from error
    images = _discover_images(image_root)
    if not images:
        raise ValueError('Image root contains no PNG or JPEG files')
    cases = []
    for path in images:
        relative = path.relative_to(output_parent)
        _reject_symlink_components(output_parent, PurePosixPath(relative.as_posix()))
        digest, dimensions = _inspect_image(path)
        cases.append({
            'path': relative.as_posix(),
            'sha256': digest,
            'dimensions': list(dimensions),
            'split': None,
            'provenance': _unknown_provenance(),
            'expected': {'mode': None, 'ids': None},
        })
    manifest = {'schema_version': 1, 'cases': cases}
    _write_json_exclusive(output, manifest)
    return manifest


def _validate_dimensions(value, label):
    if (not isinstance(value, list) or len(value) != 2 or
            any(type(number) is not int or number <= 0 for number in value)):
        raise ValueError(f'{label} dimensions must contain two positive integers')
    width, height = value
    if max(value) > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
        raise ValueError(f'{label} dimensions are unsupported')
    return tuple(value)


def _validate_provenance(value, split, dimensions):
    if not isinstance(value, dict):
        raise ValueError('Fixture provenance must be an object')
    missing = [field for field in PROVENANCE_FIELDS if field not in value]
    if missing:
        raise ValueError(f'Fixture provenance is missing {missing[0]}')
    capture_id = value['capture_id']
    if (not isinstance(capture_id, str) or not capture_id or
            capture_id != capture_id.strip()):
        raise ValueError('Fixture provenance capture_id must be text')
    for field in TEXT_PROVENANCE_FIELDS:
        item = value[field]
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f'Fixture provenance {field} must be text')
        if split == 'heldout' and item == UNKNOWN:
            raise ValueError(f'Heldout provenance requires concrete {field}')
    resolution = value['resolution']
    if resolution == UNKNOWN:
        if split == 'heldout':
            raise ValueError('Heldout provenance requires concrete resolution')
    elif _validate_dimensions(resolution, 'Provenance') != dimensions:
        raise ValueError('Fixture provenance resolution must match image dimensions')
    player_count = value['player_count']
    if player_count == UNKNOWN:
        if split == 'heldout':
            raise ValueError('Heldout provenance requires concrete player_count')
    elif type(player_count) is not int or not 1 <= player_count <= 4:
        raise ValueError('Fixture provenance player_count must be between 1 and 4')
    if value['captured_at'] != UNKNOWN:
        try:
            captured_at = datetime.fromisoformat(value['captured_at'])
        except (TypeError, ValueError) as error:
            raise ValueError('Fixture provenance requires a valid captured_at') from error
        if captured_at.tzinfo is None:
            raise ValueError('Fixture provenance captured_at must include a timezone')
    rights = value.get('rights')
    if rights is not None and (not isinstance(rights, str) or not rights):
        raise ValueError('Fixture provenance rights must be text when present')
    privacy = value.get('privacy_reviewed')
    if privacy is not None and type(privacy) is not bool:
        raise ValueError('Fixture provenance privacy_reviewed must be boolean')
    return capture_id


def _catalog_ids():
    from .scanner.stratagem_detection import catalog
    return set(catalog())


def _builtin_calibration(path):
    if path is None:
        return {}, {}
    value = read_bounded_json(path, max_bytes=MAX_MANIFEST_BYTES)
    inventory = value.get('inventory') if isinstance(value, dict) else None
    if not isinstance(inventory, dict):
        raise ValueError('Built-in fixture manifest has no integrity inventory')
    hashes = {}
    capture_splits = {}
    for relative, item in inventory.items():
        if (not isinstance(item, dict) or
                not isinstance(item.get('sha256'), str) or
                re.fullmatch(r'[0-9a-f]{64}', item['sha256']) is None or
                item.get('split') != 'calibration' or
                not isinstance(item.get('capture_id'), str)):
            raise ValueError(f'Built-in fixture inventory is invalid: {relative}')
        hashes.setdefault(item['sha256'], set()).add('calibration')
        capture_id = item['capture_id']
        capture_splits.setdefault(capture_id, set()).add('calibration')
    return hashes, capture_splits


def validate_manifest(manifest_path, *, builtin_manifest=BUILTIN_MANIFEST):
    manifest_path = Path(manifest_path).absolute()
    value = read_bounded_json(manifest_path, max_bytes=MAX_MANIFEST_BYTES)
    if not isinstance(value, dict) or set(value) != {'schema_version', 'cases'}:
        raise ValueError('Fixture manifest must contain schema_version and cases')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('Unsupported fixture manifest schema version')
    cases = value['cases']
    if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
        raise ValueError(f'Fixture manifest must contain 1 to {MAX_CASES} cases')
    root = manifest_path.parent.resolve()
    catalog_ids = _catalog_ids()
    validated = []
    paths = set()
    hashes, capture_splits = _builtin_calibration(builtin_manifest)
    for index, case in enumerate(cases):
        label = f'Fixture case {index + 1}'
        if not isinstance(case, dict) or set(case) != {
                'path', 'sha256', 'dimensions', 'split', 'provenance', 'expected'}:
            raise ValueError(f'{label} has invalid fields')
        relative = _relative_image_path(case['path'])
        relative_text = relative.as_posix()
        if relative_text in paths:
            raise ValueError(f'Duplicate fixture path: {relative_text}')
        paths.add(relative_text)
        _reject_symlink_components(root, relative)
        path = root.joinpath(*relative.parts)
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as error:
            raise ValueError(
                f'Fixture path escapes the manifest directory: {relative_text}') from error
        digest = case['sha256']
        if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
            raise ValueError(f'{label} has an invalid SHA-256')
        actual_digest, actual_dimensions = _inspect_image(path)
        if digest != actual_digest:
            raise ValueError(f'{label} SHA-256 does not match the image')
        dimensions = _validate_dimensions(case['dimensions'], label)
        if dimensions != actual_dimensions:
            raise ValueError(f'{label} dimensions do not match the image')
        split = case['split']
        if split not in SPLITS:
            raise ValueError(f'{label} split must be calibration or heldout')
        capture_id = _validate_provenance(case['provenance'], split, dimensions)
        expected = case['expected']
        if (not isinstance(expected, dict) or set(expected) != {'mode', 'ids'} or
                expected['mode'] not in MODES):
            raise ValueError(f'{label} labels require mission or selection mode')
        identifiers = expected['ids']
        if (not isinstance(identifiers, list) or len(identifiers) > 32 or
                any(identifier is not None and not isinstance(identifier, str)
                    for identifier in identifiers)):
            raise ValueError(f'{label} labels require an ordered ID/null list')
        unknown = sorted({identifier for identifier in identifiers
                          if identifier is not None and identifier not in catalog_ids})
        if unknown:
            raise ValueError(f'{label} ID is not in the scanner catalog: {unknown[0]}')
        hashes.setdefault(digest, set()).add(split)
        capture_splits.setdefault(capture_id, set()).add(split)
        validated.append(ValidatedCase(
            relative_text, path, digest, dimensions, split, capture_id,
            expected['mode'], list(identifiers)))
    if any(len(splits) > 1 for splits in hashes.values()):
        raise ValueError('Identical fixture content appears across calibration and heldout splits')
    for capture_id, splits in capture_splits.items():
        if capture_id != UNKNOWN and len(splits) > 1:
            raise ValueError(
                f'Known capture_id appears across calibration and heldout splits: {capture_id}')
    return validated


def _expected_status(identifiers):
    if not identifiers:
        return 'no_detections'
    return 'partial' if any(identifier is None for identifier in identifiers) else 'matched'


def _scanner_command(case, workers, interpreter):
    return [str(interpreter), '-m', 'automatic_stratagems.scanner',
            '--image', str(case.path), '--mode', 'auto', '--no-cache',
            '--workers', str(workers), '--json']


def evaluate_manifest(manifest_path, output, *, split=None, workers=1,
                      builtin_manifest=BUILTIN_MANIFEST, interpreter=sys.executable):
    if split is not None and split not in SPLITS:
        raise ValueError('Evaluation split must be calibration or heldout')
    if type(workers) is not int or not 1 <= workers <= 32:
        raise ValueError('Workers must be between 1 and 32')
    output = Path(output).absolute()
    if os.path.lexists(output):
        raise ValueError(f'Output already exists: {output}')
    if not output.parent.is_dir():
        raise ValueError(f'Output directory does not exist: {output.parent}')
    manifest_path = Path(manifest_path).absolute()
    manifest_digest = _bounded_file_sha256(manifest_path, MAX_MANIFEST_BYTES)
    cases = validate_manifest(manifest_path, builtin_manifest=builtin_manifest)
    if _bounded_file_sha256(manifest_path, MAX_MANIFEST_BYTES) != manifest_digest:
        raise ValueError('Fixture manifest changed during validation')
    available = {case.split for case in cases}
    if split is None:
        if len(available) != 1:
            raise ValueError('Mixed manifests require --split calibration or heldout')
        split = next(iter(available))
    selected = [case for case in cases if case.split == split]
    if not selected:
        raise ValueError(f'Manifest has no {split} cases')
    if output == manifest_path or output in {case.path for case in cases}:
        raise ValueError('Evaluation output cannot replace a manifest or source image')
    results = []
    for case in selected:
        started = time.monotonic()
        actual_mode = None
        actual_ids = None
        actual_status = None
        error = None
        try:
            code, stdout, _ = _run_owned(
                _scanner_command(case, workers, interpreter), cwd=ROOT,
                timeout=SCAN_TIMEOUT_SECONDS)
            report = json.loads(stdout.decode('utf-8'))
            validate_report(report)
            expected_exit_status = {
                0: 'matched', 3: 'partial', 4: 'no_detections',
            }.get(code)
            if report['status'] == 'error':
                raise ValueError(report.get('error') or f'Scanner exited with status {code}')
            if expected_exit_status != report['status']:
                raise ValueError(
                    f'Scanner exit status {code} conflicts with {report["status"]}')
            actual_mode = report.get('mode')
            if actual_mode not in MODES:
                raise ValueError('Scanner returned an invalid detected mode')
            actual_status = report['status']
            actual_ids = [row.get('id') for row in report['rows']]
        except Exception as caught:
            error = f'{type(caught).__name__}: {caught}'
        try:
            after_digest, _ = _inspect_image(case.path)
            if after_digest != case.sha256:
                error = 'Fixture image changed during evaluation'
        except ValueError as caught:
            error = f'Fixture image changed during evaluation: {caught}'
        expected_status = _expected_status(case.expected_ids)
        passed = (error is None and actual_mode == case.expected_mode and
                  actual_status == expected_status and actual_ids == case.expected_ids)
        results.append({
            'path': case.relative_path,
            'sha256': case.sha256,
            'split': case.split,
            'expected_mode': case.expected_mode,
            'actual_mode': actual_mode,
            'expected_status': expected_status,
            'actual_status': actual_status,
            'expected_ids': case.expected_ids,
            'actual_ids': actual_ids,
            'status': 'passed' if passed else 'failed',
            'error': error,
            'seconds': round(time.monotonic() - started, 4),
        })
    failures = sum(result['status'] == 'failed' for result in results)
    if _bounded_file_sha256(manifest_path, MAX_MANIFEST_BYTES) != manifest_digest:
        raise ValueError('Fixture manifest changed during evaluation')
    artifact = {
        'schema_version': 1,
        'manifest_sha256': manifest_digest,
        'split': split,
        'evidence': 'calibration regression' if split == 'calibration' else 'heldout evaluation',
        'independence_limit': (
            'Hash and capture-ID checks detect known overlap but cannot prove independence.'),
        'workers': workers,
        'total': len(results),
        'passed': len(results) - failures,
        'failed': failures,
        'cases': results,
    }
    _write_json_exclusive(output, artifact)
    return artifact


def manifest_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='write an incomplete manifest template')
    prepare.add_argument('image_root', type=Path)
    prepare.add_argument('--output', type=Path, required=True)
    validate = commands.add_parser('validate', help='validate a curated manifest')
    validate.add_argument('manifest', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            value = prepare_manifest(args.image_root, args.output)
            print(f"Prepared {len(value['cases'])} incomplete fixture cases: {args.output}")
        else:
            cases = validate_manifest(args.manifest)
            splits = ', '.join(sorted({case.split for case in cases}))
            print(f'Validated {len(cases)} fixture cases ({splits})')
        return 0
    except (OSError, ValueError) as error:
        parser.error(str(error))


def evaluation_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--split', choices=SPLITS)
    parser.add_argument('--workers', type=int, default=1)
    args = parser.parse_args(argv)
    try:
        artifact = evaluate_manifest(
            args.manifest, args.output, split=args.split, workers=args.workers)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"{artifact['passed']}/{artifact['total']} {artifact['split']} cases passed; "
          f"{args.output}")
    return 1 if artifact['failed'] else 0
