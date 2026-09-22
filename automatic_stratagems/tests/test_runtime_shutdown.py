"""Runtime installs stop with the plugin and leave no staging behind."""
import fcntl
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from automatic_stratagems.provision import build
from automatic_stratagems.provision.runtime_profile import ScanSetupError

from . import test_optional_integration
from .package_loader import plugin_module


class StagingSweepTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.staging = Path(self.directory.name) / 'staging'
        self.staging.mkdir()

    def stage(self, name, *, lock=True):
        stage = self.staging / name
        (stage / 'python').mkdir(parents=True)
        (stage / 'python/partial').write_bytes(b'x')
        if lock:
            (self.staging / f'{name}{build.STAGE_LOCK_SUFFIX}').touch()
        return stage

    def hold_lock(self, name):
        """Hold a stage lock from another process, as a live install would."""
        path = self.staging / f'{name}{build.STAGE_LOCK_SUFFIX}'
        holder = subprocess.Popen(
            [sys.executable, '-c',
             'import fcntl, sys\n'
             'descriptor = open(sys.argv[1], "a")\n'
             'fcntl.flock(descriptor, fcntl.LOCK_EX)\n'
             'print("held", flush=True)\n'
             'sys.stdin.read()\n',
             str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait, 10)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        return path

    def test_sweep_removes_stages_without_a_live_owner(self):
        orphan = self.stage('flatpak-orphan', lock=False)
        released = self.stage('flatpak-released')
        stray_lock = self.staging / f'flatpak-gone{build.STAGE_LOCK_SUFFIX}'
        stray_lock.touch()

        build.sweep_stale_staging(self.staging)

        self.assertEqual(list(self.staging.iterdir()), [])
        self.assertFalse(orphan.exists())
        self.assertFalse(released.exists())

    def test_sweep_keeps_a_stage_another_install_is_using(self):
        live = self.stage('flatpak-live')
        lock = self.hold_lock('flatpak-live')
        stale = self.stage('flatpak-stale', lock=False)

        build.sweep_stale_staging(self.staging)

        self.assertTrue((live / 'python/partial').is_file())
        self.assertTrue(lock.exists())
        self.assertFalse(stale.exists())

    def test_sweep_keeps_a_lock_whose_owner_has_not_made_its_stage_yet(self):
        lock = self.hold_lock('flatpak-starting')

        build.sweep_stale_staging(self.staging)

        self.assertTrue(lock.exists())

    def test_an_install_holds_its_stage_lock_until_it_finishes(self):
        observed = []

        def preflight(stage, _manifest):
            lock = stage.parent / f'{stage.name}{build.STAGE_LOCK_SUFFIX}'
            descriptor = os.open(lock, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            build.sweep_stale_staging(stage.parent)
            observed.append(stage.is_dir())
            raise ScanSetupError('stop after preflight')

        root = Path(self.directory.name) / 'plugin'
        lock = {'schema_version': 1, 'sources': [], 'files': []}
        with patch.object(build, '_read_lock', return_value=(lock, '0' * 64)), \
             patch.object(build, 'validate_profile', return_value=({}, None)), \
             patch.object(build, '_sha256', return_value=None):
            with self.assertRaisesRegex(ScanSetupError, 'stop after preflight'):
                build.install_runtime(root, preflight=preflight)
        self.assertEqual(observed, [True])
        staging = root / 'automatic_stratagems/runtime/staging'
        self.assertEqual(list(staging.iterdir()), [])

    def test_install_sweeps_stale_staging_when_it_starts(self):
        root = Path(self.directory.name) / 'plugin'
        staging = root / 'automatic_stratagems/runtime/staging'
        (staging / 'flatpak-interrupted/python').mkdir(parents=True)
        lock = {'schema_version': 1, 'sources': [{'name': 'tesseract'}], 'files': []}
        with patch.object(build, '_read_lock', return_value=(lock, '0' * 64)), \
             patch.object(build, '_source_path',
                          side_effect=ScanSetupError('offline source missing')):
            with self.assertRaisesRegex(ScanSetupError, 'offline source missing'):
                build.install_runtime(root)
        self.assertEqual(list(staging.iterdir()), [])


class StopRuntimeInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(sys.modules, {
                'loguru': types.SimpleNamespace(logger=Mock())}):
            cls.mod = importlib.import_module(
                plugin_module('automatic_stratagems.runtime_preparation'))

    def test_stop_terminates_installs_with_a_grace_period(self):
        with patch.object(self.mod, 'terminate_runtime_installs',
                          return_value=True) as terminate:
            self.assertTrue(self.mod.stop_runtime_install(grace=1.5))
        terminate.assert_called_once_with(grace=1.5)

    def test_stop_reports_a_failure_instead_of_raising(self):
        with patch.object(self.mod, 'terminate_runtime_installs',
                          side_effect=OSError('no such process')), \
             patch.object(self.mod, 'log') as log:
            self.assertFalse(self.mod.stop_runtime_install())
        log.warning.assert_called_once()


class IntegrationShutdownTests(unittest.TestCase):
    def test_integration_shutdown_stops_a_running_runtime_install(self):
        loader = test_optional_integration.OptionalIntegrationTests(
            'test_uninstall_delegates_feature_cleanup_before_base_uninstall')
        self.addCleanup(loader.doCleanups)
        _, plugin = loader.import_without_automatic_actions()
        integration = plugin._automatic_integration
        integration.coordinator = Mock()
        preparation = type(integration).shutdown.__globals__['runtime_preparation']
        with patch.object(preparation, 'stop_runtime_install') as stop:
            integration.shutdown()
        stop.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
