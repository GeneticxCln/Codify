"""A lone surrogate in a role's reply is healed where it becomes prose, and refused where it becomes a file.

A model can spell `"\\ud800"` in its JSON; `json.loads` hands the engine a `str` that no SQLite bind, git argv or
request body can carry. PR 56 closed two doors (events, and the fixer's paths and content). This module pins the
rest of the role boundary (docs/04 section 4 and 6), found by driving each role with a poisoned reply:

- the planner's title and description made the goal FAILED with the codec's own sentence and no re-ask; a title that
  was not a string escaped `run_planning` as a `sqlite3.ProgrammingError`; a plan whose second step could not be
  stored left the first behind, and a goal with a plan cannot be planned again;
- the critic's reasons failed the step as `replay_failed` by the fixer, the codec's error blamed on the wrong role;
- the scribe's commit message reached `git commit` unhealed: a low surrogate COMMITTED (as byte 0x80) and then the
  database write failed the step, an irreversible act followed by a failure;
- a verifier explanation went into the fixer's retry prompt, and the librarian's and designer's text into the
  planner's, where the request could not be encoded;
- a path stored as `["a\\ud800.py"]` is written as ASCII by `json.dumps` and read back as the poisoned `str`, so
  `GET /goals/{id}` answered 500 for rows already in people's databases.

The rule: text a model wrote is HEALED (a lone surrogate becomes U+FFFD) wherever it becomes prose; it is REFUSED,
and the model asked once, wherever it becomes the person's file or path. The fixer is the one role whose reply is
not healed, and the tests that pin it are as much the point as the ones that pin the healing.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.agent_orchestrator import AgentOrchestrator
from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.executor import ExecutorService
from engine.laya import GateCall, LayaDecision, LayaService
from engine.models import ROLES, AgentConfig, AgentConfigUpdate, GoalCreate, WorkspaceCreate, scrub_surrogates
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.executor_support import AgentOutputInvalid
from engine.replies import REFUSED_FIELDS, VERBATIM_ROLES
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService

FFFD = "�"
SOURCE = 'def greet(name):\n    return f"hello {name}"\n'
GOOD_FILES = {"files": [{"path": "a.py", "action": "update", "content": "x = 2\n"}]}
STEP = {"title": "Rename", "description": "rename greet", "suggested_paths": ["a.py"]}


class ScriptedProvider(BaseProvider):
    """Answers each role from a list (the last entry repeats), and encodes every prompt as httpx would.

    The encode is the point: a poisoned prompt must fail here exactly as it fails on the wire, so a test passes only
    when the engine kept the surrogate out of the request, not when the double happened to be lenient.
    """

    def __init__(self) -> None:
        self.replies: dict[str, list[Any]] = {
            "librarian": [{"summary": "s", "files": [], "conventions": [], "enough": True}],
            "design": [{"applies": False}],
            "planner": [{"steps": [dict(STEP)]}],
            "fixer": [GOOD_FILES],
            "verifier": [{"argv": None, "verdict": "pass", "explanation": "nothing to run"}],
            "critic": [{"decision": "approve", "reasons": []}],
            "scribe": [{"summary": "did it", "commit_message": "feat: rename"}],
        }
        self.prompts: dict[str, list[str]] = {role: [] for role in ROLES}

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None, json_schema: dict[str, Any] | None = None,
    ) -> str:
        system_prompt.encode("utf-8")
        user_prompt.encode("utf-8")
        role = next((r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown")
        self.prompts[role].append(user_prompt)
        queue = self.replies.get(role, [{}])
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        # `json.dumps` writes a lone surrogate as the ASCII escape `\ud800`, which is how a model spells it.
        return reply if isinstance(reply, str) else json.dumps(reply)


class Factory(ProviderFactory):
    def __init__(self, provider: BaseProvider) -> None:
        super().__init__(Keychain())
        self.provider = provider

    def build(self, config: AgentConfig) -> BaseProvider:
        return self.provider


class Gate(LayaService):
    async def decide(self, state: dict[str, Any], on_call: GateCall | None = None) -> LayaDecision:
        return LayaDecision(engine="skipped", skipped_reason="test double")


class SinkCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "a.py").write_text(SOURCE, encoding="utf-8")
        self.conn = connect(self.root / "t.db")
        self.addCleanup(self.conn.close)
        self.provider = ScriptedProvider()
        registry = AgentRegistryService(self.conn, Factory(self.provider), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(model_name="test-model"))
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(self.goals, self.workspaces, registry, SandboxService(), laya=Gate())
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    def reply(self, role: str, *replies: Any) -> None:
        self.provider.replies[role] = list(replies)

    def events(self, type_: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.goals.events_after(self.goal.id, 0) if e.type == type_]

    def logs(self) -> list[str]:
        return [e["message"] for e in self.events("log")]

    def warnings(self) -> list[str]:
        return [e["message"] for e in self.events("log") if e.get("level") == "warn"]

    @property
    def file(self) -> str:
        return (self.root / "a.py").read_text(encoding="utf-8")

    async def plan(self) -> None:
        await self.executor.run_planning(self.goal.id)

    async def run_the_step(self) -> str:
        """Plan by hand, approve, and run the one step through fixer, verifier, critic and scribe."""
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")
        self.executor._insert_steps(self.goal.id, [dict(STEP)])
        step = self.goals.steps(self.goal.id)[0]
        await self.executor.run_step(self.goal.id, step.id)
        return self.goals.steps(self.goal.id)[0].status


class TestThePlanner(SinkCase):
    async def test_a_title_description_and_path_with_a_lone_surrogate_make_a_plan_not_a_failure(self) -> None:
        self.reply("planner", {"steps": [
            {"title": "Re\ud800name", "description": "rename \udc80 greet", "suggested_paths": ["a\ud800.py"]},
        ]})

        await self.plan()

        self.assertEqual([], self.events("error"))
        self.assertEqual(1, len(self.provider.prompts["planner"]), "a healed reply is not a reason to ask again")
        [step] = self.goals.steps(self.goal.id)
        self.assertEqual(f"Re{FFFD}name", step.title)
        self.assertEqual(f"rename {FFFD} greet", step.description)
        self.assertEqual([f"a{FFFD}.py"], step.suggested_paths)
        step.model_dump_json()  # raises if anything unencodable is left in the model
        self.assertEqual("PENDING", self.goals.get(self.goal.id).status)

    async def test_the_reply_to_the_re_ask_is_healed_too(self) -> None:
        # Every other test poisons the FIRST reply. The second parse site is a separate line of code.
        self.reply("planner", "not json at all", {"steps": [{**STEP, "title": "Re\ud800name"}]})

        await self.plan()

        self.assertEqual([], self.events("error"))
        self.assertEqual(2, len(self.provider.prompts["planner"]), "the planner should have been re-asked once")
        [step] = self.goals.steps(self.goal.id)
        self.assertEqual(f"Re{FFFD}name", step.title)

    async def test_accept_is_handed_the_healed_reply_not_the_poisoned_one(self) -> None:
        # The contract is "healed before accept and before any effect": an accept with an effect must never see the text.
        seen: list[Any] = []
        self.reply("planner", {"steps": [{**STEP, "title": "Re\ud800name"}]})

        await self.executor.orchestrator.run_agent(
            "planner", self.goal.id, None, "plan it", accept=seen.append,
        )

        self.assertEqual(f"Re{FFFD}name", seen[0]["steps"][0]["title"])

    async def test_accept_is_handed_the_healed_reply_to_the_re_ask_too(self) -> None:
        seen: list[Any] = []

        def accept(parsed: Any) -> None:
            seen.append(parsed)
            if len(seen) == 1:
                raise ValueError("not good enough")

        self.reply("planner", {"steps": [dict(STEP)]}, {"steps": [{**STEP, "title": "Re\ud800name"}]})

        await self.executor.orchestrator.run_agent("planner", self.goal.id, None, "plan it", accept=accept)

        self.assertEqual(2, len(seen), "accept should have been asked twice")
        self.assertEqual(f"Re{FFFD}name", seen[1]["steps"][0]["title"])

    async def test_a_reply_nested_too_deeply_to_walk_is_asked_about_once_not_left_to_escape(self) -> None:
        # `json.loads` accepts a reply nested nearly as deep as the interpreter's stack, and the healing walk needs a
        # frame per level: RecursionError (a RuntimeError) escaped `run_agent` and left the goal PLANNING. The walk is
        # made to fail here rather than sized to the stack, which depends on how deep the test runner already is.
        self.reply("planner", {"steps": [dict(STEP)]}, {"steps": [dict(STEP)]})
        real = scrub_surrogates
        failed: list[int] = []

        def walk(value: Any) -> Any:
            # The librarian and the designer are healed first; only the planner's first reply is the deep one.
            if isinstance(value, dict) and "steps" in value and not failed:
                failed.append(1)
                raise RecursionError("maximum recursion depth exceeded")
            return real(value)

        with mock.patch("engine.agent_orchestrator.scrub_surrogates", walk):
            await self.plan()

        self.assertEqual([], self.events("error"))
        self.assertEqual(2, len(self.provider.prompts["planner"]), "the planner should have been re-asked once")
        self.assertIn("nested too deeply", self.provider.prompts["planner"][1])
        self.assertEqual("PENDING", self.goals.get(self.goal.id).status)
        self.assertEqual(1, len(self.goals.steps(self.goal.id)))

    async def test_healing_a_reply_is_said_once_in_the_log(self) -> None:
        self.reply("planner", {"steps": [{**STEP, "title": "Re\ud800name"}]})

        await self.plan()

        said = [w for w in self.warnings() if "lone surrogate" in w]
        self.assertEqual(1, len(said), self.warnings())
        self.assertIn("planner", said[0])

    async def test_a_clean_reply_is_not_reported_as_healed(self) -> None:
        await self.plan()

        self.assertEqual([], [w for w in self.warnings() if "lone surrogate" in w])
        self.assertEqual(1, len(self.goals.steps(self.goal.id)))

    async def test_a_title_that_is_not_a_string_is_asked_about_once_and_the_corrected_plan_is_stored(self) -> None:
        self.reply("planner", {"steps": [dict(STEP), {**STEP, "title": ["not", "text"]}]}, {"steps": [dict(STEP)]})

        await self.plan()

        self.assertEqual(2, len(self.provider.prompts["planner"]), "the planner should have been re-asked exactly once")
        self.assertIn("title and description must be strings", self.provider.prompts["planner"][1])
        self.assertEqual([], self.events("error"))
        self.assertEqual(["Rename"], [s.title for s in self.goals.steps(self.goal.id)])

    async def test_a_description_that_is_not_a_string_is_refused_the_same_way(self) -> None:
        self.reply("planner", {"steps": [{**STEP, "description": {"a": 1}}]}, {"steps": [dict(STEP)]})

        await self.plan()

        self.assertEqual(2, len(self.provider.prompts["planner"]))
        self.assertIn("must be strings", self.provider.prompts["planner"][1])
        self.assertEqual(1, len(self.goals.steps(self.goal.id)))

    async def test_a_plan_the_planner_cannot_correct_leaves_no_steps_and_nothing_blocks_planning_it_again(self) -> None:
        bad = {"steps": [dict(STEP), {**STEP, "title": ["not", "text"]}]}
        self.reply("planner", bad, bad)

        await self.plan()

        self.assertEqual(2, len(self.provider.prompts["planner"]), "more than the one re-ask")
        self.assertEqual([], self.goals.steps(self.goal.id), "a partial plan was left behind")
        self.assertEqual("agent_output_invalid", self.events("error")[-1]["code"])
        self.assertEqual("FAILED", self.goals.get(self.goal.id).status)

        # `_plan_goal` answers "this goal already has a plan" to anything that finds a step, and the old code left
        # one of two behind, so a goal whose planning failed could never be planned again.
        self.assertEqual([], [m for m in self.logs() if "already has a plan" in m])

    async def test_insert_steps_stores_all_of_a_plan_or_none_of_it(self) -> None:
        poisoned = [dict(STEP), {"title": ["not", "text"], "description": "d", "suggested_paths": []}]

        with self.assertRaises(sqlite3.Error):
            self.executor._insert_steps(self.goal.id, poisoned)

        self.assertEqual([], self.goals.steps(self.goal.id), "the step before the bad one survived")
        self.assertFalse(self.conn.in_transaction, "a failed insert left a transaction open")
        self.executor._insert_steps(self.goal.id, [dict(STEP), {**STEP, "title": "Second"}])
        self.assertEqual(["Rename", "Second"], [s.title for s in self.goals.steps(self.goal.id)])

    async def test_a_bad_insert_does_not_commit_what_was_written_before_it(self) -> None:
        # The failure path used to commit the connection afterwards, and with it the half a plan.
        with self.assertRaises(sqlite3.Error):
            self.executor._insert_steps(self.goal.id, [dict(STEP), {**STEP, "title": ["x"]}])

        self.conn.commit()

        self.assertEqual([], self.goals.steps(self.goal.id))

    async def test_a_failure_that_is_not_the_databases_rolls_back_the_earlier_steps_too(self) -> None:
        # A step dict missing a key is a KeyError, not a sqlite3.Error: the rollback must not depend on who failed.
        with self.assertRaises(KeyError):
            self.executor._insert_steps(self.goal.id, [dict(STEP), {"title": "no description"}])

        self.conn.commit()

        self.assertEqual([], self.goals.steps(self.goal.id))


class TestTheCritic(SinkCase):
    async def test_reasons_with_a_lone_surrogate_pause_the_goal_as_a_rejection(self) -> None:
        self.reply("critic", {"decision": "request-changes", "reasons": ["bad \ud800 thing", "and \udc80 this"]})

        await self.run_the_step()

        self.assertEqual([], self.events("error"), "the codec's error was filed against the wrong role")
        [step] = self.goals.steps(self.goal.id)
        self.assertEqual("IN_PROGRESS", step.status)
        self.assertEqual(f"bad {FFFD} thing\nand {FFFD} this", step.review_notes)
        self.assertEqual("PAUSED", self.goals.get(self.goal.id).status)
        paused = [e for e in self.events("goal_status") if e["status"] == "PAUSED"]
        self.assertEqual("critic_rejected", paused[-1]["reason_code"])


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return done.stdout


class TestTheScribe(SinkCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        git(self.root, "init", "-q")
        git(self.root, "add", "a.py")
        git(self.root, "commit", "-q", "-m", "initial")

    def subjects(self) -> list[str]:
        return git(self.root, "log", "--format=%s").splitlines()

    async def commit_with(self, lone: str) -> None:
        self.reply("scribe", {"summary": "did it", "commit_message": f"feat: a{lone}b"})
        before = len(self.subjects())

        status = await self.run_the_step()

        self.assertEqual([], self.events("error"))
        self.assertEqual("COMPLETED", status)
        self.assertEqual(before + 1, len(self.subjects()), "not exactly one new commit")
        self.assertEqual(f"feat: a{FFFD}b", self.subjects()[0])
        self.assertEqual(f"feat: a{FFFD}b", self.goals.steps(self.goal.id)[0].commit_message)

    async def test_a_high_surrogate_in_the_commit_message_is_healed_before_git_sees_it(self) -> None:
        # Unhealed, this raised out of the subprocess call: nothing committed, the step FAILED replay_failed.
        await self.commit_with("\ud800")

    async def test_a_low_surrogate_in_the_commit_message_does_not_commit_and_then_fail_the_step(self) -> None:
        # Unhealed, this COMMITTED (the surrogate escaped to byte 0x80) and then the database write failed the step:
        # an irreversible act followed by a failure.
        await self.commit_with("\udc80")


class TestTheVerifier(SinkCase):
    async def test_an_explanation_with_a_lone_surrogate_reaches_the_fixers_retry_in_a_form_it_can_be_sent(self) -> None:
        self.reply(
            "verifier",
            {"argv": None, "verdict": "fail", "explanation": "bad \ud800 result"},
            {"argv": None, "verdict": "pass", "explanation": "fixed"},
        )

        status = await self.run_the_step()

        self.assertEqual([], self.events("error"))
        self.assertEqual("COMPLETED", status)
        self.assertEqual(2, len(self.provider.prompts["fixer"]), "the fixer was not asked for a retry")
        retry = self.provider.prompts["fixer"][1]
        self.assertIn(f"bad {FFFD} result", retry)


class TestTheLibrarianAndTheDesigner(SinkCase):
    async def test_a_librarian_summary_with_a_lone_surrogate_reaches_the_planner_in_a_form_it_can_be_sent(self) -> None:
        self.reply("librarian", {
            "summary": "the repo has a \ud800 problem", "files": [], "conventions": ["use \udc80 tabs"], "enough": True,
        })

        await self.plan()

        self.assertEqual([], self.events("error"))
        self.assertEqual(1, len(self.goals.steps(self.goal.id)))
        self.assertIn(f"the repo has a {FFFD} problem", self.provider.prompts["planner"][0])

    async def test_a_design_direction_with_a_lone_surrogate_reaches_the_planner_in_a_form_it_can_be_sent(self) -> None:
        self.reply("design", {
            "applies": True, "artifact": "web", "direction": "calm \ud800 and plain", "constraints": ["no \udc80"],
        })

        await self.plan()

        self.assertEqual([], [e for e in self.events("error")])
        self.assertEqual(1, len(self.goals.steps(self.goal.id)))
        self.assertIn(f"calm {FFFD} and plain", self.provider.prompts["planner"][0])

    async def test_design_md_is_refused_and_the_designer_is_asked_once_more(self) -> None:
        # The one body a model writes that a later step is told to write verbatim into a file, with a plan that can
        # start itself under Direct Apply: a heal could put a U+FFFD into the person's repository unreviewed, so the
        # designer is told which field and asked once more (`replies.REFUSED_FIELDS`).
        self.assertNotIn("design", VERBATIM_ROLES)
        self.assertEqual(("design_md",), REFUSED_FIELDS["design"])
        self.reply(
            "design",
            {"applies": True, "artifact": "web", "design_md": "# Title \ud800\n\nbody"},
            {"applies": True, "artifact": "web", "design_md": "# Title\n\nbody"},
        )

        out = await self.executor.orchestrator.run_agent("design", self.goal.id, None, "design it")

        self.assertEqual("# Title\n\nbody", out["design_md"])
        self.assertEqual(2, len(self.provider.prompts["design"]))
        self.assertIn("design_md holds a character that is not valid text", self.provider.prompts["design"][1])

    async def test_a_design_md_that_stays_poisoned_fails_the_role_by_name(self) -> None:
        bad = {"applies": True, "artifact": "web", "design_md": "x \udc80"}
        self.reply("design", bad, bad)

        with self.assertRaises(AgentOutputInvalid):
            await self.executor.orchestrator.run_agent("design", self.goal.id, None, "design it")
        self.assertEqual(2, len(self.provider.prompts["design"]))

    async def test_the_rest_of_the_designers_reply_is_still_healed(self) -> None:
        self.reply("design", {
            "applies": True, "artifact": "web", "design_md": "# Title\n\nbody", "direction": "calm \ud800 and plain",
        })

        out = await self.executor.orchestrator.run_agent("design", self.goal.id, None, "design it")

        self.assertEqual(f"calm {FFFD} and plain", out["direction"])
        self.assertEqual(1, len(self.provider.prompts["design"]))


class TestRowsAlreadyInADatabase(SinkCase):
    def poison_a_step(self) -> None:
        """A step as `_insert_steps` used to store it: `json.dumps` wrote the lone surrogate as ASCII."""
        self.conn.execute(
            "INSERT INTO plan_steps (id, goal_id, ordinal, title, description, suggested_paths, status) "
            "VALUES (?, ?, 0, 'Rename', 'rename', ?, 'PENDING')",
            (str(uuid.uuid4()), self.goal.id, json.dumps(["a\ud800.py", "ok.py"])),
        )
        self.conn.commit()

    async def test_a_stored_suggested_path_holding_the_escape_is_healed_on_read(self) -> None:
        self.poison_a_step()

        [step] = self.goals.steps(self.goal.id)

        self.assertEqual([f"a{FFFD}.py", "ok.py"], step.suggested_paths)
        step.model_dump_json()

    async def test_the_goal_route_answers_200_for_a_goal_holding_such_a_row(self) -> None:
        self.poison_a_step()
        app.state.goals = self.goals
        app.state.token = BOOT_TOKEN

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
            headers={"Authorization": f"Bearer {BOOT_TOKEN}"},
        ) as client:
            res = await client.get(f"/goals/{self.goal.id}")

        self.assertEqual(200, res.status_code, res.text[:300])
        self.assertEqual([f"a{FFFD}.py", "ok.py"], res.json()["steps"][0]["suggested_paths"])

    async def test_a_running_step_of_such_a_goal_sends_a_prompt_the_fixer_can_be_asked(self) -> None:
        self.poison_a_step()
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")
        step = self.goals.steps(self.goal.id)[0]

        await self.executor.run_step(self.goal.id, step.id)

        self.assertEqual([], self.events("error"))
        self.assertEqual("COMPLETED", self.goals.steps(self.goal.id)[0].status)


class TestTheFixerStaysStrict(SinkCase):
    """The fixer's text becomes the person's files: refused and asked once, never healed (invariant 9's neighbour)."""

    async def assert_asked_once_more_then_failed_with_nothing_written(self, bad: Any) -> None:
        self.reply("fixer", bad)

        status = await self.run_the_step()

        self.assertEqual("FAILED", status)
        self.assertEqual(2, len(self.provider.prompts["fixer"]), "asked again exactly once")
        self.assertEqual("agent_output_invalid", self.events("error")[-1]["code"])
        self.assertEqual("fixer", self.events("error")[-1]["role"])
        self.assertEqual(SOURCE, self.file, "a refused reply wrote something")
        self.assertEqual(["a.py", "t.db"], sorted(p.name for p in self.root.iterdir() if not p.name.startswith("t.db-")))
        self.assertEqual([], [w for w in self.warnings() if "lone surrogate" in w], "the fixer's reply was healed")

    async def test_content_with_a_lone_surrogate_is_refused(self) -> None:
        await self.assert_asked_once_more_then_failed_with_nothing_written(
            {"files": [{"path": "a.py", "action": "update", "content": "x\ud800y"}]}
        )

    async def test_a_path_with_a_lone_surrogate_is_refused(self) -> None:
        await self.assert_asked_once_more_then_failed_with_nothing_written(
            {"files": [{"path": "b\udc80.py", "action": "create", "content": "x\n"}]}
        )

    async def test_the_new_text_of_an_edit_with_a_lone_surrogate_is_refused(self) -> None:
        await self.assert_asked_once_more_then_failed_with_nothing_written(
            {"files": [{"path": "a.py", "action": "edit",
                        "edits": [{"old_text": "greet", "new_text": "wel\ud800come", "count": 0}]}]}
        )

    async def test_the_roles_that_are_not_healed_are_exactly_the_fixer(self) -> None:
        self.assertEqual(frozenset({"fixer"}), VERBATIM_ROLES)

    async def test_every_other_role_is_healed_and_the_fixer_is_handed_back_as_it_came(self) -> None:
        orchestrator: AgentOrchestrator = self.executor.orchestrator
        for role in ROLES:
            with self.subTest(role):
                reply = {"files": [{"path": "a\ud800", "content": ["x\udc80"]}]}

                healed = orchestrator._healed(role, self.goal.id, None, reply)

                if role in VERBATIM_ROLES:
                    self.assertIs(reply, healed)
                else:
                    self.assertEqual({"files": [{"path": f"a{FFFD}", "content": [f"x{FFFD}"]}]}, healed)
                    json.dumps(healed, ensure_ascii=False).encode("utf-8")
