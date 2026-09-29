"""`extract_json` reads what small local models actually write (audit of 2026-09-29, H4).

It was "first `{` or `[` to last `}` or `]`, then `json.loads`", and it failed on every shape below
that a real model produces: a `<think>…</think>` block that mentions braces (Qwen3, DeepSeek-R1 — the
README's own `qwen3:8b` example), an example object before the real one, a trailing comma, single
quotes and Python `None`/`True`, and output cut off by `max_tokens`. Every role's contract is JSON, so
each of those was a failed step, and with no fallback configured a failed goal — the single largest
reliability risk for the stated target of small local models.

The corpus here is *written from* the shapes those model families are documented to produce and that
the audit reproduced; it is **not** captured from a live model, and says so. Real captures live in
`tests/fixtures/model_replies/` (see its README) and are checked by the same assertions at the bottom.

What "right" means is stated per case: the object the role's contract asked for, not merely *an* object.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.executor import REPLY_KEYS, REPLY_TOLERATES_TRUNCATION, extract_json

STEPS = {"steps": [{"title": "Rename greeting", "description": "change it", "suggested_paths": ["hello.py"]}]}
STEPS_JSON = json.dumps(STEPS)
PLANNER = REPLY_KEYS["planner"]

# (name, raw reply, expected keys for the role, the object that must come back)
CASES: list[tuple[str, str, tuple[str, ...], Any]] = [
    ("plain object", STEPS_JSON, (), STEPS),
    ("leading and trailing whitespace and a BOM", "﻿\n  " + STEPS_JSON + "  \n", (), STEPS),
    ("fenced as json", f"```json\n{STEPS_JSON}\n```", (), STEPS),
    ("fenced with no language", f"```\n{STEPS_JSON}\n```", (), STEPS),
    ("prose before and after", f"Sure! Here is the plan:\n{STEPS_JSON}\nLet me know if you want changes.", (), STEPS),
    ("fence with prose around it", f"Here you go.\n\n```json\n{STEPS_JSON}\n```\n\nHope that helps!", (), STEPS),
    # ── reasoning models ─────────────────────────────────────────────────────────────────
    (
        "a think block whose reasoning mentions braces",
        "<think>The user wants {a plan}. I should reply as {\"steps\": [...]} and keep it short.</think>\n" + STEPS_JSON,
        PLANNER, STEPS,
    ),
    (
        "a think block that contains a complete example object",
        '<think>An example of the shape: {"steps": [{"title": "x"}]}. Now the real one.</think>' + STEPS_JSON,
        PLANNER, STEPS,
    ),
    (
        "a thinking block in the other spelling, over several lines",
        "<thinking>\nstep 1: read\nstep 2: {plan}\n</thinking>\n" + STEPS_JSON,
        PLANNER, STEPS,
    ),
    ("an upper-case THINK tag", "<THINK>hmm {x}</THINK>" + STEPS_JSON, PLANNER, STEPS),
    ("a think block that never closes, with the answer after it", "<think>let me consider\n" + STEPS_JSON, PLANNER, STEPS),
    # ── more than one object ────────────────────────────────────────────────────────────
    (
        "an example object, then the real one — the role's keys decide",
        'For instance {"example": true} would look like that.\n' + STEPS_JSON,
        PLANNER, STEPS,
    ),
    ("the real object, then a trailing remark object", STEPS_JSON + '\n{"note": "done"}', PLANNER, STEPS),
    ("no expected keys given: the last object wins", '{"draft": 1}\n{"final": 2}', (), {"final": 2}),
    ("a footnote-style list in the prose before the object", "As discussed [1] earlier:\n" + STEPS_JSON, PLANNER, STEPS),
    # ── near-JSON ────────────────────────────────────────────────────────────────────────
    ("a trailing comma in an object", '{"steps": [{"title": "a", "description": "b", "suggested_paths": [],},],}', PLANNER,
     {"steps": [{"title": "a", "description": "b", "suggested_paths": []}]}),
    ("single quotes and Python literals", "{'applies': True, 'direction': None, 'tokens': ['a', 'b']}", ("applies",),
     {"applies": True, "direction": None, "tokens": ["a", "b"]}),
    ("JSON with a line comment", '{\n  "verdict": "pass", // it ran\n  "argv": null\n}', ("verdict",), {"verdict": "pass", "argv": None}),
    ("JSON with a block comment", '{"verdict": /* checked */ "fail", "explanation": "x"}', ("verdict",), {"verdict": "fail", "explanation": "x"}),
    ("a comma inside a string is left alone", '{"files": [{"path": "a.txt", "action": "create", "content": "a, b,],}"}],}',
     ("files",), {"files": [{"path": "a.txt", "action": "create", "content": "a, b,],}"}]}),
    # ── strings that look like structure ──────────────────────────────────────────────────
    (
        "braces and quotes inside a string value",
        json.dumps({"files": [{"path": "a.py", "action": "create", "content": "def f():\n    return {'a': [1, 2]}  # }\n"}]}),
        ("files",), {"files": [{"path": "a.py", "action": "create", "content": "def f():\n    return {'a': [1, 2]}  # }\n"}]},
    ),
    ("unicode is preserved", '{"summary": "café — naïve 🙂"}', ("summary",), {"summary": "café — naïve 🙂"}),
    ("a list at the top level", '[{"title": "a"}, {"title": "b"}]', (), [{"title": "a"}, {"title": "b"}]),
]


# A reply cut off by `max_tokens`: what was finished is kept and the element that was not is dropped —
# never completed, never a string closed. (name, raw, expect, expected)
TRUNCATED: list[tuple[str, str, tuple[str, ...], Any]] = [
    (
        "cut off mid-string in the last array element",
        '{"steps": [{"title": "a", "description": "b", "suggested_paths": ["x"]}, {"title": "c", "descr',
        PLANNER, {"steps": [{"title": "a", "description": "b", "suggested_paths": ["x"]}]},
    ),
    (
        "cut off mid-value in the last array element",
        '{"steps": [{"title": "a", "description": "b", "suggested_paths": []}, {"title": "c", "description": "half a sen',
        PLANNER, {"steps": [{"title": "a", "description": "b", "suggested_paths": []}]},
    ),
    ("cut off right after a comma", '{"steps": [{"title": "a", "description": "b", "suggested_paths": []},', PLANNER,
     {"steps": [{"title": "a", "description": "b", "suggested_paths": []}]}),
    ("only the outer closer is missing after a closed container", '{"steps": [{"title": "a", "description": "b", "suggested_paths": []}]',
     PLANNER, {"steps": [{"title": "a", "description": "b", "suggested_paths": []}]}),
    ("a last scalar might be unfinished (`tru`), so it is dropped, not trusted", '{"summary": "ok", "enough": true', ("summary",), {"summary": "ok"}),
    ("cut off inside a field of a plain object", '{"summary": "ok", "direction": "wri', ("summary",), {"summary": "ok"}),
    ("cut off in the very first element", '{"steps": [{"title": "a", "descr', PLANNER, {"steps": []}),
    ("cut off before any value", '{"verdict": "pass", "explanation": ', ("verdict",), {"verdict": "pass"}),
]


class TestTheShapesModelsProduce(unittest.TestCase):
    def test_every_shape_in_the_corpus_yields_the_object_the_contract_asked_for(self) -> None:
        for name, raw, expect, expected in CASES:
            with self.subTest(shape=name):
                self.assertEqual(expected, extract_json(raw, expect))

    def test_the_corpus_covers_the_fifteen_shapes_the_audit_named_and_more(self) -> None:
        self.assertGreaterEqual(len(CASES), 22)


class TestACutOffReply(unittest.TestCase):
    def test_what_was_finished_is_kept_and_the_unfinished_element_is_dropped(self) -> None:
        for name, raw, expect, expected in TRUNCATED:
            with self.subTest(shape=name):
                self.assertEqual(expected, extract_json(raw, expect, repair_truncation=True))

    def test_without_the_flag_a_cut_off_reply_is_refused_not_quietly_shortened(self) -> None:
        for name, raw, expect, _ in TRUNCATED:
            with self.subTest(shape=name):
                with self.assertRaises(ValueError) as caught:
                    extract_json(raw, expect)
                self.assertIn("ends before the document does", str(caught.exception))

    def test_a_string_is_never_closed_so_a_half_written_file_is_never_made_whole(self) -> None:
        raw = '{"files": [{"path": "a.py", "action": "create", "content": "def f():\\n    retu'
        repaired = extract_json(raw, REPLY_KEYS["fixer"], repair_truncation=True)
        self.assertEqual({"files": []}, repaired)
        self.assertNotIn("retu", json.dumps(repaired))

    def test_the_fixer_is_the_one_role_that_may_not_have_its_reply_repaired_this_way(self) -> None:
        self.assertNotIn("fixer", REPLY_TOLERATES_TRUNCATION)
        self.assertEqual(set(REPLY_KEYS) - {"fixer"}, set(REPLY_TOLERATES_TRUNCATION))


class TestWhatIsRefusedAndHowItSaysSo(unittest.TestCase):
    def refused(self, raw: str, expect: tuple[str, ...] = ()) -> str:
        with self.assertRaises(ValueError) as caught:
            extract_json(raw, expect)
        return str(caught.exception)

    def test_an_empty_reply_says_it_was_empty(self) -> None:
        self.assertIn("empty", self.refused(""))
        self.assertIn("empty", self.refused("   \n"))

    def test_prose_only_says_there_was_no_json(self) -> None:
        self.assertIn("no JSON", self.refused("I'm sorry, I can't help with that."))

    def test_nothing_but_unfinished_reasoning_says_so(self) -> None:
        message = self.refused("<think>let me think about this for a long time and never")
        self.assertIn("reasoning", message)

    def test_an_object_that_cannot_be_repaired_names_what_was_wrong_with_it(self) -> None:
        self.assertIn("JSON", self.refused('{"a" 1}'))

    def test_a_reply_that_is_valid_json_but_not_an_object_is_not_returned_as_one(self) -> None:
        # A bare string or number is JSON and is not what any role's contract asks for.
        self.refused('"just a string"')
        self.refused("42")


class TestNothingChangesForWhatAlreadyWorked(unittest.TestCase):
    def test_a_well_formed_reply_is_returned_exactly(self) -> None:
        payload = {"files": [{"path": "a.py", "action": "update", "content": "x = 1\n"}], "n": 3, "ok": True, "none": None}
        self.assertEqual(payload, extract_json(json.dumps(payload), REPLY_KEYS["fixer"]))
        self.assertEqual(payload, extract_json(json.dumps(payload, indent=2)))

    def test_the_expected_keys_are_a_preference_not_a_requirement(self) -> None:
        # A reply with none of the role's keys is still returned: the contract check that says
        # "the planner must return steps" belongs to the role, and reports it in the role's words.
        self.assertEqual({"other": 1}, extract_json('{"other": 1}', PLANNER))

    def test_every_role_that_replies_in_json_has_expected_keys_named_once(self) -> None:
        self.assertEqual(
            {"librarian", "design", "planner", "fixer", "verifier", "critic", "scribe"}, set(REPLY_KEYS),
        )
        for role, keys in REPLY_KEYS.items():
            with self.subTest(role=role):
                self.assertTrue(keys and all(isinstance(k, str) for k in keys))


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_replies"


class TestCapturedReplies(unittest.TestCase):
    """Replies captured from real models, each next to the object it must parse to.

    A fixture is `<name>.txt` (the raw reply, byte for byte) and `<name>.json` with
    `{"role": ..., "model": ..., "expect": <the parsed object>}`. The directory may hold none yet;
    when it holds some, every one is held to the same rule as the corpus above.
    """

    def test_every_captured_reply_parses_to_what_was_recorded(self) -> None:
        for raw_path in sorted(FIXTURES.glob("*.txt")):
            meta = json.loads(raw_path.with_suffix(".json").read_text(encoding="utf-8"))
            with self.subTest(fixture=raw_path.name, model=meta["model"]):
                parsed = extract_json(raw_path.read_text(encoding="utf-8"), REPLY_KEYS[meta["role"]])
                self.assertEqual(meta["expect"], parsed)


if __name__ == "__main__":
    unittest.main()
