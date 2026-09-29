"""The role preflight: one line at the top of a run, naming what cannot work.

The failure this exists for is specific. A store can hold a role with no model
and five roles whose provider has no credential, and the engine will then
discover that one role at a time, one dead run at a time, naming only the role
it happened to reach. Every diagnosis it produced was correct; the complaint is
that the user had to pay a run to collect seven of them.

So the assertions here are about *completeness* and *timing* — all of them, at
once, before the first call — and about staying quiet when there is nothing to
say, which is the half a preflight usually gets wrong.
"""

from __future__ import annotations

import unittest
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from tests.test_design_role import (  # noqa: F401 — the rig is shared, not duplicated
    MockFactory,
    MockProvider,
    SkippedGate,
    _Harness,
    _responses,
)
from engine.models import ROLES, AgentConfigUpdate
from engine.role_repair import config_problems, plan_role_repair

KEYS = {
    "ollama": {"needs_key": False, "has_key": True},
    "anthropic": {"needs_key": True, "has_key": False},
    "openai": {"needs_key": True, "has_key": False},
    "deepseek": {"needs_key": True, "has_key": False},
}
# The same facts in the list shape `plan_role_repair` takes, so the agreement
# test below is comparing the two callers rather than two input formats.
KEY_STATUS = [
    {"provider": p, "has_key": v["has_key"], "needs_key": v["needs_key"]}
    for p, v in KEYS.items()
]


def _config(role: str, provider: str, model: str, **extra: Any) -> dict[str, Any]:
    """One stored role row, in the shape both callers pass around."""
    return {
        "role": role,
        "provider": provider,
        "model_name": model,
        "protocol": "ollama" if provider == "ollama" else "openai-chat",
        "fallback_provider": "",
        "fallback_model_name": "",
        **extra,
    }


class ConfigProblemsTests(unittest.TestCase):
    """The offline rule, on its own."""

    def test_every_broken_role_is_named_with_its_own_reason(self) -> None:
        found = dict(config_problems([
            _config("laya", "ollama", ""),
            _config("librarian", "ollama", ""),
            _config("planner", "anthropic", "claude-opus-5"),
            _config("fixer", "ollama", "qwen2.5-coder:7b"),
        ], KEYS))
        self.assertEqual(found["laya"], "no model is chosen")
        self.assertEqual(found["librarian"], "no model is chosen")
        self.assertEqual(
            found["planner"], "anthropic needs a credential and none is stored"
        )
        # The one role that is fine is not in the answer at all.
        self.assertNotIn("fixer", found)

    def test_a_working_configuration_says_nothing(self) -> None:
        self.assertEqual(
            config_problems([_config(r, "ollama", "qwen2.5-coder:7b") for r in ROLES], KEYS),
            [],
        )

    def test_a_role_with_no_stored_row_is_still_reported(self) -> None:
        """The one case the repair screen cannot see, because it has no row."""
        found = dict(config_problems(
            [_config(r, "ollama", "qwen2.5-coder:7b") for r in ROLES if r != "design"],
            KEYS,
            ROLES,
        ))
        self.assertEqual(list(found), ["design"])
        self.assertIn("no configuration is stored", found["design"])

    def test_a_role_that_can_run_on_its_fallback_is_not_reported(self) -> None:
        """Warning about a role that will run teaches people to ignore warnings."""
        found = config_problems([
            _config("fixer", "anthropic", "claude-sonnet-4-6",
                    fallback_provider="ollama", fallback_model_name="qwen2.5-coder:7b",
                    fallback_protocol="ollama"),
        ], KEYS)
        self.assertEqual(found, [])

    def test_a_role_whose_fallback_also_needs_a_key_is_reported_with_both_halves(self) -> None:
        found = dict(config_problems([
            _config("fixer", "anthropic", "claude-sonnet-4-6",
                    fallback_provider="openai", fallback_model_name="gpt-4.1-mini",
                    fallback_protocol="openai-chat"),
        ], KEYS))
        self.assertIn("anthropic needs a credential", found["fixer"])
        self.assertIn("fallback openai", found["fixer"])

    def test_an_unknown_provider_is_treated_as_needing_a_key(self) -> None:
        """The honest guess: the engine has no record of a keyless custom slug."""
        found = dict(config_problems([_config("scribe", "my-proxy", "m")], KEYS))
        self.assertIn("credential", found["scribe"])

    def test_it_agrees_with_the_repair_plan_on_the_same_database(self) -> None:
        """One rule, two screens. A disagreement here is a bug in one of them."""
        rows = [
            _config("laya", "ollama", ""),
            _config("librarian", "ollama", ""),
            _config("planner", "anthropic", "claude-opus-5"),
            _config("fixer", "ollama", "qwen2.5-coder:7b"),
        ]
        preflight = {role for role, _ in config_problems(rows, KEYS)}
        # An empty catalog and empty discovery: the plan can only act on the
        # same configuration facts, so anything it repairs, this must name.
        repair = {role for role, _ in plan_role_repair(rows, KEY_STATUS, [], []).to_repair}
        self.assertEqual(preflight, repair)


class ACustomProviderThatHoldsItsOwnKeyTests(unittest.TestCase):
    """A role's own stored credential counts (second audit pass, 2026-09-29).

    A custom provider's key is stored *for the role* (`api_key_ref`): that is what the role card's key field
    does, and what the factory reads first. The preflight and the repair plan only asked the provider table,
    which lists the built-in slugs, so every role on a custom provider was reported "needs a credential and
    none is stored" while it was being called successfully — on every goal, and by the Repair button, which
    then treated a working role as one to repoint. Found because a real-model baseline printed "8 of 8 agent
    roles cannot be called" on every run that then called all eight.
    """

    def _custom(self, **extra: Any) -> dict[str, Any]:
        row = _config("fixer", "my-proxy", "m", **extra)
        row["protocol"] = "openai_compat"
        return row

    def test_a_role_holding_its_own_key_is_not_reported(self) -> None:
        self.assertEqual([], config_problems([self._custom(has_role_key=True)], KEYS))

    def test_the_repair_plan_leaves_such_a_role_alone(self) -> None:
        plan = plan_role_repair([self._custom(has_role_key=True)], KEY_STATUS, [], [])
        self.assertEqual([], plan.to_repair)
        self.assertEqual(["fixer"], [role for role, _ in plan.left_alone])

    def test_a_custom_role_with_no_key_anywhere_is_still_reported(self) -> None:
        found = dict(config_problems([self._custom(has_role_key=False)], KEYS))
        self.assertIn("credential", found["fixer"])

    def test_a_provider_key_in_the_table_counts_for_a_custom_slug(self) -> None:
        keys = {**KEYS, "my-proxy": {"needs_key": True, "has_key": True}}
        self.assertEqual([], config_problems([self._custom()], keys))

    def test_the_role_key_belongs_to_the_primary_and_not_to_its_fallback(self) -> None:
        # The fallback is a different provider and never inherits the role's credential
        # (`fallback_config_for` drops it for the same reason). Here the primary has no model at all, so the
        # role runs only if its fallback can: and the fallback is a custom provider with no key of its own.
        row = _config("fixer", "ollama", "", fallback_provider="my-proxy", fallback_model_name="m",
                      has_role_key=True)
        row["fallback_protocol"] = "openai_compat"
        found = dict(config_problems([row], KEYS))
        self.assertIn("fixer", found, "the role's key was lent to a different provider")


class PreflightInAGoalTests(_Harness):
    """The line itself, in a real goal's transcript."""

    RESPONSES: dict[str, Any] = _responses()

    def _break_roles(self) -> None:
        """Six roles on a keyless local model, two on providers with no key."""
        for role in ROLES:
            self.registry.set_config(
                role, AgentConfigUpdate(provider="ollama", model_name="qwen2.5-coder:7b")
            )
        self.registry.set_config(
            "librarian",
            AgentConfigUpdate(provider="anthropic", model_name="claude-sonnet-4-6"),
        )
        self.registry.set_config(
            "planner", AgentConfigUpdate(provider="openai", model_name="gpt-4.1-mini")
        )

    async def test_one_warning_names_every_broken_role_and_its_reason(self) -> None:
        self._break_roles()
        await self.executor.run_planning(self.goal.id)
        preflights = [w for w in self._warnings() if "cannot be called" in w]
        self.assertEqual(len(preflights), 1, f"expected one line, got {preflights}")
        line = preflights[0]
        self.assertIn("2 of 8", line)
        self.assertIn("librarian: anthropic needs a credential", line)
        self.assertIn("planner: openai needs a credential", line)
        # It points at the fix rather than only at the fact.
        self.assertIn("Settings", line)
        self.assertIn("Repair", line)

    async def test_the_working_roles_are_not_named(self) -> None:
        self._break_roles()
        await self.executor.run_planning(self.goal.id)
        line = next(w for w in self._warnings() if "cannot be called" in w)
        for role in ("fixer", "critic", "scribe", "verifier", "design", "laya"):
            self.assertNotIn(f"{role}:", line)

    def _custom_roles(self, with_key: bool) -> None:
        for role in ROLES:
            self.registry.set_config(role, AgentConfigUpdate(
                provider="my-proxy", protocol="openai_compat", model_name="m",
                base_url="http://127.0.0.1:9/v1", api_key="a-key" if with_key else None,
            ))

    async def test_a_custom_provider_with_its_own_key_says_nothing(self) -> None:
        self._custom_roles(with_key=True)
        await self.executor.run_planning(self.goal.id)
        self.assertEqual([w for w in self._warnings() if "cannot be called" in w], [])

    async def test_a_custom_provider_with_no_key_is_still_named(self) -> None:
        self._custom_roles(with_key=False)
        await self.executor.run_planning(self.goal.id)
        line = next((w for w in self._warnings() if "cannot be called" in w), "")
        self.assertIn("my-proxy needs a credential", line)

    async def test_a_fully_configured_workspace_says_nothing(self) -> None:
        """Silence is the point. A preflight that always speaks is noise."""
        for role in ROLES:
            self.registry.set_config(
                role, AgentConfigUpdate(provider="ollama", model_name="qwen2.5-coder:7b")
            )
        await self.executor.run_planning(self.goal.id)
        self.assertEqual([w for w in self._warnings() if "cannot be called" in w], [])

    async def test_the_goal_still_runs_after_the_warning(self) -> None:
        """A warning, not a block: the roles that work should still do their work."""
        self._break_roles()
        await self.executor.run_planning(self.goal.id)
        self.assertTrue(self._warnings())
        self.assertEqual(len(self.goals.steps(self.goal.id)), 1)

    async def test_the_line_is_the_first_thing_in_the_transcript(self) -> None:
        """Before the first model call, not after the first failure."""
        self._break_roles()
        await self.executor.run_planning(self.goal.id)
        events = self.goals.events_after(self.goal.id, 0)
        first = next(i for i, e in enumerate(events)
                     if e.type == "log" and "cannot be called" in str(e.payload))
        first_call = next(i for i, e in enumerate(events) if e.type == "agent_assigned")
        self.assertLess(first, first_call)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
