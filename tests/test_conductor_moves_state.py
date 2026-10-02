"""What the conductor's moves say must be true, and what a step remembers between them must be too.

The moves are the pipeline's own stages behind a tool menu, and each one used to say something the engine did
not know: `plan` returned a goal status of FAILED with an empty step list as a success; a second `write` on a
step replaced the first's files, so the commit missed them and the critic reviewed half the change; a `write`
after a `review` left the old approval standing, so `summarize` committed work nobody had looked at; a failed
`verify` could still be reviewed; `review` threw away the critic's reasons and then the next `write` was
refused as "not approved" when the goal had been paused by the critic itself; and `summarize` marked a step
COMPLETED whether or not the scribe had committed anything.

These drive the real moves, the real fixer and a real git repository, through the dispatch table the loop
calls, and assert on the files, the commits and the step rows, not on what the model was told.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest import mock

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.conductor_tools import ConductorTools
from engine.skills import load_skills
from tests.test_fixer_reask import SOURCE, ReAskCase, ScriptedProvider

A_V2 = {"files": [{"path": "a.py", "action": "update", "content": "a = 2\n"}]}
B_V1 = {"files": [{"path": "b.py", "action": "update", "content": "b = 1\n"}]}
A_V3 = {"files": [{"path": "a.py", "action": "update", "content": "a = 3\n"}]}
FAIL = {"argv": None, "verdict": "fail", "explanation": "the tests are red"}


def _git(root: Any, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root, capture_output=True, text=True, check=False,
    )
    return done.stdout


class _Counting(ScriptedProvider):
    """The scripted provider, counting how often each role is asked."""

    def __init__(self, fixer_replies: list[Any]) -> None:
        super().__init__(fixer_replies)
        self.asked: dict[str, int] = {}

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> str:
        for role in self.roles:
            if f"You are Codify {role.capitalize()}" in system_prompt:
                self.asked[role] = self.asked.get(role, 0) + 1
        return await super().complete(
            system_prompt, user_prompt, model, temperature, max_tokens,
            num_ctx=num_ctx, keep_alive=keep_alive,
        )


class MovesCase(ReAskCase):
    """An approved goal with one step over a.py and b.py, in a real repository."""

    def build(self, *fixer_replies: Any) -> _Counting:
        provider = _Counting(list(fixer_replies))
        self.provider = provider
        from engine.executor import ExecutorService
        from engine.models import ROLES, AgentConfigUpdate
        from engine.providers import Keychain
        from engine.sandbox import SandboxService
        from engine.services import AgentRegistryService
        from tests.test_fixer_reask import Factory, Gate

        registry = AgentRegistryService(self.conn, Factory(provider), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(model_name="test-model"))
        self.executor = ExecutorService(self.goals, self.workspaces, registry, SandboxService(), laya=Gate())
        return provider

    def repo(self) -> None:
        (self.root / "b.py").write_text("b = 0\n", encoding="utf-8")
        _git(self.root, "init", "-q")
        _git(self.root, "add", "a.py", "b.py")
        _git(self.root, "commit", "-qm", "first")

    def approve(self, *, dry_run: bool = False) -> str:
        if dry_run:
            self.conn.execute("UPDATE goals SET dry_run=1 WHERE id=?", (self.goal.id,))
            self.conn.commit()
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")
        self.executor._insert_steps(
            self.goal.id,
            [{"title": "Change", "description": "change a.py and b.py", "suggested_paths": ["a.py", "b.py"]}],
        )
        return self.goals.steps(self.goal.id)[0].id

    def table(self) -> dict[str, Any]:
        goal = self.goals.get(self.goal.id)
        return self.executor._conductor_dispatch(
            goal.id, goal, str(self.root), load_skills(str(self.root)),
        )

    def step_status(self) -> str:
        return self.goals.steps(self.goal.id)[0].status

    def committed_files(self) -> set[str]:
        return {line for line in _git(self.root, "show", "--name-only", "--format=", "HEAD").split() if line}


class TestAFailedPlanLeavesTheGoalAlive(MovesCase):
    async def test_the_move_says_planning_failed_and_the_goal_is_not_failed(self) -> None:
        provider = self.build(A_V2)
        provider.roles["planner"] = {"steps": []}
        table = self.table()
        await table["recon"]({"task": "look"})
        before = self.goals.get(self.goal.id).status

        out = await table["plan"]({"task": "change it"})

        self.assertTrue(out.startswith("Planning failed"), out)
        self.assertEqual(before, self.goals.get(self.goal.id).status, "a failed plan must not fail the goal")
        self.assertEqual([], self.goals.steps(self.goal.id))
        self.assertEqual([], self.events("error"), "the goal was reported as failed")

    async def test_the_conductor_may_plan_again_after_a_failed_plan(self) -> None:
        provider = self.build(A_V2)
        provider.roles["planner"] = {"steps": []}
        table = self.table()
        await table["recon"]({"task": "look"})
        await table["plan"]({"task": "change it"})
        provider.roles["planner"] = {
            "steps": [{"title": "S1", "description": "d", "suggested_paths": ["a.py"]}],
        }

        out = await table["plan"]({"task": "change it, simply"})

        self.assertEqual(["S1"], [s["title"] for s in json.loads(out)["steps"]])

    async def test_the_pipelines_own_planning_still_fails_the_goal(self) -> None:
        # The control: only the conductor's door changed. The recipe's planner failing is still a failed goal.
        provider = self.build(A_V2)
        provider.roles["planner"] = {"steps": []}

        await self.executor.run_planning(self.goal.id)

        self.assertEqual("FAILED", self.goals.get(self.goal.id).status)


class TestTheStepRemembersEverythingWrittenForIt(MovesCase):
    async def test_two_writes_are_both_verified_reviewed_and_committed(self) -> None:
        self.build(A_V2, B_V1)
        self.repo()
        step_id = self.approve()
        table = self.table()

        await table["write"]({"step_id": step_id, "instructions": "change a"})
        await table["write"]({"step_id": step_id, "instructions": "change b"})
        await table["verify"]({"step_id": step_id})
        await table["review"]({"step_id": step_id})
        out = await table["summarize"]({"step_id": step_id})

        self.assertEqual({"a.py", "b.py"}, self.committed_files(), f"the commit missed a write: {out}")
        self.assertEqual("COMPLETED", self.step_status())

    async def test_the_critic_is_shown_every_file_the_step_wrote(self) -> None:
        provider = self.build(A_V2, B_V1)
        step_id = self.approve()
        table = self.table()
        seen: list[str] = []
        original = self.executor._critic

        async def spy(goal_id: str, step: Any, fs: Any, diffs: list[dict[str, Any]], *a: Any, **k: Any) -> None:
            seen.extend(d["path"] for d in diffs)
            await original(goal_id, step, fs, diffs, *a, **k)

        await table["write"]({"step_id": step_id, "instructions": "a"})
        await table["write"]({"step_id": step_id, "instructions": "b"})
        await table["verify"]({"step_id": step_id})
        with mock.patch.object(self.executor, "_critic", spy):
            await table["review"]({"step_id": step_id})

        self.assertEqual(["a.py", "b.py"], sorted(seen))
        self.assertEqual(1, provider.asked["critic"])

    async def test_writing_the_same_file_twice_keeps_one_entry_and_the_latest_content(self) -> None:
        self.build(A_V2, A_V3)
        self.repo()
        step_id = self.approve()
        table = self.table()

        await table["write"]({"step_id": step_id, "instructions": "first"})
        await table["write"]({"step_id": step_id, "instructions": "second"})
        await table["verify"]({"step_id": step_id})
        await table["review"]({"step_id": step_id})
        await table["summarize"]({"step_id": step_id})

        self.assertEqual({"a.py"}, self.committed_files())
        self.assertEqual("a = 3\n", _git(self.root, "show", "HEAD:a.py"))


    async def test_a_second_write_that_changes_nothing_does_not_erase_the_first_ones_diff(self) -> None:
        # The fixer proposes the content a.py already has. That says nothing changed *this time*; the critic
        # must still be shown the write that did change it.
        self.build(A_V2, A_V2)
        step_id = self.approve()
        table = self.table()
        seen: list[dict[str, Any]] = []
        original = self.executor._critic

        async def spy(goal_id: str, step: Any, fs: Any, diffs: list[dict[str, Any]], *a: Any, **k: Any) -> None:
            seen.extend(diffs)
            await original(goal_id, step, fs, diffs, *a, **k)

        await table["write"]({"step_id": step_id, "instructions": "first"})
        await table["write"]({"step_id": step_id, "instructions": "again, same"})
        await table["verify"]({"step_id": step_id})
        with mock.patch.object(self.executor, "_critic", spy):
            await table["review"]({"step_id": step_id})

        self.assertEqual(1, len(seen))
        self.assertTrue(seen[0].get("changed", True))
        self.assertIn("+a = 2", seen[0]["unified_diff"])

    async def test_the_scribe_stages_each_path_once_even_if_it_is_handed_it_twice(self) -> None:
        self.build(A_V2)
        self.repo()
        step_id = self.approve()
        step = self.goals.steps(self.goal.id)[0]
        entry = {"path": "a.py", "action": "update", "unified_diff": "+a = 2", "changed": True}
        staged: list[list[str]] = []

        def commit(root: str, message: str, paths: list[str], *a: Any, **k: Any) -> None:
            staged.append(list(paths))

        with mock.patch.object(self.executor.git, "commit", commit):
            await self.executor._scribe(self.goal.id, step, [entry, dict(entry)], str(self.root), False, None)

        self.assertEqual([["a.py"]], staged)
        self.assertEqual(step_id, step.id)


class TestAWriteInvalidatesWhatWasKnownBeforeIt(MovesCase):
    async def test_a_write_after_a_review_cannot_be_summarized_on_the_old_approval(self) -> None:
        self.build(A_V2, A_V3)
        self.repo()
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "first"})
        await table["verify"]({"step_id": step_id})
        await table["review"]({"step_id": step_id})

        await table["write"]({"step_id": step_id, "instructions": "second, unreviewed"})
        out = await table["summarize"]({"step_id": step_id})

        self.assertIn("not been reviewed", out)
        self.assertNotEqual("COMPLETED", self.step_status())
        self.assertEqual("first", _git(self.root, "log", "-1", "--format=%s").strip(), "an unreviewed change was committed")

    async def test_a_write_after_a_verify_needs_a_fresh_verdict_before_review(self) -> None:
        provider = self.build(A_V2, A_V3)
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "first"})
        await table["verify"]({"step_id": step_id})
        await table["write"]({"step_id": step_id, "instructions": "second"})

        out = await table["review"]({"step_id": step_id})

        self.assertIn("verify", out)
        self.assertNotIn("critic", provider.asked, "the critic judged a change nobody had verified")


class TestAFailedVerificationIsNotReviewable(MovesCase):
    async def test_review_is_refused_until_the_failure_is_taken_back_to_write(self) -> None:
        provider = self.build(A_V2, A_V3)
        provider.roles["verifier"] = FAIL
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "first"})
        failed = await table["verify"]({"step_id": step_id})

        out = await table["review"]({"step_id": step_id})

        self.assertIn("FAILED", failed)
        self.assertIn("failed", out)
        self.assertIn("write", out)
        self.assertNotIn("critic", provider.asked)

    async def test_a_later_pass_makes_it_reviewable_again(self) -> None:
        provider = self.build(A_V2, A_V3)
        provider.roles["verifier"] = FAIL
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "first"})
        await table["verify"]({"step_id": step_id})
        provider.roles["verifier"] = {"argv": None, "verdict": "pass", "explanation": "green"}
        await table["write"]({"step_id": step_id, "instructions": "fix it"})
        await table["verify"]({"step_id": step_id})

        out = await table["review"]({"step_id": step_id})

        self.assertIn("approved", out)
        self.assertEqual(1, provider.asked["critic"])


class TestACompletedStepStaysCompleted(MovesCase):
    async def test_a_write_to_a_completed_step_is_refused_and_changes_nothing(self) -> None:
        self.build(A_V2, A_V3)
        self.repo()
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "first"})
        await table["verify"]({"step_id": step_id})
        await table["review"]({"step_id": step_id})
        await table["summarize"]({"step_id": step_id})
        on_disk = (self.root / "a.py").read_text(encoding="utf-8")

        out = await table["write"]({"step_id": step_id, "instructions": "again"})

        self.assertIn("already complete", out)
        self.assertEqual(on_disk, (self.root / "a.py").read_text(encoding="utf-8"))


class TestADryRunKeepsItsFirstProposal(MovesCase):
    async def test_a_second_write_is_refused_because_it_would_replace_the_first_proposal(self) -> None:
        self.build(A_V2, B_V1)
        step_id = self.approve(dry_run=True)
        table = self.table()
        first = await table["write"]({"step_id": step_id, "instructions": "a"})

        second = await table["write"]({"step_id": step_id, "instructions": "b"})

        self.assertEqual(["a.py"], [c["path"] for c in json.loads(first)["changed"]])
        # The refusal, not a second success: a successful dry-run write also says "dry run" in its note.
        self.assertIn("one stored proposal per step", second)
        self.assertNotIn('"changed"', second)
        # And the first proposal is still what Apply would replay, with nothing from the refused write.
        self.assertEqual("a = 2\n", self.goals.proposed_content(self.goal.id, step_id, "a.py"))
        self.assertIsNone(self.goals.proposed_content(self.goal.id, step_id, "b.py"))
        self.assertEqual(SOURCE, (self.root / "a.py").read_text(encoding="utf-8"), "a dry run wrote a file")


class TestTheCriticsReasonsReachTheConductor(MovesCase):
    REASONS = ["a.py: no docstring on the new function", "b.py: the name does not match the style"]

    async def _reject(self) -> tuple[Any, str, str]:
        provider = self.build(A_V2, A_V3)
        provider.roles["critic"] = {"decision": "request-changes", "reasons": self.REASONS}
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "first"})
        await table["verify"]({"step_id": step_id})
        return table, step_id, await table["review"]({"step_id": step_id})

    async def test_every_reason_is_in_the_result(self) -> None:
        _, _, out = await self._reject()

        for reason in self.REASONS:
            self.assertIn(reason, out)

    async def test_the_result_says_the_goal_is_paused_and_who_resumes_it(self) -> None:
        _, _, out = await self._reject()

        self.assertEqual("PAUSED", self.goals.get(self.goal.id).status)
        self.assertIn("paused", out)
        self.assertIn("Start", out)

    async def test_a_write_after_the_pause_is_refused_as_paused_not_as_unapproved(self) -> None:
        table, step_id, _ = await self._reject()

        out = await table["write"]({"step_id": step_id, "instructions": "act on it"})

        self.assertIn("Nothing was written", out)
        self.assertIn("paused", out)
        self.assertIn("Start", out)
        self.assertNotIn("has not been approved", out, "the goal was approved; the critic paused it")


class TestWhyAWriteWasRefused(MovesCase):
    async def test_a_goal_nobody_has_started_says_the_plan_is_not_approved(self) -> None:
        self.build(A_V2)
        self.executor._insert_steps(
            self.goal.id, [{"title": "Change", "description": "d", "suggested_paths": ["a.py"]}],
        )
        step_id = self.goals.steps(self.goal.id)[0].id

        out = await self.table()["write"]({"step_id": step_id, "instructions": "go"})

        self.assertIn("has not been approved", out)

    async def test_a_finished_or_failed_goal_says_so(self) -> None:
        self.build(A_V2)
        step_id = self.approve()
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "FAILED")

        out = await self.table()["write"]({"step_id": step_id, "instructions": "go"})

        self.assertIn("Nothing was written", out)
        self.assertIn("failed", out)
        self.assertNotIn("has not been approved", out)


class TestSummarizeReportsWhatTheScribeDid(MovesCase):
    async def _through_review(self) -> tuple[dict[str, Any], str]:
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "first"})
        await table["verify"]({"step_id": step_id})
        await table["review"]({"step_id": step_id})
        return table, step_id

    async def test_a_commit_is_reported_as_a_commit(self) -> None:
        self.build(A_V2)
        self.repo()
        table, step_id = await self._through_review()

        out = await table["summarize"]({"step_id": step_id})

        self.assertIn("committed", out)
        self.assertEqual("COMPLETED", self.step_status())

    async def test_a_folder_that_is_not_a_repository_says_nothing_was_committed(self) -> None:
        self.build(A_V2)
        table, step_id = await self._through_review()

        out = await table["summarize"]({"step_id": step_id})

        self.assertIn("not a git repository", out)
        self.assertNotIn("committed.", out)
        self.assertEqual("COMPLETED", self.step_status(), "the change is on disk, so the step is done")

    async def test_a_cancelled_scribe_does_not_complete_the_step(self) -> None:
        self.build(A_V2)
        self.repo()
        table, step_id = await self._through_review()

        async def cancelled(*args: Any, **kwargs: Any) -> str:
            return "cancelled"

        with mock.patch.object(self.executor, "_scribe", cancelled):
            out = await table["summarize"]({"step_id": step_id})

        self.assertIn("cancelled", out)
        self.assertNotEqual("COMPLETED", self.step_status())


class TestWhatTheFixerAndPlannerSaidIsNotDropped(MovesCase):
    async def test_a_fixer_that_wants_another_pass_is_told_to_the_conductor(self) -> None:
        unfinished = dict(A_V2, needs_another_pass=True)
        self.build(unfinished)
        step_id = self.approve()

        out = await self.table()["write"]({"step_id": step_id, "instructions": "first part"})

        payload = json.loads(out)
        self.assertTrue(payload["needs_another_pass"], out)
        self.assertIn("write", payload["note"])
        self.assertNotIn("Call `verify` next", payload["note"], "it must not be sent to verify half-written work")

    async def test_a_write_shows_the_conductor_what_changed(self) -> None:
        self.build(A_V2)
        step_id = self.approve()

        out = await self.table()["write"]({"step_id": step_id, "instructions": "go"})

        changed = json.loads(out)["changed"]
        self.assertIn("+a = 2", changed[0]["diff"])

    async def test_a_plan_says_what_each_step_is_and_where(self) -> None:
        self.build(A_V2)
        table = self.table()
        await table["recon"]({"task": "look"})

        out = await table["plan"]({"task": "rename"})

        step = json.loads(out)["steps"][0]
        self.assertEqual("rename greet", step["description"])
        self.assertEqual(["a.py"], step["suggested_paths"])


class TestACompletedTurnNeverOverwritesATerminalStatus(MovesCase):
    async def test_a_goal_that_failed_during_the_run_stays_failed_when_the_conductor_stops(self) -> None:
        from engine.toolcall import ToolReply
        from tests.test_conductor import _call, _ToolProvider

        tool_provider = _ToolProvider([
            ToolReply(tool_calls=[_call("read_file", path="a.py")]),
            ToolReply(text="All done."),
        ])
        executor = self._turn_executor(tool_provider)

        async def reads_and_fails(self_: Any, args: dict[str, Any]) -> str:
            executor._fail(self.goal.id, None, "provider_error", "the endpoint went away")
            return "partial"

        with mock.patch.object(ConductorTools, "read_file", reads_and_fails):
            await executor.run_chat(self.goal.id)

        self.assertEqual("FAILED", self.goals.get(self.goal.id).status)

    def _turn_executor(self, provider: Any) -> Any:
        from engine.executor import ExecutorService
        from engine.laya import LayaDecision, LayaService
        from engine.models import ROLES, AgentConfigUpdate
        from engine.providers import Keychain
        from engine.sandbox import SandboxService
        from engine.services import AgentRegistryService
        from tests.test_conductor import _Factory

        class _Question(LayaService):
            async def decide(self, state: dict[str, Any], on_call: Any = None) -> LayaDecision:
                return LayaDecision(
                    engine="sdk", answers={"intent": {"choice": "question", "confidence": 0.99}},
                )

        registry = AgentRegistryService(self.conn, _Factory(provider), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(model_name="stub-model"))
        return ExecutorService(self.goals, self.workspaces, registry, SandboxService(), laya=_Question())
