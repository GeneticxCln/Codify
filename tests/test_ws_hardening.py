"""WebSocket handshakes are bounded before they can hold anything (audit gap 4).

Three holes were open at the handshake, not in the streams themselves. Auth was copied into
`ws_engine` and `ws_goal`, so the two could drift. A browser-chosen `Origin` was ignored, so a page
on any site could open a socket and present a token it had somehow read. And the auth wait was
unbounded in number: every silent socket held a coroutine and a descriptor for five seconds.

The stream-isolation suite (`tests/test_concurrent_streams_ws.py`, through
`tests/stream_isolation.py`) proves that a *legitimate* connection sees only its own goal's frames.
These tests pin the boundary in front of it: a foreign origin gets 4403, a socket over the pending
cap gets 4429, and uvicorn closes an oversized frame with 1009 before it reaches a handler. A client
with no Origin — the Tauri shell, a command-line test — is still admitted by its token.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import unittest
from typing import Any
from unittest import mock

import uvicorn
import websockets
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import (
    BOOT_TOKEN,
    WS_MAX_MESSAGE_BYTES,
    WS_MAX_PENDING,
    WS_ORIGIN_REFUSED,
    WS_TOO_MANY_PENDING,
    WS_WAIT_SHED,
    app,
)

AUTH = {"authorization": f"Bearer {BOOT_TOKEN}"}


def close_code(exc: websockets.ConnectionClosed) -> int | None:
    """The code the peer sent, without the deprecated `ConnectionClosed.code` property."""
    return exc.rcvd.code if exc.rcvd is not None else None


class _Watch:
    """Just enough of a catalogue watch to subscribe and be waited on."""

    def __init__(self) -> None:
        self.subscribers = 0

    def subscribe(self) -> None:
        self.subscribers += 1

    def unsubscribe(self) -> None:
        self.subscribers = max(0, self.subscribers - 1)


def _leave(ws: Any, app: Any) -> None:
    """Close, then wait until the endpoint has finished before the client's `with` exits.

    Starlette's test client sends the disconnect and immediately cancels the app's scope. An
    endpoint that needs a few loop turns to notice is cancelled mid-teardown whenever the machine
    is busy, and the context then ends in `CancelledError` instead of an assertion. `subscribers`
    reaching zero is the endpoint's last act, so waiting on it lets the cancel land on a handler
    that has already returned. This is the same reason `test_catalog_watch.py` waits to leave.
    """
    ws.close(1000)
    deadline = time.monotonic() + 10
    while app.state.catalog_watch.subscribers and time.monotonic() < deadline:
        time.sleep(0.005)


class TestTheHandshake(unittest.TestCase):
    def test_a_foreign_origin_is_refused_on_both_endpoints(self) -> None:
        with TestClient(app) as client:
            for path in ("/ws/engine", "/ws/goals/x"):
                with self.subTest(path=path):
                    with self.assertRaises(WebSocketDisconnect) as caught:
                        with client.websocket_connect(
                            path, headers={"origin": "http://evil.example"}
                        ) as ws:
                            ws.receive_text()
                    self.assertEqual(WS_ORIGIN_REFUSED, caught.exception.code)

    def test_an_allowed_origin_and_a_client_with_no_origin_are_admitted(self) -> None:
        with TestClient(app) as client:
            # :4173 is the preview server's documented fallback. HTTP CORS already admits any
            # loopback port; the WebSocket handshake has to agree, or a preview tab gets HTTP and
            # no live updates.
            for extra in (
                {"origin": "http://tauri.localhost"},
                {"origin": "http://localhost:4173"},
                {},
            ):
                with self.subTest(origin=extra.get("origin")):
                    with client.websocket_connect("/ws/engine", headers={**AUTH, **extra}) as ws:
                        # The handshake is the assertion. No frame is owed before the catalogue changes.
                        ws.send_text("ignored")
                        _leave(ws, app)


class ServerCase(unittest.IsolatedAsyncioTestCase):
    """A real uvicorn server, because `ws_max_size` is its setting and TestClient does not enforce it."""

    async def asyncSetUp(self) -> None:
        app.state.token = BOOT_TOKEN
        app.state.engine_conns = set()
        app.state.catalog_watch = _Watch()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            lifespan="off",
            timeout_graceful_shutdown=1,
            ws_max_size=WS_MAX_MESSAGE_BYTES,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve()), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        self.assertTrue(self.server.started, "the test server never came up")
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/engine"


class TestTheFrameAndSocketBounds(ServerCase):
    async def test_an_oversized_first_frame_is_closed_with_1009(self) -> None:
        wire = await websockets.connect(self.url, additional_headers=AUTH)

        with self.assertRaises(websockets.ConnectionClosed) as caught:
            await wire.send("x" * (WS_MAX_MESSAGE_BYTES + 1))
            await wire.recv()

        closed = caught.exception
        assert isinstance(closed, websockets.ConnectionClosed)
        self.assertEqual(1009, close_code(closed), "uvicorn did not enforce ws_max_size")

    async def test_a_silent_flood_cannot_lock_out_a_first_frame_client(self) -> None:
        """The UI authenticates with a frame, not a header: a browser cannot set one.

        Sixteen silent sockets used to take every pending slot for the whole five-second wait,
        so the UI's own first-frame client got 4429 while a local process reopened the flood.
        A silent socket now takes no slot; only a delivered frame is counted.
        """
        held = []
        for _ in range(WS_MAX_PENDING * 2):
            held.append(await websockets.connect(self.url))
            await asyncio.sleep(0.02)
        try:
            ui = await websockets.connect(self.url)
            await ui.send(json.dumps({"type": "auth", "token": BOOT_TOKEN}))
            await asyncio.sleep(0.1)
            self.assertIsNone(
                ui.close_code,
                "a silent flood locked out the UI's first-frame authentication",
            )
            await ui.close()
        finally:
            for wire in held:
                await wire.close()
        await asyncio.sleep(0.2)

        good = await websockets.connect(self.url)
        await good.send(json.dumps({"type": "auth", "token": BOOT_TOKEN}))
        await asyncio.sleep(0.1)
        self.assertIsNone(good.close_code, "closing the flood did not release the auth checks")
        await good.close()

    async def test_the_cap_still_bounds_a_delivered_frame(self) -> None:
        app.state.ws_pending = WS_MAX_PENDING
        try:
            extra = await websockets.connect(self.url)
            await extra.send(json.dumps({"type": "auth", "token": BOOT_TOKEN}))
            with self.assertRaises(websockets.ConnectionClosed) as caught:
                await extra.recv()
            closed = caught.exception
            assert isinstance(closed, websockets.ConnectionClosed)
            self.assertEqual(WS_TOO_MANY_PENDING, close_code(closed))
        finally:
            app.state.ws_pending = 0


class TestSilentSocketsAreBounded(ServerCase):
    """A silent socket takes no pending slot, so the descriptors it holds are bounded another way."""

    async def test_the_longest_waiting_silent_socket_is_shed_and_the_newest_is_never_refused(self) -> None:
        with mock.patch("engine.app.WS_MAX_WAITING", 4):
            held = []
            for _ in range(4):
                held.append(await websockets.connect(self.url))
                await asyncio.sleep(0.03)
            # One more than the bound: the OLDEST is closed, the new one is admitted and can still authenticate.
            newest = await websockets.connect(self.url)
            await newest.send(json.dumps({"type": "auth", "token": BOOT_TOKEN}))
            with self.assertRaises(websockets.ConnectionClosed) as shed:
                await asyncio.wait_for(held[0].recv(), 5)
            closed = shed.exception
            assert isinstance(closed, websockets.ConnectionClosed)
            self.assertEqual(WS_WAIT_SHED, close_code(closed))
            await asyncio.sleep(0.1)
            self.assertIsNone(newest.close_code, "the newest socket was refused instead of the oldest shed")
            for wire in (*held[1:], newest):
                await wire.close()

    async def test_a_socket_that_has_sent_its_frame_is_no_longer_waiting(self) -> None:
        with mock.patch("engine.app.WS_MAX_WAITING", 2):
            ui = await websockets.connect(self.url)
            await ui.send(json.dumps({"type": "auth", "token": BOOT_TOKEN}))
            await asyncio.sleep(0.1)
            flood = [await websockets.connect(self.url) for _ in range(6)]
            await asyncio.sleep(0.2)
            self.assertIsNone(ui.close_code, "a flood shed a socket that had already authenticated")
            for wire in flood:
                await wire.close()
            await ui.close()


if __name__ == "__main__":
    unittest.main()
