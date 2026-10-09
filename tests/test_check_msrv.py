"""`scripts/check_msrv.py`: a declared Rust version the dependency graph contradicts is a failure.

The script exists because `rust-version = "1.77.2"` sat in Cargo.toml while the locked graph needed 1.88, and nothing
compared the two. It takes `cargo metadata` JSON on stdin, so these tests hand it graphs and need no cargo.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import unittest
from typing import Any

from scripts.check_msrv import parse_version, problems


def graph(declared: str | None, *dependencies: tuple[str, str | None]) -> dict[str, Any]:
    root: dict[str, Any] = {"id": "root", "name": "codify-desktop", "version": "0.2.0", "rust_version": declared}
    deps = [
        {"id": name, "name": name, "version": "1.0.0", "rust_version": needs}
        for name, needs in dependencies
    ]
    return {"packages": [root, *deps], "resolve": {"root": "root"}}


class TestTheDeclaredVersionAgainstTheGraph(unittest.TestCase):
    def test_a_declared_version_below_a_dependency_is_named_with_the_fix(self) -> None:
        found = problems(graph("1.77.2", ("darling", "1.88.0"), ("serde", "1.56")))
        self.assertEqual(len(found), 1)
        self.assertIn("declares rust-version 1.77.2", found[0])
        self.assertIn("darling 1.0.0 needs 1.88.0", found[0])
        self.assertIn("set rust-version to at least 1.88.0", found[0])

    def test_a_declared_version_that_meets_or_exceeds_the_graph_passes(self) -> None:
        self.assertEqual(problems(graph("1.88.0", ("darling", "1.88.0"))), [])
        self.assertEqual(problems(graph("1.90", ("darling", "1.88.0"))), [])

    def test_versions_compare_as_versions_not_as_text(self) -> None:
        # "1.9" is above "1.88" as text and below it as a version.
        self.assertEqual(problems(graph("1.9", ("darling", "1.88.0")))[0].count("needs 1.88.0"), 1)
        self.assertLess(parse_version("1.9"), parse_version("1.88"))
        self.assertEqual(parse_version("1.88"), parse_version("1.88.0"))

    def test_dependencies_that_declare_nothing_are_not_a_requirement(self) -> None:
        self.assertEqual(problems(graph("1.77.2", ("old", None))), [])

    def test_a_graph_with_no_root_or_no_declaration_fails_rather_than_passing_vacuously(self) -> None:
        self.assertEqual(len(problems({"packages": [], "resolve": {"root": None}})), 1)
        found = problems(graph(None, ("darling", "1.88.0")))
        self.assertEqual(len(found), 1)
        self.assertIn("declares no rust-version", found[0])

    def test_the_real_declaration_is_the_one_the_script_checks(self) -> None:
        # The checked-in file says what the graph needs at the time of writing; the gate (`make check-tauri`) is
        # what keeps it true. This pins that the file was not left at the old, unmeetable number.
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        declared = next(line for line in text.splitlines() if line.startswith("rust-version"))
        self.assertNotIn("1.77", declared)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
