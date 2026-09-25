"""The fake provider's prompt-reading helpers.

`scripts/fake_ollama.py` is the harness every manual smoke test and E2E
walkthrough runs against, and it is not a test double the suite controls: it is
a script a human points the engine at. That makes its replies easy to trust
without checking — a canned scribe reply naming a file from an older fixture
showed up in a live transcript as a commit subject contradicting the diff
directly above it, and nothing failed.

So the helpers that read the prompt are tested here. The routing that picks
between them still lives in the request handler (it needs a socket), but every
reply a human is expected to believe is produced by a function in this module.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any, cast

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fake_ollama.py"


def load_fake() -> ModuleType:
    """Import the script by path: `scripts/` is not a package, and importing it
    must not start the server (the module is guarded by `__main__`)."""
    spec = importlib.util.spec_from_file_location("fake_ollama_for_tests", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestScribeReply(unittest.TestCase):
    """The scribe describes what it was given — the diff the engine hands over."""

    def setUp(self) -> None:
        self.fake = load_fake()

    def reply(self, prompt: str) -> dict[str, Any]:
        # `cast` because the script is imported by path: mypy sees the module
        # attribute as `Any`, and this suite's strictness forbids leaking it.
        return cast(dict[str, Any], json.loads(self.fake.scribe_reply(prompt)))

    def test_it_names_the_file_the_engine_showed_it(self) -> None:
        prompt = "Step: Write DESIGN.md\npublish it\n\nDiffs:\nFile: DESIGN.md (create)\n+hello\n"
        reply = self.reply(prompt)
        self.assertEqual(reply["commit_message"], "feat: add DESIGN.md")
        self.assertIn("DESIGN.md", reply["summary"])

    def test_a_step_that_touched_nothing_keeps_the_canned_reply(self) -> None:
        # No `File:` lines means no diffs were handed over at all, which is the
        # shape the legacy canned reply exists for.
        self.assertEqual(
            json.loads(self.fake.scribe_reply("Step: Do a thing\n")),
            json.loads(self.fake.SCRIBE),
        )

    def test_a_multi_file_step_says_how_many(self) -> None:
        prompt = (
            "Diffs:\nFile: a.py (update)\n+x\nFile: b.py (update)\n+y\n"
        )
        reply = self.reply(prompt)
        self.assertEqual(reply["commit_message"], "chore: update 2 files")

    def test_a_deletion_is_a_removal_not_an_addition(self) -> None:
        prompt = "Diffs:\nFile: gone.txt (delete)\n-y\n"
        reply = self.reply(prompt)
        self.assertEqual(reply["commit_message"], "chore: remove gone.txt")

    def test_only_the_diff_section_is_read(self) -> None:
        # `File:` shapes appear elsewhere in real prompts (the librarian's
        # evidence), and reading those would name a file the step never touched.
        # One decoy mid-line and one at line start: only an implementation that
        # scopes to the block survives the second.
        prompt = (
            "What the librarian found:\n"
            "File: decoy.py (create)\n"
            "Summary: File: also-decoy.py (create) is a lie\n\n"
            "Diffs:\nFile: real.py (create)\n+x\n"
        )
        self.assertEqual(self.reply(prompt)["commit_message"], "feat: add real.py")


class TestFixerFiles(unittest.TestCase):
    """The fixer writes where the plan said, and echoes a handed-over draft."""

    def setUp(self) -> None:
        self.fake = load_fake()

    def files(self, prompt: str) -> list[dict[str, Any]]:
        payload = cast(dict[str, Any], json.loads(self.fake.fixer_files(prompt)))
        return cast(list[dict[str, Any]], payload["files"])

    def test_it_writes_the_first_suggested_path(self) -> None:
        prompt = (
            "Step: Add banner\n\nSuggested paths (current contents):\n"
            "banner.txt: hello\n"
        )
        self.assertEqual(self.files(prompt)[0]["path"], "banner.txt")

    def test_a_design_deliverable_is_echoed_verbatim(self) -> None:
        # "write exactly this" is an instruction the fake has to obey, or a live
        # design run looks like it drifted from its own contract.
        draft = "# fake-brand\n\nink #0d1117.\n"
        prompt = (
            "Step: Write DESIGN.md\n\n"
            "Suggested paths (current contents):\n"
            "(no readable suggested path)\n\n"
            f"--- DESIGN.md (write exactly this) ---\n{draft}--- end DESIGN.md ---\n\n"
        )
        written = self.files(prompt)[0]
        self.assertEqual(written["path"], "DESIGN.md")
        self.assertEqual(written["content"], draft)

    def test_an_ordinary_step_still_gets_the_canned_line(self) -> None:
        prompt = "Step: Add banner\n\nSuggested paths (current contents):\n(new file)\n"
        written = self.files(prompt)[0]
        self.assertEqual(written["path"], "banner.txt")
        self.assertEqual(written["content"], "hello from codify\n")


if __name__ == "__main__":
    unittest.main()
