"""The workflow, the Makefile and `pyproject.toml` say the same things about what the gate needs.

`.github/workflows/check.yml` opens by claiming that the union of its jobs is exactly the targets
`make check` depends on, and `make help`, `.PHONY` and the dev-tool lists each restate part of the
same contract. Every one of them has drifted at least once: a hand-written tool list in the workflow
that was a second copy of `pyproject.toml`'s `dev` extra, a Node version that said "22" over a comment
that said "22.6+" when the floor was 22.22.2, a `smoke-tabs` target missing from `.PHONY`, and a smoke
benchmark that `make check` never ran. A comment promising they stay in step is not a mechanism; this is.

Parsing is plain text (no YAML library, no `tomllib`): the suite has to pass on the declared 3.10
floor, and the files are line-oriented enough that a small extractor is honest. Each extractor asserts
it found what it looked for, so a moved file or a renamed key fails here instead of comparing two
empty lists.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")


def _makefile_prerequisites(target: str) -> list[str]:
    match = re.search(rf"^{re.escape(target)}:(?!=)([^\n#]*)", MAKEFILE, re.M)
    if not match:
        raise AssertionError(f"no `{target}:` rule in the Makefile")
    return match.group(1).split()


def _pyproject_dev_extra() -> list[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"^dev\s*=\s*\[(.*?)^\]", text, re.S | re.M)
    if not match:
        raise AssertionError("no `dev = [...]` array in pyproject.toml")
    specs = re.findall(r'"([^"]+)"', re.sub(r"#.*", "", match.group(1)))
    if not specs:
        raise AssertionError("pyproject.toml's dev extra parsed to nothing")
    return specs


class TestTheMakefileDescribesItself(unittest.TestCase):
    def test_every_rule_is_phony_and_every_phony_name_is_a_rule(self) -> None:
        phony = re.search(r"^\.PHONY:(.*)$", MAKEFILE, re.M)
        assert phony is not None, "no .PHONY line"
        declared = set(phony.group(1).split())
        rules = set(re.findall(r"^([A-Za-z0-9_-]+):(?!=)", MAKEFILE, re.M))

        self.assertEqual(set(), rules - declared, "targets that are not in .PHONY (a file of that name would stop them running)")
        self.assertEqual(set(), declared - rules, ".PHONY names with no rule")

    def test_help_says_what_check_runs(self) -> None:
        line = next(
            (ln for ln in MAKEFILE.splitlines() if "@echo" in ln and "make check " in ln and "Run all verifications" in ln),
            None,
        )
        assert line is not None, "make help has no line for `make check`"
        for word in ("ruff", "mypy", "UI tests", "UI test typecheck", "Python tests", "stream", "UI build", "Tauri"):
            self.assertIn(word, line, f"`make help` describes `make check` without {word!r}")


class TestTheWorkflowRunsWhatMakeCheckRuns(unittest.TestCase):
    def test_every_target_make_check_depends_on_is_a_step(self) -> None:
        steps = set(re.findall(r"^\s+run:\s+make\s+([a-z0-9-]+)\s*$", WORKFLOW, re.M))
        wanted = set(_makefile_prerequisites("check"))
        self.assertTrue(wanted, "`check` has no prerequisites")

        self.assertEqual(set(), wanted - steps, "make check runs these and the workflow does not")

    def test_the_floor_legs_make_ci_adds_are_steps_too(self) -> None:
        steps = set(re.findall(r"^\s+run:\s+make\s+([a-z0-9-]+)\s*$", WORKFLOW, re.M))
        floors = {t for t in _makefile_prerequisites("ci") if t.endswith("-floor")}
        self.assertTrue(floors, "`ci` has no floor legs")

        # The Python floor is the matrix's 3.10 entry, not a step of its own.
        self.assertEqual(set(), floors - steps - {"ci-python-floor"})
        self.assertIn('python: ["3.10"', WORKFLOW)

    def test_the_workflow_also_runs_the_smoke_benchmark(self) -> None:
        # Not a prerequisite of `check` (docs/08 §3), so the test above cannot see it.
        self.assertIn("make bench-smoke", WORKFLOW)

    def test_the_rust_job_installs_clippy(self) -> None:
        self.assertRegex(WORKFLOW, r"components:\s*rustfmt,\s*clippy")


class TestOneListOfWhatTheGateNeeds(unittest.TestCase):
    def test_the_python_job_installs_the_dev_extra_and_names_no_tool_by_hand(self) -> None:
        self.assertIn('pip install -e ".[dev]"', WORKFLOW)
        for spec in _pyproject_dev_extra():
            name = re.split(r"[<>=!~\[;@ ]", spec, maxsplit=1)[0]
            self.assertNotRegex(
                WORKFLOW, rf'pip install[^\n]*"{re.escape(name)}\b',
                f"the workflow installs {name} by hand; it is in pyproject.toml's dev extra",
            )

    def test_the_floor_script_installs_only_what_the_dev_extra_declares_verbatim(self) -> None:
        script = (ROOT / "scripts" / "ci-python-floor.sh").read_text(encoding="utf-8")
        match = re.search(r"^tools=\((.*?)\)", script, re.S | re.M)
        assert match is not None, "no `tools=( ... )` array in scripts/ci-python-floor.sh"
        tools = re.findall(r'"([^"]+)"', match.group(1))
        self.assertTrue(tools)

        self.assertEqual(set(), set(tools) - set(_pyproject_dev_extra()), "the floor installs a tool pyproject.toml does not declare, or at another version")


class TestTheNodeFloorIsTheOneTheUiDeclares(unittest.TestCase):
    def test_the_ui_job_asks_for_the_lowest_node_the_package_allows(self) -> None:
        engines = json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))["engines"]["node"]
        floors = re.findall(r"\^(\d+\.\d+\.\d+)", engines)
        assert floors, f"no ^X.Y.Z in engines.node {engines!r}"
        lowest = min(floors, key=lambda v: tuple(int(p) for p in v.split(".")))
        asked = re.search(r'node-version:\s*"([^"]+)"', WORKFLOW)
        assert asked is not None, "the ui job names no node-version"

        self.assertEqual(lowest, asked.group(1), "the ui job does not test the Node floor the package declares")
        self.assertNotIn("22.6+", WORKFLOW, "the workflow still gives 22.6 as the floor")


if __name__ == "__main__":
    unittest.main()
