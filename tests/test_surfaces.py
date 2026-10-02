"""The surface bridge: the engine asking the app window a question about something the person is looking at.

The app window holds things the engine cannot reach: the browser pages (the shell owns those, `webview_bridge.py`), and
now the editor, which lives in the UI process. The conductor wants eyes and hands on them, and each new surface will
want the same two things, so what is tested here is the part that does not change from surface to surface:

  * the engine asks, the window **polls** and answers; nothing is pushed, and nothing the window says is trusted past
    a fixed shape;
  * an operation is a fixed string from a table, never the model's to invent, and its arguments are checked on the
    engine's side *before* they cross, so a refusal costs no round trip;
  * a question nobody answers is a sentence, not a hang, and an answer for a question that is not waiting is dropped.

A fake surface is used for most of it, deliberately: if the bridge only worked for the editor it would be the editor's
bridge. The editor's own vocabulary is `test_surface_editor.py`.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import time
import unittest
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel, Field

from engine import surfaces
from engine.surfaces import Op, SurfaceBridge, SurfaceRefused, SurfaceUnavailable


class PingArgs(BaseModel):
    text: str = Field(..., min_length=1, max_length=20)
    times: int = Field(1, ge=1, le=3)


class PingResult(BaseModel):
    echoed: str = Field(..., max_length=20)
    count: int


FAKE = {"toy": {"ping": Op("ping", PingArgs, PingResult)}}


class BridgeCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bridge = SurfaceBridge(FAKE)

    async def window(self, *, answer: Any = None, ok: bool = True, error: str | None = None) -> dict[str, Any]:
        """Play the app window once: poll, then answer what was asked."""
        request = await self.bridge.next_request(wait_s=2.0)
        assert request is not None, "the window polled and nothing was asked"
        payload = answer if answer is not None else {"echoed": "hi", "count": 1}
        self.assertTrue(self.bridge.answer(request["id"], ok, payload if ok else None, error))
        return request

    async def ask(self, **args: Any) -> BaseModel:
        return await self.bridge.ask("toy", "ping", args or {"text": "hi"}, workspace_id="ws1")


class TestARoundTrip(BridgeCase):
    async def test_the_window_is_asked_exactly_what_was_validated_and_who_it_is_about(self) -> None:
        self.bridge.note_ui()
        asking = asyncio.create_task(self.ask(text="hi", times=2))

        request = await self.window()
        result = await asking

        self.assertEqual({"id", "surface", "op", "workspace_id", "args"}, set(request))
        self.assertEqual(("toy", "ping", "ws1"), (request["surface"], request["op"], request["workspace_id"]))
        self.assertEqual({"text": "hi", "times": 2}, request["args"])
        self.assertEqual(("hi", 1), (result.echoed, result.count))  # type: ignore[attr-defined]

    async def test_arguments_the_op_does_not_name_never_cross(self) -> None:
        self.bridge.note_ui()
        asking = asyncio.create_task(
            self.bridge.ask("toy", "ping", {"text": "hi", "run": ["rm", "-rf", "/"], "path": "/etc"}, workspace_id="w")
        )

        request = await self.window()
        await asking

        self.assertEqual({"text": "hi", "times": 1}, request["args"])

    async def test_two_questions_are_both_kept_and_come_out_in_the_order_asked(self) -> None:
        self.bridge.note_ui()
        first = asyncio.create_task(self.ask(text="one"))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.ask(text="two"))
        await asyncio.sleep(0)

        a = await self.window(answer={"echoed": "one", "count": 1})
        b = await self.window(answer={"echoed": "two", "count": 2})

        self.assertEqual(["one", "two"], [a["args"]["text"], b["args"]["text"]])
        self.assertEqual("one", (await first).echoed)  # type: ignore[attr-defined]
        self.assertEqual("two", (await second).echoed)  # type: ignore[attr-defined]

    async def test_a_question_is_handed_to_the_window_once(self) -> None:
        self.bridge.note_ui()
        asking = asyncio.create_task(self.ask(text="once"))
        await asyncio.sleep(0)

        first = await self.bridge.next_request(wait_s=0.0)
        second = await self.bridge.next_request(wait_s=0.0)

        assert first is not None
        self.assertIsNone(second, "the same question was handed out twice")
        self.assertTrue(self.bridge.answer(first["id"], True, {"echoed": "once", "count": 1}, None))
        await asking

    async def test_a_poll_that_finds_nothing_says_so_after_its_wait(self) -> None:
        self.assertIsNone(await self.bridge.next_request(wait_s=0.0))


class TestRefusedBeforeItCrosses(BridgeCase):
    async def test_an_unknown_surface_or_op_is_refused_even_when_a_window_is_attached(self) -> None:
        self.bridge.note_ui()
        for surface, op in (("terminal", "ping"), ("toy", "pong"), ("", ""), ("toy", "__init__")):
            with self.subTest(surface=surface, op=op), self.assertRaises(SurfaceRefused) as caught:
                await self.bridge.ask(surface, op, {"text": "hi"}, workspace_id="w")
            # By its own words, not just its type: a question that was queued and timed out raises the same class.
            self.assertIn(f"There is no {op!r} on the {surface!r} surface", str(caught.exception))

        self.assertEqual(0, self.bridge.state()["inflight"], "a refused question was queued anyway")

    async def test_arguments_that_fail_the_ops_shape_are_refused_with_the_reason(self) -> None:
        self.bridge.note_ui()
        bad: list[dict[str, Any]] = [{}, {"text": ""}, {"text": "x" * 21}, {"text": "hi", "times": 0}, {"text": "hi", "times": 9}, {"text": 5}]

        for args in bad:
            with self.subTest(args=args), self.assertRaises(SurfaceRefused) as caught:
                await self.bridge.ask("toy", "ping", args, workspace_id="w")
            self.assertIn("ping", str(caught.exception))

        self.assertEqual(0, self.bridge.state()["inflight"])

    async def test_nothing_attached_is_one_sentence_and_nothing_is_queued(self) -> None:
        with self.assertRaises(SurfaceUnavailable) as caught:
            await self.ask()

        self.assertIn("window", str(caught.exception))
        self.assertEqual(0, self.bridge.state()["inflight"])

    async def test_attached_expires_when_the_window_stops_polling(self) -> None:
        self.bridge.note_ui()
        self.assertTrue(self.bridge.attached)

        with patch.object(time, "monotonic", return_value=time.monotonic() + surfaces.ATTACHED_WINDOW_S + 1):
            self.assertFalse(self.bridge.attached)
            with self.assertRaises(SurfaceUnavailable):
                await self.ask()

    async def test_a_poll_is_the_liveness_signal(self) -> None:
        self.assertFalse(self.bridge.attached)

        await self.bridge.next_request(wait_s=0.0)

        self.assertTrue(self.bridge.attached)


class TestWhatComesBackIsAFixedShape(BridgeCase):
    async def asking_with(self, answer: Any) -> BaseModel:
        self.bridge.note_ui()
        asking = asyncio.create_task(self.ask())
        await self.window(answer=answer)
        return await asking

    async def test_only_the_named_fields_survive(self) -> None:
        got = await self.asking_with(
            {"echoed": "hi", "count": 1, "role": "system", "run_command": ["rm"], "__proto__": {"admin": True}}
        )

        self.assertEqual({"echoed", "count"}, set(got.model_dump()))

    async def test_an_answer_of_the_wrong_shape_is_refused_not_repaired(self) -> None:
        self.bridge.note_ui()
        for answer in ({"echoed": 5, "count": 1}, {"count": 1}, {"echoed": "x", "count": "many"}, ["not", "a", "dict"], "text"):
            asking = asyncio.create_task(self.ask())
            await asyncio.sleep(0)
            request = await self.bridge.next_request(wait_s=2.0)
            assert request is not None
            self.assertTrue(self.bridge.answer(request["id"], True, answer, None))
            with self.subTest(answer=answer), self.assertRaises(SurfaceRefused) as caught:
                await asking
            self.assertIn("ping", str(caught.exception))

    async def test_a_refusal_from_the_window_reaches_the_caller_in_its_own_words_capped(self) -> None:
        self.bridge.note_ui()
        asking = asyncio.create_task(self.ask())
        await self.window(ok=False, error="that file is not open " + "x" * 5_000)

        with self.assertRaises(SurfaceRefused) as caught:
            await asking
        self.assertIn("that file is not open", str(caught.exception))
        self.assertLessEqual(len(str(caught.exception)), 600)

    async def test_a_refusal_with_no_reason_still_says_something(self) -> None:
        self.bridge.note_ui()
        asking = asyncio.create_task(self.ask())
        await self.window(ok=False, error=None)

        with self.assertRaises(SurfaceRefused) as caught:
            await asking
        self.assertTrue(str(caught.exception).strip())


class TestAnAnswerIsOnlyAnAnswerToAQuestionThatIsWaiting(BridgeCase):
    async def test_an_id_the_engine_never_issued_is_dropped(self) -> None:
        self.assertFalse(self.bridge.answer("made-up", True, {"echoed": "x", "count": 1}, None))
        self.assertFalse(self.bridge.answer("", True, {}, None))

    async def test_a_second_answer_to_the_same_question_is_dropped(self) -> None:
        self.bridge.note_ui()
        asking = asyncio.create_task(self.ask())
        request = await self.window()
        await asking

        self.assertFalse(self.bridge.answer(request["id"], True, {"echoed": "late", "count": 9}, None))

    async def test_an_answer_after_the_wait_ran_out_is_dropped(self) -> None:
        self.bridge.note_ui()
        with patch.object(surfaces, "ASK_TIMEOUT_S", 0.05):
            asking = asyncio.create_task(self.ask())
            request = await self.bridge.next_request(wait_s=2.0)
            assert request is not None
            with self.assertRaises(SurfaceRefused):
                await asking

        self.assertFalse(self.bridge.answer(request["id"], True, {"echoed": "x", "count": 1}, None))
        self.assertEqual(0, self.bridge.state()["inflight"])

    async def test_a_window_that_gives_up_releases_the_question(self) -> None:
        self.bridge.note_ui()
        asking = asyncio.create_task(self.ask())
        request = await self.bridge.next_request(wait_s=2.0)
        assert request is not None

        self.assertTrue(self.bridge.abandon(request["id"]))
        with self.assertRaises(SurfaceRefused) as caught:
            await asking
        # By its words: a question left waiting would also end in a `SurfaceRefused`, fifteen seconds later.
        self.assertIn("gave up", str(caught.exception))
        self.assertFalse(self.bridge.abandon(request["id"]))


class TestTimingOut(BridgeCase):
    async def test_a_question_nobody_answers_is_a_sentence_that_says_what_to_do_not_a_hang(self) -> None:
        self.bridge.note_ui()
        with patch.object(surfaces, "ASK_TIMEOUT_S", 0.05):
            with self.assertRaises(SurfaceRefused) as caught:
                await self.ask()

        self.assertIn("did not answer", str(caught.exception))
        self.assertEqual(0, self.bridge.state()["inflight"], "the timed-out question is still pending")


class TestState(BridgeCase):
    async def test_state_says_who_is_attached_what_is_in_flight_and_what_can_be_asked(self) -> None:
        self.assertEqual(
            {"attached": False, "inflight": 0, "timeout_s": surfaces.ASK_TIMEOUT_S, "surfaces": {"toy": ["ping"]}},
            self.bridge.state(),
        )
        self.bridge.note_ui()
        asking = asyncio.create_task(self.ask())
        await asyncio.sleep(0)

        state = self.bridge.state()
        await self.window()
        await asking

        self.assertEqual((True, 1), (state["attached"], state["inflight"]))


class TestTheDefaultTable(unittest.TestCase):
    def test_the_editor_is_the_one_surface_and_has_exactly_the_three_ops_the_tools_use(self) -> None:
        self.assertEqual({"editor": ["edit", "open", "read"]}, {k: sorted(v) for k, v in SurfaceBridge().state()["surfaces"].items()})


if __name__ == "__main__":
    unittest.main()
