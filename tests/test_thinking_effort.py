"""Thinking effort is one setting, four wire fields, and a clearable database column.

The setting is per role because the honest amount of thinking differs by job: a gate verdict and a
scribe summary do not need the budget a planner does. It is one field on `AgentConfig`; each protocol
translates it into the control it actually has. The stand-in server below is the regression test:
it records the body each adapter sent, so a translation that silently stopped being sent fails here
rather than in a live run against a model nobody asked.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.db import connect
from engine.models import AgentConfigUpdate
from engine.providers import (
    AnthropicProvider,
    GoogleProvider,
    Keychain,
    OllamaProvider,
    OpenAICompatProvider,
    ProviderFactory,
)
from engine.services import AgentRegistryService


class _Wire:
    """One HTTP endpoint answering every protocol in the shape it expects."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.paths: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                # OpenAI-compatible structured-output discovery asks for the model list.
                self._send({"data": []})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.bodies.append(body)
                outer.paths.append(self.path)
                if self.path.endswith("/v1/messages"):
                    self._send({
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    })
                elif self.path.endswith(":generateContent"):
                    self._send({
                        "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
                    })
                elif self.path.endswith("/api/generate"):
                    self._send({
                        "response": "ok", "done": True,
                        "prompt_eval_count": 1, "eval_count": 1,
                    })
                else:
                    self._send({
                        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    })

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def __enter__(self) -> _Wire:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _complete(provider: Any, effort: str | None, max_tokens: int = 4096) -> str:
    return str(asyncio.run(provider.complete(
        "system", "user", "a-model", 0.2, max_tokens,
        **({"thinking_effort": effort} if effort is not None else {}),
    )))


class TestTheProtocolTranslation(unittest.TestCase):
    def test_openai_compatible_sends_reasoning_effort_and_omits_it_when_unset(self) -> None:
        with _Wire() as wire:
            provider = OpenAICompatProvider("k", f"{wire.base_url}/v1", "openai")
            self.assertEqual("ok", _complete(provider, "high"))
            self.assertEqual("high", wire.bodies[-1]["reasoning_effort"])
            self.assertEqual("ok", _complete(provider, None))
            self.assertNotIn("reasoning_effort", wire.bodies[-1], "an unset role asked for a level it did not choose")

    def test_anthropic_sends_a_budget_below_the_reply_cap(self) -> None:
        with _Wire() as wire:
            provider = AnthropicProvider("k", wire.base_url)
            self.assertEqual("ok", _complete(provider, "medium"))
            self.assertEqual(
                {"type": "enabled", "budget_tokens": 2048},
                wire.bodies[-1]["thinking"],
                "the budget must leave room in max_tokens for the reply",
            )
            self.assertEqual("ok", _complete(provider, None))
            self.assertNotIn("thinking", wire.bodies[-1])

    def test_a_small_reply_cap_does_not_send_an_unacceptable_budget(self) -> None:
        # Claude refuses a thinking budget under 1024. A role with max_tokens=1024 cannot have one, so
        # the adapter sends no thinking block rather than a request the API will 400.
        with _Wire() as wire:
            provider = AnthropicProvider("k", wire.base_url)
            self.assertEqual("ok", _complete(provider, "high", max_tokens=1024))
            self.assertNotIn("thinking", wire.bodies[-1])

    def test_google_sends_the_named_budget_and_ollama_sends_the_level(self) -> None:
        with _Wire() as wire:
            google = GoogleProvider("k", f"{wire.base_url}/v1beta")
            self.assertEqual("ok", _complete(google, "low"))
            self.assertEqual(
                {"thinkingBudget": 1024},
                wire.bodies[-1]["generationConfig"]["thinkingConfig"],
            )

            ollama = OllamaProvider(wire.base_url)
            self.assertEqual("ok", _complete(ollama, "low"))
            self.assertEqual("low", wire.bodies[-1]["think"])
            self.assertNotIn("think", wire.bodies[-2], "Google's call must not carry Ollama's field")

    def test_unset_keeps_every_protocol_at_its_own_default(self) -> None:
        with _Wire() as wire:
            providers = (
                (OpenAICompatProvider("k", f"{wire.base_url}/v1", "openai"), "reasoning_effort"),
                (AnthropicProvider("k", wire.base_url), "thinking"),
                (GoogleProvider("k", f"{wire.base_url}/v1beta"), "thinkingConfig"),
                (OllamaProvider(wire.base_url), "think"),
            )
            for provider, field in providers:
                with self.subTest(provider=type(provider).__name__):
                    _complete(provider, None)
                    body = wire.bodies[-1]
                    serialized = json.dumps(body)
                    self.assertNotIn(field, serialized, f"an unset role sent {field}")


class TestTheSettingPersists(unittest.TestCase):
    def test_a_level_saves_and_an_explicit_null_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            conn = connect(root / "codify.db")
            try:
                keychain = Keychain(secrets_path=root / "secrets.json")
                registry = AgentRegistryService(conn, ProviderFactory(keychain), keychain)
                registry.set_config("fixer", AgentConfigUpdate(thinking_effort="high"))
                self.assertEqual("high", registry.get_config("fixer").thinking_effort)

                registry.set_config("fixer", AgentConfigUpdate(thinking_effort=None))
                self.assertIsNone(
                    registry.get_config("fixer").thinking_effort,
                    "an explicit null must clear, not read as no change",
                )

                # And the column survives a fresh connection, which is what a settings save promises.
                conn2 = connect(root / "codify.db")
                try:
                    again = AgentRegistryService(conn2, ProviderFactory(keychain), keychain)
                    self.assertIsNone(again.get_config("fixer").thinking_effort)
                finally:
                    conn2.close()
            finally:
                conn.close()

    def test_an_unknown_level_is_refused_by_the_model(self) -> None:
        with self.assertRaises(ValueError):
            AgentConfigUpdate.model_validate({"thinking_effort": "maximum"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
