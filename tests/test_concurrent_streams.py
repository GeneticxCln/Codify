from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
"""Two goals running concurrently against one workspace must never cross streams.

The chat renders one timeline per goal by replaying `events_after(goal_id, …)`
over the same WebSocket loop (`/ws/goals/{goal_id}` polls it verbatim). If two
concurrent goals could see each other's events — through a lost WHERE clause, a
shared sequence counter, or a provider that answers the wrong run — the user
would read goal A's diff inside goal B's conversation.

So this test runs two goals *interleaved* (one shared provider, one shared
database, readers polling while the writers run) and asserts four independent
properties:

1. Purity       — every event a reader saw belongs to its goal.
2. Integrity    — each stream's sequences are dense and ascending from 1.
3. Separation   — no bytes of goal B (its marker word) appear in goal A's
                  stream, by content, not just by id bookkeeping.
4. Reachability — a late subscriber replaying from 0 gets exactly what the
                  live reader collected, nothing more.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from engine.db import connect
from engine.executor import ExecutorService
from engine.laya import LayaDecision, LayaService
from engine.models import ROLES, AgentConfigUpdate, GoalCreate, WorkspaceCreate
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import ApiError, AgentRegistryService, GoalService, WorkspaceService


class _GoalAwareProvider(BaseProvider):
    """Answers every role with the asking goal's marker baked in.

    Each goal's plan, diff, verdict explanation and summary carry its own
    marker word, so a crossed stream shows up as foreign text in the transcript
    — not merely as a bookkeeping mismatch. The marker is detected from the
    prompt itself (every role's prompt names the goal or the plan built from
    it), which is exactly the information a real provider has.
    """

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
        role = next(
            (r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown"
        )
        marker = (
            "alpha" if "alpha" in user_prompt
            else "beta" if "beta" in user_prompt
            else "none"
        )
        self.calls.append((marker, role))
        # Yield between every call so the two goals truly interleave instead of
        # running one after the other by accident of the event loop.
        await asyncio.sleep(0)

        if role == "planner":
            return json.dumps({"steps": [{
                "title": f"{marker} step",
                "description": f"create the {marker} file",
                "suggested_paths": [f"{marker}.txt"],
            }]})
        if role == "fixer":
            return json.dumps({"files": [{
                "path": f"{marker}.txt", "action": "create", "content": f"{marker} body\n",
            }]})
        if role == "verifier":
            return json.dumps({
                "argv": None, "verdict": "pass",
                "explanation": f"the {marker} change trivially passes",
            })
        if role == "critic":
            return json.dumps({"decision": "approve", "reasons": []})
        if role == "scribe":
            return json.dumps({
                "summary": f"the {marker} file was created",
                "commit_message": f"feat: add {marker} file",
            })
        # librarian: one quiet round, no requests.
        return json.dumps({"enough": True, "files": []})


class _GoalAwareFactory(ProviderFactory):
    def __init__(self, provider):
        super().__init__(Keychain())
        self.provider = provider

    def build(self, config):
        return self.provider


class _SkippedGate(LayaService):
    """The gate is a separate concern; this test is about stream isolation."""

    async def decide(self, state):
        return LayaDecision(engine="skipped", skipped_reason="test double")


class TestConcurrentGoalsDoNotCrossStreams(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.provider = _GoalAwareProvider()
        self.registry = AgentRegistryService(
            self.conn, _GoalAwareFactory(self.provider), Keychain()
        )
        # Seeded roles carry no model id; state the one this suite runs with.
        for role in ROLES:
            self.registry.set_config(role, AgentConfigUpdate(model_name="test-model"))
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=_SkippedGate()
        )
        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))

    async def asyncTearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    async def _collect_stream(self, goal_id: str) -> list:
        """Poll like the WebSocket endpoint does until the goal is terminal.

        Keeps polling briefly after the terminal status so a straggler event —
        the kind a lost flush produces — cannot hide behind the reader stopping
        early.
        """
        seen: list = []
        after = 0
        terminal = False
        empty_polls_after_terminal = 0
        while empty_polls_after_terminal < 5:
            events = self.goals.events_after(goal_id, after)
            if events:
                seen.extend(events)
                after = events[-1].sequence
                if any(
                    e.type == "goal_status"
                    and e.payload.get("status") in ("COMPLETED", "FAILED")
                    for e in events
                ):
                    terminal = True
                empty_polls_after_terminal = 0
            elif terminal:
                empty_polls_after_terminal += 1
            await asyncio.sleep(0.001)
        return seen

    async def test_two_concurrent_goals_never_cross_streams(self):
        goal_a = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="goal-alpha", description="first"))
        goal_b = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="goal-beta", description="second"))

        # Readers start BEFORE any writer, so they observe the goals live —
        # the same vantage point the chat's WebSocket has.
        reader_a = asyncio.create_task(self._collect_stream(goal_a.id))
        reader_b = asyncio.create_task(self._collect_stream(goal_b.id))

        await asyncio.gather(
            self.executor.run_planning(goal_a.id),
            self.executor.run_planning(goal_b.id),
        )
        for g in (goal_a, goal_b):
            refreshed = self.goals.get(g.id)
            self.goals.update_status(g.id, refreshed.version, "RUNNING")
        await asyncio.gather(
            *(
                self.executor.run_step(g.id, s.id)
                for g in (goal_a, goal_b)
                for s in self.goals.steps(g.id)
            )
        )
        # What app._run_steps does after the last step: close the goal out so
        # the streams reach a terminal event, exactly as a real run would.
        for g in (goal_a, goal_b):
            refreshed = self.goals.get(g.id)
            self.goals.update_status(g.id, refreshed.version, "COMPLETED")

        stream_a, stream_b = await asyncio.wait_for(
            asyncio.gather(reader_a, reader_b), timeout=30
        )

        # The pipeline actually ran, twice — the assertions below are not
        # vacuously true over empty streams.
        self.assertEqual(self.goals.get(goal_a.id).status, "COMPLETED", "goal alpha must finish")
        self.assertEqual(self.goals.get(goal_b.id).status, "COMPLETED", "goal beta must finish")
        self.assertGreaterEqual(len(stream_a), 10, "alpha's stream is suspiciously thin")
        self.assertGreaterEqual(len(stream_b), 10, "beta's stream is suspiciously thin")

        # 1. Purity: every event a reader collected belongs to its goal.
        for event in stream_a:
            self.assertEqual(event.goal_id, goal_a.id, "a foreign event reached alpha's stream")
        for event in stream_b:
            self.assertEqual(event.goal_id, goal_b.id, "a foreign event reached beta's stream")

        # 2. Integrity: per-goal sequences are dense and ascending from 1. The
        # counter is per-goal (UPDATE … RETURNING on the goal row) — a shared
        # counter would interleave and break density; a racy one would skip.
        for name, stream in (("alpha", stream_a), ("beta", stream_b)):
            seqs = [e.sequence for e in stream]
            self.assertEqual(seqs, list(range(1, len(stream) + 1)), f"{name}'s sequence broke")

        # 3. Separation by content: no byte of one goal appears in the other's
        # transcript. UUID hex cannot contain these markers (no 'l' or 't'), so
        # a hit is real content, not an id collision.
        text_a = "\n".join(e.model_dump_json() for e in stream_a)
        text_b = "\n".join(e.model_dump_json() for e in stream_b)
        self.assertNotIn("beta", text_a, "alpha's stream contains beta's content")
        self.assertNotIn("alpha", text_b, "beta's stream contains alpha's content")
        # The marker flows through the events that carry real content: the
        # fixer's diff, the verifier's explanation, the scribe's message.
        self.assertIn("alpha body", text_a)
        self.assertIn("the alpha change trivially passes", text_a)
        self.assertIn("beta body", text_b)
        self.assertIn("feat: add beta file", text_b)

        # 4. A late subscriber replaying from 0 gets exactly the live stream.
        self.assertEqual(
            [e.sequence for e in self.goals.events_after(goal_a.id, 0)],
            [e.sequence for e in stream_a],
            "replay diverged from what the live reader saw",
        )
        self.assertEqual(
            [e.sequence for e in self.goals.events_after(goal_b.id, 0)],
            [e.sequence for e in stream_b],
            "replay diverged from what the live reader saw",
        )

        # The interleaved fixers wrote to their own goal's paths, and the plans
        # stayed with their goals: every step of one goal is unknown to the
        # other — a crossed step id is a 404, not another goal's work.
        self.assertEqual(
            [s.title for s in self.goals.steps(goal_a.id)], ["alpha step"]
        )
        self.assertEqual(
            [s.title for s in self.goals.steps(goal_b.id)], ["beta step"]
        )
        step_b = self.goals.steps(goal_b.id)[0]
        with self.assertRaises(ApiError) as ctx:
            self.executor._step(goal_a.id, step_b.id)
        self.assertEqual(ctx.exception.code, "unknown_step")

        self.assertEqual((self.root / "alpha.txt").read_text(), "alpha body\n")
        self.assertEqual((self.root / "beta.txt").read_text(), "beta body\n")

        # Every provider call answered the goal whose material it was shown —
        # no call was routed with another goal's prompt.
        for marker, role in self.provider.calls:
            self.assertIn(marker, ("alpha", "beta"), f"{role} call had no goal marker")


if __name__ == "__main__":
    unittest.main()
