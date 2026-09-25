"""Role repair: fix what cannot run, and leave everything else exactly alone.

The restraint is the feature. An "apply to every role" button already existed and
overwrites choices the user made; this rule has to be narrow enough that pressing it
on a working install changes nothing at all.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import unittest
from typing import Any

from engine.models import ROLES
from engine.role_repair import choose_target, plan_role_repair


def config(role: str, provider: str, model: str, protocol: str = "ollama", **rest: Any) -> dict[str, Any]:
    return {"role": role, "provider": provider, "model_name": model, "protocol": protocol, **rest}


def key(provider: str, has_key: bool, needs_key: bool) -> dict[str, Any]:
    return {"provider": provider, "has_key": has_key, "needs_key": needs_key}


def status(provider: str, ok: bool, error: str | None = None, count: int = 1) -> dict[str, Any]:
    return {"provider": provider, "ok": ok, "count": count, "error": error}


OLLAMA_KEY = key("ollama", True, False)
OPENAI_KEY = key("openai", False, True)
OPENAI_KEY_PRESENT = key("openai", True, True)
KEYS = [OLLAMA_KEY, OPENAI_KEY]


class TestWhoNeedsRepair(unittest.TestCase):
    def test_a_role_with_no_model_needs_repair(self) -> None:
        plan = plan_role_repair(
            configs=[config("planner", "ollama", "")],
            key_status=KEYS,
            provider_status=[status("ollama", True)],
            catalog=[{"provider": "ollama", "id": "local-1", "supports_chat": None}],
        )
        self.assertEqual(plan.to_repair, [("planner", "no model is chosen")])
        self.assertEqual(plan.left_alone, [])
        self.assertTrue(plan.changed)

    def test_a_role_on_a_keyless_provider_with_a_model_is_left_alone(self) -> None:
        plan = plan_role_repair(
            configs=[config("fixer", "ollama", "local-1")],
            key_status=KEYS,
            provider_status=[status("ollama", True)],
            catalog=[{"provider": "ollama", "id": "local-1", "supports_chat": None}],
        )
        self.assertEqual(plan.to_repair, [])
        self.assertFalse(plan.changed)
        self.assertIn("verified in the catalog", plan.left_alone[0][1])

    def test_a_role_on_a_provider_without_a_key_needs_repair(self) -> None:
        plan = plan_role_repair(
            configs=[config("tester", "openai", "gpt-4.1-mini", protocol="openai_compat")],
            key_status=KEYS,
            provider_status=[status("openai", False, error="no API key configured for this provider")],
            catalog=[{"provider": "ollama", "id": "local-1", "supports_chat": None}],
        )
        self.assertEqual(plan.to_repair, [("tester", "openai needs a credential and none is stored")])

    def test_a_role_whose_model_was_retired_needs_repair(self) -> None:
        plan = plan_role_repair(
            configs=[config("fixer", "ollama", "ghost:latest")],
            key_status=KEYS,
            provider_status=[status("ollama", True, count=2)],
            catalog=[
                {"provider": "ollama", "id": "local-1", "supports_chat": None},
                {"provider": "ollama", "id": "local-2", "supports_chat": None},
            ],
        )
        self.assertEqual(plan.to_repair, [("fixer", 'ollama no longer reports "ghost:latest"')])

    def test_a_failed_discovery_never_makes_a_role_repairable(self) -> None:
        """An unanswered provider is an unknown, not a fault.

        Repointing a role because discovery timed out would be the same false claim
        as calling a live model retired.
        """
        plan = plan_role_repair(
            configs=[config("fixer", "openai", "gpt-4.1-mini", protocol="openai_compat")],
            key_status=[OPENAI_KEY_PRESENT, OLLAMA_KEY],
            provider_status=[status("openai", False, error="HTTP 500")],
            catalog=[{"provider": "ollama", "id": "local-1", "supports_chat": None}],
        )
        self.assertEqual(plan.to_repair, [])
        reason = plan.left_alone[0][1]
        self.assertIn("not verified", reason)
        # The reason must not claim the role works: the provider never answered.
        self.assertNotIn("usable", reason)
        self.assertTrue(any("did not answer discovery" in n for n in plan.notes))

    def test_an_unreachable_provider_is_reported_once_not_once_per_role(self) -> None:
        """Eight roles on one dead provider is one caveat, not eight."""
        plan = plan_role_repair(
            configs=[config(role, "ollama", "local-1") for role in ROLES],
            key_status=[OLLAMA_KEY],
            provider_status=[status("ollama", False, error="ConnectError")],
            catalog=[],
        )
        self.assertEqual(plan.to_repair, [])
        self.assertEqual(len(plan.left_alone), len(ROLES))
        self.assertEqual(len(plan.notes), 1, plan.notes)
        self.assertIn("did not answer discovery", plan.notes[0])

    def test_a_model_the_provider_still_reports_is_never_touched(self) -> None:
        plan = plan_role_repair(
            configs=[
                config("fixer", "ollama", "local-2"),
                config("tester", "openai", "gpt-4.1", protocol="openai_compat"),
            ],
            key_status=[OLLAMA_KEY, OPENAI_KEY_PRESENT],
            provider_status=[status("ollama", True), status("openai", True)],
            catalog=[
                {"provider": "ollama", "id": "local-2", "supports_chat": None},
                {"provider": "openai", "id": "gpt-4.1", "supports_chat": True},
            ],
        )
        self.assertEqual(plan.to_repair, [])
        self.assertEqual(len(plan.left_alone), 2)


class TestWhatItPicks(unittest.TestCase):
    def test_it_prefers_the_model_the_working_roles_already_use(self) -> None:
        plan = plan_role_repair(
            configs=[
                config("fixer", "ollama", "shared-model"),
                config("critic", "ollama", "shared-model"),
                config("planner", "ollama", ""),
            ],
            key_status=KEYS,
            provider_status=[status("ollama", True, count=2)],
            catalog=[
                {"provider": "ollama", "id": "brand-new", "supports_chat": None},
                {"provider": "ollama", "id": "shared-model", "supports_chat": None},
            ],
        )
        self.assertEqual(plan.target, {"provider": "ollama", "model": "shared-model"})
        self.assertIn("2 of 2 working roles already use", plan.target_reason)

    def test_it_prefers_a_provider_that_needs_no_credential(self) -> None:
        keyless, reason = choose_target(
            configs=[config("planner", "openai", "", protocol="openai_compat")],
            working_roles=set(),
            catalog=[
                {"provider": "openai", "id": "gpt-x", "supports_chat": True},
                {"provider": "ollama", "id": "local-1", "supports_chat": None},
            ],
            keys={"openai": OPENAI_KEY_PRESENT, "ollama": OLLAMA_KEY},
        )
        self.assertEqual(keyless, {"provider": "ollama", "model": "local-1"})
        self.assertIn("needs no credential", reason)

    def test_it_skips_models_the_provider_reports_as_non_chat(self) -> None:
        plan = plan_role_repair(
            configs=[config("planner", "ollama", "")],
            key_status=KEYS,
            provider_status=[status("ollama", True, count=2)],
            catalog=[
                {"provider": "ollama", "id": "nomic-embed-text", "supports_chat": False},
                {"provider": "ollama", "id": "local-1", "supports_chat": None},
            ],
        )
        self.assertEqual(plan.target, {"provider": "ollama", "model": "local-1"})

    def test_an_empty_catalog_changes_nothing_and_says_why(self) -> None:
        plan = plan_role_repair(
            configs=[config("planner", "ollama", "")],
            key_status=KEYS,
            provider_status=[],
            catalog=[],
        )
        self.assertIsNone(plan.target)
        self.assertFalse(plan.changed)
        # The reason names what is missing and what to do about it, so the screen has
        # something actionable to show instead of a silent no-op.
        self.assertIn("nothing to point these roles at", plan.target_reason)
        self.assertIn("add a provider key", plan.target_reason)
        # Nothing was performed, so there is nothing extra to caveat.
        self.assertEqual(plan.notes, [])
        self.assertEqual(plan.to_repair, [("planner", "no model is chosen")])


class TestFallbackInterplay(unittest.TestCase):
    """A role with a working fallback is a role that runs.

    Repair decides what "cannot run" means, and a fallback changes that answer. If
    the two disagreed, pressing "fix the roles that can't run" would repoint the
    primary of a role that was already keeping the goal alive — overwriting the
    very choice holding it up.
    """

    def test_a_role_rescued_by_its_fallback_is_left_alone(self) -> None:
        plan = plan_role_repair(
            configs=[
                config(
                    "planner", "anthropic", "claude-opus-5",
                    protocol="anthropic",
                    fallback_provider="ollama", fallback_model_name="local-1",
                )
            ],
            key_status=KEYS,
            provider_status=[status("ollama", True), status("anthropic", False, error="no key")],
            catalog=[{"provider": "ollama", "id": "local-1", "supports_chat": None}],
        )
        self.assertEqual(plan.to_repair, [])
        reason = plan.left_alone[0][1]
        self.assertIn("runs on its fallback", reason)
        self.assertIn("ollama/local-1", reason)
        # The reason still says why the primary is not the one being used.
        self.assertIn("needs a credential", reason)

    def test_a_fallback_the_provider_no_longer_serves_does_not_rescue_the_role(self) -> None:
        plan = plan_role_repair(
            configs=[
                config(
                    "planner", "anthropic", "claude-opus-5",
                    protocol="anthropic",
                    fallback_provider="ollama", fallback_model_name="ghost:latest",
                )
            ],
            key_status=KEYS,
            provider_status=[status("ollama", True, count=1)],
            catalog=[{"provider": "ollama", "id": "local-1", "supports_chat": None}],
        )
        self.assertEqual(len(plan.to_repair), 1)
        role, reason = plan.to_repair[0]
        self.assertEqual(role, "planner")
        self.assertIn("needs a credential", reason)
        self.assertIn("its fallback ollama/ghost:latest is unusable too", reason)
        self.assertIn('ollama no longer reports "ghost:latest"', reason)

    def test_an_unverifiable_fallback_is_not_a_proven_fault(self) -> None:
        """The fallback provider never answered, so nothing is proven about it.

        Leaving the role alone is the same restraint the primary gets: the role may
        well run, and repointing it would trade a possible fix for a certain
        overwrite.
        """
        plan = plan_role_repair(
            configs=[
                config(
                    "planner", "anthropic", "claude-opus-5",
                    protocol="anthropic",
                    fallback_provider="ollama", fallback_model_name="local-1",
                )
            ],
            key_status=KEYS,
            provider_status=[status("ollama", False, error="ConnectError")],
            catalog=[],
        )
        self.assertEqual(plan.to_repair, [])
        reason = plan.left_alone[0][1]
        self.assertIn("may run on its fallback", reason)
        self.assertNotIn("usable:", reason)
        self.assertEqual(len(plan.notes), 1)

    def test_a_rescued_roles_fallback_model_counts_as_the_one_in_use(self) -> None:
        """Repair should meet the install where it is.

        The rescued role is running the fallback model, so pointing the broken
        roles at the same one introduces nothing new — while the primary model it
        is *not* using would be a second model the install never planned for.
        """
        plan = plan_role_repair(
            configs=[
                config(
                    "planner", "anthropic", "claude-opus-5",
                    protocol="anthropic",
                    fallback_provider="ollama", fallback_model_name="local-2",
                ),
                config("fixer", "ollama", ""),
            ],
            key_status=KEYS,
            provider_status=[status("ollama", True, count=2), status("anthropic", False, error="no key")],
            catalog=[
                {"provider": "ollama", "id": "local-1", "supports_chat": None},
                {"provider": "ollama", "id": "local-2", "supports_chat": None},
            ],
        )
        self.assertEqual(plan.target, {"provider": "ollama", "model": "local-2"})
        self.assertIn("1 of 1", plan.target_reason)


if __name__ == "__main__":
    unittest.main()
