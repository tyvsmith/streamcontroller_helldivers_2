import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from automatic_stratagems import runtime_install
from automatic_stratagems.runtime_profile import ScanSetupError


ENABLED = {runtime_install.FEATURE_SETTING: True}


class FeatureFlagTests(unittest.TestCase):
    def test_feature_is_disabled_unless_the_setting_is_exactly_true(self):
        self.assertFalse(runtime_install.feature_enabled({}))
        self.assertFalse(runtime_install.feature_enabled(
            {runtime_install.FEATURE_SETTING: False}))
        self.assertFalse(runtime_install.feature_enabled(
            {runtime_install.FEATURE_SETTING: 'true'}))
        self.assertTrue(runtime_install.feature_enabled(
            {runtime_install.FEATURE_SETTING: True}))

    def test_ensure_skips_installation_while_the_feature_is_disabled(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            status = runtime_install.ensure_scanner_runtime(
                Path(directory), {}, flatpak=True,
                check=lambda: calls.append('check'),
                install=lambda: calls.append('install'))
        self.assertEqual(status['state'], runtime_install.STATE_DISABLED)
        self.assertEqual(calls, [])


class EnsureRuntimeTests(unittest.TestCase):
    def test_ensure_reports_ready_without_installing_a_valid_runtime(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            status = runtime_install.ensure_scanner_runtime(
                Path(directory), ENABLED, flatpak=True,
                check=lambda: calls.append('check'),
                install=lambda: calls.append('install'))
        self.assertEqual(status['state'], runtime_install.STATE_READY)
        self.assertEqual(calls, ['check'])

    def test_ensure_installs_once_and_revalidates_a_missing_runtime(self):
        calls = []

        def check():
            calls.append('check')
            if calls.count('check') == 1:
                raise ScanSetupError('Scanner Python is missing')

        with tempfile.TemporaryDirectory() as directory:
            status = runtime_install.ensure_scanner_runtime(
                Path(directory), ENABLED, flatpak=False,
                check=check, install=lambda: calls.append('install'))
        self.assertEqual(status['state'], runtime_install.STATE_INSTALLED)
        self.assertEqual(calls, ['check', 'install', 'check'])
        self.assertEqual(status['profile'], 'native')

    def test_ensure_reports_an_installation_error_verbatim(self):
        def install():
            raise ScanSetupError('Cannot download scanner runtime source')

        with tempfile.TemporaryDirectory() as directory:
            status = runtime_install.ensure_scanner_runtime(
                Path(directory), ENABLED, flatpak=True,
                check=lambda: (_ for _ in ()).throw(ScanSetupError('absent')),
                install=install)
        self.assertEqual(status['state'], runtime_install.STATE_ERROR)
        self.assertEqual(status['error'], 'Cannot download scanner runtime source')

    def test_ensure_reports_a_failing_revalidation_after_installation(self):
        def check():
            raise ScanSetupError('Missing scanner dependency: evdev')

        with tempfile.TemporaryDirectory() as directory:
            status = runtime_install.ensure_scanner_runtime(
                Path(directory), ENABLED, flatpak=False,
                check=check, install=lambda: None)
        self.assertEqual(status['state'], runtime_install.STATE_ERROR)
        self.assertEqual(status['error'], 'Missing scanner dependency: evdev')

    def test_ensure_records_an_unexpected_failure_instead_of_raising(self):
        def install():
            raise OSError('disk full')

        with tempfile.TemporaryDirectory() as directory:
            status = runtime_install.ensure_scanner_runtime(
                Path(directory), ENABLED, flatpak=True,
                check=lambda: (_ for _ in ()).throw(ScanSetupError('absent')),
                install=install)
        self.assertEqual(status['state'], runtime_install.STATE_ERROR)
        self.assertIn('disk full', status['error'])

    def test_ensure_writes_a_status_record_that_reads_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = runtime_install.ensure_scanner_runtime(
                root, ENABLED, flatpak=True, check=lambda: None,
                install=lambda: None)
            recorded = runtime_install.read_status(root)
            written = json.loads(
                runtime_install.status_path(root).read_text())
        self.assertEqual(recorded, status)
        self.assertEqual(written, status)
        self.assertEqual(status['schema_version'], 1)
        self.assertEqual(status['profile'], 'flatpak')
        self.assertIsNone(status['error'])
        self.assertTrue(status['updated_at'].endswith('Z'))

    def test_read_status_reports_no_record_before_any_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(runtime_install.read_status(Path(directory)))


class ScannerVenvTests(unittest.TestCase):
    def test_the_scanner_venv_is_owned_by_the_feature_directory(self):
        self.assertEqual(
            runtime_install.scanner_venv(Path('/plugin')),
            Path('/plugin/automatic_stratagems/.venv'))

    def test_native_install_creates_the_venv_and_installs_the_pinned_file(self):
        commands = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'automatic_stratagems').mkdir()
            (root / 'automatic_stratagems/requirements.txt').write_text(
                'numpy==2.5.3\n')
            runtime_install.install_scanner_venv(
                root, runner=lambda command: commands.append(command) or (0, b''))
            interpreter = runtime_install.scanner_venv(root) / 'bin/python'
            self.assertTrue(interpreter.is_file())
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0], str(interpreter))
        self.assertEqual(commands[0][1:4], ['-m', 'pip', 'install'])
        self.assertEqual(
            commands[0][-1], str(root / 'automatic_stratagems/requirements.txt'))
        self.assertEqual(commands[0][-2], '-r')

    def test_native_install_replaces_an_unusable_existing_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'automatic_stratagems').mkdir()
            (root / 'automatic_stratagems/requirements.txt').write_text('')
            stale = runtime_install.scanner_venv(root)
            (stale / 'lib/python3.12/site-packages').mkdir(parents=True)
            marker = stale / 'lib/python3.12/site-packages/numpy.py'
            marker.write_text('stale')
            runtime_install.install_scanner_venv(
                root, runner=lambda command: (0, b''))
            self.assertFalse(marker.exists())
            self.assertTrue((stale / 'bin/python').is_file())

    def test_native_install_reports_the_failing_dependency_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'automatic_stratagems').mkdir()
            (root / 'automatic_stratagems/requirements.txt').write_text('')
            with self.assertRaisesRegex(ScanSetupError, 'no matching distribution'):
                runtime_install.install_scanner_venv(
                    root, runner=lambda command: (1, b'no matching distribution'))


class DefaultInstallerTests(unittest.TestCase):
    def failing_check(self, calls):
        def check():
            calls.append('check')
            if calls.count('check') == 1:
                raise ScanSetupError('absent')
        return check

    def test_ensure_rebuilds_the_owned_venv_outside_flatpak(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(runtime_install, 'install_scanner_venv') as venv_install,
                  patch.object(runtime_install, 'install_runtime') as payload_install):
                status = runtime_install.ensure_scanner_runtime(
                    root, ENABLED, flatpak=False, check=self.failing_check(calls))
            venv_install.assert_called_once_with(root)
            payload_install.assert_not_called()
        self.assertEqual(status['state'], runtime_install.STATE_INSTALLED)

    def test_ensure_installs_the_locked_payload_inside_flatpak(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(runtime_install, 'install_scanner_venv') as venv_install,
                  patch.object(runtime_install, 'install_runtime') as payload_install):
                status = runtime_install.ensure_scanner_runtime(
                    root, ENABLED, flatpak=True, check=self.failing_check(calls))
            payload_install.assert_called_once_with(root)
            venv_install.assert_not_called()
        self.assertEqual(status['state'], runtime_install.STATE_INSTALLED)


class InstallHookTests(unittest.TestCase):
    def installed_tree(self, directory, settings):
        data = Path(directory) / 'data'
        plugin_root = data / 'plugins/net_jslay_helldivers_2'
        plugin_root.mkdir(parents=True)
        if settings is not None:
            settings_dir = data / 'settings/plugins/net_jslay_helldivers_2'
            settings_dir.mkdir(parents=True)
            (settings_dir / 'settings.json').write_text(json.dumps(settings))
        return plugin_root

    def test_plugin_settings_are_read_from_the_installed_data_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(directory, ENABLED)
            self.assertEqual(
                runtime_install.installed_plugin_settings(plugin_root), ENABLED)

    def test_plugin_settings_are_empty_without_a_settings_file(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(directory, None)
            self.assertEqual(
                runtime_install.installed_plugin_settings(plugin_root), {})

    def test_the_install_hook_records_the_disabled_feature_and_exits_zero(self):
        feature = Path(runtime_install.__file__).resolve().parent
        hook = feature.parent / '__install__.py'
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(
                directory, {runtime_install.FEATURE_SETTING: False})
            (plugin_root / '__install__.py').write_bytes(hook.read_bytes())
            (plugin_root / 'automatic_stratagems').symlink_to(feature)

            result = subprocess.run(
                [sys.executable, str(plugin_root / '__install__.py')],
                capture_output=True, text=True, timeout=120)

            self.assertEqual(result.returncode, 0, result.stderr)
            status = runtime_install.read_status(plugin_root)
        self.assertEqual(status['state'], runtime_install.STATE_DISABLED)

    def test_the_install_hook_module_avoids_the_scanner_dependencies(self):
        script = ('import sys\n'
                  'import automatic_stratagems.runtime_install\n'
                  "print(sorted(name for name in sys.modules"
                  " if name in {'numpy', 'cv2', 'PIL', 'evdev', 'gi'}))\n")
        result = subprocess.run(
            [sys.executable, '-c', script], capture_output=True, text=True,
            cwd=str(Path(runtime_install.__file__).resolve().parents[1]),
            timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '[]')

    def test_plugin_settings_follow_a_symlinked_plugin_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / 'checkout'
            checkout.mkdir()
            data = Path(directory) / 'data'
            (data / 'plugins').mkdir(parents=True)
            link = data / 'plugins/net_jslay_helldivers_2'
            link.symlink_to(checkout)
            settings_dir = data / 'settings/plugins/net_jslay_helldivers_2'
            settings_dir.mkdir(parents=True)
            (settings_dir / 'settings.json').write_text(json.dumps(ENABLED))

            self.assertEqual(
                runtime_install.installed_plugin_settings(link), ENABLED)
