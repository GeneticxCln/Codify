"""`openai_compat` asks for a key on every call path, or on none (review of 2026-09-29).

`complete` refused an empty key with `missing_api_key`; `complete_with_tools` — the conductor's call — sent
`Authorization: Bearer ` with nothing after it. So a keyless server answered the conductor and refused every
role, and a hosted one got an empty bearer token and a 401 that names nothing. The engine's rule everywhere else
(`needs_key` for any provider that is not a local Ollama, `role_repair.target_needs_key`, discovery) is that
`openai_compat` needs a key; a local server that ignores it takes any placeholder (`benchmarks.seed_endpoint`
stores one and says so). Supporting genuinely keyless local servers would be a product change, not a fix, and is
not made here: both call paths hold the one rule, and neither sends a request without a key.
"""

from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.providers import OpenAICompatProvider, ProviderError
from engine.toolcall import ToolSpec


class CountingServer:
    def __init__(self) -> None:
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                outer.requests += 1
                self.send_response(500)
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                outer.requests += 1
                self.send_response(500)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> CountingServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"


TOOL = ToolSpec(name="t", description="d", parameters={"type": "object", "properties": {}})


class TestTheKeyRule(unittest.IsolatedAsyncioTestCase):
    async def test_a_role_call_without_a_key_is_refused_before_any_request(self) -> None:
        with CountingServer() as server:
            with self.assertRaises(ProviderError) as caught:
                await OpenAICompatProvider("", server.url).complete("s", "u", "m", 0.0, 8)

        self.assertEqual("missing_api_key", caught.exception.code)
        self.assertEqual(0, server.requests)

    async def test_a_tool_call_without_a_key_is_refused_the_same_way(self) -> None:
        with CountingServer() as server:
            with self.assertRaises(ProviderError) as caught:
                await OpenAICompatProvider(None, server.url).complete_with_tools(
                    "s", [{"role": "user", "content": "hi"}], [TOOL], "m", 0.0, 8,
                )

        self.assertEqual("missing_api_key", caught.exception.code)
        self.assertEqual(0, server.requests, "a tool call went out with an empty bearer token")

    async def test_a_placeholder_key_still_works_for_a_local_server(self) -> None:
        with CountingServer() as server:
            with self.assertRaises(ProviderError) as caught:
                await OpenAICompatProvider("local-placeholder", server.url).complete_with_tools(
                    "s", [{"role": "user", "content": "hi"}], [TOOL], "m", 0.0, 8,
                )

        # The server answered 500: what matters is that the request was made.
        self.assertEqual("provider_http", caught.exception.code)
        self.assertGreaterEqual(server.requests, 1)


if __name__ == "__main__":
    unittest.main()
