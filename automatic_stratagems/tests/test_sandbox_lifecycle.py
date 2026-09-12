import json
import ast
import os
from pathlib import Path
from queue import Queue
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from automatic_stratagems.tests import sandbox_support
from automatic_stratagems.tests.action_test_support import ActionTestHarness


class SandboxSupportTests(unittest.TestCase):
    def test_process_audit_requires_executed_packaged_tesseract(self):
        tool = runpy.run_path(str(
            Path(__file__).resolve().parents[1] / "tools/check-sandbox"))
        validate = tool["_validate_tesseract_observations"]
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory) / "net_jslay_helldivers_2"
            executable = (installed / "automatic_stratagems/runtime/profiles/p1"
                          / "bin/tesseract")
            observed = [{
                "argv": ["tesseract", "stdin", "stdout"],
                "executable": str(executable),
                "mount_namespace": "mnt:[scanner]",
            }]
            validated = validate(observed, installed, {"mnt:[scanner]"})
            self.assertEqual(validated[0]["executable"], str(executable))
            with self.assertRaisesRegex(RuntimeError, "Tesseract execution"):
                validate([], installed, {"mnt:[scanner]"})
            with self.assertRaisesRegex(RuntimeError, "Tesseract execution"):
                validate([{**observed[0], "mount_namespace": "mnt:[host]"}],
                         installed, {"mnt:[scanner]"})

    def test_outer_timeout_retains_output_and_process_metrics(self):
        tool = runpy.run_path(str(
            Path(__file__).resolve().parents[1] / "tools/check-sandbox"))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "timeout.stdout"
            command = [
                sys.executable, "-c",
                "import subprocess,sys,time; "
                "print('timeout stdout',flush=True); "
                "print('timeout stderr',file=sys.stderr,flush=True); "
                "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
                "time.sleep(30)",
            ]
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                tool["_run"](
                    command, environment=os.environ.copy(), output=output,
                    timeout=.1)
            self.assertEqual(output.read_text(), "timeout stdout\n")
            self.assertEqual(
                output.with_suffix(".stdout.stderr").read_text(),
                "timeout stderr\n")
            metrics = json.loads(
                output.with_suffix(".stdout.metrics.json").read_text())
            self.assertTrue(metrics["timed_out"])
            self.assertGreaterEqual(metrics["peak_descendant_members"], 2)
            for process in metrics["observed_processes"]:
                try:
                    start_time = (Path(f"/proc/{process['pid']}/stat").read_text()
                                  .rsplit(")", 1)[1].split()[19])
                except FileNotFoundError:
                    continue
                self.assertNotEqual(start_time, process["start_time"])

    def test_host_support_module_has_only_stdlib_top_level_imports(self):
        tree = ast.parse(Path(sandbox_support.__file__).read_text())
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".", 1)[0])
        self.assertLessEqual(imported, {
            "argparse", "hashlib", "json", "os", "pathlib", "re", "resource",
            "signal", "shutil", "subprocess", "sys", "threading", "time",
        })

    def test_host_fixture_script_preserves_image_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / 'fixture.png'
            fixture.write_bytes(b'fixture-png')
            output = root / 'output.png'
            audit = root / 'audit.jsonl'
            result = subprocess.run([
                sys.executable, str(Path(sandbox_support.__file__)),
                'host-cli', 'screenshot', str(output)],
                env={**os.environ, sandbox_support.FIXTURE_ENV: str(fixture),
                     sandbox_support.AUDIT_ENV: str(audit)},
                capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(output.read_bytes(), fixture.read_bytes())
            record = json.loads(audit.read_text().splitlines()[0])
            self.assertEqual(record['argv'], ['screenshot', str(output)])

    def test_measure_retains_child_output_and_sampled_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "sample"
            result = subprocess.run([
                sys.executable, str(Path(sandbox_support.__file__)), "measure",
                "--output-prefix", str(prefix), "--", sys.executable, "-c",
                "import time; print('measured'); time.sleep(.03)",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(prefix.with_suffix(".stdout").read_text(), "measured\n")
            metrics = json.loads(prefix.with_suffix(".metrics.json").read_text())
            self.assertEqual(metrics["returncode"], 0)
            self.assertEqual(metrics["sampler"], "sum /proc PPID-tree RSS at 10 ms")
            self.assertGreater(metrics["peak_descendant_rss_bytes"], 0)


@unittest.skipUnless(
    os.environ.get("HD2_SANDBOX_LIFECYCLE") == "1",
    "run automatic_stratagems/tools/check-sandbox for real Flatpak lifecycle coverage",
)
class SandboxLifecycleTests(ActionTestHarness, unittest.TestCase):
    def setUp(self):
        from automatic_stratagems.tests.test_temporary_scan_page import Deck, PageManager

        super().setUp()

        class CountingLock:
            def __init__(self):
                self.lock = threading.Lock()
                self.releases = 0

            def acquire(self, *args, **kwargs):
                return self.lock.acquire(*args, **kwargs)

            def release(self):
                self.releases += 1
                self.lock.release()

            def locked(self):
                return self.lock.locked()

        temporary = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[2].parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'captures').mkdir()
        script = self.root / 'fixture-trigger'
        script.write_text('#!/bin/sh\nexit 0\n')
        script.chmod(0o700)
        scan_runner_globals = self.mod.scan_lifecycle.run_scan.__globals__
        self.plugin.get_settings = lambda: {
            'automatic_stratagems_enabled': True,
            'screenshot_trigger': 'script', 'screenshot_script': str(script),
            'screenshot_folder': str(self.root / 'captures'),
            'screenshot_delete': True,
        }
        self.plugin.PATH = str(Path(__file__).resolve().parents[2])
        self.plugin.stratagems = json.loads(
            (Path(self.plugin.PATH) / "assets/data/stratagems.json").read_text())
        self.plugin.input_lock = CountingLock()
        self.source = self.root / "source.json"
        self.source.write_text('{"keys": {}}')
        self.deck = Deck(self.source)
        self.page = self.deck.active_page
        self.coordinator = self.mod.ScanCoordinator(
            self.plugin, state_dir=self.root / "scan-state")
        self.coordinator.enable_temporary_pages(PageManager())
        load_page = self.deck.load_page

        def switch(page):
            old_path = self.deck.active_page.json_path
            load_page(page)
            self.coordinator.page_changed(self.deck, old_path, page.json_path)

        self.deck.load_page = switch
        self.opener = self.rendering_action(
            self.mod.AutomaticStratagemPage,
            {"group": "HD2", "capture_backend": "screenshot"},
        )
        self.opener.show_error = Mock()
        self.opener.input_ident = types.SimpleNamespace(
            input_type="keys", json_identifier="4x0")
        self.opener.state = 0
        self.opener.on_ready_called = True
        self.opener.get_own_action_index = lambda: 0
        self.opener.get_is_present = (
            lambda: self.deck.active_page.json_path == self.opener.page.json_path)
        self.opener.page.action_objects = {
            "keys": {"4x0": {0: {0: self.opener}}}}
        self.coordinator.actions.add(self.opener)
        self.queued = Queue()
        self.threads = []
        real_thread = threading.Thread
        real_scan_command = scan_runner_globals["scan_command"]

        def thread(**kwargs):
            worker = real_thread(**kwargs)
            self.threads.append(worker)
            return worker

        def scanner_command(*args, **kwargs):
            command = real_scan_command(*args, **kwargs)
            index = command.index("-m")
            self.assertEqual(command[index + 1], "automatic_stratagems.scanner")
            return [
                command[0], str(Path(sandbox_support.__file__).resolve()),
                "scanner-wrapper", *command[index + 2:],
            ]

        for replacement in (
                patch.object(self.mod, "Thread", side_effect=thread),
                patch.object(self.mod.scan_lifecycle.GLib, "idle_add", side_effect=lambda callback, *args:
                             self.queued.put((callback, args)) or 1),
                patch.dict(scan_runner_globals, {
                    "scan_command": Mock(side_effect=scanner_command)})):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.addCleanup(self._cleanup)
        self.expected = json.loads(os.environ[sandbox_support.EXPECTED_ENV])
        self.production_jobs_before = self._production_jobs()

    def _production_jobs(self):
        from automatic_stratagems import host_commands

        root = host_commands._cache_root()
        return {path.name for path in root.iterdir()} if root.exists() else set()

    def _cleanup(self):
        self.coordinator.cancel_all()
        self.coordinator.shutdown(timeout=20)
        for thread in self.threads:
            thread.join(20)

    def _drain_until_released(self, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while not self.queued.empty():
                callback, args = self.queued.get_nowait()
                callback(*args)
            if not self.plugin.input_lock.locked():
                break
            time.sleep(.01)
        self.assertFalse(self.plugin.input_lock.locked(), "scanner input lock was not released")
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "scanner worker was not reaped")
        while not self.queued.empty():
            callback, args = self.queued.get_nowait()
            callback(*args)
        self.assertEqual(self.plugin.input_lock.releases, 1)
        self.assertEqual(self._production_jobs(), self.production_jobs_before)

    def host_environment(self, scenario):
        return {
            sandbox_support.AUDIT_ENV: os.environ[sandbox_support.AUDIT_ENV],
            sandbox_support.GEOMETRY_ENV: os.environ[sandbox_support.GEOMETRY_ENV],
            sandbox_support.SCENARIO_ENV: scenario,
            sandbox_support.HOST_STARTED_ENV:
                os.environ[sandbox_support.HOST_STARTED_ENV],
        }

    def run_host_probe(self, scenario, *, timeout=5, stdout_limit=64 * 1024,
                       stderr_limit=64 * 1024, cancel_event=None, arguments=()):
        from automatic_stratagems import host_commands
        from automatic_stratagems.scanner import game_capture

        command = [
            "/usr/bin/python3", str(Path(sandbox_support.__file__).resolve()),
            "host-cli", "probe", *arguments,
        ]
        base = Path(os.environ[sandbox_support.AUDIT_ENV]).parent / "host-jobs"
        with host_commands.create_host_job(base=base, hard_timeout=10) as job:
            job_path = job.path
            with patch.dict(os.environ, host_commands.host_job_environment(job)):
                result = game_capture.run_command(
                    command, host=True, operation=f"test-{scenario}",
                    host_env=self.host_environment(scenario), timeout=timeout,
                    stdout_limit=stdout_limit, stderr_limit=stderr_limit,
                    cancel_event=cancel_event,
                )
        self.assertFalse(job_path.exists(), "host job was retained after confirmation")
        return result

    def test_03_real_host_transport_preserves_argv_and_failure_boundaries(self):
        from automatic_stratagems.scanner.game_capture import ScanError

        arguments = ("", "space value", "$(touch /tmp/never)", ";", "*", "quote'\"")
        output = self.run_host_probe("complete", arguments=arguments)
        self.assertEqual(json.loads(output), ["probe", *arguments])
        for scenario, pattern, limits in (
                ("nonzero", "deliberate probe failure", {}),
                ("stdout-overflow", "stdout exceeded", {"stdout_limit": 1024}),
                ("stderr-overflow", "stderr exceeded", {"stderr_limit": 1024}),
                ("timeout", "timed out", {"timeout": .2})):
            with self.subTest(scenario=scenario), self.assertRaisesRegex(ScanError, pattern):
                self.run_host_probe(scenario, **limits)

    def test_04_real_host_transport_cancel_waits_for_group_cleanup(self):
        from concurrent.futures import CancelledError

        started = Path(os.environ[sandbox_support.HOST_STARTED_ENV])
        started.unlink(missing_ok=True)
        cancel = threading.Event()

        def cancel_after_start():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not started.exists():
                time.sleep(.01)
            cancel.set()

        thread = threading.Thread(target=cancel_after_start)
        thread.start()
        with self.assertRaises(CancelledError):
            self.run_host_probe("cancel", cancel_event=cancel)
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_01_scanner_report_reaches_page_and_releases_input_lock(self):
        with patch.dict(os.environ, {
                sandbox_support.SCENARIO_ENV: "complete"}):
            self.coordinator.start(self.opener)
            self.assertTrue(self.plugin.input_lock.locked())
            self._drain_until_released()
        generated = Path(self.deck.active_page.json_path)
        self.assertNotEqual(
            generated, self.source,
            f"scan attempt: {getattr(self.opener, '_scan_attempt', None)!r}")
        self.assertTrue(generated.is_file())
        saved = self.coordinator.store.load({
            "deck": self.deck.serial_number(), "page": str(generated),
            "group": "HD2",
        }, self.plugin.stratagems)
        report = saved.latest_report()
        actual = [row.get("id") for row in report["rows"]]
        for index, expected in enumerate(self.expected):
            if expected is not None:
                self.assertEqual(actual[index], expected)
        self.assertTrue(any(
            expected is None and actual[index] is not None
            for index, expected in enumerate(self.expected)
        ), "fixture did not exercise a mission fallback")
        self.assertEqual(
            [value for value in saved.snapshot().assignments.values() if value], actual)
        self.assertEqual(json.loads(self.source.read_text()), {"keys": {}})
        self.assertFalse((self.root / 'captures' / 'frame.png').exists(),
                         'shared default deletion did not remove the fixture capture')

    def test_02_cancel_holds_input_lock_until_host_cleanup(self):
        started = Path(os.environ[sandbox_support.HOST_STARTED_ENV])
        started.unlink(missing_ok=True)
        with patch.dict(os.environ, {sandbox_support.SCENARIO_ENV: "cancel"}):
            self.coordinator.start(self.opener)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not started.exists():
                time.sleep(.01)
            self.assertTrue(
                started.exists(),
                f"host capture did not start; scan attempt: "
                f"{getattr(self.opener, '_scan_attempt', None)!r}",
            )
            self.coordinator.cancel_context(self.coordinator.context(self.opener))
            self.assertTrue(
                self.plugin.input_lock.locked(),
                "input lock released before host cleanup completed",
            )
            self.assertEqual(self.plugin.input_lock.releases, 0)
            self._drain_until_released()
        self.assertEqual(self.deck.active_page.json_path, str(self.source))
        self.assertEqual(list((self.root / "temporary-pages").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
