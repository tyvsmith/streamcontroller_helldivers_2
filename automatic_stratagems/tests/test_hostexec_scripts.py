"""Prove the hostexec scripts import cleanly as bare host scripts."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from automatic_stratagems import host_commands as hc
from automatic_stratagems import hostexec
from automatic_stratagems.scanner.capture import gamescope as sandbox_gamescope
from automatic_stratagems.shared.gamescope_target import (
    FlatpakGamescopeTarget, GAMESCOPECTL_PATH)


def _host_environment():
    """The environment a host launch would see: no inherited PYTHONPATH."""
    return {name: value for name, value in os.environ.items()
            if name != 'PYTHONPATH'}


def _bare_script(script, *args):
    """Run a script with no site-packages (-S) and no user path (-I).

    Without -S the dev virtualenv's third-party packages stay importable, so a
    stray non-stdlib import in a host script would still pass.
    """
    return subprocess.run(
        [sys.executable, '-I', '-S', str(script), *args],
        cwd='/', env=_host_environment(), capture_output=True, text=True,
        timeout=60)


class HostexecScriptTests(unittest.TestCase):
    def test_scripts_are_located_by_name(self):
        self.assertTrue(hostexec.script('host_metadata.py').is_file())
        self.assertTrue(hostexec.script('gamescope_flatpak.py').is_file())

    def test_every_hostexec_script_imports_under_a_bare_host_interpreter(self):
        # check-imports only sees import statements; this proves each script's
        # sys.path bootstrap works and it loads nothing outside the stdlib.
        scripts = sorted(path for path in hostexec.DIRECTORY.glob('*.py')
                         if path.name != '__init__.py')
        self.assertGreaterEqual(len(scripts), 2)
        for script in scripts:
            with self.subTest(script=script.name):
                result = _bare_script(script)
                self.assertNotIn('Traceback', result.stderr, result.stderr)

    def test_host_metadata_runs_as_a_bare_script_without_the_package_on_path(self):
        result = _bare_script(hostexec.script('host_metadata.py'), '--nope')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Expected --capture-target.', result.stderr)

    def test_gamescope_flatpak_runs_as_a_bare_script_without_the_package_on_path(self):
        result = _bare_script(hostexec.script('gamescope_flatpak.py'))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Missing host job arguments.', result.stderr)

    def test_capture_target_resolution_launches_the_hostexec_script(self):
        with patch(
                'automatic_stratagems.scanner.game_capture.run_command',
                return_value=b'{"kind":"native","socket":"/run/user/1000/gamescope-2"}'
        ) as run:
            sandbox_gamescope.resolve_gamescope_capture_target()
        command = run.call_args.args[0]
        self.assertEqual(command[0], '/usr/bin/python3')
        self.assertEqual(command[1], str(hostexec.script('host_metadata.py')))
        self.assertEqual(command[2], '--capture-target')

    def test_flatpak_capture_launches_the_hostexec_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache'
            cache.mkdir()
            metadata = cache.stat()
            target = FlatpakGamescopeTarget(
                game_pid=100, game_start_time='1000', sandbox_pid=50,
                sandbox_start_time='500', instance_id='123',
                socket='/run/user/1000/gamescope-4', socket_dev=1, socket_ino=2,
                host_cache=str(cache), sandbox_cache='/steam/cache',
                cache_dev=metadata.st_dev, cache_ino=metadata.st_ino,
                gamescopectl=GAMESCOPECTL_PATH)
            with hc.create_host_job(base=root / 'jobs', hard_timeout=3) as job:
                output = job.path / 'gamescope-test' / 'capture.png'
                output.parent.mkdir(mode=0o700)
                with patch.dict(os.environ, hc.host_job_environment(job)), \
                     patch('automatic_stratagems.scanner.game_capture.run_command',
                           return_value=b'') as run:
                    sandbox_gamescope.capture_into_shared_path(
                        target, output, timeout=1, deadline=123.5)
            capture_argv = run.call_args_list[0].args[0]
            cleanup_argv = run.call_args_list[1].args[0]
        script = str(hostexec.script('gamescope_flatpak.py'))
        self.assertEqual(capture_argv[:2], ['/usr/bin/python3', script])
        self.assertEqual(cleanup_argv[:2], ['/usr/bin/python3', script])


if __name__ == '__main__':
    unittest.main()
