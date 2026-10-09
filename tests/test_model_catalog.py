"""Model discovery tests.

The guarantee under test: the catalog is built only from what providers actually
report. An empty or unreachable configuration must produce an empty catalog with
per-provider reasons — never a plausible-looking list compiled into the binary.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from collections.abc import Callable

import httpx

from engine.db import connect
from engine import model_catalog
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


def reset_ollama_windows() -> None:
    """Forget what Ollama said about its models: the answers are kept in the module, so every test in the process shares them."""
    model_catalog._OLLAMA_WINDOWS.clear()


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
    def __init__(
        self, role: str, provider: str, protocol: str, base_url: str | None = None,
        api_key_ref: str | None = None, model_name: str = "m",
                 fallback_provider: str | None = None, fallback_protocol: str | None = None,
                 fallback_base_url: str | None = None):
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
    def __init__(self, configs: list[Any] | None = None) -> None:
        self._configs = configs or []

    def list_configs(self) -> list[Any]:
        return self._configs


def _handler(
    routes: dict[str, tuple[int, Any]], calls: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
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


def _client(
    routes: dict[str, tuple[int, Any]], calls: list[httpx.Request] | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_handler(routes, calls)))


class TestProtocolDiscovery(unittest.IsolatedAsyncioTestCase):
    async def test_openai_compat(self) -> None:
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

    async def test_anthropic_uses_its_own_headers_and_display_name(self) -> None:
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

    async def test_google_strips_the_resource_prefix_and_follows_pages(self) -> None:
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

    async def test_google_discovery_sends_the_key_in_the_header_not_the_url(self) -> None:
        """Same credential rule as GoogleProvider.complete(): a query-string key
        lands in proxy and server access logs, so discovery must use the
        x-goog-api-key header too."""
        calls: list[httpx.Request] = []
        routes = {"/models": (200, {"models": [{"name": "models/gemini-2.5-flash"}]})}
        target = ProviderTarget(
            "google", "google", "https://generativelanguage.googleapis.com/v1beta", "goog-key"
        )
        async with _client(routes, calls) as client:
            result = await discover_provider(target, client=client)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(calls[0].headers["x-goog-api-key"], "goog-key")
        self.assertNotIn("key=", str(calls[0].url), "the key must not ride in the URL query")

    async def test_ollama_lists_local_models(self) -> None:
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

    async def test_no_embedding_name_filtering(self) -> None:
        """Ollama's list is reported as-is: filtering by name is how a hardcoded
        idea of 'a real model' creeps back in."""
        routes = {"/api/tags": (200, {"models": [{"name": "nomic-embed-text:latest"}]})}
        target = ProviderTarget("ollama", "ollama", "http://127.0.0.1:11434", needs_key=False)
        async with _client(routes) as client:
            result = await discover_provider(target, client=client)
        self.assertEqual([m["id"] for m in result.models], ["nomic-embed-text:latest"])


class TestProviderFailures(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_is_reported_without_a_request(self) -> None:
        calls: list[httpx.Request] = []
        target = ProviderTarget("openai", "openai_compat", "https://api.openai.com/v1", "")
        async with _client({}, calls) as client:
            result = await discover_provider(target, client=client)
        self.assertFalse(result.ok)
        assert result.error is not None, "a failed discovery says why"
        self.assertIn("no API key", result.error)
        self.assertEqual(calls, [], "an unauthenticated provider must not be called")

    async def test_401_explains_itself(self) -> None:
        routes = {"/models": (401, {"error": {"message": "invalid api key"}})}
        target = ProviderTarget("openai", "openai_compat", "https://api.openai.com/v1", "bad")
        async with _client(routes) as client:
            result = await discover_provider(target, client=client)
        self.assertFalse(result.ok)
        assert result.error is not None, "a failed discovery says why"
        self.assertIn("HTTP 401", result.error)
        self.assertIn("check the API key", result.error)

    async def test_unknown_protocol_is_reported(self) -> None:
        target = ProviderTarget("weird", "carrier-pigeon", "https://example.com", "k")
        result = await discover_provider(target)
        self.assertFalse(result.ok)
        assert result.error is not None, "a failed discovery says why"
        self.assertIn("unknown protocol", result.error)

    async def test_transport_error_does_not_raise(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        target = ProviderTarget("ollama", "ollama", "http://127.0.0.1:11434", needs_key=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
            result = await discover_provider(target, client=client)
        self.assertFalse(result.ok)
        assert result.error is not None, "a failed discovery says why"
        self.assertIn("connection refused", result.error)


class TestTargets(unittest.TestCase):
    def test_role_configs_keys_and_locals_are_all_queried(self) -> None:
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

    def test_a_custom_slug_speaking_the_ollama_protocol_needs_no_key(self) -> None:
        """L6: any non-built-in slug was assumed to need a key, so a second local Ollama (a
        different port, a different machine on loopback) answered "no API key configured" without
        ever being asked. `role_repair.target_needs_key` already decides this by protocol."""
        registry = _StubRegistry([
            _StubConfig("fixer", "my-ollama", "ollama", base_url="http://127.0.0.1:11435"),
            _StubConfig("planner", "acme", "openai_compat", base_url="https://acme.test/v1"),
        ])
        targets = {t.provider: t for t in _targets(registry, _StubKeychain())}

        self.assertFalse(targets["my-ollama"].needs_key)
        self.assertTrue(targets["acme"].needs_key, "a custom OpenAI-compatible endpoint still needs a key")

    def test_a_fallback_custom_ollama_needs_no_key_either(self) -> None:
        registry = _StubRegistry([
            _StubConfig(
                "fixer", "anthropic", "anthropic",
                fallback_provider="lab-ollama", fallback_protocol="ollama",
                fallback_base_url="http://127.0.0.1:11436",
            ),
        ])
        targets = {t.provider: t for t in _targets(registry, _StubKeychain())}

        self.assertFalse(targets["lab-ollama"].needs_key)

    def test_ollama_always_present_even_with_no_configs(self) -> None:
        targets = _targets(_StubRegistry([]), _StubKeychain())
        self.assertEqual([t.provider for t in targets], ["ollama"])

    def test_builtin_base_url_used_when_config_has_none(self) -> None:
        registry = _StubRegistry([_StubConfig("fixer", "deepseek", "openai_compat")])
        targets = {t.provider: t for t in _targets(registry, _StubKeychain(provider_keys={"deepseek": "k"}))}
        self.assertEqual(targets["deepseek"].base_url, "https://api.deepseek.com")

    def test_a_fallback_provider_is_discoverable_too(self) -> None:
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

    def test_a_role_set_endpoint_beats_the_seed_defaults(self) -> None:
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
    async def _service(
        self, registry: Any, keychain: Any, routes: dict[str, tuple[int, Any]],
        calls: list[httpx.Request] | None = None,
    ) -> ModelCatalogService:
        return ModelCatalogService(
            registry, keychain, ttl_s=60, transport=httpx.MockTransport(_handler(routes, calls))
        )

    async def test_empty_configuration_yields_no_models(self) -> None:
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

    async def test_partial_failure_does_not_empty_the_catalog(self) -> None:
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

    async def test_cache_serves_seconds_calls_and_refresh_bypasses_it(self) -> None:
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

    async def test_invalidate_forces_a_refetch(self) -> None:
        calls: list[httpx.Request] = []
        routes = {"/api/tags": (200, {"models": [{"name": "m1"}]})}
        service = await self._service(_StubRegistry([]), _StubKeychain(), routes, calls)
        await service.get()
        after_first = len(calls)
        service.invalidate()
        await service.get()
        self.assertGreater(len(calls), after_first)

    async def test_new_models_appear_without_a_restart(self) -> None:
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

    async def test_real_registry_integration(self) -> None:
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
    def test_dated_models_come_first_newest_to_oldest(self) -> None:
        models: list[dict[str, Any]] = [
            {"id": "b", "created": 100.0},
            {"id": "undated"},
            {"id": "a", "created": 300.0},
            {"id": "c", "created": None},
        ]
        self.assertEqual([m["id"] for m in _sorted_models(models)], ["a", "b", "c", "undated"])

    def test_undated_only_lists_sort_by_name(self) -> None:
        models = [{"id": "z"}, {"id": "a"}, {"id": "m"}]
        self.assertEqual([m["id"] for m in _sorted_models(models)], ["a", "m", "z"])


class TestOllamaDiscoveryHoldsTheLocalRule(unittest.IsolatedAsyncioTestCase):
    """Invariant 5 (docs/00 §6.5): a local provider's base_url passes `validate_local_base_url` before every request.

    The `OllamaProvider` constructor enforced it and the discovery path did not, so a stored `ollama`-protocol
    provider pointed at a LAN host was asked for its model list anyway (review of 2026-09-29). Save-time
    validation makes that unreachable through the settings screen; this is the same rule held where the request
    is made.
    """

    async def discover(self, base_url: str) -> tuple[Any, list[httpx.Request]]:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"models": [{"name": "llama3:8b"}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with client:
            result = await discover_provider(
                ProviderTarget("ollama", "ollama", base_url, needs_key=False), client=client,
            )
        return result, seen

    async def test_a_lan_host_is_refused_and_never_asked(self) -> None:
        result, seen = await self.discover("http://10.0.0.5:11434")

        self.assertFalse(result.ok)
        self.assertIn("localhost", result.error or "")
        self.assertEqual([], seen, "a non-loopback host was sent a request")

    async def test_https_is_refused_for_a_local_provider(self) -> None:
        result, seen = await self.discover("https://127.0.0.1:11434")

        self.assertFalse(result.ok)
        self.assertEqual([], seen)

    async def test_loopback_still_discovers(self) -> None:
        result, seen = await self.discover("http://127.0.0.1:11434")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(["llama3:8b"], [m["id"] for m in result.models])
        # The list, and then one `/api/show` per model for its context length. Every one of them is made to
        # the host that passed the rule: the second request is held to the same invariant as the first.
        self.assertEqual({"/api/tags", "/api/show"}, {r.url.path for r in seen})
        self.assertEqual({"127.0.0.1"}, {r.url.host for r in seen})


class TestContextLength(unittest.IsolatedAsyncioTestCase):
    """What a provider says a model's context window is, and what is never made up.

    A number a person reads next to a model name is a claim about whether their prompt fits. Every path
    here is "the provider said it" or "unknown": nothing is read from a model's name, nothing is rounded,
    and a provider that fails to answer costs the number and never the list.
    """

    async def asyncSetUp(self) -> None:
        reset_ollama_windows()

    async def asyncTearDown(self) -> None:
        reset_ollama_windows()

    def test_only_plainly_a_context_length_is_taken(self) -> None:
        from engine.model_catalog import MAX_CONTEXT_TOKENS, _positive_int

        for good, want in ((8192, 8192), (131072.0, 131072), (1, 1), (MAX_CONTEXT_TOKENS, MAX_CONTEXT_TOKENS)):
            self.assertEqual(_positive_int(good), want, good)
        bad_values: tuple[Any, ...] = (True, False, 0, -4096, 4096.5, "4096", None, [], {}, MAX_CONTEXT_TOKENS + 1, float("inf"), float("nan"))
        for bad in bad_values:
            self.assertIsNone(_positive_int(bad), repr(bad))

    async def test_openai_compatible_gateways_report_it_under_the_names_they_use(self) -> None:
        rows = [
            {"id": "or", "context_length": 200000},
            {"id": "groq", "context_window": 131072},
            {"id": "mistral", "max_context_length": 32768},
            {"id": "vllm", "max_model_len": 8192},
            {"id": "openai-style"},
            {"id": "junk", "context_length": "lots", "context_window": True},
            {"id": "two", "context_length": 1000, "context_window": 9999},
        ]
        target = ProviderTarget("gateway", "openai_compat", "https://gw.example/v1", "k")
        async with _client({"/models": (200, {"data": rows})}) as client:
            result = await discover_provider(target, client=client)
        got = {m["id"]: m["context_tokens"] for m in result.models}
        self.assertEqual(got, {
            "or": 200000, "groq": 131072, "mistral": 32768, "vllm": 8192,
            "openai-style": None, "junk": None, "two": 1000,
        })

    async def test_anthropic_reports_its_max_input_tokens(self) -> None:
        routes = {"/v1/models": (200, {"data": [
            {"id": "claude-x", "display_name": "X", "max_input_tokens": 1000000, "max_tokens": 128000},
            {"id": "claude-old", "display_name": "Old"},
        ]})}
        target = ProviderTarget("anthropic", "anthropic", "https://api.anthropic.com", "k")
        async with _client(routes) as client:
            result = await discover_provider(target, client=client)
        self.assertEqual({m["id"]: m["context_tokens"] for m in result.models}, {"claude-x": 1000000, "claude-old": None})

    async def test_google_reports_its_input_token_limit(self) -> None:
        routes = {"/models": (200, {"models": [
            {"name": "models/gemini-a", "inputTokenLimit": 1048576, "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-b"},
        ]})}
        target = ProviderTarget("google", "google", "https://generativelanguage.googleapis.com/v1beta", "k")
        async with _client(routes) as client:
            result = await discover_provider(target, client=client)
        self.assertEqual({m["id"]: m["context_tokens"] for m in result.models}, {"gemini-a": 1048576, "gemini-b": None})

    async def test_every_entry_carries_the_field_even_when_it_is_unknown(self) -> None:
        # A consumer reads `m["context_tokens"]`; a protocol that left the key out would make that a KeyError.
        for protocol, base, routes in (
            ("openai_compat", "https://x/v1", {"/models": (200, {"data": [{"id": "a"}]})}),
            ("anthropic", "https://x", {"/v1/models": (200, {"data": [{"id": "a"}]})}),
            ("google", "https://x/v1beta", {"/models": (200, {"models": [{"name": "models/a"}]})}),
            ("ollama", "http://127.0.0.1:11434", {"/api/tags": (200, {"models": [{"name": "a"}]})}),
        ):
            target = ProviderTarget(protocol, protocol, base, "k", needs_key=protocol != "ollama")
            async with _client(routes) as client:
                result = await discover_provider(target, client=client)
            self.assertTrue(result.ok, (protocol, result.error))
            self.assertIn("context_tokens", result.models[0], protocol)
            self.assertIsNone(result.models[0]["context_tokens"], protocol)


class TestOllamaContextLength(unittest.IsolatedAsyncioTestCase):
    """`/api/tags` has no context length; `/api/show` does. Asked for politely, and never at the list's expense."""

    TARGET = ProviderTarget("ollama", "ollama", "http://127.0.0.1:11434", needs_key=False)

    async def asyncSetUp(self) -> None:
        reset_ollama_windows()
        self.shown: list[str] = []
        self.flying = 0
        self.most_flying = 0

    async def asyncTearDown(self) -> None:
        reset_ollama_windows()

    def _client(self, models: list[dict[str, Any]], show: Any, delay: float = 0.0) -> httpx.AsyncClient:
        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/tags"):
                return httpx.Response(200, json={"models": models})
            if request.url.path.endswith("/api/show"):
                name = json.loads(request.content)["model"]
                self.shown.append(name)
                self.flying += 1
                self.most_flying = max(self.most_flying, self.flying)
                try:
                    if delay:
                        await asyncio.sleep(delay)
                finally:
                    self.flying -= 1
                answer = show(name) if callable(show) else show
                if isinstance(answer, int):
                    return httpx.Response(answer, json={"error": "no"})
                return httpx.Response(200, json=answer)
            return httpx.Response(404, json={})

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    async def _discover(self, client: httpx.AsyncClient) -> Any:
        async with client:
            return await discover_provider(self.TARGET, client=client)

    async def test_the_architecture_prefixed_key_is_the_context_length(self) -> None:
        shows = {
            "llama": {"model_info": {"general.architecture": "llama", "llama.context_length": 131072}},
            "qwen": {"model_info": {"general.architecture": "qwen2", "qwen2.context_length": 32768, "qwen2.block_count": 28}},
            "none": {"model_info": {"general.architecture": "x"}},
            "bad": {"model_info": {"x.context_length": "big"}},
            "plain": {"model_info": {"context_length": 4096}},
        }
        client = self._client([{"name": n, "digest": n} for n in shows], lambda n: shows[n])
        result = await self._discover(client)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(
            {m["id"]: m["context_tokens"] for m in result.models},
            {"llama": 131072, "qwen": 32768, "none": None, "bad": None, "plain": 4096},
        )

    async def test_a_show_that_fails_costs_the_number_and_never_the_list(self) -> None:
        for answer in (404, 500, {"model_info": "nonsense"}, {}):
            reset_ollama_windows()
            result = await self._discover(self._client([{"name": "m"}], answer))
            self.assertTrue(result.ok, (answer, result.error))
            self.assertEqual([(m["id"], m["context_tokens"]) for m in result.models], [("m", None)], answer)

    async def test_a_slow_ollama_is_given_a_budget_and_not_the_whole_discovery(self) -> None:
        import time
        from unittest import mock

        client = self._client([{"name": f"m{i}", "digest": str(i)} for i in range(5)], {"model_info": {"a.context_length": 8192}}, delay=5.0)
        with mock.patch("engine.model_catalog.OLLAMA_SHOW_BUDGET_S", 0.2):
            started = time.monotonic()
            result = await self._discover(client)
            took = time.monotonic() - started
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.models), 5, "the list was lost because a show was slow")
        self.assertTrue(all(m["context_tokens"] is None for m in result.models))
        self.assertLess(took, 2.0, f"discovery waited {took:.1f}s on a slow show")
        self.assertEqual(self.flying, 0, "a request that outlived the budget was left running")

    async def test_a_show_past_the_budget_is_finished_with_before_the_lookup_returns(self) -> None:
        # Checked the instant it returns, with nothing awaited in between: a cancelled task that nobody awaits
        # is still running until the loop next visits it, and closing the client (which yields) hides that.
        from unittest import mock

        from engine.model_catalog import _ollama_windows

        rows = [{"name": f"m{i}", "digest": str(i)} for i in range(4)]
        client = self._client(rows, {"model_info": {"a.context_length": 8192}}, delay=5.0)
        with mock.patch("engine.model_catalog.OLLAMA_SHOW_BUDGET_S", 0.1):
            async with client:
                found = await _ollama_windows("http://127.0.0.1:11434", client, rows)
                self.assertEqual(self.flying, 0, "a request past the budget was cancelled but not awaited")
        self.assertEqual(found, {})

    async def test_what_ollama_said_is_remembered_by_digest(self) -> None:
        answer = {"model_info": {"a.context_length": 8192}}
        models = [{"name": "m", "digest": "d1"}]
        await self._discover(self._client(models, answer))
        await self._discover(self._client(models, answer))
        self.assertEqual(self.shown, ["m"], "an unchanged model was asked about twice")
        # A new digest is a different model under the same name: asked again, and the new answer is used.
        changed = await self._discover(self._client([{"name": "m", "digest": "d2"}], {"model_info": {"a.context_length": 16384}}))
        self.assertEqual(self.shown, ["m", "m"])
        self.assertEqual(changed.models[0]["context_tokens"], 16384)

    async def test_a_failed_show_is_asked_again_next_time(self) -> None:
        models = [{"name": "m", "digest": "d1"}]
        first = await self._discover(self._client(models, 500))
        self.assertIsNone(first.models[0]["context_tokens"])
        second = await self._discover(self._client(models, {"model_info": {"a.context_length": 8192}}))
        self.assertEqual(second.models[0]["context_tokens"], 8192, "a failure was remembered as an answer")

    async def test_only_the_newest_are_asked_and_only_a_few_at_once(self) -> None:
        from engine.model_catalog import OLLAMA_SHOW_CONCURRENCY, OLLAMA_SHOW_MAX_MODELS

        total = OLLAMA_SHOW_MAX_MODELS + 36
        models = [
            {"name": f"m{i:03d}", "digest": str(i), "modified_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z"}
            for i in range(total)
        ]
        result = await self._discover(self._client(models, {"model_info": {"a.context_length": 8192}}, delay=0.01))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.models), total, "the whole list is still listed")
        self.assertEqual(len(self.shown), OLLAMA_SHOW_MAX_MODELS)
        newest = {f"m{i:03d}" for i in range(total - OLLAMA_SHOW_MAX_MODELS, total)}
        self.assertEqual(set(self.shown), newest, "it asked about old models while newer ones had no number")
        self.assertLessEqual(self.most_flying, OLLAMA_SHOW_CONCURRENCY)
        known = {m["id"] for m in result.models if m["context_tokens"] is not None}
        self.assertEqual(known, newest)


if __name__ == "__main__":
    unittest.main()
