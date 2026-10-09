"""A role's temperature is for the local model, and no hosted one is sent it.

Claude Opus 4.7 and later, Fable 5 and the Sonnet 5 line answer a request that carries `temperature` with a 400, and
OpenAI's reasoning models only take their default; every role has a temperature (0.0 to 0.4), so while it was sent
no role could run on them. Ollama always gets it. An OpenAI-compatible endpoint gets it when it is a local server
(loopback or a private address: LM Studio, vLLM, llama.cpp) and not when it is hosted. Anthropic and Google never do.
"""
from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import unittest
from typing import Any
from unittest import mock

import httpx

from engine.providers import (
    AnthropicProvider,
    BaseProvider,
    GoogleProvider,
    OllamaProvider,
    OpenAICompatProvider,
    is_local_endpoint,
)
from engine.toolcall import ToolSpec

TOOLS = [ToolSpec(name="read_file", description="Read a file.")]
HISTORY: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
ANSWER = {
    "content": [{"type": "text", "text": "ok"}], "response": "ok", "done": True, "usage": {},
    "choices": [{"message": {"content": "ok"}}], "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
    "message": {"role": "assistant", "content": "ok"},
}


def _temperature(body: dict[str, Any]) -> Any:
    """Wherever the dialect puts it: top level, Ollama's `options`, or Google's `generationConfig`."""
    for place in (body, body.get("options") or {}, body.get("generationConfig") or {}):
        if "temperature" in place:
            return place["temperature"]
    return None


class TestWhoIsSentATemperature(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bodies: list[dict[str, Any]] = []

        def answer(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            if request.method == "GET":
                return httpx.Response(404)
            return httpx.Response(200, json=ANSWER)

        real = httpx.AsyncClient

        def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(answer)
            return real(*args, **kwargs)

        patcher = mock.patch.object(httpx, "AsyncClient", client)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _both(self, provider: BaseProvider) -> list[Any]:
        """The temperature each of the provider's two calls sent."""
        self.bodies.clear()
        await provider.complete("s", "hi", "m", 0.3, 16)
        await provider.complete_with_tools("s", HISTORY, TOOLS, "m", 0.3, 16)
        posts = [b for b in self.bodies if b]
        self.assertEqual(len(posts), 2, "each call should make exactly one request")
        return [_temperature(b) for b in posts]

    async def test_ollama_gets_it_on_both_calls(self) -> None:
        self.assertEqual(await self._both(OllamaProvider("http://127.0.0.1:11434")), [0.3, 0.3])

    async def test_a_local_openai_compatible_server_gets_it_on_both_calls(self) -> None:
        # Loopback is the path that works: the repo never sends a key to a LAN host over plain http (docs/03).
        for url in ("http://127.0.0.1:1234/v1", "http://localhost:8080/v1", "http://[::1]:8000/v1"):
            with self.subTest(url=url):
                self.assertEqual(await self._both(OpenAICompatProvider("placeholder", url)), [0.3, 0.3])

    async def test_a_hosted_openai_compatible_endpoint_gets_none(self) -> None:
        for url in ("https://api.openai.com/v1", "https://openrouter.ai/api/v1", "https://api.groq.com/openai/v1"):
            with self.subTest(url=url):
                self.assertEqual(await self._both(OpenAICompatProvider("sk-test", url)), [None, None])

    async def test_claude_gets_none(self) -> None:
        self.assertEqual(await self._both(AnthropicProvider("sk-test", "https://api.anthropic.com")), [None, None])

    async def test_gemini_gets_none(self) -> None:
        provider = GoogleProvider("g-test", "https://generativelanguage.googleapis.com/v1beta")
        self.assertEqual(await self._both(provider), [None, None])


class TestWhatCountsAsLocal(unittest.TestCase):
    def test_loopback_and_private_addresses_are_local(self) -> None:
        for url in ("http://127.0.0.1:11434", "http://localhost:1234/v1", "http://[::1]:8000/v1",
                    "http://10.0.0.5:8080/v1", "http://192.168.0.2/v1", "http://172.16.4.4:9000/v1",
                    "http://169.254.1.1/v1"):
            with self.subTest(url=url):
                self.assertTrue(is_local_endpoint(url))

    def test_a_hosted_or_unknowable_endpoint_is_not(self) -> None:
        for url in ("https://api.openai.com/v1", "http://8.8.8.8/v1", "https://lmstudio.example/v1",
                    "http://127.evil.example/v1", "not a url", ""):
            with self.subTest(url=url):
                self.assertFalse(is_local_endpoint(url))


if __name__ == "__main__":
    unittest.main()
