import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from automatic_stratagems.provision import runtime_install
from automatic_stratagems.provision.runtime_profile import ScanSetupError


ENABLED = {runtime_install.FEATURE_SETTING: True}


def settings_document(settings):
    """Wrap plugin settings the way StreamController's PluginBase writes them."""
    return {'file-version': '2.0', 'settings': settings}


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

    def test_a_reinstall_keeps_why_the_first_check_failed(self):
        checks = []

        def check():
            checks.append('check')
            if len(checks) == 1:
                raise ScanSetupError('Missing scanner dependency: evdev')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = runtime_install.ensure_scanner_runtime(
                root, ENABLED, flatpak=False, check=check, install=lambda: None)
            recorded = runtime_install.read_status(root)
        self.assertEqual(status['state'], runtime_install.STATE_INSTALLED)
        self.assertIsNone(status['error'])
        self.assertEqual(
            status['previous_error'], 'Missing scanner dependency: evdev')
        self.assertEqual(recorded, status)

    def test_a_failed_reinstall_keeps_why_the_first_check_failed(self):
        def install():
            raise ScanSetupError('Cannot download scanner runtime source')

        with tempfile.TemporaryDirectory() as directory:
            status = runtime_install.ensure_scanner_runtime(
                Path(directory), ENABLED, flatpak=True,
                check=lambda: (_ for _ in ()).throw(ScanSetupError('absent')),
                install=install)
        self.assertEqual(status['state'], runtime_install.STATE_ERROR)
        self.assertEqual(status['error'], 'Cannot download scanner runtime source')
        self.assertEqual(status['previous_error'], 'absent')

    def test_a_ready_runtime_records_no_previous_error(self):
        with tempfile.TemporaryDirectory() as directory:
            status = runtime_install.ensure_scanner_runtime(
                Path(directory), ENABLED, flatpak=True, check=lambda: None,
                install=lambda: None)
        self.assertNotIn('previous_error', status)

    def test_a_long_previous_error_still_fits_the_status_record(self):
        def check():
            raise ScanSetupError('x' * 10000)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_install.ensure_scanner_runtime(
                root, ENABLED, flatpak=True, check=check, install=lambda: None)
            recorded = runtime_install.read_status(root)
        self.assertEqual(recorded['state'], runtime_install.STATE_ERROR)
        self.assertTrue(recorded['previous_error'].startswith('xxx'))

    def test_long_non_ascii_errors_still_write_a_record_within_the_cap(self):
        def check():
            raise ScanSetupError('é' * 1024)

        def install():
            raise ScanSetupError('😀' * 1024)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = runtime_install.ensure_scanner_runtime(
                root, ENABLED, flatpak=True, check=check, install=install)
            size = runtime_install.status_path(root).stat().st_size
            recorded = runtime_install.read_status(root)
        self.assertLessEqual(size, runtime_install.MAX_STATUS_BYTES)
        self.assertEqual(recorded, status)
        self.assertEqual(recorded['state'], runtime_install.STATE_ERROR)
        self.assertEqual(recorded['profile'], 'flatpak')
        self.assertEqual(recorded['schema_version'], 1)
        self.assertTrue(recorded['updated_at'].endswith('Z'))
        self.assertTrue(recorded['error'].startswith('😀'))
        self.assertTrue(recorded['previous_error'].startswith('é'))

    def test_read_status_keeps_fields_it_does_not_know(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {'schema_version': 1, 'state': 'installed', 'profile': 'native',
                      'error': None, 'previous_error': 'absent',
                      'updated_at': '2026-01-01T00:00:00Z'}
            runtime_install.status_path(root).parent.mkdir(parents=True)
            runtime_install.status_path(root).write_text(json.dumps(record))
            self.assertEqual(runtime_install.read_status(root), record)

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


def _process_gone(pid, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            with open(f'/proc/{pid}/stat') as stat:
                if stat.read().rsplit(')', 1)[1].split()[0] == 'Z':
                    return True
        except OSError:
            return True
        time.sleep(0.02)
    return False


class PipProcessTests(unittest.TestCase):
    """The native pip install runs in its own group that shutdown can stop."""

    def setUp(self):
        registry = patch.object(
            runtime_install, '_INSTALLS', runtime_install._InstallRegistry())
        registry.start()
        self.addCleanup(registry.stop)
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)

    def start_pip(self, script):
        result = {}

        def run():
            try:
                result['value'] = runtime_install._pip(
                    [sys.executable, '-c', script])
            except BaseException as error:  # noqa: BLE001 - asserted below
                result['error'] = error

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, result

    def wait_for(self, path, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and path.read_text().strip():
                return path.read_text().split()
            time.sleep(0.02)
        self.fail(f'{path} was never written')

    def test_pip_runs_in_a_new_process_group(self):
        code, stderr = runtime_install._pip([
            sys.executable, '-c',
            'import os, sys; sys.stderr.write(f"{os.getpid()} {os.getpgrp()}")'])
        self.assertEqual(code, 0)
        pid, group = stderr.decode().split()
        self.assertEqual(pid, group)
        self.assertNotEqual(int(group), os.getpgrp())

    def test_terminate_stops_the_running_install_and_its_children(self):
        marker = self.directory / 'pids'
        script = (
            'import os, subprocess, sys, time\n'
            'child = subprocess.Popen([sys.executable, "-c",'
            ' "import time; time.sleep(60)"])\n'
            f'open({str(marker)!r}, "w").write(f"{{os.getpid()}} {{child.pid}}")\n'
            'time.sleep(60)\n')
        thread, result = self.start_pip(script)
        _pid, child = self.wait_for(marker)

        self.assertTrue(runtime_install.terminate_runtime_installs(grace=5))

        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result['value'][0], -signal.SIGTERM)
        self.assertTrue(_process_gone(int(child)))

    def test_terminate_kills_an_install_that_ignores_sigterm(self):
        marker = self.directory / 'pid'
        script = (
            'import os, signal, time\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            f'open({str(marker)!r}, "w").write(str(os.getpid()))\n'
            'time.sleep(60)\n')
        thread, result = self.start_pip(script)
        self.wait_for(marker)

        started = time.monotonic()
        self.assertTrue(runtime_install.terminate_runtime_installs(grace=0.3))
        thread.join(10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result['value'][0], -signal.SIGKILL)

    def test_no_install_starts_once_installs_are_terminated(self):
        self.assertTrue(runtime_install.terminate_runtime_installs(grace=1))
        with self.assertRaisesRegex(ScanSetupError, 'shutting down'):
            runtime_install._pip([sys.executable, '-c', 'pass'])


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
    def installed_tree(self, directory, settings, *, document=settings_document):
        data = Path(directory) / 'data'
        plugin_root = data / 'plugins/net_jslay_helldivers_2'
        plugin_root.mkdir(parents=True)
        if settings is not None:
            settings_dir = data / 'settings/plugins/net_jslay_helldivers_2'
            settings_dir.mkdir(parents=True)
            (settings_dir / 'settings.json').write_text(
                json.dumps(document(settings)))
        return plugin_root

    def test_plugin_settings_are_read_from_the_installed_data_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(directory, ENABLED)
            self.assertEqual(
                runtime_install.installed_plugin_settings(plugin_root), ENABLED)

    def test_an_enabled_plugin_reads_back_as_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(directory, ENABLED)
            self.assertIs(runtime_install.feature_enabled(
                runtime_install.installed_plugin_settings(plugin_root)), True)

    def test_pre_envelope_flat_plugin_settings_are_still_read(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(
                directory, ENABLED, document=lambda settings: settings)
            self.assertEqual(
                runtime_install.installed_plugin_settings(plugin_root), ENABLED)

    def settings_file(self, plugin_root):
        return (plugin_root.parent.parent
                / 'settings/plugins/net_jslay_helldivers_2/settings.json')

    def assert_unusable(self, plugin_root, reason):
        with self.assertRaisesRegex(runtime_install.PluginSettingsError, reason):
            runtime_install.installed_plugin_settings(plugin_root)

    def test_an_envelope_without_a_settings_object_is_unusable(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(
                directory, ENABLED,
                document=lambda settings: {'file-version': '2.0', 'settings': []})
            self.assert_unusable(plugin_root, 'settings object')

    def test_an_unknown_settings_file_version_is_unusable(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(
                directory, ENABLED,
                document=lambda settings: {'file-version': '3.0', 'settings': settings})
            self.assert_unusable(plugin_root, 'file-version 3.0')

    def test_invalid_settings_json_is_unusable(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(directory, ENABLED)
            self.settings_file(plugin_root).write_text('{"automatic')
            self.assert_unusable(plugin_root, 'Invalid JSON')

    def test_a_settings_document_that_is_not_an_object_is_unusable(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(
                directory, ENABLED, document=lambda settings: [settings])
            self.assert_unusable(plugin_root, 'not an object')

    def test_oversized_settings_are_unusable(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(directory, ENABLED)
            self.settings_file(plugin_root).write_text(
                ' ' * (runtime_install.MAX_SETTINGS_BYTES + 1))
            self.assert_unusable(plugin_root, 'too large')

    @unittest.skipIf(os.geteuid() == 0, 'root ignores file permissions')
    def test_unreadable_settings_are_unusable(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(directory, ENABLED)
            path = self.settings_file(plugin_root)
            path.chmod(0)
            try:
                self.assert_unusable(plugin_root, 'Permission denied')
            finally:
                path.chmod(0o600)

    def test_an_unusable_settings_file_is_recorded_as_a_setup_error(self):
        error = runtime_install.PluginSettingsError('Invalid JSON document')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = runtime_install.record_settings_error(
                root, error, flatpak=False)
            recorded = runtime_install.read_status(root)
        self.assertEqual(recorded, status)
        self.assertEqual(status['state'], runtime_install.STATE_ERROR)
        self.assertEqual(status['profile'], 'native')
        self.assertEqual(
            status['error'], 'cannot read plugin settings: Invalid JSON document')

    def test_plugin_settings_are_empty_without_a_settings_file(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(directory, None)
            self.assertEqual(
                runtime_install.installed_plugin_settings(plugin_root), {})

    def test_the_install_hook_records_the_disabled_feature_and_exits_zero(self):
        feature = Path(runtime_install.__file__).resolve().parents[1]
        hook = feature.parent / '__install__.py'
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(
                directory, {runtime_install.FEATURE_SETTING: False})
            (plugin_root / '__install__.py').write_bytes(hook.read_bytes())
            # Link the feature's contents but not its runtime/ payload directory,
            # so the status record lands in this tree and a record left by an
            # earlier run cannot satisfy the assertion.
            installed_feature = plugin_root / 'automatic_stratagems'
            installed_feature.mkdir()
            for entry in feature.iterdir():
                if entry.name not in ('runtime', '.venv'):
                    (installed_feature / entry.name).symlink_to(entry)

            result = subprocess.run(
                [sys.executable, str(plugin_root / '__install__.py')],
                capture_output=True, text=True, timeout=120)

            self.assertEqual(result.returncode, 0, result.stderr)
            status = runtime_install.read_status(plugin_root)
        self.assertEqual(status['state'], runtime_install.STATE_DISABLED)

    def run_hook(self, plugin_root):
        feature = Path(runtime_install.__file__).resolve().parents[1]
        hook = feature.parent / '__install__.py'
        (plugin_root / '__install__.py').write_bytes(hook.read_bytes())
        installed_feature = plugin_root / 'automatic_stratagems'
        installed_feature.mkdir()
        for entry in feature.iterdir():
            if entry.name not in ('runtime', '.venv'):
                (installed_feature / entry.name).symlink_to(entry)
        return subprocess.run(
            [sys.executable, str(plugin_root / '__install__.py')],
            capture_output=True, text=True, timeout=120)

    def test_the_install_hook_records_unusable_settings_as_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(directory, ENABLED)
            self.settings_file(plugin_root).write_text('{"automatic')
            result = self.run_hook(plugin_root)
            self.assertEqual(result.returncode, 0, result.stderr)
            status = runtime_install.read_status(plugin_root)
        self.assertEqual(status['state'], runtime_install.STATE_ERROR)
        self.assertEqual(
            status['error'], 'cannot read plugin settings: Invalid JSON document')
        self.assertEqual(result.stderr, '')
        self.assertEqual(len(result.stdout.splitlines()), 1)

    def test_the_install_hook_treats_a_missing_settings_file_as_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = self.installed_tree(directory, None)
            result = self.run_hook(plugin_root)
            self.assertEqual(result.returncode, 0, result.stderr)
            status = runtime_install.read_status(plugin_root)
        self.assertEqual(status['state'], runtime_install.STATE_DISABLED)
        self.assertIsNone(status['error'])

    def test_the_install_hook_module_avoids_the_scanner_dependencies(self):
        script = ('import sys\n'
                  'import automatic_stratagems.provision.runtime_install\n'
                  "print(sorted(name for name in sys.modules"
                  " if name in {'numpy', 'cv2', 'PIL', 'evdev', 'gi'}))\n")
        result = subprocess.run(
            [sys.executable, '-c', script], capture_output=True, text=True,
            cwd=str(Path(runtime_install.__file__).resolve().parents[2]),
            timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '[]')

    def test_importing_runtime_install_avoids_the_build_only_modules(self):
        script = ('import sys\n'
                  'import automatic_stratagems.provision.runtime_install\n'
                  "print(sorted(name for name in ('urllib.request', 'tarfile', 'zipfile')"
                  " if name in sys.modules))\n")
        result = subprocess.run(
            [sys.executable, '-c', script], capture_output=True, text=True,
            cwd=str(Path(runtime_install.__file__).resolve().parents[2]),
            timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '[]')

    def test_importing_verify_avoids_the_build_only_modules(self):
        script = ('import sys\n'
                  'import automatic_stratagems.provision.verify\n'
                  "print(sorted(name for name in ('urllib.request', 'tarfile', 'zipfile')"
                  " if name in sys.modules))\n")
        result = subprocess.run(
            [sys.executable, '-c', script], capture_output=True, text=True,
            cwd=str(Path(runtime_install.__file__).resolve().parents[2]),
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
            (settings_dir / 'settings.json').write_text(
                json.dumps(settings_document(ENABLED)))

            self.assertEqual(
                runtime_install.installed_plugin_settings(link), ENABLED)
