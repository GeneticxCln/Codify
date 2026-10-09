"""A stored API key goes only where it cannot be read off the wire (audit M5).

`validate_local_base_url` guards the `ollama` protocol and nothing else, so choosing another
protocol walked round it: `PUT /settings/agents/scribe {"provider": "openai", "base_url":
"http://10.0.0.5:8080"}` was accepted, and a listener there received `Authorization: Bearer
<the stored key>` on `/models` and `/chat/completions`. The rule is now about the *key*, not
the protocol: it is sent to an `https` endpoint, or to a loopback one, and to nothing else. A
keyless plain-http server on the LAN (vLLM, a llama.cpp box) is unaffected — there is no
credential to protect and refusing it would be a regression with nothing gained.

It is enforced at every place a key can leave: the provider constructors (so `build` and any
direct use), model discovery, and the save that would point a keyed provider at such a URL.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.db import connect
from engine.model_catalog import DiscoveryResult, ProviderTarget, discover_provider
from engine.models import AgentConfig, AgentConfigUpdate
from engine.providers import (
    AnthropicProvider,
    GoogleProvider,
    Keychain,
    OpenAICompatProvider,
    ProviderError,
    ProviderFactory,
    key_may_be_sent_to,
)
from engine.services import AgentRegistryService, ApiError

LAN = "http://10.0.0.5:8080"


class TestWhereAKeyMayGo(unittest.TestCase):
    def test_https_and_loopback_are_allowed(self) -> None:
        for url in (
            "https://api.openai.com/v1", "https://my-gateway.corp.example/v1", "https://10.0.0.5:8443",
            "http://127.0.0.1:1234/v1", "http://localhost:1234", "http://[::1]:8080", "http://127.255.0.9",
            "https://localhost:8443",
        ):
            with self.subTest(url=url):
                self.assertTrue(key_may_be_sent_to(url))

    def test_plain_http_to_anywhere_else_is_not(self) -> None:
        for url in (
            LAN, "http://192.168.1.10", "http://api.openai.com/v1", "http://example.test",
            # Names that merely look local: the host is decided by parsing it, never by its spelling.
            "http://127.evil.example", "http://127.0.0.1.evil.example", "http://localhost.evil.example",
            # Userinfo makes `127.0.0.1` the *user*, and the host is what follows the `@`.
            "http://127.0.0.1@evil.example/", "http://127.0.0.1:80@10.0.0.5/",
        ):
            with self.subTest(url=url):
                self.assertFalse(key_may_be_sent_to(url))

    def test_things_that_are_not_http_endpoints_at_all_are_not(self) -> None:
        for url in ("", "ftp://example.test", "file:///etc/passwd", "example.test", "http://", "https://"):
            with self.subTest(url=url):
                self.assertFalse(key_may_be_sent_to(url))


class TestConstructorsRefuseBeforeAnythingCanBeSent(unittest.TestCase):
    def test_a_provider_holding_a_key_will_not_be_built_for_a_plain_http_remote_host(self) -> None:
        for build in (
            lambda: OpenAICompatProvider("sk-secret", LAN),
            lambda: AnthropicProvider("sk-secret", LAN),
            lambda: GoogleProvider("sk-secret", LAN),
        ):
            with self.subTest(build=build):
                with self.assertRaises(ProviderError) as caught:
                    build()
                self.assertEqual("invalid_base_url", caught.exception.code)
                self.assertNotIn("sk-secret", caught.exception.message)
                self.assertIn("10.0.0.5", caught.exception.message)

    def test_an_error_names_the_host_and_never_the_url_it_came_from(self) -> None:
        # A base URL may carry userinfo. The refusal must not echo a credential back.
        with self.assertRaises(ProviderError) as caught:
            OpenAICompatProvider("sk-secret", "http://user:hunter2@10.0.0.5:8080/v1")
        self.assertNotIn("hunter2", caught.exception.message)

    def test_an_address_that_is_not_http_at_all_is_refused_without_echoing_its_userinfo(self) -> None:
        # The other branch of the same refusal: not plain http to a remote host, but not http(s) at all. It used
        # to quote the whole address back, password included.
        for url in ("ftp://user:hunter2@files.example/v1", "user:hunter2@files.example", "http://user:hunter2@"):
            with self.subTest(url=url):
                with self.assertRaises(ProviderError) as caught:
                    OpenAICompatProvider("sk-secret", url)
                self.assertEqual("invalid_base_url", caught.exception.code)
                self.assertNotIn("hunter2", caught.exception.message)
                self.assertIn("is not an http(s) endpoint", caught.exception.message)

    def test_a_keyless_plain_http_server_is_still_fine(self) -> None:
        OpenAICompatProvider(None, LAN)
        OpenAICompatProvider("", LAN)

    def test_the_destinations_a_key_may_reach_still_build(self) -> None:
        OpenAICompatProvider("sk-secret", "https://gateway.example/v1")
        OpenAICompatProvider("sk-secret", "http://127.0.0.1:1234/v1")
        AnthropicProvider("sk-secret", "https://api.anthropic.com")
        GoogleProvider("sk-secret", "https://generativelanguage.googleapis.com/v1beta")


class RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.conn = connect(base / "t.db")
        self.addCleanup(self.conn.close)
        self.keychain = Keychain(secrets_path=base / "secrets.json")
        self.registry = AgentRegistryService(self.conn, ProviderFactory(self.keychain), self.keychain)

    def refused(self, role: str, patch: AgentConfigUpdate) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            self.registry.set_config(role, patch)
        return caught.exception


class TestSavingAKeyedProviderAtAPlainHttpHost(RegistryCase):
    def test_a_key_in_the_same_request_is_refused_and_not_stored(self) -> None:
        error = self.refused(
            "scribe", AgentConfigUpdate(provider="openai", base_url=LAN, api_key="sk-secret"),
        )

        self.assertEqual("invalid_base_url", error.code)
        self.assertFalse(self.keychain.get("codify/agents/scribe"), "the key was stored anyway")
        self.assertNotEqual(LAN, self.registry.get_config("scribe").base_url)

    def test_retargeting_a_provider_that_already_has_a_key_is_refused(self) -> None:
        # The audit's own reproduction: the key was saved earlier, and the *URL* moves.
        self.keychain.set_provider_key("openai", "sk-stored")

        error = self.refused("scribe", AgentConfigUpdate(provider="openai", base_url=LAN))

        self.assertEqual("invalid_base_url", error.code)
        self.assertNotEqual(LAN, self.registry.get_config("scribe").base_url)

    def test_the_fallback_target_is_held_to_the_same_rule(self) -> None:
        self.keychain.set_provider_key("openai", "sk-stored")

        error = self.refused(
            "scribe",
            AgentConfigUpdate(fallback_provider="openai", fallback_protocol="openai_compat",
                              fallback_base_url=LAN, fallback_model_name="m"),
        )

        self.assertEqual("invalid_base_url", error.code)

    def test_keyless_and_secure_destinations_are_accepted(self) -> None:
        # No key stored for this provider: nothing to protect, and a LAN box is a real setup.
        saved = self.registry.set_config(
            "scribe",
            AgentConfigUpdate(provider="lan-vllm", protocol="openai_compat", base_url=LAN, model_name="m"),
        )
        self.assertEqual(LAN, saved.base_url)
        # A key with an https gateway, and with a loopback server.
        self.keychain.set_provider_key("openai", "sk-stored")
        for url in ("https://gateway.example/v1", "http://127.0.0.1:1234/v1"):
            with self.subTest(url=url):
                self.assertEqual(
                    url, self.registry.set_config("fixer", AgentConfigUpdate(provider="openai", base_url=url)).base_url,
                )


class TestTheFactoryIsTheLastDoor(RegistryCase):
    def test_a_config_that_reached_the_database_some_other_way_still_sends_nothing(self) -> None:
        # Old rows, a hand-edited database, a future code path: the save-time check is a courtesy
        # and this is the guarantee. Nothing sends the key, because the provider cannot be built.
        self.keychain.set_provider_key("openai", "sk-stored")
        config = AgentConfig(
            role="scribe", display_name="Scribe", provider="openai", protocol="openai_compat",
            model_name="m", base_url=LAN,
        )

        with self.assertRaises(ProviderError) as caught:
            ProviderFactory(self.keychain).build(config)

        self.assertEqual("invalid_base_url", caught.exception.code)


class TestDiscoveryDoesNotSendTheKeyEither(unittest.TestCase):
    def test_model_discovery_reports_the_refusal_and_makes_no_request(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"data": []})

        async def go() -> DiscoveryResult:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await discover_provider(
                    ProviderTarget(provider="openai", protocol="openai_compat", base_url=LAN, api_key="sk-secret"),
                    client=client,
                )

        result = asyncio.run(go())

        self.assertEqual([], seen, "a request left the engine carrying the key")
        self.assertFalse(result.ok)
        self.assertNotIn("sk-secret", str(result.error))

    def test_discovery_for_a_keyless_lan_server_still_asks(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"data": [{"id": "m1"}]})

        async def go() -> DiscoveryResult:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await discover_provider(
                    ProviderTarget(provider="lan", protocol="openai_compat", base_url=LAN, needs_key=False),
                    client=client,
                )

        result = asyncio.run(go())

        self.assertEqual(1, len(seen))
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
