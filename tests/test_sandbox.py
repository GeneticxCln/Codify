from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from engine.app import PICKER_MARKER, _picker_command
from engine.fs import FileSystemService
from engine.sandbox import CommandNotAllowed, SandboxService, validate_argv
from engine.spawn_guard import ENV_PARENT_PID, Guard, parent_pid_from_env
# The process-table vocabulary, shared verbatim with the live-engine twin
# (tests/test_sandbox_orphans_e2e.py) so the two layers cannot drift.
from tests.process_probe import file_text, pids_matching, sigkill_matching, wait_for_text, wait_until

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# A string that appears in exactly one process's command line: the grandchild the
# survivor probes below look for. Nothing else in this file may contain it.
ORPHAN_PROBE = "codify-sandbox-orphan-probe"


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


class TestGuardDecisions(unittest.TestCase):
    """Who the guard watches, and how it tells a dead engine from a live parent."""

    def test_the_engine_pid_is_read_from_the_environment(self) -> None:
        self.assertEqual(4321, parent_pid_from_env({ENV_PARENT_PID: "4321"}))
        for value in ("", "   ", "not-a-pid", "0", "-2", "1e3"):
            self.assertIsNone(parent_pid_from_env({ENV_PARENT_PID: value}), value)

    def test_a_guard_started_by_an_already_dead_engine_knows_it(self) -> None:
        """The fork/exec window: no signal can arrive for a parent that is gone, and
        no reparenting will happen later — the pid the engine passed is the only way
        this window is visible at all."""
        self.assertTrue(Guard(expected_parent=1).started_after_the_engine_died())
        self.assertFalse(Guard(expected_parent=os.getppid()).started_after_the_engine_died())
        # No pid handed over (a guard run by hand): the live parent is all there is.
        self.assertFalse(Guard().started_after_the_engine_died())

    def test_a_live_parent_is_not_a_dead_engine(self) -> None:
        self.assertFalse(Guard().engine_is_gone())


class TestGuardedCommands(unittest.TestCase):
    """The command the guard runs: its status, and its tree dying with the engine."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.sandbox = SandboxService()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_a_normal_exit_is_reported_as_its_own_code(self) -> None:
        (self.root / "exits.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
        result = self.sandbox.run_command(str(self.root), ["python3", "exits.py"], timeout_s=30)
        self.assertEqual(3, result["exit_code"])
        self.assertEqual("", result["stderr"], "the guard must not add output of its own")

    def test_a_command_killed_by_a_signal_still_reports_that_signal(self) -> None:
        """The contract the guard has to keep: the engine reads -15, not 143.

        The verdict text quotes the exit code, so a guard that exited 143 where the
        command was killed by 15 would change what the transcript says happened.
        """
        (self.root / "suicide.py").write_text(
            "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n", encoding="utf-8"
        )
        result = self.sandbox.run_command(str(self.root), ["python3", "suicide.py"], timeout_s=30)
        self.assertEqual(-signal.SIGTERM, result["exit_code"])

    def test_a_command_taken_by_sigkill_is_reported_as_sigkill(self) -> None:
        """The one signal whose disposition cannot be reset.

        `signal.signal(SIGKILL, ...)` raises, so the naive "reset then re-raise" turns
        a command the OOM killer took into exit 1. This is the case that pins the
        exception: the guard has to re-raise without touching the disposition.
        """
        (self.root / "rampage.py").write_text(
            "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n", encoding="utf-8"
        )
        result = self.sandbox.run_command(str(self.root), ["python3", "rampage.py"], timeout_s=30)
        self.assertEqual(-signal.SIGKILL, result["exit_code"])

    def test_the_command_tree_dies_with_the_engine_that_started_it(self) -> None:
        """The stray this exists for: a command still writing to the workspace after
        the engine is gone.

        A killed window SIGKILLs the engine, so nothing inside it runs another line —
        the only thing left that can take the command's session down is the guard, so
        the kill here is SIGKILL rather than a polite TERM.
        """
        (self.root / "spawner.py").write_text(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', "
            f"'import time; time.sleep(120)  # {ORPHAN_PROBE}'])\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )
        driver = (
            "from engine.sandbox import SandboxService\n"
            f"SandboxService().run_command({str(self.root)!r}, ['python3', 'spawner.py'],"
            " timeout_s=120)\n"
        )
        engine = subprocess.Popen(
            [sys.executable, "-c", driver],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(
                wait_until(ORPHAN_PROBE, matches=True, timeout=10),
                "the command never got as far as spawning its grandchild",
            )
            engine.kill()  # SIGKILL: exactly what closing the window does
            engine.wait(timeout=10)
            self.assertTrue(
                wait_until(ORPHAN_PROBE, matches=False, timeout=10),
                "the grandchild outlived the engine that started the command",
            )
            self.assertTrue(
                wait_until(str(self.root), matches=False, timeout=5),
                "the command (or its guard) outlived the engine",
            )
        finally:
            engine.kill()
            for pattern in (str(self.root), ORPHAN_PROBE):
                sigkill_matching(pattern)


# The stand-in grandchild's marker: a sleeper the survivor probes below hunt for.
# The picker's own marker is engine/app.py's PICKER_MARKER, imported above — the
# test greps for the same string the route injects, so the two cannot drift.
PICKER_PROBE = "codify-picker-orphan-probe"


class TestGuardedFolderPicker(unittest.TestCase):
    """The engine's own GUI spawn: the native folder picker behind /workspaces/browse.

    The picker is `python3 -c <GTK source>` — no script file, so nothing in its
    command line names a path and `pgrep -f` would have nothing to hold on to; the
    marker lives in a comment inside the source itself (engine/app.py::PICKER_MARKER).
    It is also the one engine spawn whose normal lifetime is *a human thinking*: the
    dialog stays open until answered, so the 120 s timeout never fires in the case
    that matters — the engine dying (closed window) while the dialog is still on
    screen. The stand-in below keeps the guarded shape (same argv prefix, same pid
    handover, same `start_new_session`) and swaps only the GTK half for a heartbeat
    and a sleeping grandchild, the two things a stray picker was ever caught doing.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_an_open_picker_dies_with_the_engine_that_opened_it(self) -> None:
        # The exact command the route runs — the seam exists so a test can hold it
        # without opening GTK. Its preconditions are asserted here, not trusted:
        # the guard in front, the `-c` shape with its marker, the pid handover.
        argv, env = _picker_command()
        self.assertTrue(
            argv[1].endswith("spawn_guard.py"), "the picker ran with no guard in front of it"
        )
        self.assertEqual(argv[3], "-c")
        self.assertIn(PICKER_MARKER, argv[4], "the `-c` source lost its only pgrep marker")
        self.assertIn("Gtk.FileChooserNative", argv[4])
        self.assertEqual(env[ENV_PARENT_PID], str(os.getpid()))

        # The stand-in: same guarded shape, the GTK half replaced by a heartbeat and
        # a sleeping grandchild. Its source travels by *file*, not in the driver's
        # command line: the driver is an ancestor of the tree, and a marker in an
        # ancestor's cmdline makes `pgrep -f` see a survivor for exactly as long as
        # the test itself lives — a false one (the probes must only ever match the
        # tree the guard owns).
        heartbeat = self.root / "picker-heartbeat.txt"
        stand_in_path = self.root / "picker_stand_in.py"
        stand_in_path.write_text(
            f"# {PICKER_MARKER}\n"
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"HEARTBEAT = Path({str(heartbeat)!r})\n"
            f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)  # {PICKER_PROBE}'])\n"
            "while True:\n"
            "    with HEARTBEAT.open('a', encoding='utf-8') as handle:\n"
            "        handle.write('beat\\n')\n"
            "    time.sleep(0.2)\n",
            encoding="utf-8",
        )
        driver = (
            "import subprocess\n"
            "from engine.app import _picker_command\n"
            "argv, env = _picker_command()\n"
            f"with open({str(stand_in_path)!r}, encoding='utf-8') as handle:\n"
            "    argv[-1] = handle.read()\n"
            "subprocess.run(argv, env=env, capture_output=True, timeout=120, start_new_session=True)\n"
        )
        engine = subprocess.Popen(
            [sys.executable, "-c", driver],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(
                wait_until(PICKER_MARKER, matches=True, timeout=30),
                "the stand-in picker never started",
            )
            self.assertNotEqual(
                [], pids_matching(PICKER_PROBE), "the picker never spawned its grandchild"
            )
            before = wait_for_text(heartbeat, timeout=30)
            self.assertNotEqual("", before, "the picker never wrote its heartbeat")

            engine.kill()  # SIGKILL: exactly what closing the window does
            engine.wait(timeout=10)

            self.assertTrue(
                wait_until(PICKER_PROBE, matches=False, timeout=15),
                "the picker's grandchild outlived the engine that opened the dialog",
            )
            self.assertTrue(
                wait_until(PICKER_MARKER, matches=False, timeout=15),
                "the picker, or the guard in front of it, outlived the engine",
            )
            frozen = file_text(heartbeat)
            time.sleep(1.0)
            self.assertEqual(
                frozen, file_text(heartbeat),
                "the picker kept writing after the engine died",
            )
        finally:
            engine.kill()
            for pattern in (PICKER_PROBE, PICKER_MARKER):
                sigkill_matching(pattern)


if __name__ == "__main__":
    unittest.main()
