import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from automatic_stratagems.scanner import host_metadata as hm


class HostMetadataTests(unittest.TestCase):
    def make_proc(self, root):
        proc = root / 'proc'
        (proc / 'net').mkdir(parents=True)
        (proc / 'net' / 'unix').write_text(
            'Num RefCount Protocol Flags Type St Inode Path\n')
        return proc

    def add_process(self, proc, pid, *, ppid=1, app_id=None,
                    comm='wineserver', exe=None, sockets=()):
        directory = proc / str(pid)
        (directory / 'fd').mkdir(parents=True)
        (directory / 'status').write_text(f'Name:\t{comm}\nPPid:\t{ppid}\n')
        (directory / 'comm').write_text(comm + '\n')
        environment = b'' if app_id is None else f'SteamAppId={app_id}\0'.encode()
        (directory / 'environ').write_bytes(environment)
        (directory / 'cmdline').write_bytes((comm + '\0').encode())
        (directory / 'stat').write_text(
            f'{pid} ({comm}) S ' + ' '.join(
                ['1'] * 18 + [str(pid + 1000), '0']) + '\n')
        if exe is not None:
            os.symlink(exe, directory / 'exe')
        for index, inode in enumerate(sockets):
            os.symlink(f'socket:[{inode}]', directory / 'fd' / str(index))

    def add_unix_socket(self, proc, inode, path):
        with (proc / 'net' / 'unix').open('a') as stream:
            stream.write(
                f'0: 00000002 00000000 00010000 0001 01 {inode} {path}\n')

    def add_flatpak_namespace(self, root, proc, pid, *,
                              app_id='com.valvesoftware.Steam',
                              instance_id='steam-instance', start_time='4242'):
        sandbox = root / 'sandbox'
        (sandbox / 'home' / 'player').mkdir(parents=True)
        extension = (sandbox / 'usr' / 'lib' / 'extensions' / 'vulkan' /
                     'gamescope')
        (extension / 'bin').mkdir(parents=True)
        (extension / 'lib').mkdir()
        cli = extension / 'bin' / 'gamescopectl'
        cli.write_text('#!/bin/sh\n')
        cli.chmod(0o755)
        (sandbox / '.flatpak-info').write_text(
            f'[Application]\nname={app_id}\n\n'
            f'[Instance]\ninstance-id={instance_id}\n'
            f'instance-path={root / "host-home" / ".var" / "app" / app_id}\n')
        os.symlink(sandbox, proc / str(pid) / 'root')
        # Field 22 follows 19 numeric fields after state.
        (proc / str(pid) / 'stat').write_text(
            f'{pid} (gamescope) S ' + ' '.join(
                ['1'] * 18 + [start_time, '0']) + '\n')
        (proc / str(pid) / 'environ').write_bytes(
            b'HOME=/home/player\0XDG_CACHE_HOME=/home/player/.cache\0')
        return sandbox

    def test_auto_discovery_uses_unique_named_helldivers_process(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = self.make_proc(Path(directory))
            self.add_process(proc, 100, app_id='553850', comm='wineserver')
            self.add_process(proc, 200, app_id='553850',
                             comm='helldivers2.exe',
                             exe='/games/helldivers2.exe')
            self.assertEqual(hm.helldivers_process_pid(proc_root=proc), 200)

    def test_launcher_argument_does_not_count_as_another_game(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = self.make_proc(Path(directory))
            self.add_process(proc, 100, app_id='553850', comm='proton')
            (proc / '100' / 'cmdline').write_bytes(
                b'proton\0waitforexitandrun\0/games/helldivers2.exe\0')
            self.add_process(proc, 200, app_id='553850', comm='helldivers2.exe')
            self.assertEqual(hm.helldivers_process_pid(proc_root=proc), 200)

    def test_proton_windows_executable_path_identifies_renamed_game(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = self.make_proc(Path(directory))
            self.add_process(proc, 100, app_id='553850', comm='steam.exe')
            (proc / '100' / 'cmdline').write_bytes(
                b'c:\\windows\\system32\\steam.exe\0'
                b'S:\\steamapps\\common\\Helldivers 2\\bin\\helldivers2.exe\0')
            self.add_process(proc, 200, app_id='553850', comm='main',
                             exe='/proton/files/lib/wine/x86_64-unix/wine-preloader')
            (proc / '200' / 'cmdline').write_bytes(
                b'S:\\steamapps\\common\\Helldivers 2\\bin\\helldivers2.exe\0'
                b'--bundle-dir\0')
            self.assertEqual(hm.helldivers_process_pid(proc_root=proc), 200)

    def test_steam_game_id_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = self.make_proc(Path(directory))
            self.add_process(proc, 200, comm='helldivers2.exe')
            (proc / '200' / 'environ').write_bytes(b'SteamGameId=553850\0')
            self.assertEqual(hm.helldivers_process_pid(proc_root=proc), 200)

    def test_app_id_must_be_an_exact_environment_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = self.make_proc(Path(directory))
            self.add_process(proc, 200, app_id='5538500', comm='helldivers2.exe')
            with self.assertRaisesRegex(hm.HostMetadataError, 'No Helldivers'):
                hm.helldivers_process_pid(proc_root=proc)

    def test_generic_tagged_process_is_not_treated_as_the_game(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = self.make_proc(Path(directory))
            self.add_process(proc, 200, app_id='553850', comm='wine64')
            (proc / '200' / 'comm').unlink()
            (proc / '200' / 'cmdline').unlink()
            with self.assertRaisesRegex(hm.HostMetadataError, 'No Helldivers'):
                hm.helldivers_process_pid(proc_root=proc)

    def test_multiple_named_games_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = self.make_proc(Path(directory))
            for pid in (200, 201):
                self.add_process(proc, pid, app_id='553850',
                                 comm='helldivers2.exe')
            with self.assertRaisesRegex(hm.HostMetadataError, 'Multiple Helldivers'):
                hm.helldivers_process_pid(proc_root=proc)

    def test_process_scan_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = self.make_proc(Path(directory))
            self.add_process(proc, 100, app_id='553850', comm='helldivers2.exe')
            self.add_process(proc, 101, app_id='553850', comm='helldivers2.exe')
            with patch.object(hm, 'MAX_PROC_ENTRIES', 1), \
                 self.assertRaisesRegex(hm.HostMetadataError, 'Too many host processes'):
                hm.helldivers_process_pid(proc_root=proc)

    def test_unix_socket_table_read_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = self.make_proc(root)
            self.add_process(proc, 100, ppid=50, app_id='553850',
                             comm='helldivers2.exe')
            self.add_process(proc, 50, sockets=('123',))
            (proc / 'net' / 'unix').write_bytes(b'x' * 9)
            with patch.object(hm, 'MAX_UNIX_SOCKET_BYTES', 8), \
                 self.assertRaisesRegex(hm.HostMetadataError, 'too large'):
                hm.gamescope_capture_target(
                    proc_root=proc, runtime_dir=root / 'run')

    def test_flatpak_target_proves_steam_identity_and_cache_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = self.make_proc(root)
            host_home = root / 'host-home'
            host_cache = (host_home / '.var' / 'app' /
                          'com.valvesoftware.Steam' / 'cache')
            host_cache.mkdir(parents=True)
            runtime = root / 'run-user'
            runtime.mkdir()
            gamescope = runtime / 'gamescope-7'
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(gamescope))
            try:
                self.add_process(proc, 200, ppid=50, app_id='553850',
                                 comm='helldivers2.exe')
                self.add_process(proc, 50, sockets=('123',))
                self.add_unix_socket(proc, '123', gamescope)
                sandbox = self.add_flatpak_namespace(root, proc, 50)
                os.symlink(host_cache, sandbox / 'home' / 'player' / '.cache')
                socket_alias = sandbox / gamescope.relative_to('/')
                socket_alias.parent.mkdir(parents=True)
                os.symlink(gamescope, socket_alias)

                target = hm.flatpak_gamescope_target(
                    proc_root=proc, runtime_dir=runtime, host_home=host_home)

                self.assertEqual(target.game_pid, 200)
                self.assertEqual(target.game_start_time, '1200')
                self.assertEqual(target.sandbox_pid, 50)
                self.assertEqual(target.sandbox_start_time, '4242')
                self.assertEqual(target.instance_id, 'steam-instance')
                self.assertEqual(target.socket, str(gamescope))
                self.assertEqual(target.host_cache, str(host_cache))
                self.assertEqual(target.sandbox_cache, '/home/player/.cache')
                self.assertEqual((target.cache_dev, target.cache_ino),
                                 (host_cache.stat().st_dev,
                                  host_cache.stat().st_ino))
                self.assertTrue(target.gamescopectl.endswith('/gamescopectl'))
            finally:
                listener.close()

    def test_flatpak_target_rejects_another_application_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = self.make_proc(root)
            runtime = root / 'run-user'
            runtime.mkdir()
            gamescope = runtime / 'gamescope-7'
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(gamescope))
            self.add_process(proc, 200, ppid=50, app_id='553850',
                             comm='helldivers2.exe')
            self.add_process(proc, 50, sockets=('123',))
            self.add_unix_socket(proc, '123', gamescope)
            sandbox = self.add_flatpak_namespace(
                root, proc, 50, app_id='com.example.NotSteam')
            socket_alias = sandbox / gamescope.relative_to('/')
            socket_alias.parent.mkdir(parents=True)
            os.symlink(gamescope, socket_alias)
            try:
                with self.assertRaisesRegex(hm.HostMetadataError,
                                            'Steam Flatpak'):
                    hm.flatpak_gamescope_target(
                        proc_root=proc, runtime_dir=runtime,
                        host_home=root / 'host-home')
            finally:
                listener.close()

    def test_flatpak_target_validation_rejects_pid_reuse(self):
        target = hm.FlatpakGamescopeTarget(
            game_pid=200, game_start_time='3131', sandbox_pid=50,
            sandbox_start_time='4242', instance_id='steam-instance',
            socket='/run/user/1000/gamescope-7', socket_dev=1, socket_ino=2,
            host_cache='/host/cache', sandbox_cache='/home/player/.cache',
            cache_dev=3, cache_ino=4,
            gamescopectl=hm.GAMESCOPECTL_PATH)
        with patch.object(hm, 'process_start_time', return_value='9999'), \
             self.assertRaisesRegex(hm.HostMetadataError, 'changed'):
            hm.validate_flatpak_gamescope_target(target)

    def test_capture_target_classifies_exact_native_socket_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = self.make_proc(root)
            runtime = root / 'run-user'
            runtime.mkdir()
            gamescope = runtime / 'gamescope-7'
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(gamescope))
            try:
                self.add_process(proc, 200, ppid=50, app_id='553850',
                                 comm='helldivers2.exe')
                self.add_process(proc, 50, sockets=('123',))
                self.add_unix_socket(proc, '123', gamescope)
                native_root = root / 'native-root'
                socket_alias = native_root / gamescope.relative_to('/')
                socket_alias.parent.mkdir(parents=True)
                os.symlink(gamescope, socket_alias)
                os.symlink(native_root, proc / '50' / 'root')

                target = hm.gamescope_capture_target(
                    proc_root=proc, runtime_dir=runtime)

                self.assertEqual(target, {
                    'kind': 'native', 'socket': str(gamescope)})
            finally:
                listener.close()

    def test_capture_target_does_not_fallback_from_nonsteam_flatpak(self):
        with patch.object(hm, 'helldivers_process_pid', return_value=200), \
             patch.object(hm, '_flatpak_socket_target',
                          return_value=(50, '/run/user/1000/gamescope-7', 1, 2)), \
             patch.object(hm, 'flatpak_gamescope_target',
                          side_effect=hm.HostMetadataError(
                              'Helldivers is not in the expected Steam Flatpak.')), \
             patch.object(hm, '_flatpak_info_presence', return_value=True), \
             self.assertRaisesRegex(hm.HostMetadataError, 'expected Steam'):
            hm.gamescope_capture_target()

    def test_flatpak_target_rejects_missing_extension_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance = root / 'instance'
            library = root / 'lib'
            library.mkdir()

            def sandbox_path(_proc, _pid, path):
                if str(path) == hm.GAMESCOPECTL_PATH:
                    return root / 'missing-gamescopectl'
                if str(path) == hm.GAMESCOPE_LIBRARY_PATH:
                    return library
                return root / 'cache-alias'

            with patch.object(hm, 'helldivers_process_pid', return_value=200), \
                 patch.object(hm, 'process_start_time',
                              side_effect=['1200', '1050']), \
                 patch.object(hm, '_flatpak_socket_target',
                              return_value=(50, '/run/user/1000/gamescope-7',
                                            1, 2)), \
                 patch.object(hm, '_flatpak_info',
                              return_value=('steam-instance', instance)), \
                 patch.object(hm, '_read_bounded', return_value=
                              b'HOME=/home/player\0XDG_CACHE_HOME=/home/player/.cache\0'), \
                 patch.object(hm, '_directory_identity',
                              side_effect=[(3, 4), (3, 4)]), \
                 patch.object(hm, '_sandbox_path', side_effect=sandbox_path), \
                 self.assertRaisesRegex(hm.HostMetadataError, 'gamescopectl'):
                hm.flatpak_gamescope_target()

    def test_capture_target_response_must_be_an_object(self):
        with patch('automatic_stratagems.scanner.game_capture.run_command',
                   return_value=b'[]'), \
             self.assertRaisesRegex(hm.HostMetadataError,
                                         'response is invalid'):
            hm.resolve_gamescope_capture_target()

    def test_metadata_cli_preserves_capture_target_route(self):
        target = {'kind': 'native', 'socket': '/run/user/1000/gamescope-2'}
        with patch.object(hm, 'gamescope_capture_target', return_value=target), \
             patch('builtins.print') as output:
            self.assertEqual(hm.main(['--capture-target']), 0)
        output.assert_called_once_with(
            '{"kind":"native","socket":"/run/user/1000/gamescope-2"}')


if __name__ == '__main__':
    unittest.main()
