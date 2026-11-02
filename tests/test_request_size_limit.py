"""A request body over 8 MiB is refused with one 413, before a route can buffer it (audit gap 5).

The engine had no ceiling on the bytes it would read from a request. A client that streams a body with no
Content-Length is the case a header check cannot see, so the middleware counts `http.request` chunks as they
arrive. The Content-Length fast path is still there: an announced oversize body is refused without pulling a
byte.

The answer is the engine's `{code, message}` shape, not a bare 413, and it is registered *inside* auth: a
request with no token is a 401 and its body is never read. `fs.MAX_EDIT_BYTES` remains the authority for a
file's content — this is a transport ceiling, not an editor limit.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, cast

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from starlette.types import Message, Scope

from engine.app import BOOT_TOKEN, MAX_REQUEST_BODY_BYTES, app

MIB = 1024 * 1024


def scope(headers: list[tuple[bytes, bytes]]) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/goals",
        "raw_path": b"/goals",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"authorization", f"Bearer {BOOT_TOKEN}".encode()), *headers],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 7430),
    }


async def call_app(
    chunks: list[bytes] | None, headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[list[Message], int]:
    """Invoke the real ASGI stack and return (sent messages, how many body reads happened)."""
    sent: list[Message] = []
    reads = 0
    remaining = list(chunks or [])

    async def receive() -> Any:
        nonlocal reads
        reads += 1
        if remaining:
            body = remaining.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(remaining)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Any) -> None:
        sent.append(cast(Message, message))

    app.state.token = BOOT_TOKEN
    await app(scope(headers or []), receive, send)
    return sent, reads


def response(sent: list[Message]) -> tuple[int, dict[str, Any]]:
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return int(start["status"]), json.loads(body)


class RequestSizeLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_streamed_oversize_body_with_no_content_length_is_413(self) -> None:
        chunks = [b"x" * MIB for _ in range(9)]

        sent, reads = await call_app(chunks)

        status, body = response(sent)
        self.assertEqual(413, status)
        self.assertEqual("request_too_large", body["code"])
        self.assertIn(str(MAX_REQUEST_BODY_BYTES), body["message"])
        self.assertEqual(9, reads, "the middleware stopped reading before the body crossed the ceiling")

    async def test_an_announced_oversize_body_is_refused_without_reading_it(self) -> None:
        headers = [(b"content-length", str(MAX_REQUEST_BODY_BYTES + 1).encode())]

        sent, reads = await call_app([b"x"], headers=headers)

        status, body = response(sent)
        self.assertEqual(413, status)
        self.assertEqual("request_too_large", body["code"])
        self.assertEqual(0, reads, "an announced oversize body should not be pulled at all")

    async def test_a_body_under_the_ceiling_still_reaches_the_route(self) -> None:
        sent, _ = await call_app([b"{}"])

        status, body = response(sent)
        self.assertNotEqual(413, status)
        self.assertEqual("invalid_request", body["code"])


if __name__ == "__main__":
    unittest.main()
