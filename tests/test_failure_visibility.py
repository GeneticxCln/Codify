"""A failure has to be visible to whoever has to act on it.

`format_command` printed stdout *or* stderr, so a run that printed a banner on stdout and its traceback on
stderr showed the banner. The verifier's `test_result` carried a verdict and an exit code and none of the
output, so the fixer's retry was told "tests failed" and the exit code, and the conductor's `verify` said the
same. And the fixer was shown the first 4000 characters of each file with nothing saying a file went on, which
is how a model edits the top of a long file believing it has read all of it.

These assert on what the model is actually handed.
"""

from __future__ import annotations

import json
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.library import command_tail, format_command
from tests.test_conductor_moves_state import A_V2, MovesCase, _Counting

MARKER = "KABOOM-the-assertion-that-failed"

# A real command that fails the way a test run does: a banner on stdout, the reason on stderr, a non-zero exit.
FAILING_SCRIPT = (
    "import sys\n"
    "print('collected 1 item')\n"
    f"print('{MARKER}', file=sys.stderr)\n"
    "sys.exit(1)\n"
)


class _VerifierScript(_Counting):
    """The scripted provider whose verifier proposes a command and then reads its output back."""

    def __init__(self, fixer_replies: list[Any]) -> None:
        super().__init__(fixer_replies)
        self.verifier_replies: list[dict[str, Any]] = []

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> str:
        if "You are Codify Verifier" in system_prompt and self.verifier_replies:
            self.asked["verifier"] = self.asked.get("verifier", 0) + 1
            reply = self.verifier_replies.pop(0) if len(self.verifier_replies) > 1 else self.verifier_replies[0]
            return json.dumps(reply)
        return await super().complete(
            system_prompt, user_prompt, model, temperature, max_tokens,
            num_ctx=num_ctx, keep_alive=keep_alive,
        )


class FailureCase(MovesCase):
    def build(self, *fixer_replies: Any) -> _VerifierScript:
        MovesCase.build(self, *fixer_replies)
        provider = _VerifierScript(list(fixer_replies))
        # Rebuild the executor over the richer provider.
        from engine.executor import ExecutorService
        from engine.models import ROLES, AgentConfigUpdate
        from engine.providers import Keychain
        from engine.sandbox import SandboxService
        from engine.services import AgentRegistryService
        from tests.test_fixer_reask import Factory, Gate

        registry = AgentRegistryService(self.conn, Factory(provider), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(model_name="test-model"))
        self.executor = ExecutorService(self.goals, self.workspaces, registry, SandboxService(), laya=Gate())
        self.provider = provider
        provider.verifier_replies = [
            {"argv": ["python3", "t.py"], "verdict": "fail", "explanation": "ran it"},
            {"argv": None, "verdict": "fail", "explanation": "t.py exited 1"},
        ]
        (self.root / "t.py").write_text(FAILING_SCRIPT, encoding="utf-8")
        return provider


class TestACommandShowsBothStreams(MovesCase):
    def test_stdout_and_stderr_are_both_shown_when_both_exist(self) -> None:
        text = format_command({
            "argv": ["pytest"], "exit_code": 1,
            "stdout": "collected 1 item", "stderr": f"E   {MARKER}",
        })

        self.assertIn("collected 1 item", text)
        self.assertIn(MARKER, text, "the traceback was hidden because the banner existed")
        self.assertIn("(exit 1)", text)

    def test_a_single_stream_is_shown_as_it_always_was(self) -> None:
        self.assertEqual(
            "--- $ ls (exit 0)\na.py",
            format_command({"argv": ["ls"], "exit_code": 0, "stdout": "a.py\n", "stderr": ""}),
        )
        self.assertEqual(
            "--- $ x (exit 2)\nboom",
            format_command({"argv": ["x"], "exit_code": 2, "stdout": "", "stderr": "boom\n"}),
        )

    def test_the_tail_keeps_the_end_of_each_stream_within_its_budget(self) -> None:
        result = {"stdout": "x" * 5000, "stderr": "early\n" + "y" * 3000 + "\nthe last line"}

        tail = command_tail(result, limit=2000)

        self.assertLessEqual(len(tail), 2200)
        self.assertIn("the last line", tail, "a test runner's reason is at the end, not the start")
        self.assertNotIn("early", tail)

    def test_nothing_to_show_is_an_empty_tail(self) -> None:
        self.assertEqual("", command_tail({"stdout": "", "stderr": "  "}))
        self.assertEqual("", command_tail({}))


class TestTheVerdictCarriesTheOutput(FailureCase):
    async def test_the_test_result_event_has_the_tail_of_what_the_command_printed(self) -> None:
        self.build(A_V2)
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "go"})

        await table["verify"]({"step_id": step_id})

        results = self.events("test_result")
        self.assertEqual(1, len(results))
        self.assertIn(MARKER, results[0]["output_tail"])
        self.assertIn("collected 1 item", results[0]["output_tail"])

    async def test_the_conductors_verify_says_why_it_failed(self) -> None:
        self.build(A_V2)
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "go"})

        out = await table["verify"]({"step_id": step_id})

        self.assertIn("FAILED", out)
        self.assertIn(MARKER, out)

    async def test_a_step_that_ran_nothing_has_an_empty_tail_not_a_missing_key(self) -> None:
        provider = self.build(A_V2)
        provider.verifier_replies = [{"argv": None, "verdict": "skip", "explanation": "nothing to run"}]
        step_id = self.approve()
        table = self.table()
        await table["write"]({"step_id": step_id, "instructions": "go"})

        await table["verify"]({"step_id": step_id})

        self.assertEqual("", self.events("test_result")[0]["output_tail"])

    async def test_the_tail_is_not_recallable(self) -> None:
        # Output comes from running a repository's own code, so it is third-party text. The recall tool
        # projects a stored test_result to its verdict and nothing else, and that must stay true.
        from engine.recall import RECALLABLE

        self.assertEqual(("verdict",), RECALLABLE["test_result"])


class TestTheFixerIsToldHowItFailed(FailureCase):
    async def test_a_retry_after_a_failed_run_carries_the_output(self) -> None:
        provider = self.build(A_V2)
        step_id = self.approve()

        await self.executor.run_step(self.goal.id, step_id)

        self.assertGreaterEqual(len(provider.fixer_prompts), 2, "the failed run should have been fed back once")
        retry = provider.fixer_prompts[1]
        self.assertIn("your previous attempt FAILED verification", retry)
        self.assertIn(MARKER, retry, "the fixer was told 'tests failed' and not why")


class TestAFileThatWasCutIsSaidToBeCut(MovesCase):
    def test_a_long_file_ends_with_how_much_was_not_shown(self) -> None:
        self.build(A_V2)
        (self.root / "long.py").write_text("x = 1\n" * 1000, encoding="utf-8")  # 6000 characters
        from engine.fs import FileSystemService

        ctx, _ = self.executor._suggested_paths_context(FileSystemService(str(self.root)), ["long.py"])

        self.assertIn("2000 more characters of long.py not shown", ctx)

    def test_a_file_that_fits_has_no_marker(self) -> None:
        self.build(A_V2)
        from engine.fs import FileSystemService

        ctx, _ = self.executor._suggested_paths_context(FileSystemService(str(self.root)), ["a.py"])

        self.assertNotIn("not shown", ctx)
