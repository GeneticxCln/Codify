from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from engine.git import GitService
from tests.process_probe import file_text, pids_matching, sigkill_matching, wait_for_text, wait_until

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The marker in the grandchild's command line: the survivor the probes below hunt for.
GIT_ORPHAN_PROBE = "codify-git-orphan-probe"
# The commit message names the git process itself (`git commit … -m <message>` keeps it
# in argv) and the guard in front of it, which carries the whole guarded argv.
COMMIT_MARKER = "feat: codify-git-guard-marker"
# "The guard, with that commit as its argument": the ERE can only match the guard's own
# command line, because nothing else has spawn_guard.py before the message.
GUARDED_COMMIT = f"spawn_guard\\.py.*{COMMIT_MARKER}"
# Git runs a hook as `.git/hooks/post-commit` from the worktree root — a *relative*
# path, so the hook's command line does not name the repository. The hook therefore execs
# this file, whose unique name is the hook's marker (`python3 ./<name>`).
HOOK_BODY = "codify_git_hook_body.py"
# A hook that never returns: it writes a heartbeat and leaves a sleeping grandchild,
# the two things a stray hook was ever caught doing.
HOOK_BODY_SOURCE = (
    "import subprocess\n"
    "import sys\n"
    "import time\n"
    "from pathlib import Path\n"
    "\n"
    "HEARTBEAT = Path({heartbeat!r})\n"
    "\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)  # {probe}'])\n"
    "while True:\n"
    "    with HEARTBEAT.open('a', encoding='utf-8') as handle:\n"
    "        handle.write('beat\\n')\n"
    "    time.sleep(0.2)\n"
)


class GitTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.git = GitService()
        self.git.init_repo(str(self.root))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text)

    def git_out(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=False
        ).stdout

    def committed_paths(self, rev: str = "HEAD") -> set[str]:
        return {
            line
            for line in self.git_out("ls-tree", "-r", "--name-only", rev).splitlines()
            if line
        }

    def porcelain(self) -> str:
        return self.git_out("status", "--porcelain")


class TestGitService(GitTestBase):
    def test_git_init_and_detection(self) -> None:
        self.assertTrue(self.git.is_git_repo(str(self.root)))
        self.assertFalse(self.git.is_git_repo(str(self.root / "nope")))

    def test_status_and_commit(self) -> None:
        self.assertEqual(self.git.get_status(str(self.root)).strip(), "")

        self.write("hello.py", "print('hello')\n")
        self.assertIn("hello.py", self.git.get_status(str(self.root)))

        rev = self.git.commit(str(self.root), "feat: initial commit", ["hello.py"])
        assert rev is not None, "the commit must report its revision"
        self.assertEqual(len(rev), 40)
        self.assertEqual(self.git.get_status(str(self.root)).strip(), "")

        # Nothing left to record for that path.
        self.assertIsNone(self.git.commit(str(self.root), "feat: duplicate", ["hello.py"]))

    def test_commit_does_not_run_repo_hooks(self) -> None:
        """Hooks are workspace content: a checked-in pre-commit hook must not
        execute (with the engine's environment) as a side effect of committing.
        """
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            "echo HOOK_RAN >> hook-evidence.txt\n"
            "exit 1\n"  # a hook that fails the commit if it runs
        )
        hook.chmod(0o755)
        self.write("hooked.py", "x = 1\n")
        rev = self.git.commit(str(self.root), "feat: hook test", ["hooked.py"])
        self.assertIsNotNone(rev, "commit must succeed regardless of repo hooks")
        self.assertFalse((self.root / "hook-evidence.txt").exists(),
                         "pre-commit hook executed during commit")

    def test_commit_env_does_not_carry_credentials(self) -> None:
        """The commit subprocess env must not include provider key variables —
        hooks would otherwise run with live credentials in scope."""
        captured: dict[str, Any] = {}
        real_run = subprocess.run

        def spy(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            # Every git command runs one process deeper than it used to —
            # [python, engine/spawn_guard.py, <absolute git>, "commit", …] — so the
            # git binary and its subcommand are found two slots further along.
            if len(argv) > 3 and argv[2].endswith("git") and argv[3] == "commit":
                captured.update(kwargs.get("env") or {})
            return real_run(argv, **kwargs)

        import os as _os
        _os.environ["ANTHROPIC_API_KEY"] = "sk-leak-check"
        _os.environ["OPENAI_API_KEY"] = "sk-leak-check-2"
        try:
            import unittest.mock as mock
            with mock.patch("engine.git.subprocess.run", side_effect=spy):
                self.write("envcheck.py", "y = 2\n")
                rev = self.git.commit(str(self.root), "feat: env", ["envcheck.py"])
                self.assertIsNotNone(rev)
        finally:
            _os.environ.pop("ANTHROPIC_API_KEY", None)
            _os.environ.pop("OPENAI_API_KEY", None)
        self.assertNotIn("ANTHROPIC_API_KEY", captured)
        self.assertNotIn("OPENAI_API_KEY", captured)
        # Git still gets what it needs to run.
        self.assertIn("PATH", captured)

    def test_a_commit_contains_only_the_paths_it_names(self) -> None:
        """The regression: `git add -A` swept the user's work into our commit.

        A workspace is usually a working tree with somebody's unfinished business in
        it. A step that wrote one file must not commit the others — not the ones
        sitting untracked, and not the ones the user had already staged.
        """
        self.write("ours.py", "codify wrote this\n")
        self.write("user_wip.py", "half-finished by hand\n")
        self.write("user_staged.py", "deliberately staged by hand\n")
        subprocess.run(
            ["git", "add", "--", "user_staged.py"], cwd=self.root, check=False
        )

        rev = self.git.commit(str(self.root), "feat: our change", ["ours.py"])
        self.assertIsNotNone(rev)
        self.assertEqual(self.committed_paths(), {"ours.py"})

        status = self.porcelain()
        self.assertIn("?? user_wip.py", status, "untracked work stays untracked")
        self.assertIn("A  user_staged.py", status, "the user's staged file stays staged")

    def test_an_empty_path_list_commits_nothing_at_all(self) -> None:
        """A step that changed nothing must not commit what the user had staged."""
        self.write("user_staged.py", "staged before we ran\n")
        subprocess.run(["git", "add", "--", "user_staged.py"], cwd=self.root, check=False)

        self.assertIsNone(self.git.commit(str(self.root), "feat: nothing", []))
        # No commit was made at all: the repository still has an unborn HEAD.
        self.assertEqual(self.git_out("log", "--oneline").strip(), "")
        self.assertIn("A  user_staged.py", self.porcelain())

    def test_a_deletion_is_part_of_the_commit(self) -> None:
        self.write("gone.py", "temporary\n")
        self.git.commit(str(self.root), "feat: add gone", ["gone.py"])
        (self.root / "gone.py").unlink()

        rev = self.git.commit(str(self.root), "chore: remove gone", ["gone.py"])
        self.assertIsNotNone(rev)
        self.assertEqual(self.committed_paths(), set())
        self.assertEqual(self.git.get_status(str(self.root)).strip(), "")

    def test_a_path_that_does_not_exist_is_not_an_error_per_path(self) -> None:
        """A mixed proposal (one real file, one phantom) still commits the real one."""
        self.write("real.py", "content\n")
        rev = self.git.commit(str(self.root), "feat: add real", ["real.py", "phantom.py"])
        self.assertIsNotNone(rev)
        self.assertEqual(self.committed_paths(), {"real.py"})


class TestGuardedGitCommands(unittest.TestCase):
    """The last process the engine starts of its own: git, and git's own children.

    `test_sandbox.py` proves the guard for the commands a *model* asks for; this is the
    same proof for the processes the engine starts itself. It matters more than it
    looks: a `git commit` runs the repository's hooks (`--no-verify` skips pre-commit
    and commit-msg, not post-commit), hooks are workspace content, and git waits for
    one to finish. A hook that never returns used to keep running — and keep writing to
    the repository — after the engine that ran the commit was already gone.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.git = GitService()
        self.git.init_repo(str(self.root))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_a_hanging_commit_hook_dies_with_the_engine_that_started_the_commit(self) -> None:
        heartbeat = self.root / "hook-heartbeat.txt"
        hook = self.root / ".git" / "hooks" / "post-commit"
        hook.write_text(f"#!/bin/sh\nexec python3 ./{HOOK_BODY}\n", encoding="utf-8")
        hook.chmod(0o755)
        (self.root / HOOK_BODY).write_text(
            HOOK_BODY_SOURCE.format(heartbeat=str(heartbeat), probe=GIT_ORPHAN_PROBE),
            encoding="utf-8",
        )
        (self.root / "hooked.py").write_text("x = 1\n", encoding="utf-8")

        # A stand-in engine: the commit is the only thing it does, and it blocks inside
        # git waiting for the hook, exactly as the real engine's scribe stage does.
        driver = (
            "from engine.git import GitService\n"
            f"GitService().commit({str(self.root)!r}, {COMMIT_MARKER!r}, ['hooked.py'])\n"
        )
        engine = subprocess.Popen(
            [sys.executable, "-c", driver],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(
                wait_until(GIT_ORPHAN_PROBE, matches=True, timeout=15),
                "the commit never reached its post-commit hook",
            )
            # The hook, git and the guard are all live *now* — the patterns below must
            # mean something before the kill, or "gone afterwards" proves nothing.
            before = wait_for_text(heartbeat)
            self.assertNotEqual("", before, "the hook never wrote its heartbeat")
            self.assertNotEqual([], pids_matching(HOOK_BODY), "the hook body was not running")
            self.assertNotEqual(
                [], pids_matching(GUARDED_COMMIT),
                "the engine ran git with no guard in front of it",
            )

            engine.kill()  # SIGKILL: a closed window does not ask politely
            engine.wait(timeout=10)

            self.assertTrue(
                wait_until(GIT_ORPHAN_PROBE, matches=False, timeout=15),
                "the hook's grandchild outlived the engine that started the commit",
            )
            self.assertTrue(
                wait_until(HOOK_BODY, matches=False, timeout=15),
                "the commit hook itself outlived the engine that started the commit",
            )
            self.assertTrue(
                wait_until(COMMIT_MARKER, matches=False, timeout=15),
                "git, or the guard in front of it, outlived the engine",
            )
            frozen = file_text(heartbeat)
            time.sleep(1.0)
            self.assertEqual(
                frozen, file_text(heartbeat),
                "the hook kept writing to the repository after the engine died",
            )
        finally:
            engine.kill()
            for pattern in (GIT_ORPHAN_PROBE, HOOK_BODY, COMMIT_MARKER, str(self.root)):
                sigkill_matching(pattern)


if __name__ == "__main__":
    unittest.main()
