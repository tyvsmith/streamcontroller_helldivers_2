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
        from automatic_stratagems.runtime_install import (
            ensure_scanner_runtime, installed_plugin_settings,
        )
        status = ensure_scanner_runtime(ROOT, installed_plugin_settings(ROOT))
    except Exception as error:  # noqa: BLE001 - never fail the installation
        print(f"automatic stratagems: setup skipped: {error}", file=sys.stderr)
        return 0
    detail = f": {status['error']}" if status.get("error") else ""
    print(f"automatic stratagems: scanner runtime {status['state']}{detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
