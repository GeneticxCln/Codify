"""A fixer reply the engine cannot apply is asked for again — once — with the reason (audit of 2026-09-29, 3.3).

Found by running Qwen2.5-1.5B against the benchmark's `repo-rename-greeting`: the model answered
`{"action": "edit", "edits": [{"old_text": "greet", "new_text": "welcome", "count": 1}]}` for a file
in which `greet` appears three times. The engine's answer — `old_text appears 2 time(s), expected 1` — is
precise and the model can act on it, but it went to the goal as a failure: the same-model re-ask (H4) only
covered replies that were not JSON, so the one case where the engine knows exactly what to say was the one
case where it said it to a person instead of to the model.

Rules pinned here: a reply that parsed but could not be *applied* (an edit that matches the wrong number of
times or not at all, an entry the contract refuses) gets one re-ask of the same fixer, carrying the original
task, the reason, and the reply it refers to; nothing is written by the reply that failed; a reply that
applied is never re-asked; and after the second failure the step fails exactly as it did before.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.db import connect
from engine.executor import ExecutorService
from engine.laya import GateCall, LayaDecision, LayaService
from engine.models import ROLES, AgentConfig, AgentConfigUpdate, GoalCreate, WorkspaceCreate
from engine.providers import BaseProvider, Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService

SOURCE = 'def greet(name):\n    return f"hello {name}"\n\n\ndef shout(name):\n    return greet(name).upper()\n'
AMBIGUOUS = {"files": [{"path": "a.py", "action": "edit",
                        "edits": [{"old_text": "greet", "new_text": "welcome", "count": 1}]}]}
CORRECTED = {"files": [{"path": "a.py", "action": "edit",
                        "edits": [{"old_text": "greet", "new_text": "welcome", "count": 0}]}]}


class ScriptedProvider(BaseProvider):
    """Answers each role from a list (the last entry repeats), and remembers every fixer prompt."""

    def __init__(self, fixer_replies: list[Any]) -> None:
        self.fixer_replies = list(fixer_replies)
        self.fixer_prompts: list[str] = []
        self.roles = {
            "librarian": {"summary": "s", "files": [], "conventions": [], "enough": True},
            "design": {"applies": False},
            "planner": {"steps": [{"title": "Rename", "description": "rename greet", "suggested_paths": ["a.py"]}]},
            "verifier": {"argv": None, "verdict": "pass", "explanation": "nothing to run"},
            "critic": {"decision": "approve", "reasons": []},
            "scribe": {"summary": "did it", "commit_message": "feat: rename"},
        }

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> str:
        role = next((r for r in ROLES if f"You are Codify {r.capitalize()}" in system_prompt), "unknown")
        if role == "fixer":
            self.fixer_prompts.append(user_prompt)
            step = self.fixer_replies.pop(0) if len(self.fixer_replies) > 1 else self.fixer_replies[0]
            return step if isinstance(step, str) else json.dumps(step)
        return json.dumps(self.roles.get(role, {}))


class Factory(ProviderFactory):
    def __init__(self, provider: BaseProvider) -> None:
        super().__init__(Keychain())
        self.provider = provider

    def build(self, config: AgentConfig) -> BaseProvider:
        return self.provider


class Gate(LayaService):
    async def decide(self, state: dict[str, Any], on_call: GateCall | None = None) -> LayaDecision:
        return LayaDecision(engine="skipped", skipped_reason="test double")


class ReAskCase(unittest.IsolatedAsyncioTestCase):
    def build(self, *fixer_replies: Any) -> ScriptedProvider:
        self.provider = ScriptedProvider(list(fixer_replies))
        registry = AgentRegistryService(self.conn, Factory(self.provider), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(model_name="test-model"))
        self.executor = ExecutorService(self.goals, self.workspaces, registry, SandboxService(), laya=Gate())
        return self.provider

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "a.py").write_text(SOURCE, encoding="utf-8")
        self.conn = connect(self.root / "t.db")
        self.addCleanup(self.conn.close)
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="T", description=""))

    async def run_the_step(self) -> str:
        g = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, g.version, "RUNNING")
        self.executor._insert_steps(
            self.goal.id, [{"title": "Rename", "description": "rename greet", "suggested_paths": ["a.py"]}]
        )
        step = self.goals.steps(self.goal.id)[0]
        await self.executor.run_step(self.goal.id, step.id)
        return self.goals.steps(self.goal.id)[0].status

    def events(self, type_: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.goals.events_after(self.goal.id, 0) if e.type == type_]

    @property
    def file(self) -> str:
        return (self.root / "a.py").read_text(encoding="utf-8")


class TestAnEditThatCouldNotBeApplied(ReAskCase):
    async def test_the_reason_goes_back_to_the_model_and_the_corrected_reply_is_used(self) -> None:
        provider = self.build(AMBIGUOUS, CORRECTED)

        status = await self.run_the_step()

        self.assertEqual(2, len(provider.fixer_prompts), "the same fixer should have been asked exactly twice")
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual(SOURCE.replace("greet", "welcome"), self.file)

    async def test_the_second_prompt_carries_the_task_the_reason_and_the_reply_it_refers_to(self) -> None:
        provider = self.build(AMBIGUOUS, CORRECTED)

        await self.run_the_step()

        first, second = provider.fixer_prompts
        self.assertTrue(second.startswith(first), "the repair must carry the original task unchanged")
        self.assertIn("appears 2 time(s), expected 1", second, "the engine's own reason was not passed on")
        self.assertIn('"old_text": "greet"', second, "the model was not shown the reply the reason is about")

    async def test_nothing_is_written_by_the_reply_that_failed(self) -> None:
        # The second reply also fails, so whatever is on disk afterwards came from a failed reply.
        self.build(AMBIGUOUS)

        await self.run_the_step()

        self.assertEqual(SOURCE, self.file)

    async def test_the_first_failure_is_recorded_as_a_failed_call_that_was_retried(self) -> None:
        self.build(AMBIGUOUS, CORRECTED)

        await self.run_the_step()

        failed = [e for e in self.events("agent_call_failed") if e["role"] == "fixer"]
        self.assertEqual(1, len(failed))
        self.assertEqual("agent_output_invalid", failed[0]["code"])
        self.assertTrue(failed[0]["retrying"])
        self.assertIn("appears 2 time(s)", failed[0]["message"])

    async def test_a_reply_that_applied_is_never_asked_for_twice(self) -> None:
        provider = self.build(CORRECTED)

        await self.run_the_step()

        self.assertEqual(1, len(provider.fixer_prompts))
        self.assertEqual([], [e for e in self.events("agent_call_failed") if e["role"] == "fixer"])

    async def test_after_the_second_failure_the_step_fails_as_it_always_did(self) -> None:
        provider = self.build(AMBIGUOUS)

        status = await self.run_the_step()

        self.assertEqual("FAILED", status)
        self.assertEqual(2, len(provider.fixer_prompts), "asked more than the one repair")
        errors = self.events("error")
        self.assertEqual("agent_output_invalid", errors[-1]["code"])
        self.assertEqual("fixer", errors[-1]["role"])
        self.assertIn("appears 2 time(s), expected 1", errors[-1]["message"])
        self.assertIn("after one repair attempt", errors[-1]["message"])


class TestAnEntryTheContractRefuses(ReAskCase):
    async def test_a_file_entry_with_an_unknown_action_is_asked_for_again(self) -> None:
        bad = {"files": [{"path": "a.py", "action": "rewrite", "content": "x = 1\n"}]}
        good = {"files": [{"path": "a.py", "action": "update", "content": "x = 1\n"}]}
        provider = self.build(bad, good)

        status = await self.run_the_step()

        self.assertEqual(2, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual("x = 1\n", self.file)
        self.assertIn("fixer file entry invalid", provider.fixer_prompts[1])


class TestAFilesListOfTheWrongShape(ReAskCase):
    async def asked_again_and_recovers(self, wrong: Any) -> None:
        # The first real-model run produced `files` holding lists, and the parser raised AttributeError.
        good = {"files": [{"path": "a.py", "action": "update", "content": "x = 1\n"}]}
        provider = self.build(wrong, good)

        status = await self.run_the_step()

        self.assertEqual(2, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual("x = 1\n", self.file)

    async def test_an_entry_that_is_a_list(self) -> None:
        await self.asked_again_and_recovers({"files": [["a.py", "update", "x = 1\n"]]})

    async def test_an_entry_that_is_a_string(self) -> None:
        await self.asked_again_and_recovers({"files": ["a.py"]})

    async def test_an_entry_that_is_null(self) -> None:
        await self.asked_again_and_recovers({"files": [None]})

    async def test_a_reply_that_is_a_bare_list_of_something_else(self) -> None:
        await self.asked_again_and_recovers([["a.py", "update", "x = 1\n"]])

    async def test_a_bare_list_of_file_entries_is_used_without_asking_again(self) -> None:
        # What Qwen2.5-1.5B actually answered, twice in a row when asked to fix it: the contract's array
        # with no object around it. The meaning is unambiguous, so it is read, and no second call is paid for.
        provider = self.build([{"path": "a.py", "action": "update", "content": "x = 1\n"}])

        status = await self.run_the_step()

        self.assertEqual(1, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual("x = 1\n", self.file)

    async def test_the_same_slip_twice_is_invalid_output_not_an_internal_error(self) -> None:
        self.build({"files": [["a.py", "update", "x = 1\n"]]})

        status = await self.run_the_step()

        self.assertEqual("FAILED", status)
        self.assertEqual("agent_output_invalid", self.events("error")[-1]["code"])


class TestAFieldOfTheWrongType(ReAskCase):
    """An entry of the right shape whose `path` or `content` is not a string.

    `TestAFilesListOfTheWrongShape` covers entries that are not objects. These are objects that pass every check the
    parser made and then fail inside `FileSystemService.apply` with an `AttributeError` (`'list' object has no
    attribute 'splitlines'`), which the re-ask does not catch: the step failed as an internal error. A file written
    as an array of its lines is the commonest of them.
    """

    GOOD = {"files": [{"path": "a.py", "action": "update", "content": "x = 1\n"}]}

    async def asked_again_and_recovers(self, wrong: Any, said: str) -> None:
        provider = self.build(wrong, self.GOOD)

        status = await self.run_the_step()

        self.assertEqual(2, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual("x = 1\n", self.file)
        self.assertIn(said, provider.fixer_prompts[1], "the model was not told what was wrong")

    async def test_content_written_as_a_list_of_lines(self) -> None:
        wrong = {"files": [{"path": "a.py", "action": "update", "content": ["x = 1", ""]}]}
        await self.asked_again_and_recovers(wrong, "must be one string")

    async def test_content_that_is_a_number_or_an_object(self) -> None:
        for content in (1, 2.5, True, {"text": "x = 1"}):
            with self.subTest(content=content):
                await self.asyncTearDown_and_up()
                await self.asked_again_and_recovers(
                    {"files": [{"path": "a.py", "action": "create", "content": content}]}, "must be one string",
                )

    async def test_a_path_that_is_a_list_or_an_object(self) -> None:
        for path in (["a.py"], {"file": "a.py"}, 7):
            with self.subTest(path=path):
                await self.asyncTearDown_and_up()
                await self.asked_again_and_recovers(
                    {"files": [{"path": path, "action": "update", "content": "x = 1\n"}]}, "path must be a string",
                )

    async def test_an_edit_count_that_is_not_a_finite_number(self) -> None:
        # `Infinity` is JSON as Python reads it, and `int()` of it is an OverflowError, not a ValueError.
        wrong = (
            '{"files": [{"path": "a.py", "action": "edit", '
            '"edits": [{"old_text": "greet", "new_text": "hello", "count": Infinity}]}]}'
        )
        provider = self.build(wrong, self.GOOD)

        status = await self.run_the_step()

        self.assertEqual(2, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual("x = 1\n", self.file)

    async def test_the_same_slip_twice_is_invalid_output_not_an_internal_error(self) -> None:
        self.build({"files": [{"path": "a.py", "action": "update", "content": ["x = 1"]}]})

        status = await self.run_the_step()

        self.assertEqual("FAILED", status)
        self.assertEqual("agent_output_invalid", self.events("error")[-1]["code"])
        self.assertEqual(SOURCE, self.file, "a reply that was refused wrote something")

    async def test_a_null_content_still_means_an_empty_file(self) -> None:
        # Not a type error: `create` with no content is how a model makes an empty file.
        provider = self.build({"files": [{"path": "empty.txt", "action": "create", "content": None}]})

        status = await self.run_the_step()

        self.assertEqual(1, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual("", (self.root / "empty.txt").read_text(encoding="utf-8"))

    async def asyncTearDown_and_up(self) -> None:
        """A fresh workspace and goal for the next case of a loop, because a step runs once per goal."""
        self.temp_dir.cleanup()
        self.conn.close()
        await self.asyncSetUp()


class TestAPathTheWorkspaceRefuses(ReAskCase):
    """A path the model wrote in the wrong form: absolute, climbing out, or inside `.git`.

    The first real-model run wrote `"/src/app.py"` and `"/tests/test_app.py"` — a workspace-relative path with
    a slash in front, the commonest small-model slip there is. The engine refused it, correctly, and the step
    failed with `path_escape`; but the refusal names exactly what to change, so it goes back to the model.
    The refusal itself does not move: nothing outside the workspace is ever written, and a step that still
    cannot name a legal path fails with the same `path_escape` code it always had.
    """

    async def recovers(self, wrong_path: str) -> None:
        wrong = {"files": [{"path": wrong_path, "action": "update", "content": "x = 1\n"}]}
        good = {"files": [{"path": "a.py", "action": "update", "content": "x = 1\n"}]}
        provider = self.build(wrong, good)

        status = await self.run_the_step()

        self.assertEqual(2, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual("x = 1\n", self.file)
        self.assertIn("relative to the workspace root", provider.fixer_prompts[1])
        self.assertIn(wrong_path, provider.fixer_prompts[1], "the refusal did not name the path")

    async def test_a_leading_slash_is_put_to_the_model(self) -> None:
        await self.recovers("/a.py")

    async def test_a_path_that_climbs_out_is_put_to_the_model(self) -> None:
        await self.recovers("../a.py")

    async def test_a_path_inside_git_is_put_to_the_model(self) -> None:
        await self.recovers(".git/hooks/pre-commit")

    async def test_the_same_slip_twice_fails_with_the_code_it_always_had_and_writes_nothing(self) -> None:
        provider = self.build({"files": [{"path": "/etc/cron.d/evil", "action": "create", "content": "x\n"}]})

        status = await self.run_the_step()

        self.assertEqual("FAILED", status)
        self.assertEqual(2, len(provider.fixer_prompts))
        error = self.events("error")[-1]
        self.assertEqual("path_escape", error["code"])
        self.assertEqual("fixer", error["role"])
        self.assertIn("after one repair attempt", error["message"])
        self.assertEqual(SOURCE, self.file)
        self.assertFalse((self.root / "etc").exists(), "an absolute path was quietly made relative and written")

    async def test_a_protected_workspace_root_is_not_put_to_the_model(self) -> None:
        # That refusal is about where the workspace *is*, not about anything the model wrote: asking again
        # cannot change it.
        from unittest import mock

        from engine.fs import FileSystemService, ProtectedRootError

        provider = self.build({"files": [{"path": "a.py", "action": "update", "content": "x\n"}]})
        with mock.patch.object(FileSystemService, "apply", side_effect=ProtectedRootError("/", "the machine")):
            status = await self.run_the_step()

        self.assertEqual("FAILED", status)
        self.assertEqual(1, len(provider.fixer_prompts))
        self.assertEqual("path_escape", self.events("error")[-1]["code"])


class TestALoneSurrogate(ReAskCase):
    """A reply whose JSON spells a lone surrogate (`"\\ud800"`) cannot be saved as UTF-8, and is asked about once.

    A real run used to be re-asked with the codec's own sentence, which names no file; a dry run (a plan-only goal)
    passed the engine's checks, failed later when the proposal was stored, and so was never re-asked, ended with
    the wrong error code, and left the rows stored before the bad one for Apply to replay.
    """

    BAD_UPDATE = {"path": "a.py", "action": "update", "content": "x\ud800y"}
    NEW_FILE = {"path": "new.py", "action": "create", "content": "ok\n"}
    GOOD = {"files": [{"path": "a.py", "action": "update", "content": "x = 2\n"}]}

    def proposed(self) -> list[Any]:
        return self.conn.execute("SELECT path, action FROM proposed_files").fetchall()

    async def test_a_real_run_is_asked_again_with_the_file_named_in_words(self) -> None:
        provider = self.build({"files": [self.NEW_FILE, self.BAD_UPDATE]}, self.GOOD)

        status = await self.run_the_step()

        self.assertEqual(2, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        second = provider.fixer_prompts[1]
        self.assertIn("'a.py'", second, "the reason did not name the file")
        self.assertIn("lone surrogate", second)
        self.assertNotIn("codec", second)
        self.assertEqual("x = 2\n", self.file)
        self.assertFalse((self.root / "new.py").exists(), "the batch was half applied")

    async def test_a_reason_that_names_a_path_with_one_in_it_can_still_be_sent(self) -> None:
        # The file is named in the reason the model is asked about, and a reason with a lone surrogate in it
        # cannot be encoded into the request that carries it: the re-ask would die in the provider, not in us.
        wrong_shape = {"path": "a\ud800.py", "action": "update", "content": ["x = 1"]}
        provider = self.build({"files": [wrong_shape]}, self.GOOD)

        status = await self.run_the_step()

        self.assertEqual(2, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        second = provider.fixer_prompts[1]
        second.encode("utf-8")  # raises if the reason carried the surrogate through
        self.assertIn("a\\ud800.py", second, "the reason should show the escape, not drop the name")
        self.assertEqual("x = 2\n", self.file)

    async def test_a_dry_run_is_asked_again_and_proposes_nothing_in_part(self) -> None:
        self.goals.set_dry_run(self.goal.id, True)
        provider = self.build({"files": [self.NEW_FILE, self.BAD_UPDATE]})

        status = await self.run_the_step()

        self.assertEqual("FAILED", status)
        self.assertEqual(2, len(provider.fixer_prompts), "a dry run was not asked again")
        self.assertEqual("agent_output_invalid", self.events("error")[-1]["code"])
        self.assertEqual([], self.proposed(), "a refused batch left part of a proposal for Apply to replay")
        self.assertEqual(SOURCE, self.file)

    async def test_a_dry_run_that_is_corrected_stores_the_corrected_proposal_only(self) -> None:
        self.goals.set_dry_run(self.goal.id, True)
        self.build({"files": [self.NEW_FILE, self.BAD_UPDATE]}, self.GOOD)

        status = await self.run_the_step()

        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual([("a.py", "update")], [tuple(r) for r in self.proposed()])

    async def test_a_path_that_would_be_a_non_utf8_filename_is_asked_again(self) -> None:
        provider = self.build({"files": [{"path": "b\udc80.py", "action": "create", "content": "x\n"}]}, self.GOOD)

        status = await self.run_the_step()

        self.assertEqual(2, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual(["a.py", "t.db"], sorted(p.name for p in self.root.iterdir() if not p.name.startswith("t.db-")))

    async def test_the_new_text_of_an_edit_is_asked_again(self) -> None:
        edit = {"files": [{"path": "a.py", "action": "edit",
                           "edits": [{"old_text": "greet", "new_text": "wel\ud800come", "count": 0}]}]}
        provider = self.build(edit, CORRECTED)

        status = await self.run_the_step()

        self.assertEqual(2, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertIn("'a.py'", provider.fixer_prompts[1])
        self.assertEqual(SOURCE.replace("greet", "welcome"), self.file)

    async def test_a_surrogate_pair_is_one_character_and_is_not_asked_about(self) -> None:
        pair = '{"files": [{"path": "a.py", "action": "update", "content": "smile \\ud83d\\ude00\\n"}]}'
        provider = self.build(pair)

        status = await self.run_the_step()

        self.assertEqual(1, len(provider.fixer_prompts))
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual("smile \U0001f600\n", self.file)


class TestAVerifierThatHasNothingToRun(ReAskCase):
    """`"argv": []` is "no command", not a malformed command.

    Qwen2.5-1.5B answered `{"argv": [], "verdict": "skip", "explanation": null}` on three of eleven tasks and each
    failed the step with "verifier argv must be a non-empty string list". The contract's spelling for "nothing to
    run" is null, but an empty list can only mean the same thing, and running nothing is the one reading that
    can do no harm.
    """

    async def verdict_for(self, reply: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        provider = self.build({"files": [{"path": "a.py", "action": "update", "content": "x = 1\n"}]})
        provider.roles["verifier"] = reply

        status = await self.run_the_step()

        return status, self.events("test_result")

    async def test_an_empty_argv_is_no_command(self) -> None:
        status, results = await self.verdict_for({"argv": [], "verdict": "skip", "explanation": None})

        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual("skip", results[-1]["verdict"])
        self.assertFalse(results[-1]["ran"])

    async def test_an_empty_string_is_no_command_too(self) -> None:
        status, results = await self.verdict_for({"argv": "", "verdict": "skip", "explanation": "nothing to run"})

        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertFalse(results[-1]["ran"])

    async def test_a_real_command_that_the_sandbox_refuses_is_still_refused(self) -> None:
        # `python3 -c` is not on the allowlist (docs/04 §5). Asking to run it is a proposal, not "no command",
        # and it must fail the way it always did.
        status, _ = await self.verdict_for(
            {"argv": ["python3", "-c", "print(1)"], "verdict": "pass", "explanation": None}
        )

        self.assertEqual("FAILED", status)
        self.assertIn("sandbox refused", self.events("error")[-1]["message"])


class TestAnEditThatIsReallyAWholeFile(ReAskCase):
    """`"action": "edit"` with a full `content` and no `edits` is a whole-file write in the wrong spelling.

    The commonest fixer reply Qwen2.5-1.5B produced (13 of 43 file entries in the recorded baseline): `edit`, a
    complete new file in `content`, and no `edits`. The contract says `edit` needs edits, so every one of these
    failed the step, and asking again did not help — the model does not know what `edit` is for. The content is
    what it meant to write; `update` is what that is called. Only when there is content to write: an empty one
    would blank the file, and an `edit` that carries edits keeps its contract meaning.
    """

    async def test_edit_with_content_and_no_edits_writes_the_content_without_asking_again(self) -> None:
        provider = self.build({"files": [{"path": "a.py", "action": "edit", "content": "x = 1\n"}]})

        status = await self.run_the_step()

        self.assertEqual(1, len(provider.fixer_prompts), "a usable reply was asked for again")
        self.assertNotEqual("FAILED", status, self.events("error"))
        self.assertEqual("x = 1\n", self.file)

    async def test_the_reinterpretation_is_said_out_loud(self) -> None:
        self.build({"files": [{"path": "a.py", "action": "edit", "content": "x = 1\n", "edits": None}]})

        await self.run_the_step()

        notes = [e["message"] for e in self.events("log") if "whole-file" in e["message"]]
        self.assertEqual(1, len(notes), "the engine changed what the reply meant and did not say so")
        self.assertIn("a.py", notes[0])

    async def test_edit_with_edits_keeps_its_contract_meaning_and_ignores_content(self) -> None:
        provider = self.build({"files": [{
            "path": "a.py", "action": "edit", "content": "THE WRONG FILE\n",
            "edits": [{"old_text": "greet(name).upper()", "new_text": "greet(name).lower()", "count": 1}],
        }]})

        await self.run_the_step()

        self.assertEqual(1, len(provider.fixer_prompts))
        self.assertEqual(SOURCE.replace("greet(name).upper()", "greet(name).lower()"), self.file)

    async def test_an_empty_content_is_never_a_whole_file_write(self) -> None:
        provider = self.build({"files": [{"path": "a.py", "action": "edit", "content": ""}]})

        status = await self.run_the_step()

        self.assertEqual("FAILED", status)
        self.assertEqual(2, len(provider.fixer_prompts), "an edit with nothing to do was not asked for again")
        self.assertEqual(SOURCE, self.file, "the file was blanked")

    async def test_the_reask_names_the_alternative_when_an_edit_will_not_apply(self) -> None:
        provider = self.build(AMBIGUOUS, CORRECTED)

        await self.run_the_step()

        self.assertIn('action "update"', provider.fixer_prompts[1])
        self.assertIn("count to 0", provider.fixer_prompts[1])


if __name__ == "__main__":
    unittest.main()
