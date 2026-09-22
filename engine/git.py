from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class GitService:
    def __init__(self):
        self._git_bin = shutil.which("git") or "git"

    def is_git_repo(self, root_path: str) -> bool:
        git_dir = Path(root_path).resolve() / ".git"
        return git_dir.exists()

    def init_repo(self, root_path: str) -> bool:
        try:
            res = subprocess.run(
                [self._git_bin, "init"],
                cwd=str(Path(root_path).resolve()),
                capture_output=True,
                text=True,
                check=False,
            )
            return res.returncode == 0
        except Exception:
            return False

    def _tracked_files(self, cwd: str, env: dict[str, str]) -> list[str]:
        """Workspace-relative paths git already knows about (used for deletions)."""
        res = subprocess.run(
            [self._git_bin, "ls-files", "-z"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            return []
        return [p for p in res.stdout.split("\0") if p]

    def get_status(self, root_path: str) -> str:
        if not self.is_git_repo(root_path):
            return ""
        res = subprocess.run(
            [self._git_bin, "status", "--porcelain"],
            cwd=str(Path(root_path).resolve()),
            capture_output=True,
            text=True,
            check=False,
        )
        return res.stdout

    def commit(
        self,
        root_path: str,
        message: str,
        paths: list[str],
        author_name: str = "Codify",
        author_email: str = "codify@local",
    ) -> str | None:
        """Commit exactly `paths` (workspace-relative). Empty list → no commit.

        `paths` is required, and that is the point. This used to run a bare
        `git add -A`, which stages **every** file in the workspace: a user with
        half-finished work in their tree would find it swept into a commit whose
        message says "feat: add banner file". A commit may only contain what the
        step actually touched, and a step that touched nothing must not commit at
        all — including whatever the user happened to have staged themselves.
        """
        if not self.is_git_repo(root_path):
            return None
        # Nothing changed in this run: not our commit to make.
        if not paths:
            return None
        cwd = str(Path(root_path).resolve())
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = author_name
        env["GIT_AUTHOR_EMAIL"] = author_email
        env["GIT_COMMITTER_NAME"] = author_name
        env["GIT_COMMITTER_EMAIL"] = author_email

        # A pathspec matching nothing is fatal to the whole command ("did not match
        # any files"), and a model can name a file that never existed — which would
        # cost the *real* files their commit. So the list is filtered first: keep a
        # path that is inside the workspace and either on disk, or known to git (a
        # deletion has no file, but it does have an index entry).
        root = Path(cwd)
        tracked = set(self._tracked_files(cwd, env))
        stageable: list[str] = []
        for p in paths:
            resolved = (root / p).resolve()
            if resolved != root and root not in resolved.parents:
                continue  # outside the workspace: git could not commit it anyway
            if resolved.exists() or p in tracked:
                stageable.append(p)
        if not stageable:
            return None

        # New paths must be known to git before a pathspec commit can name them;
        # `-A` also records deletions. Only these paths are touched in the index.
        added = subprocess.run(
            [self._git_bin, "add", "-A", "--", *stageable],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if added.returncode != 0:
            return None

        # Commit *with the pathspec*, so a file the user had staged for their own
        # commit stays staged instead of riding along in this one. Git takes the
        # worktree contents of the named paths, which is exactly what was written.
        res = subprocess.run(
            [self._git_bin, "commit", "-m", message, "--", *stageable],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            # Exit 1 means "nothing to commit" for these paths — the step's files
            # already match the last commit, or git refused for its own reasons.
            return None

        # Return hash
        rev = subprocess.run(
            [self._git_bin, "rev-parse", "HEAD"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return rev.stdout.strip() if rev.returncode == 0 else None
