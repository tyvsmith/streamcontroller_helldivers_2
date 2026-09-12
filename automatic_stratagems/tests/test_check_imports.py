"""Tests for automatic_stratagems/tools/check-imports.

The tool has no file extension (it is an executable script, not an importable
module), so it is loaded with importlib.machinery.SourceFileLoader directly
from its path rather than by name.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "automatic_stratagems/tools/check-imports"


def load_tool():
    loader = importlib.machinery.SourceFileLoader("check_imports_under_test", str(TOOL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def write(root: Path, relative: str, content: str = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class CheckImportsToolTests(unittest.TestCase):
    """Exercise the tool in-process via its main(argv) entry point."""

    def setUp(self):
        self.tool = load_tool()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def run_tool(self, *args):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.tool.main(["--root", str(self.root), *args])
        return code, buffer.getvalue()

    def assertFails(self, *args):
        code, text = self.run_tool(*args)
        self.assertEqual(code, 1, text)
        return text

    def assertPasses(self, *args):
        code, text = self.run_tool(*args)
        self.assertEqual(code, 0, text)
        return text

    # -- passing cases --------------------------------------------------

    def test_stdlib_only_shared_hostexec_provision_and_install_hook_pass(self):
        write(self.root, "automatic_stratagems/shared/fs.py", "import os\nimport json\n")
        write(self.root, "automatic_stratagems/hostexec/run.py", "import subprocess\n")
        write(self.root, "automatic_stratagems/provision/setup.py", "import shutil\n")
        write(self.root, "__install__.py", "import sys\n")
        out = self.assertPasses()
        self.assertIn("4 files OK", out)

    def test_future_annotations_import_passes(self):
        write(self.root, "automatic_stratagems/shared/fs.py",
              "from __future__ import annotations\nimport os\n")
        self.assertPasses("automatic_stratagems/shared")

    # -- failing cases: direct imports -----------------------------------

    def test_module_level_third_party_import_fails_and_names_file_and_line(self):
        write(self.root, "automatic_stratagems/shared/bad.py", "import os\nimport numpy\n")
        out = self.assertFails("automatic_stratagems/shared")
        self.assertIn("automatic_stratagems/shared/bad.py:2", out)
        self.assertIn("numpy", out)

    def test_function_level_import_fails(self):
        write(self.root, "automatic_stratagems/shared/bad.py",
              "def f():\n    import gi\n    return gi\n")
        out = self.assertFails("automatic_stratagems/shared")
        self.assertIn("automatic_stratagems/shared/bad.py:2", out)
        self.assertIn("gi", out)

    def test_import_inside_try_except_fails(self):
        write(self.root, "automatic_stratagems/shared/bad.py",
              "try:\n    import cv2\nexcept ImportError:\n    cv2 = None\n")
        out = self.assertFails("automatic_stratagems/shared")
        self.assertIn("automatic_stratagems/shared/bad.py:2", out)
        self.assertIn("cv2", out)

    # -- internal prefix rules -------------------------------------------

    def test_hostexec_may_import_its_own_and_shared_prefixes(self):
        write(self.root, "automatic_stratagems/hostexec/run.py",
              "from automatic_stratagems.shared import fs\n"
              "from automatic_stratagems.hostexec import helpers\n")
        self.assertPasses("automatic_stratagems/hostexec")

    def test_shared_may_not_import_hostexec(self):
        write(self.root, "automatic_stratagems/shared/fs.py",
              "from automatic_stratagems.hostexec import helpers\n")
        out = self.assertFails("automatic_stratagems/shared")
        self.assertIn("automatic_stratagems.hostexec", out)

    def test_other_internal_top_level_modules_are_never_allowed(self):
        write(self.root, "automatic_stratagems/shared/fs.py",
              "import automatic_stratagems.scanner\n")
        out = self.assertFails("automatic_stratagems/shared")
        self.assertIn("automatic_stratagems.scanner", out)

    # -- relative imports --------------------------------------------------

    def test_relative_import_reaching_into_sibling_scanner_package_fails(self):
        write(self.root, "automatic_stratagems/shared/fs.py",
              "from ..scanner import game_capture\n")
        out = self.assertFails("automatic_stratagems/shared")
        self.assertIn("automatic_stratagems.scanner", out)

    def test_relative_import_within_shared_passes(self):
        write(self.root, "automatic_stratagems/shared/__init__.py", "")
        write(self.root, "automatic_stratagems/shared/fs.py", "")
        write(self.root, "automatic_stratagems/shared/user.py", "from . import fs\n")
        self.assertPasses("automatic_stratagems/shared")

    def test_relative_import_from_parent_package_by_name_fails(self):
        write(self.root, "automatic_stratagems/shared/fs.py",
              "from .. import scanner\n")
        out = self.assertFails("automatic_stratagems/shared")
        self.assertIn("automatic_stratagems.scanner", out)

    def test_relative_import_climbing_above_the_root_fails(self):
        write(self.root, "automatic_stratagems/shared/fs.py",
              "from .... import escape\n")
        out = self.assertFails("automatic_stratagems/shared")
        self.assertIn("automatic_stratagems/shared/fs.py:1", out)
        self.assertIn("climbs above", out)

    # -- dynamic imports -----------------------------------------------

    def test_importlib_import_module_literal_fails(self):
        write(self.root, "automatic_stratagems/shared/fs.py",
              "import importlib\nimportlib.import_module('cv2')\n")
        out = self.assertFails("automatic_stratagems/shared")
        self.assertIn("cv2", out)

    def test_dunder_import_literal_fails(self):
        write(self.root, "automatic_stratagems/shared/fs.py", "__import__('PIL')\n")
        out = self.assertFails("automatic_stratagems/shared")
        self.assertIn("PIL", out)

    def test_import_module_with_variable_argument_is_ignored(self):
        write(self.root, "automatic_stratagems/shared/fs.py",
              "import importlib\nname = 'cv2'\nimportlib.import_module(name)\n")
        self.assertPasses("automatic_stratagems/shared")

    # -- install hook scope -----------------------------------------------

    def test_install_hook_importing_provision_passes(self):
        write(self.root, "__install__.py",
              "from automatic_stratagems.provision import runtime\n")
        self.assertPasses("__install__.py")

    def test_install_hook_importing_disallowed_module_fails(self):
        write(self.root, "__install__.py",
              "from automatic_stratagems.runtime_install import ensure_scanner_runtime\n")
        out = self.assertFails("__install__.py")
        self.assertIn("automatic_stratagems.runtime_install", out)

    # -- scope selection and existence -------------------------------------

    def test_missing_scope_directory_fails(self):
        # automatic_stratagems/hostexec does not exist anywhere under root.
        out = self.assertFails("automatic_stratagems/hostexec")
        self.assertIn("automatic_stratagems/hostexec", out)

    def test_scope_directory_with_no_python_files_fails(self):
        (self.root / "automatic_stratagems/shared").mkdir(parents=True)
        write(self.root, "automatic_stratagems/shared/README.txt", "not python\n")
        out = self.assertFails("automatic_stratagems/shared")
        self.assertIn("automatic_stratagems/shared", out)

    def test_unknown_scope_name_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_tool("automatic_stratagems/nonexistent")
        self.assertEqual(caught.exception.code, 2)

    def test_syntax_error_fails(self):
        write(self.root, "automatic_stratagems/shared/broken.py", "def f(:\n    pass\n")
        out = self.assertFails("automatic_stratagems/shared")
        self.assertIn("automatic_stratagems/shared/broken.py", out)

    def test_selecting_single_scope_only_checks_that_scope(self):
        write(self.root, "automatic_stratagems/shared/fs.py", "import os\n")
        write(self.root, "automatic_stratagems/hostexec/run.py", "import numpy\n")
        out = self.assertPasses("automatic_stratagems/shared")
        self.assertIn("1 files OK", out)


class CheckImportsSubprocessTests(unittest.TestCase):
    """Exercise the tool as an executable, stdlib-only script."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(TOOL), "--root", str(self.root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)

    def test_clean_tree_exits_zero(self):
        write(self.root, "automatic_stratagems/shared/fs.py", "import os\n")
        result = self.run_cli("automatic_stratagems/shared")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 files OK", result.stdout)

    def test_violation_exits_one_with_message_on_stdout(self):
        write(self.root, "automatic_stratagems/shared/bad.py", "import numpy\n")
        result = self.run_cli("automatic_stratagems/shared")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("numpy", result.stdout)

    def test_no_arguments_checks_every_rule(self):
        write(self.root, "automatic_stratagems/shared/fs.py", "import os\n")
        write(self.root, "automatic_stratagems/hostexec/run.py", "import subprocess\n")
        write(self.root, "automatic_stratagems/provision/setup.py", "import shutil\n")
        write(self.root, "__install__.py", "import sys\n")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("4 files OK", result.stdout)

    def test_unknown_scope_exits_two(self):
        result = self.run_cli("nope")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)


class CheckImportsRealRepositoryTests(unittest.TestCase):
    """Runs the tool against the actual repository tree, not a temp fixture.

    Every stdlib-only scope declared in check-imports' RULES must actually
    hold in this repository, not just in synthetic fixtures.
    """

    def test_real_repository_passes_every_scope(self):
        result = subprocess.run(
            [sys.executable, str(TOOL)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
