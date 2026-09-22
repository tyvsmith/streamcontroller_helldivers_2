"""Every scanner module must catch and raise the same ScanError class object."""

import ast
import importlib
from pathlib import Path
import unittest

from automatic_stratagems.scanner.errors import ScanError
from automatic_stratagems.scanner import capture_backends, image_source, scan_game
from automatic_stratagems.scanner import selection_layout
from automatic_stratagems.scanner.capture import command, gamescope
from automatic_stratagems.scanner.capture import (screenshot_detect, screenshot_files,
                                                  screenshot_trigger)
from automatic_stratagems.scanner import image_decode


def _resolve(module_name, node):
    """Fully-qualified module a relative ``from ... import ScanError`` reaches."""
    relative_name = '.' * node.level + (node.module or '')
    target = importlib.import_module(relative_name, package=module_name)
    return target.ScanError


def _scanerror_import_targets(module):
    """Every module ``ScanError`` is imported from, at module or function scope."""
    tree = ast.parse(Path(module.__file__).read_text())
    targets = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.ImportFrom) and
                any(alias.name == 'ScanError' for alias in node.names)):
            targets.append(_resolve(module.__package__, node))
    return targets


class ScanErrorIdentityTests(unittest.TestCase):
    def test_split_modules_share_the_one_scanerror_class(self):
        self.assertIs(command.ScanError, ScanError)
        self.assertIs(image_decode.ScanError, ScanError)

    def test_module_scope_importers_share_the_one_scanerror_class(self):
        self.assertIs(capture_backends.ScanError, ScanError)
        self.assertIs(image_source.ScanError, ScanError)
        self.assertIs(scan_game.ScanError, ScanError)
        self.assertIs(selection_layout.ScanError, ScanError)

    def test_function_scope_importers_resolve_the_same_scanerror_class(self):
        # These modules import ScanError inside functions rather than at module
        # scope, so patch-object identity can't be checked directly. Resolve
        # every such import statement's target module and compare classes.
        for module in (gamescope, screenshot_detect, screenshot_files,
                       screenshot_trigger):
            targets = _scanerror_import_targets(module)
            with self.subTest(module=module.__name__):
                self.assertTrue(targets, "no ScanError import found")
                for target in targets:
                    self.assertIs(target, ScanError)


if __name__ == '__main__':
    unittest.main()
