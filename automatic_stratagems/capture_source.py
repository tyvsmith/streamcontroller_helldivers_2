"""Dependency-light capture-source normalization and serialization."""

import os

CAPTURE_BACKENDS = ('auto', 'gamescope', 'screenshot')
DEFAULT_SCREENSHOT_TRIGGER = 'hotkey'
DEFAULT_SCREENSHOT_HOTKEY = 'KEY_F12'
DEFAULT_SCREENSHOT_SCRIPT = ''
DEFAULT_SCREENSHOT_FOLDER = ''
DEFAULT_SCREENSHOT_DELETE = True
SCREENSHOT_TRIGGERS = ('hotkey', 'script')
SCREENSHOT_SETTING_KEYS = (
    'screenshot_trigger', 'screenshot_hotkey', 'screenshot_script',
    'screenshot_folder', 'screenshot_delete',
)
_LEGACY_IMAGE_SOURCE_HISTORY = (
    'image_source_last_fingerprint',
    'image_source_last_path',
    'image_source_last_mtime_ns',
)
MAX_SOURCE_STRING = 4096


def normalize_capture_backend(settings, default='auto'):
    """Return the canonical backend for current and retired settings."""
    settings = settings if isinstance(settings, dict) else {}
    value = settings.get('capture_backend')
    if value in CAPTURE_BACKENDS:
        return value
    if settings.get('image_source_kind', 'live') in ('file', 'folder', 'path'):
        return 'screenshot'
    if value is not None:
        return 'screenshot'
    return default if default in CAPTURE_BACKENDS else 'screenshot'


def screenshot_config(settings):
    """Return the normalized public screenshot source configuration."""
    settings = settings if isinstance(settings, dict) else {}
    trigger = settings.get('screenshot_trigger', DEFAULT_SCREENSHOT_TRIGGER)
    if trigger not in SCREENSHOT_TRIGGERS:
        trigger = DEFAULT_SCREENSHOT_TRIGGER
    hotkey = settings.get('screenshot_hotkey', DEFAULT_SCREENSHOT_HOTKEY)
    if not isinstance(hotkey, str) or not hotkey.strip():
        hotkey = DEFAULT_SCREENSHOT_HOTKEY
    script = settings.get('screenshot_script', DEFAULT_SCREENSHOT_SCRIPT)
    if not isinstance(script, str):
        script = DEFAULT_SCREENSHOT_SCRIPT
    folder = settings.get('screenshot_folder', DEFAULT_SCREENSHOT_FOLDER)
    if not isinstance(folder, str):
        folder = DEFAULT_SCREENSHOT_FOLDER
    delete = settings.get('screenshot_delete', DEFAULT_SCREENSHOT_DELETE)
    if type(delete) is not bool:
        delete = DEFAULT_SCREENSHOT_DELETE
    return {
        'kind': 'folder', 'path': folder.strip(), 'trigger': trigger,
        'hotkey': hotkey.strip(), 'script': script.strip(),
        'delete_after_scan': delete,
    }


def screenshot_source_identity(backend, settings):
    """Bind a capture backend to the global screenshot configuration."""
    if backend not in CAPTURE_BACKENDS:
        raise ValueError('Invalid capture backend')
    return {
        'backend': backend,
        'source': None if backend == 'gamescope' else screenshot_config(settings),
    }


def source_settings(action_settings, plugin_settings):
    """Merge the action backend with plugin-owned source settings."""
    merged = dict(action_settings if isinstance(action_settings, dict) else {})
    for key in _LEGACY_IMAGE_SOURCE_HISTORY:
        merged.pop(key, None)
    merged['capture_backend'] = normalize_capture_backend(merged)
    config = screenshot_config(plugin_settings)
    merged.update(
        screenshot_trigger=config['trigger'], screenshot_hotkey=config['hotkey'],
        screenshot_script=config['script'], screenshot_folder=config['path'],
        screenshot_delete=config['delete_after_scan'])
    return merged


def source_identity(settings):
    return screenshot_source_identity(
        normalize_capture_backend(settings), settings)


def operation_source(settings, *, allow_rescan=False):
    identity = source_identity(settings)
    if identity['source'] is None:
        return None
    return {**identity['source'], 'previous_fingerprint': None,
            'allow_rescan': bool(allow_rescan)}


def page_source_settings(settings):
    return {key: settings[key]
            for key in ('capture_backend', *SCREENSHOT_SETTING_KEYS)
            if key in settings}


def source_arguments(source, *, defer_trigger=False):
    """Validate and serialize a source for the scanner command boundary."""
    if source is None:
        return []
    if not isinstance(source, dict) or source.get('kind') not in (
            'file', 'folder', 'path'):
        raise ValueError('Invalid image source kind')
    path = source.get('path')
    if not isinstance(path, str) or len(path) > MAX_SOURCE_STRING:
        raise ValueError('Choose an image source path')
    previous = source.get('previous_fingerprint')
    if previous is not None and (not isinstance(previous, str) or len(previous) != 64
                                 or any(c not in '0123456789abcdef' for c in previous)):
        raise ValueError('Invalid previous image source fingerprint')
    allow_rescan = source.get('allow_rescan', False)
    if type(allow_rescan) is not bool:
        raise ValueError('Invalid image source rescan choice')
    trigger = source.get('trigger', 'none')
    hotkey = source.get('hotkey', 'KEY_F12')
    script = source.get('script', '')
    delete = source.get('delete_after_scan', False)
    if trigger not in ('none', 'hotkey', 'script') or type(delete) is not bool:
        raise ValueError('Invalid screenshot trigger or deletion choice')
    if (not isinstance(hotkey, str) or len(hotkey) > 256
            or not isinstance(script, str) or len(script) > MAX_SOURCE_STRING):
        raise ValueError('Invalid screenshot trigger settings')
    if (trigger == 'script' and not defer_trigger
            and not os.path.isabs(os.path.expanduser(script))):
        raise ValueError('Screenshot script must be an absolute executable path')
    path = os.path.expanduser(path.strip())
    if path and not os.path.isabs(path) and not defer_trigger:
        raise ValueError('Image source path must be absolute or start with ~/')
    path = os.path.abspath(path) if os.path.isabs(path) else path
    arguments = ['--image-source-kind', source['kind'], '--image-source-path', path]
    arguments.extend(['--screenshot-trigger', trigger,
                      '--screenshot-hotkey', hotkey, '--screenshot-script', script])
    if delete:
        arguments.append('--delete-screenshot')
    if previous:
        arguments.extend(['--previous-image-fingerprint', previous])
    if allow_rescan:
        arguments.append('--allow-image-rescan')
    return arguments
