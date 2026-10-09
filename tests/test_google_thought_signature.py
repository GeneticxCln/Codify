"""A Gemini function call goes back to Gemini with the signature it came with.

Gemini 3 models put a `thoughtSignature` on the first `functionCall` part of a step, and the API refuses the next
request (400, "Function call ... is missing a `thought_signature`") when that part comes back without it
(ai.google.dev/gemini-api/docs/thought-signatures). The neutral `ToolCall` held an id, a name and the arguments, so
the signature was dropped on the way in and the conductor's second tool round on Gemini 3 failed every time.
"""
from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import unittest
from typing import Any
from unittest import mock

import httpx

from engine.providers import GoogleProvider
from engine.toolcall import ToolCall, ToolSpec, to_google_contents

TOOLS = [ToolSpec(name="read_file", description="Read a file.")]


class TestTheSignatureMakesTheRoundTrip(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.replies = [
            {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "read_file", "args": {"path": "a.py"}}, "thoughtSignature": "sig-1"},
            ]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "It defines main."}]}}]},
        ]

        def answer(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, json=self.replies[len(self.bodies) - 1])

        real = httpx.AsyncClient

        def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(answer)
            return real(*args, **kwargs)

        patcher = mock.patch.object(httpx, "AsyncClient", client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = GoogleProvider("g-test", "https://generativelanguage.googleapis.com/v1beta")

    async def test_the_second_round_sends_the_first_rounds_signature_back(self) -> None:
        messages: list[dict[str, Any]] = [{"role": "user", "content": "what is in a.py?"}]
        first = await self.provider.complete_with_tools("system", messages, TOOLS, "gemini-3-x", 0.0, 64)
        self.assertEqual([c.name for c in first.tool_calls], ["read_file"])
        # What the conductor appends (engine/conductor.py): the reply as it came, then each result.
        messages.append({"role": "assistant", "content": first.text, "tool_calls": first.tool_calls})
        messages.append({
            "role": "tool", "tool_call_id": first.tool_calls[0].id, "name": "read_file", "content": "def main(): ...",
        })

        second = await self.provider.complete_with_tools("system", messages, TOOLS, "gemini-3-x", 0.0, 64)

        self.assertEqual(second.text, "It defines main.")
        model_turn = self.bodies[1]["contents"][1]
        self.assertEqual(model_turn["role"], "model")
        call_part = next(p for p in model_turn["parts"] if "functionCall" in p)
        self.assertEqual(call_part.get("thoughtSignature"), "sig-1", "Gemini 3 refuses a call sent back unsigned")


class TestTheTranslation(unittest.TestCase):
    def test_a_call_with_no_signature_sends_none(self) -> None:
        """Older Gemini models, and every other provider's calls, carry none: nothing is invented for them."""
        out = to_google_contents([
            {"role": "assistant", "content": "", "tool_calls": [ToolCall("call_0", "read_file", {"path": "a"})]},
        ])
        self.assertEqual(out[0]["parts"], [{"functionCall": {"name": "read_file", "args": {"path": "a"}}}])


if __name__ == "__main__":
    unittest.main()
