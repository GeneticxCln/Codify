"""`/ws/engine` leaves nothing behind when its handler is cancelled (second pass).

Found as an intermittent `CancelledError` in `test_catalog_watch` under CPU load, which turned out to be two
things. Starlette's test client sends the disconnect and then *immediately* cancels the app's scope, so a
handler that needs a few loop turns to notice can be cancelled mid-wait — and that is a legal thing to do to
a handler (the server stopping does it). The handler was not ready for it: its `queue.get()` and
`receive_text()` tasks were only cleaned on the path where the wait *finished*, so a cancelled handler left
both running, and the event loop printed "Task exception was never retrieved" for the receive that later
ended in the disconnect. The tests that used the client now wait for the endpoint to finish before leaving
(`tests/test_catalog_watch.py`); this is the engine's half.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, cast

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from starlette.websockets import WebSocket

from engine.app import ws_engine


class _Watch:
    def __init__(self) -> None:
        self.subscribers = 0

    def subscribe(self) -> None:
        self.subscribers += 1

    def unsubscribe(self) -> None:
        self.subscribers -= 1


class _Socket:
    """Just enough of a WebSocket for `ws_engine`: authenticated by header, silent, never closing."""

    def __init__(self, token: str) -> None:
        self.headers = {"authorization": f"Bearer {token}"}
        self.watch = _Watch()
        self.conns: set[Any] = set()
        self.app = SimpleNamespace(
            state=SimpleNamespace(token=token, engine_conns=self.conns, catalog_watch=self.watch)
        )
        self.received: asyncio.Event | None = None
        self.sent: list[str] = []

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        await asyncio.Event().wait()  # a client that says nothing, forever
        raise AssertionError("unreachable")

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        return None


class TestACancelledHandlerLeavesNothingRunning(unittest.IsolatedAsyncioTestCase):
    async def _start(self) -> tuple[asyncio.Task[None], _Socket]:
        socket = _Socket("t" * 64)
        handler = asyncio.create_task(ws_engine(cast(WebSocket, socket)))
        for _ in range(50):
            if socket.watch.subscribers == 1:
                break
            await asyncio.sleep(0)
        self.assertEqual(1, socket.watch.subscribers, "the handler never subscribed")
        # One more turn: the loop has created its two tasks and is waiting on them.
        await asyncio.sleep(0)
        return handler, socket

    async def test_no_task_outlives_a_cancelled_handler(self) -> None:
        before = asyncio.all_tasks()
        handler, socket = await self._start()

        handler.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await handler
        await asyncio.sleep(0)

        leaked = asyncio.all_tasks() - before - {asyncio.current_task()}
        self.assertEqual(set(), leaked, "the handler's queue and receive tasks kept running after it was cancelled")

    async def test_a_cancelled_handler_still_unsubscribes(self) -> None:
        handler, socket = await self._start()

        handler.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await handler

        self.assertEqual(0, socket.watch.subscribers, "the engine would keep polling providers for a client that left")
        self.assertEqual(set(), socket.conns, "the queue stayed registered for frames nobody will read")

    async def test_the_finished_state_is_only_reached_when_the_children_are_gone(self) -> None:
        # `subscribers == 0` is what a caller waits on to know the handler is done, so it must not be
        # true while a child task is still running.
        handler, socket = await self._start()
        before = asyncio.all_tasks() - {handler}

        handler.cancel()
        for _ in range(50):
            if socket.watch.subscribers == 0:
                break
            await asyncio.sleep(0)

        running = {t for t in asyncio.all_tasks() - before - {asyncio.current_task(), handler} if not t.done()}
        self.assertEqual(set(), running, "unsubscribed while a child task was still running")
        with self.assertRaises(asyncio.CancelledError):
            await handler
