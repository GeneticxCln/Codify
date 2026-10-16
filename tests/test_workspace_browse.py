"""A folder picker that failed is not a picker the user cancelled (audit of 2026-09-29, M8).

`POST /workspaces/browse` ran `python3 -c "import gi…"` and answered `{"cancelled": true}` for *any*
nonzero exit. Without PyGObject (any venv, conda or pyenv Python) or without a display the script died
in 59 ms and the route reported that the user had pressed Cancel; the UI showed nothing at all. First run
was not blocked — a typed path still works — but the button looked broken for no reason anyone could see.

The outcome is now one of four things, and only one of them is silent:

    chosen      a path, and the workspace for it
    cancelled   the person closed the dialog                 -> {"cancelled": true}
    unavailable no dialog could open, and why                 -> 503 picker_unavailable, in words
    (a dialog that never answers is killed with everything it started, and is reported the same way)

The GTK dialog under PyGObject stays first, because it is what already ships and what the process-guard
tests pin; when that reports it cannot run, `zenity` and then `kdialog` are tried, which is what a machine
with a desktop but a venv Python has.

Every dialog here is a stub script on an isolated PATH: a real one would open a window on a developer's
screen.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from httpx import ASGITransport

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import app, lifespan


class BrowseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.chosen = self.base / "state" / "chosen-project"
        self.chosen.mkdir(parents=True)
        env = {
            "CODIFY_HOME": str(self.base / "state"),
            "CODIFY_DB": str(self.base / "state" / "codify.db"),
            "CODIFY_SECRETS": str(self.base / "state" / "secrets.json"),
            # Only the stubs: a developer's real zenity must not be found by a test about its absence.
            "PATH": str(self.bin),
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

    def stub(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    async def browse(self) -> httpx.Response:
        return await self.client.post("/workspaces/browse", headers=self.headers)


class TestThePythonDialogsExitCodesMeanWhatTheyMean(BrowseCase):
    async def test_a_path_on_stdout_is_a_chosen_workspace(self) -> None:
        self.stub("python3", f'echo "{self.chosen}"')

        r = await self.browse()

        self.assertEqual(200, r.status_code, r.text)
        self.assertFalse(r.json()["cancelled"])
        self.assertEqual(str(self.chosen), r.json()["workspace"]["root_path"])

    async def test_a_dialog_closed_with_nothing_chosen_is_the_only_cancel(self) -> None:
        self.stub("python3", "exit 0")

        r = await self.browse()

        self.assertEqual(200, r.status_code)
        self.assertEqual({"cancelled": True}, r.json())

    async def test_missing_pygobject_is_reported_not_mistaken_for_a_cancel(self) -> None:
        # The audit's case: exit 3 is what the picker script says when `import gi` fails.
        self.stub("python3", 'echo "codify-picker:unavailable: PyGObject is not importable" >&2\nexit 3')

        r = await self.browse()

        self.assertEqual(503, r.status_code, r.text)
        body = r.json()
        self.assertEqual("picker_unavailable", body["code"])
        self.assertIn("PyGObject", body["message"])
        self.assertIn("type", body["message"].lower(), "the message must point at the typed-path way in")

    async def test_no_display_is_reported_as_such(self) -> None:
        self.stub("python3", 'echo "codify-picker:no-display: GTK could not open a display" >&2\nexit 4')

        r = await self.browse()

        self.assertEqual(503, r.status_code)
        self.assertIn("display", r.json()["message"])

    async def test_a_crash_is_a_failure_with_the_reason_never_a_cancel(self) -> None:
        self.stub("python3", 'echo "Traceback (most recent call last): boom in gtk" >&2\nexit 1')

        r = await self.browse()

        self.assertEqual(503, r.status_code, "a crashed picker was reported as the user pressing Cancel")
        self.assertEqual("picker_unavailable", r.json()["code"])
        self.assertIn("boom in gtk", r.json()["message"])


class TestTheFallbackDialogs(BrowseCase):
    def python_cannot_show_a_dialog(self) -> None:
        self.stub("python3", 'echo "codify-picker:unavailable: PyGObject is not importable" >&2\nexit 3')

    async def test_zenity_is_used_when_the_gtk_dialog_cannot_run(self) -> None:
        self.python_cannot_show_a_dialog()
        self.stub("zenity", f'echo "{self.chosen}"')

        r = await self.browse()

        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(str(self.chosen), r.json()["workspace"]["root_path"])

    async def test_kdialog_is_used_when_zenity_is_not_installed(self) -> None:
        self.python_cannot_show_a_dialog()
        self.stub("kdialog", f'echo "{self.chosen}"')

        r = await self.browse()

        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(str(self.chosen), r.json()["workspace"]["root_path"])

    async def test_zenity_closed_with_nothing_chosen_is_a_cancel_and_stops_the_search(self) -> None:
        self.python_cannot_show_a_dialog()
        self.stub("zenity", "exit 1")
        self.stub("kdialog", f'echo "{self.chosen}"')  # must NOT be tried after a real cancel

        r = await self.browse()

        self.assertEqual({"cancelled": True}, r.json())

    async def test_zenity_that_cannot_open_a_display_is_a_failure_not_a_cancel(self) -> None:
        # zenity exits 1 both for Cancel and for "cannot open display"; only its stderr tells them apart.
        self.python_cannot_show_a_dialog()
        self.stub("zenity", 'echo "(zenity:1): Gtk-WARNING **: cannot open display: " >&2\nexit 1')

        r = await self.browse()

        self.assertEqual(503, r.status_code, r.text)
        self.assertIn("display", r.json()["message"])

    async def test_nothing_installed_names_what_would_work(self) -> None:
        self.python_cannot_show_a_dialog()

        r = await self.browse()

        self.assertEqual(503, r.status_code)
        message = r.json()["message"]
        for wanted in ("zenity", "kdialog", "PyGObject"):
            self.assertIn(wanted, message)


class TestADialogThatNeverAnswers(BrowseCase):
    async def test_it_is_stopped_with_everything_it_started_and_reported(self) -> None:
        pidfile = self.base / "child.pid"
        self.stub("python3", f'/bin/sleep 300 &\necho $! > "{pidfile}"\nwait')

        with patch("engine.app.PICKER_TIMEOUT_S", 1, create=True):
            started = time.monotonic()
            r = await self.browse()
            took = time.monotonic() - started

        self.assertEqual(503, r.status_code, r.text)
        self.assertIn("did not answer", r.json()["message"])
        self.assertLess(took, 15)
        child = int(pidfile.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        os.kill(child, 9)
        self.fail("a dialog the engine gave up on is still open")


class TestTheRouteStillCreatesTheWorkspace(BrowseCase):
    async def test_choosing_the_same_folder_twice_gives_one_workspace(self) -> None:
        self.stub("python3", f'echo "{self.chosen}"')

        first = (await self.browse()).json()["workspace"]
        second = (await self.browse()).json()["workspace"]

        self.assertEqual(first["id"], second["id"])
        listed: Any = (await self.client.get("/workspaces", headers=self.headers)).json()
        self.assertEqual(1, len(listed))


class TestAFolderWhoseNameIsNotText(BrowseCase):
    """A dialog that worked, and a person who chose, must never end in a plain-text 500.

    The dialogs were read with `text=True`, so `communicate` decoded their output itself and a folder whose name is not
    UTF-8 (an unzipped Latin-1 archive) raised `UnicodeDecodeError` there, where only `TimeoutExpired` was caught. Every
    path the engine keeps, sends to a model or stores is text, so such a folder cannot be a workspace; the person is
    told that in a sentence, with the route's existing error shape, and no workspace is made.
    """

    async def assertTheSentence(self, r: httpx.Response) -> None:
        self.assertEqual(400, r.status_code, r.text)
        body = r.json()
        self.assertEqual("invalid_root", body["code"], "the code a typed path that is not a usable folder gets")
        self.assertIn("not valid UTF-8", body["message"])
        self.assertIn("bad\\xffdir", body["message"], "the folder is named with its byte written out, not guessed at")
        self.assertIn("Rename", body["message"])
        listed: Any = (await self.client.get("/workspaces", headers=self.headers)).json()
        self.assertEqual([], listed, "a workspace was made out of a path that cannot be said as text")

    async def test_the_gtk_dialog_choosing_one_is_told_in_words(self) -> None:
        self.stub("python3", "printf '/tmp/bad\\377dir\\n'")

        await self.assertTheSentence(await self.browse())

    async def test_the_fallback_dialogs_are_told_the_same_way(self) -> None:
        self.stub("python3", 'echo "codify-picker:unavailable: PyGObject is not importable" >&2\nexit 3')
        self.stub("zenity", "printf '/tmp/bad\\377dir\\n'")
        self.stub("kdialog", f'echo "{self.chosen}"')  # a dialog that answered is not asked again

        await self.assertTheSentence(await self.browse())

    async def test_an_encoded_surrogate_is_not_text_either(self) -> None:
        # CESU-style bytes for U+D800: Python's decoder refuses them, and so must the route.
        self.stub("python3", "printf '/tmp/bad\\355\\240\\200dir\\n'")

        r = await self.browse()

        self.assertEqual(400, r.status_code, r.text)
        self.assertEqual("invalid_root", r.json()["code"])
        self.assertIn("\\xed\\xa0\\x80", r.json()["message"])

    async def test_a_dialog_that_says_something_that_is_not_text_on_stderr_is_still_a_failure_in_words(self) -> None:
        self.stub("python3", "printf 'boom \\377 in gtk\\n' >&2\nexit 1")

        r = await self.browse()

        self.assertEqual(503, r.status_code, r.text)
        self.assertEqual("picker_unavailable", r.json()["code"])
        self.assertIn("boom", r.json()["message"])

    async def test_a_folder_with_a_name_that_is_text_but_not_ascii_is_still_chosen(self) -> None:
        folder = self.base / "state" / "projé-日本"
        folder.mkdir()
        self.stub("python3", f'echo "{folder}"')

        r = await self.browse()

        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(str(folder), r.json()["workspace"]["root_path"])

    def test_the_gtk_script_writes_the_chosen_path_as_bytes(self) -> None:
        # `print` would raise on a surrogate escape, after the person had chosen: the script writes the bytes it was
        # given and leaves it to the engine to say what it cannot use.
        from engine.app import _picker_command

        argv, _env = _picker_command()
        script = argv[argv.index("-c") + 1]

        self.assertIn("sys.stdout.buffer.write(os.fsencode(dialog.get_filename())", script)
        self.assertNotIn("print(dialog.get_filename())", script)


if __name__ == "__main__":
    unittest.main()
