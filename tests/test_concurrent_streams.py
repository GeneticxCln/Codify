from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
"""Two goals running concurrently against one workspace must never cross streams.

The chat renders one timeline per goal by replaying `events_after(goal_id, …)`
over the same WebSocket loop (`/ws/goals/{goal_id}` polls it verbatim). If two
concurrent goals could see each other's events — through a lost WHERE clause, a
shared sequence counter, or a provider that answers the wrong run — the user
would read goal A's diff inside goal B's conversation.

The guarantees live in `tests/stream_isolation.py` exactly once, shared with
the wire-level twin (`test_concurrent_streams_ws.py`) so the two layers cannot
drift. This file runs the goals *interleaved* (one shared provider, one shared
database, readers polling while the writers run) and asserts, through that
helper:

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
import time
import unittest
from pathlib import Path

from engine.db import connect
from engine.executor import ExecutorService
from engine.laya import LayaDecision, LayaService
from engine.models import ROLES, AgentConfigUpdate, GoalCreate, WorkspaceCreate
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import ApiError, AgentRegistryService, GoalService, WorkspaceService
from tests.stream_isolation import (
    assert_cancelled_stream_ends_cleanly,
    assert_content_separated,
    assert_dense_from_one,
    assert_midrun_resume_is_seamless,
    assert_replay_equals_live,
    assert_stream_pure,
)


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
        # (marker, role) pairs whose model call parks on hold_gate — lets a
        # test cancel a goal while one of its calls is genuinely in flight.
        self.hold_keys: set[tuple[str, str]] = set()
        self.hold_gate: asyncio.Event | None = None

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
        role = next(
            (r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown"
        )
        marker = (
            "alpha" if "alpha" in user_prompt
            else "beta" if "beta" in user_prompt
            else "gamma" if "gamma" in user_prompt
            else "delta" if "delta" in user_prompt
            else "none"
        )
        self.calls.append((marker, role))
        if self.on_call:
            self.on_call(marker, role)
        if (marker, role) in self.hold_keys and self.hold_gate is not None:
            await self.hold_gate.wait()
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
        # Same terminal set as the UI (goalStream.ts) and the wire drain:
        # CANCELLED ends a stream too. The grace is 50 quiet polls (~50ms) so
        # a tail that straggles past the terminal frame — a cancelled goal's
        # in-flight step, for one — cannot be cut off by the reader.
        while empty_polls_after_terminal < 50:
            events = self.goals.events_after(goal_id, after)
            if events:
                seen.extend(events)
                after = events[-1].sequence
                if any(
                    e.type == "goal_status"
                    and e.payload.get("status") in ("COMPLETED", "FAILED", "CANCELLED")
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

        # 1–3. The shared guarantees, asserted exactly as the wire twin does.
        for name, stream, goal in (("alpha", stream_a, goal_a), ("beta", stream_b, goal_b)):
            assert_stream_pure(self, stream, goal.id, name)
            assert_dense_from_one(self, stream, name)
        assert_content_separated(self, {"alpha": stream_a, "beta": stream_b})
        # The marker flows through the events that carry real content: the
        # fixer's diff, the verifier's explanation, the scribe's message.
        from tests.stream_isolation import _frame_text
        text_a = _frame_text(stream_a)
        text_b = _frame_text(stream_b)
        self.assertIn("alpha body", text_a)
        self.assertIn("the alpha change trivially passes", text_a)
        self.assertIn("beta body", text_b)
        self.assertIn("feat: add beta file", text_b)

        # 4. A late subscriber replaying from 0 gets exactly the live stream.
        assert_replay_equals_live(
            self, [e.sequence for e in self.goals.events_after(goal_a.id, 0)],
            [e.sequence for e in stream_a], "alpha",
        )
        assert_replay_equals_live(
            self, [e.sequence for e in self.goals.events_after(goal_b.id, 0)],
            [e.sequence for e in stream_b], "beta",
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

        # 1–3. The shared guarantees, pairwise across all three goals.
        for name, stream, goal in (
            ("alpha", stream_a, goal_a), ("beta", stream_b, goal_b), ("gamma", stream_c, goal_c),
        ):
            assert_stream_pure(self, stream, goal.id, name)
            assert_dense_from_one(self, stream, name)
        assert_content_separated(
            self, {"alpha": stream_a, "beta": stream_b, "gamma": stream_c},
            body_markers={"alpha": "alpha body", "beta": "beta body", "gamma": "gamma body"},
        )

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
        assert_midrun_resume_is_seamless(self, stream_c, snapshot + tail, floor, goal_c.id, "gamma")
        # And the resume tail itself was gapless from the floor.
        full_seqs = [e.sequence for e in stream_c]
        self.assertIn(floor, full_seqs, "the attach floor vanished from the stream")
        self.assertEqual(
            [e.sequence for e in tail], list(range(floor + 1, len(full_seqs) + 1)),
            "the resuming subscriber's tail has a gap or an overlap",
        )

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

    async def test_cancelled_goal_ends_its_stream_cleanly(self):
        """A goal cancelled mid-run ends its stream at the cancel.

        The service-layer twin of the wire test's delta: four goals interleave,
        delta's fixer call is parked mid-flight (on the provider's hold gate),
        and the cancel lands while that call is genuinely outstanding. The
        cancelled stream must be pure, dense, end at CANCELLED with no status
        after it, and show at most one completed step — asserted through the
        same helper the wire test uses, so both layers prove one contract.
        """
        goal_a = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="goal-alpha", description="first"))
        goal_b = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="goal-beta", description="second"))
        goal_c = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="goal-gamma", description="third"))
        goal_d = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="goal-delta", description="fourth"))

        # Park delta's fixer call the moment it starts, so the cancel lands
        # while the pipeline is genuinely mid-flight. Keyed by (marker, role)
        # so planning itself is never deadlocked.
        self.provider.hold_keys = {("delta", "fixer")}
        self.provider.hold_gate = asyncio.Event()

        reader_a = asyncio.create_task(self._collect_stream(goal_a.id))
        reader_b = asyncio.create_task(self._collect_stream(goal_b.id))
        reader_c = asyncio.create_task(self._collect_stream(goal_c.id))
        reader_d = asyncio.create_task(self._collect_stream(goal_d.id))

        await asyncio.gather(
            self.executor.run_planning(goal_a.id),
            self.executor.run_planning(goal_b.id),
            self.executor.run_planning(goal_c.id),
            self.executor.run_planning(goal_d.id),
        )
        for g in (goal_a, goal_b, goal_c, goal_d):
            refreshed = self.goals.get(g.id)
            self.goals.update_status(g.id, refreshed.version, "RUNNING")

        step_tasks = {
            g.id: [
                asyncio.create_task(self.executor.run_step(g.id, s.id))
                for s in self.goals.steps(g.id)
            ]
            for g in (goal_a, goal_b, goal_c, goal_d)
        }

        # Delta hits its fixer hold almost immediately; wait until it is
        # genuinely parked before cancelling, otherwise the cancel could win
        # the race and land before any step was in flight.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ("delta", "fixer") in {
                (m, r) for m, r in self.provider.calls
            }:
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("delta's fixer call never started — cannot cancel mid-flight")

        g = self.goals.get(goal_d.id)
        self.goals.update_status(goal_d.id, g.version, "CANCELLED")
        # What app._run_steps does between steps and after each one: the
        # runner sees a non-RUNNING status and stops. The parked fixer call is
        # released so its task can unwind like a real in-flight request would.
        self.provider.hold_gate.set()
        await asyncio.gather(*step_tasks[goal_d.id])

        # Meanwhile the other three goals run to completion.
        for gid in (goal_a.id, goal_b.id, goal_c.id):
            await asyncio.gather(*step_tasks[gid])
        for gid in (goal_a.id, goal_b.id, goal_c.id):
            refreshed = self.goals.get(gid)
            self.goals.update_status(gid, refreshed.version, "COMPLETED")

        stream_a, stream_b, stream_c, stream_d = await asyncio.wait_for(
            asyncio.gather(reader_a, reader_b, reader_c, reader_d), timeout=30
        )

        # The living goals actually finished — the isolation assertions below
        # are not vacuously true over empty streams.
        for name, gid in (("alpha", goal_a.id), ("beta", goal_b.id), ("gamma", goal_c.id)):
            self.assertEqual(self.goals.get(gid).status, "COMPLETED", f"goal {name} must finish")
        self.assertEqual(self.goals.get(goal_d.id).status, "CANCELLED", "delta must stay cancelled")

        for name, stream, gid in (
            ("alpha", stream_a, goal_a.id), ("beta", stream_b, goal_b.id),
            ("gamma", stream_c, goal_c.id), ("delta", stream_d, goal_d.id),
        ):
            assert_stream_pure(self, stream, gid, name)
            assert_dense_from_one(self, stream, name)
        assert_content_separated(
            self, {"alpha": stream_a, "beta": stream_b, "gamma": stream_c, "delta": stream_d},
            body_markers={
                "alpha": "alpha body", "beta": "beta body",
                "gamma": "gamma body", "delta": "delta body",
            },
        )

        # The cancelled stream, through the same helper the wire test uses:
        # ends at CANCELLED, no status after it, at most one step completed.
        assert_cancelled_stream_ends_cleanly(self, stream_d, goal_d.id, "delta")

        # No goal shares step ids or file content with another.
        step_ids = {
            name: {s.id for s in self.goals.steps(gid)}
            for name, gid in (("a", goal_a.id), ("b", goal_b.id), ("c", goal_c.id), ("d", goal_d.id))
        }
        for name_a, ids_a in step_ids.items():
            for name_b, ids_b in step_ids.items():
                if name_a < name_b:
                    self.assertFalse(ids_a & ids_b, "two goals share a step id")
        for marker in ("alpha", "beta", "gamma"):
            self.assertEqual((self.root / f"{marker}.txt").read_text(), f"{marker} body\n")

        # Every provider call answered a goal whose material it was shown.
        for marker, role in self.provider.calls:
            self.assertIn(marker, ("alpha", "beta", "gamma", "delta"), f"{role} call had no goal marker")


if __name__ == "__main__":
    unittest.main()
