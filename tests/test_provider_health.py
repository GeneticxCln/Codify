"""Regression tests for provider health checks (reachability, auth, latency, offline mode, down notices)."""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — isolated CODIFY_HOME

import json
import unittest
from typing import Any

import httpx

from engine.catalog_watch import CatalogWatch
from engine.model_catalog import ModelCatalogService, ProviderTarget, discover_provider
from tests.test_model_catalog import _StubKeychain, _StubRegistry


class TestProviderHealth(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_provider_reports_reachable_auth_ok_and_latency(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

        target = ProviderTarget(
            provider="openai", protocol="openai_compat",
            base_url="https://api.openai.com/v1", api_key="sk-test", needs_key=True,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            res = await discover_provider(target, client=client)
        payload = res.to_payload()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["reachable"])
        self.assertIs(payload["auth_ok"], True)
        self.assertIsInstance(payload["latency_ms"], int)
        self.assertGreaterEqual(payload["latency_ms"], 0)
        self.assertFalse(payload["offline_skipped"])

    async def test_401_reports_reachable_but_auth_failed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid key"})

        target = ProviderTarget(
            provider="openai", protocol="openai_compat",
            base_url="https://api.openai.com/v1", api_key="sk-bad", needs_key=True,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            res = await discover_provider(target, client=client)
        self.assertFalse(res.ok)
        self.assertTrue(res.reachable)
        self.assertIs(res.auth_ok, False)
        self.assertIsInstance(res.latency_ms, int)

    async def test_connection_failure_reports_unreachable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        target = ProviderTarget(
            provider="ollama", protocol="ollama",
            base_url="http://127.0.0.1:11434", needs_key=False,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            res = await discover_provider(target, client=client)
        self.assertFalse(res.ok)
        self.assertFalse(res.reachable)
        self.assertIsNone(res.auth_ok)
        self.assertIsInstance(res.latency_ms, int)

    async def test_offline_mode_skips_remote_providers_and_still_checks_local_ones(self) -> None:
        seen_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_hosts.append(request.url.host or "")
            if request.url.path.endswith("/api/tags"):
                return httpx.Response(200, json={"models": [{"name": "qwen:7b"}]})
            return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

        catalog = ModelCatalogService(
            _StubRegistry(),
            _StubKeychain({"openai": "sk-live"}),
            transport=httpx.MockTransport(handler),
            is_offline=lambda: True,
        )
        data = await catalog.get(refresh=True)
        by_slug = {p["provider"]: p for p in data["providers"]}
        self.assertTrue(by_slug["ollama"]["ok"])
        self.assertFalse(by_slug["ollama"]["offline_skipped"])
        self.assertFalse(by_slug["openai"]["ok"])
        self.assertTrue(by_slug["openai"]["offline_skipped"])
        self.assertNotIn("api.openai.com", seen_hosts, "offline mode must not ping remote providers")

    async def test_watch_announces_provider_down_and_recovery_without_baseline_noise(self) -> None:
        state = {"ollama_ok": True}

        def handler(request: httpx.Request) -> httpx.Response:
            if not state["ollama_ok"]:
                raise httpx.ConnectError("down")
            return httpx.Response(
                200,
                text=json.dumps({"models": [{"name": "m1"}]}),
                headers={"content-type": "application/json"},
            )

        catalog = ModelCatalogService(
            _StubRegistry(), _StubKeychain(), transport=httpx.MockTransport(handler),
        )
        sent: list[dict[str, Any]] = []
        watch = CatalogWatch(catalog, publish=sent.append)
        watch.subscribe()

        # Baseline: silent about health.
        await watch.sweep()
        self.assertEqual([f for f in sent if f["type"] == "provider_health_changed"], [])

        # Provider goes down: announced once.
        state["ollama_ok"] = False
        await watch.sweep()
        health_frames = [f for f in sent if f["type"] == "provider_health_changed"]
        self.assertEqual(len(health_frames), 1)
        self.assertEqual([d["provider"] for d in health_frames[0]["payload"]["down"]], ["ollama"])
        self.assertFalse(health_frames[0]["payload"]["down"][0]["reachable"])

        # Staying down does not spam.
        await watch.sweep()
        self.assertEqual(len([f for f in sent if f["type"] == "provider_health_changed"]), 1)

        # Provider recovers: announced once.
        state["ollama_ok"] = True
        await watch.sweep()
        health_frames = [f for f in sent if f["type"] == "provider_health_changed"]
        self.assertEqual(len(health_frames), 2)
        self.assertEqual([r["provider"] for r in health_frames[1]["payload"]["recovered"]], ["ollama"])


if __name__ == "__main__":
    unittest.main()
