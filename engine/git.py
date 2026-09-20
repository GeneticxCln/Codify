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

    def get_diff(self, root_path: str) -> str:
        if not self.is_git_repo(root_path):
            return ""
        res = subprocess.run(
            [self._git_bin, "diff"],
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
        author_name: str = "Codify",
        author_email: str = "codify@local",
    ) -> str | None:
        if not self.is_git_repo(root_path):
            return None
        cwd = str(Path(root_path).resolve())
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = author_name
        env["GIT_AUTHOR_EMAIL"] = author_email
        env["GIT_COMMITTER_NAME"] = author_name
        env["GIT_COMMITTER_EMAIL"] = author_email

        # Stage all changes
        subprocess.run([self._git_bin, "add", "-A"], cwd=cwd, env=env, check=False)

        # Check if there are staged changes
        diff_cached = subprocess.run(
            [self._git_bin, "diff", "--cached", "--quiet"],
            cwd=cwd,
            env=env,
            check=False,
        )
        if diff_cached.returncode == 0:
            # Nothing to commit
            return None

        # Commit
        res = subprocess.run(
            [self._git_bin, "commit", "-m", message],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
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
