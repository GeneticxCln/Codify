"""The editor's three routes: list a workspace's files, read one, save one back.

    GET /workspaces/{id}/files?limit=     the paths quick-open searches
    GET /workspaces/{id}/file?path=       {path, content, version, size}
    PUT /workspaces/{id}/file             {path, content, base_version} -> {path, version, size}

Each is a thin shell over `FileSystemService` and `LibraryService`, so what is asserted here is the contract a client
sees: the status and `code` of every refusal (so the UI can say the right sentence), that a refused request changed
nothing, and that a stale save is a 409 that carries the version to resolve it against. Authentication is not repeated:
`test_every_route_is_authenticated` sweeps every route in the app, these included.

Every refusal that is not a validation failure is a 4xx the error contract already declares (400, 404, 409, 422), so no
new status joins `ERROR_RESPONSES`.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from httpx import ASGITransport

from engine.app import app, lifespan
from engine.fs import MAX_EDIT_BYTES


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class FilesCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
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
            "/workspaces", headers=self.headers, json={"name": "P", "root_path": str(self.project)}
        )
        self.assertEqual(200, made.status_code, made.text)
        self.ws = made.json()["id"]

    def seed(self, name: str, raw: bytes | str) -> Path:
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
        return path

    async def listing(self, **params: Any) -> httpx.Response:
        return await self.client.get(f"/workspaces/{self.ws}/files", headers=self.headers, params=params)

    async def read(self, path: str) -> httpx.Response:
        return await self.client.get(f"/workspaces/{self.ws}/file", headers=self.headers, params={"path": path})

    async def save(self, **body: Any) -> httpx.Response:
        return await self.client.put(f"/workspaces/{self.ws}/file", headers=self.headers, json=body)


class TestListingFiles(FilesCase):
    async def test_every_file_in_the_tree_is_listed_by_relative_path_in_order(self) -> None:
        self.seed("README.md", "x")
        self.seed("src/app/main.py", "x")
        self.seed("src/util.py", "x")

        r = await self.listing()

        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertEqual(["README.md", "src/app/main.py", "src/util.py"], body["files"])
        self.assertFalse(body["truncated"])

    async def test_a_tree_deeper_than_the_librarians_orientation_is_listed_whole(self) -> None:
        self.seed("a/b/c/d/e/deep.txt", "x")

        self.assertEqual(["a/b/c/d/e/deep.txt"], (await self.listing()).json()["files"])

    async def test_dependency_and_metadata_folders_are_not_listed(self) -> None:
        self.seed("keep.py", "x")
        for skipped in (".git/config", "node_modules/pkg/index.js", "__pycache__/a.pyc", ".venv/bin/python", "dist/out.js"):
            self.seed(skipped, "x")

        self.assertEqual(["keep.py"], (await self.listing()).json()["files"])

    async def test_a_link_to_a_folder_outside_the_workspace_is_not_followed(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("s", encoding="utf-8")
        self.seed("keep.py", "x")
        (self.project / "out").symlink_to(outside, target_is_directory=True)

        self.assertEqual(["keep.py"], (await self.listing()).json()["files"])

    async def test_a_limit_truncates_and_says_so(self) -> None:
        for i in range(5):
            self.seed(f"f{i}.txt", "x")

        body = (await self.listing(limit=3)).json()

        self.assertEqual(3, len(body["files"]))
        self.assertTrue(body["truncated"])
        self.assertEqual(3, body["limit"])

    async def test_a_tree_of_exactly_the_limit_is_not_called_truncated(self) -> None:
        for i in range(3):
            self.seed(f"f{i}.txt", "x")

        body = (await self.listing(limit=3)).json()

        self.assertEqual(["f0.txt", "f1.txt", "f2.txt"], body["files"])
        self.assertFalse(body["truncated"], "nothing was left out, so nothing is truncated")

    async def test_a_limit_outside_what_is_sensible_is_refused(self) -> None:
        for limit in (0, -1, 10_001):
            with self.subTest(limit=limit):
                self.assertEqual(422, (await self.listing(limit=limit)).status_code)

    async def test_an_unknown_workspace_is_a_404(self) -> None:
        r = await self.client.get("/workspaces/nope/files", headers=self.headers)

        self.assertEqual(404, r.status_code)
        self.assertEqual("unknown_workspace", r.json()["code"])


class TestReadingAFile(FilesCase):
    async def test_the_content_comes_back_with_version_and_size(self) -> None:
        raw = b"print('hi')\n"
        self.seed("src/a.py", raw)

        r = await self.read("src/a.py")

        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(
            {"path": "src/a.py", "content": "print('hi')\n", "version": sha(raw), "size": len(raw)}, r.json()
        )

    async def test_a_file_that_is_not_there_is_a_404_with_its_own_code(self) -> None:
        self.seed("dir/x.txt", "x")
        for path in ("nope.py", "dir"):
            with self.subTest(path=path):
                r = await self.read(path)
                self.assertEqual(404, r.status_code)
                self.assertEqual("file_missing", r.json()["code"])

    async def test_a_path_out_of_the_workspace_is_a_400_for_every_spelling(self) -> None:
        self.seed(".git/config", "[core]\n")
        for path in ("../x", "/etc/passwd", ".git/config", "a\x00b"):
            with self.subTest(path=repr(path)):
                r = await self.read(path)
                self.assertEqual(400, r.status_code, r.text)
                self.assertEqual("file_escape", r.json()["code"])

    async def test_each_kind_of_file_it_cannot_show_is_a_422_that_says_which(self) -> None:
        self.seed("logo.png", b"\x89PNG\x00\x00")
        self.seed("latin.txt", "caf\xe9".encode("latin-1"))
        self.seed("big.txt", b"a" * (MAX_EDIT_BYTES + 1))

        codes = {name: (await self.read(name)) for name in ("logo.png", "latin.txt", "big.txt")}

        self.assertEqual(
            {"logo.png": ("file_binary", 422), "latin.txt": ("file_not_text", 422), "big.txt": ("file_too_large", 422)},
            {name: (r.json()["code"], r.status_code) for name, r in codes.items()},
        )

    async def test_the_path_is_required(self) -> None:
        r = await self.client.get(f"/workspaces/{self.ws}/file", headers=self.headers)

        self.assertEqual(422, r.status_code)
        self.assertEqual("invalid_request", r.json()["code"])


class TestSavingAFile(FilesCase):
    async def test_a_save_replaces_the_file_and_returns_the_new_version(self) -> None:
        before = b"old\n"
        target = self.seed("a.txt", before)

        r = await self.save(path="a.txt", content="new\n", base_version=sha(before))

        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual({"path": "a.txt", "version": sha(b"new\n"), "size": 4}, r.json())
        self.assertEqual(b"new\n", target.read_bytes())
        self.assertEqual(sha(b"new\n"), (await self.read("a.txt")).json()["version"])

    async def test_two_saves_in_a_row_work_when_the_second_names_the_first_ones_version(self) -> None:
        first = b"1\n"
        self.seed("a.txt", first)

        one = (await self.save(path="a.txt", content="2\n", base_version=sha(first))).json()
        two = await self.save(path="a.txt", content="3\n", base_version=one["version"])

        self.assertEqual(200, two.status_code, two.text)
        self.assertEqual(b"3\n", (self.project / "a.txt").read_bytes())

    async def test_a_stale_save_is_a_409_that_carries_the_version_to_resolve_against(self) -> None:
        read = b"mine\n"
        target = self.seed("a.txt", read)
        target.write_bytes(b"theirs\n")

        r = await self.save(path="a.txt", content="mine, edited\n", base_version=sha(read))

        self.assertEqual(409, r.status_code, r.text)
        body = r.json()
        self.assertEqual("file_changed", body["code"])
        self.assertEqual(sha(b"theirs\n"), body["current_version"])
        self.assertEqual(b"theirs\n", target.read_bytes(), "a conflicting save wrote anyway")

    async def test_a_save_cannot_create_a_file(self) -> None:
        r = await self.save(path="new.txt", content="hi\n", base_version=sha(b""))

        self.assertEqual(404, r.status_code)
        self.assertEqual("file_missing", r.json()["code"])
        self.assertFalse((self.project / "new.txt").exists())

    async def test_a_path_out_of_the_workspace_is_refused_and_nothing_is_written(self) -> None:
        config = self.seed(".git/config", "[core]\n")
        outside = self.base / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")

        for path in ("../outside.txt", ".git/config", "/etc/hostname", "a\x00b"):
            with self.subTest(path=repr(path)):
                r = await self.save(path=path, content="pwned\n", base_version=sha(b""))
                self.assertEqual(400, r.status_code, r.text)
                self.assertEqual("file_escape", r.json()["code"])

        self.assertEqual(b"[core]\n", config.read_bytes())
        self.assertEqual("secret\n", outside.read_text(encoding="utf-8"))

    async def test_content_a_file_could_not_hold_is_a_422(self) -> None:
        raw = b"keep\n"
        target = self.seed("a.txt", raw)

        nul = await self.save(path="a.txt", content="a\x00b", base_version=sha(raw))
        big = await self.save(path="a.txt", content="a" * (MAX_EDIT_BYTES + 1), base_version=sha(raw))

        self.assertEqual((422, "file_binary"), (nul.status_code, nul.json()["code"]))
        self.assertEqual((422, "file_too_large"), (big.status_code, big.json()["code"]))
        self.assertEqual(raw, target.read_bytes())

    async def test_a_workspace_rooted_somewhere_protected_is_a_400_of_its_own(self) -> None:
        raw = b"x\n"
        target = self.seed("a.txt", raw)

        with patch("engine.fs.protected_root_reason", return_value="it is a system directory"):
            r = await self.save(path="a.txt", content="y\n", base_version=sha(raw))

        self.assertEqual((400, "workspace_protected"), (r.status_code, r.json()["code"]))
        self.assertIn("system directory", r.json()["message"])
        self.assertEqual(raw, target.read_bytes())

    async def test_a_refusal_by_the_system_is_a_422_with_the_reason(self) -> None:
        raw = b"x\n"
        target = self.seed("a.txt", raw)

        with patch("engine.fs.FileSystemService._write_atomic", side_effect=PermissionError(13, "Permission denied")):
            r = await self.save(path="a.txt", content="y\n", base_version=sha(raw))

        self.assertEqual((422, "file_access"), (r.status_code, r.json()["code"]))
        self.assertIn("Permission denied", r.json()["message"])
        self.assertEqual(raw, target.read_bytes())

    async def test_the_body_is_closed_and_complete(self) -> None:
        self.seed("a.txt", "x")
        good = {"path": "a.txt", "content": "y", "base_version": sha(b"x")}

        extra = await self.save(**good, mode="create")
        for field in good:
            missing = await self.save(**{k: v for k, v in good.items() if k != field})
            with self.subTest(missing=field):
                self.assertEqual(422, missing.status_code)

        self.assertEqual(422, extra.status_code, "an unknown field was accepted")
        self.assertEqual(b"x", (self.project / "a.txt").read_bytes())

    async def test_an_unknown_workspace_is_a_404(self) -> None:
        r = await self.client.put(
            "/workspaces/nope/file", headers=self.headers, json={"path": "a", "content": "", "base_version": "0"}
        )

        self.assertEqual(404, r.status_code)
        self.assertEqual("unknown_workspace", r.json()["code"])


if __name__ == "__main__":
    unittest.main()
