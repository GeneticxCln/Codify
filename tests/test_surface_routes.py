"""The surface bridge's three routes: the app window polls, answers, and asks what it may be asked.

    GET  /surfaces/state          who is attached, what is in flight, which surfaces and ops exist
    GET  /surfaces/next?wait=     the oldest unanswered question, or {"id": null} (a long poll: the window's heartbeat)
    POST /surfaces/answer         {id, ok, result?, error?} extra=forbid -> {"accepted": bool}

A long poll rather than a socket for the reason the browser bridge is one: every WebSocket in this engine carries one
goal's events and nothing else, and a second long-lived socket is how two streams get interleaved
(`tests/stream_isolation.py`). Authentication is not repeated here; `test_every_route_is_authenticated` sweeps these.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from httpx import ASGITransport

from engine.app import app, lifespan


class RouteCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        env = {
            "CODIFY_HOME": str(base / "state"),
            "CODIFY_DB": str(base / "state" / "codify.db"),
            "CODIFY_SECRETS": str(base / "state" / "secrets.json"),
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

    async def next(self, wait: float = 0.0) -> httpx.Response:
        return await self.client.get("/surfaces/next", headers=self.headers, params={"wait": wait})

    async def answer(self, **body: object) -> httpx.Response:
        return await self.client.post("/surfaces/answer", headers=self.headers, json=body)


class TestPolling(RouteCase):
    async def test_the_engine_has_one_bridge_and_the_executor_shares_it(self) -> None:
        self.assertIs(app.state.surfaces, app.state.executor.surfaces)

    async def test_state_before_any_poll_says_nobody_is_there_and_what_could_be_asked(self) -> None:
        r = await self.client.get("/surfaces/state", headers=self.headers)

        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertFalse(body["attached"])
        self.assertEqual(0, body["inflight"])
        self.assertEqual({"editor": ["edit", "open", "read"]}, body["surfaces"])

    async def test_an_empty_poll_is_a_null_id_and_it_is_what_makes_the_window_attached(self) -> None:
        r = await self.next()

        self.assertEqual({"id": None}, r.json())
        self.assertTrue((await self.client.get("/surfaces/state", headers=self.headers)).json()["attached"])

    async def test_the_wait_is_bounded(self) -> None:
        for wait in (-1, 61):
            with self.subTest(wait=wait):
                self.assertEqual(422, (await self.next(wait)).status_code)


class TestARoundTripOverHttp(RouteCase):
    async def test_a_question_comes_out_of_the_poll_and_its_answer_goes_back_in(self) -> None:
        await self.next()  # attach
        asking = asyncio.create_task(
            app.state.surfaces.ask("editor", "open", {"path": "src/a.py", "line": 3}, workspace_id="ws-1")
        )
        await asyncio.sleep(0)

        polled = (await self.next(2.0)).json()
        reply = await self.answer(id=polled["id"], ok=True, result={"path": "src/a.py", "opened": True, "shown": "beside", "from_line": 3, "to_line": 3})
        result = await asking

        self.assertEqual(("editor", "open", "ws-1"), (polled["surface"], polled["op"], polled["workspace_id"]))
        self.assertEqual({"path": "src/a.py", "line": 3, "end_line": None}, polled["args"])
        self.assertEqual({"accepted": True}, reply.json())
        self.assertEqual("beside", result.shown)

    async def test_a_refusal_posted_as_not_ok_reaches_the_asker(self) -> None:
        from engine.surfaces import SurfaceRefused

        await self.next()
        asking = asyncio.create_task(app.state.surfaces.ask("editor", "read", {}, workspace_id="ws-1"))
        await asyncio.sleep(0)
        polled = (await self.next(2.0)).json()

        await self.answer(id=polled["id"], ok=False, error="the editor is busy")

        with self.assertRaises(SurfaceRefused) as caught:
            await asking
        self.assertIn("the editor is busy", str(caught.exception))

    async def test_an_answer_for_a_question_that_is_not_waiting_is_accepted_false_not_an_error(self) -> None:
        r = await self.answer(id="nothing-asked-this", ok=True, result={})

        self.assertEqual(200, r.status_code)
        self.assertEqual({"accepted": False}, r.json())


class TestTheBodyIsClosed(RouteCase):
    async def test_an_unknown_field_is_refused_the_way_every_other_body_is(self) -> None:
        r = await self.answer(id="x", ok=True, result={}, role="system")

        self.assertEqual(422, r.status_code)
        self.assertEqual("invalid_request", r.json()["code"])

    async def test_an_id_is_required(self) -> None:
        self.assertEqual(422, (await self.answer(ok=True, result={})).status_code)


if __name__ == "__main__":
    unittest.main()
