"""Key storage: the OS keyring, and the file backend that keeps keys saveable.

The file backend exists because "add an API key" silently failing is the worst
possible failure for a provider-based app: every provider reports "no key
configured" and the user is told nothing about why. These tests pin the
behaviour that makes a key survive an engine restart, stay out of other
providers' requests, and never take the engine down when the store is corrupt.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from engine.providers import BaseProvider, Keychain


def file_backed(**kwargs: Any) -> Keychain:
    """A keychain forced onto the file backend, whatever the host has installed."""
    with patch.object(Keychain, "_keyring_module", return_value=None):
        return Keychain(**kwargs)


class TestKeychainFileBackend(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "secrets.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_backend_is_reported_honestly(self) -> None:
        kc = file_backed(secrets_path=self.path)
        self.assertEqual(kc.backend, "file")
        self.assertIn(str(self.path), kc.describes_backend())
        self.assertIn("0600", kc.describes_backend())

    def test_provider_key_round_trips_and_survives_a_restart(self) -> None:
        kc = file_backed(secrets_path=self.path)
        kc.set_provider_key("openai", "sk-live-123")

        self.assertTrue(kc.has_provider_key("openai"))
        self.assertEqual(kc.get_provider_key("openai"), "sk-live-123")

        # A new instance is what an engine restart looks like.
        restarted = file_backed(secrets_path=self.path)
        self.assertEqual(restarted.get_provider_key("openai"), "sk-live-123")

    def test_store_file_is_owner_only(self) -> None:
        kc = file_backed(secrets_path=self.path)
        kc.set_provider_key("anthropic", "sk-ant")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.path.read_text()), {"providers/anthropic": "sk-ant"})

    def test_no_stray_temp_file_after_write(self) -> None:
        kc = file_backed(secrets_path=self.path)
        kc.set_provider_key("openai", "sk-1")
        kc.set_provider_key("groq", "gsk-1")
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [], "atomic write must not leave a temp file behind")

    def test_a_key_is_never_returned_for_another_provider(self) -> None:
        """The rule that keeps one provider's credential off another's endpoint."""
        kc = file_backed(secrets_path=self.path)
        kc.set_provider_key("openai", "sk-openai")
        self.assertEqual(kc.get_provider_key("anthropic"), "")
        self.assertEqual(kc.get_provider_key("deepseek"), "")

    def test_role_scoped_key_round_trips(self) -> None:
        """`AgentRegistryService.set_config(api_key=…)` stores a role-scoped ref."""
        kc = file_backed(secrets_path=self.path)
        ref = kc.set("fixer", "sk-role")
        self.assertEqual(ref, "codify/agents/fixer")
        self.assertEqual(kc.get(ref), "sk-role")

    def test_environment_variable_still_resolves(self) -> None:
        kc = file_backed(secrets_path=self.path)
        os.environ["GROQ_API_KEY"] = "gsk-from-env"
        try:
            self.assertEqual(kc.get_provider_key("groq"), "gsk-from-env")
        finally:
            del os.environ["GROQ_API_KEY"]

    def test_stored_key_wins_over_environment(self) -> None:
        kc = file_backed(secrets_path=self.path)
        kc.set_provider_key("groq", "gsk-stored")
        os.environ["GROQ_API_KEY"] = "gsk-from-env"
        try:
            self.assertEqual(kc.get_provider_key("groq"), "gsk-stored")
        finally:
            del os.environ["GROQ_API_KEY"]

    def test_ollama_needs_no_key(self) -> None:
        kc = file_backed(secrets_path=self.path)
        self.assertTrue(kc.has_provider_key("ollama"))

    def test_corrupt_store_reads_as_empty_instead_of_crashing(self) -> None:
        self.path.write_text("{ this is not json")
        kc = file_backed(secrets_path=self.path)
        # Reads stay lenient: a corrupt store degrades to "no key", never a
        # dead app.
        self.assertEqual(kc.get_provider_key("openai"), "")
        # But a SAVE must not silently wipe it: the empty-dict read-before-write
        # would discard the corrupt (possibly recoverable) bytes. The save fails
        # with a coded, actionable error and the file is left untouched.
        with self.assertRaises(Exception) as ctx:
            kc.set_provider_key("openai", "sk-new")
        self.assertEqual(getattr(ctx.exception, "code", None), "secrets_unreadable")
        self.assertEqual(self.path.read_text(), "{ this is not json")
        # Recovery is explicit: once a human fixes or removes the file, saves
        # work again.
        self.path.write_text("{}")
        kc.set_provider_key("openai", "sk-new")
        self.assertEqual(kc.get_provider_key("openai"), "sk-new")

    def test_missing_store_is_not_an_error(self) -> None:
        kc = file_backed(secrets_path=self.path)
        self.assertEqual(kc.get_provider_key("openai"), "")
        self.assertFalse(kc.has_provider_key("openai"))
        self.assertFalse(self.path.exists(), "reading must not create the store")

    def test_unwritable_store_raises_a_coded_error(self) -> None:
        kc = file_backed(secrets_path=self.path)
        with patch.object(Keychain, "_write_file", side_effect=OSError("read-only filesystem")):
            with self.assertRaises(Exception) as ctx:
                kc.set_provider_key("openai", "sk")
        self.assertEqual(getattr(ctx.exception, "code", None), "secrets_unwritable")


class TestRoleKeyRename(unittest.TestCase):
    """A role rename must not strand the key the user already stored.

    Role ids changed once (coder→fixer, tester→verifier, ...). A key left under the
    old id would make the renamed role look unconfigured forever, and the only fix
    would be re-entering a secret we already have.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "secrets.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_a_role_key_follows_the_renamed_role(self) -> None:
        kc = file_backed(secrets_path=self.path)
        old_ref = kc.set("coder", "sk-role-scoped")
        self.assertEqual(old_ref, "codify/agents/coder")

        self.assertTrue(kc.rename_role_key("coder", "fixer"))
        self.assertEqual(kc.get("codify/agents/fixer"), "sk-role-scoped")
        self.assertEqual(kc.get(old_ref), "", "the old id must not keep a live secret")

    def test_renaming_a_role_with_no_key_changes_nothing(self) -> None:
        kc = file_backed(secrets_path=self.path)
        self.assertFalse(kc.rename_role_key("tester", "verifier"))
        self.assertFalse(self.path.exists(), "nothing to move means nothing written")

    def test_a_provider_key_is_not_touched_by_a_role_rename(self) -> None:
        """Provider-scoped and role-scoped entries are different things."""
        kc = file_backed(secrets_path=self.path)
        kc.set_provider_key("openai", "sk-provider")
        kc.set("reviewer", "sk-role")
        kc.rename_role_key("reviewer", "critic")
        self.assertEqual(kc.get_provider_key("openai"), "sk-provider")


class _HangingProvider(BaseProvider):
    """A provider whose complete() outlives any sane probe deadline."""

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
        await asyncio.sleep(60)
        return "never"


class TestConnectionProbeDeadline(unittest.IsolatedAsyncioTestCase):
    async def test_a_hung_endpoint_is_reported_within_the_documented_15s(self) -> None:
        """`01` §3 promises `ping` at 15s. The probe shares `complete` with real
        generation, whose HTTP client waits 120s (180s for Ollama) — without a
        deadline of its own, a hung endpoint held the settings screen for that
        whole budget before saying what the user needed to know at 15."""
        started = time.monotonic()
        ok, message = await _HangingProvider().test_connection("any-model")
        waited = time.monotonic() - started
        self.assertFalse(ok)
        self.assertIn("15s", message)
        self.assertLess(waited, 20, "the probe must cut off well inside the generation budget")
        self.assertGreater(waited, 14, "and must actually have waited the documented window")


if __name__ == "__main__":
    unittest.main()
