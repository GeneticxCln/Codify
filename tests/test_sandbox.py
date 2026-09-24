from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from engine.fs import FileSystemService
from engine.sandbox import CommandNotAllowed, SandboxService, validate_argv


class TestSandboxService(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.fs = FileSystemService(str(self.root))
        self.sandbox = SandboxService()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_timeout_kills_whole_process_group(self) -> None:
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

    def test_empty_and_basename(self) -> None:
        with self.assertRaises(CommandNotAllowed):
            validate_argv([], self.fs)

        with self.assertRaises(CommandNotAllowed):
            validate_argv(["/bin/ls"], self.fs)

        with self.assertRaises(CommandNotAllowed):
            validate_argv(["./test"], self.fs)

    def test_unallowed_binary(self) -> None:
        for bin_name in ["sh", "bash", "curl", "wget", "rm", "node"]:
            with self.assertRaises(CommandNotAllowed):
                validate_argv([bin_name], self.fs)

    def test_pytest_validation(self) -> None:
        # Allowed flags and paths
        (self.root / "test_main.py").write_text("", encoding="utf-8")
        validate_argv(["pytest", "-q", "-v", "--tb=short", "--no-header", "--maxfail=1", "test_main.py"], self.fs)

        # Disallowed flag
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["pytest", "--pdb"], self.fs)

        # Path escaping root
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["pytest", "../other.py"], self.fs)

    def test_python_validation(self) -> None:
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

    def test_npm_pnpm_validation(self) -> None:
        for cmd in ["npm", "pnpm"]:
            validate_argv([cmd, "test"], self.fs)
            validate_argv([cmd, "run", "test:unit"], self.fs)
            with self.assertRaises(CommandNotAllowed):
                validate_argv([cmd, "install"], self.fs)
            with self.assertRaises(CommandNotAllowed):
                validate_argv([cmd, "run", "semi;bad"], self.fs)

    def test_cargo_validation(self) -> None:
        validate_argv(["cargo", "test"], self.fs)
        validate_argv(["cargo", "test", "--quiet"], self.fs)
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["cargo", "build"], self.fs)

    def test_go_validation(self) -> None:
        validate_argv(["go", "test"], self.fs)
        validate_argv(["go", "test", "./..."], self.fs)
        # Subpackage recursive patterns: a real workspace prefix plus /...
        (self.root / "pkg").mkdir()
        (self.root / "internal" / "store").mkdir(parents=True)
        validate_argv(["go", "test", "./pkg/..."], self.fs)
        validate_argv(["go", "test", "./internal/store/..."], self.fs)
        # A /... wildcard that escapes the workspace stays refused. (The check
        # is lexical, not existence-based: ./missing_pkg/... is allowed and go
        # itself reports the unknown package.)
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["go", "test", "../sibling/..."], self.fs)
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["go", "test", "/tmp/..."], self.fs)
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["go", "run", "main.go"], self.fs)

    def test_git_validation(self) -> None:
        validate_argv(["git", "status"], self.fs)
        validate_argv(["git", "diff"], self.fs)
        validate_argv(["git", "log", "-1"], self.fs)
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["git", "commit", "-m", "foo"], self.fs)
        with self.assertRaises(CommandNotAllowed):
            validate_argv(["git", "push"], self.fs)

    def test_read_only_git_cannot_mutate_refs(self) -> None:
        """The librarian's `git branch`/`git tag` are read-only: -d/-D/--delete
        remove refs, and a nominally read-only allowlist that omits them lets
        `git branch -D main` through in read-only mode."""
        for argv in (
            ["git", "branch", "-D", "main"],
            ["git", "branch", "-d", "topic"],
            ["git", "branch", "--delete", "main"],
            ["git", "tag", "-d", "v1"],
            ["git", "tag", "--delete", "v1"],
            ["git", "branch", "-m", "renamed"],
            ["git", "branch", "-f", "main", "HEAD~1"],
            ["git", "branch", "--force", "x", "y"],
        ):
            with self.assertRaises(CommandNotAllowed):
                validate_argv(argv, self.fs, mode="read_only")
        # The benign forms these came in on still pass.
        validate_argv(["git", "branch", "-a"], self.fs, mode="read_only")
        validate_argv(["git", "tag", "-l"], self.fs, mode="read_only")

    def test_read_only_git_grep_cannot_run_a_pager(self) -> None:
        """`git grep -Opager` executes the named binary as its pager — arbitrary
        code execution from a nominally read-only allowlist."""
        for argv in (
            ["git", "grep", "-Oless", "pattern"],
            ["git", "grep", "--open-files-in-pager", "pattern"],
            ["git", "grep", "-dO", "pattern"],  # combined short cluster
            ["git", "log", "--pager=less"],
        ):
            with self.assertRaises(CommandNotAllowed):
                validate_argv(argv, self.fs, mode="read_only")
        # Plain grep stays allowed.
        validate_argv(["git", "grep", "pattern"], self.fs, mode="read_only")

    def test_timeout_reports_exit_124(self) -> None:
        """The documented timeout contract is exit 124 (timeout(1)'s code), not
        the raw -15/-9 signal death."""
        (self.root / "sleeper.py").write_text("import time; time.sleep(60)\n", encoding="utf-8")
        result = self.sandbox.run_command(
            str(self.root), ["python3", "sleeper.py"], timeout_s=1
        )
        self.assertEqual(result["exit_code"], 124)
        self.assertIn("timed out after 1s", result["stderr"])


if __name__ == "__main__":
    unittest.main()
