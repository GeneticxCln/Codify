"""`POST /workspaces/{id}/push` and `GET /workspaces/{id}/ci`: the person's push and the checks it starts.

The push is the one write here that leaves the machine, so these tests use real repositories and a real
bare remote: a fake would prove the route's shape and nothing about whether the refspec, the branch name or
the guarded spawn actually work. The checks reader is the opposite — it is an HTTP read with a fixed host,
so its tests use `httpx.MockTransport` and assert the states a header can draw, including the one that says
"unknown" rather than "passed".
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from httpx import ASGITransport

from engine import github_remote
from engine.app import app, lifespan
from engine.git import GitService, sanitize_remote_url

AUTHOR = {
    "GIT_AUTHOR_NAME": "Codify Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Codify Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        env={**os.environ, **AUTHOR},
        capture_output=True,
        text=True,
        check=True,
    )


def _repo(root: Path, *, remote: Path | None = None) -> Path:
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("one\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "first")
    if remote is not None:
        git(root, "remote", "add", "origin", str(remote))
    return root


class TestGitHubRemotes(unittest.TestCase):
    def test_the_three_spellings_git_writes_are_read(self) -> None:
        for url in (
            "https://github.com/acme/widget",
            "https://github.com/acme/widget.git",
            "git@github.com:acme/widget.git",
            "ssh://git@github.com/acme/widget.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    github_remote.GithubRemote(owner="acme", repo="widget"),
                    github_remote.parse_remote(url),
                )

    def test_every_other_host_is_refused(self) -> None:
        for url in (
            "",
            "/tmp/remote.git",
            "https://gitlab.com/acme/widget.git",
            "https://github.com.evil.example/acme/widget.git",
            "https://evil.example/github.com/acme/widget",
            "https://github.com/acme",
            "https://github.com/acme/widget/tree/main",
            "file:///tmp/remote.git",
        ):
            with self.subTest(url=url):
                self.assertIsNone(github_remote.parse_remote(url))

    def test_the_compare_link_is_the_branch_that_was_pushed(self) -> None:
        remote = github_remote.GithubRemote(owner="acme", repo="widget")

        self.assertEqual(
            "https://github.com/acme/widget/compare/release/2.0?expand=1",
            github_remote.compare_url(remote, "release/2.0"),
        )


class TestRemoteUrlsAreShownWithoutCredentials(unittest.TestCase):
    def test_an_embedded_token_is_not_echoed(self) -> None:
        for raw, shown in (
            ("https://user:secret@github.com/acme/widget.git", "https://github.com/acme/widget.git"),
            ("https://ghp_token@github.com/acme/widget.git", "https://github.com/acme/widget.git"),
            ("https://alice@github.com/acme/widget.git", "https://github.com/acme/widget.git"),
            ("ssh://git@github.com/acme/widget.git", "ssh://git@github.com/acme/widget.git"),
            ("git@github.com:acme/widget.git", "git@github.com:acme/widget.git"),
            ("hg@example.com:widget", "hg@example.com:widget"),
            ("https://github.com/acme/widget.git", "https://github.com/acme/widget.git"),
            ("/tmp/remote.git", "/tmp/remote.git"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(shown, sanitize_remote_url(raw))

    def test_a_token_in_the_username_position_is_removed_too(self) -> None:
        self.assertEqual(
            "ssh://github.com/acme/widget.git",
            sanitize_remote_url("ssh://ghp_token@github.com/acme/widget.git"),
        )


class TestPushService(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.service = GitService()

    def test_a_branch_reaches_the_bare_remote(self) -> None:
        remote = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", str(remote))
        local = _repo(self.root / "local", remote=remote)
        (local / "README.md").write_text("two\n", encoding="utf-8")
        git(local, "add", "README.md")
        git(local, "commit", "-q", "-m", "second")

        answer = self.service.push(str(local))

        self.assertIs(True, answer["ok"])
        self.assertEqual("main", answer["branch"])
        self.assertEqual(str(remote), answer["remote"])
        self.assertEqual(
            git(local, "rev-parse", "HEAD").stdout.strip(),
            git(remote, "rev-parse", "refs/heads/main").stdout.strip(),
        )

    def test_a_push_runs_the_repositorys_pre_push_hook(self) -> None:
        """Push is the person's action, so its hook is the point, not a hazard.

        The hook-disabling hardening that protects the engine's own git commands used to
        sit in the one door every command shares, so the button silently skipped a
        pre-push hook that would block the same push in a terminal.
        """
        remote = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", str(remote))
        local = _repo(self.root / "local", remote=remote)
        marker = local / "hook-ran.txt"
        hook = local / ".git" / "hooks" / "pre-push"
        hook.write_text(
            "#!/bin/sh\n"
            f"touch '{marker}'\n"
            "exit 1\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)

        answer = self.service.push(str(local))

        self.assertIs(False, answer["ok"], "a pre-push hook that refuses did not block the push")
        self.assertTrue(marker.exists(), "the Push button skipped the repository's pre-push hook")

    def test_a_repository_filter_does_not_block_the_persons_push(self) -> None:
        """A push runs no filter, so the commit guard must not take the button away."""
        remote = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", str(remote))
        local = _repo(self.root / "local", remote=remote)
        git(local, "config", "filter.evil.clean", "sh -c 'exit 1'")
        (local / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")

        answer = self.service.push(str(local))

        self.assertIs(True, answer["ok"], answer["reason"])

    def test_the_answer_does_not_echo_a_credential_in_the_remote(self) -> None:
        done = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(GitService, "is_git_repo", return_value=True), \
                patch.object(GitService, "status", return_value={"branch": "main"}), \
                patch.object(GitService, "remote_url", return_value="https://user:secret@example.com/repo.git"), \
                patch.object(GitService, "head_sha", return_value="a" * 40), \
                patch.object(GitService, "_run_text", return_value=done):
            answer = self.service.push(str(self.root))

        self.assertIs(True, answer["ok"])
        self.assertEqual("https://example.com/repo.git", answer["remote"])
        self.assertNotIn("secret", str(answer))

    def test_a_refused_push_does_not_echo_the_credential_in_gits_sentence(self) -> None:
        raw = "https://user:secret@example.com/repo.git"
        refused = subprocess.CompletedProcess(
            [], 1, "", f"fatal: Authentication failed for '{raw}/'"
        )
        with patch.object(GitService, "is_git_repo", return_value=True), \
                patch.object(GitService, "status", return_value={"branch": "main"}), \
                patch.object(GitService, "remote_url", return_value=raw), \
                patch.object(GitService, "_run_text", return_value=refused):
            answer = self.service.push(str(self.root))

        self.assertIs(False, answer["ok"])
        self.assertNotIn("secret", str(answer))
        self.assertIn("https://example.com/repo.git", answer["reason"] or "")

    def test_no_origin_is_a_sentence_not_an_exception(self) -> None:
        local = _repo(self.root / "local")

        answer = self.service.push(str(local))

        self.assertIs(False, answer["ok"])
        self.assertIn("origin", answer["reason"] or "")

    def test_a_detached_head_has_no_branch_to_push(self) -> None:
        remote = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", str(remote))
        local = _repo(self.root / "local", remote=remote)
        git(local, "checkout", "-q", "--detach")

        answer = self.service.push(str(local))

        self.assertIs(False, answer["ok"])
        self.assertIn("detached", answer["reason"] or "")

    def test_a_folder_that_is_not_a_repository_is_refused(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()

        answer = self.service.push(str(plain))

        self.assertIs(False, answer["ok"])
        self.assertIn("not a git repository", answer["reason"] or "")


class TestThePushRoute(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.remote = self.base / "remote.git"
        git(self.base, "init", "-q", "--bare", str(self.remote))
        self.repo = _repo(self.base / "repo", remote=self.remote)

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

    async def test_the_route_pushes_and_answers_what_it_pushed(self) -> None:
        answer = await self.client.post(f"/workspaces/{self.ws}/push", headers=self.headers)

        self.assertEqual(200, answer.status_code, answer.text)
        body = answer.json()
        self.assertIs(True, body["ok"])
        self.assertEqual("main", body["branch"])
        self.assertEqual(str(self.remote), body["remote"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), body["sha"])
        self.assertIsNone(body["pr_url"], "a file remote became a GitHub pull-request link")

    async def test_the_route_needs_the_boot_token(self) -> None:
        refused = await self.client.post(f"/workspaces/{self.ws}/push")

        self.assertEqual(401, refused.status_code)

    async def test_a_refused_push_is_a_200_with_the_reason(self) -> None:
        git(self.repo, "remote", "remove", "origin")

        answer = await self.client.post(f"/workspaces/{self.ws}/push", headers=self.headers)

        self.assertEqual(200, answer.status_code, answer.text)
        self.assertIs(False, answer.json()["ok"])
        self.assertIn("origin", answer.json()["reason"])


class TestTheChecksRoute(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = _repo(self.base / "repo")

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
        self.ws = made.json()["id"]

    async def test_a_non_github_origin_is_a_reason_not_a_check(self) -> None:
        git(self.repo, "remote", "add", "origin", "/tmp/elsewhere.git")

        answer = await self.client.get(f"/workspaces/{self.ws}/ci", headers=self.headers)

        self.assertEqual(200, answer.status_code, answer.text)
        self.assertEqual("unknown", answer.json()["state"])
        self.assertIn("not a GitHub repository", answer.json()["reason"])

    async def test_the_route_asks_about_head_and_returns_what_github_said(self) -> None:
        git(self.repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
        sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        checked = {
            "state": "failure",
            "checks": [{"name": "check", "status": "completed", "conclusion": "failure", "url": ""}],
            "reason": None,
        }

        with patch("engine.github_remote.fetch_checks", return_value=checked) as asked:
            answer = await self.client.get(f"/workspaces/{self.ws}/ci", headers=self.headers)

        self.assertEqual(200, answer.status_code, answer.text)
        self.assertEqual("failure", answer.json()["state"])
        self.assertEqual(f"https://github.com/acme/widget/commit/{sha}/checks", answer.json()["url"])
        self.assertEqual("check", answer.json()["checks"][0]["name"])
        asked.assert_awaited_once_with(
            github_remote.GithubRemote(owner="acme", repo="widget"), sha,
        )


class TestTheChecksReader(unittest.IsolatedAsyncioTestCase):
    REMOTE = github_remote.GithubRemote(owner="acme", repo="widget")

    async def _read(self, handler: object) -> dict[str, Any]:
        return await github_remote.fetch_checks(
            self.REMOTE, "a" * 40, transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        )

    async def test_pending_success_and_failure_are_drawn_apart(self) -> None:
        def answer(runs: list[dict[str, object]]) -> object:
            return lambda request: httpx.Response(200, json={"total_count": len(runs), "check_runs": runs})

        pending = await self._read(answer([{"name": "build", "status": "in_progress", "conclusion": None}]))
        passed = await self._read(answer([{"name": "build", "status": "completed", "conclusion": "success"}]))
        failed = await self._read(answer([{"name": "build", "status": "completed", "conclusion": "failure"}]))
        none = await self._read(answer([]))

        self.assertEqual(("pending", "success", "failure", "none"),
                         (pending["state"], passed["state"], failed["state"], none["state"]))

    async def test_a_404_or_403_is_unknown_with_a_reason(self) -> None:
        for status in (404, 403):
            with self.subTest(status=status):
                answer = await self._read(lambda request, code=status: httpx.Response(code, json={}))

                self.assertEqual("unknown", answer["state"])
                self.assertTrue(answer["reason"])

    async def test_a_run_whose_url_is_not_this_repository_loses_it(self) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"check_runs": [{
                "name": "build", "status": "completed", "conclusion": "success",
                "html_url": "https://evil.example/check",
            }]})

        result = await self._read(answer)

        self.assertEqual("", result["checks"][0]["url"])


if __name__ == "__main__":
    unittest.main()
