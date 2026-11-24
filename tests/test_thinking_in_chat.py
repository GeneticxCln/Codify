"""Thinking reaches the chat path: the conductor's tool calls, not only a role's single-shot `complete`.

Found by a person asking why cloud models never "think" in chat. `thinking_effort` was a per-role setting that only
`complete` read; the conductor answers every turn through `complete_with_tools`, which took no thinking setting in
any of the four protocols. Turning it on is not the whole fix. With thinking on, Anthropic returns signed `thinking`
blocks and refuses a follow-up request whose assistant tool-use turn does not lead with them unchanged, so a turn that
reads a file and then answers needs those blocks kept and echoed (`ToolReply.reasoning`). The fake below enforces that
rule the way the API documents it, and the negative control proves it is strict.

What this cannot prove: that a real cloud model accepts these requests. No API key is available to the suite (and
must not be); the wire shapes follow each provider's documentation and the shapes `complete` already sends.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from engine.conductor import CALL_MAX_TOKENS, Conductor
from engine.providers import (
    AnthropicProvider,
    GoogleProvider,
    OllamaProvider,
    OpenAICompatProvider,
    ProviderError,
    thinking_headroom,
)
from engine.toolcall import ToolCall, ToolReply, ToolSpec

READ = ToolSpec(
    name="read_file",
    description="read a file",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)
THINKING_BLOCK = {"type": "thinking", "thinking": "The answer is in app.py.", "signature": "sig-abc123"}


class _Fake(ThreadingHTTPServer):
    """One loopback server for the four protocols, recording every POST body.

    `anthropic` is a list of scripted replies, consumed in order. When a request asks for thinking, every assistant
    turn that carries a `tool_use` must lead with a thinking block, or the reply is the 400 the API documents.
    """

    def __init__(self) -> None:
        self.bodies: list[tuple[str, dict[str, Any]]] = []
        self.anthropic: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # noqa: ARG002 — quiet in the suite
                return

            def do_POST(self) -> None:
                server = cast(_Fake, self.server)
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                server.bodies.append((self.path, body))
                status, reply = 200, server.answer(self.path, body)
                if reply is None:
                    status, reply = 400, {"type": "error", "error": {
                        "type": "invalid_request_error",
                        "message": "Expected `thinking` or `redacted_thinking`, but found `tool_use`.",
                    }}
                raw = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def answer(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if path.endswith("/v1/messages"):
            if "thinking" in body:
                for m in body.get("messages", []):
                    content = m.get("content")
                    if m.get("role") == "assistant" and isinstance(content, list) and any(
                        b.get("type") == "tool_use" for b in content
                    ) and content[0].get("type") not in ("thinking", "redacted_thinking"):
                        return None
            return self.anthropic.pop(0) if self.anthropic else {"content": [{"type": "text", "text": "done"}]}
        if path.endswith("/chat/completions"):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        if path.endswith("/api/chat"):
            return {"message": {"role": "assistant", "content": "ok"}}
        if ":generateContent" in path:
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        return {}


class _Recorder:
    """A tool-capable provider that remembers how it was called."""

    supports_tools = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete_with_tools(self, system_prompt: str, messages: Any, tools: Any, model: str,
                                  temperature: float, max_tokens: int, **kwargs: Any) -> ToolReply:
        self.calls.append({"max_tokens": max_tokens, **kwargs})
        return ToolReply(text="hello")


class TestTheConductorAsksForThinking(unittest.IsolatedAsyncioTestCase):
    async def test_off_sends_nothing_and_the_call_is_the_one_it_always_was(self) -> None:
        provider = _Recorder()
        await Conductor(provider, "m", "/tmp", system_prompt="s").run("hi")
        self.assertNotIn("thinking_effort", provider.calls[0], "a model with no thinking control was sent one")
        self.assertEqual(CALL_MAX_TOKENS, provider.calls[0]["max_tokens"])

    async def test_on_sends_the_level_and_room_for_its_whole_budget(self) -> None:
        # Capped at 2048, every level was clamped to the same 1024-token budget: "high" thought as much as "low".
        for effort in ("low", "medium", "high"):
            provider = _Recorder()
            await Conductor(provider, "m", "/tmp", system_prompt="s", thinking_effort=effort).run("hi")
            self.assertEqual(effort, provider.calls[0]["thinking_effort"])
            self.assertEqual(CALL_MAX_TOKENS + thinking_headroom(effort), provider.calls[0]["max_tokens"])
        self.assertLess(thinking_headroom("low"), thinking_headroom("medium"))
        self.assertLess(thinking_headroom("medium"), thinking_headroom("high"))
        self.assertEqual(0, thinking_headroom(None))


class TestAnthropicKeepsItsThinking(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fake = _Fake()
        self.addCleanup(self.fake.shutdown)
        self.provider = AnthropicProvider("test-key", self.fake.url)

    async def test_a_tool_turn_with_thinking_on_completes(self) -> None:
        # The live failure shape: think, call a tool, then continue after its result. The second request must
        # send the first reply's thinking block back, unchanged and first, or the API refuses it.
        self.fake.anthropic = [
            {"content": [THINKING_BLOCK, {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "app.py"}}]},
            {"content": [{"type": "text", "text": "app.py defines parse()."}]},
        ]

        async def read_file(args: dict[str, Any]) -> str:
            return "def parse(text): return text"

        conductor = Conductor(
            self.provider, "claude-x", "/tmp", system_prompt="s", tools=[READ],
            dispatch={"read_file": read_file}, thinking_effort="medium",
        )
        answer = await conductor.run("what does app.py define?")

        self.assertEqual("app.py defines parse().", answer)
        first, second = (body for path, body in self.fake.bodies if path.endswith("/v1/messages"))
        self.assertEqual({"type": "enabled", "budget_tokens": 4096}, first["thinking"], "medium is not 4096 tokens")
        assistant = next(m for m in second["messages"] if m["role"] == "assistant")
        self.assertEqual(THINKING_BLOCK, assistant["content"][0], "the signed block was not sent back unchanged")
        self.assertEqual("tool_use", assistant["content"][1]["type"])

    async def test_the_fake_refuses_a_tool_turn_that_drops_its_thinking(self) -> None:
        # The negative control: without this, the test above would pass against a fake that accepts anything.
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [ToolCall("t1", "read_file", {"path": "a"})]},
            {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "x"},
        ]
        with self.assertRaises(ProviderError):
            await self.provider.complete_with_tools(
                "s", messages, [READ], "claude-x", 0.2, CALL_MAX_TOKENS + thinking_headroom("low"),
                thinking_effort="low",
            )

    async def test_off_sends_no_thinking_field(self) -> None:
        await self.provider.complete_with_tools("s", [{"role": "user", "content": "q"}], [READ], "claude-x", 0.2, 2048)
        self.assertNotIn("thinking", self.fake.bodies[-1][1])


class TestTheOtherProtocolsSendTheirOwnControl(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fake = _Fake()
        self.addCleanup(self.fake.shutdown)

    async def _last(self) -> dict[str, Any]:
        return self.fake.bodies[-1][1]

    async def test_openai_compatible_sends_reasoning_effort(self) -> None:
        provider = OpenAICompatProvider("k", self.fake.url, provider_name="openai")
        await provider.complete_with_tools("s", [{"role": "user", "content": "q"}], [READ], "m", 0.2, 4096,
                                           thinking_effort="high")
        self.assertEqual("high", (await self._last())["reasoning_effort"])

    async def test_deepseek_keeps_thinking_off_on_tool_calls(self) -> None:
        # Its thinking mode needs every earlier turn's reasoning_content sent back, which this transcript cannot do.
        provider = OpenAICompatProvider("k", self.fake.url, provider_name="deepseek")
        await provider.complete_with_tools("s", [{"role": "user", "content": "q"}], [READ], "m", 0.2, 4096,
                                           thinking_effort="high")
        body = await self._last()
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual({"type": "disabled"}, body["thinking"])

    async def test_ollama_sends_think(self) -> None:
        provider = OllamaProvider(self.fake.url)
        await provider.complete_with_tools("s", [{"role": "user", "content": "q"}], [READ], "m", 0.2, 4096,
                                           thinking_effort="medium")
        self.assertEqual("medium", (await self._last())["think"])

    async def test_google_sends_a_thinking_budget(self) -> None:
        provider = GoogleProvider("k", self.fake.url)
        await provider.complete_with_tools("s", [{"role": "user", "content": "q"}], [READ], "g", 0.2,
                                           CALL_MAX_TOKENS + thinking_headroom("medium"), thinking_effort="medium")
        config = (await self._last())["generationConfig"]
        self.assertEqual({"thinkingBudget": 4096}, config["thinkingConfig"])

    async def test_none_of_them_send_a_control_when_it_is_off(self) -> None:
        for provider, key in (
            (OpenAICompatProvider("k", self.fake.url, provider_name="openai"), "reasoning_effort"),
            (OllamaProvider(self.fake.url), "think"),
        ):
            await provider.complete_with_tools("s", [{"role": "user", "content": "q"}], [READ], "m", 0.2, 4096)
            self.assertNotIn(key, await self._last())
        await GoogleProvider("k", self.fake.url).complete_with_tools(
            "s", [{"role": "user", "content": "q"}], [READ], "g", 0.2, 4096)
        self.assertNotIn("thinkingConfig", (await self._last())["generationConfig"])


class TestTheSetting(unittest.TestCase):
    def test_only_the_known_levels_are_accepted(self) -> None:
        from engine.app import _clean_engine_string
        from engine.services import ApiError

        for value in ("", "low", "medium", "high"):
            self.assertEqual(value, _clean_engine_string("conductor_thinking_effort", value))
        with self.assertRaises(ApiError):
            _clean_engine_string("conductor_thinking_effort", "max")

    def test_the_executor_reads_it_fresh_and_treats_anything_else_as_off(self) -> None:
        from engine.executor_conduct import _Conduct

        class Settings:
            value = "high"

            def get_str(self, key: str) -> str:
                assert key == "conductor_thinking_effort"
                return self.value

        host = cast(Any, object.__new__(_Conduct))
        host.settings = Settings()
        self.assertEqual("high", host._thinking_effort())
        host.settings.value = ""
        self.assertIsNone(host._thinking_effort())
        host.settings = None
        self.assertIsNone(host._thinking_effort(), "a missing store must not turn thinking on")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(asyncio.sleep(0))
    unittest.main()
