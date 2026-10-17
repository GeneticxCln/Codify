"""The conductor's `audit_usage`: what it is offered as, what it answers, and what it cannot be made to do.

`tests/test_usage_audit.py` holds the audit. This file holds what stands between a model and it: the menu, the one argument it takes,
the sentences it answers with, that it only reads, and the skill that tells the model how to report what it finds honestly.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import time
import unittest
from typing import Any

from engine.chat_prompts import CONDUCTOR_SYSTEM_PROMPT
from engine.conductor import AUDIT_USAGE, BASE_TOOLS, STEP_TOOLS
from engine.conductor_tools import ConductorTools
from engine.models import Event
from engine.skills import builtin_skills, load_skills
from tests.test_conductor import ConductorTestCase, _ToolProvider


class AuditToolCase(ConductorTestCase):
    def table(self) -> dict[str, Any]:
        return self._dispatch(self.goal.id)

    _n = 0

    def put(self, kind: str, payload: dict[str, Any], at: float | None = None, step: str | None = None) -> None:
        AuditToolCase._n += 1
        self.goals.publish(Event(
            id=f"au{AuditToolCase._n}", goal_id=self.goal.id, step_id=step, type=kind, payload=payload,
            timestamp=time.time() if at is None else at, sequence=self.goals.next_sequence(self.goal.id),
        ))

    def seed_reask(self) -> None:
        self.put("usage", {"role": "planner", "provider": "p", "model": "m", "input_tokens": 600, "output_tokens": 100})
        self.put("agent_call_failed", {"role": "planner", "provider": "p", "model": "m", "code": "agent_output_invalid", "retrying": True})
        self.put("usage", {"role": "planner", "provider": "p", "model": "m", "input_tokens": 650, "output_tokens": 90})
        self.put("stage_result", {"stage": "planner", "role": "planner", "outcome": "plan", "tokens": 1440})


class TestItIsOnTheMenuLikeTheOtherReadTools(AuditToolCase):
    async def test_it_is_a_base_tool_the_table_answers_to_and_no_setting_gates_it(self) -> None:
        self.assertIn("audit_usage", [t.name for t in BASE_TOOLS])
        self.assertNotIn("audit_usage", [t.name for t in STEP_TOOLS])
        self.assertIn("audit_usage", ConductorTools.NAMES)
        self.assertIn("audit_usage", self.table())
        executor = self._executor(_ToolProvider())
        for planned in (False, True):
            if planned:
                executor._insert_steps(self.goal.id, [{"title": "S", "description": "d", "suggested_paths": ["a.py"]}])
            self.assertIn("audit_usage", [t.name for t in executor.conductor_menu(self.goal.id)()])

    async def test_it_takes_one_optional_window_and_nothing_that_reaches_further(self) -> None:
        props = AUDIT_USAGE.parameters["properties"]
        self.assertEqual({"days"}, set(props))
        self.assertEqual("integer", props["days"]["type"])
        self.assertNotIn("required", AUDIT_USAGE.parameters)

    async def test_the_description_and_the_prompt_say_it_is_tokens_and_an_empty_answer_is_not_no_waste(self) -> None:
        description = " ".join(AUDIT_USAGE.description.split())
        self.assertIn("in TOKENS", description)
        self.assertIn("keeps no prices", description)
        self.assertIn("never 'no waste'", description)
        self.assertIn("Read-only", description)
        self.assertIn("`audit_usage` reports wasted model calls in tokens; check a finding before acting", " ".join(CONDUCTOR_SYSTEM_PROMPT.split()))


class TestWhatACallAnswers(AuditToolCase):
    async def test_a_seeded_reask_is_reported_as_waste_with_the_first_calls_tokens(self) -> None:
        self.seed_reask()
        answer = await self.table()["audit_usage"]({})
        self.assertIn("retry-waste: 700 tokens in 1 re-asked call(s) whose step then succeeded", answer)
        self.assertIn("Coverage", answer)
        self.assertLess(answer.index("Coverage"), answer.index("Findings"))

    async def test_an_empty_workspace_still_says_what_it_could_not_see(self) -> None:
        answer = await self.table()["audit_usage"]({})
        self.assertIn("every matching event was read", answer)
        self.assertIn("tool-loop-waste: UNAVAILABLE", answer)
        self.assertIn("cache-churn: UNAVAILABLE", answer)
        self.assertIn("says nothing about the detectors marked UNAVAILABLE", answer)

    async def test_days_bounds_the_window_and_a_bad_value_is_all_time_not_an_error(self) -> None:
        self.put("usage", {"role": "planner", "input_tokens": 5_000, "output_tokens": 0}, at=time.time() - 30 * 86_400)
        self.put("usage", {"role": "planner", "input_tokens": 70, "output_tokens": 0})
        recent = await self.table()["audit_usage"]({"days": 7})
        everything = await self.table()["audit_usage"]({})
        self.assertIn("Total recorded: 1 calls, 70 tokens", recent)
        self.assertIn("Total recorded: 2 calls, 5,070 tokens", everything)
        for odd in ("7", True, None, 3.5, -3, 10**9, [1]):
            answer = await self.table()["audit_usage"]({"days": odd})
            self.assertTrue(answer.startswith("Usage audit for this workspace"), repr(odd))

    async def test_there_is_no_dollar_figure_anywhere_in_an_answer(self) -> None:
        self.seed_reask()
        answer = await self.table()["audit_usage"]({})
        for token in ("$", "USD", "EUR"):
            self.assertNotIn(token, answer)


class TestItOnlyReads(AuditToolCase):
    def counts(self) -> dict[str, int]:
        tables = [r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}  # noqa: S608 — table names from sqlite_master

    async def test_a_call_changes_nothing_but_one_log_line(self) -> None:
        self.seed_reask()
        before = self.counts()
        await self.table()["audit_usage"]({"days": 30})
        after = self.counts()
        changed = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
        self.assertEqual({"events": (before["events"], before["events"] + 1)}, changed)
        last = self.conn.execute("SELECT type, payload FROM events ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual("log", last["type"])
        self.assertIn("conductor audited usage", last["payload"])


class TestTheUsageAuditSkill(AuditToolCase):
    def skill(self) -> Any:
        skills, problems = builtin_skills(ConductorTools.NAMES)
        self.assertEqual(problems, [])
        return {s.name: s for s in skills}["usage-audit"]

    async def test_it_ships_and_every_move_it_names_is_a_tool_that_exists(self) -> None:
        skill = self.skill()
        self.assertEqual(skill.source, "built-in")
        self.assertEqual(skill.moves, ("audit_usage", "recall"))
        self.assertEqual(skill.notes, ())
        self.assertIn("usage-audit", load_skills(str(self.repo)).names())

    async def test_it_carries_the_honesty_rules_it_was_written_to_carry(self) -> None:
        body = " ".join(self.skill().body.split())
        self.assertIn("Say what it could not see first", body)
        self.assertIn('Never turn "no findings" into "no waste"', body)
        self.assertIn("A re-ask that did not recover is spend", body)
        self.assertIn("Check before you recommend", body)
        self.assertIn("Do not state a dollar amount", body)

    async def test_it_changes_nothing_and_names_no_writing_move(self) -> None:
        body = " ".join(self.skill().body.split())
        self.assertNotIn("`write`", body)
        self.assertNotIn("delegate", body)
        self.assertIn("Do not change settings, files or agent configuration", body)

    async def test_use_skill_returns_it_with_no_note_because_every_move_it_names_is_on_the_menu(self) -> None:
        answer = await self.table()["use_skill"]({"name": "usage-audit"})
        self.assertIn("# Usage audit", answer)
        self.assertNotIn("Note from the engine", answer)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
