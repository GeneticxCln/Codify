"""A submodule's own config cannot start a program through the engine's `status` and `diff`.

The jail leaves the workspace writable and binds only the top-level `.git` read-only. A submodule's
`.git` is a file inside the workspace, so a command the jail ran could replace it with a repository
whose config names a clean filter. The engine's next `git status` (the UI's poll) compared that
submodule's files through the filter, outside the jail and as the person. The audit reproduced it end
to end: the jailed run exited 0 and the marker file appeared after `GitService().status()`.

These tests run real git against a real superproject, with the same-size content change that makes git
run a filter at all.
"""

from __future__ import annotations

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


def _git(cwd: Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-c", "user.email=a@b.c", "-c", "user.name=t", *args],
        cwd=cwd, env=env, check=True, capture_output=True, text=True,
    )


class SubmoduleFilterCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.marker = base / "MARKER-filter-ran"
        source = base / "source"
        source.mkdir()
        _git(source, "init", "-q", ".")
        (source / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
        (source / "x.txt").write_text("data\n", encoding="utf-8")
        _git(source, "add", ".")
        _git(source, "commit", "-qm", "sub")
        self.root = base / "super"
        self.root.mkdir()
        _git(self.root, "init", "-q", ".")
        _git(self.root, "submodule", "--quiet", "add", str(source), "sub")
        _git(self.root, "commit", "-qm", "super")
        # What a jailed command can do: the submodule's gitfile is workspace content, so replace it with
        # a repository of its own whose config names a filter.
        payload = base / "payload.sh"
        payload.write_text(f"#!/bin/sh\necho ran > {self.marker}\ncat\n", encoding="utf-8")
        payload.chmod(0o755)
        sub = self.root / "sub"
        (sub / ".git").unlink()
        _git(sub, "init", "-q", ".")
        (sub / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
        _git(sub, "config", "filter.evil.clean", str(payload))
        (sub / "x.txt").write_text("data\n", encoding="utf-8")
        _git(sub, "add", "-A")
        self.marker.unlink(missing_ok=True)  # `git add` above ran it in the setup; the test starts clean
        # Same size, later mtime: git can only tell this file changed by running the filter.
        time.sleep(1.1)
        (sub / "x.txt").write_text("DATA\n", encoding="utf-8")

    def test_the_engines_status_does_not_run_a_submodules_filter(self) -> None:
        GitService().status(str(self.root))
        self.assertFalse(self.marker.exists(), "git status ran a program named by a submodule's config")

    def test_the_read_only_diff_does_not_run_a_submodules_filter(self) -> None:
        sandbox = SandboxService()
        sandbox.run_command(str(self.root), ["git", "diff"], mode="read_only")
        self.assertFalse(self.marker.exists(), "git diff ran a program named by a submodule's config")

    def test_a_caller_cannot_switch_submodule_scanning_back_on(self) -> None:
        fs = FileSystemService(str(self.root))
        for spelling in ("--ignore-submodules=none", "--ignore-submodules=untracked", "--ignore-submodules"):
            with self.assertRaises(CommandNotAllowed, msg=spelling):
                validate_argv(["git", "diff", spelling], fs, mode="read_only")

    def test_the_runner_puts_the_safe_setting_on_status_and_diff(self) -> None:
        for sub in ("status", "diff"):
            self.assertIn("--ignore-submodules=dirty", git_readonly.runner_args([sub]))


if __name__ == "__main__":
    unittest.main()
