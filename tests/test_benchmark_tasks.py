"""Every real-model benchmark task can be failed, and can be passed (audit of 2026-09-29, 3.3).

A task whose checks pass on the untouched repository measures nothing, and one whose checks nothing can
satisfy measures the checks. Neither shows up until a model has been paid to run against it, so both are
proved here, on the committed fixture, with no model:

* on the repository as it ships, **every `repo_scale` task has a quality check that fails** — doing
  nothing is not a pass;
* given a hand-written **reference solution** (the change a competent person makes), **every quality check
  passes** — the checks say what the description asks, and can be met.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from benchmarks.runner import DEFAULT_MANIFEST, load_manifest, materialize, run_check, split_checks

APP = "src/app.py"
TESTS = "tests/test_app.py"


def edit(path: str, old: str, new: str) -> tuple[str, str, str]:
    return (path, old, new)


def write(path: str, text: str) -> tuple[str, None, str]:
    return (path, None, text)


# task id -> the change that satisfies it. `(path, old, new)` replaces text in an existing file;
# `(path, None, text)` writes a file whole.
REFERENCE: dict[str, list[tuple[str, str | None, str]]] = {
    "repo-rename-greeting": [
        edit(APP, "def greet", "def welcome"), edit(APP, "return greet(", "return welcome("),
        edit(TESTS, "import greet, shout", "import welcome, shout"),
        edit(TESTS, "greet(", "welcome("),
    ],
    "repo-close-the-gap": [edit(APP, '"""A deliberately small module for the benchmark fixture to act on."""\n',
                                '"""A deliberately small module for the benchmark fixture to act on."""\n\n__all__ = ["greet", "shout"]\n')],
    "repo-add-whisper": [
        edit(APP, "\n\ndef shout", '\n\ndef whisper(name: str) -> str:\n    return greet(name).lower()\n\n\ndef shout'),
        edit(TESTS, "import greet, shout", "import greet, shout, whisper"),
        edit(TESTS, "\n\nif __name__", '\n    def test_whisper(self) -> None:\n        self.assertEqual(whisper("WORLD"), "hello world")\n\n\nif __name__'),
    ],
    "repo-default-name": [edit(APP, "def greet(name: str)", 'def greet(name: str = "world")')],
    "repo-add-version": [edit(APP, "\n\ndef greet", '\n\nVERSION = "0.1.0"\n\n\ndef greet')],
    "repo-add-clamp": [write("src/util.py", "def clamp(value, low, high):\n    return max(low, min(value, high))\n")],
    "repo-readme-usage": [edit("README.md", "## Layout", "## Usage\n\n```python\ngreet(\"world\")\n```\n\n## Layout")],
    "repo-shout-exclaim": [
        edit(APP, "return greet(name).upper()", 'return greet(name).upper() + "!"'),
        edit(TESTS, '"HELLO WORLD"', '"HELLO WORLD!"'),
    ],
    "repo-remove-shout": [
        edit(APP, '\n\ndef shout(name: str) -> str:\n    """Upper-cases the greeting. Unfinished on purpose: a task tier needs\n    something real to change, and a fixture that is already correct can only\n    prove that nothing broke."""\n    return greet(name).upper()\n', ""),
        edit(TESTS, "import greet, shout", "import greet"),
        edit(TESTS, '    def test_shout_is_the_loud_version(self) -> None:\n        self.assertEqual(shout("world"), "HELLO WORLD")\n', ""),
    ],
    "repo-word-count": [edit(APP, "\n\ndef shout", "\n\ndef word_count(text: str) -> int:\n    return len(text.split())\n\n\ndef shout")],
    "repo-changelog": [write("CHANGELOG.md", "# Changelog\n\n## 0.1.0\n\n- First release.\n")],
}


def repo_scale_tasks() -> list[dict[str, Any]]:
    return [t for t in load_manifest(DEFAULT_MANIFEST)["tasks"] if t.get("tier") == "repo_scale"]


def quality_results(task: dict[str, Any], workspace: Path) -> list[tuple[str, str, str]]:
    _, quality = split_checks(task)
    return [
        (str(check.get("type")), *run_check(check, workspace, [], "COMPLETED", [])[:2])
        for check in quality
    ]


class TestRealModelTasks(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_there_are_at_least_ten_tasks_and_each_has_a_reference_solution(self) -> None:
        ids = {t["id"] for t in repo_scale_tasks()}

        self.assertGreaterEqual(len(ids), 10)
        self.assertEqual(ids, set(REFERENCE), "a task has no reference solution, or a solution has no task")

    def test_doing_nothing_passes_no_task(self) -> None:
        for task in repo_scale_tasks():
            with self.subTest(task=task["id"]):
                workspace = materialize("synthetic-repo", self.tmp / f"untouched-{task['id']}")
                failed = [r for r in quality_results(task, workspace) if r[1] == "failed"]
                self.assertTrue(failed, f"{task['id']} is passed by the repository as it ships")

    def test_the_reference_solution_passes_every_quality_check(self) -> None:
        for task in repo_scale_tasks():
            with self.subTest(task=task["id"]):
                workspace = materialize("synthetic-repo", self.tmp / f"solved-{task['id']}")
                for path, old, new in REFERENCE[task["id"]]:
                    target = workspace / path
                    if old is None:
                        target.write_text(new, encoding="utf-8")
                        continue
                    text = target.read_text(encoding="utf-8")
                    self.assertIn(old, text, f"{task['id']}: the reference solution's anchor is missing from {path}")
                    target.write_text(text.replace(old, new), encoding="utf-8")

                results = quality_results(task, workspace)

                self.assertEqual([], [r for r in results if r[1] != "passed"], f"{task['id']}: {results}")


if __name__ == "__main__":
    unittest.main()
