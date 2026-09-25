"""Prove the git hooks block, refuse and stand down exactly where they claim to.

`.githooks/pre-commit` and `.githooks/pre-push` are, with GitHub Actions unavailable,
the only thing standing between a broken tree and the remote. They are shell: no type
checker reads them, no import covers them, and they shipped once with a silent hole —
the branch test keyed on the *local* ref, and `git push origin HEAD:branch` sends
`HEAD` there, so every push made that way skipped the gate while reporting that there
was nothing to gate. That hole was found by hand, in a shell, which is not where this
project's guarantees live.

Two things make a hook testable without running the gate it calls:

* `make` is a stub on a PATH built for the test (`HookTestBase.asked_make`), so a hook
  under test records what it asked for instead of running it — `make ci` from inside
  `make test` would be the gate re-entering its own suite;
* the hooks are *copied* into a throwaway repo rather than run in place, because a hook
  finds its project root from its own path: running the checked-in file would reach
  into this repository's `.git` and `.venv` instead of the fixture the test controls.

Every case a hook claims to handle is asserted here: the gate it runs and the targets
it names, the red gate that must block, the missing linter that must refuse rather
than pass, the merge that must not be blocked by conflict markers, and the tag or
deletion push that must be skipped *out loud*.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOKS = PROJECT_ROOT / ".githooks"

# Git's pre-push protocol, one line per ref:
#   <local ref> <local sha> <remote ref> <remote sha>
# A ref the remote does not have yet, or one being deleted, arrives as all zeros.
REAL_SHA = "1" * 40
NULL_SHA = "0" * 40

# Everything the hooks shell out to, which the test PATH has to carry. Nothing else is
# on it on purpose: a `ruff` that happens to live in /usr/bin must not be able to
# decide whether the pre-commit tool check passes.
_NEEDED_TOOLS = ("bash", "git", "dirname", "cat")


class HookTestBase(unittest.TestCase):
    """A throwaway repo holding the real hooks, a recording `make`, and a real `.git`."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        hooks = self.root / ".githooks"
        hooks.mkdir()
        for name in ("pre-commit", "pre-push"):
            shutil.copy2(HOOKS / name, hooks / name)
        # A genuine git dir, because the pre-commit hook asks git where MERGE_HEAD
        # lives: a hand-made directory would not exercise that lookup.
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, capture_output=True)
        self.make_log = self.root / "make.log"
        self.bin = self._stub_path()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _stub_path(self) -> Path:
        """A PATH holding only what these tests put on it: thin wrappers for the
        externals a hook shells out to, and a `make` that records its arguments."""
        path = self.root / "bin"
        path.mkdir()
        for tool in _NEEDED_TOOLS:
            found = shutil.which(tool)
            if not found:
                self.fail(f"these tests need {tool} on PATH to run a hook at all")
            self._script(path / tool, f'exec "{found}" "$@"\n')
        self._script(
            path / "make",
            f'printf "%s\\n" "$*" >> "{self.make_log}"\n'
            'exit "${STUB_MAKE_STATUS:-0}"\n',
        )
        return path

    def _script(self, path: Path, body: str) -> None:
        """Write an executable stub. A wrapper, not a symlink: creating a symlink on
        Windows needs privileges, and this suite has to run wherever the project does."""
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o700)

    def run_hook(
        self, name: str, stdin: str = "", *, make_status: int = 0
    ) -> subprocess.CompletedProcess[str]:
        """Run one of the hooks the way git does: stdin carries the refs, the exit
        status decides. `make_status` is what the stub `make` exits with."""
        env = {
            "PATH": str(self.bin),
            "HOME": str(self.root),
            "STUB_MAKE_STATUS": str(make_status),
        }
        return subprocess.run(
            [str(self.root / ".githooks" / name)],
            cwd=self.root,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def asked_make(self) -> list[str]:
        """What a hook asked `make` to run, in order. Empty when it never called it."""
        if not self.make_log.exists():
            return []
        return [line for line in self.make_log.read_text().splitlines() if line]

    def give_dev_tools(self) -> None:
        """Install `ruff` and `mypy` into the fixture's venv, where an unactivated
        shell finds them (`make typecheck` relies on the same fallback)."""
        for tool in ("ruff", "mypy"):
            path = self.root / ".venv" / "bin" / tool
            path.parent.mkdir(parents=True, exist_ok=True)
            self._script(path, "exit 0\n")

    def push_line(self, local_ref: str, local_sha: str, remote_ref: str, remote_sha: str) -> str:
        return f"{local_ref} {local_sha} {remote_ref} {remote_sha}\n"


class TestPrePushHook(HookTestBase):
    def test_a_branch_push_runs_the_whole_gate(self) -> None:
        """The gate is `make ci` — one command, every toolchain. If the hook ever
        called a narrower target the push would be checked less than the gate claims."""
        result = self.run_hook(
            "pre-push", self.push_line("refs/heads/master", REAL_SHA, "refs/heads/master", REAL_SHA)
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["ci"], self.asked_make())

    def test_a_push_of_head_onto_a_branch_is_gated(self) -> None:
        """The regression this suite exists for.

        `git push origin HEAD:branch` sends `HEAD` as the *local* ref, so a hook that
        tests the local ref for `refs/heads/*` skips the push while printing that there
        was nothing to gate. The remote ref is what says a branch is being written.
        """
        result = self.run_hook(
            "pre-push", self.push_line("HEAD", REAL_SHA, "refs/heads/topic", NULL_SHA)
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["ci"], self.asked_make())

    def test_a_failing_gate_blocks_the_push(self) -> None:
        """A red gate must stop the push — and name the deliberate way past it, so the
        override is a decision rather than a discovery."""
        result = self.run_hook(
            "pre-push",
            self.push_line("refs/heads/master", REAL_SHA, "refs/heads/master", REAL_SHA),
            make_status=1,
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("BLOCKED", result.stderr)
        self.assertIn("--no-verify", result.stderr)

    def test_a_multi_branch_push_runs_the_gate_once(self) -> None:
        """Two branches in one push is still one gate, not two: the cost is the gate,
        and running it per ref would make a broad push needlessly slow."""
        stdin = self.push_line("refs/heads/a", REAL_SHA, "refs/heads/a", REAL_SHA) + self.push_line(
            "refs/heads/b", REAL_SHA, "refs/heads/b", NULL_SHA
        )
        result = self.run_hook("pre-push", stdin)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["ci"], self.asked_make())

    def test_a_tag_only_push_does_not_run_the_gate(self) -> None:
        """A tag adds no source for the suite to judge — and says so out loud, because
        a gate that skips in silence is the failure mode this hook exists to prevent."""
        result = self.run_hook(
            "pre-push", self.push_line("refs/tags/v1", REAL_SHA, "refs/tags/v1", NULL_SHA)
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.asked_make())
        self.assertIn("nothing for 'make ci' to gate", result.stdout)

    def test_a_deletion_does_not_run_the_gate(self) -> None:
        """A deletion pushes no content: the all-zero local sha is how that is told
        apart from an update, and only an update has anything to check."""
        result = self.run_hook(
            "pre-push", self.push_line("(delete)", NULL_SHA, "refs/heads/gone", REAL_SHA)
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.asked_make())


class TestPreCommitHook(HookTestBase):
    def test_it_runs_the_cheap_legs_the_gate_starts_with(self) -> None:
        """`lint` then `typecheck`, the same two targets `make check` opens with — a
        green commit must not mean something different from a green gate."""
        self.give_dev_tools()
        result = self.run_hook("pre-commit")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["lint typecheck"], self.asked_make())

    def test_it_finds_the_project_venv_unactivated(self) -> None:
        """The tools live in `.venv/bin`, which is not on PATH in a plain shell. The
        Makefile already falls back to it for mypy; a hook stricter than the gate it
        stands for would block commits for an environment reason."""
        self.give_dev_tools()
        result = self.run_hook("pre-commit")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["lint typecheck"], self.asked_make())

    def test_a_failing_check_blocks_the_commit(self) -> None:
        """The point of the hook: the failure lands in your hands rather than two
        minutes into a push."""
        self.give_dev_tools()
        result = self.run_hook("pre-commit", make_status=1)
        self.assertEqual(1, result.returncode)
        self.assertIn("BLOCKED", result.stderr)
        self.assertIn("--no-verify", result.stderr)

    def test_a_missing_linter_refuses_instead_of_passing(self) -> None:
        """A linter that is not installed must never read as a check that passed, and
        must not fail as make's bare "ruff: No such file or directory" either — the
        dev tools are not engine dependencies, so the refusal has to say where they
        come from. Nothing is delegated to `make` on the way out."""
        result = self.run_hook("pre-commit")
        self.assertEqual(1, result.returncode)
        self.assertIn("ruff", result.stderr)
        self.assertIn("pip install -e", result.stderr)
        self.assertEqual([], self.asked_make())

    def test_a_merge_in_progress_stands_down(self) -> None:
        """A merge stopped on conflicts leaves `<<<<<<<` in the tree, which is a syntax
        error to ruff and to mypy: checking there would block a merge over a conflict
        git is still working through. Pre-push stays the proof."""
        self.give_dev_tools()
        (self.root / ".git" / "MERGE_HEAD").write_text(REAL_SHA + "\n")
        result = self.run_hook("pre-commit")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.asked_make())
        self.assertIn("MERGE_HEAD", result.stdout)

    def test_a_rebase_in_progress_stands_down(self) -> None:
        """Same conflicts, different marker: `git rebase` writes a directory, not a
        file, so a hook that only tested for MERGE_HEAD would lint a mid-rebase tree."""
        self.give_dev_tools()
        (self.root / ".git" / "rebase-merge").mkdir()
        result = self.run_hook("pre-commit")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.asked_make())


class TestTheHooksAndTheProjectAgree(unittest.TestCase):
    """The hooks are shell, so nothing else checks what they depend on in this repo."""

    def test_the_targets_the_hooks_call_exist(self) -> None:
        """A hook calling a target nobody defines fails at push time, not at check
        time — the one moment nobody can afford to discover it."""
        makefile = (PROJECT_ROOT / "Makefile").read_text()
        for target in ("ci", "lint", "typecheck"):
            self.assertRegex(makefile, rf"(?m)^{target}:", f"the Makefile has no {target} target")

    def test_the_install_points_git_at_the_versioned_hooks(self) -> None:
        """`make hooks` is the whole install: without it the checked-in files are not a
        gate at all, and an uninstalled hook fails open while looking present."""
        makefile = (PROJECT_ROOT / "Makefile").read_text()
        self.assertRegex(makefile, r"(?m)^\tgit config core\.hooksPath \.githooks$")

    def test_the_hooks_are_executable(self) -> None:
        """Git ignores a hook it cannot execute, silently — which is how a gate goes
        quiet after a clone that lost the mode bit."""
        for name in ("pre-commit", "pre-push"):
            hook = HOOKS / name
            self.assertTrue(hook.is_file(), f".githooks/{name} is missing")
            self.assertTrue(os.access(hook, os.X_OK), f".githooks/{name} is not executable")
            self.assertTrue(
                hook.read_bytes().startswith(b"#!/usr/bin/env bash"),
                f".githooks/{name} must carry its shebang",
            )


if __name__ == "__main__":
    unittest.main()
