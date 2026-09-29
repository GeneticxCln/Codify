"""`ws_goal` closes a socket that may already be gone, quietly (review of 2026-09-29).

`ws_engine` wraps its refusals in `except (WebSocketDisconnect, RuntimeError)`: closing a WebSocket the peer has
already hung up on raises, and an exception escaping a handler is a traceback in the engine's log for a client
that merely left — which trains people to stop reading the log. `ws_goal` had three closes with no such guard
(the bad-token refusal and both "no such goal" answers). The handler is driven directly with a socket whose
`close` raises the way a departed peer's does, because the ASGI test client cancels the handler first and hides
exactly this.
"""

from __future__ import annotations

import asyncio
import types
import unittest
from typing import Any

from starlette.websockets import WebSocketDisconnect

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import BOOT_TOKEN, ws_goal
from engine.services import ApiError


class GoneSocket:
    """A WebSocket whose peer has already hung up: every close raises, as Starlette's does."""

    def __init__(self, *, bearer: bool, goal_exists: bool, raises: type[Exception]) -> None:
        self.headers = {"authorization": f"Bearer {BOOT_TOKEN}"} if bearer else {}
        self.closed_with: list[int] = []
        self._raises = raises

        class Goals:
            def get(self_inner, goal_id: str) -> Any:  # noqa: N805 — a tiny stand-in, `self_inner` is the goals object
                if not goal_exists:
                    raise ApiError(404, "not_found", "no such goal")
                return object()

            def events_after(self_inner, *a: Any, **k: Any) -> list[Any]:  # noqa: N805
                return []

        self.app = types.SimpleNamespace(state=types.SimpleNamespace(token=BOOT_TOKEN, goals=Goals()))

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        raise WebSocketDisconnect(1001)  # the peer left inside the auth window

    async def send_text(self, text: str) -> None:
        raise self._raises("Unexpected ASGI message 'websocket.send', after sending 'websocket.close'")

    async def close(self, code: int = 1000) -> None:
        self.closed_with.append(code)
        raise self._raises("Unexpected ASGI message 'websocket.close', after sending 'websocket.close'")


class TestQuietClose(unittest.IsolatedAsyncioTestCase):
    async def run_handler(self, ws: GoneSocket) -> None:
        await asyncio.wait_for(ws_goal(ws, "some-goal"), 5)  # type: ignore[arg-type]

    async def test_a_bad_token_from_a_peer_that_has_left_is_refused_without_a_traceback(self) -> None:
        for raises in (RuntimeError, WebSocketDisconnect):
            with self.subTest(raises=raises.__name__):
                ws = GoneSocket(bearer=False, goal_exists=True, raises=raises)

                await self.run_handler(ws)

                self.assertEqual([4401], ws.closed_with)

    async def test_a_goal_that_does_not_exist_is_answered_without_a_traceback(self) -> None:
        for raises in (RuntimeError, WebSocketDisconnect):
            with self.subTest(raises=raises.__name__):
                ws = GoneSocket(bearer=True, goal_exists=False, raises=raises)

                await self.run_handler(ws)

                self.assertEqual([4404], ws.closed_with)


if __name__ == "__main__":
    unittest.main()
