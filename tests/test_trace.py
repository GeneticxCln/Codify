from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from engine.app import app
from engine.db import connect
from engine.executor import ExecutorService
from engine.models import (
    ROLES,
    AgentConfig,
    AgentConfigUpdate,
    GoalCreate,
    Workspace,
    WorkspaceCreate,
)
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, ApiError, GoalService, WorkspaceService
from engine.trace import ReplayProvider, TraceMismatch, TraceService, keep_prompts, prompt_digest

from tests.test_design_role import SkippedGate  # noqa: F401 — the gate is its own concern

RESPONSES: dict[str, Any] = {
    "librarian": {"summary": "s", "files": [], "conventions": [], "enough": True},
    "design": {"applies": False},
    "planner": {"steps": [{"title": "S1", "description": "d", "suggested_paths": ["a.txt"]}]},
    "fixer": {"files": [{"path": "a.txt", "action": "create", "content": "hi\n"}]},
    "verifier": {"argv": None, "verdict": "pass", "explanation": "ok"},
    "critic": {"decision": "approve", "reasons": []},
    "scribe": {"summary": "did", "commit_message": "feat: a"},
}


class ScriptedProvider(BaseProvider):
    """Answers by role and reports usage, so a recording has something to keep."""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.role: str | None = None
        self.prompts: list[tuple[str, str, str]] = []

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
        self.prompts.append((self.role or "?", system_prompt, user_prompt))
        if self.usage_sink is not None:
            self.usage_sink({"input_tokens": 11, "output_tokens": 7, "total_tokens": 18})
        return json.dumps(self.responses.get(self.role or "", {}))


class ScriptedFactory(ProviderFactory):
    def __init__(self, provider: ScriptedProvider, keychain: Keychain) -> None:
        super().__init__(keychain)
        self.provider = provider

    def build(self, config: AgentConfig) -> BaseProvider:
        self.provider.role = config.role
        return self.provider


class ReplayFactory(ProviderFactory):
    """Serves one goal's recording, the way a benchmark wires the engine."""

    def __init__(self, provider: ReplayProvider, keychain: Keychain) -> None:
        super().__init__(keychain)
        self.provider = provider

    def build(self, config: AgentConfig) -> BaseProvider:
        self.provider.current_role = config.role
        return self.provider


class TraceHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name).resolve()
        # The database lives *beside* the workspace, not inside it. A run writes
        # into the workspace it is pointed at, and a replay has to be handed a
        # workspace that looks exactly like the one the recording saw; a `t.db`
        # in the tree would both leak the engine's own state into the librarian's
        # evidence pack and change the tree between the two runs.
        self.conn = connect(self.home / "t.db")
        self.traces = TraceService(self.conn)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.ws = self._workspace("ws", "WS")

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _workspace(self, folder: str, name: str) -> Workspace:
        """A workspace with one committed file, so the tree is not empty."""
        root = self.home / folder
        root.mkdir()
        (root / "README.md").write_text("# fixture\n")
        return self.workspaces.create(WorkspaceCreate(name=name, root_path=str(root)))

    def _clone_of(self, workspace: Workspace, folder: str = "ws-clone") -> Workspace:
        """A byte-identical copy of a workspace, taken while it is untouched.

        This is what makes a replay possible at all: the run being replayed has
        to start from the tree the recording started from, or the prompts differ
        and the replay correctly refuses. It is also what a benchmark does — run
        the task, restore the vendored snapshot, run it again.
        """
        target = self.home / folder
        shutil.copytree(workspace.root_path, target)
        return self.workspaces.create(
            WorkspaceCreate(name=workspace.name, root_path=str(target))
        )

    def _engine(self, provider: ScriptedProvider, *, tracer: bool = True) -> ExecutorService:
        registry = AgentRegistryService(
            self.conn, ScriptedFactory(provider, Keychain()), Keychain()
        )
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(provider="ollama", model_name="m"))
        return ExecutorService(
            self.goals, self.workspaces, registry, SandboxService(),
            laya=SkippedGate(), tracer=self.traces if tracer else None,
        )

    def _replay_engine(self, goal_id: str) -> tuple[ExecutorService, ReplayProvider]:
        provider = self.traces.replay_provider(goal_id)
        registry = AgentRegistryService(
            self.conn, ReplayFactory(provider, Keychain()), Keychain()
        )
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(provider="ollama", model_name="m"))
        executor = ExecutorService(
            self.goals, self.workspaces, registry, SandboxService(),
            laya=SkippedGate(), tracer=self.traces,
        )
        return executor, provider

    async def _run(self, executor: ExecutorService, goal_id: str) -> None:
        await executor.run_planning(goal_id)
        step = self.goals.steps(goal_id)[0]
        self.goals.update_status(goal_id, self.goals.get(goal_id).version, "RUNNING")
        await executor.run_step(goal_id, step.id)


class DigestCase(unittest.TestCase):
    def test_the_digest_separates_its_two_halves(self) -> None:
        """A digest of two prompts that concatenate identically would match a
        replay of a different call."""
        self.assertNotEqual(
            prompt_digest("ab", "c"), prompt_digest("a", "bc"),
            "the separator has to be a byte neither prompt can contain",
        )

    def test_the_digest_is_stable_and_opaque(self) -> None:
        digest = prompt_digest("system", "user")
        self.assertEqual(digest, prompt_digest("system", "user"))
        self.assertEqual(len(digest), 32)
        self.assertNotIn("system", digest)

    def test_prompts_are_kept_only_when_asked(self) -> None:
        original = os.environ.get("CODIFY_TRACE_PROMPTS")
        try:
            os.environ.pop("CODIFY_TRACE_PROMPTS", None)
            self.assertFalse(keep_prompts())
            os.environ["CODIFY_TRACE_PROMPTS"] = "0"
            self.assertFalse(keep_prompts(), "an explicit 0 is not a yes")
            os.environ["CODIFY_TRACE_PROMPTS"] = "1"
            self.assertTrue(keep_prompts())
        finally:
            if original is None:
                os.environ.pop("CODIFY_TRACE_PROMPTS", None)
            else:
                os.environ["CODIFY_TRACE_PROMPTS"] = original


class RecordingCase(TraceHarness):
    async def test_a_goal_that_did_not_ask_records_nothing(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="")
        )
        provider = ScriptedProvider(dict(RESPONSES))
        await self._run(self._engine(provider), goal.id)
        self.assertEqual(self.traces.count(goal.id), 0)
        self.assertTrue(provider.prompts, "the run still happened")

    async def test_a_goal_that_asked_records_every_call_with_its_usage(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        provider = ScriptedProvider(dict(RESPONSES))
        await self._run(self._engine(provider), goal.id)

        calls = self.traces.calls(goal.id)
        self.assertTrue(calls)
        self.assertEqual(
            [c["seq"] for c in calls], list(range(1, len(calls) + 1)),
            "order is part of what a replay is",
        )
        roles = [c["role"] for c in calls]
        for expected in ("librarian", "design", "planner", "fixer", "verifier", "critic", "scribe"):
            self.assertIn(expected, roles)
        first = calls[0]
        self.assertEqual(first["input_tokens"], 11)
        self.assertEqual(first["output_tokens"], 7)
        self.assertTrue(first["response"])
        self.assertIsNotNone(first["duration_ms"])

    async def test_the_prompt_is_kept_as_a_digest_not_as_text(self) -> None:
        """The prompt holds the goal, the evidence pack and the user's own
        source. The digest is what a replay needs; the text is opt-in."""
        original = os.environ.get("CODIFY_TRACE_PROMPTS")
        os.environ.pop("CODIFY_TRACE_PROMPTS", None)
        try:
            goal = self.goals.create(
                GoalCreate(workspace_id=self.ws.id, title="secret title", description="",
                           trace=True)
            )
            await self._run(self._engine(ScriptedProvider(dict(RESPONSES))), goal.id)
            calls = self.traces.calls(goal.id)
            self.assertTrue(calls)
            for call in calls:
                self.assertIsNone(call["user_prompt"])
                self.assertIsNone(call["system_prompt"])
                self.assertEqual(len(call["prompt_hash"]), 32)
            # Nothing anywhere in the row leaks the prompt it was built from.
            self.assertNotIn(
                "secret title", " ".join(str(c["response"] or "") for c in calls)
            )
            summary = self.traces.summary(goal.id)
            self.assertFalse(summary["prompts_kept"], "the UI must be able to say so")
        finally:
            if original is not None:
                os.environ["CODIFY_TRACE_PROMPTS"] = original

    async def test_an_engine_without_a_tracer_records_nothing(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        provider = ScriptedProvider(dict(RESPONSES))
        await self._run(self._engine(provider, tracer=False), goal.id)
        self.assertEqual(self.traces.count(goal.id), 0)

    async def test_switching_tracing_off_stops_the_recording(self) -> None:
        """Asked per call, not once per run: a user who notices a problem
        mid-planning can stop without it being a lie about the rest."""
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        self.traces.record(
            goal.id, None, role="planner", provider="ollama", model="m",
            temperature=0.2, max_tokens=10, system_prompt="s", user_prompt="u",
            response="{}",
        )
        self.goals.set_trace(goal.id, False)
        self.assertFalse(self.traces.enabled(goal.id))
        before = self.traces.count(goal.id)
        await self._run(self._engine(ScriptedProvider(dict(RESPONSES))), goal.id)
        self.assertEqual(self.traces.count(goal.id), before)


class ReplayCase(TraceHarness):
    async def _record(
        self, workspace: Workspace | None = None, **goal_kwargs: Any
    ) -> tuple[str, list[str]]:
        goal = self.goals.create(
            GoalCreate(
                workspace_id=(workspace or self.ws).id, title="t", description="",
                **goal_kwargs,
            )
        )
        provider = ScriptedProvider(dict(RESPONSES))
        await self._run(self._engine(provider), goal.id)
        return goal.id, [c["role"] for c in self.traces.calls(goal.id)]

    async def test_a_replayed_goal_takes_the_same_stages(self) -> None:
        """The whole point of the recording: a second goal, on an untouched
        copy of the same tree, is answered the same way and so lands in the
        same place — with no provider in the loop at all."""
        clone = self._clone_of(self.ws)
        recorded_id, _roles = await self._record(trace=True)
        replay_goal = self.goals.create(
            GoalCreate(workspace_id=clone.id, title="t", description="", trace=False)
        )
        executor, provider = self._replay_engine(recorded_id)
        await self._run(executor, replay_goal.id)

        def stages(goal_id: str) -> list[tuple[str, str]]:
            return [
                (str(e.payload.get("stage")), str(e.payload.get("outcome")))
                for e in self.goals.events_after(goal_id, 0) if e.type == "stage_result"
            ]

        self.assertEqual(stages(recorded_id), stages(replay_goal.id))
        self.assertEqual(
            [s.title for s in self.goals.steps(replay_goal.id)], ["S1"]
        )
        self.assertTrue(provider.served, "the replay actually served the recording")

    async def test_a_replay_of_a_different_prompt_refuses_instead_of_lying(self) -> None:
        """The recorded reply would be answering a different question. Serving
        it anyway produces a green run that proves nothing — worse than none."""
        clone = self._clone_of(self.ws)
        recorded_id, _roles = await self._record(trace=True)
        other = self.goals.create(
            GoalCreate(workspace_id=clone.id, title="a different title entirely",
                       description="different", trace=True)
        )
        executor, _provider = self._replay_engine(recorded_id)
        await executor.run_planning(other.id)
        self.assertEqual(self.goals.get(other.id).status, "FAILED")

    async def test_a_workspace_the_recording_never_saw_also_refuses(self) -> None:
        """The other half of the guard. Even with the same goal, a tree that has
        moved on makes the evidence pack a different document, so the reply
        would be answering a different question."""
        recorded_id, _roles = await self._record(trace=True)
        (Path(self.ws.root_path) / "a.txt").write_text("hi\n")
        executor, _provider = self._replay_engine(recorded_id)
        other = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="")
        )
        await executor.run_planning(other.id)
        self.assertEqual(self.goals.get(other.id).status, "FAILED")

    async def test_a_missing_call_says_which_role_asked_for_it(self) -> None:
        recorded_id, _roles = await self._record(trace=True)
        provider = self.traces.replay_provider(recorded_id)
        provider.current_role = "scribe"
        with self.assertRaises(TraceMismatch) as ctx:
            await provider.complete("a system prompt nobody recorded", "a user prompt", "m", 0.1, 8)
        self.assertIn("scribe", str(ctx.exception))
        self.assertEqual(ctx.exception.code, "trace_mismatch")

    async def test_a_role_the_recording_never_ran_is_named_as_such(self) -> None:
        recorded_id, _roles = await self._record(trace=True)
        provider = self.traces.replay_provider(recorded_id)
        provider.current_role = "librarian"
        # A digest the recording does not hold for that role at all.
        calls = self.traces.calls(recorded_id)
        with self.assertRaises(TraceMismatch) as ctx:
            await provider.complete("never", "seen", "m", 0.1, 8)
        message = str(ctx.exception)
        self.assertTrue(calls)
        self.assertIn("librarian", message)

    async def test_the_same_call_twice_is_served_twice(self) -> None:
        """Two identical calls in a run (a retry) are two recorded replies, and
        a replay that served the first one twice would hide the retry."""
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        self.traces.record(
            goal.id, None, role="planner", provider="ollama", model="m", temperature=0.2,
            max_tokens=10, system_prompt="s", user_prompt="u", response='{"steps":[]}',
        )
        self.traces.record(
            goal.id, None, role="planner", provider="ollama", model="m", temperature=0.2,
            max_tokens=10, system_prompt="s", user_prompt="u", response='{"steps":[]}',
        )
        provider = self.traces.replay_provider(goal.id)
        provider.current_role = "planner"
        self.assertEqual(
            await provider.complete("s", "u", "m", 0.2, 10), '{"steps":[]}'
        )


class DeletionCase(TraceHarness):
    async def test_deleting_is_the_users_call_and_is_idempotent(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        await self._run(self._engine(ScriptedProvider(dict(RESPONSES))), goal.id)
        held = self.traces.count(goal.id)
        self.assertGreater(held, 0)
        self.assertEqual(self.traces.delete(goal.id), held, "the count it says it removed")
        self.assertEqual(self.traces.count(goal.id), 0)
        self.assertEqual(self.traces.delete(goal.id), 0, "deleting twice is not an error")

    async def test_deleting_a_goal_deletes_its_recording(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        await self._run(self._engine(ScriptedProvider(dict(RESPONSES))), goal.id)
        self.assertTrue(self.traces.count(goal.id))
        self.goals.update_status(goal.id, self.goals.get(goal.id).version, "COMPLETED")
        self.goals.delete(goal.id)
        self.assertEqual(self.traces.count(goal.id), 0, "a recording outliving its goal")

    async def test_retention_prunes_by_age_and_zero_keeps_everything(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        self.traces.record(
            goal.id, None, role="planner", provider="ollama", model="m", temperature=0.2,
            max_tokens=10, system_prompt="s", user_prompt="u", response="{}",
        )
        self.assertEqual(self.traces.delete_older_than(0), 0)
        self.assertEqual(self.traces.count(goal.id), 1)
        self.assertEqual(self.traces.delete_older_than(1), 0, "nothing is a day old yet")


class RecordingFailureCase(TraceHarness):
    """A recording that could not be stored has to say so.

    Without this the two states a user can hold — "I never armed it" and "I
    armed it and the write failed" — are the same row of zeros, and the only
    one of them they can act on is the second."""

    def _break_writes(self) -> str:
        """Take the table away, returning its real DDL so the read side can be
        put back. The schema is read from the store rather than repeated here:
        a test carrying its own copy of the DDL would keep passing after the
        table changed shape."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'trace_calls'"
        ).fetchone()
        assert row is not None, "trace_calls exists before it is broken"
        self.conn.execute("DROP TABLE trace_calls")
        return str(row[0])

    def _restore(self, ddl: str) -> None:
        self.conn.execute(ddl)
        self.conn.commit()

    async def test_a_failed_write_is_reported_rather_than_indistinguishable(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        ddl = self._break_writes()
        self.traces.record(
            goal.id, None, role="planner", provider="ollama", model="m",
            temperature=0.2, max_tokens=10, system_prompt="s", user_prompt="u",
            response="{}",
        )
        self._restore(ddl)
        summary = self.traces.summary(goal.id)
        self.assertEqual(summary["calls"], 0)
        self.assertIsNotNone(summary["recording_error"], "an empty recording says why")
        self.assertIn("no such table", summary["recording_error"].lower())

    async def test_a_recording_that_worked_reports_no_error(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        await self._run(self._engine(ScriptedProvider(dict(RESPONSES))), goal.id)
        self.assertTrue(self.traces.count(goal.id))
        self.assertIsNone(
            self.traces.summary(goal.id)["recording_error"],
            "nothing went wrong, and the field says so plainly",
        )

    async def test_another_goals_failure_is_not_reported_on_this_one(self) -> None:
        """The error is keyed to the goal it happened on, or one broken run
        would explain every other run's empty summary forever."""
        healthy = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="ok", description="", trace=True)
        )
        self.traces.record(
            healthy.id, None, role="planner", provider="ollama", model="m",
            temperature=0.2, max_tokens=10, system_prompt="s", user_prompt="u",
            response="{}",
        )
        broken = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="a", description="", trace=True)
        )
        ddl = self._break_writes()
        self.traces.record(
            broken.id, None, role="planner", provider="ollama", model="m",
            temperature=0.2, max_tokens=10, system_prompt="s", user_prompt="u",
            response="{}",
        )
        self._restore(ddl)

        self.assertIsNotNone(self.traces.summary(broken.id)["recording_error"])
        self.assertIsNone(
            self.traces.summary(healthy.id)["recording_error"],
            "a healthy run does not inherit another run's failure",
        )


class TraceToggleCase(unittest.TestCase):
    def _goal(self, status: str) -> tuple[GoalService, str]:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name).resolve()
        conn = connect(root / "t.db")
        self.addCleanup(conn.close)
        goals = GoalService(conn)
        ws = WorkspaceService(conn).create(
            WorkspaceCreate(name="WS", root_path=str(root))
        )
        goal = goals.create(GoalCreate(workspace_id=ws.id, title="t", description=""))
        if status != "PLANNING":
            goals.update_status(goal.id, goals.get(goal.id).version, status)
        return goals, goal.id

    def test_recording_can_be_switched_on_while_a_goal_is_still_planning(self) -> None:
        goals, goal_id = self._goal("PLANNING")
        self.assertTrue(goals.set_trace(goal_id, True).trace)

    def test_recording_cannot_start_halfway_through_a_run(self) -> None:
        """A trace of half a run is not a replay of the run."""
        for status in ("RUNNING", "COMPLETED", "FAILED", "CANCELLED"):
            with self.subTest(status=status):
                goals, goal_id = self._goal(status)
                with self.assertRaises(ApiError) as ctx:
                    goals.set_trace(goal_id, True)
                self.assertEqual(ctx.exception.code, "trace_locked")

    def test_recording_can_always_be_stopped(self) -> None:
        """Stopping is not a state change, so no status may refuse it. A user
        who realises mid-run that they did not mean to record anything must not
        have to cancel the goal to stop it."""
        goals, goal_id = self._goal("PLANNING")
        self.assertTrue(goals.set_trace(goal_id, True).trace)
        for status in ("RUNNING", "COMPLETED", "FAILED", "CANCELLED"):
            with self.subTest(status=status):
                goals.update_status(goal_id, goals.get(goal_id).version, status)
                self.assertFalse(
                    goals.set_trace(goal_id, False).trace,
                    "a recording nobody can switch off is a leak",
                )

    def test_an_unknown_goal_is_a_404(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        goals = GoalService(connect(Path(temp_dir.name) / "t.db"))
        with self.assertRaises(ApiError) as ctx:
            goals.set_trace("no-such-goal", True)
        self.assertEqual(ctx.exception.status, 404)


class TraceApiCase(TraceHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        app.state.conn = self.conn
        app.state.goals = self.goals
        app.state.workspaces = self.workspaces
        app.state.traces = self.traces
        # The stats read is where recordings are pruned, so the screen it
        # happens on has to be fully wired or the sweep silently never runs.
        from engine.services import SettingsService
        from engine.stats_history import StatsSnapshotService
        from engine.stats_import import StatsImportService

        app.state.settings = SettingsService(self.conn)
        app.state.stats_snapshots = StatsSnapshotService(self.conn)
        app.state.stats_imports = StatsImportService(self.conn)
        from engine.app import BOOT_TOKEN

        self.token = BOOT_TOKEN

    def _client(self) -> Any:
        import httpx
        from httpx import ASGITransport

        return httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": f"Bearer {self.token}"},
        )

    async def test_the_api_reports_a_summary_and_deletes_on_request(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        await self._run(self._engine(ScriptedProvider(dict(RESPONSES))), goal.id)

        async with self._client() as client:
            got = await client.get(f"/goals/{goal.id}/trace")
            self.assertEqual(got.status_code, 200, got.text)
            body = got.json()
            self.assertGreater(body["calls"], 0)
            self.assertIn("planner", body["by_role"])
            self.assertFalse(body["prompts_kept"])

            gone = await client.delete(f"/goals/{goal.id}/trace")
            self.assertEqual(gone.status_code, 200, gone.text)
            self.assertGreater(gone.json()["deleted"], 0)
            after = await client.get(f"/goals/{goal.id}/trace")
            self.assertEqual(after.json()["calls"], 0)

    async def test_the_api_toggles_tracing_and_refuses_too_late(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="")
        )
        async with self._client() as client:
            on = await client.put(f"/goals/{goal.id}/trace", json={"enabled": True})
            self.assertEqual(on.status_code, 200, on.text)
            self.assertTrue(on.json()["trace"])

            self.goals.update_status(goal.id, self.goals.get(goal.id).version, "COMPLETED")
            late = await client.put(f"/goals/{goal.id}/trace", json={"enabled": True})
            self.assertEqual(late.status_code, 409, late.text)
            self.assertEqual(late.json()["code"], "trace_locked")

    async def test_the_recording_retention_setting_is_real_and_reachable(self) -> None:
        """A policy nobody can read or write is not a policy. Default 30 days,
        same band as snapshot retention, and the knob is on the same screen."""
        async with self._client() as client:
            got = await client.get("/settings/engine")
            self.assertEqual(got.status_code, 200, got.text)
            knob = got.json()["trace_retention_days"]
            self.assertEqual(knob["value"], 30, "bounded by default, not forever")
            self.assertEqual((knob["min"], knob["max"]), (0, 730))

            saved = await client.put(
                "/settings/engine", json={"trace_retention_days": 5}
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["saved"]["trace_retention_days"], 5)

            clamped = await client.put(
                "/settings/engine", json={"trace_retention_days": 999999}
            )
            self.assertEqual(clamped.json()["saved"]["trace_retention_days"], 730)

    async def test_a_recording_older_than_the_policy_is_pruned_on_the_stats_read(self) -> None:
        """Pruned where the history policy is enforced, rather than by a job
        that only exists if something schedules it — so lowering the window
        takes effect on the next read instead of tomorrow."""
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        await self._run(self._engine(ScriptedProvider(dict(RESPONSES))), goal.id)
        held = self.traces.count(goal.id)
        self.assertGreater(held, 0)
        # Backdate past the 30-day default.
        self.conn.execute(
            "UPDATE trace_calls SET created_at = ? WHERE goal_id = ?",
            (time.time() - 40 * 86400, goal.id),
        )
        self.conn.commit()
        self.assertEqual(self.traces.count(goal.id), held)

        async with self._client() as client:
            res = await client.get("/stats/overview?window=0")
            self.assertEqual(res.status_code, 200, res.text)

        self.assertEqual(
            self.traces.count(goal.id), 0, "older than the policy, so forgotten"
        )
        self.assertGreater(res.json()["goals"]["goals"], 0, "the read still worked")

    async def test_a_recording_inside_the_policy_survives_the_same_read(self) -> None:
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws.id, title="t", description="", trace=True)
        )
        await self._run(self._engine(ScriptedProvider(dict(RESPONSES))), goal.id)
        held = self.traces.count(goal.id)
        self.assertGreater(held, 0)
        async with self._client() as client:
            self.assertEqual(
                (await client.get("/stats/overview?window=0")).status_code, 200
            )
        self.assertEqual(self.traces.count(goal.id), held, "fresh, so kept")

    async def test_an_unknown_goal_is_a_404_on_every_trace_route(self) -> None:
        async with self._client() as client:
            for call in (
                client.get("/goals/no-such-goal/trace"),
                client.delete("/goals/no-such-goal/trace"),
                client.put("/goals/no-such-goal/trace", json={"enabled": True}),
            ):
                res = await call
                self.assertEqual(res.status_code, 404, res.text)
