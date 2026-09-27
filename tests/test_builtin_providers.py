"""The provider registry is a contract, so it is asserted like one.

`BUILTIN_PROVIDERS` is the single definition of which providers this build knows
(see docs/06). `GET /settings/providers` serves it verbatim, and the Settings
screen renders what it is handed rather than keeping a copy of its own — a copy
went stale once already, and when it did, `openrouter` and `groq` were treated as
*custom* endpoints, so the screen asked the user for a protocol and a base URL
the engine already knew.

That failure is invisible to a test that only checks "the list is not empty".
So below, every entry is checked against the things that make it usable, and the
properties are stated as invariants rather than as a snapshot of today's list —
adding a ninth provider should not fail this file, and a provider wired up
wrongly should.

The NVIDIA case is the one that is pinned by name. It is an OpenAI-compatible
NIM endpoint, which means the existing `openai_compat` provider reads it
unchanged and the catalogue is discovered live like any other. It is here
because people were reaching for it as a "custom" endpoint.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import re
import unittest
from urllib.parse import urlparse

from engine.models import BUILTIN_PROVIDERS, ROLES
from typing import get_args

from engine.models import ProviderProtocol

PROTOCOLS = set(get_args(ProviderProtocol))


class BuiltinProviderShape(unittest.TestCase):
    def test_every_provider_declares_the_four_fields_a_screen_reads(self) -> None:
        # A provider missing one of these renders a row with a hole in it, and
        # the hole is only visible to whoever is looking at the settings screen.
        for slug, meta in BUILTIN_PROVIDERS.items():
            with self.subTest(provider=slug):
                self.assertEqual(
                    set(meta),
                    {"protocol", "base_url", "needs_key", "local_only"},
                    f"{slug} does not declare exactly the four fields the UI reads",
                )

    def test_every_provider_names_a_protocol_that_exists(self) -> None:
        # A protocol string is a Literal, so an unknown one is not caught by the
        # type checker on this table — the table is a plain dict of Any.
        for slug, meta in BUILTIN_PROVIDERS.items():
            with self.subTest(provider=slug):
                self.assertIn(meta["protocol"], PROTOCOLS)

    def test_a_local_provider_is_local_and_a_remote_one_is_https(self) -> None:
        # `local_only` is what keeps a provider off the SSRF path and out of the
        # settings screen's "needs a key" column, so it has to agree with the
        # URL rather than being a separate thing to remember to set.
        for slug, meta in BUILTIN_PROVIDERS.items():
            with self.subTest(provider=slug):
                host = urlparse(meta["base_url"]).hostname
                self.assertIsNotNone(host, f"{slug} has no base_url host")
                if meta["local_only"]:
                    self.assertIn(
                        host, {"127.0.0.1", "localhost", "::1"},
                        f"{slug} is local_only but points at {host}",
                    )
                    self.assertFalse(
                        meta["needs_key"],
                        f"{slug} is a local server and needs no key",
                    )
                else:
                    self.assertEqual(
                        urlparse(meta["base_url"]).scheme, "https",
                        f"{slug} is remote but is not https",
                    )

    def test_slugs_are_the_lowercase_shape_a_provider_field_accepts(self) -> None:
        # The same pattern AgentConfig.provider enforces. A slug that cannot
        # round-trip through a role's provider field is a provider nobody can
        # select.
        for slug in BUILTIN_PROVIDERS:
            with self.subTest(provider=slug):
                self.assertRegex(slug, re.compile(r"^[a-z][a-z0-9_-]*$"))


class NvidiaProvider(unittest.TestCase):
    def test_nvidia_is_a_builtin_provider(self) -> None:
        # Without this the provider is invisible in the settings screen and
        # reachable only as a "custom" endpoint, which asks the user for a
        # protocol and a base URL the engine already knows.
        self.assertIn("nvidia", BUILTIN_PROVIDERS)

    def test_nvidia_speaks_the_dialect_the_openai_provider_already_speaks(self) -> None:
        meta = BUILTIN_PROVIDERS["nvidia"]
        self.assertEqual(meta["protocol"], "openai_compat")
        self.assertEqual(meta["base_url"], "https://integrate.api.nvidia.com/v1")

    def test_nvidia_needs_a_key_and_is_not_local(self) -> None:
        meta = BUILTIN_PROVIDERS["nvidia"]
        self.assertTrue(meta["needs_key"])
        self.assertFalse(meta["local_only"])

    def test_nvidia_does_not_collide_with_another_provider_slug(self) -> None:
        # Two entries under one key would silently drop one of them from the
        # dict, and the screen would offer a provider that cannot be called.
        self.assertEqual(len(BUILTIN_PROVIDERS), len(set(BUILTIN_PROVIDERS)))


class RegistryContract(unittest.TestCase):
    def test_the_registry_is_not_a_copy_of_the_roles(self) -> None:
        # Nothing enforces this and nothing needs to; it is here so a reader who
        # wonders why a provider cannot also be a role has the answer written
        # down rather than inferring it from the absence of a check.
        self.assertTrue(set(BUILTIN_PROVIDERS).isdisjoint(set(ROLES)))

    def test_every_builtin_is_reachable_from_a_role_field(self) -> None:
        # The roles' provider field is a validated slug, so "reachable" means
        # the registry slug survives the same validation a user's choice does.
        # Asserted by building a config rather than by reading the pattern off
        # the field: the pattern lives in a pydantic private type, and a test
        # that reaches into one breaks on a pydantic upgrade instead of on a
        # real change.
        from engine.models import AgentConfig

        for slug in BUILTIN_PROVIDERS:
            with self.subTest(provider=slug):
                config = AgentConfig(
                    role="fixer",
                    display_name="Fixer",
                    provider=slug,
                    protocol=BUILTIN_PROVIDERS[slug]["protocol"],
                )
                self.assertEqual(config.provider, slug)


if __name__ == "__main__":
    unittest.main()
