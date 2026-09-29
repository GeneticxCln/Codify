"""A credential the client chose the bytes of must be refused, never crash the check.

`secrets.compare_digest` raises `TypeError` on two `str` values when either holds
a non-ASCII character. Header values and websocket messages are attacker-chosen,
so `Authorization: Bearer é` used to escape the auth check as an unhandled
exception: a 500 and a traceback from every route, `/health` included, in place
of the 401 invariant 3 (docs/00 §6.3) promises.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import unittest

import httpx
from httpx import ASGITransport

from engine.app import BOOT_TOKEN, app

NON_ASCII = "Bearer café".encode("latin-1")  # what an HTTP client can actually send


class TestNonAsciiCredentials(unittest.IsolatedAsyncioTestCase):
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
        )

    async def test_a_protected_route_answers_401_not_500(self) -> None:
        async with self._client() as client:
            response = await client.get("/settings/providers", headers={b"Authorization": NON_ASCII})
        self.assertEqual(401, response.status_code)

    async def test_health_is_refused_the_same_way(self) -> None:
        # `/health` is behind the same middleware, so a bad credential is a 401
        # there too; the UI reads that 401 as "engine up, token stale".
        async with self._client() as client:
            response = await client.get("/health", headers={b"Authorization": NON_ASCII})
        self.assertEqual(401, response.status_code)

    async def test_the_real_token_still_works(self) -> None:
        async with self._client() as client:
            response = await client.get(
                "/health", headers={"Authorization": f"Bearer {BOOT_TOKEN}"}
            )
        self.assertEqual({"ok": True, "authenticated": True}, response.json())

    def test_a_websocket_with_such_a_header_is_refused_cleanly(self) -> None:
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        with TestClient(app) as client:
            with self.assertRaises(WebSocketDisconnect) as caught:
                with client.websocket_connect("/ws/engine", headers={b"Authorization": NON_ASCII}) as ws:
                    ws.receive_text()
        self.assertEqual(4401, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
