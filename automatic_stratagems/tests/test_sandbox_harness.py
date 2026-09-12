import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from automatic_stratagems.tests import sandbox_support


TOOL = Path(__file__).resolve().parents[1] / "tools/check-sandbox"
REQUIRED = (
    "automatic_stratagems/tools/check-sandbox",
    "automatic_stratagems/tests/sandbox_support.py",
    "automatic_stratagems/tests/test_sandbox_lifecycle.py",
)


def git(root, *arguments):
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout


@unittest.skipUnless(shutil.which("git"), "host source-staging tests require Git")
class SandboxStagingTests(unittest.TestCase):
    def repository(self, root, *, track_harness=True):
        git(root, "init", "-q")
        git(root, "config", "user.email", "test@example.invalid")
        git(root, "config", "user.name", "Test")
        tracked = root / "automatic_stratagems/scanner.py"
        tracked.parent.mkdir(parents=True)
        tracked.write_text("committed\n")
        if track_harness:
            for relative in REQUIRED:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative + "\n")
        git(root, "add", ".")
        git(root, "commit", "-qm", "fixture")
        return tracked

    def test_stage_source_rejects_dirty_tracked_bytes(self):
        tool = runpy.run_path(str(TOOL))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            tracked = self.repository(source)
            tracked.write_text("dirty\n")
            destination = root / "net_jslay_helldivers_2"

            with self.assertRaisesRegex(ValueError, "tracked changes"):
                tool["_stage_source"](source, destination)

            self.assertFalse(destination.exists())

    def test_stage_source_requires_harness_files_in_commit(self):
        tool = runpy.run_path(str(TOOL))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            self.repository(source, track_harness=False)
            for relative in REQUIRED:
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("untracked replacement\n")

            with self.assertRaisesRegex(ValueError, "not tracked"):
                tool["_stage_source"](
                    source, root / "net_jslay_helldivers_2")

    def test_stage_source_uses_and_returns_one_recorded_commit(self):
        tool = runpy.run_path(str(TOOL))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            self.repository(source)
            commit = git(source, "rev-parse", "HEAD").decode().strip()
            destination = root / "net_jslay_helldivers_2"

            manifest, recorded = tool["_stage_source"](source, destination)

            self.assertEqual(recorded, commit)
            for entry in manifest:
                committed = git(source, "show", f"{commit}:{entry['path']}")
                self.assertEqual(
                    (destination / entry["path"]).read_bytes(), committed)


class SandboxOutputTests(unittest.TestCase):
    @staticmethod
    def assert_dead(pid):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            except FileNotFoundError:
                return
            if fields[0] == "Z":
                return
            time.sleep(.01)
        raise AssertionError(f"process remains live: {pid}")

    @staticmethod
    def noisy_command(pid_file):
        script = (
            "import os,pathlib,subprocess,sys,time;"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid));"
            "chunk=b'x'*8192;"
            "[(os.write(1,chunk),os.write(2,chunk)) for _ in range(256)];"
            "time.sleep(30)"
        )
        return [sys.executable, "-c", script, str(pid_file)]

    def test_outer_runner_bounds_noisy_output_and_reaps_group(self):
        tool = runpy.run_path(str(TOOL))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "noisy.stdout"
            child_pid = root / "child.pid"

            with self.assertRaisesRegex(RuntimeError, "output exceeded"):
                tool["_run"](
                    self.noisy_command(child_pid), environment=os.environ.copy(),
                    output=output, timeout=5, output_limit=4096)

            self.assertLessEqual(output.stat().st_size, 4096)
            self.assertLessEqual(
                output.with_suffix(".stdout.stderr").stat().st_size, 4096)
            metrics = json.loads(
                output.with_suffix(".stdout.metrics.json").read_text())
            self.assertTrue(metrics["output_overflow"])
            self.assertTrue(metrics["process_group_cleanup_confirmed"])
            self.assert_dead(int(child_pid.read_text()))

    def test_outer_runner_detects_small_overflow_before_timeout(self):
        tool = runpy.run_path(str(TOOL))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "small-overflow.stdout"
            command = [
                sys.executable, "-c",
                "import os,time;os.write(1,b'x'*8192);time.sleep(30)",
            ]

            with self.assertRaisesRegex(RuntimeError, "output exceeded"):
                tool["_run"](
                    command, environment=os.environ.copy(), output=output,
                    timeout=.2, output_limit=4096)

            metrics = json.loads(
                output.with_suffix(".stdout.metrics.json").read_text())
            self.assertTrue(metrics["stdout_overflow"])
            self.assertFalse(metrics["timed_out"])
            self.assertTrue(metrics["process_group_cleanup_confirmed"])

    def test_measure_bounds_noisy_output_and_retains_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "measure"
            child_pid = root / "child.pid"
            result = subprocess.run([
                sys.executable, str(Path(sandbox_support.__file__)), "measure",
                "--output-prefix", str(prefix), "--output-limit", "4096",
                "--timeout", "5", "--", *self.noisy_command(child_pid),
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

            self.assertEqual(result.returncode, 125, result.stderr.decode())
            self.assertLessEqual(prefix.with_suffix(".stdout").stat().st_size, 4096)
            self.assertLessEqual(prefix.with_suffix(".stderr").stat().st_size, 4096)
            metrics = json.loads(prefix.with_suffix(".metrics.json").read_text())
            self.assertTrue(metrics["output_overflow"])
            self.assertTrue(metrics["process_group_cleanup_confirmed"])
            self.assert_dead(int(child_pid.read_text()))

    def test_outer_runner_reaps_and_rejects_lingering_success_descendant(self):
        tool = runpy.run_path(str(TOOL))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "linger.stdout"
            child_pid = root / "child.pid"
            script = (
                "import pathlib,subprocess,sys;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "left processes"):
                    tool["_run"](
                        [sys.executable, "-c", script, str(child_pid)],
                        environment=os.environ.copy(), output=output, timeout=5)
            finally:
                if child_pid.exists():
                    try:
                        os.kill(int(child_pid.read_text()), 9)
                    except ProcessLookupError:
                        pass

            metrics = json.loads(
                output.with_suffix(".stdout.metrics.json").read_text())
            self.assertTrue(metrics["lingering_process_group"])
            self.assertTrue(metrics["process_group_cleanup_confirmed"])
            self.assert_dead(int(child_pid.read_text()))


if __name__ == "__main__":
    unittest.main()
