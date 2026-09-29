"""The librarian's "read-only" git cannot change a repository, run a program or read outside it.

The rule this file exists for, from the audit of 2026-09-29: the read-only allowlist was a
*denylist of flag spellings* over git, and git accepts unambiguous abbreviations of long
options. `git grep --open-files-in-pa="touch X" p` ran `touch`; `git branch -v --del NAME`
deleted a branch; `git branch -v NAME` and `git tag --sort=x NAME` created refs; and
`git diff <file> /dev/null` printed a file from outside the workspace, because git silently
switches to `--no-index` when a path lies outside the tree and nothing checked the
positional arguments. The tests that stood guard (`tests/test_sandbox.py`) called
`validate_argv` on the spellings the authors had already met and never ran git.

So these run git. Each attack goes through the real `SandboxService.run_command(mode=
"read_only")` against a real repository, and the assertion is about the world afterwards —
refs, config, working tree, a marker file, an output file, an outside file's contents —
not about whether some string was on a list. The property tests at the bottom are the ones
that would have caught the class: no abbreviation of any allowed option is accepted, so a
new spelling git grows cannot walk around the table.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine import git_readonly
from engine.fs import FileSystemService
from engine.git import GitService
from engine.sandbox import CommandNotAllowed, SandboxService, validate_argv

OUTSIDE_TEXT = "OUTSIDE-SECRET-CONTENT"


class RealRepoCase(unittest.TestCase):
    """A workspace that is a real git repository, and a secret that is not in it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "ws"
        self.root.mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.txt"
        self.secret.write_text(OUTSIDE_TEXT + "\n", encoding="utf-8")
        self.marker = base / "MARKER-a-command-ran"
        self.outfile = base / "OUTFILE-git-wrote-this"
        self.sandbox = SandboxService()

        self.git("init", "-q", ".")
        (self.root / "a.txt").write_text("one\nneedle\n", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "b.txt").write_text("two\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        (self.root / "a.txt").write_text("one\nneedle\nthree\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "second")
        self.git("branch", "feature-x")
        self.git("tag", "v1")

    # ── plumbing the test itself needs (never the code under test) ───────────
    def git(self, *args: str, cwd: Path | None = None) -> str:
        env = {
            "PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test",
        }
        done = subprocess.run(
            ["git", *args], cwd=cwd or self.root, env=env, capture_output=True, text=True, check=True,
        )
        return done.stdout

    def world(self) -> dict[str, str]:
        """Everything a read-only command must leave exactly as it found it."""
        tree = hashlib.sha256()
        for path in sorted(p for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts):
            tree.update(str(path.relative_to(self.root)).encode())
            tree.update(path.read_bytes())
        return {
            "refs": self.git("for-each-ref", "--format=%(refname) %(objectname)"),
            "config": self.git("config", "--local", "--list"),
            "status": self.git("status", "--porcelain"),
            "head": self.git("rev-parse", "HEAD"),
            "tree": tree.hexdigest(),
        }

    def attempt(self, *argv: str) -> dict[str, object] | None:
        """The result, or None when the sandbox refused before running anything."""
        try:
            return dict(self.sandbox.run_command(str(self.root), list(argv), timeout_s=20, mode="read_only"))
        except CommandNotAllowed:
            return None

    def assertNothingHappened(self, argv: list[str], before: dict[str, str]) -> None:
        self.assertEqual(before, self.world(), f"{argv} changed the repository")
        self.assertFalse(self.marker.exists(), f"{argv} ran a command")
        self.assertFalse(self.outfile.exists(), f"{argv} wrote a file outside the workspace")


class TestAttacksRefusedAndHarmless(RealRepoCase):
    """Every argv the audit found, plus the neighbours a fix by spelling would miss."""

    def attacks(self) -> list[list[str]]:
        marker = str(self.marker)
        out = str(self.outfile)
        secret = str(self.secret)
        return [
            # arbitrary command execution through grep's pager, every spelling
            ["git", "grep", f"--open-files-in-pa=touch {marker}", "one"],
            ["git", "grep", f"--open-files-in-pager=touch {marker}", "one"],
            ["git", "grep", f"-Otouch {marker}", "one"],
            ["git", "grep", "-O", f"touch {marker}", "one"],
            ["git", "grep", f"--open-files-in-p=touch {marker}", "one"],
            # branch and tag writes that arrive as ordinary-looking flags
            ["git", "branch", "-v", "--del", "feature-x"],
            ["git", "branch", "--dele", "feature-x"],
            ["git", "branch", "-v", "newb"],
            ["git", "branch", "--sort=refname", "newb"],
            ["git", "branch", "--column", "newb"],
            ["git", "branch", "-v", "-c", "master", "copyb"],
            ["git", "branch", "-v", "-u", "master"],
            ["git", "branch", "--set-upstream-to=master"],
            ["git", "branch", "-m", "renamed"],
            ["git", "tag", "--sort=refname", "newtag"],
            ["git", "tag", "--format=x", "newtag"],
            ["git", "tag", "-n", "newtag"],
            ["git", "tag", "-v", "v1"],
            ["git", "tag", "-a", "x", "-m", "y"],
            ["git", "tag", "-d", "v1"],
            # reading outside the workspace, with and without the option that names it
            ["git", "diff", secret, "/dev/null"],
            ["git", "diff", "/etc/hostname", "a.txt"],
            ["git", "diff", "--no-index", secret, "/dev/null"],
            ["git", "diff", "--no-ind", secret, "/dev/null"],
            ["git", "show", "HEAD", "--", secret],
            ["git", "log", "-1", "--", secret],
            ["git", "grep", "-n", "SECRET", "--", secret],
            ["git", "blame", secret],
            ["git", "ls-files", "--others", str(self.outside)],
            ["git", "diff", "../outside/secret.txt", "/dev/null"],
            ["git", "show", "HEAD:../outside/secret.txt"],
            ["git", "diff", "~/.ssh/id_rsa", "/dev/null"],
            # writing a file anywhere
            ["git", "diff", f"--output={out}"],
            ["git", "log", f"--output={out}"],
            ["git", "show", f"--output={out}", "HEAD"],
            ["git", "diff", "--outp=" + out],
            # everything that is not a read at all
            ["git", "commit", "-m", "x"], ["git", "checkout", "."], ["git", "reset", "--hard"],
            ["git", "clean", "-fd"], ["git", "stash"], ["git", "remote", "add", "x", "y"],
            ["git", "fetch"], ["git", "push"], ["git", "apply", "x"], ["git", "config", "x.y", "z"],
            ["git", "worktree", "add", "../w"], ["git", "gc"], ["git", "update-ref", "-d", "HEAD"],
            # global options are not the caller's to set
            ["git", "-c", "core.pager=id", "log"], ["git", "-C", "..", "log"],
            ["git", "--git-dir=../x", "log"], ["git", "--exec-path"], ["git", "--no-pager", "log"],
            # object reads that take their input from somewhere else
            ["git", "cat-file", "--batch"], ["git", "cat-file", "--textconv", "HEAD:a.txt"],
            ["git", "diff", "--ext-diff"], ["git", "diff", "--textconv"],
        ]

    def test_every_attack_is_refused_and_leaves_the_world_as_it_was(self) -> None:
        for argv in self.attacks():
            with self.subTest(argv=argv):
                before = self.world()
                refused = self.attempt(*argv) is None
                self.assertNothingHappened(argv, before)
                self.assertTrue(refused, f"{argv} was accepted by the read-only sandbox")

    def test_an_outside_file_is_never_printed(self) -> None:
        # The sharpest form of the leak, asserted on output rather than on refusal, so a
        # future change that lets one of these through still cannot hand a model the text.
        for argv in (
            ["git", "diff", str(self.secret), "/dev/null"],
            ["git", "diff", "/dev/null", str(self.secret)],
            ["git", "show", "HEAD", "--", str(self.secret)],
        ):
            with self.subTest(argv=argv):
                result = self.attempt(*argv)
                if result is not None:
                    self.assertNotIn(OUTSIDE_TEXT, str(result["stdout"]) + str(result["stderr"]))


class TestNothingSecretReachesTheChild(RealRepoCase):
    def test_the_read_only_child_environment_carries_no_credentials(self) -> None:
        # A recon command runs with the user's engine environment unless something strips
        # it, and the engine's environment holds provider keys. `git.read_only` passed
        # env=None; the sandbox's own path filtered. Both go through one function now.
        planted = {"OPENAI_API_KEY": "sk-PLANTED", "ANTHROPIC_API_KEY": "sk-ant-PLANTED",
                   "CODIFY_BOOT_TOKEN": "tok-PLANTED", "HOME": "/home/planted"}
        saved = {k: os.environ.get(k) for k in planted}
        os.environ.update(planted)

        def restore() -> None:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

        env = git_readonly.runner_env(os.environ)

        joined = " ".join(f"{k}={v}" for k, v in env.items())
        self.assertNotIn("PLANTED", joined, env)
        self.assertEqual(os.devnull, env["GIT_CONFIG_GLOBAL"])
        self.assertEqual("0", env["GIT_OPTIONAL_LOCKS"], "a read must not take the index lock")
        self.assertEqual("0", env["GIT_TERMINAL_PROMPT"])

    def test_the_conductors_door_runs_git_with_that_environment(self) -> None:
        # `GitService.read_only` is the second door onto the same validator. Point it at a
        # fake `git` that reports what it was started with, and read the report back.
        report = Path(self.tmp.name) / "env-report"
        fake = Path(self.tmp.name) / "fakegit"
        fake.write_text(
            f'#!/bin/sh\nenv > "{report}"\nprintf "%s\\n" "$@" >> "{report}"\n', encoding="utf-8",
        )
        fake.chmod(0o755)
        os.environ["OPENAI_API_KEY"] = "sk-PLANTED"
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY", None)
        service = GitService()
        service._git_bin = str(fake)

        service.read_only(str(self.root), ["log", "-1"])

        text = report.read_text(encoding="utf-8")
        self.assertNotIn("sk-PLANTED", text)
        self.assertIn("--no-pager", text)
        self.assertIn("--no-textconv", text, "diff-shaped commands must not run a configured textconv")


class TestWhatStillWorks(RealRepoCase):
    """The allowlist is only worth having if the librarian can still do its job."""

    def expect(self, argv: list[str], needle: str) -> None:
        result = self.attempt(*argv)
        self.assertIsNotNone(result, f"{argv} was refused")
        assert result is not None
        self.assertEqual(0, result["exit_code"], f"{argv}: {result['stderr']}")
        self.assertIn(needle, str(result["stdout"]), f"{argv}")

    def test_the_recon_commands_a_librarian_actually_uses(self) -> None:
        cases: list[tuple[list[str], str]] = [
            (["git", "status", "--short"], ""),
            (["git", "log", "-2", "--oneline"], "second"),
            (["git", "log", "--format=%s", "-1"], "second"),
            (["git", "log", "--since=2000-01-01", "--oneline"], "first"),
            (["git", "log", "--oneline", "--", "a.txt"], "first"),
            (["git", "show", "HEAD:a.txt"], "needle"),
            (["git", "show", "--stat", "HEAD"], "a.txt"),
            (["git", "diff", "HEAD~1", "HEAD", "--stat"], "a.txt"),
            (["git", "diff", "--name-only", "HEAD~1", "HEAD"], "a.txt"),
            (["git", "grep", "-n", "needle"], "a.txt"),
            (["git", "grep", "-n", "-e", "needle", "--", "a.txt"], "needle"),
            (["git", "grep", "-i", "-l", "NEEDLE"], "a.txt"),
            (["git", "branch", "--list"], "feature-x"),
            (["git", "branch", "-a"], "feature-x"),
            (["git", "branch", "--show-current"], ""),
            (["git", "branch", "--list", "feature-*"], "feature-x"),
            (["git", "tag", "-l"], "v1"),
            (["git", "tag", "-l", "v*"], "v1"),
            (["git", "tag", "--list", "v*"], "v1"),
            (["git", "ls-files"], "a.txt"),
            (["git", "ls-files", "--others", "--exclude-standard"], ""),
            (["git", "rev-parse", "HEAD"], ""),
            (["git", "rev-parse", "--abbrev-ref", "HEAD"], ""),
            (["git", "blame", "a.txt"], "needle"),
            (["git", "cat-file", "-p", "HEAD:a.txt"], "needle"),
            (["git", "cat-file", "-t", "HEAD"], "commit"),
            (["git", "describe", "--always"], ""),
            (["git", "shortlog", "-sn", "HEAD"], "t"),
            (["git", "show-ref", "--heads"], "refs/heads"),
        ]
        for argv, needle in cases:
            with self.subTest(argv=argv):
                self.expect(argv, needle)

    def test_a_revision_range_is_not_mistaken_for_a_path_escape(self) -> None:
        # `a..b` contains two dots but is not a `..` path component.
        self.expect(["git", "log", "--oneline", "HEAD~1..HEAD"], "second")
        self.expect(["git", "diff", "--stat", "HEAD~1...HEAD"], "a.txt")


class TestListingFormsAreAcceptedAndCreateNothing(RealRepoCase):
    """`branch` and `tag` take a pattern when a listing flag is present, and a name otherwise.

    The refusal of a bare name is only half the rule. This is the other half, asserted on
    the repository rather than on the validator: git itself agrees that these are listings.
    """

    def test_a_pattern_after_a_listing_flag_lists_and_creates_no_ref(self) -> None:
        for argv in (
            ["git", "branch", "-l", "newb"], ["git", "branch", "--list", "newb"],
            ["git", "tag", "-l", "newtag"], ["git", "tag", "--list", "newtag"],
            ["git", "branch", "-a", "--list", "feature-*"],
        ):
            with self.subTest(argv=argv):
                before = self.world()
                result = self.attempt(*argv)
                self.assertIsNotNone(result, f"{argv} is a listing and was refused")
                self.assertEqual(before, self.world(), f"{argv} changed a ref")


class TestRepositoryConfigCannotRunAProgram(RealRepoCase):
    """A repository's own config can name programs; a read must not start any of them.

    `core.fsmonitor`, `diff.external` and `diff.<driver>.textconv` are all commands in
    `.git/config`, and git runs them from plain `status`, `diff`, `log -p` and `show`. Each
    test first shows the attack works against plain git — a control that cannot fire would
    make the assertion after it worthless — then that the sandbox's git does not run it.
    """

    def plant(self, key: str, value: str) -> None:
        self.git("config", key, value)

    def programme(self, name: str) -> Path:
        script = Path(self.tmp.name) / name
        script.write_text(f'#!/bin/sh\ntouch "{self.marker}"\ncat "$2" 2>/dev/null\nexit 0\n', encoding="utf-8")
        script.chmod(0o755)
        return script

    def dirty(self) -> None:
        (self.root / "a.txt").write_text("one\nneedle\nchanged\n", encoding="utf-8")

    def assertControlFires(self, *plain: str) -> None:
        """Plain git, as any developer runs it, starts the planted program."""
        self.marker.unlink(missing_ok=True)
        self.git(*plain)
        self.assertTrue(self.marker.exists(), "the control did not fire: this test proves nothing")
        self.marker.unlink()

    def assertNoDoorStartsIt(self, argv: list[str]) -> None:
        self.marker.unlink(missing_ok=True)
        result = self.attempt(*argv)
        self.assertIsNotNone(result, f"{argv} was refused; the test is about what it runs")
        self.assertFalse(self.marker.exists(), f"{argv} started a program named in the repository's config")
        self.marker.unlink(missing_ok=True)
        GitService().read_only(str(self.root), argv[1:])
        self.assertFalse(self.marker.exists(), f"git_history {argv[1:]} started a program named in the config")

    def test_a_configured_external_diff_is_not_run(self) -> None:
        self.plant("diff.external", str(self.programme("ext-diff.sh")))
        self.dirty()
        self.assertControlFires("--no-pager", "diff")

        self.assertNoDoorStartsIt(["git", "diff"])
        self.assertNoDoorStartsIt(["git", "log", "-p", "-1"])
        self.assertNoDoorStartsIt(["git", "show", "HEAD"])

    def test_a_configured_textconv_is_not_run(self) -> None:
        (self.root / ".gitattributes").write_text("*.txt diff=evil\n", encoding="utf-8")
        self.plant("diff.evil.textconv", str(self.programme("textconv.sh")))
        self.dirty()
        self.assertControlFires("--no-pager", "diff")

        for argv in (["git", "diff"], ["git", "log", "-p", "-1"], ["git", "show", "HEAD:a.txt"],
                     ["git", "show", "HEAD"], ["git", "blame", "a.txt"]):
            with self.subTest(argv=argv):
                self.assertNoDoorStartsIt(argv)

    def test_a_configured_fsmonitor_is_not_run(self) -> None:
        self.plant("core.fsmonitor", str(self.programme("fsmonitor.sh")))
        self.assertControlFires("--no-pager", "status")

        for argv in (["git", "status"], ["git", "diff"], ["git", "ls-files", "--modified"]):
            with self.subTest(argv=argv):
                self.assertNoDoorStartsIt(argv)

    def test_the_verifiers_git_in_test_mode_is_the_same_hardened_git(self) -> None:
        # Test mode allows exactly `git status`, `git diff` and `git log -1`, and they are
        # the commands most exposed to a planted diff driver.
        self.plant("diff.external", str(self.programme("ext-diff.sh")))
        self.plant("core.fsmonitor", str(self.programme("fsmonitor.sh")))
        self.dirty()
        for argv in (["git", "status"], ["git", "diff"], ["git", "log", "-1"]):
            with self.subTest(argv=argv):
                self.marker.unlink(missing_ok=True)
                self.sandbox.run_command(str(self.root), list(argv), timeout_s=20, mode="test")
                self.assertFalse(self.marker.exists(), f"{argv} in test mode started a configured program")

    def test_a_status_leaves_the_index_alone(self) -> None:
        # `git status` refreshes the index and writes it back. A read must not write, which
        # is what GIT_OPTIONAL_LOCKS=0 is for: the index file is byte-identical after.
        index = self.root / ".git" / "index"
        os.utime(self.root / "a.txt", (1, 1))  # make the cached stat data stale, so a refresh would rewrite
        before = index.read_bytes()

        self.assertIsNotNone(self.attempt("git", "status"))

        self.assertEqual(before, index.read_bytes(), "a read-only status rewrote the index")


class TestTheWorkspaceIsTheCeiling(RealRepoCase):
    """A workspace that is not a repository must not read the history of one above it."""

    def test_a_workspace_inside_another_repository_sees_none_of_it(self) -> None:
        outer = Path(self.tmp.name) / "outer"
        project = outer / "project"
        project.mkdir(parents=True)
        self.git("init", "-q", ".", cwd=outer)
        (outer / "outer-secret.txt").write_text("OUTER-FILE-CONTENT\n", encoding="utf-8")
        self.git("add", "-A", cwd=outer)
        self.git("commit", "-q", "-m", "OUTER-COMMIT-SUBJECT", cwd=outer)
        (project / "mine.txt").write_text("x\n", encoding="utf-8")

        for argv in (["git", "log", "--oneline"], ["git", "show", "HEAD:outer-secret.txt"],
                     ["git", "ls-files"], ["git", "status", "--short"], ["git", "rev-parse", "--show-toplevel"]):
            with self.subTest(argv=argv):
                result = self.sandbox.run_command(str(project), list(argv), timeout_s=20, mode="read_only")
                seen = str(result["stdout"]) + str(result["stderr"])
                self.assertNotIn("OUTER-COMMIT-SUBJECT", seen)
                self.assertNotIn("OUTER-FILE-CONTENT", seen)
                self.assertNotIn(str(outer), seen.replace(str(project), ""), "the parent repository was found")
                self.assertNotEqual(0, result["exit_code"], f"{argv} worked in a directory that is not a repository")

        said = GitService().read_only(str(project), ["log", "--oneline"])
        self.assertNotIn("OUTER-COMMIT-SUBJECT", said)


class TestSymlinksDoNotCarryARead(RealRepoCase):
    def test_a_link_to_a_directory_outside_is_not_a_path_into_it(self) -> None:
        os.symlink(self.outside, self.root / "outlink")
        os.symlink(self.secret, self.root / "filelink")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "links")
        before = self.world()
        for argv in (
            ["git", "blame", "outlink/secret.txt"], ["git", "log", "-1", "--", "outlink/secret.txt"],
            ["git", "grep", "-n", "SECRET", "--", "outlink"], ["git", "ls-files", "--others", "outlink"],
            ["git", "diff", "outlink/secret.txt", "a.txt"], ["git", "blame", "filelink"],
            ["git", "show", "HEAD:outlink/secret.txt"],
        ):
            with self.subTest(argv=argv):
                result = self.attempt(*argv)
                if result is not None:
                    self.assertNotIn(OUTSIDE_TEXT, str(result["stdout"]) + str(result["stderr"]))
        self.assertEqual(before, self.world())

    def test_searching_the_whole_tree_does_not_follow_links_out_of_it(self) -> None:
        os.symlink(self.outside, self.root / "outlink")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "links")
        result = self.attempt("git", "grep", "-n", "SECRET")
        assert result is not None
        self.assertNotIn(OUTSIDE_TEXT, str(result["stdout"]))


class TestNothingWaitsForAKeyboard(RealRepoCase):
    def test_a_command_that_reads_stdin_gets_end_of_file_not_the_engines_stdin(self) -> None:
        # `git shortlog` with no revision reads standard input. The engine's own stdin is
        # nobody's business and, if it is an open pipe, it never ends: the call would sit
        # out its whole timeout.
        read_end, write_end = os.pipe()
        saved = os.dup(0)
        self.addCleanup(os.close, read_end)
        self.addCleanup(os.close, write_end)
        os.dup2(read_end, 0)
        try:
            result = self.sandbox.run_command(str(self.root), ["git", "shortlog", "-s"], timeout_s=8, mode="read_only")
        finally:
            os.dup2(saved, 0)
            os.close(saved)
        self.assertNotEqual(124, result["exit_code"], "git waited on the engine's stdin until the timeout")
        self.assertEqual(0, result["exit_code"])

    def test_the_conductors_door_does_not_inherit_stdin_either(self) -> None:
        read_end, write_end = os.pipe()
        saved = os.dup(0)
        self.addCleanup(os.close, read_end)
        self.addCleanup(os.close, write_end)
        os.dup2(read_end, 0)
        try:
            said = GitService().read_only(str(self.root), ["shortlog", "-s"])
        finally:
            os.dup2(saved, 0)
            os.close(saved)
        self.assertIn("(exit 0)", said)


class TestAModelChosenReadIsBounded(RealRepoCase):
    def test_a_git_that_never_finishes_is_stopped_with_everything_it_started(self) -> None:
        # A stand-in git that starts a child and waits on it forever. The bound has to
        # remove the child too: stopping only the process the engine spawned would leave
        # the sleeper running, holding the output pipes.
        pidfile = Path(self.tmp.name) / "child.pid"
        fake = Path(self.tmp.name) / "hanging-git"
        fake.write_text(f'#!/bin/sh\nsleep 300 &\necho $! > "{pidfile}"\nwait\n', encoding="utf-8")
        fake.chmod(0o755)
        service = GitService()
        service._git_bin = str(fake)
        service.read_only_timeout_s = 1

        started = time.monotonic()
        said = service.read_only(str(self.root), ["log", "-1"])
        elapsed = time.monotonic() - started

        self.assertIn("did not finish in 1s", said)
        self.assertLess(elapsed, 15, "the bound did not bound anything")
        child = int(pidfile.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        os.kill(child, 9)
        self.fail("the child git started is still running after the timeout")


class TestFormatsThatWouldRunGpg(RealRepoCase):
    def test_a_format_that_asks_for_a_signature_is_refused_and_others_are_not(self) -> None:
        for argv in (
            ["git", "log", "--format=%G?"], ["git", "log", "--pretty=format:%H %GS"],
            ["git", "show", "--format=%GK", "HEAD"], ["git", "branch", "--list", "--format=%(signature)"],
            ["git", "tag", "--list", "--format=%(contents:signature)"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(self.attempt(*argv), f"{argv} would make git run gpg")
        for argv in (["git", "log", "--format=%H %s", "-1"], ["git", "log", "--pretty=oneline", "-1"],
                     ["git", "branch", "--list", "--format=%(refname:short)"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(self.attempt(*argv), f"{argv} is an ordinary format and was refused")


class TestPropertiesThatWouldHaveCaughtTheClass(unittest.TestCase):
    """Facts about the table itself, so a new spelling cannot walk around it."""

    def test_no_abbreviation_of_an_allowed_long_option_is_accepted(self) -> None:
        # Git accepts any unambiguous prefix of a long option. If the validator does too,
        # or if it is exact-match but lists an option that is a prefix of something
        # dangerous, `--del` for `--delete` walks straight through.
        fs = FileSystemService(tempfile.gettempdir())
        for sub, options in git_readonly.GIT_READ_ONLY.items():
            allowed = set(options)
            for option in allowed:
                if not option.startswith("--"):
                    continue
                for cut in range(3, len(option)):
                    prefix = option[:cut]
                    if prefix in allowed:
                        continue  # a shorter allowed option that happens to be a prefix
                    with self.subTest(sub=sub, option=option, abbreviation=prefix):
                        with self.assertRaises(CommandNotAllowed):
                            validate_argv(["git", sub, prefix], fs, mode="read_only")

    def test_nothing_that_writes_executes_or_names_a_file_is_on_the_table(self) -> None:
        forbidden = {
            "--output", "--ext-diff", "--textconv", "--no-index", "--open-files-in-pager",
            "-O", "--pager", "--exec", "--exec-path", "--git-dir", "--work-tree", "-C", "-c",
            "--delete", "-d", "-D", "--move", "-m", "-M", "--copy", "--force", "-f", "--edit-description",
            "--set-upstream-to", "-u", "--unset-upstream", "--batch", "--batch-check", "--batch-all-objects",
            "-a", "--annotate", "-s", "--sign", "--file", "--exclude-from", "--exclude-per-directory",
            "--orderfile", "--verify-tag", "-F",
        }
        # A few of those spellings are legitimate *and different* options in other
        # subcommands (`-s` is --short in status, `-a` is --all in branch, `-C` is
        # copy-detection in diff); the test names the pairs that must not appear.
        legitimate_elsewhere = {
            ("status", "-s"), ("branch", "-a"), ("diff", "-C"), ("log", "-C"), ("show", "-C"),
            ("blame", "-C"), ("blame", "-s"), ("shortlog", "-s"), ("log", "-s"), ("show", "-s"),
            ("diff", "-s"), ("diff", "-M"), ("log", "-M"), ("show", "-M"), ("blame", "-M"),
            ("grep", "-C"), ("grep", "-a"), ("describe", "-a"), ("show-ref", "-s"),
            ("tag", "-a"), ("cat-file", "-s"), ("branch", "-c"),
        }
        for sub, options in git_readonly.GIT_READ_ONLY.items():
            for option in options:
                if option in forbidden and (sub, option) not in legitimate_elsewhere:
                    self.fail(f"git {sub} allows {option}, which writes, executes or names a file")
        # The ones that are dangerous *in the subcommand that has them*.
        for sub, option in (("branch", "-d"), ("branch", "-D"), ("branch", "-m"), ("branch", "-c"),
                            ("branch", "-u"), ("tag", "-a"), ("tag", "-d"), ("tag", "-s"), ("tag", "-v"),
                            ("grep", "-O"), ("grep", "-f"), ("diff", "--output"), ("log", "--output"),
                            ("cat-file", "--batch")):
            self.assertNotIn(option, git_readonly.GIT_READ_ONLY.get(sub, {}), f"git {sub} {option}")

    def test_the_subcommand_list_is_derived_from_the_table_not_a_second_literal(self) -> None:
        self.assertEqual(frozenset(git_readonly.GIT_READ_ONLY), git_readonly.READ_ONLY_GIT_SUBCOMMANDS)

    def test_control_characters_in_any_argument_are_a_refusal_not_a_crash(self) -> None:
        fs = FileSystemService(tempfile.gettempdir())
        for argv in (["git", "log", "a\x00b"], ["git", "grep", "x\ny"], ["ls", "a\x00b"], ["wc", "a\x00b"]):
            with self.subTest(argv=argv):
                with self.assertRaises(CommandNotAllowed):
                    validate_argv(argv, fs, mode="read_only")
        for argv in (["pytest", "a\x00b"], ["python3", "x\x00.py"]):
            with self.subTest(argv=argv):
                with self.assertRaises(CommandNotAllowed):
                    validate_argv(argv, fs, mode="test")


if __name__ == "__main__":
    unittest.main()
