"""The release workflow's two security rules, held where they can be read.

A workflow is code that runs with `contents: write`, so the two failures this file
pins are the ones a review can miss: an action referenced by a moving tag (the tag
owner can change what runs), and a dispatch input pasted into a shell script (the
person who starts the run can change what runs). Neither is visible to `make ci`'s
tests, which is why they are held here, against the file itself.
"""

from __future__ import annotations

import re
import unittest
from collections.abc import Iterator
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"

# A commit, not a tag: 40 lowercase hex characters.
PINNED = re.compile(r"^[0-9a-f]{40}(\s|$)")


class TestReleaseWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.lines = self.text.splitlines()

    def test_every_action_is_pinned_to_a_commit(self) -> None:
        uses = [
            line.split("uses:", 1)[1].strip()
            for line in self.lines
            if re.match(r"^\s*(?:-\s*)?uses:", line)
        ]
        self.assertTrue(uses, "the workflow has no actions left to check")
        for use in uses:
            name, _, ref = use.partition("@")
            self.assertRegex(
                ref,
                PINNED,
                f"{name} is referenced by a moving tag rather than a commit: {use}",
            )

    def test_the_dispatch_tag_never_reaches_a_shell_script(self) -> None:
        # It may be an `env:` value (data) but never a `${{ }}` inside `run:`,
        # where GitHub substitutes it before the shell parses the line.
        for block in self._run_blocks():
            self.assertNotIn("${{ github.event.inputs.tag", block, block)
        self.assertIn("TAG_INPUT: ${{ github.event.inputs.tag }}", self.text)
        self.assertIn("Refusing to publish", self.text)
        self.assertIn(r"^v[0-9]+\.[0-9]+\.[0-9]+", self.text)

    def _run_blocks(self) -> Iterator[str]:
        """Every `run: |` body, by indentation, so a test can read the script."""
        index = 0
        while index < len(self.lines):
            if not re.match(r"^\s+run:\s*\|", self.lines[index]):
                index += 1
                continue
            indent = len(self.lines[index]) - len(self.lines[index].lstrip())
            body: list[str] = []
            index += 1
            while index < len(self.lines):
                line = self.lines[index]
                if line.strip() and len(line) - len(line.lstrip()) <= indent:
                    break
                body.append(line)
                index += 1
            yield "\n".join(body)
