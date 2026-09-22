"""Image size limits have one dependency-free home that every scanner check reads."""

import ast
from pathlib import Path
import unittest

from automatic_stratagems.scanner import (capture_backends, image_decode, image_source,
                                          limits, scan_game)
from automatic_stratagems.scanner.capture import command, screenshot_files


class ScannerLimitsTests(unittest.TestCase):
    def test_limits_module_imports_nothing(self):
        tree = ast.parse(Path(limits.__file__).read_text())
        imports = [node for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [])

    def test_image_checks_read_the_shared_limits(self):
        for module in (capture_backends, image_decode, image_source, scan_game,
                       command, screenshot_files):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.MAX_ENCODED_IMAGE_BYTES,
                                 limits.MAX_ENCODED_IMAGE_BYTES)
        for module in (image_decode, image_source):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.MAX_IMAGE_DIMENSION, limits.MAX_IMAGE_DIMENSION)
                self.assertEqual(module.MAX_IMAGE_PIXELS, limits.MAX_IMAGE_PIXELS)

    def test_command_output_is_sized_for_one_encoded_capture(self):
        self.assertEqual(command.MAX_COMMAND_STDOUT_BYTES, limits.MAX_ENCODED_IMAGE_BYTES)


if __name__ == '__main__':
    unittest.main()
