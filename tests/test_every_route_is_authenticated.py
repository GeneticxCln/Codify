"""Invariant 3, asserted for every route the engine has, not for the ones somebody thought of (docs/00 §6.3).

"Every HTTP/WS request requires `Authorization: Bearer <boot_token>`" was held by a middleware and by
tests of a handful of routes. The middleware makes the promise true today; what nothing checked is that it
stays true for the *next* route — one mounted as a sub-application, a static mount, a `Route` added with
its own handler — which a per-route test cannot catch because nobody has written its test yet.

So this walks `app.routes` itself. Whatever the engine registers is swept: each path, each method, with no
credential, with a wrong one, and with the right token in the places a token does not count.
"""

from __future__ import annotations

import asyncio
import re
import unittest
from collections.abc import Awaitable
from typing import Any
from unittest import mock

import httpx
from httpx import ASGITransport
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import BOOT_TOKEN, app

SKIPPED_METHODS = {"HEAD", "OPTIONS"}
# How long a WebSocket may take to send its auth frame (`ws_engine`, `ws_goal`).
AUTH_WINDOW_S = 5.0


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def http_routes() -> list[tuple[str, str]]:
    """Every (method, concrete path) the engine registers."""
    found = []
    for route in app.routes:
        if isinstance(route, Route):
            for method in sorted(m for m in (route.methods or ()) if m not in SKIPPED_METHODS):
                found.append((method, concrete(route.path)))
    return found


def websocket_paths() -> list[str]:
    return [concrete(r.path) for r in app.routes if isinstance(r, WebSocketRoute)]


class TestEveryHttpRoute(unittest.IsolatedAsyncioTestCase):
    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
        )

    async def test_the_sweep_actually_covers_the_engine(self) -> None:
        routes = http_routes()

        self.assertGreater(len(routes), 40, "the sweep found almost no routes — is it reading app.routes?")
        methods = {m for m, _ in routes}
        self.assertTrue({"GET", "POST", "PUT", "DELETE"} <= methods, methods)
        self.assertIn(("GET", "/health"), routes)

    async def test_no_route_answers_without_a_credential(self) -> None:
        async with self.client() as client:
            leaks = []
            for method, path in http_routes():
                r = await client.request(method, path, json={})
                if r.status_code != 401:
                    leaks.append((method, path, r.status_code))
        self.assertEqual([], leaks, "these routes answered a request that carried no token")

    async def test_no_route_accepts_a_wrong_token(self) -> None:
        async with self.client() as client:
            leaks = []
            for method, path in http_routes():
                r = await client.request(method, path, json={}, headers={"Authorization": "Bearer not-the-token"})
                if r.status_code != 401:
                    leaks.append((method, path, r.status_code))
        self.assertEqual([], leaks)

    async def test_the_right_token_in_the_wrong_place_does_not_count(self) -> None:
        # A token in the query string lands in access logs and history; a different scheme is a different
        # credential. Neither may authenticate, on any route.
        async with self.client() as client:
            leaks = []
            for method, path in http_routes():
                for kwargs in (
                    {"params": {"token": BOOT_TOKEN, "access_token": BOOT_TOKEN}},
                    {"headers": {"Authorization": f"Basic {BOOT_TOKEN}"}},
                    {"headers": {"Authorization": BOOT_TOKEN}},
                    {"headers": {"X-Api-Key": BOOT_TOKEN, "Cookie": f"token={BOOT_TOKEN}"}},
                ):
                    r = await client.request(method, path, json={}, **kwargs)  # type: ignore[arg-type]
                    if r.status_code != 401:
                        leaks.append((method, path, r.status_code, sorted(kwargs)))
        self.assertEqual([], leaks)

    async def test_an_unauthenticated_options_request_reaches_no_handler(self) -> None:
        # A browser's CORS preflight is answered by the CORS layer, outside the token check, and never reaches a
        # route (tests/test_cors.py). Any other OPTIONS without the token is refused like every other request: a
        # 401, never the router's 405 or 404, which would tell a client with no token which routes exist.
        async with self.client() as client:
            leaks = []
            for _, path in http_routes():
                r = await client.options(path)
                if r.status_code != 401:
                    leaks.append((path, r.status_code))
        self.assertEqual([], leaks)

    async def test_the_right_token_still_gets_through(self) -> None:
        async with self.client() as client:
            r = await client.get("/health", headers={"Authorization": f"Bearer {BOOT_TOKEN}"})
        self.assertEqual(200, r.status_code)


class TestEveryWebSocket(unittest.TestCase):
    def test_there_are_websockets_and_the_sweep_sees_them(self) -> None:
        self.assertEqual({"/ws/engine", "/ws/goals/x"}, set(websocket_paths()))

    def test_every_websocket_closes_4401_for_a_frame_that_is_not_the_token(self) -> None:
        with TestClient(app) as client:
            for path in websocket_paths():
                for frame in ('{"type": "auth", "token": "not-the-token"}', "hello", '{"type": "auth"}'):
                    with self.subTest(path=path, frame=frame):
                        with self.assertRaises(WebSocketDisconnect) as caught:
                            with client.websocket_connect(path) as ws:
                                ws.send_text(frame)
                                ws.receive_text()
                        self.assertEqual(4401, caught.exception.code)

    def test_a_socket_that_sends_nothing_is_not_left_open_as_authenticated(self) -> None:
        # The handler waits a bounded time (5 s) for the auth frame and then refuses. Sending no frame at all
        # must end in 4401 too, never in a stream. The wait is shortened for the test and every other
        # `wait_for` is passed through untouched.
        real_wait_for = asyncio.wait_for

        async def shortened(awaitable: Awaitable[Any], timeout: float | None = None) -> Any:
            return await real_wait_for(awaitable, 0.05 if timeout == AUTH_WINDOW_S else timeout)

        with mock.patch.object(asyncio, "wait_for", shortened), TestClient(app) as client:
            for path in websocket_paths():
                with self.subTest(path=path):
                    with self.assertRaises(WebSocketDisconnect) as caught:
                        with client.websocket_connect(path) as ws:
                            ws.receive_text()
                    self.assertEqual(4401, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
