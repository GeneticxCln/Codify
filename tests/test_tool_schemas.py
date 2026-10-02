"""Every tool the conductor offers is well-formed in every dialect it is sent in.

The four provider translations in `engine/providers.py` are the part of this
pipeline with no type checker, and their failure is someone else's HTTP 400 on a
tool the model needs. Nothing else in this suite sees it, because every other
test talks to a double that accepts whatever it is handed.

So this checks the document each provider is actually sent: built by the same
`ToolSpec.to_*` methods the providers call, over the real `TOOLS` tuple. It is
our reading of the providers' rules and not their validator, so it can say a
schema is well-formed and only a live call can say a provider accepts it. That
limit is why the negative controls below exist: a checker that finds nothing
wrong with anything looks identical to one that works.
"""

from __future__ import annotations

import unittest
from typing import Any

from engine.conductor import GIT_HISTORY, RUN_COMMAND, SEARCH_CODE, READ_FILE, TOOLS
from engine.toolcall import (
    DIALECTS,
    ToolSpec,
    _kept,
    coerce_arguments,
    schema_problems,
)


def _spec(properties: dict[str, Any], required: list[str] | None = None, name: str = "t") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="a tool",
        parameters={"type": "object", "properties": properties, "required": required or []},
    )


class TestEveryRealToolIsWellFormed(unittest.TestCase):
    def test_the_real_tools_are_checked_in_every_dialect(self) -> None:
        # A checker over an empty list passes. Eighteen tools today; the floor is
        # what makes "no problems" mean something.
        self.assertGreaterEqual(len(TOOLS), 18)
        failures: list[str] = []
        for spec in TOOLS:
            for dialect in DIALECTS:
                for problem in schema_problems(spec, dialect):
                    failures.append(f"{spec.name} [{dialect}]: {problem}")
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_the_two_array_tools_declare_their_items(self) -> None:
        for spec, key in ((GIT_HISTORY, "args"), (RUN_COMMAND, "argv")):
            declared = spec.parameters["properties"][key]
            self.assertEqual(declared.get("items"), {"type": "string"}, f"{spec.name}.{key}")


class TestTheCheckerCanFail(unittest.TestCase):
    """Negative controls: each rule must be able to say no."""

    def test_an_array_without_items_is_reported_in_every_dialect(self) -> None:
        spec = _spec({"args": {"type": "array", "description": "argv"}}, ["args"])
        for dialect in DIALECTS:
            problems = schema_problems(spec, dialect)
            self.assertTrue(any("no items" in p for p in problems), f"{dialect}: {problems}")

    def test_a_nested_array_without_items_is_found(self) -> None:
        spec = _spec(
            {"rows": {"type": "array", "items": {"type": "array"}}}, ["rows"]
        )
        self.assertTrue(any("no items" in p for p in schema_problems(spec, "openai")))

    def test_required_naming_a_missing_property_is_reported(self) -> None:
        spec = _spec({"a": {"type": "string"}}, ["a", "ghost"])
        self.assertTrue(
            any("ghost" in p for p in schema_problems(spec, "anthropic")),
            schema_problems(spec, "anthropic"),
        )

    def test_a_property_with_no_type_or_an_unknown_one_is_reported(self) -> None:
        self.assertTrue(schema_problems(_spec({"a": {"description": "x"}}), "openai"))
        self.assertTrue(schema_problems(_spec({"a": {"type": "text"}}), "openai"))

    def test_an_enum_member_of_the_wrong_type_is_reported(self) -> None:
        spec = _spec({"n": {"type": "integer", "enum": [1, "two"]}})
        self.assertTrue(any("two" in p for p in schema_problems(spec, "openai")))
        spec = _spec({"n": {"type": "integer", "enum": [True]}})
        self.assertTrue(schema_problems(spec, "openai"), "a bool is not an integer")

    def test_a_name_a_provider_would_refuse_is_reported(self) -> None:
        for bad in ("has space", "", "x" * 65, "dot.name"):
            spec = _spec({"a": {"type": "string"}}, name=bad)
            self.assertTrue(any("name" in p for p in schema_problems(spec, "openai")), bad)

    def test_an_empty_description_is_reported(self) -> None:
        spec = ToolSpec(name="t", description="  ", parameters={"type": "object", "properties": {}})
        self.assertTrue(any("description" in p for p in schema_problems(spec, "google")))

    def test_an_unknown_dialect_is_an_error_not_a_pass(self) -> None:
        with self.assertRaises(ValueError):
            schema_problems(_spec({}), "cohere")

    def test_what_the_google_translation_drops_is_reported(self) -> None:
        source = {"type": "object", "properties": {"mode": {
            "type": "string", "enum": ["keyword"], "description": "how",
        }}}
        shaped = {"type": "OBJECT", "properties": {"mode": {"type": "STRING"}}}
        problems = _kept(source, shaped, "parameters")
        self.assertTrue(any("description" in p for p in problems), problems)
        self.assertTrue(any("enum" in p for p in problems), problems)
        kept = {"type": "OBJECT", "properties": {"mode": {
            "type": "STRING", "enum": ["keyword"], "description": "how",
        }}}
        self.assertEqual(_kept(source, kept, "parameters"), [])


class TestGoogleKeepsWhatTheModelNeeds(unittest.TestCase):
    """Gemini was sent only each property's `type`, so it never saw a description."""

    def test_descriptions_and_enums_survive(self) -> None:
        read = READ_FILE.to_google()["parameters"]["properties"]
        self.assertEqual(read["path"]["description"], "workspace-relative path")
        search = SEARCH_CODE.to_google()["parameters"]["properties"]
        self.assertEqual(search["mode"]["enum"], ["keyword"])
        self.assertIn("BM25", search["mode"]["description"])

    def test_array_items_survive_and_are_upper_case(self) -> None:
        props = RUN_COMMAND.to_google()["parameters"]["properties"]
        self.assertEqual(props["argv"]["type"], "ARRAY")
        self.assertEqual(props["argv"]["items"], {"type": "STRING"})

    def test_an_array_that_declares_no_items_still_goes_out_with_some(self) -> None:
        # Google refuses an array that does not say what it holds; the translator supplies
        # STRING, and `schema_problems` still reports the spec so the defect is not hidden.
        spec = _spec({"args": {"type": "array", "description": "argv"}}, ["args"])
        sent = spec.to_google()["parameters"]["properties"]["args"]
        self.assertEqual(sent["items"], {"type": "STRING"})

    def test_an_enum_is_sent_as_strings_and_only_on_a_string_property(self) -> None:
        spec = _spec({"n": {"type": "integer", "enum": [1, 2]}, "s": {"type": "string", "enum": ["a"]}})
        sent = spec.to_google()["parameters"]["properties"]
        self.assertNotIn("enum", sent["n"])
        self.assertEqual(sent["s"]["enum"], ["a"])
        # An integer enum is something Gemini cannot be told, and the checker says so.
        self.assertTrue(any("enum" in p for p in schema_problems(spec, "google")))

    def test_required_is_still_a_subset_of_properties(self) -> None:
        for spec in TOOLS:
            shaped = spec.to_google()["parameters"]
            self.assertTrue(set(shaped["required"]) <= set(shaped["properties"]), spec.name)

    def test_nested_object_properties_are_translated(self) -> None:
        spec = _spec({"opts": {
            "type": "object",
            "properties": {"depth": {"type": "integer", "description": "how deep"}},
            "required": ["depth"],
        }}, ["opts"])
        opts = spec.to_google()["parameters"]["properties"]["opts"]
        self.assertEqual(opts["type"], "OBJECT")
        self.assertEqual(opts["properties"]["depth"]["description"], "how deep")
        self.assertEqual(opts["required"], ["depth"])
        self.assertEqual(schema_problems(spec, "google"), [])


class TestAnArrayArgumentArrivesInWhateverShapeTheModelSent(unittest.TestCase):
    def test_a_json_array_sent_as_a_string_is_parsed(self) -> None:
        # Small models do this: the argument is an array and they wrote it out as text.
        got = coerce_arguments(RUN_COMMAND, {"argv": '["pytest", "-q"]'})
        self.assertEqual(got["argv"], ["pytest", "-q"])

    def test_a_real_array_is_left_alone(self) -> None:
        got = coerce_arguments(GIT_HISTORY, {"args": ["log", "-5"]})
        self.assertEqual(got["args"], ["log", "-5"])

    def test_text_that_is_not_an_array_is_left_for_the_tool_to_refuse(self) -> None:
        # Not guessed at: `"pytest -q"` could be one argument or two, and the sandbox is the
        # thing that decides what an argv is.
        for raw in ("pytest -q", "not json", '{"a": 1}', "[1, 2"):
            got = coerce_arguments(RUN_COMMAND, {"argv": raw})
            self.assertEqual(got["argv"], raw, raw)

    def test_a_json_array_of_non_strings_is_left_alone(self) -> None:
        got = coerce_arguments(RUN_COMMAND, {"argv": "[1, 2]"})
        self.assertEqual(got["argv"], "[1, 2]")


if __name__ == "__main__":
    unittest.main()
