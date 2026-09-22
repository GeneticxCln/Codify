from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from engine.fs import FileSystemService
from engine.sandbox import CommandNotAllowed, SandboxService, validate_argv


class TestSandboxService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.fs = FileSystemService(str(self.root))
        self.sandbox = SandboxService()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_timeout_kills_whole_process_group(self):
        # A timed-out command that spawned grandchildren must leave no strays:
        # the kill targets the process group, not just the direct child.
        (self.root / "spawner.py").write_text(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        started = time.monotonic()
        result = self.sandbox.run_command(str(self.root), ["python3", "spawner.py"], timeout_s=2)
        elapsed = time.monotonic() - started
        self.assertIn("timed out after 2s", result["stderr"])
        # The group kill lets the pipes EOF immediately; killing only the direct
        # child leaves the grandchild holding the write ends, so run_command
        # blocks until the grandchild exits (~60s here) and the 2s timeout lies.
        self.assertLess(elapsed, 20, f"run_command blocked {elapsed:.1f}s past its 2s timeout")
        time.sleep(0.5)
        survivors = subprocess.run(
            ["pgrep", "-f", r"time\.sleep\(60\)"], capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(survivors, "", f"stray grandchildren survived: {survivors}")

    def test_empty_and_basename(self):
        with self.assertRaises(CommandNotAllowed):
            validate_argv([], self.fs)

        with self.assertRaises(CommandNotAllowed):
            validate_argv(["/bin/ls"], self.fs)

        with self.assertRaises(CommandNotAllowed):
            validate_argv(["./test"], self.fs)

    def test_unallowed_binary(self):
        for bin_name in ["sh", "bash", "curl", "wget", "rm", "node"]:
            with self.assertRaises(CommandNotAllowed):
                validate_argv([bin_name], self.fs)

    def test_pytest_validation(self):
        # Allowed flags and paths
        (self.root / "test_main.py").write_text("", encoding="utf-8")
        validate_argv(["pytest", "-q", "-v", "--tb=short", "--no-header", "--maxfail=1", "test_main.py"], self.fs)

        # Disallowed flag
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["pytest", "--pdb"], self.fs)

        # Path escaping root
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["pytest", "../other.py"], self.fs)

    def test_python_validation(self):
        # python and python3 -m pytest
        validate_argv(["python", "-m", "pytest", "-q"], self.fs)
        validate_argv(["python3", "-m", "pytest", "-v"], self.fs)

        # python and python3 single script
        (self.root / "script.py").write_text("", encoding="utf-8")
        validate_argv(["python", "script.py"], self.fs)
        validate_argv(["python3", "script.py"], self.fs)

        # Disallowed -c
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["python", "-c", "print(1)"], self.fs)
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["python3", "-c", "print(1)"], self.fs)

        # Disallowed -m other
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["python", "-m", "http.server"], self.fs)
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["python3", "-m", "http.server"], self.fs)

    def test_npm_pnpm_validation(self):
        for cmd in ["npm", "pnpm"]:
            validate_argv([cmd, "test"], self.fs)
            validate_argv([cmd, "run", "test:unit"], self.fs)
            with self.assertRaises(CommandNotAllowed):
                validate_argv([cmd, "install"], self.fs)
            with self.assertRaises(CommandNotAllowed):
                validate_argv([cmd, "run", "semi;bad"], self.fs)

    def test_cargo_validation(self):
        validate_argv(["cargo", "test"], self.fs)
        validate_argv(["cargo", "test", "--quiet"], self.fs)
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["cargo", "build"], self.fs)

    def test_go_validation(self):
        validate_argv(["go", "test"], self.fs)
        validate_argv(["go", "test", "./..."], self.fs)
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["go", "run", "main.go"], self.fs)

    def test_git_validation(self):
        validate_argv(["git", "status"], self.fs)
        validate_argv(["git", "diff"], self.fs)
        validate_argv(["git", "log", "-1"], self.fs)
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["git", "commit", "-m", "foo"], self.fs)
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["git", "push"], self.fs)


if __name__ == "__main__":
    unittest.main()
