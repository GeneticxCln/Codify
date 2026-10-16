"""A lone surrogate in a request body is healed before it is sent, on every provider and every way of asking.

Python carries a lone surrogate in a `str` happily (`json.loads('"\\ud800"')` makes one, and so does `os.walk` for a
file name that is not UTF-8). httpx encodes `json=` as UTF-8, and a lone surrogate cannot be: all four providers raised
a bare `UnicodeEncodeError` for it, in `complete` (system prompt or user prompt) and in `complete_with_tools`. That is
a `ValueError`, neither an `httpx.HTTPError` nor a `ProviderError`, so it skipped `_post_until_answered`'s handling and
`FALLBACK_TRIGGER_CODES`, and the goal ended `agent_output_invalid`, `replay_failed` or `internal_error`: a code that
blames Codify, for a provider that was never asked.

The rule (docs/04 section 6): text a model wrote is healed wherever it becomes prose, a title, a note, a message or a
prompt. A request is prose to a model, so it is healed here, in the two functions every completion body passes
through (`_post_until_answered` for `post_json` and `post_bytes`, and `open_stream`), whatever put the surrogate in:
a reply echoed back, a stored row, a tool result, a file name.

**Scope of the guard test.** `test_only_the_two_senders_post_a_body` reads `engine/providers.py` and nothing else. It is
not a census of every request the engine makes: `engine/model_catalog.py` also posts (and swallows its exceptions), and
other modules send requests of their own. It says only that a provider added to this file cannot reach the wire except
through the two functions that heal.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import ast
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from engine import providers
from engine.providers import (
    AnthropicProvider,
    BaseProvider,
    GoogleProvider,
    OllamaProvider,
    OpenAICompatProvider,
    ProviderError,
    _healed_body,
    open_stream,
    post_bytes,
    post_json,
)
from engine.toolcall import ToolCall, ToolSpec

PROVIDERS_PY = Path(providers.__file__).resolve()
POISON = "before \ud800 after"
HEALED = "before � after"
TOOLS = [ToolSpec(name="read_file", description="Read a file.")]

OPENAI_STREAM = b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],"usage":{}}\n\ndata: [DONE]\n\n'
OLLAMA_STREAM = b'{"response":"ok","done":true}\n'

ANSWERS: dict[str, dict[str, Any]] = {
    "anthropic": {"content": [{"type": "text", "text": "ok"}], "usage": {}},
    "openai_compat": {"choices": [{"message": {"content": "ok"}}], "usage": {}},
    "ollama": {"response": "ok", "message": {"content": "ok"}, "done": True},
    "google": {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
}


def build(kind: str) -> BaseProvider:
    if kind == "anthropic":
        return AnthropicProvider("sk-test", "https://api.anthropic.com")
    if kind == "openai_compat":
        return OpenAICompatProvider("sk-test", "https://api.openai.com/v1")
    if kind == "ollama":
        return OllamaProvider("http://127.0.0.1:11434")
    return GoogleProvider("g-test", "https://generativelanguage.googleapis.com/v1beta")


class Wire(unittest.IsolatedAsyncioTestCase):
    """Every POST the code under test makes, as the bytes httpx put on the wire (so the encoding really happened)."""

    async def asyncSetUp(self) -> None:
        self.bodies: list[bytes] = []
        self.stream_reply: bytes = b""

        def answer(request: httpx.Request) -> httpx.Response:
            if request.method != "POST":
                return httpx.Response(404)  # the json-mode probe: not advertised
            self.bodies.append(request.content)
            if self.stream_reply:
                return httpx.Response(200, content=self.stream_reply)
            kind = next(k for k in ANSWERS if k in self.kind)
            return httpx.Response(200, json=ANSWERS[kind])

        self.kind = ""
        real = httpx.AsyncClient

        def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(answer)
            return real(*args, **kwargs)

        patcher = mock.patch.object(httpx, "AsyncClient", client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sent_text(self) -> str:
        """The one body that was sent, decoded as UTF-8: that it decodes at all is the point."""
        self.assertEqual(1, len(self.bodies), "the provider did not make exactly one request")
        return self.bodies[0].decode("utf-8")


class TestEveryProviderHealsItsRequest(Wire):
    """4 providers x (system prompt, user prompt, a tool conversation) = the 12 combinations that raised."""

    async def test_complete_heals_the_system_prompt_and_the_user_prompt(self) -> None:
        for kind in ANSWERS:
            for where in ("system", "user"):
                with self.subTest(provider=kind, poisoned=where):
                    self.bodies.clear()
                    self.kind = kind
                    system, user = (POISON, "hi") if where == "system" else ("sys", POISON)
                    reply = await build(kind).complete(system, user, "m", 0.0, 16)
                    self.assertEqual("ok", reply)
                    self.assertIn(HEALED, self.sent_text().replace("\\ufffd", "�"))
                    self.assertNotIn("\\ud800", self.sent_text())

    async def test_complete_with_tools_heals_the_conversation(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": POISON},
            {"role": "assistant", "content": "", "tool_calls": [ToolCall("c1", "read_file", {"path": POISON})]},
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": POISON},
        ]
        for kind in ANSWERS:
            with self.subTest(provider=kind):
                self.bodies.clear()
                self.kind = kind
                await build(kind).complete_with_tools("sys", messages, TOOLS, "m", 0.0, 16)
                sent = self.sent_text().replace("\\ufffd", "�")
                self.assertIn(HEALED, sent)
                if kind != "openai_compat":
                    # The OpenAI dialect sends a tool call's arguments as a JSON *string*, where the translator has
                    # already spelled the character as the ASCII escape `\\ud800`: encodable, so not this layer's.
                    self.assertNotIn("\\ud800", self.sent_text())

    async def test_the_streaming_paths_heal_too(self) -> None:
        for kind, stream in (("openai_compat", OPENAI_STREAM), ("ollama", OLLAMA_STREAM)):
            with self.subTest(provider=kind):
                self.bodies.clear()
                self.kind = kind
                self.stream_reply = stream
                provider = build(kind)
                seen: list[str] = []
                provider.on_delta = seen.append
                reply = await provider.complete("sys", POISON, "m", 0.0, 16)
                self.assertEqual("ok", reply)
                body = self.bodies[0].decode("utf-8")
                self.assertTrue(json.loads(body)["stream"], "this was not the streaming request")
                self.assertIn(HEALED, body.replace("\\ufffd", "�"))
                self.assertNotIn("\\ud800", body)

    async def test_the_caller_keeps_its_own_unhealed_payload(self) -> None:
        """Healing builds a copy for the wire; the dict a caller holds (a retry, a log) is not rewritten under it."""
        self.kind = "openai_compat"
        payload = {"messages": [{"role": "user", "content": POISON}]}
        async with httpx.AsyncClient() as client:
            await post_json(client, "https://api.openai.com/v1/chat/completions", label="t", json=payload)
        self.assertEqual(POISON, payload["messages"][0]["content"])

    async def test_a_dict_key_with_a_lone_surrogate_is_healed_as_well(self) -> None:
        self.kind = "openai_compat"
        async with httpx.AsyncClient() as client:
            await post_json(client, "https://api.openai.com/v1/chat/completions", label="t", json={"a\udc80": 1})
        self.assertEqual({"a�": 1}, json.loads(self.sent_text()))

    async def test_post_bytes_heals_a_json_body_like_post_json(self) -> None:
        self.kind = "openai_compat"
        async with httpx.AsyncClient() as client:
            await post_bytes(client, "https://api.openai.com/v1/audio/speech", label="t", json={"input": POISON})
        self.assertEqual({"input": HEALED}, json.loads(self.sent_text()))

    async def test_open_stream_heals_the_body(self) -> None:
        self.kind = "ollama"
        self.stream_reply = OLLAMA_STREAM
        async with httpx.AsyncClient() as client:
            async with open_stream(client, "http://127.0.0.1:11434/api/generate", label="t", json={"prompt": POISON}) as response:
                self.assertEqual(200, response.status_code)
        self.assertEqual({"prompt": HEALED}, json.loads(self.sent_text()))

    async def test_a_clean_request_is_sent_unchanged(self) -> None:
        self.kind = "openai_compat"
        async with httpx.AsyncClient() as client:
            await post_json(client, "https://api.openai.com/v1/chat/completions", label="t", json={"a": "café \U0001f600"})
        self.assertEqual({"a": "café \U0001f600"}, json.loads(self.sent_text()))

    async def test_a_valid_pair_is_not_a_lone_surrogate(self) -> None:
        self.kind = "openai_compat"
        async with httpx.AsyncClient() as client:
            await post_json(client, "https://api.openai.com/v1/chat/completions", label="t", json={"a": "x\U0001f600y"})
        self.assertEqual("x\U0001f600y", json.loads(self.sent_text())["a"])

    async def test_an_unencodable_request_is_never_a_bare_unicode_error(self) -> None:
        """Whatever else happens, the error that skipped the fallback rules is not what comes out."""
        self.kind = "anthropic"
        try:
            await build("anthropic").complete(POISON, POISON, "m", 0.0, 16)
        except UnicodeEncodeError:  # pragma: no cover - the failure this file exists to catch
            self.fail("a lone surrogate reached the encoder")
        except ProviderError:
            pass


class TestHealedBody(unittest.TestCase):
    def test_a_clean_body_returns_the_same_kwargs(self) -> None:
        kwargs = {"json": {"a": "b"}, "headers": {"x": "y"}}
        self.assertIs(kwargs, _healed_body(kwargs))

    def test_no_json_body_returns_the_same_kwargs(self) -> None:
        kwargs = {"data": {"a": "\ud800"}, "files": {"file": ("a.wav", b"x", "audio/wav")}}
        self.assertIs(kwargs, _healed_body(kwargs))

    def test_a_poisoned_body_is_healed_in_a_copy_and_the_other_arguments_travel(self) -> None:
        kwargs = {"json": {"a": ["x\ud800"]}, "headers": {"x-api-key": "k"}}
        healed = _healed_body(kwargs)
        self.assertEqual({"json": {"a": ["x�"]}, "headers": {"x-api-key": "k"}}, healed)
        self.assertEqual({"a": ["x\ud800"]}, kwargs["json"])
        self.assertIsNot(kwargs, healed)

    def test_a_body_nested_past_the_stack_is_a_provider_error_not_a_runtime_error(self) -> None:
        """The walk needs a frame per level; `json.dumps` copes with deeper, so the heal must not be the thing that fails.

        Built by a loop, not `json.loads`, so the depth is certain whatever the stack is when the test runs. A
        bare `RecursionError` is a `RuntimeError`: neither an `httpx.HTTPError` nor a `ProviderError`, so it skipped
        the provider rules and reached the goal as `internal_error`.
        """
        nested: Any = []
        for _ in range(sys.getrecursionlimit() * 2):
            nested = [nested]
        with self.assertRaises(ProviderError) as caught:
            _healed_body({"json": {"messages": nested}})
        self.assertEqual("request_unsendable", caught.exception.code)
        self.assertNotIn(
            "request_unsendable", providers.FALLBACK_TRIGGER_CODES,
            "a second provider is sent the same conversation: it would fail the same way",
        )


class TestOnlyTheTwoSendersPostABody(unittest.TestCase):
    """The structural half: the heal is in `_post_until_answered` and `open_stream`, so those must be the only senders.

    This reads `engine/providers.py` only. It is not a census of every outgoing request in the engine
    (`engine/model_catalog.py` also posts, and swallows its exceptions); it keeps a provider added to this file from
    reaching the wire any other way.
    """

    SENDERS = {"_post_until_answered", "open_stream"}
    BODY_CALLS = {"post", "put", "patch", "send", "request", "stream"}

    def _functions(self) -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
        tree = ast.parse(PROVIDERS_PY.read_text(encoding="utf-8"))
        return [n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))]

    @staticmethod
    def _calls(function: ast.AsyncFunctionDef | ast.FunctionDef) -> list[ast.Call]:
        return [n for n in ast.walk(function) if isinstance(n, ast.Call)]

    def test_only_the_two_senders_post_a_body(self) -> None:
        owners: dict[str, set[str]] = {}
        for function in self._functions():
            for call in self._calls(function):
                target = call.func
                if isinstance(target, ast.Attribute) and target.attr in self.BODY_CALLS:
                    receiver = ast.unparse(target.value)
                    # `client.post(`, `client.send(`; `response.aiter_lines()` and friends are not in the set.
                    if receiver.endswith("client") or receiver.endswith("httpx"):
                        owners.setdefault(function.name, set()).add(f"{receiver}.{target.attr}")
        self.assertEqual(self.SENDERS, set(owners), f"a body is sent from somewhere else: {owners}")

    def test_both_senders_heal_before_they_read_the_credentials_or_send(self) -> None:
        by_name = {f.name: f for f in self._functions() if f.name in self.SENDERS}
        self.assertEqual(self.SENDERS, set(by_name))
        for name, function in by_name.items():
            with self.subTest(sender=name):
                lines: dict[str, int] = {}
                for call in self._calls(function):
                    if isinstance(call.func, ast.Name) and call.func.id in ("_healed_body", "_known_secrets"):
                        lines.setdefault(call.func.id, call.lineno)
                    if isinstance(call.func, ast.Attribute) and call.func.attr in ("post", "send"):
                        lines.setdefault("send", call.lineno)
                self.assertEqual({"_healed_body", "_known_secrets", "send"}, set(lines))
                self.assertLess(lines["_healed_body"], lines["_known_secrets"], "heal before known = _known_secrets(kwargs)")
                self.assertLess(lines["_known_secrets"], lines["send"])


if __name__ == "__main__":
    unittest.main()
