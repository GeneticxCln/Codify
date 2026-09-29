"""A conductor someone configured and then silently ignored is a setting that does nothing (second pass).

The Conductor card offers "Custom Provider…", but the conductor's own pair has no field for an address: a
custom provider's endpoint lives on the role row that introduced it. A pair naming a slug no row defines
produced a target with no address (calls went nowhere and read as a dead endpoint); then a target that was
skipped without a word, leaving the turn to fall back and the person to wonder why the model they chose was
never used. A configured target that cannot be used now says why in the goal's log; a conductor that simply
borrows the scribe's row and cannot call tools stays the documented quiet degradation.
"""

from __future__ import annotations

from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from tests.test_conductor import ConductorTestCase, _PlainProvider, _ToolProvider

from engine.models import AgentConfigUpdate
from engine.toolcall import ToolReply


class TestAConfiguredConductorIsExplained(ConductorTestCase):
    def _executor(self, provider: Any, laya: Any = None) -> Any:
        # The shared builder leaves the settings store unattached (the app's lifespan attaches it), and
        # these tests are about what the settings say.
        executor = super()._executor(provider, laya)
        executor.settings = self.settings
        return executor

    def _warnings(self) -> list[str]:
        return [e.payload["message"] for e in self.goals.events_after(self.goal.id, 0)
                if e.type == "log" and e.payload.get("level") == "warn"]

    async def test_a_slug_no_role_defines_is_named_in_the_log(self) -> None:
        self.settings.set_str("conductor_provider", "ghost")
        self.settings.set_str("conductor_model", "ghost-model")
        executor = self._executor(_ToolProvider(replies=[ToolReply(text="hello")]))

        await executor.run_chat(self.goal.id)

        warnings = self._warnings()
        self.assertTrue(
            any("'ghost'" in w and "no role defines" in w for w in warnings), warnings
        )

    async def test_the_turn_still_answers_after_saying_so(self) -> None:
        self.settings.set_str("conductor_provider", "ghost")
        self.settings.set_str("conductor_model", "ghost-model")
        executor = self._executor(_ToolProvider(replies=[ToolReply(text="hello")]))

        await executor.run_chat(self.goal.id)

        self.assertEqual("COMPLETED", self.goals.get(self.goal.id).status)

    async def test_a_defined_custom_provider_is_used_without_a_complaint(self) -> None:
        self.settings.set_str("conductor_provider", "acme")
        self.settings.set_str("conductor_model", "acme-model")
        executor = self._executor(_ToolProvider(replies=[ToolReply(text="hello")]))
        executor.orchestrator.registry.set_config("librarian", AgentConfigUpdate(
            provider="acme", protocol="openai_compat", model_name="lib-model",
            base_url="https://llm.example.test/v1",
        ))

        await executor.run_chat(self.goal.id)

        self.assertEqual([], [w for w in self._warnings() if "conductor" in w], self._warnings())

    async def test_a_borrowing_conductor_on_a_plain_model_stays_quiet(self) -> None:
        # Nothing was configured for the conductor: no tool support is the documented quiet
        # degradation to a plain answer, and a warning on every turn of such an install is noise.
        executor = self._executor(_PlainProvider())

        await executor.run_chat(self.goal.id)

        self.assertEqual([], [w for w in self._warnings() if "conductor" in w.lower()], self._warnings())

    async def test_a_configured_conductor_that_cannot_call_tools_says_so(self) -> None:
        self.settings.set_str("conductor_provider", "groq")
        self.settings.set_str("conductor_model", "some-model")
        executor = self._executor(_PlainProvider())

        await executor.run_chat(self.goal.id)

        warnings: list[Any] = self._warnings()
        self.assertTrue(any("cannot call tools" in w and "'groq'" in w for w in warnings), warnings)
