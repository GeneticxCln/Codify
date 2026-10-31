"""DeepSeek's tool calls turn thinking off; every other call leaves it alone.

DeepSeek's thinking mode requires an assistant turn's `reasoning_content` to be passed back on every
later request that carries `tools`, and the conductor's transcript is a neutral message list with no
slot for it. The first real conductor run against DeepSeek therefore ended in a 400 before any move
ran. The fix is one wire field on one path: `complete_with_tools` asks DeepSeek for thinking off,
while the roles' `complete` calls — planning, review, the gate's fallback — keep it on.

The stand-in below is the regression test: it refuses a tool request that does not disable thinking,
exactly as the provider does, and it records what each path actually sent.
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.models import AgentConfig
from engine.providers import Keychain, OpenAICompatProvider, ProviderFactory
from engine.toolcall import ToolSpec

TOOL = ToolSpec(
    name="read_file",
    description="Read one file.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)


class _DeepSeekStandIn:
    """An OpenAI-compatible server with DeepSeek's thinking-with-tools rule."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                # Structured-output discovery asks for the model list before a role call.
                self._send(200, {"data": []})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.bodies.append(body)
                if body.get("tools") and str(body.get("model") or "").startswith("deepseek"):
                    # This is the provider's actual rule: a tool-carrying request in thinking mode
                    # must echo reasoning_content, and this request does not carry it.
                    if body.get("thinking") != {"type": "disabled"}:
                        self._send(400, {"error": {
                            "message": "reasoning_content must be passed back when using tools",
                        }})
                        return
                    self._send(200, {"choices": [{"message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"},
                        }],
                    }, "finish_reason": "tool_calls"}], "usage": {}})
                    return
                self._send(200, {"choices": [{"message": {
                    "role": "assistant", "content": "{\"summary\": \"ok\"}",
                }, "finish_reason": "stop"}], "usage": {}})

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def __enter__(self) -> _DeepSeekStandIn:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _call(provider: OpenAICompatProvider, model: str = "deepseek-chat") -> Any:
    return asyncio.run(provider.complete_with_tools(
        "You are a test.", [{"role": "user", "content": "read a.py"}], [TOOL],
        model, 0.2, 1024,
    ))


class DeepSeekThinkingTests(unittest.TestCase):
    def test_a_tool_call_tells_deepseek_to_stop_thinking(self) -> None:
        with _DeepSeekStandIn() as server:
            provider = OpenAICompatProvider("k", server.base_url, "deepseek")
            reply = _call(provider)

        self.assertEqual(["read_file"], [c.name for c in reply.tool_calls])
        self.assertEqual({"type": "disabled"}, server.bodies[0]["thinking"])

    def test_a_role_call_keeps_thinking_on(self) -> None:
        # Roles answer JSON in a single turn, so there is no reasoning to echo back: the toggle
        # belongs to the tool loop and must not reach `complete`.
        with _DeepSeekStandIn() as server:
            provider = OpenAICompatProvider("k", server.base_url, "deepseek")
            provider._json_capabilities = {}  # skip discovery; this test is about the payload
            asyncio.run(provider.complete("s", "u", "deepseek-chat", 0.2, 100))

        self.assertNotIn("thinking", server.bodies[0])

    def test_another_provider_is_not_sent_the_field(self) -> None:
        with _DeepSeekStandIn() as server:
            provider = OpenAICompatProvider("k", server.base_url, "openai")
            _call(provider, "gpt-4.1-mini")

        self.assertNotIn("thinking", server.bodies[0])

    def test_the_factory_passes_the_catalog_slug_through(self) -> None:
        # The behavior above is only reachable if the one constructor site tells the provider which
        # catalog entry it is. A missing slug would be silent: the request would still be sent.
        cfg = AgentConfig(
            role="scribe", display_name="Scribe", provider="deepseek",
            protocol="openai_compat", model_name="deepseek-chat",
        )
        provider = ProviderFactory(Keychain()).build(cfg)
        self.assertEqual("deepseek", getattr(provider, "_provider_name", ""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
