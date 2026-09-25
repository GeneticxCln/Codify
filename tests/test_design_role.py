from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import asyncio
import json
import subprocess
import tempfile
import time
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport

from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.git import GitService
from engine.executor import (
    MAX_BRAND_DRIFTS,
    MAX_CONTRACT_FILE_CHARS,
    MAX_DESIGN_COMPONENTS,
    MAX_DESIGN_MD_CHARS,
    MAX_DRIFT_DIFF_CHARS,
    AgentOutputInvalid,
    ExecutorService,
)
from engine.laya import LayaDecision, LayaService
from engine.models import (
    ROLES,
    AgentConfig,
    AgentConfigUpdate,
    GoalCreate,
    WorkspaceCreate,
)
from engine.default_prompts import DESIGN_BRIEF_PROMPT
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, ApiError, GoalService, WorkspaceService


class MockProvider(BaseProvider):
    """Answers each role from a map, and records what each call was handed."""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        # Which role's config the factory last built a provider for. Routing uses
        # this rather than a scan of the system prompt: the prompts name each
        # other on purpose, so a keyword scan hands back another role's script.
        self.current_role: str | None = None

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        self.calls.append({
            "role": self.current_role,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        })
        return json.dumps(self.responses.get(self.current_role or "", {}))

    def prompt_for(self, role: str) -> str:
        """The user prompt the named role was last handed."""
        handed = [c["user_prompt"] for c in self.calls if c["role"] == role]
        return handed[-1] if handed else ""


class MockFactory(ProviderFactory):
    def __init__(self, provider: MockProvider, keychain: Keychain) -> None:
        super().__init__(keychain)
        self.provider = provider

    def build(self, config: AgentConfig) -> MockProvider:
        self.provider.current_role = config.role
        return self.provider


class SkippedGate(LayaService):
    """The gate is its own concern (docs/05); these tests are about the design
    contract."""

    async def decide(self, state: dict[str, Any]) -> LayaDecision:
        return LayaDecision(engine="skipped", skipped_reason="test double")


CONTRACT: dict[str, Any] = {
    "applies": True,
    "artifact": "dashboard",
    "direction": "Editorial KPI wall: dense, monochrome, one accent.",
    "design_system": {"name": "codify-brand", "source": None},
    "tokens": {
        "colors": [
            {"name": "ink", "value": "#0d1117"},
            {"name": "accent", "value": "#2f81f7"},
        ],
        "typography": [{"name": "display", "value": "Inter, system-ui, sans-serif"}],
        "spacing": ["4px", "8px", "16px"],
        "radii": ["6px"],
    },
    "components": [{"name": "KpiTile", "purpose": "one metric with its trend"}],
    "conventions": ["Tailwind utility classes", "components under ui/src/components"],
    "constraints": ["reuse the existing token names", "no new dependencies"],
    "acceptance": ["every KPI renders in its own tile", "no placeholder values"],
    "design_md": "# codify-brand\n\nink #0d1117, accent #2f81f7.\n",
}

# A brand contract a workspace might really have: prose the model must derive
# tokens from rather than invent its own.
BRAND = "# acme-brand\n\nink #101418, accent #6f42c1. Inter for all text. 8px grid.\n"


def _responses() -> dict[str, Any]:
    """A full cast: every role the pipeline calls, with a sane reply."""
    return {
        "librarian": {
            "summary": "a React dashboard repo",
            "files": [],
            "conventions": ["Tailwind utility classes"],
            "enough": True,
        },
        "design": dict(CONTRACT),
        "planner": {
            "steps": [{"title": "S1", "description": "build the board", "suggested_paths": []}]
        },
        "fixer": {
            "files": [
                {"path": "ui/src/board.html", "action": "create", "content": "<main></main>\n"}
            ]
        },
        "verifier": {"argv": None, "verdict": "pass", "explanation": "nothing to run"},
        "critic": {"decision": "approve", "reasons": []},
        "scribe": {"summary": "did it", "commit_message": "feat: board"},
    }


class _Harness(unittest.IsolatedAsyncioTestCase):
    """The shared rig: one workspace, one scripted provider, every role configured.

    `BRAND` is written to `BRAND_PATH` before the engine starts when it is set,
    so a subclass only has to say which file a workspace claims as its brand.
    """

    RESPONSES: dict[str, Any] = {}
    BRAND: str | None = None
    BRAND_PATH = "brand/DESIGN.md"
    # Goal fields a subclass needs at creation — a design-mode goal, say. The
    # default goal is the default pipeline, so nothing here changes it.
    GOAL_KWARGS: dict[str, Any] = {}

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        if self.BRAND is not None:
            target = self.root / self.BRAND_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.BRAND, encoding="utf-8")
        self.conn = connect(self.root / "t.db")
        self.provider = MockProvider(dict(self.RESPONSES))
        self.registry = AgentRegistryService(
            self.conn, MockFactory(self.provider, Keychain()), Keychain()
        )
        for role in ROLES:
            self.registry.set_config(role, AgentConfigUpdate(provider="ollama", model_name="m"))
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=SkippedGate()
        )
        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(
            GoalCreate(
                workspace_id=self.ws.id,
                title="Build a KPI dashboard",
                description="",
                **self.GOAL_KWARGS,
            )
        )

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _events(self, type_: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.goals.events_after(self.goal.id, 0) if e.type == type_]

    def _warnings(self) -> list[str]:
        return [e["message"] for e in self._events("log") if e["level"] == "warn"]

    async def _run_the_step(self) -> None:
        await self.executor.run_planning(self.goal.id)
        step = self.goals.steps(self.goal.id)[0]
        self.goals.update_status(self.goal.id, self.goals.get(self.goal.id).version, "RUNNING")
        await self.executor.run_step(self.goal.id, step.id)


class DesignRoleCase(_Harness):
    RESPONSES = _responses()

    # ── the direction has to survive the handoffs ────────────────────────────

    async def test_the_locked_direction_reaches_the_planner(self) -> None:
        """A direction that never reaches the plan is decoration.

        The point of locking it is that the planner plans against values rather
        than inventing a palette per step, so the values — not just the name of
        the design system — must be in the prompt.
        """
        await self.executor.run_planning(self.goal.id)
        planner_prompt = self.provider.prompt_for("planner")
        self.assertIn(CONTRACT["direction"], planner_prompt)
        self.assertIn("#2f81f7", planner_prompt, "token values must reach the planner")
        self.assertIn(CONTRACT["design_md"], planner_prompt)

    async def test_the_direction_reaches_the_writer_and_the_judge(self) -> None:
        """The fixer obeys the contract, and the critic judges against it."""
        await self._run_the_step()
        fixer_prompt = self.provider.prompt_for("fixer")
        self.assertIn(CONTRACT["direction"], fixer_prompt)
        self.assertIn("KpiTile", fixer_prompt)
        critic_prompt = self.provider.prompt_for("critic")
        self.assertIn(CONTRACT["acceptance"][0], critic_prompt)

    async def test_design_runs_after_the_librarian_and_before_the_planner(self) -> None:
        """It cannot lock a direction from a blank page, and the planner cannot
        plan against a contract that has not been decided yet."""
        await self.executor.run_planning(self.goal.id)
        order = [
            c["role"]
            for c in self.provider.calls
            if c["role"] in ("librarian", "design", "planner")
        ]
        self.assertEqual(order, ["librarian", "design", "planner"])

    async def test_the_contract_is_published_for_the_transcript(self) -> None:
        await self.executor.run_planning(self.goal.id)
        contracts = self._events("design_contract")
        self.assertEqual(len(contracts), 1)
        self.assertEqual(contracts[0]["artifact"], "dashboard")
        self.assertEqual([c["name"] for c in contracts[0]["components"]], ["KpiTile"])
        self.assertIsNone(contracts[0]["design_system"]["source"])
        self.assertIsNone(contracts[0]["design_system"]["origin"])

    async def test_the_design_agent_writes_nothing(self) -> None:
        """It asks for a DESIGN.md; the fixer is still the only writer.

        The role has no file tools at all, so a direction that wants a file
        written can only become a step — which is what keeps one writer in the
        pipeline.
        """
        await self.executor.run_planning(self.goal.id)
        self.assertFalse(
            (self.root / "DESIGN.md").exists(),
            "planning must not write the files a contract asks for",
        )
        self.assertIn("DESIGN.md", self.provider.prompt_for("planner"))

    # ── a design stage that cannot run must not kill the goal ────────────────

    async def test_a_reply_that_is_not_a_contract_does_not_fail_the_goal(self) -> None:
        """Same rule the librarian gets: the stage is an aid, not a gate."""
        self.provider.responses["design"] = ["not", "a", "contract"]
        await self.executor.run_planning(self.goal.id)

        self.assertEqual(self._events("error"), [])
        self.assertEqual([s.title for s in self.goals.steps(self.goal.id)], ["S1"])
        self.assertEqual(self._events("design_contract"), [])
        self.assertTrue(
            any("design unavailable" in m for m in self._warnings()),
            f"the skip must be stated, not silent: {self._warnings()}",
        )

    async def test_a_goal_with_no_rendered_surface_skips_the_contract(self) -> None:
        """`applies: false` is an answer, not a failure: the planner is told
        there is no direction rather than being handed an invented one."""
        self.provider.responses["design"] = {"applies": False}
        await self.executor.run_planning(self.goal.id)

        self.assertEqual(self._events("error"), [])
        self.assertEqual(self._events("design_contract"), [])
        self.assertIn("(none", self.provider.prompt_for("planner"))
        self.assertIn("invent no visual direction", self.provider.prompt_for("planner"))

    async def test_a_step_reads_the_contract_back_from_the_event_log(self) -> None:
        """Not from memory: a step driven in another process must be handed the
        same direction its planner planned from."""
        await self.executor.run_planning(self.goal.id)
        self.assertEqual(self.executor._design_for(self.goal.id)["artifact"], "dashboard")
        # A goal planned before this role existed has no contract, and nothing
        # may fail on the empty lookup.
        other = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="unrelated", description="")
        )
        self.assertEqual(self.executor._design_for(other.id), {})
        self.assertIn("invent no visual direction", self.executor._design_text({}))


class PinnedBrandContractCase(_Harness):
    """A workspace can pin its own brand contract, and the engine makes it binding."""

    RESPONSES = _responses()
    BRAND = BRAND

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.workspaces.set_design_contract(self.ws.id, self.BRAND_PATH)

    async def test_the_pinned_contract_is_handed_over_and_marked_binding(self) -> None:
        await self.executor.run_planning(self.goal.id)
        prompt = self.provider.prompt_for("design")
        self.assertIn("acme-brand", prompt, "the file's own words must reach the agent")
        self.assertIn("8px grid", prompt)
        self.assertIn("BINDING", prompt)
        self.assertIn(self.BRAND_PATH, prompt)

    async def test_the_source_is_the_path_the_engine_resolved(self) -> None:
        """The model does not get to relabel where the brand came from."""
        self.provider.responses["design"] = dict(
            CONTRACT, design_system={"name": "acme-brand", "source": "somewhere/else.md"}
        )
        await self.executor.run_planning(self.goal.id)

        system = self._events("design_contract")[0]["design_system"]
        self.assertEqual(system["source"], self.BRAND_PATH)
        self.assertEqual(system["origin"], "pinned")
        self.assertEqual(system["name"], "acme-brand")

    async def test_a_pinned_contract_cannot_be_overwritten_by_a_design_md_body(self) -> None:
        """Two contracts, one about to be written over the other, is the drift the
        pin exists to stop — so the body is dropped before the planner sees it."""
        await self.executor.run_planning(self.goal.id)
        contract = self._events("design_contract")[0]
        self.assertIsNone(contract["design_md"])
        self.assertNotIn(
            "DESIGN.md body",
            self.provider.prompt_for("planner"),
            "the planner must not be handed a file to write over the pinned contract",
        )

    async def test_the_binding_label_reaches_the_writer_and_the_judge(self) -> None:
        await self._run_the_step()
        self.assertIn("pinned at", self.provider.prompt_for("fixer"))
        self.assertIn("pinned at", self.provider.prompt_for("critic"))

    async def test_a_pinned_file_that_vanished_is_warned_about_not_silently_ignored(self) -> None:
        (self.root / self.BRAND_PATH).unlink()
        await self.executor.run_planning(self.goal.id)

        self.assertTrue(
            any("pinned brand contract" in m for m in self._warnings()),
            f"a pin that is not in force must say so: {self._warnings()}",
        )
        # It falls back to proposing a brand, and says the origin is not the pin —
        # so nobody reads the result as "the pinned brand was used".
        system = self._events("design_contract")[0]["design_system"]
        self.assertIsNone(system["origin"])
        self.assertIsNone(system["source"])
        self.assertEqual([s.title for s in self.goals.steps(self.goal.id)], ["S1"])

    async def test_an_oversized_contract_is_truncated_and_says_so(self) -> None:
        target = self.root / "brand" / "BIG.md"
        target.write_text("x" * (MAX_CONTRACT_FILE_CHARS + 250), encoding="utf-8")
        self.workspaces.set_design_contract(self.ws.id, "brand/BIG.md")
        await self.executor.run_planning(self.goal.id)

        self.assertTrue(
            any("using the first" in m for m in self._warnings()), self._warnings()
        )
        prompt = self.provider.prompt_for("design")
        self.assertIn("x" * 500, prompt)
        self.assertNotIn("x" * (MAX_CONTRACT_FILE_CHARS + 1), prompt)


class DiscoveredBrandContractCase(_Harness):
    """A `DESIGN.md` at the workspace root is binding without being pinned —
    zero-config is the point, and the pin is for everything else."""

    RESPONSES = _responses()
    BRAND = BRAND
    BRAND_PATH = "DESIGN.md"

    async def test_a_design_md_at_the_root_is_binding_without_a_pin(self) -> None:
        await self.executor.run_planning(self.goal.id)
        system = self._events("design_contract")[0]["design_system"]
        self.assertEqual(system["source"], "DESIGN.md")
        self.assertEqual(system["origin"], "discovered")
        self.assertIn("acme-brand", self.provider.prompt_for("design"))
        self.assertIsNone(self._events("design_contract")[0]["design_md"])

    async def test_a_pin_beats_what_convention_would_find(self) -> None:
        (self.root / "brand").mkdir()
        (self.root / "brand" / "PINNED.md").write_text(
            "# pinned-brand\n\npin marker 4242\n", encoding="utf-8"
        )
        self.workspaces.set_design_contract(self.ws.id, "brand/PINNED.md")
        await self.executor.run_planning(self.goal.id)

        prompt = self.provider.prompt_for("design")
        self.assertIn("pin marker 4242", prompt)
        self.assertNotIn("acme-brand", prompt, "the root DESIGN.md must not be consulted")
        system = self._events("design_contract")[0]["design_system"]
        self.assertEqual(system["source"], "brand/PINNED.md")
        self.assertEqual(system["origin"], "pinned")

    async def test_a_whitespace_only_root_file_is_not_a_brand_contract(self) -> None:
        """An empty file is not a brand. Treating it as one would bind every goal
        to nothing and silently drop the body the fixer was supposed to write."""
        (self.root / "DESIGN.md").write_text("\n\n", encoding="utf-8")
        await self.executor.run_planning(self.goal.id)

        contract = self._events("design_contract")[0]
        self.assertIsNone(contract["design_system"]["origin"])
        self.assertEqual(
            contract["design_md"],
            CONTRACT["design_md"],
            "nothing was found, so a proposed body is still allowed",
        )


class PinningValidationCase(_Harness):
    """Pinning is a settings action with a user watching, so it is checked here
    rather than discovered mid-goal."""

    RESPONSES = _responses()
    BRAND = BRAND
    BRAND_PATH = "DESIGN.md"

    async def test_an_escaping_path_is_refused(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.workspaces.set_design_contract(self.ws.id, "../outside.md")
        self.assertEqual(ctx.exception.code, "design_contract_escape")
        self.assertEqual(self.workspaces.get(self.ws.id).design_contract_path, "")

    async def test_an_absolute_path_is_refused(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.workspaces.set_design_contract(self.ws.id, "/etc/hosts")
        self.assertEqual(ctx.exception.code, "design_contract_escape")

    async def test_a_missing_file_is_refused(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.workspaces.set_design_contract(self.ws.id, "nope/DESIGN.md")
        self.assertEqual(ctx.exception.code, "design_contract_missing")

    async def test_a_directory_is_refused(self) -> None:
        (self.root / "brand").mkdir()
        with self.assertRaises(ApiError) as ctx:
            self.workspaces.set_design_contract(self.ws.id, "brand")
        self.assertEqual(ctx.exception.code, "design_contract_missing")

    async def test_a_binary_file_is_refused(self) -> None:
        (self.root / "logo.png").write_bytes(b"\x00\x01\x02not-text")
        with self.assertRaises(ApiError) as ctx:
            self.workspaces.set_design_contract(self.ws.id, "logo.png")
        self.assertEqual(ctx.exception.code, "design_contract_binary")

    async def test_an_unknown_workspace_is_a_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.workspaces.set_design_contract("no-such-workspace", "DESIGN.md")
        self.assertEqual(ctx.exception.status, 404)

    async def test_clearing_with_an_empty_path_unpins(self) -> None:
        self.workspaces.set_design_contract(self.ws.id, self.BRAND_PATH)
        self.assertEqual(
            self.workspaces.get(self.ws.id).design_contract_path, self.BRAND_PATH
        )
        cleared = self.workspaces.set_design_contract(self.ws.id, "   ")
        self.assertEqual(cleared.design_contract_path, "", "whitespace is not a path")

    async def test_a_refused_pin_leaves_the_previous_one_in_force(self) -> None:
        self.workspaces.set_design_contract(self.ws.id, self.BRAND_PATH)
        with self.assertRaises(ApiError):
            self.workspaces.set_design_contract(self.ws.id, "gone.md")
        self.assertEqual(
            self.workspaces.get(self.ws.id).design_contract_path,
            self.BRAND_PATH,
            "a rejected pin must not clear the one that worked",
        )


class TestContractNormalization(unittest.IsolatedAsyncioTestCase):
    """The contract is prompt material, so malformed rows are trimmed instead
    of failing the stage — but the engine's vocabulary is not a place to pass a
    model's invention through."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.provider = MockProvider({})
        self.registry = AgentRegistryService(
            self.conn, MockFactory(self.provider, Keychain()), Keychain()
        )
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=SkippedGate()
        )

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_bounds_and_vocabulary(self) -> None:
        contract = self.executor._design_contract({
            "applies": True,
            "artifact": "hologram",
            "direction": "  dense  ",
            "components": [
                {"name": f"c{i}"} for i in range(MAX_DESIGN_COMPONENTS + 10)
            ],
            "tokens": {"colors": [{"value": "#fff"}, {"name": "ink", "value": "#000"}]},
            "design_md": "x" * (MAX_DESIGN_MD_CHARS + 500),
        })
        self.assertEqual(contract["artifact"], "other")
        self.assertEqual(contract["direction"], "dense")
        self.assertEqual(len(contract["components"]), MAX_DESIGN_COMPONENTS)
        self.assertEqual(
            [c["name"] for c in contract["tokens"]["colors"]],
            ["ink"],
            "an unnamed token is a value nothing can reference — it must not pass on",
        )
        self.assertEqual(len(contract["design_md"]), MAX_DESIGN_MD_CHARS)

    async def test_an_unlocked_direction_is_no_contract_rather_than_an_error(self) -> None:
        self.assertEqual(self.executor._design_contract({"applies": True, "direction": "  "}), {})
        self.assertEqual(self.executor._design_contract({"applies": False, "direction": "x"}), {})
        self.assertEqual(self.executor._design_contract({}), {})

    async def test_a_non_object_reply_is_a_contract_error(self) -> None:
        with self.assertRaises(AgentOutputInvalid):
            self.executor._design_contract(["not", "a", "contract"])

    async def test_the_empty_contract_says_so_instead_of_nothing(self) -> None:
        text = self.executor._design_text({})
        self.assertIn("invent no visual direction", text)

    async def test_a_brand_contract_is_stamped_by_the_engine(self) -> None:
        brand = {"path": "brand/DESIGN.md", "origin": "pinned", "text": BRAND}
        contract = self.executor._design_contract(dict(CONTRACT), brand)
        self.assertEqual(contract["design_system"]["source"], "brand/DESIGN.md")
        self.assertEqual(contract["design_system"]["origin"], "pinned")
        self.assertIsNone(contract["design_md"], "the pinned file is the only contract")

    async def test_a_brand_file_backs_the_name_the_model_did_not_give(self) -> None:
        """A pinned file must render as *something* even when the reply names no
        brand: the filename is a fact, where a made-up label would not be."""
        brand = {"path": "DESIGN.md", "origin": "discovered", "text": BRAND}
        contract = self.executor._design_contract(
            {"applies": True, "direction": "d", "design_system": {"name": ""}}, brand
        )
        self.assertEqual(contract["design_system"]["name"], "DESIGN")
        self.assertIn("found at DESIGN.md — binding", self.executor._design_text(contract))


class TestVerifierBrandCheck(unittest.IsolatedAsyncioTestCase):
    """The verifier's mechanical check of written artifacts against a binding
    brand contract: what text comparison can prove, the engine proves, so a
    drift is caught by evidence rather than by whether the critic notices.

    Unit-level on `_brand_drifts` — the input is the same `diffs` list run_step
    hands the verifier (fs.apply summaries), and `design` is the same contract
    `_design_for` reads back from the event log, so these tests exercise the
    exact wiring without a scripted pipeline behind every case.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.provider = MockProvider({})
        self.registry = AgentRegistryService(
            self.conn, MockFactory(self.provider, Keychain()), Keychain()
        )
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=SkippedGate()
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    # -- fixtures ------------------------------------------------------------

    @staticmethod
    def _contract(origin: str | None = "pinned", source: str | None = "brand/DESIGN.md") -> dict[str, Any]:
        tokens: dict[str, Any] = {
            "colors": [
                {"name": "ink", "value": "#101418"},
                {"name": "accent", "value": "#6f42c1"},
            ],
            "typography": [{"name": "body", "value": "Inter, system-ui, sans-serif"}],
            "spacing": ["8px"],
            "radii": ["6px"],
        }
        system: dict[str, Any] = {} if origin is None else {"name": "acme", "source": source, "origin": origin}
        return {
            "applies": True,
            "artifact": "web_prototype",
            "direction": "monochrome, one accent",
            "design_system": system,
            "tokens": tokens,
            "components": [],
            "conventions": [],
            "constraints": ["no new dependencies"],
            "acceptance": ["dashboard header uses the accent"],
            "design_md": None,
        }

    @staticmethod
    def _diff(path: str, content: str) -> dict[str, Any]:
        return {"path": path, "action": "update", "unified_diff": "", "resolved_content": content}

    def _drifts(
        self, diffs: list[dict[str, Any]], design: dict[str, Any]
    ) -> list[str]:
        return self.executor._brand_drifts(diffs, design, str(self.root))

    # -- when the check runs at all -----------------------------------------

    def test_a_proposed_brand_is_advice_not_enforced_law(self) -> None:
        """origin null — no contract file governs, so there is nothing to
        enforce mechanically: enforcement is for what a workspace signed."""
        design = self._contract(origin=None, source=None)
        self.assertEqual(self._drifts([], design), [])

    def test_pinned_and_discovered_govern_alike(self) -> None:
        """The user's pin and the convention the engine found carry the same
        weight — both are the workspace's own file, and changes unrelated to
        the contract draw findings under either."""
        diffs = [self._diff("index.html", "<h1>something unrelated</h1>")]
        for origin in ("pinned", "discovered"):
            with self.subTest(origin=origin):
                self.assertNotEqual(self._drifts(diffs, self._contract(origin=origin)), [])

    def test_no_diffs_means_no_findings(self) -> None:
        """The check reads what this step wrote, so a step that wrote nothing
        (a no-op replay) reports nothing rather than declaring the whole
        contract unmet."""
        self.assertEqual(self._drifts([], self._contract()), [])

    def test_the_corpus_is_capped_per_file(self) -> None:
        """Each file's evidence is bounded (MAX_DRIFT_DIFF_CHARS): a token
        inside the cap is seen, the same token past it is not — the checker's
        memory of one huge file cannot grow without bound."""
        design = self._only_colors({"name": "accent", "value": "#6f42c1"})
        inside = "x" * (MAX_DRIFT_DIFF_CHARS - 20) + " accent #6f42c1"
        self.assertEqual(self._drifts([self._diff("inside.html", inside)], design), [])
        outside = "x" * MAX_DRIFT_DIFF_CHARS + " accent #6f42c1"
        self.assertNotEqual(self._drifts([self._diff("outside.html", outside)], design), [])

    # -- what counts as drift -----------------------------------------------

    def _only_colors(self, *colors: dict[str, str]) -> dict[str, Any]:
        """A contract whose only checkable claims are the given colors."""
        design = self._contract()
        design["tokens"] = {"colors": list(colors), "typography": [], "spacing": [], "radii": []}
        design["acceptance"] = []
        design["constraints"] = []
        return design

    def test_an_unused_token_color_is_drift(self) -> None:
        design = self._only_colors({"name": "ink", "value": "#101418"}, {"name": "accent", "value": "#6f42c1"})
        diffs = [self._diff("index.html", "<p style='color: #101418'>hi</p>")]
        drifts = self._drifts(diffs, design)
        self.assertTrue(
            any("#6f42c1" in d and "accent" in d for d in drifts),
            f"the unused accent must be named: {drifts}",
        )
        self.assertFalse(any("#101418" in d for d in drifts))

    def test_a_used_token_color_is_not_drift(self) -> None:
        design = self._only_colors({"name": "ink", "value": "#101418"})
        diffs = [self._diff("index.html", "<p style='color: #101418'>hi</p>")]
        self.assertEqual(self._drifts(diffs, design), [])

    def test_case_insensitive_token_match(self) -> None:
        design = self._only_colors({"name": "accent", "value": "#6f42c1"})
        diffs = [self._diff("index.html", "<p style='COLOR: #6F42C1'>hi</p>")]
        self.assertEqual(self._drifts(diffs, design), [])

    def test_a_family_word_satisfies_the_typography_token(self) -> None:
        design = self._contract()
        design["tokens"] = {"colors": [], "typography": [{"name": "body", "value": "Inter, system-ui, sans-serif"}], "spacing": [], "radii": []}
        design["acceptance"] = []
        design["constraints"] = []
        diffs = [self._diff("index.css", "body { font-family: 'Inter', serif; }")]
        self.assertEqual(self._drifts(diffs, design), [])

    def test_a_spacing_token_satisfies_the_token_check(self) -> None:
        design = self._contract()
        design["tokens"] = {"colors": [], "typography": [], "spacing": ["8px"], "radii": []}
        design["acceptance"] = []
        design["constraints"] = []
        diffs = [self._diff("index.css", "gap: 8px;")]
        self.assertEqual(self._drifts(diffs, design), [])

    def test_an_unevidenced_acceptance_line_is_drift(self) -> None:
        design = self._contract()
        design["tokens"] = {"colors": [], "typography": [], "spacing": [], "radii": []}
        design["constraints"] = []
        diffs = [self._diff("index.html", "<h1>hello</h1>")]
        drifts = self._drifts(diffs, design)
        self.assertTrue(
            any("acceptance not evidenced" in d and "dashboard header uses the accent" in d for d in drifts),
            f"the unaddressed acceptance line must be reported: {drifts}",
        )

    def test_an_addressed_acceptance_line_is_not_drift(self) -> None:
        design = self._contract()
        design["tokens"] = {"colors": [], "typography": [], "spacing": [], "radii": []}
        design["constraints"] = []
        diffs = [self._diff("index.html", "<header>dashboard header</header>")]
        self.assertEqual(self._drifts(diffs, design), [])

    def test_an_unevidenced_constraint_is_drift(self) -> None:
        design = self._contract()
        design["tokens"] = {"colors": [], "typography": [], "spacing": [], "radii": []}
        design["acceptance"] = []
        diffs = [self._diff("index.html", "<h1>hi</h1>")]
        drifts = self._drifts(diffs, design)
        self.assertTrue(any("constraint not evidenced" in d for d in drifts))
        self.assertTrue(any("no new dependencies" in d for d in drifts))

    def test_a_token_name_in_an_acceptance_line_is_not_itself_evidence(self) -> None:
        """An acceptance line whose only checkable words are a token name must
        not pass because that name appears in the diff as an identifier."""
        design = self._only_colors({"name": "accent", "value": "#6f42c1"})
        design["acceptance"] = ["accent covers every surface"]
        diffs = [self._diff("index.html", "accent accent accent nothing here")]
        drifts = self._drifts(diffs, design)
        self.assertTrue(
            any("acceptance not evidenced" in d for d in drifts),
            f"the token name alone must not count as evidence: {drifts}",
        )

    def test_drifts_are_capped(self) -> None:
        design = self._contract()
        design["acceptance"] = [
            f"acceptance criterion {c} with distinct words unaddressed" for c in range(MAX_BRAND_DRIFTS + 5)
        ]
        diffs = [self._diff("index.html", "nothing that satisfies any of it")]
        self.assertEqual(len(self._drifts(diffs, design)), MAX_BRAND_DRIFTS)

    # -- what evidence is read -----------------------------------------------

    def test_a_text_artifact_is_read_from_disk_when_the_diff_is_empty(self) -> None:
        """A write whose diff carries no text (a binary write) is still subject
        to the contract — read the artifact itself rather than shrug."""
        (self.root / "generated.css").write_text("accent: #6f42c1;", encoding="utf-8")
        design = self._only_colors({"name": "accent", "value": "#6f42c1"})
        diffs = [{"path": "generated.css", "action": "create", "unified_diff": "", "resolved_content": None}]
        self.assertEqual(self._drifts(diffs, design), [])

    def test_an_undecodable_artifact_reports_nothing(self) -> None:
        """A binary blob cannot be checked mechanically, and an absence of
        evidence is not evidence of absence — report nothing rather than a
        drift the checker cannot support."""
        (self.root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n accent #6f42c1")
        design = self._only_colors({"name": "accent", "value": "#6f42c1"})
        diffs = [{"path": "logo.png", "action": "create", "unified_diff": "", "resolved_content": None}]
        self.assertEqual(self._drifts(diffs, design), [])

    def test_evidence_comes_from_the_changes_not_the_whole_tree(self) -> None:
        """A workspace already full of the brand must not pass for that reason:
        the check reads what the fixer wrote, not everything there is."""
        (self.root / "brand").mkdir(parents=True, exist_ok=True)
        (self.root / "brand/DESIGN.md").write_text(BRAND, encoding="utf-8")
        diffs = [self._diff("index.html", "<h1>something unrelated</h1>")]
        self.assertNotEqual(self._drifts(diffs, self._only_colors({"name": "ink", "value": "#101418"})), [])


class VerifierCheckCase(_Harness):
    """The check wired into the step: the engine verifies what was written
    against the brand contract mechanically, and the finding reaches the
    transcript and the critic without becoming a verdict."""

    RESPONSES = _responses()
    BRAND = BRAND
    BRAND_PATH = "DESIGN.md"

    def _verifier_result(self) -> dict[str, Any]:
        """The verifier's own record: the first verdict-only result of the step.

        Later `test_result` events belong to the critic or the scribe running
        their own read-only commands (the only other roles that may run one),
        and none of those carry the verifier's brand check."""
        results = [e for e in self._events("test_result") if e.get("ran") is False]
        return results[0] if results else {}

    async def test_the_check_rides_on_the_test_result(self) -> None:
        """A fixer that ignores the contract's tokens gets a mechanical finding
        on the verifier's own record — the same payload every consumer reads.

        The check enforces the contract as locked (the design agent's
        normalization of the brand file, source-stamped by the engine), so the
        findings name its token values, not the file's prose."""
        self.provider.responses["fixer"] = {
            "files": [{"path": "ui/src/board.html", "action": "create", "content": "<main>plain</main>\n"}]
        }
        await self._run_the_step()
        result = self._verifier_result()
        self.assertTrue(result, "the verifier must publish a result")
        self.assertEqual(result["verdict"], "pass")
        self.assertTrue(result["brand_drifts"], "an ignored contract must be caught mechanically")
        self.assertTrue(any("#2f81f7" in d for d in result["brand_drifts"]))

    async def test_a_drift_is_warned_in_the_transcript(self) -> None:
        self.provider.responses["fixer"] = {
            "files": [{"path": "ui/src/board.html", "action": "create", "content": "<main>plain</main>\n"}]
        }
        await self._run_the_step()
        self.assertTrue(
            any(m.startswith("brand contract drift:") for m in self._warnings()),
            f"the drift must be stated, not silent: {self._warnings()}",
        )

    async def test_the_critic_is_shown_what_is_already_checked(self) -> None:
        """The findings are facts about the text, so the critic is told they are
        settled — its judgment is spent elsewhere."""
        self.provider.responses["fixer"] = {
            "files": [{"path": "ui/src/board.html", "action": "create", "content": "<main>plain</main>\n"}]
        }
        await self._run_the_step()
        critic_prompt = self.provider.prompt_for("critic")
        self.assertIn("Mechanical brand-contract findings", critic_prompt)
        self.assertIn("#2f81f7", critic_prompt)

    async def test_a_compliant_change_reports_no_drift(self) -> None:
        """A fixer that used the contract's tokens and echoed its acceptance
        and constraints draws no finding — the check must not be noise that
        fires on every step. The prose is echoed verbatim because that is
        exactly what the mechanical check reads for: evidence of each claim."""
        self.provider.responses["fixer"] = {
            "files": [{
                "path": "ui/src/board.html",
                "action": "create",
                "content": (
                    "<main class='kpi' style='color:#0d1117;background:#2f81f7;"
                    "font-family:Inter;padding:8px;border-radius:6px'>"
                    "KpiTile renders — reuse the existing token names, no new "
                    "dependencies, no placeholder values"
                    "</main>\n"
                ),
            }]
        }
        await self._run_the_step()
        self.assertEqual(self._verifier_result()["brand_drifts"], [])

    async def test_a_drift_never_fails_the_step(self) -> None:
        """Advisory by design: the verdict stays the tests', and only the tests
        (or the critic) fail a step — a mechanical finding must not."""
        self.provider.responses["fixer"] = {
            "files": [{"path": "ui/src/board.html", "action": "create", "content": "<main>plain</main>\n"}]
        }
        await self._run_the_step()
        # run_step is driven directly here (no driver loop), so the goal is
        # still RUNNING; the step itself is what the check must not fail.
        self.assertEqual(self.goals.steps(self.goal.id)[0].status, "COMPLETED")
        self.assertEqual(self._verifier_result()["verdict"], "pass")

    async def test_an_ignored_constraint_is_reported_too(self) -> None:
        """The contract's constraint line names nothing the change touches, so
        the mechanical check reports it where the critic might not have."""
        self.provider.responses["fixer"] = {
            "files": [{"path": "ui/src/board.html", "action": "create", "content": "<main>plain</main>\n"}]
        }
        await self._run_the_step()
        self.assertTrue(
            any("constraint not evidenced" in d for d in self._verifier_result()["brand_drifts"]),
            "the contract's constraints are checked, not just its tokens",
        )


class DesignDeliverableCase(_Harness):
    """A design-mode goal: the workspace's own brand contract is the deliverable.

    The usual relationship is inverted — the design agent authors DESIGN.md
    instead of deriving a direction from one — but the invariants do not move:
    the fixer is still the only writer, the critic still decides whether a step
    stands, and the pin stays a user action. These tests hold the three handoffs
    that make the inversion real: the draft survives into the plan, the write
    step is handed the draft rather than an interpretation, and the verifier
    reviews prose on disk instead of guessing a command to run.
    """

    RESPONSES = _responses()
    GOAL_KWARGS = {"mode": "design"}

    def _plan_writing_design_md(self) -> None:
        """The one plan shape the deliverable needs: a step that names its file.

        `suggested_paths` is the only honest signal a plan gives about intent,
        and it is what the fixer and the verifier both read to recognize the
        write step.
        """
        self.provider.responses["planner"] = {
            "steps": [{
                "title": "Write DESIGN.md",
                "description": "publish the brand contract",
                "suggested_paths": ["DESIGN.md"],
            }]
        }
        self.provider.responses["fixer"] = {
            "files": [{
                "path": "DESIGN.md",
                "action": "create",
                "content": CONTRACT["design_md"],
            }]
        }

    def _verifier_result(self) -> dict[str, Any]:
        results = [e for e in self._events("test_result") if e.get("ran") is False]
        return results[0] if results else {}

    # ── the mode is real storage, not a UI preference ──────────────────────

    async def test_the_mode_survives_the_database(self) -> None:
        self.assertEqual(self.goal.mode, "design")
        self.assertEqual(self.goals.get(self.goal.id).mode, "design")

    async def test_a_goal_that_does_not_ask_is_the_normal_pipeline(self) -> None:
        other = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="Fix a bug")
        )
        self.assertEqual(other.mode, "normal", "mode must not be opt-out")

    # ── the draft is the deliverable, and it survives ──────────────────────

    async def test_the_draft_is_published_with_its_body(self) -> None:
        await self.executor.run_planning(self.goal.id)
        contracts = self._events("design_contract")
        self.assertTrue(contracts, "a design goal must publish the draft it authored")
        self.assertEqual(contracts[-1]["mode"], "design")
        self.assertEqual(
            contracts[-1]["design_md"],
            CONTRACT["design_md"],
            "a normal goal's body is dropped behind a brand file; this one IS the file",
        )

    async def test_the_design_agent_is_told_it_is_the_author(self) -> None:
        await self.executor.run_planning(self.goal.id)
        design_prompt = self.provider.prompt_for("design")
        self.assertIn(
            DESIGN_BRIEF_PROMPT,
            design_prompt,
            "the brief is engine policy, so it is exported and asserted verbatim",
        )
        self.assertIn("write the one it should have", design_prompt)

    async def test_the_planner_is_handed_the_draft_to_plan_against(self) -> None:
        """The plan has to realize the draft — including the step that writes it.

        A planner that never receives the body cannot know a `DESIGN.md` step is
        needed, and the deliverable would stay a promise on an event.
        """
        await self.executor.run_planning(self.goal.id)
        planner_prompt = self.provider.prompt_for("planner")
        self.assertIn(CONTRACT["design_md"], planner_prompt)
        self.assertIn("write it verbatim in its own step", planner_prompt)

    async def test_a_draft_with_no_body_is_a_warning_not_a_failed_goal(self) -> None:
        """The one stage that must be loud: a design goal with nothing written
        has nothing to deliver. It still must not kill a plan the planner can
        make — the same non-fatal rule every other planning stage gets."""
        self.provider.responses["design"] = {
            k: v for k, v in CONTRACT.items() if k != "design_md"
        }
        await self.executor.run_planning(self.goal.id)
        self.assertEqual(
            self._events("design_contract"), [],
            "a goal with no body must not publish a contract it cannot deliver",
        )
        self.assertTrue(
            any("design unavailable" in w for w in self._warnings()),
            f"the missing body must be said out loud: {self._warnings()}",
        )
        self.assertTrue(self.goals.steps(self.goal.id), "planning still happens")

    # ── the fixer writes the draft, not an interpretation of it ────────────

    async def test_only_the_write_step_is_told_to_write_the_draft_verbatim(self) -> None:
        await self._run_the_step()
        self.assertNotIn(
            "this step writes the design deliverable itself",
            self.provider.prompt_for("fixer"),
            "a step that touches no DESIGN.md must not be told it is the deliverable",
        )

    async def test_the_write_step_gets_the_draft_verbatim(self) -> None:
        self._plan_writing_design_md()
        await self._run_the_step()
        fixer_prompt = self.provider.prompt_for("fixer")
        self.assertIn("this step writes the design deliverable itself", fixer_prompt)
        self.assertIn("write it to DESIGN.md verbatim", fixer_prompt)
        self.assertIn(CONTRACT["design_md"], fixer_prompt)

    # ── the verifier reviews prose instead of running a command ────────────

    async def test_the_verifier_reviews_the_written_file_without_a_command(self) -> None:
        """There is no code to falsify: the artifact is prose on disk. A command
        run here would be a wasted guess, so the verifier is told to return its
        verdict directly and is handed the file to judge."""
        self._plan_writing_design_md()
        await self._run_the_step()
        verifier_prompt = self.provider.prompt_for("verifier")
        self.assertIn("do not run a command", verifier_prompt)
        self.assertIn("--- DESIGN.md as written ---", verifier_prompt)
        self.assertIn(CONTRACT["design_md"], verifier_prompt)
        result = self._verifier_result()
        self.assertTrue(result, "the verifier must still publish a verdict")
        self.assertEqual(result["verdict"], "pass")
        self.assertFalse(result["ran"], "reviewing a file runs nothing")

    async def test_a_missing_file_is_told_to_skip_rather_than_pass(self) -> None:
        """A write step that produced nothing is not a pass — but it is also not
        something the verifier should invent a scope for. `skip` says exactly
        what is known: there was no file to review."""
        self.provider.responses["planner"] = {
            "steps": [{
                "title": "Write DESIGN.md",
                "description": "publish the brand contract",
                "suggested_paths": ["DESIGN.md"],
            }]
        }
        await self._run_the_step()
        verifier_prompt = self.provider.prompt_for("verifier")
        self.assertIn("No DESIGN.md was found on disk", verifier_prompt)
        self.assertIn("verdict 'skip'", verifier_prompt)

    async def test_both_judges_of_a_deliverable_read_the_same_bytes(self) -> None:
        """The verifier and the critic are the only two roles that judge this
        artifact, so they must not be judging different things.

        A dry run is the case that exposed it: the content exists only as the
        step's stored proposal, and reading it in two places is how one role
        ended up approving a `+`-prefixed diff while the other read the prose.
        """
        self._plan_writing_design_md()
        goal = self.goals.create(GoalCreate(
            workspace_id=self.ws.id, title="Dry draft", mode="design", dry_run=True,
        ))
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, self.goals.get(goal.id).version, "RUNNING")
        await self.executor.run_step(goal.id, step.id)

        # Nothing reached the disk...
        self.assertFalse((self.root / "DESIGN.md").exists())
        # ...and both judges were handed the proposal itself, labelled as one.
        for role in ("verifier", "critic"):
            prompt = self.provider.prompt_for(role)
            self.assertIn("as proposed by this step (nothing was written to disk)", prompt)
            self.assertIn(CONTRACT["design_md"], prompt, f"the {role} must be given the prose")

    async def test_an_empty_proposal_is_reviewed_rather_than_skipped(self) -> None:
        """A dry run that proposed an empty DESIGN.md proposed something, and an
        empty contract is a failure a reviewer must be able to state out loud.
        `skip` means there was nothing to review, not that the artifact was thin."""
        self._plan_writing_design_md()
        self.provider.responses["fixer"] = {
            "files": [{"path": "DESIGN.md", "action": "create", "content": ""}]
        }
        goal = self.goals.create(GoalCreate(
            workspace_id=self.ws.id, title="Empty draft", mode="design", dry_run=True,
        ))
        await self.executor.run_planning(goal.id)
        step = self.goals.steps(goal.id)[0]
        self.goals.update_status(goal.id, self.goals.get(goal.id).version, "RUNNING")
        await self.executor.run_step(goal.id, step.id)

        verifier_prompt = self.provider.prompt_for("verifier")
        self.assertIn("nothing was written to disk", verifier_prompt)
        self.assertNotIn(
            "verdict 'skip'",
            verifier_prompt,
            "an empty draft is a reviewable failure, not an absence to skip",
        )

    # ── the critic's approval is what the pin waits on ─────────────────────

    async def test_the_critic_is_told_its_approval_leads_to_the_pin(self) -> None:
        self._plan_writing_design_md()
        await self._run_the_step()
        critic_prompt = self.provider.prompt_for("critic")
        self.assertIn("delivers the workspace's DESIGN.md itself", critic_prompt)
        self.assertIn("only your approval puts it in front of them", critic_prompt)


class DesignDeliverableRevisionCase(_Harness):
    """A design-mode goal in a workspace that already has a brand contract.

    Revision is as valid as invention, and that is the one thing the normal
    pipeline may not do: a goal answering a pinned brand with a competing body
    has that body dropped. A design goal is shown the current contract as
    material and still delivers its own draft.
    """

    RESPONSES = _responses()
    GOAL_KWARGS = {"mode": "design"}
    BRAND = BRAND
    BRAND_PATH = "DESIGN.md"

    async def test_the_existing_brand_is_shown_as_revision_material(self) -> None:
        await self.executor.run_planning(self.goal.id)
        design_prompt = self.provider.prompt_for("design")
        self.assertIn("current brand contract (discovered)", design_prompt)
        self.assertIn(BRAND, design_prompt)
        self.assertIn("revise or replace it", design_prompt)

    async def test_a_revision_still_delivers_its_own_body(self) -> None:
        await self.executor.run_planning(self.goal.id)
        self.assertEqual(
            self._events("design_contract")[-1]["design_md"],
            CONTRACT["design_md"],
            "the draft is the deliverable even when a contract already governs",
        )


class DesignDeliverableEndToEndCase(_Harness):
    """The whole chain over HTTP: create the goal, let it plan, run its step,
    pin the file the step wrote, and watch that pin govern the next goal.

    Every other design-mode test drives the executor directly, which is the
    right unit of proof for one handoff but not for the promise. The promise is
    a path — `mode: "design"` entering through `POST /goals`, a draft becoming a
    file on disk, the critic approving it, the user pinning that file, and the
    pin then being what the next goal obeys — and a path only exists end to end.

    The provider is scripted, but nothing else is: real routes, real database,
    real filesystem, real git, and the background planning and step tasks the
    routes spawn.
    """

    RESPONSES = _responses()
    # The stub scribe's message, asserted against what git actually records.
    COMMIT_MESSAGE = "docs: add the workspace brand contract"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # The harness's engine objects, wired into the app the routes read from:
        # the API is what is under test here, not a second engine built next to it.
        app.state.conn = self.conn
        app.state.registry = self.registry
        app.state.goals = self.goals
        app.state.workspaces = self.workspaces
        app.state.executor = self.executor
        app.state.token = BOOT_TOKEN
        self.client = httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}
        self._plan_the_write_step()
        self._scribe_commit()

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def _wait_for(self, check: Callable[[], Awaitable[bool]], what: str) -> None:
        """Let the spawned pipeline tasks run until `check` is true.

        Planning and step execution are background tasks the routes spawn, not
        work the request waits for — so the test waits the way the UI does, by
        polling the API, rather than reaching into the executor for the answer.
        """
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if await check():
                return
            await asyncio.sleep(0.01)
        self.fail(f"timed out waiting for {what}")

    async def _wait_for_file(self, path: Path, what: str) -> None:
        async def exists() -> bool:
            return path.exists()

        await self._wait_for(exists, what)

    # ── the API, as the UI calls it ───────────────────────────────────────

    async def _make_workspace(self, name: str, *, git: bool = False) -> tuple[str, Path]:
        """Register a workspace over HTTP, optionally as a real git checkout.

        The repository matters: the scribe only commits against one
        (`GitService.is_git_repo`), and a deliverable that never reaches git is
        a file the next run may overwrite without a record of what it replaced.
        """
        ws_dir = self.root / name
        ws_dir.mkdir()
        if git:
            self.assertTrue(
                GitService().init_repo(str(ws_dir)),
                "the fixture repository must initialize",
            )
        r = await self.client.post(
            "/workspaces", headers=self.headers,
            json={"name": name.upper(), "root_path": str(ws_dir)},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"], ws_dir

    async def _create_design_goal(self, ws_id: str, **extra: Any) -> str:
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={
                "workspace_id": ws_id,
                "title": "Draft the brand contract",
                "description": "The workspace has no DESIGN.md today.",
                "mode": "design",
                **extra,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        created: dict[str, Any] = r.json()
        self.assertEqual(created["mode"], "design")
        goal_id: str = created["id"]
        return goal_id

    async def _goal_json(self, goal_id: str) -> dict[str, Any]:
        res = await self.client.get(f"/goals/{goal_id}", headers=self.headers)
        goal: dict[str, Any] = res.json()
        return goal

    async def _http_events(self, goal_id: str) -> list[dict[str, Any]]:
        """The event log as a client sees it — what the transcript renders from."""
        res = await self.client.get(f"/goals/{goal_id}/events", headers=self.headers)
        events: list[dict[str, Any]] = res.json()
        return events

    async def _settle(self, goal_id: str, status: str, what: str) -> dict[str, Any]:
        """Wait for the goal to reach `status`, then hand back the goal body."""
        async def reached() -> bool:
            return bool((await self._goal_json(goal_id))["status"] == status)

        await self._wait_for(reached, what)
        return await self._goal_json(goal_id)

    async def _start(self, goal_id: str) -> None:
        goal = await self._goal_json(goal_id)
        r = await self.client.post(
            f"/goals/{goal_id}/start", headers=self.headers,
            json={"expected_version": goal["version"]},
        )
        self.assertEqual(r.status_code, 200, r.text)

    async def _pin(self, ws_id: str, path: str) -> httpx.Response:
        return await self.client.put(
            f"/workspaces/{ws_id}/design-contract",
            headers=self.headers, json={"path": path},
        )

    def _git(self, ws_dir: Path, *args: str) -> str:
        """Read the fixture repository directly.

        The engine's own report of a commit is a claim about the workspace; this
        is the check, run the way tests/test_git.py runs git.
        """
        res = subprocess.run(
            ["git", *args], cwd=ws_dir, capture_output=True, text=True, check=False,
        )
        self.assertEqual(res.returncode, 0, f"git {' '.join(args)}: {res.stderr}")
        return res.stdout.strip()

    def _plan_the_write_step(self) -> None:
        """The plan and the write this deliverable needs.

        The step names its own file, and the fixer writes the draft verbatim —
        which is what a real fixer is told to do for this step, and what has to
        happen for there to be a file to pin.
        """
        self.provider.responses["planner"] = {
            "steps": [{
                "title": "Write DESIGN.md",
                "description": "publish the brand contract",
                "suggested_paths": ["DESIGN.md"],
            }]
        }
        self.provider.responses["fixer"] = {
            "files": [{
                "path": "DESIGN.md",
                "action": "create",
                "content": CONTRACT["design_md"],
            }]
        }

    def _scribe_commit(self) -> None:
        """A commit message distinctive enough to assert against git's own log."""
        self.provider.responses["scribe"] = {
            "summary": "The workspace's brand contract is written and reviewed.",
            "commit_message": self.COMMIT_MESSAGE,
        }

    async def test_a_design_goal_writes_its_contract_and_the_pin_makes_it_bind(self) -> None:
        ws_id, ws_dir = await self._make_workspace("brand-e2e")

        # ── the mode enters through the API and the agent authors a draft ─────
        goal_id = await self._create_design_goal(ws_id)
        await self._settle(goal_id, "PENDING", "the plan to be ready")
        self.assertIn(
            DESIGN_BRIEF_PROMPT,
            self.provider.prompt_for("design"),
            "the mode must reach the agent as its brief, not merely a column",
        )

        steps = (await self._goal_json(goal_id))["steps"]
        self.assertTrue(steps, "the draft must be planned into at least one step")
        self.assertIn("DESIGN.md", steps[0]["suggested_paths"])

        # The draft the UI pins from, read the way the UI reads it.
        contracts = [
            e["payload"] for e in await self._http_events(goal_id)
            if e["type"] == "design_contract"
        ]
        self.assertTrue(contracts, "a design goal must publish the draft it authored")
        contract = contracts[-1]
        self.assertEqual(contract["mode"], "design")
        body = contract["design_md"]
        self.assertTrue(body.strip(), "the published draft must carry its body")

        # ── nothing has been written yet, so there is nothing to pin ──────────
        r = await self._pin(ws_id, "DESIGN.md")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["code"], "design_contract_missing")
        self.assertFalse((ws_dir / "DESIGN.md").exists())

        # ── the step runs: fixer writes the draft, verifier reviews, critic rules
        await self._start(goal_id)
        completed = await self._settle(goal_id, "COMPLETED", "the step to run and be reviewed")

        # Written verbatim — the fixer was handed the draft, not the direction
        # to interpret — and reviewed as prose rather than as a command's subject.
        self.assertEqual((ws_dir / "DESIGN.md").read_text(encoding="utf-8"), body)
        verifier_prompt = self.provider.prompt_for("verifier")
        self.assertIn("do not run a command", verifier_prompt)
        self.assertIn("--- DESIGN.md as written ---", verifier_prompt)
        self.assertIn("delivers the workspace's DESIGN.md itself", self.provider.prompt_for("critic"))
        self.assertIn(
            "DESIGN.md as written",
            self.provider.prompt_for("critic"),
            "the approver reads the prose, not only the diff it also receives",
        )
        self.assertEqual(completed["steps"][0]["status"], "COMPLETED")
        # The verdict the review produced, on the same record the UI renders:
        # `ran: false` is the engine saying nothing was executed.
        verdicts = [
            e["payload"] for e in await self._http_events(goal_id)
            if e["type"] == "test_result" and e["payload"].get("ran") is False
        ]
        self.assertTrue(verdicts, "the review must publish a verdict of its own")
        self.assertEqual(verdicts[0]["verdict"], "pass")

        # ── the user pins what the run produced ──────────────────────────────
        r = await self._pin(ws_id, "DESIGN.md")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["design_contract_path"], "DESIGN.md")

        # ── and the pin is now what the next goal obeys ──────────────────────
        r = await self.client.post(
            "/goals", headers=self.headers,
            json={"workspace_id": ws_id, "title": "Use the brand"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        second_id = r.json()["id"]
        self.assertEqual(r.json()["mode"], "normal", "the mode is per goal, not per workspace")
        await self._settle(second_id, "PENDING", "the second goal to plan")

        governed = [
            e["payload"] for e in await self._http_events(second_id)
            if e["type"] == "design_contract"
        ][-1]
        self.assertEqual(governed["design_system"]["origin"], "pinned")
        self.assertEqual(
            governed["design_system"]["source"], "DESIGN.md",
            "the engine states the path it resolved, not one the model named",
        )
        self.assertFalse(
            governed.get("design_md"),
            "a pinned brand drops a competing body — the draft is now the file",
        )

    async def test_the_reviewed_deliverable_is_what_the_scribe_commits(self) -> None:
        """The chain in a real repository, where the commit is the last stage.

        A deliverable that never reaches git is a file the next run may overwrite
        with no record of what it replaced. So the strongest form of the promise
        is not "a file exists" but "the reviewed contract is committed, and the
        user's own uncommitted work was not swept into it" — the scribe is
        handed only the paths the step wrote.
        """
        ws_id, ws_dir = await self._make_workspace("brand-git", git=True)
        # The user's own half-finished work, sitting in the tree on purpose.
        scratch = "half-finished, not mine to commit\n"
        (ws_dir / "scratch.txt").write_text(scratch, encoding="utf-8")

        goal_id = await self._create_design_goal(ws_id)
        await self._settle(goal_id, "PENDING", "the plan to be ready")
        await self._start(goal_id)
        await self._settle(goal_id, "COMPLETED", "the step to run, be reviewed and committed")

        self.assertEqual(
            (ws_dir / "DESIGN.md").read_text(encoding="utf-8"), CONTRACT["design_md"]
        )

        # The engine's own report, in the transcript the user reads.
        committed = [
            str(e["payload"].get("message", "")) for e in await self._http_events(goal_id)
            if e["type"] == "log" and "git committed" in str(e["payload"].get("message", ""))
        ]
        self.assertTrue(committed, f"the scribe must report its commit: {committed}")
        self.assertIn(self.COMMIT_MESSAGE, committed[-1])

        # And git agrees — one commit, exactly the file the step wrote.
        self.assertEqual(self._git(ws_dir, "log", "--format=%s", "-1"), self.COMMIT_MESSAGE)
        self.assertEqual(
            self._git(ws_dir, "show", "--name-only", "--format=", "HEAD").split(),
            ["DESIGN.md"],
            "the user's own work must not ride along in the step's commit",
        )
        self.assertEqual((ws_dir / "scratch.txt").read_text(encoding="utf-8"), scratch)

        # The committed file is what the pin binds — the commit is not a
        # substitute for it, it is what makes the file survive to be pinned.
        r = await self._pin(ws_id, "DESIGN.md")
        self.assertEqual(r.status_code, 200, r.text)

    async def test_a_dry_run_deliverable_is_reviewed_but_not_written_until_applied(self) -> None:
        """The case where the reviewed draft is not yet the deliverable.

        A dry run reaches the disk not at all, so there is no file for the
        verifier to read and nothing for the pin to bind — and the engine says
        both out loud instead of letting a reviewed draft look written. Applying
        the run writes exactly what was reviewed, and only then does a pin hold.
        """
        ws_id, ws_dir = await self._make_workspace("brand-dry-run", git=True)
        goal_id = await self._create_design_goal(ws_id, dry_run=True)
        await self._settle(goal_id, "PENDING", "the plan to be ready")

        draft = [
            e["payload"]["design_md"] for e in await self._http_events(goal_id)
            if e["type"] == "design_contract"
        ][-1]
        self.assertTrue(draft.strip(), "a dry run still authors the deliverable")

        await self._start(goal_id)
        await self._settle(goal_id, "COMPLETED", "the dry-run step to be reviewed")

        # Nothing was written — and the review still had the artifact to judge:
        # a dry run's deliverable exists as the step's stored proposal, which is
        # byte-for-byte what Apply writes. Telling the verifier there was nothing
        # to read would have made "reviewed" a claim about the pipeline.
        self.assertFalse((ws_dir / "DESIGN.md").exists())
        verifier_prompt = self.provider.prompt_for("verifier")
        self.assertIn("nothing was written to disk", verifier_prompt)
        self.assertIn(draft, verifier_prompt, "the proposal is the artifact under review")
        self.assertIn("do not answer 'skip' because it is not on disk", verifier_prompt)
        self.assertIn(
            "as proposed by this step",
            self.provider.prompt_for("critic"),
            "the role that decides the step reads the proposal too, not just its diff",
        )
        proposals = [
            e["payload"] for e in await self._http_events(goal_id)
            if e["type"] == "file_change_summary"
        ]
        self.assertTrue(proposals, "a dry run must publish a proposal")
        self.assertTrue(proposals[-1]["dry_run"], "the change is a proposal, not a write")
        self.assertEqual(proposals[-1]["paths"], ["DESIGN.md"])

        # And the bytes the verifier was handed are the stored proposal itself —
        # the same record Apply replays, not a re-render of the diff.
        step_id = (await self._goal_json(goal_id))["steps"][0]["id"]
        self.assertEqual(self.goals.proposed_content(goal_id, step_id, "DESIGN.md"), draft)
        self.assertEqual(
            self.goals.proposed_content(goal_id, step_id, "design.md"),
            draft,
            "a plan that capitalized the name still finds its own proposal",
        )
        self.assertIsNone(
            self.goals.proposed_content(goal_id, step_id, "src/nothing.md"),
            "a path this step never proposed has no content to review",
        )
        self.assertIsNone(
            self.goals.proposed_content(goal_id, "no-such-step", "DESIGN.md"),
            "another step's proposal is not this step's to review",
        )
        self.assertNotEqual(
            subprocess.run(
                ["git", "rev-list", "--count", "HEAD"], cwd=ws_dir,
                capture_output=True, text=True, check=False,
            ).returncode,
            0,
            "a dry run must commit nothing — there is no revision to point at",
        )

        # A reviewed draft with no file behind it is not pinnable.
        r = await self._pin(ws_id, "DESIGN.md")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["code"], "design_contract_missing")

        # ── Apply writes exactly what was reviewed, and commits it ───────────
        goal_body = await self._goal_json(goal_id)
        r = await self.client.post(
            f"/goals/{goal_id}/apply", headers=self.headers,
            json={"expected_version": goal_body["version"]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # Apply is a background run too; its terminal signal is the commit.
        async def applied() -> bool:
            return any(
                "git committed" in str(e["payload"].get("message", ""))
                for e in await self._http_events(goal_id)
                if e["type"] == "log"
            )

        await self._wait_for(applied, "the applied deliverable to be committed")
        self.assertEqual((ws_dir / "DESIGN.md").read_text(encoding="utf-8"), draft)
        self.assertEqual(self._git(ws_dir, "show", "--name-only", "--format=", "HEAD").split(), ["DESIGN.md"])
        self.assertIn(
            "DESIGN.md as written",
            self.provider.prompt_for("critic"),
            "after Apply the reviewer reads the file itself, not a proposal",
        )

        # Now, and only now, the reviewed draft can be pinned.
        r = await self._pin(ws_id, "DESIGN.md")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["design_contract_path"], "DESIGN.md")

