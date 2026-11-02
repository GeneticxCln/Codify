"""A file edited while the fixer is answering is refused, not overwritten (audit gap 1).

The fixer sees the current contents of its step's suggested paths, then spends a model call — minutes of one,
on a local model. That window used to be a hole: the reply was applied against the bytes that were on disk
when the call *started*, so an edit a person made while waiting was silently replaced, and the step reported
COMPLETED. The evidence read now records `version_of` of the exact bytes it showed, and `apply` refuses a
path whose bytes no longer match.

The refusal is not a failed step. It is the same one-retry contract a refused path gets: the fixer is asked
once more, with a prompt rebuilt from the files as they are *now* (not the stale text it answered against).
A second move means the file is not stable enough to write, so the step stops and names it.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
from tests.test_fixer_reask import Factory, Gate, ReAskCase, ScriptedProvider

from engine.executor import ExecutorService
from engine.fs import FileMovedUnderProposalError, FileSystemService, version_of
from engine.models import ROLES, AgentConfigUpdate
from engine.providers import Keychain
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService

STALE = {"files": [{"path": "a.py", "action": "update", "content": "value = 2\n"}]}
# The person's edit, made while the first fixer call was in flight.
PERSON = "value = 99\n# kept by the person\n"
# What a fixer that was shown PERSON would write.
UPDATED = PERSON + "value = 100\n"
UPDATED_REPLY = {"files": [{"path": "a.py", "action": "update", "content": UPDATED}]}


class MovingProvider(ScriptedProvider):
    """Runs `move` before every fixer call, so a test can edit the file mid-flight."""

    def __init__(self, fixer_replies: list[Any], move: Callable[[int], None]) -> None:
        super().__init__(fixer_replies)
        self.move = move
        self.fixer_calls = 0

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
        json_schema: dict[str, Any] | None = None, thinking_effort: str | None = None,
    ) -> str:
        if "You are Codify Fixer" in system_prompt:
            self.fixer_calls += 1
            self.move(self.fixer_calls)
        return await super().complete(
            system_prompt, user_prompt, model, temperature, max_tokens,
            num_ctx=num_ctx, keep_alive=keep_alive, json_schema=json_schema,
            thinking_effort=thinking_effort,
        )


class FileMovedCase(ReAskCase):
    def build_moving(
        self, fixer_replies: list[Any], move: Callable[[int], None],
    ) -> MovingProvider:
        provider = MovingProvider(list(fixer_replies), move)
        self.provider = provider
        registry = AgentRegistryService(self.conn, Factory(provider), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(model_name="test-model"))
        self.executor = ExecutorService(
            self.goals, self.workspaces, registry, SandboxService(), laya=Gate(),
        )
        return provider


class TestAFileThatMovedWhileTheFixerAnswered(FileMovedCase):
    async def test_the_persons_edit_survives_and_the_retry_sees_it(self) -> None:
        def move(call: int) -> None:
            if call == 1:
                (self.root / "a.py").write_text(PERSON, encoding="utf-8")

        provider = self.build_moving([STALE, UPDATED_REPLY], move)

        status = await self.run_the_step()

        self.assertEqual("COMPLETED", status, self.events("error"))
        self.assertEqual(UPDATED, self.file, "the fixer overwrote the edit made while it was answering")
        self.assertEqual(2, len(provider.fixer_prompts), "the fixer was not asked exactly once more")
        retry = provider.fixer_prompts[1]
        self.assertIn("changed while you were answering", retry)
        self.assertIn(PERSON, retry, "the retry still carried the stale contents, not what is on disk now")
        self.assertIn("a.py", retry)

    async def test_a_second_move_stops_the_step_and_names_the_file(self) -> None:
        def move(call: int) -> None:
            (self.root / "a.py").write_text(f"value = {call}0\n", encoding="utf-8")

        provider = self.build_moving([STALE], move)

        status = await self.run_the_step()

        self.assertEqual("FAILED", status)
        self.assertEqual(2, len(provider.fixer_prompts), "a third ask would not be bounded by the contract")
        errors = self.events("error")
        self.assertEqual("agent_output_invalid", errors[-1]["code"])
        self.assertIn("a.py", errors[-1]["message"])
        self.assertIn("changed again", errors[-1]["message"])
        self.assertEqual("value = 20\n", self.file, "a refused reply wrote something")

    async def test_a_missing_path_records_the_empty_create_baseline(self) -> None:
        self.build()
        fs = FileSystemService(str(self.root))
        versions: dict[str, str | None] = {}

        self.executor._suggested_paths_context(fs, ["a.py", "new.py"], versions=versions)

        self.assertEqual(version_of((self.root / "a.py").read_bytes()), versions["a.py"])
        self.assertEqual("", versions["new.py"], "a path that did not exist must be recognised as absent")

    async def test_the_version_guard_matches_the_same_path_spelled_differently(self) -> None:
        """A planner's `./a.py` must guard the fixer's `a.py` (and the reverse).

        The versions map used to be keyed by the planner's spelling and looked up by the
        fixer's exact spelling, so an alias skipped the check and overwrote the person's
        edit made while the fixer was answering.
        """
        self.build()
        fs = FileSystemService(str(self.root))
        (self.root / "sub").mkdir()
        (self.root / "sub" / "b.py").write_text("value = 1\n", encoding="utf-8")
        edited = "value = 99\n# person's edit\n"

        for suggested, written in (("./a.py", "a.py"), ("sub//b.py", "sub/b.py")):
            with self.subTest(suggested=suggested, written=written):
                versions: dict[str, str | None] = {}
                self.executor._suggested_paths_context(fs, [suggested], versions=versions)
                (self.root / written).write_text(edited, encoding="utf-8")

                with self.assertRaises(FileMovedUnderProposalError):
                    fs.apply(
                        [{"path": written, "action": "update", "content": "value = 2\n"}],
                        dry_run=False,
                        expected_versions=versions,
                    )

                self.assertEqual(edited, (self.root / written).read_text(encoding="utf-8"))

    async def test_the_version_guard_matches_an_absolute_spelling(self) -> None:
        """A planner can name an in-workspace file by its absolute path.

        The guard keyed it as `home/user/ws/a.py`, which never matched the fixer's
        `a.py`, so the person's mid-call edit was overwritten.
        """
        self.build()
        fs = FileSystemService(str(self.root))
        absolute = str((self.root / "a.py").resolve())
        edited = "value = 99\n# person's edit\n"
        versions: dict[str, str | None] = {}

        self.executor._suggested_paths_context(fs, [absolute], versions=versions)
        self.assertEqual(version_of((self.root / "a.py").read_bytes()), versions[absolute])
        (self.root / "a.py").write_text(edited, encoding="utf-8")

        with self.assertRaises(FileMovedUnderProposalError):
            fs.apply(
                [{"path": "a.py", "action": "update", "content": "value = 2\n"}],
                dry_run=False,
                expected_versions=versions,
            )

        self.assertEqual(edited, (self.root / "a.py").read_text(encoding="utf-8"))

    async def test_case_distinct_files_do_not_share_a_version_key(self) -> None:
        """`A.py` and `a.py` are two files where the filesystem says so.

        The keys used to be case-folded, so a version recorded for `A.py` guarded a
        write to `a.py` and the write was refused as "moved" when nothing had moved.
        """
        self.build()
        fs = FileSystemService(str(self.root))
        upper = self.root / "A.py"
        lower = self.root / "a.py"
        upper.write_text("upper = 1\n", encoding="utf-8")
        lower.write_text("lower = 1\n", encoding="utf-8")
        if upper.read_text(encoding="utf-8") == lower.read_text(encoding="utf-8"):
            self.skipTest("this filesystem is case-insensitive, so A.py and a.py are one file")
        versions: dict[str, str | None] = {"A.py": version_of(upper.read_bytes())}

        fs.apply(
            [{"path": "a.py", "action": "update", "content": "lower = 2\n"}],
            dry_run=False,
            expected_versions=versions,
        )

        self.assertEqual("lower = 2\n", lower.read_text(encoding="utf-8"))
        self.assertEqual("upper = 1\n", upper.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
