"""Compaction: what a thread replaces, what it keeps, and what undo restores.

The summary is a second reading of rows the transcript still holds, so these
tests care about the boundary more than the prose. A boundary that moves the
wrong way either drops turns the person just said or re-sends turns the summary
already covers; both look like a model with a bad memory and neither is visible
in the transcript.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import BOOT_TOKEN, app
from engine.compaction import CompactionService, MAX_SUMMARY_CHARS, summary_is_usable
from engine.db import connect
from engine.executor import ExecutorService
from engine.models import ConversationCreate, Event, TurnCreate, WorkspaceCreate
from engine.providers import Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import (
    AgentRegistryService,
    ApiError,
    ConversationService,
    GoalService,
    SettingsService,
    WorkspaceService,
)


class CompactionTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name).resolve()
        self.conn: sqlite3.Connection = connect(root / "test.db")
        keychain = Keychain(secrets_path=root / "secrets.json")
        registry = AgentRegistryService(self.conn, ProviderFactory(keychain), keychain)
        self.workspaces = WorkspaceService(self.conn)
        self.conversations = ConversationService(self.conn)
        self.goals = GoalService(self.conn)
        self.settings = SettingsService(self.conn)
        self.compactions = CompactionService(self.conn, self.goals)
        self.executor = ExecutorService(
            self.goals, self.workspaces, registry, SandboxService()
        )
        self.executor.settings = self.settings
        self.executor.compactions = self.compactions

        ws = self.workspaces.create(WorkspaceCreate(name="repo", root_path=str(root)))
        self.thread = self.conversations.create(ConversationCreate(workspace_id=ws.id))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _turn(self, prompt: str, *, done: bool = True) -> Any:
        goal = self.goals.create_turn(self.thread.id, TurnCreate(prompt=prompt))
        if done:
            self.conn.execute(
                "UPDATE goals SET status = 'COMPLETED' WHERE id = ?", (goal.id,)
            )
            self.conn.commit()
        return goal

    def _reply(self, goal_id: str, text: str) -> None:
        self.goals.publish(Event(
            id=f"ev-{goal_id}",
            goal_id=goal_id,
            type="log",
            payload={"level": "info", "message": text, "turn": True},
            timestamp=time.time(),
            sequence=self.goals.next_sequence(goal_id),
        ))

    def test_the_plan_replaces_the_old_turns_and_keeps_the_newest_verbatim(self) -> None:
        for n in range(6):
            self._turn(f"turn {n}")

        plan = self.compactions.plan(self.thread.id, 3)

        self.assertEqual(3, plan.compactable)
        self.assertEqual(["turn 0", "turn 1", "turn 2"], [t["prompt"] for t in plan.older])
        self.assertEqual(["turn 3", "turn 4", "turn 5"], [t["prompt"] for t in plan.recent])

    def test_a_stored_summary_moves_the_boundary_and_keeps_the_kept_turns(self) -> None:
        for n in range(6):
            goal = self._turn(f"turn {n}")
            self._reply(goal.id, f"reply {n}")
        plan = self.compactions.plan(self.thread.id, 3)
        self.compactions.create(
            self.thread.id, "The first three turns were summarised here.", plan.compactable, plan.covers_through,
        )
        self._turn("turn 6")

        after = self.compactions.plan(self.thread.id, 3)

        # The kept turns are newer than `covers_through`, so they stay live; the
        # summarised ones are gone from the plan even though their rows remain.
        self.assertEqual(1, after.compactable)
        self.assertEqual("turn 3", after.older[0]["prompt"])
        self.assertEqual("turn 6", after.recent[-1]["prompt"])
        self.assertEqual(7, self.compactions.status(self.thread.id, self.settings).turns)

    def test_undo_forgets_the_summary_and_never_deletes_a_turn(self) -> None:
        for n in range(6):
            self._turn(f"turn {n}")
        plan = self.compactions.plan(self.thread.id, 3)
        self.compactions.create(
            self.thread.id, "The first three turns were summarised here.", plan.compactable, plan.covers_through,
        )

        self.assertTrue(self.compactions.undo(self.thread.id))
        self.assertFalse(self.compactions.undo(self.thread.id))
        self.assertEqual(6, self.compactions.status(self.thread.id, self.settings).turns)
        self.assertEqual(3, self.compactions.plan(self.thread.id, 3).compactable)

    async def test_compact_thread_stores_a_bounded_summary_and_reports_it(self) -> None:
        for n in range(6):
            goal = self._turn(f"turn {n}")
            self._reply(goal.id, f"reply {n}")
        self.settings.set_int("session_compact_keep_turns", 3)
        run_agent = AsyncMock(return_value=" ".join(
            f"Turn {n} kept parser {n} small and reply {n} bounded." for n in range(120)
        ))
        cast(Any, self.executor.orchestrator).run_agent = run_agent

        status = await self.executor.compact_thread(self.thread.id)

        self.assertIsNotNone(status.latest)
        assert status.latest is not None
        self.assertEqual(3, status.latest.turns)
        self.assertLessEqual(len(status.latest.summary), MAX_SUMMARY_CHARS)
        self.assertEqual(0, status.compactable)
        self.assertEqual(1, run_agent.await_count)

    async def test_a_wordless_summary_is_asked_again_and_the_retry_is_stored(self) -> None:
        for n in range(6):
            self._turn(f"turn {n}")
        self.settings.set_int("session_compact_keep_turns", 3)
        run_agent = AsyncMock(side_effect=["{\n\n}", "The thread chose SQLite and finished the parser."])
        cast(Any, self.executor.orchestrator).run_agent = run_agent

        status = await self.executor.compact_thread(self.thread.id)

        assert status.latest is not None
        self.assertEqual("The thread chose SQLite and finished the parser.", status.latest.summary)
        self.assertEqual(2, run_agent.await_count)

    async def test_a_summary_with_no_words_is_never_stored(self) -> None:
        for n in range(6):
            self._turn(f"turn {n}")
        self.settings.set_int("session_compact_keep_turns", 3)
        cast(Any, self.executor.orchestrator).run_agent = AsyncMock(side_effect=["{}", "  "])

        with self.assertRaises(ApiError):
            await self.executor.compact_thread(self.thread.id)
        self.assertIsNone(self.compactions.latest(self.thread.id))

    def test_a_summary_must_look_like_prose_not_json_or_a_loop(self) -> None:
        self.assertTrue(summary_is_usable("The thread chose SQLite and finished the parser."))
        self.assertFalse(summary_is_usable("{}"))
        malformed = '{"user asked about parsers": "' + "#}" * 300 + '"}'
        self.assertFalse(summary_is_usable(malformed))

    async def test_a_json_shaped_summary_is_asked_again(self) -> None:
        for n in range(6):
            self._turn(f"turn {n}")
        self.settings.set_int("session_compact_keep_turns", 3)
        malformed = '{"user asked about parsers": "' + "#}" * 300 + '"}'
        run_agent = AsyncMock(side_effect=[
            malformed,
            "The thread defined parsers, lexers and syntax errors, and ended with no open items.",
        ])
        cast(Any, self.executor.orchestrator).run_agent = run_agent

        status = await self.executor.compact_thread(self.thread.id)

        assert status.latest is not None
        self.assertTrue(status.latest.summary.startswith("The thread defined"))
        self.assertEqual(2, run_agent.await_count)

    async def test_auto_compaction_waits_for_a_worthwhile_batch(self) -> None:
        for n in range(6):
            self._turn(f"turn {n}")
        self.settings.set_int("session_compact_keep_turns", 3)
        run_agent = AsyncMock(return_value="The thread has a useful summary today.")
        cast(Any, self.executor.orchestrator).run_agent = run_agent

        # Six turns and three kept leaves three old ones: under the four-turn
        # floor, so a turn does not pay for a summary on every message.
        current = self._turn("the question", done=False)
        await self.executor._auto_compact(self.goals.get(current.id))
        self.assertIsNone(self.compactions.latest(self.thread.id))

        self._turn("turn 6")
        current = self.goals.get(current.id)
        await self.executor._auto_compact(current)
        self.assertIsNotNone(self.compactions.latest(self.thread.id))

    async def test_the_prompt_uses_the_summary_and_stops_at_the_boundary(self) -> None:
        first = self._turn("the very old question")
        self._reply(first.id, "the very old answer")
        for n in range(4):
            goal = self._turn(f"turn {n}")
            self._reply(goal.id, f"reply {n}")
        plan = self.compactions.plan(self.thread.id, 3)
        self.compactions.create(
            self.thread.id, "The old decision was kept in the thread.", plan.compactable, plan.covers_through,
        )
        current = self._turn("the new question", done=False)

        prompt = self.executor._turn_prompt(self.goals.get(current.id))

        self.assertIn("The old decision was kept in the thread.", prompt)
        self.assertNotIn("the very old question", prompt)
        self.assertIn("turn 3", prompt)
        self.assertIn("the new question", prompt)
        history = self.executor._thread_history(self.goals.get(current.id))
        self.assertTrue(all(float(t["created_at"]) > plan.covers_through for t in history))

    async def test_the_routes_read_compact_and_undo_one_thread(self) -> None:
        for n in range(7):
            goal = self._turn(f"turn {n}")
            self._reply(goal.id, f"reply {n}")
        self.settings.set_int("session_compact_keep_turns", 3)
        cast(Any, self.executor.orchestrator).run_agent = AsyncMock(
            return_value="The thread has a useful summary today."
        )
        app.state.conn = self.conn
        app.state.conversations = self.conversations
        app.state.compactions = self.compactions
        app.state.settings = self.settings
        app.state.executor = self.executor
        app.state.token = BOOT_TOKEN
        headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            read = await client.get(
                f"/conversations/{self.thread.id}/compaction", headers=headers
            )
            self.assertEqual(200, read.status_code, read.text)
            self.assertEqual(1, read.json()["auto"])
            self.assertEqual(4, read.json()["compactable"])

            compacted = await client.post(
                f"/conversations/{self.thread.id}/compaction", headers=headers
            )
            self.assertEqual(200, compacted.status_code, compacted.text)
            self.assertEqual(4, compacted.json()["latest"]["turns"])

            undone = await client.delete(
                f"/conversations/{self.thread.id}/compaction", headers=headers
            )
            self.assertEqual(200, undone.status_code, undone.text)
            self.assertIsNone(undone.json()["latest"])
