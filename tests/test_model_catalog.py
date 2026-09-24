"""Model discovery tests.

The guarantee under test: the catalog is built only from what providers actually
report. An empty or unreachable configuration must produce an empty catalog with
per-provider reasons — never a plausible-looking list compiled into the binary.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from engine.db import connect
from engine.model_catalog import (
    ModelCatalogService,
    ProviderTarget,
    _sorted_models,
    _targets,
    discover_provider,
)
from engine.models import AgentConfigUpdate
from engine.providers import Keychain, ProviderFactory
from engine.services import AgentRegistryService


class _StubKeychain:
    """Duck-typed Keychain with no environment leakage."""

    def __init__(self, provider_keys: dict[str, str] | None = None, role_keys: dict[str, str] | None = None):
        self.provider_keys = provider_keys or {}
        self.role_keys = role_keys or {}

    def get(self, ref: str | None) -> str:
        return self.role_keys.get(ref or "", "")

    def get_provider_key(self, provider: str) -> str:
        return self.provider_keys.get(provider, "")

    def has_provider_key(self, provider: str) -> bool:
        if provider == "ollama":
            return True
        return bool(self.provider_keys.get(provider))


class _StubConfig:
    def __init__(self, role, provider, protocol, base_url=None, api_key_ref=None, model_name="m",
                 fallback_provider=None, fallback_protocol=None, fallback_base_url=None):
        self.role = role
        self.provider = provider
        self.protocol = protocol
        self.base_url = base_url
        self.api_key_ref = api_key_ref
        self.model_name = model_name
        self.fallback_provider = fallback_provider
        self.fallback_protocol = fallback_protocol
        self.fallback_base_url = fallback_base_url


class _StubRegistry:
    def __init__(self, configs=None):
        self._configs = configs or []

    def list_configs(self):
        return self._configs


def _handler(routes: dict[str, tuple[int, Any]], calls: list[httpx.Request] | None = None):
    """Map path-suffix → (status, json) so a test can assert what was requested.

    Suffix matching because the same logical endpoint sits under different
    prefixes per provider (`/models` for OpenAI at `/v1/models`, bare `/models`
    for Google, `/api/tags` for Ollama).
    """

    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        path = request.url.path
        for prefix, (status, body) in routes.items():
            if path.endswith(prefix):
                payload = body if isinstance(body, str) else json.dumps(body)
                return httpx.Response(status, text=payload, headers={"content-type": "application/json"})
        return httpx.Response(404, json={"error": "not found"})

    return handle


def _client(routes, calls=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_handler(routes, calls)))


class TestProtocolDiscovery(unittest.IsolatedAsyncioTestCase):
    async def test_openai_compat(self):
        calls: list[httpx.Request] = []
        routes = {
            "/models": (
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "gpt-4o", "created": 1715367049, "owned_by": "openai"},
                        {"id": "o3-mini", "created": 1735689600, "owned_by": "openai"},
                    ],
                },
            )
        }
        target = ProviderTarget("openai", "openai_compat", "https://api.openai.com/v1", "sk-test")
        async with _client(routes, calls) as client:
            result = await discover_provider(target, client=client)

        self.assertTrue(result.ok, result.error)
        self.assertEqual({m["id"] for m in result.models}, {"gpt-4o", "o3-mini"})
        self.assertEqual(result.models[0]["provider"], "openai")
        self.assertEqual(result.models[0]["protocol"], "openai_compat")
        self.assertEqual(calls[0].headers["authorization"], "Bearer sk-test")

        # Ordering is the catalog's job (newest first), not the discoverer's.
        service = ModelCatalogService(
            _StubRegistry([]),
            _StubKeychain(provider_keys={"openai": "sk-test"}),
            transport=httpx.MockTransport(_handler(routes)),
        )
        payload = await service.get()
        openai_models = [m["id"] for m in payload["models"] if m["provider"] == "openai"]
        self.assertEqual(openai_models, ["o3-mini", "gpt-4o"], "newest first")

    async def test_anthropic_uses_its_own_headers_and_display_name(self):
        calls: list[httpx.Request] = []
        routes = {
            "/v1/models": (
                200,
                {"data": [{"id": "claude-x", "display_name": "Claude X", "created_at": "2025-01-02T00:00:00Z"}]},
            )
        }
        target = ProviderTarget("anthropic", "anthropic", "https://api.anthropic.com", "sk-ant")
        async with _client(routes, calls) as client:
            result = await discover_provider(target, client=client)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.models[0]["name"], "Claude X")
        self.assertEqual(calls[0].headers["x-api-key"], "sk-ant")
        self.assertEqual(calls[0].headers["anthropic-version"], "2023-06-01")
        self.assertIsNotNone(result.models[0]["created"])

    async def test_google_strips_the_resource_prefix_and_follows_pages(self):
        seen: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if "pageToken" in str(request.url):
                return httpx.Response(
                    200,
                    json={"models": [{"name": "models/gemini-3-pro", "displayName": "Gemini 3 Pro"}]},
                )
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash",
                            "displayName": "Gemini 2.5 Flash",
                            "inputTokenLimit": 1048576,
                            "supportedGenerationMethods": ["generateContent", "countTokens"],
                        },
                        {
                            "name": "models/embedding-001",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                    ],
                    "nextPageToken": "page-2",
                },
            )

        target = ProviderTarget(
            "google", "google", "https://generativelanguage.googleapis.com/v1beta", "goog-key"
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await discover_provider(target, client=client)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.models), 3, "second page must be fetched")
        # Passing 'models/gemini-…' straight into a request builds /models/models/…
        self.assertEqual(result.models[0]["id"], "gemini-2.5-flash")
        self.assertTrue(result.models[0]["supports_chat"])
        self.assertFalse(result.models[1]["supports_chat"], "embed-only model is flagged, not hidden")
        self.assertIn("1,048,576 token context", result.models[0]["description"])
        self.assertIn("gemini-3-pro", [m["id"] for m in result.models], "page 2 was consumed")
        self.assertEqual(len(seen), 2, "exactly two requests: first page, then the next")

    async def test_ollama_lists_local_models(self):
        routes = {
            "/api/tags": (
                200,
                {
                    "models": [
                        {"name": "qwen2.5-coder:7b", "details": {"parameter_size": "7B"}},
                        {"name": "llama3.1:8b", "details": {}},
                    ]
                },
            )
        }
        target = ProviderTarget("ollama", "ollama", "http://127.0.0.1:11434", needs_key=False)
        async with _client(routes) as client:
            result = await discover_provider(target, client=client)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.models), 2)
        self.assertIn("7B", result.models[0]["description"])

    async def test_no_embedding_name_filtering(self):
        """Ollama's list is reported as-is: filtering by name is how a hardcoded
        idea of 'a real model' creeps back in."""
        routes = {"/api/tags": (200, {"models": [{"name": "nomic-embed-text:latest"}]})}
        target = ProviderTarget("ollama", "ollama", "http://127.0.0.1:11434", needs_key=False)
        async with _client(routes) as client:
            result = await discover_provider(target, client=client)
        self.assertEqual([m["id"] for m in result.models], ["nomic-embed-text:latest"])


class TestProviderFailures(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_is_reported_without_a_request(self):
        calls: list[httpx.Request] = []
        target = ProviderTarget("openai", "openai_compat", "https://api.openai.com/v1", "")
        async with _client({}, calls) as client:
            result = await discover_provider(target, client=client)
        self.assertFalse(result.ok)
        self.assertIn("no API key", result.error)
        self.assertEqual(calls, [], "an unauthenticated provider must not be called")

    async def test_401_explains_itself(self):
        routes = {"/models": (401, {"error": {"message": "invalid api key"}})}
        target = ProviderTarget("openai", "openai_compat", "https://api.openai.com/v1", "bad")
        async with _client(routes) as client:
            result = await discover_provider(target, client=client)
        self.assertFalse(result.ok)
        self.assertIn("HTTP 401", result.error)
        self.assertIn("check the API key", result.error)

    async def test_unknown_protocol_is_reported(self):
        target = ProviderTarget("weird", "carrier-pigeon", "https://example.com", "k")
        result = await discover_provider(target)
        self.assertFalse(result.ok)
        self.assertIn("unknown protocol", result.error)

    async def test_transport_error_does_not_raise(self):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        target = ProviderTarget("ollama", "ollama", "http://127.0.0.1:11434", needs_key=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
            result = await discover_provider(target, client=client)
        self.assertFalse(result.ok)
        self.assertIn("connection refused", result.error)


class TestTargets(unittest.TestCase):
    def test_role_configs_keys_and_locals_are_all_queried(self):
        registry = _StubRegistry([
            _StubConfig("planner", "anthropic", "anthropic", api_key_ref="codify/agents/planner"),
            _StubConfig("fixer", "acme", "openai_compat", base_url="https://acme.test/v1", api_key_ref="ref"),
        ])
        keychain = _StubKeychain(
            provider_keys={"groq": "gsk-1"},
            role_keys={"codify/agents/planner": "sk-ant", "ref": "acme-key"},
        )
        targets = {t.provider: t for t in _targets(registry, keychain)}

        self.assertEqual(set(targets), {"anthropic", "acme", "groq", "ollama"})
        self.assertEqual(targets["anthropic"].api_key, "sk-ant")
        self.assertEqual(targets["acme"].base_url, "https://acme.test/v1")
        self.assertEqual(targets["groq"].api_key, "gsk-1")
        self.assertFalse(targets["ollama"].needs_key)
        self.assertIn("agent:planner", targets["anthropic"].sources)

    def test_ollama_always_present_even_with_no_configs(self):
        targets = _targets(_StubRegistry([]), _StubKeychain())
        self.assertEqual([t.provider for t in targets], ["ollama"])

    def test_builtin_base_url_used_when_config_has_none(self):
        registry = _StubRegistry([_StubConfig("fixer", "deepseek", "openai_compat")])
        targets = {t.provider: t for t in _targets(registry, _StubKeychain(provider_keys={"deepseek": "k"}))}
        self.assertEqual(targets["deepseek"].base_url, "https://api.deepseek.com")

    def test_a_fallback_provider_is_discoverable_too(self):
        """The settings screen flags a stale fallback with the same live-catalog
        rule as a primary. A fallback whose provider was never queried reports
        ok=false / no models, which would dress "we never asked" up as "your
        model is gone". Both the builtin case (catalog endpoint, key from the
        keychain) and a custom endpoint case must be targeted."""
        registry = _StubRegistry([
            _StubConfig(
                "fixer", "anthropic", "anthropic",
                fallback_provider="ollama",
                # A fallback_base_url the role configured itself (custom server).
                fallback_protocol="ollama",
                fallback_base_url="http://127.0.0.1:18999",
            ),
            _StubConfig(
                "planner", "deepseek", "openai_compat",
                fallback_provider="groq",
            ),
        ])
        keychain = _StubKeychain(provider_keys={"deepseek": "dk", "groq": "gk"})
        targets = {t.provider: t for t in _targets(registry, keychain)}

        self.assertIn("ollama", targets)
        self.assertEqual(targets["ollama"].base_url, "http://127.0.0.1:18999")
        self.assertIn("fallback:fixer", targets["ollama"].sources)
        self.assertIn("groq", targets)
        self.assertEqual(targets["groq"].api_key, "gk", "builtin fallback inherits the keychain key")
        self.assertEqual(targets["groq"].base_url, "https://api.groq.com/openai/v1")

    def test_a_role_set_endpoint_beats_the_seed_defaults(self):
        """Discovery must poll the endpoint the roles actually talk to.

        list_configs is role-ordered and the seeded laya role has no base_url,
        so the first 'ollama' target carried the builtin 11434 endpoint. The
        merge kept that first base_url and silently dropped the endpoint every
        other role had configured — discovery polled a server the roles never
        talk to, and the stale-model warning judged models against that wrong
        catalog (a model missing from 11434 but served by the configured
        endpoint warned; the reverse silently passed).
        """
        registry = _StubRegistry([
            _StubConfig("laya", "ollama", "ollama"),  # seeded: no base_url
            _StubConfig("fixer", "ollama", "ollama", base_url="http://127.0.0.1:18999"),
        ])
        targets = {t.provider: t for t in _targets(registry, _StubKeychain())}
        self.assertEqual(
            targets["ollama"].base_url, "http://127.0.0.1:18999",
            "a role-configured endpoint must win over the seed default",
        )
        # And when only the seed has spoken, the builtin default stands.
        registry2 = _StubRegistry([_StubConfig("laya", "ollama", "ollama")])
        targets2 = {t.provider: t for t in _targets(registry2, _StubKeychain())}
        self.assertEqual(targets2["ollama"].base_url, "http://127.0.0.1:11434")


class TestCatalogService(unittest.IsolatedAsyncioTestCase):
    async def _service(self, registry, keychain, routes, calls=None):
        return ModelCatalogService(
            registry, keychain, ttl_s=60, transport=httpx.MockTransport(_handler(routes, calls))
        )

    async def test_empty_configuration_yields_no_models(self):
        """The regression test for hardcoded models: nothing configured, ollama
        down → an empty catalog and a reason, not a plausible fake list."""
        calls: list[httpx.Request] = []
        service = await self._service(_StubRegistry([]), _StubKeychain(), {}, calls)
        payload = await service.get()

        self.assertEqual(payload["models"], [])
        self.assertFalse(payload["cached"])
        statuses = {p["provider"]: p for p in payload["providers"]}
        self.assertEqual(list(statuses), ["ollama"])
        self.assertFalse(statuses["ollama"]["ok"])
        self.assertIn("404", statuses["ollama"]["error"])

    async def test_partial_failure_does_not_empty_the_catalog(self):
        routes = {
            "/api/tags": (200, {"models": [{"name": "local-model"}]}),
            "/models": (401, {"error": "bad key"}),
        }
        keychain = _StubKeychain(provider_keys={"openai": "bad"})
        service = await self._service(_StubRegistry([]), keychain, routes)
        payload = await service.get()

        self.assertEqual([m["id"] for m in payload["models"]], ["local-model"])
        statuses = {p["provider"]: p for p in payload["providers"]}
        self.assertTrue(statuses["ollama"]["ok"])
        self.assertFalse(statuses["openai"]["ok"])

    async def test_cache_serves_seconds_calls_and_refresh_bypasses_it(self):
        calls: list[httpx.Request] = []
        routes = {"/api/tags": (200, {"models": [{"name": "m1"}]})}
        service = await self._service(_StubRegistry([]), _StubKeychain(), routes, calls)

        first = await service.get()
        self.assertFalse(first["cached"])
        after_first = len(calls)

        second = await service.get()
        self.assertTrue(second["cached"])
        self.assertEqual(len(calls), after_first, "a cached call must not hit the network")

        third = await service.get(refresh=True)
        self.assertFalse(third["cached"])
        self.assertGreater(len(calls), after_first, "refresh must re-query")

    async def test_invalidate_forces_a_refetch(self):
        calls: list[httpx.Request] = []
        routes = {"/api/tags": (200, {"models": [{"name": "m1"}]})}
        service = await self._service(_StubRegistry([]), _StubKeychain(), routes, calls)
        await service.get()
        after_first = len(calls)
        service.invalidate()
        await service.get()
        self.assertGreater(len(calls), after_first)

    async def test_new_models_appear_without_a_restart(self):
        """A model released provider-side shows up on the next refresh — the
        whole point of discovering instead of shipping a list."""
        state = {"models": [{"name": "old-model"}]}
        calls: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"models": state["models"]})

        service = ModelCatalogService(
            _StubRegistry([]), _StubKeychain(), transport=httpx.MockTransport(handle)
        )
        before = await service.get()
        self.assertEqual([m["id"] for m in before["models"]], ["old-model"])

        state["models"].append({"name": "brand-new-model"})
        after = await service.get(refresh=True)
        self.assertEqual([m["id"] for m in after["models"]], ["brand-new-model", "old-model"])

    async def test_real_registry_integration(self):
        """End-to-end through the real registry: a saved role config drives
        which provider is queried, with that role's own key and endpoint."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "catalog.db")
            try:
                registry = AgentRegistryService(conn, ProviderFactory(Keychain()), Keychain())
                registry.set_config(
                    "fixer",
                    AgentConfigUpdate(
                        provider="acme",
                        protocol="openai_compat",
                        base_url="https://acme.test/v1",
                        model_name="acme-1",
                    ),
                )
                routes = {
                    "/v1/models": (200, {"data": [{"id": "acme-1"}]}),
                    "/api/tags": (200, {"models": [{"name": "local"}]}),
                }
                service = ModelCatalogService(
                    registry,
                    _StubKeychain(role_keys={}, provider_keys={"acme": "acme-key"}),
                    transport=httpx.MockTransport(_handler(routes)),
                )
                payload = await service.get()
            finally:
                conn.close()

        self.assertEqual(sorted(m["id"] for m in payload["models"]), ["acme-1", "local"])
        statuses = {p["provider"]: p for p in payload["providers"]}
        self.assertIn("acme", statuses)


class TestSorting(unittest.TestCase):
    def test_dated_models_come_first_newest_to_oldest(self):
        models = [
            {"id": "b", "created": 100.0},
            {"id": "undated"},
            {"id": "a", "created": 300.0},
            {"id": "c", "created": None},
        ]
        self.assertEqual([m["id"] for m in _sorted_models(models)], ["a", "b", "c", "undated"])

    def test_undated_only_lists_sort_by_name(self):
        models = [{"id": "z"}, {"id": "a"}, {"id": "m"}]
        self.assertEqual([m["id"] for m in _sorted_models(models)], ["a", "m", "z"])


if __name__ == "__main__":
    unittest.main()
