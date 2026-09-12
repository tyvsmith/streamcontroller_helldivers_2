"""Prepare the scanner runtime for the plugin and interpret the outcome."""
from loguru import logger as log

from .provision.runtime_install import ensure_scanner_runtime


def prepare_scanner_runtime(plugin):
    """Prepare the runtime from the plugin's current settings; return the recorded status."""
    return ensure_scanner_runtime(plugin.PATH, plugin.get_settings())


def setup_failure_message(plugin, error):
    """Install the scanner runtime after a setup failure and report it.

    A tap that cannot scan prepares the runtime instead; the caller scans
    again once it is ready. Preparation never runs while a scan can start.
    """
    try:
        status = prepare_scanner_runtime(plugin)
    except Exception as failure:
        log.warning('Unable to prepare the scanner runtime: {}', failure)
        return str(error)
    if status.get('state') == 'installed':
        return 'Scanner runtime prepared. Scan again.'
    if status.get('state') == 'error':
        return f"Scanner setup failed: {status.get('error')}"
    return str(error)
