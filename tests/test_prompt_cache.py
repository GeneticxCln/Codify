"""The conductor's system prompt is the one prefix written to a prompt cache and read back (`docs/16` item 7).

The conductor re-sends one system prompt on every call of a turn, up to the turn cap. Anthropic is the one protocol
here with a field that marks a block cacheable, so that is the one request that carries `cache_control` — on the
system block alone. A one-shot role call never asks: its write premium would be paid and never read. A provider that
refuses the mark is retried once without it and then left alone, the way a rejected `response_format` falls back.

These tests go through the real provider and the real conductor over a mock HTTP transport, because the field is
made in the request body and a double that accepted the keyword would prove nothing about the body.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import unittest
from typing import Any
from unittest import mock

import httpx

from engine.conductor import Conductor
from engine.providers import AnthropicProvider, ProviderError
from engine.toolcall import ToolSpec

SYSTEM = "You are a test conductor."
TOOLS = [ToolSpec(name="read_file", description="Read a file.")]


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": 5, "output_tokens": 2}}


class _Messages:
    """A Messages endpoint: records every body, answers from a queue, then with a plain reply."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.replies: list[tuple[int, dict[str, Any]]] = []

    def reply(self, body: dict[str, Any], status: int = 200) -> None:
        self.replies.append((status, body))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content or b"{}"))
        status, body = self.replies.pop(0) if self.replies else (200, _text("ok"))
        return httpx.Response(status, json=body)


class CachePrefixCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = _Messages()
        real = httpx.AsyncClient

        def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(self.server)
            return real(*args, **kwargs)

        patcher = mock.patch.object(httpx, "AsyncClient", client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = AnthropicProvider("sk-test", "https://api.anthropic.com")

    def conductor(self) -> Conductor:
        return Conductor(self.provider, "claude-x", ".", list(TOOLS), {}, system_prompt=SYSTEM, max_turns=1)

    async def test_the_conductors_request_marks_the_system_block_and_nothing_else(self) -> None:
        self.server.reply(_text("It parses."))

        answer = await self.conductor().run("what does it do")

        self.assertEqual("It parses.", answer)
        body = self.server.bodies[0]
        self.assertEqual(
            [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            body["system"],
            "the conductor's stable prefix was not marked cacheable",
        )
        others = {key: value for key, value in body.items() if key != "system"}
        self.assertNotIn("cache_control", json.dumps(others), "the mark belongs on the system block alone")

    async def test_a_role_call_never_asks_for_a_cache(self) -> None:
        self.server.reply(_text("a plan"))

        await self.provider.complete(SYSTEM, "go", "claude-x", 0.0, 64)

        body = self.server.bodies[0]
        self.assertEqual(SYSTEM, body["system"], "a one-shot role call sent a cacheable block")
        self.assertNotIn("cache_control", json.dumps(body))

    async def test_a_tool_call_that_does_not_ask_is_not_marked(self) -> None:
        # The default is off: only a caller that will re-send the prompt (the conductor) opts in.
        self.server.reply(_text("ok"))

        await self.provider.complete_with_tools(SYSTEM, [{"role": "user", "content": "hi"}], TOOLS, "claude-x", 0.0, 16)

        self.assertEqual(SYSTEM, self.server.bodies[0]["system"])

    async def test_a_rejected_mark_is_dropped_and_the_call_retried_once(self) -> None:
        self.server.reply({"error": {"type": "invalid_request_error", "message": "cache_control: unexpected field"}}, 400)
        self.server.reply(_text("It parses."))

        answer = await self.conductor().run("what does it do")

        self.assertEqual("It parses.", answer)
        self.assertEqual(2, len(self.server.bodies), "the rejected request was not retried without the mark")
        self.assertEqual([{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}], self.server.bodies[0]["system"])
        self.assertEqual(SYSTEM, self.server.bodies[1]["system"])
        self.assertNotIn("cache_control", json.dumps(self.server.bodies[1]))

        # The refusal is remembered for this endpoint: the next call does not pay the rejected request again.
        self.server.reply(_text("Later."))
        self.assertEqual("Later.", await self.conductor().run("again"))
        self.assertEqual(SYSTEM, self.server.bodies[2]["system"])

    async def test_a_second_rejection_is_not_retried_again(self) -> None:
        self.server.reply({"error": {"type": "invalid_request_error", "message": "cache_control: unexpected field"}}, 400)
        self.server.reply({"error": {"type": "invalid_request_error", "message": "still unexpected"}}, 400)

        with self.assertRaises(ProviderError):
            await self.provider.complete_with_tools(
                SYSTEM, [{"role": "user", "content": "hi"}], TOOLS, "claude-x", 0.0, 16, cache_prefix=True,
            )

        self.assertEqual(2, len(self.server.bodies), "a refused fallback must surface, not loop")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
