"""Import plugin-side modules the way StreamController loads the plugin.

StreamController appends only its data directory to sys.path and imports
``plugins.<folder>.main``, so no top-level ``automatic_stratagems`` package
exists in the plugin process. tools/check and check-sandbox both put the
repository root on sys.path, which hides absolute imports that only resolve
there.
"""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
FEATURE = ROOT / 'automatic_stratagems'
PLUGIN_ID = 'net_jslay_helldivers_2'

# Plugin-process modules that import without GTK.
PLUGIN_MODULES = (
    'automatic_stratagems.host_commands',
    'automatic_stratagems.scan_runner',
    'automatic_stratagems.scan_diagnostics',
    'automatic_stratagems.scan_state',
    'automatic_stratagems.scan_session',
    'automatic_stratagems.scan_operation',
    'automatic_stratagems.page_attempts',
    'automatic_stratagems.capture_source',
    'automatic_stratagems.temporary_scan_page',
    'automatic_stratagems.provision.runtime_install',
    'automatic_stratagems.provision.verify',
    'automatic_stratagems.scanner.screenshot_capture',
)

IMPORT_SCRIPT = '''
import importlib, importlib.util, sys
data, plugin_id, *modules = sys.argv[1:]
sys.path.append(data)
if importlib.util.find_spec("automatic_stratagems") is not None:
    sys.exit("the repository root leaked onto sys.path")
failures = []
for name in modules:
    try:
        importlib.import_module(f"plugins.{plugin_id}.{name}")
    except Exception as error:
        failures.append(f"{name}: {type(error).__name__}: {error}")
print("\\n".join(failures))
sys.exit(1 if failures else 0)
'''


class PluginPackageIdentityTests(unittest.TestCase):
    def test_plugin_side_modules_import_under_the_streamcontroller_package_name(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / 'data'
            (data / 'plugins').mkdir(parents=True)
            (data / 'plugins' / PLUGIN_ID).symlink_to(ROOT)
            environment = {name: value for name, value in os.environ.items()
                           if name != 'PYTHONPATH'}
            result = subprocess.run(
                [sys.executable, '-P', '-c', IMPORT_SCRIPT, str(data), PLUGIN_ID,
                 *PLUGIN_MODULES],
                cwd='/', env=environment, capture_output=True, text=True,
                timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_plugin_side_code_never_imports_the_top_level_package_name(self):
        # Lazy imports inside functions run in the plugin process too, so this
        # also covers code the import test above never executes.
        files = sorted([*FEATURE.glob('*.py'), *(FEATURE / 'provision').glob('*.py'),
                        *(FEATURE / 'shared').glob('*.py')])
        offenders = []
        for path in files:
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or '']
                else:
                    continue
                offenders += [f'{path.relative_to(ROOT)}:{node.lineno}: {name}'
                              for name in names
                              if name.split('.')[0] == 'automatic_stratagems']
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
