"""Every provider accepts what the conductor's loop passes it (found by the first end-to-end conductor run).

`Conductor._call` passes `num_ctx` and `keep_alive` to `complete_with_tools` on every call. Only
`OllamaProvider.complete_with_tools` took them: the base class, `AnthropicProvider`, `OpenAICompatProvider` and
`GoogleProvider` did not, so on any of those the first call raised `TypeError: ... got an unexpected keyword
argument 'num_ctx'` and the turn ended `internal_error`. The conductor worked on Ollama and nowhere else — OpenAI,
DeepSeek, Groq, OpenRouter, Anthropic, Google, and every local llama.cpp / LM Studio / vLLM server — and the
suite never said so, because every test double in it accepts those keywords. The plain `complete` methods already
took them ("only Ollama consumes either"); the tool-calling variant was missed.

Two guards: the signature, over every real provider class (so a provider added later is held to it too), and a
conductor turn over a real socket, which is the only kind of test that could have caught this.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine import providers
from engine.conductor import Conductor
from engine.providers import BaseProvider, OpenAICompatProvider


def _provider_classes() -> list[type[Any]]:
    found: list[type[Any]] = [BaseProvider]
    for value in vars(providers).values():
        if inspect.isclass(value) and issubclass(value, BaseProvider) and value not in found:
            found.append(value)
    return found


class TestTheSignatureIsTheSameEverywhere(unittest.TestCase):
    def test_every_provider_class_is_found(self) -> None:
        names = {cls.__name__ for cls in _provider_classes()}
        self.assertTrue(
            {"BaseProvider", "AnthropicProvider", "OpenAICompatProvider", "OllamaProvider", "GoogleProvider"} <= names,
            names,
        )

    def test_complete_with_tools_takes_num_ctx_and_keep_alive_as_keywords(self) -> None:
        for cls in _provider_classes():
            with self.subTest(provider=cls.__name__):
                params = inspect.signature(cls.complete_with_tools).parameters
                for name in ("num_ctx", "keep_alive"):
                    self.assertIn(name, params, f"{cls.__name__}.complete_with_tools has no {name}")
                    self.assertEqual(inspect.Parameter.KEYWORD_ONLY, params[name].kind, name)
                    self.assertIsNone(params[name].default, name)

    def test_complete_takes_them_too(self) -> None:
        for cls in _provider_classes():
            with self.subTest(provider=cls.__name__):
                params = inspect.signature(cls.complete).parameters
                self.assertIn("num_ctx", params)
                self.assertIn("keep_alive", params)


class _Chat:
    """A local OpenAI-compatible server that answers every chat completion with one text reply."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                outer.bodies.append(json.loads(self.rfile.read(length) or b"{}"))
                body = json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": "It returns a greeting."},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def __enter__(self) -> _Chat:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class TestAConductorTurnOnARealOpenAICompatibleServer(unittest.TestCase):
    def test_the_loop_gets_an_answer_instead_of_a_type_error(self) -> None:
        with _Chat() as server:
            provider = OpenAICompatProvider("placeholder-key", server.base_url)
            conductor = Conductor(
                provider, "any-model", ".", dispatch={}, system_prompt="You are a test.",
                num_ctx=None, keep_alive=None,
            )

            answer = asyncio.run(conductor.run("what does greet do?"))

        self.assertEqual("It returns a greeting.", answer)
        self.assertEqual(1, len(server.bodies), "the provider never reached the server")


if __name__ == "__main__":
    unittest.main()
