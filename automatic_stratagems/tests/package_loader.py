"""Load this checkout under the installed plugin identity for integration tests."""
from pathlib import Path
import sys
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "net_jslay_helldivers_2"


def plugin_module(name):
    if PACKAGE not in sys.modules:
        package = ModuleType(PACKAGE)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package
    return f"{PACKAGE}.{name}"
