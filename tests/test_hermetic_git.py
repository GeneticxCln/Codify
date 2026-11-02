"""A suite started with git's repository variables set does not act on that repository.

`git` exports `GIT_DIR` (and `GIT_INDEX_FILE`, `GIT_WORK_TREE`, …) to a hook it runs, and from a linked worktree
`GIT_DIR` names the worktree's own git directory. Every test that ran `git add` / `git commit` in a throwaway
directory then acted on that repository: a push gate run from a worktree added four commits titled "first" to the
branch being pushed and wrote `core.bare = true`, `commit.gpgsign` and `gpg.program` into the shared
`.git/config`, which stopped every other worktree of the repository from working. `tests/hermetic.py` is the one
line every test module starts with, so it is where the launcher's repository is forgotten.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEAKED = {
    "GIT_DIR": "/somewhere/.git/worktrees/topic",
    "GIT_INDEX_FILE": "/somewhere/index",
    "GIT_WORK_TREE": "/somewhere",
    "GIT_PREFIX": "sub/",
}


class TestTheLaunchersRepositoryIsForgotten(unittest.TestCase):
    def _import_with(self, env: dict[str, str]) -> dict[str, str]:
        code = (
            "import os, json\n"
            "from tests import hermetic  # noqa\n"
            "print(json.dumps({k: v for k, v in os.environ.items() if k.startswith('GIT_')}))\n"
        )
        done = subprocess.run(  # noqa: S603 — this interpreter, a fixed program, no shell
            [sys.executable, "-c", code],
            cwd=ROOT, env={**os.environ, **env}, capture_output=True, text=True, check=True,
        )
        import json

        return dict(json.loads(done.stdout.strip().splitlines()[-1]))

    def test_the_variables_that_name_a_repository_are_gone_after_import(self) -> None:
        seen = self._import_with(LEAKED)
        for name in LEAKED:
            with self.subTest(name=name):
                self.assertNotIn(name, seen, f"{name} survived tests/hermetic.py, so a test's git could act on it")

    def test_a_commit_in_a_throwaway_directory_lands_in_that_directory(self) -> None:
        """The failure itself: with GIT_DIR leaked, this commit went to the other repository."""
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        subprocess.run(["git", "init", "-q", other.name], check=True)  # noqa: S607 — git on PATH, as everywhere here
        code = (
            "from tests import hermetic  # noqa\n"
            "import subprocess, tempfile\n"
            "d = tempfile.mkdtemp()\n"
            "run = lambda *a: subprocess.run(['git', *a], cwd=d, check=True, capture_output=True)\n"
            "run('init', '-q')\n"
            "run('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'mine')\n"
            "print(subprocess.run(['git', 'log', '--format=%s'], cwd=d, capture_output=True, text=True).stdout.strip())\n"
        )
        done = subprocess.run(  # noqa: S603 — this interpreter, a fixed program, no shell
            [sys.executable, "-c", code],
            cwd=ROOT, env={**os.environ, "GIT_DIR": str(Path(other.name) / ".git")},
            capture_output=True, text=True, check=True,
        )
        self.assertEqual("mine", done.stdout.strip())
        log = subprocess.run(  # noqa: S603, S607 — git on PATH, a fixed argv
            ["git", "-C", other.name, "log", "--format=%s"], capture_output=True, text=True, check=False,
        )
        self.assertEqual("", log.stdout.strip(), "the leaked repository received the throwaway commit")


if __name__ == "__main__":
    unittest.main()
