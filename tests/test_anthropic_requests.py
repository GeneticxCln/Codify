"""Every request to Anthropic names an API version the API accepts,.

The Messages API takes `anthropic-version: 2023-06-01` (or the older `2023-01-01`) and nothing else
(platform.claude.com/docs/en/api/versioning). The plain call sent `2023-06-01`; the tool-calling call, which is
the conductor's whole loop, sent `2023-11-01`, a version that does not exist, so a conductor on Claude never got
a tool reply. No test had ever made that call, which is how it shipped.
"""
from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import unittest
from typing import Any
from unittest import mock

import httpx

from engine.providers import AnthropicProvider
from engine.toolcall import ToolSpec

ACCEPTED = {"2023-06-01", "2023-01-01"}


class TestTheAnthropicVersionHeader(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.seen: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

        real = httpx.AsyncClient

        def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(answer)
            return real(*args, **kwargs)

        patcher = mock.patch.object(httpx, "AsyncClient", client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = AnthropicProvider("sk-test", "https://api.anthropic.com")

    def _version(self) -> str:
        self.assertEqual(len(self.seen), 1, "the provider did not make exactly one request")
        return self.seen[0].headers["anthropic-version"]

    async def test_a_plain_call_names_an_accepted_version(self) -> None:
        await self.provider.complete("system", "hi", "claude-x", 0.0, 16)
        self.assertIn(self._version(), ACCEPTED)

    async def test_a_tool_call_names_an_accepted_version(self) -> None:
        tools = [ToolSpec(name="read_file", description="Read a file.")]
        reply = await self.provider.complete_with_tools(
            "system", [{"role": "user", "content": "hi"}], tools, "claude-x", 0.0, 16,
        )
        self.assertIn(self._version(), ACCEPTED, "the conductor's loop asks Anthropic for a version it does not have")
        self.assertEqual(reply.text, "ok")


if __name__ == "__main__":
    unittest.main()
