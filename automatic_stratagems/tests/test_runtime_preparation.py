import importlib
import sys
import types
import unittest
from unittest.mock import Mock, patch

from .package_loader import plugin_module


class RuntimePreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(sys.modules, {
                'loguru': types.SimpleNamespace(logger=Mock())}):
            cls.mod = importlib.import_module(
                plugin_module('automatic_stratagems.runtime_preparation'))

    def setUp(self):
        self.settings = {'automatic_stratagems_enabled': True}
        self.plugin = types.SimpleNamespace(
            PATH='/tmp/plugin', get_settings=lambda: dict(self.settings))

    def message(self, status=None, *, raised=None):
        ensure = Mock(return_value=status, side_effect=raised)
        with patch.object(self.mod, 'ensure_scanner_runtime', ensure), \
             patch.object(self.mod, 'log') as log:
            message = self.mod.prepare_after_setup_failure(
                self.plugin, RuntimeError('Scanner Python is missing'))
        ensure.assert_called_once_with('/tmp/plugin', self.settings)
        return message, log

    def test_preparation_uses_the_plugin_path_and_current_settings(self):
        status = {'state': 'ready', 'profile': 'native', 'error': None}
        with patch.object(self.mod, 'ensure_scanner_runtime',
                          return_value=status) as ensure:
            self.assertIs(self.mod.prepare_scanner_runtime(self.plugin), status)
        ensure.assert_called_once_with('/tmp/plugin', self.settings)

    def test_preparation_lets_its_failure_reach_the_caller(self):
        with patch.object(self.mod, 'ensure_scanner_runtime',
                          side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.mod.prepare_scanner_runtime(self.plugin)

    def test_an_installed_runtime_asks_for_another_scan(self):
        message, log = self.message({'state': 'installed', 'error': None})
        self.assertEqual(message, 'Scanner runtime prepared. Scan again.')
        log.warning.assert_not_called()

    def test_a_failed_preparation_reports_its_error(self):
        message, _ = self.message(
            {'state': 'error', 'error': 'Cannot download scanner runtime source'})
        self.assertEqual(
            message, 'Scanner setup failed: Cannot download scanner runtime source')

    def test_any_other_state_keeps_the_setup_error(self):
        for state in ('ready', 'disabled', None):
            with self.subTest(state=state):
                message, _ = self.message({'state': state, 'error': None})
                self.assertEqual(message, 'Scanner Python is missing')

    def test_a_raised_preparation_keeps_the_setup_error_and_warns(self):
        failure = OSError('disk full')
        message, log = self.message(raised=failure)
        self.assertEqual(message, 'Scanner Python is missing')
        log.warning.assert_called_once_with(
            'Unable to prepare the scanner runtime: {}', failure)


if __name__ == '__main__':
    unittest.main()
