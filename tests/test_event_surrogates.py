"""One lone surrogate must not make a goal's event log unreadable forever.

A model can answer with JSON holding `"\\ud800"`. `json.loads` turns that into a `str` Python is happy to
carry and nothing downstream can encode to UTF-8. The event row is stored fine (`db.dumps` escapes it) and
comes back as the same poisoned `str`, so pydantic's `model_dump_json` and Starlette's JSON response both
raised `UnicodeEncodeError` on it: `GET /goals/{id}/events` answered 500 for that goal forever, and
`ws_goal` died on the same event for every connection (close 1011), after which the UI reconnected and
died again. Found in the audit of 2026-10 (the critic's first leftover), seen from `goalStream`'s loop.

The guarantee is in `Event`'s payload validator, because that is the one door both a new event and an old
row go through, so rows written before the fix heal on read with no migration. `publish` scrubs with the
same helper, and the raw-SQL readers of the table (statistics, the model menu, recall) scrub what they
read with `loads_payload` / `scrub_payload_text`, so an old row is healed for them as well as for `Event`.
Rows are inserted by hand here when the point is a row that already exists.
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import uvicorn
import websockets
from httpx import ASGITransport

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import engine.models as models_module
from engine.app import BOOT_TOKEN, app
from engine.db import connect, dumps
from engine.models import (
    ConversationCreate,
    Event,
    GoalCreate,
    TurnCreate,
    WorkspaceCreate,
    scrub_surrogates,
)
from engine.services import ConversationService, GoalService, WorkspaceService

LONE = "\ud800"
FFFD = "�"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def encodes(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class TestTheHelper(unittest.TestCase):
    def test_a_lone_surrogate_becomes_the_replacement_character(self) -> None:
        for lone in ("\ud800", "\udbff", "\udc00", "\udfff"):
            with self.subTest(code=hex(ord(lone))):
                self.assertEqual(f"a{FFFD}b", scrub_surrogates(f"a{lone}b"))

    def test_a_valid_astral_character_is_left_alone(self) -> None:
        # What a well-formed pair becomes as soon as it is a Python str: one character.
        text = "ok \U0001F600 and \U00010000 and \U0010FFFF"
        self.assertEqual(text, scrub_surrogates(text))
        self.assertEqual("\U0001F600", scrub_surrogates(json.loads('"\\ud83d\\ude00"')))

    def test_two_halves_side_by_side_become_the_character_they_spell(self) -> None:
        # What the stored row would have said: json.loads of the escaped pair is one character. Built from
        # code points on purpose: a literal emoji here is already one character and would never reach the merge.
        self.assertEqual("\U0001F600", scrub_surrogates(chr(0xD83D) + chr(0xDE00)))
        self.assertEqual(f"{FFFD}{FFFD}", scrub_surrogates("\ude00\ud83d"))

    def test_it_reaches_into_dicts_lists_and_keys(self) -> None:
        payload = {
            "a": f"x{LONE}y",
            f"k{LONE}": [1, f"{LONE}", {"deep": [[f"{LONE}z"]]}],
            "ok": "fine",
        }
        self.assertEqual(
            {
                "a": f"x{FFFD}y",
                f"k{FFFD}": [1, FFFD, {"deep": [[f"{FFFD}z"]]}],
                "ok": "fine",
            },
            scrub_surrogates(payload),
        )

    def test_every_other_type_comes_back_equal_and_the_same_object(self) -> None:
        for value in (None, True, 0, 7, 1.5, float("inf"), b"bytes", (1, f"t{LONE}"), {1, 2}):
            with self.subTest(value=repr(value)):
                self.assertIs(value, scrub_surrogates(value))

    def test_a_payload_with_nothing_to_scrub_is_not_rebuilt(self) -> None:
        payload = {"a": "plain", "b": [1, 2.5, None, {"c": "\U0001F600"}], "d": {"e": ["f"]}}
        self.assertIs(payload, scrub_surrogates(payload))
        self.assertIs(payload["b"], scrub_surrogates(payload["b"]))

    def test_untouched_siblings_keep_their_identity_and_the_input_is_not_mutated(self) -> None:
        untouched = {"big": ["x"] * 3}
        payload = {"first": untouched, "bad": f"a{LONE}", "last": [1]}
        before = json.dumps(payload)
        out = scrub_surrogates(payload)
        self.assertIsNot(payload, out)
        self.assertIs(untouched, out["first"])
        self.assertEqual(before, json.dumps(payload), "the caller's payload was changed")
        self.assertEqual(["first", "bad", "last"], list(out), "the key order moved")

    def test_it_is_idempotent(self) -> None:
        payload = {"a": [f"x{LONE}", {"b": f"{LONE}{LONE}"}], f"k{LONE}": 1}
        once = scrub_surrogates(payload)
        twice = scrub_surrogates(once)
        self.assertIs(once, twice, "the second pass found something to rebuild")
        self.assertEqual(once, twice)

    def test_the_result_encodes(self) -> None:
        out = scrub_surrogates({"a": f"{LONE}\udc00\ud83d"})
        json.dumps(out, ensure_ascii=False).encode("utf-8")

    def _looks_at_strings(self, payload: Any) -> int:
        """How many times the scrub looked at a string while walking `payload`."""
        looks: list[str] = []
        real = models_module._scrub_text

        def counting(text: str) -> str:
            looks.append(text)
            return real(text)

        with mock.patch.object(models_module, "_scrub_text", counting):
            scrub_surrogates(payload)
        return len(looks)

    def test_every_string_is_looked_at_once_however_many_are_bad(self) -> None:
        # A walk that started again after each replacement would look at the strings that came before it
        # again and again, which is what makes a rebuild quadratic. Counting the looks is the same check
        # without a stopwatch, which a loaded machine can fool.
        n = 2_000
        payload = {"items": [f"line {i} {LONE}" for i in range(n)], "tail": ["ok"] * n}
        self.assertEqual(2 + 2 * n, self._looks_at_strings(payload), "keys, then every value, once each")

    def test_a_wide_dict_of_bad_values_is_walked_once_too(self) -> None:
        n = 2_000
        payload = {f"k{i}": f"v{LONE}" for i in range(n)}
        self.assertEqual(2 * n, self._looks_at_strings(payload), "every key and every value, once each")


class TestTheEventModel(unittest.TestCase):
    def test_an_event_built_with_a_lone_surrogate_serialises(self) -> None:
        event = Event(
            id="e", goal_id="g", type="log", payload={"message": f"x{LONE}y"}, timestamp=1.0, sequence=1,
        )
        self.assertEqual(f"x{FFFD}y", event.payload["message"])
        self.assertEqual(f"x{FFFD}y", json.loads(event.model_dump_json())["payload"]["message"])

    def test_a_row_read_back_is_cleaned_by_the_same_door(self) -> None:
        row = {
            "id": "e", "goal_id": "g", "step_id": None, "type": "test_result",
            "payload": json.loads(dumps({"explanation": f"no {LONE} run", "n": [f"{LONE}"]})),
            "timestamp": 1.0, "sequence": 3,
        }
        self.assertIn(LONE, row["payload"]["explanation"], "the premise: the row comes back poisoned")
        event = Event.model_validate(row)
        self.assertEqual({"explanation": f"no {FFFD} run", "n": [FFFD]}, event.payload)
        event.model_dump_json()

    def test_an_ordinary_payload_is_returned_equal(self) -> None:
        payload = {"message": "café \U0001F600", "n": 3, "ok": True, "none": None, "xs": [1.5, "a"]}
        event = Event(id="e", goal_id="g", type="log", payload=payload, timestamp=1.0, sequence=1)
        self.assertEqual(payload, event.payload)


class GoalCase(unittest.IsolatedAsyncioTestCase):
    """A goal in a throwaway database, read through the same service the engine uses."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / "ws").mkdir()
        self.conn = connect(self.base / "t.db")
        self.addCleanup(self.conn.close)
        self.goals = GoalService(self.conn)
        self.workspace = WorkspaceService(self.conn).create(
            WorkspaceCreate(name="w", root_path=str(self.base / "ws"))
        )
        goal = self.goals.create(GoalCreate(workspace_id=self.workspace.id, title="g"))
        self.goal = self.goals.update_status(goal.id, goal.version, "PENDING")

    def event(self, type_: str, payload: dict[str, Any]) -> Event:
        return Event(
            id=str(uuid.uuid4()), goal_id=self.goal.id, step_id=None, type=type_,
            payload=payload, timestamp=time.time(), sequence=self.goals.next_sequence(self.goal.id),
        )

    def poison(self, type_: str, payload: dict[str, Any]) -> int:
        """A row as the engine used to write it: stored through `dumps`, bypassing the model."""
        sequence = self.goals.next_sequence(self.goal.id)
        self.conn.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), self.goal.id, None, type_, dumps(payload), time.time(), sequence),
        )
        self.conn.commit()
        return sequence

    def stored(self, sequence: int) -> str:
        row = self.conn.execute(
            "SELECT payload FROM events WHERE goal_id = ? AND sequence = ?", (self.goal.id, sequence)
        ).fetchone()
        return str(row["payload"])


class TestPublishAndRead(GoalCase):
    async def test_a_published_payload_with_a_lone_surrogate_reads_back_serialisable(self) -> None:
        sent = self.goals.publish(self.event("test_result", {"explanation": f"no {LONE} run", "ok": False}))

        (read,) = [e for e in self.goals.events_after(self.goal.id, 0) if e.type == "test_result"]
        self.assertEqual(f"no {FFFD} run", read.payload["explanation"])
        self.assertEqual(sent.sequence, read.sequence)
        json.loads(read.model_dump_json())

    async def test_the_stored_row_is_clean_even_when_the_model_was_bypassed(self) -> None:
        # `model_construct` skips validation, as does mutating the payload after the event exists.
        # What reaches the table must not depend on every caller having gone through the validator,
        # because the raw-SQL readers of this table (stats, recall) never see an `Event`.
        sequence = self.goals.next_sequence(self.goal.id)
        raw = Event.model_construct(
            id=str(uuid.uuid4()), goal_id=self.goal.id, step_id=None, type="log",
            payload={"message": f"a{LONE}b", f"k{LONE}": "v"}, timestamp=time.time(), sequence=sequence,
        )
        self.goals.publish(raw)

        text = self.stored(sequence)
        self.assertNotIn("ud800", text)
        self.assertEqual({"message": f"a{FFFD}b", f"k{FFFD}": "v"}, json.loads(text))

    async def test_a_payload_changed_after_the_event_was_built_is_cleaned_at_publish(self) -> None:
        event = self.event("log", {"message": "clean"})
        event.payload["message"] = f"late{LONE}"
        self.goals.publish(event)

        self.assertEqual({"message": f"late{FFFD}"}, json.loads(self.stored(event.sequence)))

    async def test_a_row_stored_before_the_fix_heals_on_read(self) -> None:
        sequence = self.poison("log", {"message": f"old {LONE} row", "deep": {"xs": [f"{LONE}"]}})
        self.assertIn("ud800", self.stored(sequence), "the premise: the row holds the escaped surrogate")

        events = self.goals.events_after(self.goal.id, 0)
        (hit,) = [e for e in events if e.sequence == sequence]
        self.assertEqual({"message": f"old {FFFD} row", "deep": {"xs": [FFFD]}}, hit.payload)
        for event in events:
            event.model_dump_json()

        latest = self.goals.latest_event(self.goal.id, "log")
        self.assertIsNotNone(latest)
        assert latest is not None
        latest.model_dump_json()

    async def test_the_healed_row_is_not_rewritten_by_reading_it(self) -> None:
        sequence = self.poison("log", {"message": f"a{LONE}"})
        before = self.stored(sequence)
        self.goals.events_after(self.goal.id, 0)
        self.assertEqual(before, self.stored(sequence), "a read must not write")

    async def test_valid_astral_pairs_and_other_types_survive_the_round_trip(self) -> None:
        payload = {"message": "café \U0001F600", "n": 3, "f": 1.5, "t": True, "none": None, "xs": [1, "a"]}
        self.goals.publish(self.event("log", payload))
        (read,) = [e for e in self.goals.events_after(self.goal.id, 0) if e.type == "log"]
        self.assertEqual(payload, read.payload)

    async def test_the_events_after_a_bad_one_are_still_delivered(self) -> None:
        self.goals.publish(self.event("log", {"message": "before"}))
        self.poison("log", {"message": f"bad{LONE}"})
        self.goals.publish(self.event("log", {"message": "after"}))

        messages = [e.payload.get("message") for e in self.goals.events_after(self.goal.id, 0) if e.type == "log"]
        self.assertEqual(["before", f"bad{FFFD}", "after"], messages)


class TestTheRawReaderOfTurnReplies(GoalCase):
    async def test_a_reply_stored_with_a_lone_surrogate_reaches_the_next_prompt_encodable(self) -> None:
        # `turn_history` reads the table with SQL and never builds an `Event`; its text goes into the
        # next turn's prompt and from there into a provider request body.
        thread = ConversationService(self.conn).create(ConversationCreate(workspace_id=self.workspace.id))
        turn = self.goals.create_turn(thread.id, TurnCreate(prompt="hi"))
        self.conn.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence) VALUES (?, ?, NULL, 'log', ?, ?, 1)",
            (str(uuid.uuid4()), turn.id, dumps({"turn": True, "message": f"hello {LONE} there"}), time.time()),
        )
        self.conn.commit()

        (pair,) = self.goals.turn_history(thread.id)
        self.assertEqual(f"hello {FFFD} there", pair["reply"])
        self.assertTrue(encodes(pair["reply"]))


class TestTheRouteAnswers(GoalCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        app.state.goals = self.goals
        app.state.token = BOOT_TOKEN

    async def get(self, url: str) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {BOOT_TOKEN}"},
        ) as client:
            return await client.get(url)

    async def test_the_events_route_answers_200_for_a_goal_holding_a_poisoned_row(self) -> None:
        self.poison("test_result", {"verdict": "pass", "explanation": f"no {LONE} run"})
        self.goals.publish(self.event("log", {"message": f"fresh {LONE}"}))

        res = await self.get(f"/goals/{self.goal.id}/events")

        self.assertEqual(200, res.status_code, res.text[:300])
        body = res.json()
        explanations = [e["payload"].get("explanation") for e in body if e["type"] == "test_result"]
        self.assertEqual([f"no {FFFD} run"], explanations)
        self.assertEqual(sorted(e["sequence"] for e in body), [e["sequence"] for e in body])


class TestTheRawReadersOfOldRows(GoalCase):
    """Rows written before the fix hold the escape, and the readers that never build an `Event` must heal them too.

    The statistics screen, the model menu and recall read `events.payload` with SQL and `json.loads`; each of
    them used to hand the poisoned `str` to a JSON response (a 500 for the whole screen) or to the memory
    brief (and so to a provider request body).
    """

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        app.state.goals = self.goals
        app.state.conn = self.conn
        app.state.token = BOOT_TOKEN

    async def get(self, url: str) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {BOOT_TOKEN}"},
        ) as client:
            return await client.get(url)

    async def test_the_agent_stats_route_answers_for_a_role_whose_last_failure_holds_one(self) -> None:
        self.poison("agent_call_failed", {"role": "fixer", "code": "x", "message": f"bad {LONE}", "provider": f"p{LONE}"})
        self.poison("usage", {"role": "planner", "provider": f"p{LONE}", "model": f"m{LONE}", "duration_ms": 5})

        res = await self.get("/settings/agents/stats")

        self.assertEqual(200, res.status_code, res.text[:300])
        by_role = {r["role"]: r for r in res.json()["stats"]}
        self.assertEqual(f"bad {FFFD}", by_role["fixer"]["last_error"]["message"])
        self.assertEqual(f"p{FFFD}", by_role["planner"]["last_call"]["provider"])

    async def test_the_recent_models_route_answers_for_a_model_menu_row_holding_one(self) -> None:
        self.poison("agent_assigned", {"role": "fixer", "provider": f"p{LONE}", "model": f"m{LONE}"})

        res = await self.get("/models/recent")

        self.assertEqual(200, res.status_code, res.text[:300])
        self.assertEqual([(f"p{FFFD}", f"m{FFFD}")], [(r["provider"], r["model"]) for r in res.json()])

    async def test_the_statistics_sweeps_hand_back_encodable_payloads(self) -> None:
        from engine.app import _load_metrics, _load_stats

        self.poison("usage", {"role": "planner", "provider": f"p{LONE}", "model": f"m{LONE}", "cost": 1})
        self.poison("error", {"code": "c", "message": f"bad {LONE}"})

        _goals, events, _coverage = _load_stats(self.conn)
        metrics = _load_metrics(self.conn)

        self.assertTrue(events and metrics)
        for row in (*events, *metrics):
            json.dumps(row["payload"], ensure_ascii=False).encode("utf-8")
        self.assertEqual(f"p{FFFD}", events[0]["payload"]["provider"])

    async def test_recall_does_not_see_the_surrogate_in_a_failure_it_remembers(self) -> None:
        from engine import recall

        self.poison("agent_call_failed", {"role": "fixer", "code": "x", "message": f"timeout {LONE} again"})

        rows = self.goals.recall_events(self.workspace.id)
        hits = [h for r in rows if (h := recall.project(r, "timeout")) is not None]

        self.assertEqual([f"timeout {FFFD} again"], [h["fields"]["message"] for h in hits])
        for row in rows:
            self.assertTrue(encodes(str(row["payload"])), row["payload"])

    async def test_a_payload_with_nothing_to_scrub_is_returned_as_it_was_read(self) -> None:
        from engine.models import loads_payload, scrub_payload_text

        raw = dumps({"message": "plain \U0001F600", "n": [1, 2]})
        self.assertIs(raw, scrub_payload_text(raw))
        self.assertEqual({"message": "plain \U0001F600", "n": [1, 2]}, loads_payload(raw))
        self.assertEqual({}, loads_payload(None))
        pair = '{"m": "\\ud83d\\ude00"}'
        self.assertIs(pair, scrub_payload_text(pair))
        self.assertEqual({"m": "\U0001F600"}, loads_payload(pair))
        with self.assertRaises(ValueError):
            loads_payload("{not json \\ud800")
        self.assertEqual("not json \\ud800", scrub_payload_text("not json \\ud800"))


class TestTheSocketReplaysEverything(GoalCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # The server reads through its own connection, as in tests/test_ws_goal_lifecycle.py: two threads
        # inside statements on one sqlite3 handle is not what is under test.
        server_conn = connect(self.base / "t.db")
        self.addCleanup(server_conn.close)
        app.state.goals = GoalService(server_conn)
        app.state.token = BOOT_TOKEN
        self.port = free_port()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="error", lifespan="off",
            timeout_graceful_shutdown=1,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve()), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        self.assertTrue(self.server.started, "the test server never came up")
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    async def test_a_connection_gets_every_event_and_the_one_after_the_bad_one(self) -> None:
        last = 0
        last = self.poison("test_result", {"explanation": f"no {LONE} run"})
        for i in range(3):
            self.goals.publish(self.event("log", {"message": f"after {i} {LONE}"}))
        wanted = {e.sequence for e in self.goals.events_after(self.goal.id, 0)}
        self.assertGreater(len(wanted), 4)
        self.assertIn(last, wanted)

        wire = await websockets.connect(f"ws://127.0.0.1:{self.port}/ws/goals/{self.goal.id}")
        await wire.send(json.dumps({"type": "auth", "token": BOOT_TOKEN}))
        got: list[dict[str, Any]] = []
        try:
            while {f["sequence"] for f in got} != wanted:
                got.append(json.loads(await asyncio.wait_for(wire.recv(), 5)))
        except websockets.ConnectionClosed as closed:
            self.fail(f"the stream closed ({closed.rcvd.code if closed.rcvd else None}) after {len(got)} events")
        finally:
            await wire.close()

        self.assertEqual(sorted(wanted), [f["sequence"] for f in got])
        by_seq = {f["sequence"]: f for f in got}
        self.assertEqual(f"no {FFFD} run", by_seq[last]["payload"]["explanation"])

    async def test_a_second_connection_is_no_worse_off_than_the_first(self) -> None:
        # The loop the UI was in: every reconnect replayed up to the same event and died on it.
        self.poison("log", {"message": f"bad {LONE}"})
        self.goals.publish(self.event("log", {"message": "tail"}))
        wanted = len(self.goals.events_after(self.goal.id, 0))
        for _ in range(2):
            wire = await websockets.connect(f"ws://127.0.0.1:{self.port}/ws/goals/{self.goal.id}")
            await wire.send(json.dumps({"type": "auth", "token": BOOT_TOKEN}))
            frames = [json.loads(await asyncio.wait_for(wire.recv(), 5)) for _ in range(wanted)]
            await wire.close()
            self.assertEqual("tail", frames[-1]["payload"]["message"])


if __name__ == "__main__":
    unittest.main()
