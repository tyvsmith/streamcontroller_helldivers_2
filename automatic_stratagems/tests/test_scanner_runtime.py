import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from automatic_stratagems import runtime_profile, scanner_runtime


PROFILE = "gnome-50-x86_64-cpython-313"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class ScannerRuntimeTests(unittest.TestCase):
    def test_declared_dependency_pins_match_both_runtime_profiles(self):
        feature = Path(__file__).resolve().parents[1]
        manifest = json.loads((feature / 'runtime_profiles'
                               / 'gnome-50-x86_64-cpython-313.json').read_text())
        requirements = {}
        for raw in (feature / 'requirements.txt').read_text().splitlines():
            line = raw.partition('#')[0].strip()
            if not line:
                continue
            name, separator, version = line.partition('==')
            self.assertEqual(separator, '==')
            self.assertTrue(name and version)
            self.assertNotIn(name, requirements)
            requirements[name] = version

        def runtime_pins(profile):
            return {distribution: version for distribution, _, version in
                    scanner_runtime._PYTHON_PROFILES[profile]}

        self.assertEqual(requirements, manifest['native_python_packages'])
        self.assertEqual(requirements, runtime_pins('native'))
        self.assertEqual(manifest['python_packages'], runtime_pins(PROFILE))

    def test_runtime_profile_api_validates_activation_pointers(self):
        valid = {
            "profile": PROFILE,
            "directory": "profiles/gnome-50-x86_64-cpython-313-0123456789abcdef",
            "manifest_sha256": "a" * 64,
        }
        runtime_profile.validate_activation_pointer(valid, "activation record")

        invalid = dict(valid, directory="profiles/../outside")
        with self.assertRaisesRegex(
            runtime_profile.ScanSetupError, "activation record"
        ):
            runtime_profile.validate_activation_pointer(invalid, "activation record")

    def test_runtime_profile_api_is_dependency_light(self):
        source = (
            "import sys; import automatic_stratagems.runtime_profile; "
            "names=('numpy','cv2','PIL','evdev','dbus_next','gi'); "
            "print(','.join(name for name in names if name in sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=Path(__file__).resolve().parents[2],
            stdout=subprocess.PIPE,
            check=True,
        )
        self.assertEqual(result.stdout, b"\n")

    def test_native_runtime_uses_the_owned_venv_without_mutating_parent_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            interpreter = root / "automatic_stratagems/.venv/bin/python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")
            interpreter.chmod(0o755)
            parent = {"PATH": "/native/bin", "PYTHONPATH": "/developer/modules"}

            runtime = scanner_runtime.resolve_scanner_runtime(
                root, flatpak=False, environ=parent
            )

            self.assertEqual(runtime.interpreter, interpreter)
            self.assertEqual(runtime.profile, "native")
            self.assertIsNone(runtime.runtime_root)
            self.assertEqual(dict(runtime.env), parent)
            self.assertEqual(parent["PYTHONPATH"], "/developer/modules")

    def test_native_runtime_reports_the_owned_venv_when_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                scanner_runtime.ScanSetupError,
                r"automatic_stratagems/\.venv/bin/python.*"
                r"automatic_stratagems/requirements\.txt",
            ):
                scanner_runtime.resolve_scanner_runtime(
                    Path(directory), flatpak=False, environ={}
                )

    def test_flatpak_runtime_sanitizes_child_only_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_root = self.make_active_profile(root)
            parent = {
                "PATH": "/usr/bin:/app/bin",
                "PYTHONHOME": "/host/python",
                "PYTHONPATH": "/developer/modules",
                "LD_LIBRARY_PATH": "/host/lib",
                "KEEP": "yes",
            }

            with self.flatpak_profile(root):
                runtime = scanner_runtime.resolve_scanner_runtime(
                    root, flatpak=True, environ=parent
                )

            self.assertEqual(runtime.profile, PROFILE)
            self.assertEqual(runtime.runtime_root, profile_root)
            self.assertEqual(runtime.env["PATH"], f"{profile_root}/bin:/usr/bin:/app/bin")
            self.assertEqual(runtime.env["PYTHONPATH"], f"{profile_root}/python")
            self.assertEqual(runtime.env["LD_LIBRARY_PATH"], f"{profile_root}/lib")
            self.assertEqual(runtime.env["TESSDATA_PREFIX"], f"{profile_root}/tessdata")
            self.assertEqual(runtime.env["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(runtime.env["KEEP"], "yes")
            self.assertEqual(parent["PYTHONHOME"], "/host/python")
            self.assertNotIn("PYTHONHOME", runtime.env)

    def test_flatpak_runtime_rejects_branch_or_python_abi_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_active_profile(root)
            with self.flatpak_profile(root, runtime="org.gnome.Platform/x86_64/51"):
                with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "GNOME 50"):
                    scanner_runtime.resolve_scanner_runtime(root, flatpak=True, environ={})
            with self.flatpak_profile(root, abi="cpython-314"):
                with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "CPython 3.13"):
                    scanner_runtime.resolve_scanner_runtime(root, flatpak=True, environ={})

    def test_flatpak_runtime_rejects_corrupt_payload_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_root = self.make_active_profile(root)
            (profile_root / "bin/tesseract").write_bytes(b"corrupt")
            with self.flatpak_profile(root):
                with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "hash"):
                    scanner_runtime.resolve_scanner_runtime(root, flatpak=True, environ={})

    def test_flatpak_runtime_rejects_a_manifest_changed_after_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_root = self.make_active_profile(root)
            manifest = json.loads((profile_root / "profile.json").read_text())
            manifest["unexpected"] = True
            (profile_root / "profile.json").write_text(json.dumps(manifest))
            with self.flatpak_profile(root):
                with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "manifest hash"):
                    scanner_runtime.resolve_scanner_runtime(root, flatpak=True, environ={})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_root = self.make_active_profile(root)
            tesseract = profile_root / "bin/tesseract"
            tesseract.unlink()
            tesseract.symlink_to("elsewhere")
            with self.flatpak_profile(root):
                with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "symlink"):
                    scanner_runtime.resolve_scanner_runtime(root, flatpak=True, environ={})

    def test_flatpak_runtime_rejects_unbounded_or_traversing_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "automatic_stratagems/runtime"
            runtime.mkdir(parents=True)
            (runtime / "active.json").write_bytes(b"{" + b"x" * 5000)
            with self.flatpak_profile(root):
                with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "activation"):
                    scanner_runtime.resolve_scanner_runtime(root, flatpak=True, environ={})

            (runtime / "active.json").write_text(json.dumps({
                "schema_version": 1,
                "profile": PROFILE,
                "directory": "../../outside",
            }))
            with self.flatpak_profile(root):
                with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "activation"):
                    scanner_runtime.resolve_scanner_runtime(root, flatpak=True, environ={})

    def test_preflight_command_uses_the_resolved_interpreter_and_profile(self):
        runtime = scanner_runtime.ScannerRuntime(
            interpreter=Path("/usr/bin/python3"),
            env={"PATH": "/runtime/bin:/usr/bin"},
            profile=PROFILE,
            runtime_root=Path("/runtime/profile"),
        )
        command = scanner_runtime.scanner_preflight_command(Path("/plugin"), runtime)
        self.assertEqual(command[:3], (
            "/usr/bin/python3", "-m", "automatic_stratagems.scanner_runtime"
        ))
        self.assertEqual(command[-4:], (
            "--preflight", "--root", "/plugin", "--profile=" + PROFILE
        ))

    def test_parent_import_does_not_load_scanner_dependencies(self):
        source = (
            "import sys; import automatic_stratagems.scanner_runtime; "
            "import automatic_stratagems.scanner.screenshot_capture; "
            "names=('numpy','cv2','PIL','evdev','dbus_next','gi'); "
            "print(','.join(name for name in names if name in sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=Path(__file__).resolve().parents[2],
            stdout=subprocess.PIPE,
            check=True,
        )
        self.assertEqual(result.stdout, b"\n")

    def test_preflight_rejects_a_numerical_version_change(self):
        versions = {
            "numpy": "2.2.4",
            "opencv-python-headless": "4.11.0.86",
            "pillow": "11.1.0",
            "evdev": "1.9.1",
            "dbus-next": "0.2.3",
        }
        with patch("importlib.metadata.version", side_effect=versions.__getitem__):
            with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "numpy.*2.2.3"):
                scanner_runtime.verify_python_dependencies(PROFILE)

    def test_flatpak_preflight_uses_the_installed_opencv_distribution(self):
        versions = {
            "numpy": "2.2.3",
            "opencv-python": "4.11.0.86",
            "pillow": "11.1.0",
            "evdev": "1.9.1",
            "dbus-next": "0.2.3",
        }
        with patch("importlib.metadata.version", side_effect=versions.__getitem__), \
             patch("importlib.import_module"):
            result = scanner_runtime.verify_python_dependencies(PROFILE)
        self.assertEqual(result["opencv-python"], "4.11.0.86")

    def test_preflight_executes_tesseract_and_requires_exact_english_output(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "tesseract"
            executable.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  --version) echo 'tesseract 5.5.0' ;;\n"
                "  --list-langs) printf 'List of available languages (1):\\neng\\n' ;;\n"
                "  *) echo 'RESUPPLY' ;;\n"
                "esac\n"
            )
            executable.chmod(0o755)
            image = Path("automatic_stratagems/runtime_profiles/preflight.png")
            result = scanner_runtime.verify_tesseract(
                executable, image, expected_version="5.5.0"
            )
            self.assertEqual(result, {
                "version": "5.5.0", "languages": ["eng"], "ocr": "RESUPPLY"
            })

            executable.write_text(executable.read_text().replace("RESUPPLY", "RESUPR"))
            with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "OCR self-test"):
                scanner_runtime.verify_tesseract(
                    executable, image, expected_version="5.5.0"
                )

    def test_native_preflight_accepts_additional_host_languages_when_english_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "tesseract"
            executable.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  --version) echo 'tesseract 5.5.3' ;;\n"
                "  --list-langs) printf 'List of available languages (2):\\neng\\nosd\\n' ;;\n"
                "  *) echo 'RESUPPLY' ;;\n"
                "esac\n"
            )
            executable.chmod(0o755)
            result = scanner_runtime.verify_tesseract(
                executable,
                Path("automatic_stratagems/runtime_profiles/preflight.png"),
                expected_version=None,
                require_only_english=False,
            )
            self.assertEqual(result["languages"], ["eng", "osd"])

    def test_native_preflight_keeps_missing_optional_tesseract_available(self):
        with patch.object(
            scanner_runtime, "verify_python_dependencies", return_value={}
        ), patch.object(scanner_runtime.shutil, "which", return_value=None):
            result = scanner_runtime.child_preflight(Path("/plugin"), "native")
            self.assertIsNone(result["tesseract"])

            with self.assertRaisesRegex(
                scanner_runtime.ScanSetupError, "Tesseract is missing"
            ):
                scanner_runtime.child_preflight(Path("/plugin"), PROFILE)

    def test_native_preflight_tolerates_unusable_optional_tesseract(self):
        failures = (
            'Tesseract self-test failed: broken executable',
            'Tesseract requires English trained data; found []',
            'Tesseract OCR self-test expected RESUPPLY; found no text',
        )
        for message in failures:
            with self.subTest(message=message), \
                 patch.object(scanner_runtime, 'verify_python_dependencies',
                              return_value={}), \
                 patch.object(scanner_runtime.shutil, 'which',
                              return_value='/usr/bin/tesseract'), \
                 patch.object(scanner_runtime, 'verify_tesseract',
                              side_effect=scanner_runtime.ScanSetupError(message)):
                result = scanner_runtime.child_preflight(
                    Path('/plugin'), 'native')
            self.assertIsNone(result['tesseract'])

        with patch.object(scanner_runtime, 'verify_python_dependencies',
                          return_value={}), \
             patch.object(scanner_runtime.shutil, 'which',
                          return_value='/runtime/bin/tesseract'), \
             patch.object(
                 scanner_runtime, 'verify_tesseract',
                 side_effect=scanner_runtime.ScanSetupError(failures[0])):
            with self.assertRaisesRegex(scanner_runtime.ScanSetupError,
                                        'broken executable'):
                scanner_runtime.child_preflight(Path('/plugin'), PROFILE)

    def test_flatpak_runtime_wraps_unreadable_payload_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_root = self.make_active_profile(root)
            payload = profile_root / "bin/tesseract"
            payload.chmod(0)
            try:
                with self.flatpak_profile(root):
                    with self.assertRaisesRegex(
                        scanner_runtime.ScanSetupError, "cannot be read"
                    ):
                        scanner_runtime.resolve_scanner_runtime(
                            root, flatpak=True, environ={}
                        )
            finally:
                payload.chmod(0o755)

    def make_active_profile(self, root):
        runtime = root / "automatic_stratagems/runtime"
        profile_root = runtime / "profiles" / (PROFILE + "-0123456789abcdef")
        tesseract = profile_root / "bin/tesseract"
        tesseract.parent.mkdir(parents=True)
        tesseract.write_bytes(b"executable")
        tesseract.chmod(0o755)
        for directory in ("lib", "python", "tessdata"):
            (profile_root / directory).mkdir()
        manifest = {
            "schema_version": 1,
            "profile": PROFILE,
            "files": [{
                "path": "bin/tesseract",
                "sha256": sha256(b"executable"),
                "mode": "0755",
            }],
        }
        (profile_root / "profile.json").write_text(json.dumps(manifest))
        manifest_bytes = json.dumps(manifest).encode()
        (profile_root / "profile.json").write_bytes(manifest_bytes)
        (runtime / "active.json").write_text(json.dumps({
            "schema_version": 1,
            "profile": PROFILE,
            "directory": "profiles/" + profile_root.name,
            "manifest_sha256": sha256(manifest_bytes),
        }))
        return profile_root

    def flatpak_profile(self, root, *, runtime="org.gnome.Platform/x86_64/50",
                        abi="cpython-313"):
        info = Path(root) / "flatpak-info"
        info.write_text(
            f"[Application]\nruntime=runtime/{runtime}\n"
            "[Instance]\narch=x86_64\n"
        )
        return _FlatpakPatches(info, abi)


class RuntimeSetupTests(unittest.TestCase):
    def test_offline_setup_builds_a_hash_verified_profile_and_reuses_it(self):
        from automatic_stratagems import build

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            artifacts = Path(directory) / "artifacts"
            lock_path = self.make_sources_and_lock(artifacts, root)
            observed = []

            def preflight(profile_root, _manifest):
                observed.append(profile_root)
                self.assertEqual(
                    (profile_root / "tessdata/eng.traineddata").read_bytes(), b"english"
                )

            profile_root = build.install_runtime(
                root,
                lock_path=lock_path,
                artifact_dir=artifacts,
                offline=True,
                preflight=preflight,
            )
            activation = json.loads(
                (root / "automatic_stratagems/runtime/active.json").read_text()
            )
            self.assertEqual(activation["profile"], PROFILE)
            self.assertEqual(
                activation["directory"],
                "profiles/" + profile_root.name,
            )
            self.assertEqual(len(activation["manifest_sha256"]), 64)
            self.assertFalse(any(path.is_symlink() for path in profile_root.rglob("*")))

            shutil.rmtree(artifacts)
            reused = build.install_runtime(
                root,
                lock_path=lock_path,
                artifact_dir=artifacts,
                offline=True,
                preflight=preflight,
            )
            self.assertEqual(reused, profile_root)
            self.assertEqual(observed[0].parent.name, "staging")
            self.assertEqual(observed[1], profile_root)

    def test_failed_update_keeps_the_previous_activation_and_removes_stage(self):
        from automatic_stratagems import build

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            artifacts = Path(directory) / "artifacts"
            lock_path = self.make_sources_and_lock(artifacts, root)
            first = build.install_runtime(
                root, lock_path=lock_path, artifact_dir=artifacts,
                offline=True, preflight=lambda *_: None,
            )
            original_activation = (
                root / "automatic_stratagems/runtime/active.json"
            ).read_bytes()

            lock = json.loads(lock_path.read_text())
            lock["release"] = 2
            lock_path.write_text(json.dumps(lock))
            with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "child rejected"):
                build.install_runtime(
                    root, lock_path=lock_path, artifact_dir=artifacts,
                    offline=True,
                    preflight=lambda *_: (_ for _ in ()).throw(
                        scanner_runtime.ScanSetupError("child rejected")
                    ),
                )

            self.assertEqual(
                (root / "automatic_stratagems/runtime/active.json").read_bytes(),
                original_activation,
            )
            self.assertTrue(first.is_dir())
            staging = root / "automatic_stratagems/runtime/staging"
            self.assertEqual(list(staging.iterdir()), [])

    def test_preflight_cannot_mutate_the_immutable_payload_before_activation(self):
        from automatic_stratagems import build

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            artifacts = Path(directory) / "artifacts"
            lock_path = self.make_sources_and_lock(artifacts, root)

            def mutating_preflight(profile_root, _manifest):
                cache = profile_root / "python/dbus_next/__pycache__/module.pyc"
                cache.parent.mkdir()
                cache.write_bytes(b"bytecode")

            with self.assertRaisesRegex(
                scanner_runtime.ScanSetupError, "allowlist"
            ):
                build.install_runtime(
                    root, lock_path=lock_path, artifact_dir=artifacts,
                    offline=True, preflight=mutating_preflight,
                )
            self.assertFalse(
                (root / "automatic_stratagems/runtime/active.json").exists()
            )

    def test_rollback_atomically_selects_the_previous_verified_profile(self):
        from automatic_stratagems import build

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            artifacts = Path(directory) / "artifacts"
            lock_path = self.make_sources_and_lock(artifacts, root)
            first = build.install_runtime(
                root, lock_path=lock_path, artifact_dir=artifacts,
                offline=True, preflight=lambda *_: None,
            )
            lock = json.loads(lock_path.read_text())
            lock["release"] = 2
            lock_path.write_text(json.dumps(lock))
            second = build.install_runtime(
                root, lock_path=lock_path, artifact_dir=artifacts,
                offline=True, preflight=lambda *_: None,
            )
            self.assertNotEqual(first, second)

            selected = build.rollback_runtime(
                root, preflight=lambda *_: None
            )
            self.assertEqual(selected, first)
            activation = json.loads(
                (root / "automatic_stratagems/runtime/active.json").read_text()
            )
            self.assertEqual(activation["directory"], "profiles/" + first.name)
            self.assertEqual(
                activation["previous"]["directory"], "profiles/" + second.name
            )

    def test_offline_setup_rejects_wrong_source_hash_and_archive_symlink(self):
        from automatic_stratagems import build

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            artifacts = Path(directory) / "artifacts"
            lock_path = self.make_sources_and_lock(artifacts, root)
            source = artifacts / "tesseract.deb"
            source.write_bytes(source.read_bytes() + b"corrupt")
            with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "source hash"):
                build.install_runtime(
                    root, lock_path=lock_path, artifact_dir=artifacts,
                    offline=True, preflight=lambda *_: None,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            artifacts = Path(directory) / "artifacts"
            lock_path = self.make_sources_and_lock(
                artifacts, root, tesseract_symlink=True
            )
            with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "regular file"):
                build.install_runtime(
                    root, lock_path=lock_path, artifact_dir=artifacts,
                    offline=True, preflight=lambda *_: None,
                )

    def test_check_revalidates_the_active_profile_before_child_preflight(self):
        from automatic_stratagems import verify

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_root = ScannerRuntimeTests().make_active_profile(root)
            observed = []
            with _FlatpakPatches(
                self.write_flatpak_info(root), "cpython-313"
            ):
                selected = verify.check_runtime(
                    root,
                    flatpak=True,
                    environ={"PATH": "/usr/bin"},
                    preflight=lambda path, manifest: observed.append(
                        (path, manifest["profile"])
                    ),
                )
            self.assertEqual(selected, profile_root)
            self.assertEqual(observed, [(profile_root, PROFILE)])

    def test_check_rejects_a_profile_mutated_by_child_preflight(self):
        from automatic_stratagems import verify

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_root = ScannerRuntimeTests().make_active_profile(root)

            def mutating_preflight(path, _manifest):
                (path / "bin/tesseract").write_bytes(b"replaced")

            with _FlatpakPatches(
                self.write_flatpak_info(root), "cpython-313"
            ):
                with self.assertRaisesRegex(
                    scanner_runtime.ScanSetupError, "child preflight"
                ):
                    verify.check_runtime(
                        root,
                        flatpak=True,
                        environ={"PATH": "/usr/bin"},
                        preflight=mutating_preflight,
                    )

    def test_update_rejects_a_corrupt_existing_activation(self):
        from automatic_stratagems import build

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            artifacts = Path(directory) / "artifacts"
            lock_path = self.make_sources_and_lock(artifacts, root)
            build.install_runtime(
                root, lock_path=lock_path, artifact_dir=artifacts,
                offline=True, preflight=lambda *_: None,
            )
            activation = root / "automatic_stratagems/runtime/active.json"
            value = json.loads(activation.read_text())
            value["directory"] = "../../outside"
            activation.write_text(json.dumps(value))

            with self.assertRaisesRegex(scanner_runtime.ScanSetupError, "activation"):
                build.install_runtime(
                    root, lock_path=lock_path, artifact_dir=artifacts,
                    offline=True, preflight=lambda *_: None,
                )

    def test_atomic_activation_handles_short_writes_and_cleans_failed_temps(self):
        from automatic_stratagems import runtime_profile
        from automatic_stratagems.shared import fs

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "active.json"
            value = {"schema_version": 1, "profile": PROFILE, "directory": "x"}
            real_write = fs.os.write

            def short_write(descriptor, data):
                return real_write(descriptor, data[:5])

            with patch.object(fs.os, "write", side_effect=short_write) as write:
                runtime_profile.atomic_json(path, value)
            self.assertGreater(write.call_count, 1)
            self.assertEqual(json.loads(path.read_text()), value)

            def failed_write(descriptor, data):
                real_write(descriptor, data[:5])
                raise OSError("disk full")

            with patch.object(fs.os, "write", side_effect=failed_write):
                with self.assertRaisesRegex(OSError, "disk full"):
                    runtime_profile.atomic_json(path, dict(value, directory="y"))
            self.assertEqual(json.loads(path.read_text()), value)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["active.json"])

    def test_rollback_reuses_the_bounded_validated_manifest(self):
        from automatic_stratagems import build

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            artifacts = Path(directory) / "artifacts"
            lock_path = self.make_sources_and_lock(artifacts, root)
            build.install_runtime(
                root, lock_path=lock_path, artifact_dir=artifacts,
                offline=True, preflight=lambda *_: None,
            )
            lock = json.loads(lock_path.read_text())
            lock["release"] = 2
            lock_path.write_text(json.dumps(lock))
            build.install_runtime(
                root, lock_path=lock_path, artifact_dir=artifacts,
                offline=True, preflight=lambda *_: None,
            )

            original = Path.read_bytes

            def reject_unbounded_manifest(path):
                if path.name == "profile.json":
                    raise AssertionError("unbounded profile read")
                return original(path)

            with patch.object(Path, "read_bytes", reject_unbounded_manifest):
                build.rollback_runtime(root, preflight=lambda *_: None)

    def test_rollback_rejects_a_profile_mutated_by_child_preflight(self):
        from automatic_stratagems import build

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            artifacts = Path(directory) / "artifacts"
            lock_path = self.make_sources_and_lock(artifacts, root)
            build.install_runtime(
                root, lock_path=lock_path, artifact_dir=artifacts,
                offline=True, preflight=lambda *_: None,
            )
            lock = json.loads(lock_path.read_text())
            lock["release"] = 2
            lock_path.write_text(json.dumps(lock))
            build.install_runtime(
                root, lock_path=lock_path, artifact_dir=artifacts,
                offline=True, preflight=lambda *_: None,
            )
            activation_path = root / "automatic_stratagems/runtime/active.json"
            original_activation = activation_path.read_bytes()

            def mutating_preflight(profile_root, _manifest):
                (profile_root / "bin/tesseract").write_bytes(b"replaced")

            with self.assertRaisesRegex(
                scanner_runtime.ScanSetupError, "child preflight"
            ):
                build.rollback_runtime(root, preflight=mutating_preflight)
            self.assertEqual(activation_path.read_bytes(), original_activation)

    def make_sources_and_lock(self, artifacts, root, *, tesseract_symlink=False):
        artifacts.mkdir(parents=True)
        (root / "automatic_stratagems").mkdir(parents=True)
        members = {
            "tesseract.deb": {
                "usr/bin/tesseract": (b"#!/bin/sh\necho RESUPPLY\n", 0o755,
                                      "symlink" if tesseract_symlink else "file"),
                "usr/share/doc/tesseract/copyright": (b"Apache\n", 0o644, "file"),
            },
            "libtesseract.deb": {
                "usr/lib/libtesseract.so.5.0.5": (b"libtesseract", 0o644, "file"),
                "usr/share/tessdata/configs/tsv": (b"tsv", 0o644, "file"),
            },
            "leptonica.deb": {
                "usr/lib/libleptonica.so.6.0.0": (b"leptonica", 0o644, "file"),
                "usr/share/doc/leptonica/copyright": (b"BSD\n", 0o644, "file"),
            },
            "english.deb": {
                "usr/share/tessdata/eng.traineddata": (b"english", 0o644, "file"),
                "usr/share/doc/english/copyright": (b"Apache data\n", 0o644, "file"),
            },
        }
        for name, archive_members in members.items():
            self.write_deb(artifacts / name, archive_members)
        wheel_members = {
            "dbus_next/__init__.py": b"__version__ = '0.2.3'\n",
            "dbus_next-0.2.3.dist-info/LICENSE": b"MIT\n",
        }
        with zipfile.ZipFile(artifacts / "dbus.whl", "w") as archive:
            for name, data in wheel_members.items():
                info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
                # Wheel builders may store permissions without POSIX file-type bits.
                info.external_attr = 0o644 << 16
                archive.writestr(info, data)

        source_specs = []
        for name in [*members, "dbus.whl"]:
            data = (artifacts / name).read_bytes()
            source_specs.append({
                "name": name.removesuffix(".deb").removesuffix(".whl"),
                "filename": name,
                "kind": "wheel" if name.endswith(".whl") else "deb",
                "url": "https://example.invalid/" + name,
                "bytes": len(data),
                "sha256": sha256(data),
            })

        files = []
        outputs = [
            ("tesseract", "usr/bin/tesseract", "bin/tesseract", members["tesseract.deb"]["usr/bin/tesseract"][0], "0755"),
            ("tesseract", "usr/share/doc/tesseract/copyright", "licenses/Tesseract.txt", b"Apache\n", "0644"),
            ("libtesseract", "usr/lib/libtesseract.so.5.0.5", "lib/libtesseract.so.5", b"libtesseract", "0644"),
            ("libtesseract", "usr/share/tessdata/configs/tsv", "tessdata/configs/tsv", b"tsv", "0644"),
            ("leptonica", "usr/lib/libleptonica.so.6.0.0", "lib/libleptonica.so.6", b"leptonica", "0644"),
            ("leptonica", "usr/share/doc/leptonica/copyright", "licenses/Leptonica.txt", b"BSD\n", "0644"),
            ("english", "usr/share/tessdata/eng.traineddata", "tessdata/eng.traineddata", b"english", "0644"),
            ("english", "usr/share/doc/english/copyright", "licenses/tessdata-fast.txt", b"Apache data\n", "0644"),
            ("dbus", "dbus_next/__init__.py", "python/dbus_next/__init__.py", wheel_members["dbus_next/__init__.py"], "0644"),
            ("dbus", "dbus_next-0.2.3.dist-info/LICENSE", "python/dbus_next-0.2.3.dist-info/LICENSE", b"MIT\n", "0644"),
        ]
        for source, member, path, data, mode in outputs:
            files.append({"source": source, "member": member, "path": path,
                          "bytes": len(data), "sha256": sha256(data), "mode": mode})
        lock = {
            "schema_version": 1,
            "profile": PROFILE,
            "release": 1,
            "compatibility": {
                "flatpak_runtime": "org.gnome.Platform/x86_64/50",
                "python_abi": "cpython-313",
            },
            "sources": source_specs,
            "files": files,
            "licenses": ["Apache-2.0", "BSD-2-Clause", "MIT"],
        }
        lock_path = root / "automatic_stratagems/runtime_profiles/test-lock.json"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(lock))
        return lock_path

    def write_flatpak_info(self, root):
        info = Path(root) / "flatpak-info"
        info.write_text(
            "[Application]\nruntime=runtime/org.gnome.Platform/x86_64/50\n"
            "[Instance]\narch=x86_64\n"
        )
        return info

    def write_deb(self, path, members):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:xz") as archive:
            for name, (data, mode, kind) in members.items():
                info = tarfile.TarInfo("./" + name)
                info.mode = mode
                if kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/tmp/outside"
                    info.size = 0
                    archive.addfile(info)
                else:
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
        data = payload.getvalue()
        with path.open("wb") as stream:
            stream.write(b"!<arch>\n")
            for name, value in (("debian-binary", b"2.0\n"), ("data.tar.xz", data)):
                header = (
                    f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{0o100644:<8o}"
                    f"{len(value):<10}`\n"
                ).encode("ascii")
                stream.write(header)
                stream.write(value)
                if len(value) % 2:
                    stream.write(b"\n")


class _FlatpakPatches:
    def __init__(self, info, abi):
        self.info = patch.object(runtime_profile, "FLATPAK_INFO", info)
        self.abi = patch.object(runtime_profile, "_python_abi", return_value=abi)

    def __enter__(self):
        self.info.start()
        self.abi.start()
        return self

    def __exit__(self, *args):
        self.abi.stop()
        self.info.stop()
