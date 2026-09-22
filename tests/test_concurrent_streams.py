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
        # Optional in-run hook (marker, role) — lets a test act at a precise
        # moment of the pipeline (e.g. attach a subscriber mid-goal).
        self.on_call = None

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
        role = next(
            (r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown"
        )
        marker = (
            "alpha" if "alpha" in user_prompt
            else "beta" if "beta" in user_prompt
            else "gamma" if "gamma" in user_prompt
            else "none"
        )
        self.calls.append((marker, role))
        if self.on_call:
            self.on_call(marker, role)
        # Yield between every call so the goals truly interleave instead of
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

    async def _collect_stream(self, goal_id: str, start_after: int = 0) -> list:
        """Poll like the WebSocket endpoint does until the goal is terminal.

        Keeps polling briefly after the terminal status so a straggler event —
        the kind a lost flush produces — cannot hide behind the reader stopping
        early. `start_after` is the reconnect floor: a subscriber that already
        saw up to N resumes from N and must still get a gapless rest.
        """
        seen: list = []
        after = start_after
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

    def _attach_resuming_reader(self, goal_id: str):
        """Snapshot now, then keep reading from the snapshot's floor.

        This is the reconnect contract the UI relies on (`goalStream` resumes
        with `after=` = its last seen sequence, and Apply floors the replay the
        same way): whatever was written before the attach must be exactly the
        snapshot, and the tail must continue it with no gap and no overlap.
        """
        snapshot = self.goals.events_after(goal_id, 0)
        assert snapshot, "attach point must already have events"
        floor = snapshot[-1].sequence
        reader = asyncio.create_task(self._collect_stream(goal_id, start_after=floor))
        return snapshot, floor, reader

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

    async def test_three_goals_and_a_mid_run_subscriber(self):
        """Three interleaved goals, and one subscriber attaching mid-run.

        The attach happens the moment the goal's fixer is invoked — its plan is
        done, half its transcript is written, and the run has most of its
        lifetime ahead of it. The subscriber resumes from a floor like the UI's
        reconnect does, and must stitch snapshot + tail into one gapless stream
        equal to a subscriber that had been there all along.
        """
        goal_a = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="goal-alpha", description="first"))
        goal_b = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="goal-beta", description="second"))
        goal_c = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="goal-gamma", description="third"))

        attach_signal = asyncio.Event()

        def on_call(marker: str, role: str) -> None:
            if marker == "gamma" and role == "fixer":
                attach_signal.set()

        self.provider.on_call = on_call

        reader_a = asyncio.create_task(self._collect_stream(goal_a.id))
        reader_b = asyncio.create_task(self._collect_stream(goal_b.id))
        reader_c = asyncio.create_task(self._collect_stream(goal_c.id))

        await asyncio.gather(
            self.executor.run_planning(goal_a.id),
            self.executor.run_planning(goal_b.id),
            self.executor.run_planning(goal_c.id),
        )
        for g in (goal_a, goal_b, goal_c):
            refreshed = self.goals.get(g.id)
            self.goals.update_status(g.id, refreshed.version, "RUNNING")

        async def _wait_then_attach(step_tasks):
            await attach_signal.wait()
            # The signal fires inside the fixer call, before its diff is
            # written: attaching now means the snapshot ends mid-step. The
            # provider's own await yields control right after the hook, so
            # this task actually runs at that instant.
            snapshot, floor, reader = self._attach_resuming_reader(goal_c.id)
            # Stay alongside the run until it is done (gathering the same
            # tasks, not re-awaiting their coroutines).
            await asyncio.gather(*step_tasks)
            return snapshot, floor, reader

        attacher = asyncio.create_task(_wait_then_attach([]))
        # Step tasks are created once and shared: the attacher awaits them so
        # it survives until the run completes, and so does the test body.
        step_tasks = [
            asyncio.create_task(self.executor.run_step(g.id, s.id))
            for g in (goal_a, goal_b, goal_c)
            for s in self.goals.steps(g.id)
        ]
        attacher = asyncio.create_task(_wait_then_attach(step_tasks))
        await asyncio.gather(*step_tasks)
        self.provider.on_call = None  # the hook has served its purpose
        # What app._run_steps does after the last step: close the goal out so
        # the streams reach a terminal event, exactly as a real run would.
        for g in (goal_a, goal_b, goal_c):
            refreshed = self.goals.get(g.id)
            self.goals.update_status(g.id, refreshed.version, "COMPLETED")

        stream_a, stream_b, stream_c = await asyncio.wait_for(
            asyncio.gather(reader_a, reader_b, reader_c), timeout=30
        )
        snapshot, floor, resuming_reader = await asyncio.wait_for(attacher, timeout=30)
        tail = await asyncio.wait_for(resuming_reader, timeout=30)

        # The pipeline actually ran, three times — the assertions below are not
        # vacuously true over empty streams.
        for name, goal in (("alpha", goal_a), ("beta", goal_b), ("gamma", goal_c)):
            self.assertEqual(self.goals.get(goal.id).status, "COMPLETED", f"goal {name} must finish")
        for name, stream in (("alpha", stream_a), ("beta", stream_b), ("gamma", stream_c)):
            self.assertGreaterEqual(len(stream), 10, f"{name}'s stream is suspiciously thin")

        # 1. Purity: every event a reader collected belongs to its goal.
        for name, stream, goal in (
            ("alpha", stream_a, goal_a), ("beta", stream_b, goal_b), ("gamma", stream_c, goal_c),
        ):
            for event in stream:
                self.assertEqual(event.goal_id, goal.id, f"a foreign event reached {name}'s stream")

        # 2. Integrity: per-goal sequences dense and ascending from 1.
        for name, stream in (("alpha", stream_a), ("beta", stream_b), ("gamma", stream_c)):
            seqs = [e.sequence for e in stream]
            self.assertEqual(seqs, list(range(1, len(stream) + 1)), f"{name}'s sequence broke")

        # 3. Separation by content — pairwise, across all three markers. UUID
        # hex cannot contain any marker (no 'l'/'t'/'g' is false for g, but
        # 'gamma' has an 'm' and hex is [0-9a-f], so a hit is real content).
        texts = {
            "alpha": "\n".join(e.model_dump_json() for e in stream_a),
            "beta": "\n".join(e.model_dump_json() for e in stream_b),
            "gamma": "\n".join(e.model_dump_json() for e in stream_c),
        }
        for name, text in texts.items():
            for other in texts:
                if other != name:
                    self.assertNotIn(other, text, f"{name}'s stream contains {other}'s content")
            self.assertIn(f"{name} body", text)

        # 4. The mid-run subscriber: the snapshot is a prefix of the full
        # stream, the tail continues at exactly floor+1, and the stitched whole
        # equals the full stream a from-the-start subscriber saw.
        # The attach was genuinely mid-run, not effectively at the end: the
        # snapshot is a strict prefix and the tail is non-empty — otherwise
        # every assertion below would pass vacuously.
        self.assertLess(
            len(snapshot), len(stream_c),
            "the subscriber attached after the goal was already finished — "
            "the mid-run scenario did not happen",
        )
        self.assertTrue(tail, "the resuming subscriber saw nothing after attaching")
        self.assertGreaterEqual(
            len(tail), 5,
            "the subscriber attached too late to exercise a real resume",
        )
        self.assertEqual(
            [e.sequence for e in snapshot],
            [e.sequence for e in stream_c[: len(snapshot)]],
            "the attach snapshot is not a prefix of the real stream",
        )
        full_seqs = [e.sequence for e in stream_c]
        self.assertIn(floor, full_seqs, "the attach floor vanished from the stream")
        tail_seqs = [e.sequence for e in tail]
        self.assertEqual(tail_seqs, list(range(floor + 1, len(full_seqs) + 1)),
                         "the resuming subscriber's tail has a gap or an overlap")
        stitched = snapshot + tail
        self.assertEqual(
            [e.sequence for e in stitched], full_seqs,
            "snapshot + tail is not exactly the stream a from-the-start subscriber saw",
        )
        for e in stitched:
            self.assertEqual(e.goal_id, goal_c.id)

        # Goals stayed mutually ignorant, three ways.
        step_ids = {
            name: {s.id for s in self.goals.steps(g.id)}
            for name, g in (("a", goal_a), ("b", goal_b), ("c", goal_c))
        }
        for name_a, ids_a in step_ids.items():
            for name_b, ids_b in step_ids.items():
                if name_a < name_b:
                    self.assertFalse(ids_a & ids_b, "two goals share a step id")
        for name, filename in (("alpha", "alpha.txt"), ("beta", "beta.txt"), ("gamma", "gamma.txt")):
            self.assertEqual((self.root / filename).read_text(), f"{name} body\n")

        # Every provider call answered a goal whose material it was shown.
        for marker, role in self.provider.calls:
            self.assertIn(marker, ("alpha", "beta", "gamma"), f"{role} call had no goal marker")


if __name__ == "__main__":
    unittest.main()
