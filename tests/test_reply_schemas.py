"""The reply schema and the parser that reads the reply cannot drift apart (`docs/16` item 5).

A schema is a second description of a contract that already exists in code, so it is only worth sending to a
provider if something checks that the two descriptions agree. These tests are that check: every key a parser
reads is a required property, every object is closed and lists all of its properties as required (what strict
structured output demands), and a field that is optional is nullable rather than absent.

They do not prove a provider honours the schema — no unit test can. They prove the document this engine sends
describes the document its parsers accept.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from engine.models import ROLES
from engine.replies import REPLY_KEYS, REPLY_SCHEMAS


def _objects(node: Any, where: str) -> list[tuple[str, dict[str, Any]]]:
    """Every object schema under `node`, with a dotted path for a failure message."""
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append((where, node))
            for key, child in (node.get("properties") or {}).items():
                found.extend(_objects(child, f"{where}.{key}"))
        elif node.get("type") == "array":
            found.extend(_objects(node.get("items"), f"{where}[]"))
        for union in ("anyOf", "oneOf", "allOf"):
            for index, child in enumerate(node.get(union) or []):
                found.extend(_objects(child, f"{where}<{union}:{index}>"))
    return found


class TestEveryRoleHasASchema(unittest.TestCase):
    def test_the_table_covers_every_role(self) -> None:
        self.assertEqual(set(ROLES), set(REPLY_SCHEMAS))
        # `REPLY_KEYS` is the subset `extract_json` uses to find the contract object; it must not name a role
        # this table forgot, or a reply would be asked for in a shape that was never described.
        self.assertEqual(set(REPLY_KEYS), set(REPLY_SCHEMAS) - {"laya"})

    def test_every_schema_is_a_closed_object_and_serializes(self) -> None:
        for role, schema in REPLY_SCHEMAS.items():
            with self.subTest(role=role):
                self.assertEqual("object", schema.get("type"))
                self.assertIs(False, schema.get("additionalProperties"))
                self.assertEqual(set(schema["properties"]), set(schema["required"]))
                # The provider's `response_format` carries this as JSON; a set or a callable here would be a
                # schema that cannot be sent at all.
                json.dumps(schema)


class TestTheSchemaMatchesItsParser(unittest.TestCase):
    # Every top-level key a role's parser or executor reads. Deliberately written out rather than derived from
    # `REPLY_KEYS`: `REPLY_KEYS` is for *finding* the object in prose, and it is missing keys the parser needs
    # (`conventions`, `risks`, `needs_another_pass`) as well as naming keys the parser never reads (`reads` and
    # `searches` at a planner's top level). A schema built from it would describe the wrong document.
    PARSED: dict[str, set[str]] = {
        "laya": {"answers"},
        "librarian": {
            "summary", "files", "symbols", "conventions", "test_command", "risks",
            "reads", "searches", "git", "run", "enough",
        },
        "design": {
            "applies", "artifact", "direction", "design_system", "tokens",
            "components", "conventions", "constraints", "acceptance", "design_md",
        },
        "planner": {"steps", "consult"},
        "fixer": {"files", "needs_another_pass"},
        "verifier": {"argv", "verdict", "explanation"},
        "critic": {"decision", "reasons", "run_command"},
        "scribe": {"summary", "commit_message"},
    }

    # The inner objects whose fields a parser reads by name.
    NESTED: tuple[tuple[str, str, set[str]], ...] = (
        ("laya", "answers", {"intent", "risk", "prompt_injection", "needs_clarification"}),
        ("laya", "answers.intent", {"choice", "confidence"}),
        ("laya", "answers.risk", {"score", "confidence"}),
        ("laya", "answers.prompt_injection", {"noul"}),
        ("laya", "answers.needs_clarification", {"noul"}),
        ("librarian", "files[]", {"path", "why"}),
        ("librarian", "symbols[]", {"name", "path"}),
        ("design", "design_system", {"name", "source"}),
        ("design", "tokens", {"colors", "typography", "spacing", "radii"}),
        ("design", "tokens.colors[]", {"name", "value"}),
        ("design", "tokens.typography[]", {"name", "value"}),
        ("design", "components[]", {"name", "purpose"}),
        ("planner", "steps[]", {"title", "description", "suggested_paths"}),
        ("planner", "consult", {"reads", "searches", "git", "run"}),
        ("fixer", "files[]", {"path", "action", "content", "edits"}),
        ("fixer", "files[].edits[]", {"old_text", "new_text", "count"}),
    )

    @staticmethod
    def _at(schema: dict[str, Any], path: str) -> dict[str, Any]:
        """The object schema at a dotted path, stepping through arrays and nullability."""
        node: Any = schema
        for part in path.split("."):
            is_array = part.endswith("[]")
            if is_array:
                part = part[:-2]
            while isinstance(node, dict) and "anyOf" in node:
                node = node["anyOf"][0]
            node = node["properties"][part]
            while isinstance(node, dict) and "anyOf" in node:
                node = node["anyOf"][0]
            if is_array:
                node = node["items"]
                while isinstance(node, dict) and "anyOf" in node:
                    node = node["anyOf"][0]
        assert isinstance(node, dict)
        return node

    def test_every_key_a_parser_reads_is_required(self) -> None:
        for role, keys in self.PARSED.items():
            with self.subTest(role=role):
                schema = REPLY_SCHEMAS[role]
                self.assertEqual(set(), keys - set(schema["required"]), f"{role} parser reads an unrequired key")

    def test_every_nested_key_a_parser_reads_is_required(self) -> None:
        for role, path, keys in self.NESTED:
            with self.subTest(role=role, path=path):
                blocked = self._at(REPLY_SCHEMAS[role], path)
                self.assertEqual("object", blocked.get("type"), f"{path} is not an object schema")
                self.assertEqual(set(), keys - set(blocked["required"]), f"{path} parser reads an unrequired key")

    def test_every_object_is_strict(self) -> None:
        # Strict mode rejects an object that is not closed or that leaves a property optional; a nested object
        # that breaks the rule makes the whole request a 400 on the strictest providers.
        for role, schema in REPLY_SCHEMAS.items():
            for where, node in _objects(schema, role):
                with self.subTest(role=role, object=where):
                    self.assertIs(False, node.get("additionalProperties"), f"{where} is open")
                    self.assertEqual(
                        set(node.get("properties") or {}), set(node.get("required") or []),
                        f"{where} has an optional property; make it nullable and required instead",
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
