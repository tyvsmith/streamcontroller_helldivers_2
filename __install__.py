#!/usr/bin/env python3
"""Prepare the optional scanner runtime when StreamController installs this plugin.

StreamController runs this hook with its own interpreter after every store
install and update, before the plugin is loaded. It prepares nothing while
automatic stratagems are switched off, and it always exits successfully so a
failed preparation cannot fail the plugin installation.
"""

import os
from pathlib import Path
import sys

# Normalized without resolving symlinks: a symlinked plugin directory keeps the
# installed layout that holds this plugin's settings.
ROOT = Path(os.path.abspath(__file__)).parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        from automatic_stratagems.provision.runtime_install import (
            PluginSettingsError, ensure_scanner_runtime,
            installed_plugin_settings, record_settings_error,
        )
        try:
            settings = installed_plugin_settings(ROOT)
        except PluginSettingsError as error:
            status = record_settings_error(ROOT, error)
        else:
            status = ensure_scanner_runtime(ROOT, settings)
    except Exception as error:  # noqa: BLE001 - never fail the installation
        print(f"automatic stratagems: setup skipped: {error}", file=sys.stderr)
        return 0
    detail = f": {status['error']}" if status.get("error") else ""
    print(f"automatic stratagems: scanner runtime {status['state']}{detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
