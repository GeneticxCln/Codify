"""The conductor's budgets are bounds, not advice.

`exhausted` was `calls_made >= max_turns`, read after the loop ended, so a model that *answered* on the last
call it was allowed read as cut off and the engine overrode a perfectly good answer. The move budget only
removed stage moves from the next menu: the menu is rebuilt once per model call, so a reply that asked for
four moves with one left ran all four, and a move that was never offered ran anyway because `_run_tool`
dispatched on the table rather than on the menu.

Each of those is a way for a run to spend more than it was given, and the budget is what keeps a confused
model from spending a key and a machine's time, so these assert on what was *run*, not on what was shown.
"""

from __future__ import annotations

from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.conductor import READ_FILE, RECON, STAGE_MOVES, TOOLS, Conductor
from engine.toolcall import ToolReply
from tests.test_conductor import ConductorTestCase, _call, _ToolProvider


def _counter() -> tuple[list[dict[str, Any]], Any]:
    seen: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any]) -> str:
        seen.append(args)
        return "ran"

    return seen, handler


class TestExhaustedMeansItWasCutOff(ConductorTestCase):
    async def test_an_answer_on_the_last_allowed_call_is_an_answer_not_a_cut_off(self) -> None:
        _, read_file = _counter()
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("read_file", path="app.py")]),
            ToolReply(text="It parses."),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {"read_file": read_file},
            system_prompt="s", max_turns=2,
        )

        answer = await conductor.run("what does it do")

        self.assertEqual("It parses.", answer)
        self.assertEqual(2, conductor.calls_made, "it used every call it was given")
        self.assertFalse(conductor.exhausted, "and then it finished, which is not running out")

    async def test_wanting_more_after_the_last_call_is_exhausted(self) -> None:
        # The control: the same loop, but the model still asks for a tool when it has none left.
        _, read_file = _counter()
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("read_file", path="a.py")]),
            ToolReply(tool_calls=[_call("read_file", path="b.py")]),
            ToolReply(tool_calls=[_call("read_file", path="c.py")]),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {"read_file": read_file},
            system_prompt="s", max_turns=2,
        )

        await conductor.run("go")

        self.assertTrue(conductor.exhausted)

    async def test_the_forced_answer_is_still_cut_off_even_when_it_complies(self) -> None:
        # The model was told it had no calls left and wrote an answer without a tool. It had more to do
        # (it needed the extra call to be asked), so the run was cut off, and the caller must know.
        _, read_file = _counter()
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("read_file", path="a.py")]),
            ToolReply(text="Here is what I found so far."),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {"read_file": read_file},
            system_prompt="s", max_turns=1,
        )

        await conductor.run("go")

        self.assertTrue(conductor.exhausted)


class TestAMoveRunsOnlyIfItIsOfferedAndAffordable(ConductorTestCase):
    async def test_a_move_that_is_not_on_the_menu_is_refused_and_not_run(self) -> None:
        wrote, write = _counter()
        _, read_file = _counter()
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("write", step_id="s1", instructions="x")]),
            ToolReply(text="ok"),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), [READ_FILE],
            {"read_file": read_file, "write": write}, system_prompt="s",
        )

        await conductor.run("go")

        self.assertEqual([], wrote, "a move the menu did not offer was run from the dispatch table")
        told = [m["content"] for m in provider.seen_messages[-1] if m.get("role") == "tool"]
        self.assertTrue(told and "not available" in told[0], told)
        self.assertIn("read_file", told[0], "the refusal names what is on the menu")
        self.assertEqual(0, conductor.moves_made, "a refused move costs nothing")

    async def test_four_stage_calls_in_one_reply_run_only_as_many_as_there_are_moves_left(self) -> None:
        ran, recon = _counter()
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("recon", task=f"look {i}") for i in range(4)]),
            ToolReply(text="done"),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {"recon": recon},
            system_prompt="s", max_moves=1,
        )

        await conductor.run("go")

        self.assertEqual(1, len(ran), "the budget was one move and the reply asked for four")
        self.assertEqual(1, conductor.moves_made)
        results = [m["content"] for m in provider.seen_messages[-1] if m.get("role") == "tool"]
        self.assertEqual(4, len(results), "every call gets an answer, so the model can read why")
        self.assertEqual("ran", results[0])
        for refused in results[1:]:
            self.assertIn("budget", refused)

    async def test_a_move_that_fails_still_costs_a_move(self) -> None:
        async def broken(args: dict[str, Any]) -> str:
            raise RuntimeError("the librarian fell over")

        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("recon", task="a"), _call("recon", task="b")]),
            ToolReply(text="done"),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {"recon": broken},
            system_prompt="s", max_moves=1,
        )

        await conductor.run("go")

        self.assertEqual(1, conductor.moves_made, "a failed sub-agent run was still a sub-agent run")
        results = [m["content"] for m in provider.seen_messages[-1] if m.get("role") == "tool"]
        self.assertIn("budget", results[1])

    async def test_a_name_that_is_not_wired_costs_nothing(self) -> None:
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("recon", task="a")]),
            ToolReply(text="done"),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {}, system_prompt="s", max_moves=1,
        )

        await conductor.run("go")

        self.assertEqual(0, conductor.moves_made)

    async def test_the_reads_keep_working_when_the_moves_are_spent(self) -> None:
        # Spending the moves must not make the run mute: it can still read and answer from what it has.
        reads, read_file = _counter()
        _, recon = _counter()
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("recon", task="a")]),
            ToolReply(tool_calls=[_call("read_file", path="app.py")]),
            ToolReply(text="done"),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {"recon": recon, "read_file": read_file},
            system_prompt="s", max_moves=1,
        )

        await conductor.run("go")

        self.assertEqual(1, len(reads))

    def test_what_counts_as_a_move_is_the_stage_set(self) -> None:
        # The budget and the menu agree on which names are expensive.
        self.assertIn(RECON.name, STAGE_MOVES)
        self.assertNotIn(READ_FILE.name, STAGE_MOVES)
