"""`GET /workspaces/{id}/git`: the branch and working-tree state the panel header shows (`docs/04` §6).

Three answers are quiet rather than errors, and each has its own test here because each is a real repository
state a person can be in: a folder that is not a repository, a repository with no commits, and a detached HEAD
all answer 200 with `branch: null` and a sentence in `reason`. The header shows nothing rather than an error
banner about a folder the person chose.

The status read goes through the model's own read-only git hardening (`GIT_OPTIONAL_LOCKS=0`, no user config,
no pager), so these tests use real git against real temporary repositories. A fake would prove the parser and
nothing about whether git writes the index while a header polls it.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from httpx import ASGITransport

from engine.app import app, lifespan
from engine.git import GitService

AUTHOR = {
    "GIT_AUTHOR_NAME": "Codify Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Codify Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command with a fixed identity, so the test does not depend on a developer's config."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        env={**os.environ, **AUTHOR},
        capture_output=True,
        text=True,
        check=True,
    )


class GitRepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.service = GitService()

    def repo(self, name: str = "repo", *, commit: bool = True) -> Path:
        root = self.root / name
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        if commit:
            (root / "README.md").write_text("one\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-q", "-m", "first")
        return root


class TestTheQuietAnswers(GitRepoCase):
    def test_a_clean_repository_names_its_branch_and_is_not_dirty(self) -> None:
        root = self.repo()

        state = self.service.status(str(root))

        self.assertEqual("main", state["branch"])
        self.assertIs(False, state["detached"])
        self.assertIs(False, state["dirty"])
        self.assertEqual((0, 0), (state["ahead"], state["behind"]))
        self.assertIsNone(state["reason"])

    def test_a_changed_file_makes_it_dirty(self) -> None:
        root = self.repo()
        (root / "README.md").write_text("two\n", encoding="utf-8")

        self.assertIs(True, self.service.status(str(root))["dirty"])

    def test_an_untracked_file_makes_it_dirty_too(self) -> None:
        # The header says "dirty", not "staged": a new file is work in progress exactly as an edit is.
        root = self.repo()
        (root / "new.txt").write_text("new\n", encoding="utf-8")

        self.assertIs(True, self.service.status(str(root))["dirty"])

    def test_a_detached_head_has_no_branch_and_says_so(self) -> None:
        root = self.repo()
        git(root, "checkout", "-q", "--detach")

        state = self.service.status(str(root))

        self.assertIsNone(state["branch"])
        self.assertIs(True, state["detached"])
        self.assertIn("detached", state["reason"] or "")

    def test_a_repository_with_no_commits_has_no_branch_and_says_so(self) -> None:
        root = self.repo(commit=False)

        state = self.service.status(str(root))

        self.assertIsNone(state["branch"])
        self.assertIs(False, state["detached"])
        self.assertIn("no commits", state["reason"] or "")

    def test_a_folder_that_is_not_a_repository_is_a_reason_not_an_error(self) -> None:
        root = self.root / "plain"
        root.mkdir()

        state = self.service.status(str(root))

        self.assertIsNone(state["branch"])
        self.assertIn("not a git repository", state["reason"] or "")

    def test_an_upstream_reports_how_far_the_branch_has_drifted(self) -> None:
        remote = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", str(remote))
        local = self.repo("local")
        git(local, "remote", "add", "origin", str(remote))
        git(local, "push", "-q", "-u", "origin", "main")

        other = self.root / "other"
        # `-b main`: a fresh bare repository's HEAD still points at master, and a clone that does not name the
        # branch would commit on the wrong one — then push it to the wrong ref, and the local branch would
        # compare against an upstream that never moved.
        git(self.root, "clone", "-q", "-b", "main", str(remote), str(other))
        (other / "README.md").write_text("remote change\n", encoding="utf-8")
        git(other, "add", "README.md")
        git(other, "commit", "-q", "-m", "remote")
        git(other, "push", "-q")
        git(local, "fetch", "-q")

        behind = self.service.status(str(local))
        self.assertEqual((0, 1), (behind["ahead"], behind["behind"]))

        # Now the local branch has its own commit: one each way, and the header can say both.
        (local / "README.md").write_text("local change\n", encoding="utf-8")
        git(local, "add", "README.md")
        git(local, "commit", "-q", "-m", "local")

        diverged = self.service.status(str(local))
        self.assertEqual((1, 1), (diverged["ahead"], diverged["behind"]))


class TestTheRoute(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "first")

        env = {
            "CODIFY_HOME": str(self.base / "state"),
            "CODIFY_DB": str(self.base / "state" / "codify.db"),
            "CODIFY_SECRETS": str(self.base / "state" / "secrets.json"),
        }
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._lifespan = lifespan(app)
        await self._lifespan.__aenter__()
        self.addAsyncCleanup(self._lifespan.__aexit__, None, None, None)
        self.headers = {"Authorization": f"Bearer {app.state.token}"}
        self.client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        made = await self.client.post(
            "/workspaces", headers=self.headers, json={"name": "P", "root_path": str(self.repo)}
        )
        self.assertEqual(200, made.status_code, made.text)
        self.ws = made.json()["id"]

    async def test_the_route_answers_with_the_branch_and_the_dirty_flag(self) -> None:
        clean = await self.client.get(f"/workspaces/{self.ws}/git", headers=self.headers)
        self.assertEqual(200, clean.status_code, clean.text)
        self.assertEqual(
            {"branch": "main", "detached": False, "dirty": False, "ahead": 0, "behind": 0, "reason": None},
            clean.json(),
        )

        (self.repo / "README.md").write_text("two\n", encoding="utf-8")
        dirty = await self.client.get(f"/workspaces/{self.ws}/git", headers=self.headers)
        self.assertEqual(200, dirty.status_code, dirty.text)
        self.assertIs(True, dirty.json()["dirty"])

    async def test_a_workspace_that_is_not_a_repository_is_still_a_200(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        made = await self.client.post(
            "/workspaces", headers=self.headers, json={"name": "Plain", "root_path": str(plain)}
        )
        self.assertEqual(200, made.status_code, made.text)

        answer = await self.client.get(f"/workspaces/{made.json()['id']}/git", headers=self.headers)

        self.assertEqual(200, answer.status_code, answer.text)
        self.assertIsNone(answer.json()["branch"])
        self.assertIn("not a git repository", answer.json()["reason"])


if __name__ == "__main__":
    unittest.main()
