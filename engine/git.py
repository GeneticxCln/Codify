from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from engine.spawn_guard import guarded_argv, guarded_env


class GitService:
    """The engine's git, one process deeper than it looks.

    Every git command runs under `engine/spawn_guard.py`, exactly like a sandboxed
    command does: kill the engine — a closed window SIGKILLs it — and git takes its
    whole tree with it. That is not a theoretical stray. A `git commit` runs the
    repository's hooks, which are workspace content: a hook that hangs (or a hook
    whose child keeps writing) used to live on after the engine that ran the commit
    was gone, with the repository still mutating under a closed window.
    """

    def __init__(self) -> None:
        self._git_bin = shutil.which("git") or "git"

    @staticmethod
    def _workspace_env() -> dict[str, str]:
        """Env for git subprocesses, with credential variables stripped.

        `git commit` runs the repo's pre-commit / commit-msg hooks with this
        process's environment. The engine carries provider API keys in env vars
        (the same names providers resolve through ENV_KEY_MAP) — a checked-in
        hook would otherwise execute with live credentials in scope, and hooks
        are workspace content: they run whatever the repo ships. The whitelist
        keeps git's own identity/locale knobs and drops everything else.
        """
        keep = ("PATH", "LANG", "LC_ALL", "TZ", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")
        return {k: os.environ[k] for k in keep if k in os.environ}

    def is_git_repo(self, root_path: str) -> bool:
        git_dir = Path(root_path).resolve() / ".git"
        return git_dir.exists()

    # ── the one way this class starts a process ─────────────────────────

    def _run_bytes(
        self, args: list[str], *, cwd: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one git command under the guard, capturing raw output.

        `env=None` means "inherit this process's environment", which is what the
        read-only callers have always done; the guard still gets the pid handover.
        """
        return subprocess.run(
            guarded_argv([self._git_bin, *args]),
            cwd=cwd,
            env=guarded_env(env),
            capture_output=True,
            check=False,
            # The guard must lead the session it kills (see spawn_guard.py), exactly
            # as SandboxService.run_command does it.
            start_new_session=True,
        )

    def _run_text(
        self, args: list[str], *, cwd: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Same, as text — deliberately decoding UTF-8 with replacement.

        Not `subprocess.run(text=True)`: that decodes with the *locale* encoding, and
        a repository whose status output carries a filename outside it would raise
        `UnicodeDecodeError` out of a read. Git writes paths as UTF-8 bytes (that is
        also why `_tracked_files` reads them itself).
        """
        res = self._run_bytes(args, cwd=cwd, env=env)
        return subprocess.CompletedProcess(
            res.args,
            res.returncode,
            res.stdout.decode("utf-8", errors="replace"),
            res.stderr.decode("utf-8", errors="replace"),
        )

    def init_repo(self, root_path: str) -> bool:
        try:
            res = self._run_text(["init"], cwd=str(Path(root_path).resolve()))
            return res.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _tracked_files(self, cwd: str, env: dict[str, str]) -> list[str]:
        """Workspace-relative paths git already knows about (used for deletions)."""
        res = self._run_bytes(["ls-files", "-z"], cwd=cwd, env=env)
        if res.returncode != 0:
            return []
        # Binary mode: -z output is NUL-separated raw bytes, and decoding with
        # text=True would mangle/raise on filenames outside the locale encoding.
        # git stores paths as UTF-8 bytes; each is decoded individually so one
        # odd filename degrades to a replacement char, not a crash.
        return [
            p.decode("utf-8", errors="replace") for p in res.stdout.split(b"\0") if p
        ]

    def get_status(self, root_path: str) -> str:
        if not self.is_git_repo(root_path):
            return ""
        res = self._run_text(["status", "--porcelain"], cwd=str(Path(root_path).resolve()))
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
        env = self._workspace_env()
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
        added = self._run_text(["add", "-A", "--", *stageable], cwd=cwd, env=env)
        if added.returncode != 0:
            return None

        # Commit *with the pathspec*, so a file the user had staged for their own
        # commit stays staged instead of riding along in this one. Git takes the
        # worktree contents of the named paths, which is exactly what was written.
        res = self._run_text(
            ["commit", "--no-verify", "-m", message, "--", *stageable], cwd=cwd, env=env,
        )
        if res.returncode != 0:
            # Exit 1 means "nothing to commit" for these paths — the step's files
            # already match the last commit, or git refused for its own reasons.
            return None

        # Return hash
        rev = self._run_text(["rev-parse", "HEAD"], cwd=cwd, env=env)
        return rev.stdout.strip() if rev.returncode == 0 else None
