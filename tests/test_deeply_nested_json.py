"""Text a model, a server or a tool wrote cannot crash the engine by being nested too deeply.

`json.loads` answers a document nested past the interpreter's limit with `RecursionError`, which is not a
`ValueError`. Every site below caught only the latter, so a reply of a few hundred `[` (a small model stuck repeating
one token does exactly that) escaped `Conductor.run`, whose docstring promises it never raises for anything a model did.
The limit is about 990 levels on the declared 3.10 floor and about 10,000 on 3.13, so the payloads here are 200,000 deep:
they fail on every interpreter this project supports, and a test that passes only on the newer one proves nothing.

What each site must do is whatever it already does for text that is not JSON at all: a tool call is not recovered, an
argument dictionary is empty, an MCP answer is "not JSON", a provider's stream is a `ProviderError`.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.conductor import Conductor
from engine.executor_conduct import _as_prose
from engine.laya import _extract_json as laya_extract_json
from engine.mcp import McpError, _json_message, _Reply
from engine.providers import OllamaProvider, ProviderError, error_detail
from engine.replies import extract_json
from engine.speech import parse_inputs
from engine.toolcall import (
    ToolSpec,
    coerce_arguments,
    coerce_tool_reply,
    parse_openai_tool_calls,
)

DEPTH = 200_000
DEEP_ARRAY = "[" * DEPTH
DEEP_OBJECT = "{" + '"a":{' * DEPTH
SPEC = ToolSpec(name="read_file", description="d", parameters={"type": "object", "properties": {"path": {"type": "string"}}})


class EverySiteSurvivesAHostileDepth(unittest.TestCase):
    def test_a_reply_written_as_a_tool_call_is_not_recovered_and_does_not_raise(self) -> None:
        for body in (DEEP_OBJECT, '{"a":' + DEEP_ARRAY, "```json\n" + DEEP_OBJECT + "\n```", "hi\n```json\n" + DEEP_OBJECT + "\n```\n"):
            with self.subTest(body=body[:24]):
                reply = coerce_tool_reply(body, [], [SPEC])
                self.assertFalse(reply.wants_tools)

    def test_arguments_that_are_too_deep_are_an_empty_dictionary(self) -> None:
        self.assertEqual({}, coerce_arguments(SPEC, DEEP_OBJECT))
        self.assertEqual({}, coerce_arguments(SPEC, "[" * DEPTH))
        calls = parse_openai_tool_calls({"tool_calls": [{"id": "1", "function": {"name": "read_file", "arguments": DEEP_OBJECT}}]})
        self.assertEqual([("read_file", {})], [(c.name, c.arguments) for c in calls])

    def test_the_final_answer_unwrapping_leaves_deep_text_as_prose(self) -> None:
        self.assertEqual(DEEP_OBJECT, _as_prose(DEEP_OBJECT))

    def test_a_role_reply_that_is_too_deep_is_a_refusal_not_a_crash(self) -> None:
        for body in (DEEP_OBJECT, '{"steps":' + DEEP_ARRAY):
            with self.subTest(body=body[:24]):
                try:
                    extract_json(body, ("steps",))
                except (ValueError, KeyError) as exc:
                    self.assertNotIsInstance(exc, RecursionError)
                except RecursionError as exc:  # pragma: no cover - the failure this test exists for
                    self.fail(f"RecursionError escaped extract_json: {exc}")

    def test_the_gates_reply_is_an_empty_object(self) -> None:
        self.assertEqual({}, laya_extract_json(DEEP_OBJECT))
        self.assertEqual({}, laya_extract_json("x " + DEEP_OBJECT + " y"))

    def test_a_providers_error_body_that_is_too_deep_is_shown_as_text(self) -> None:
        self.assertIsInstance(error_detail(DEEP_OBJECT), str)

    def test_a_pipewire_dump_that_is_too_deep_lists_no_sources(self) -> None:
        self.assertEqual([], parse_inputs(DEEP_ARRAY))

    def test_an_mcp_server_that_answers_too_deeply_is_a_refused_answer(self) -> None:
        """The one hostile party in the list: a server a person added is still someone else's server."""
        for content_type in ("application/json", "text/event-stream"):
            body = DEEP_OBJECT if content_type == "application/json" else "data: " + DEEP_OBJECT + "\n\n"
            with self.subTest(content_type=content_type):
                reply = _Reply(status=200, content_type=content_type, body=body.encode(), session_id="")
                with self.assertRaises(McpError):
                    _json_message(reply, 1)


class _DeepServer(BaseHTTPRequestHandler):
    """An Ollama that answers every request with one reply nested far too deep."""

    body = ""

    def do_POST(self) -> None:  # noqa: N802 - the stdlib's name
        self.rfile.read(int(self.headers.get("content-length", 0)))
        out = self.body.encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args: Any) -> None:
        pass


class _Served:
    def __init__(self, body: str) -> None:
        handler = type("Handler", (_DeepServer,), {"body": body})
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class TheConductorKeepsItsPromise(unittest.IsolatedAsyncioTestCase):
    async def test_run_returns_text_when_the_model_loops_on_an_opening_bracket(self) -> None:
        chat = json.dumps({"message": {"role": "assistant", "content": '{"a":' + DEEP_ARRAY}, "done": True})
        with _Served(chat) as url, tempfile.TemporaryDirectory() as root:
            conductor = Conductor(
                OllamaProvider(url), "m", root, tools=[], dispatch={}, system_prompt="you are a conductor",
            )
            text = await asyncio.wait_for(conductor.run("hello"), timeout=60)
        self.assertIsInstance(text, str)
        self.assertTrue(text.startswith('{"a":['), "the model's words come back as the answer, as for any text that is not a call")

    async def test_a_streamed_ollama_line_that_is_too_deep_is_a_provider_error(self) -> None:
        with _Served(DEEP_OBJECT) as url:
            provider = OllamaProvider(url)
            provider.on_delta = lambda text: None
            with self.assertRaises(ProviderError) as caught:
                await provider.complete("system", "user", "m", 0.0, 64)
        self.assertEqual("provider_bad_response", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
