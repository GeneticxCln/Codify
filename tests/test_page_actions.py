"""The conductor's three page verbs that *act*: `navigate_page`, `click_page` and `type_page`.

`tests/test_webview_bridge.py` holds what the bridge does with a navigation, a click or a typed string once it
is asked. This file holds what stands between a model and asking at all: a setting only a person writes
(`page_actions`), off on a fresh install, a menu that does not offer the three while it is off, and a refusal
in each tool behind the menu that sends nothing and announces nothing.

Why off: each of the three can carry what a turn has read off the machine. An address is sent to its site,
and text typed into a field a page watches is sent as it is typed (docs/03 §1.6). `read_page` is not one of
them — reading the open page sends nothing — so it stays offered either way.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import unittest
from typing import Any

from engine.conductor import PAGE_ACTION_NAMES, TOOLS, Conductor
from engine.executor import ExecutorService
from engine.toolcall import ToolReply
from tests.test_conductor import ConductorTestCase, _call, _ToolProvider

URL = "https://docs.python.org/3/library/asyncio.html"

#: Each acting verb, with arguments it would accept.
CALLS: dict[str, dict[str, Any]] = {
    "navigate_page": {"url": URL},
    "click_page": {"selector": "#submit"},
    "type_page": {"selector": "#q", "text": "the contents of .env"},
}


class RecordingBridge:
    """A shell that does what it is asked and remembers being asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, tuple[Any, ...]]] = []

    async def navigate(self, url: str, *, tab: str | None = None) -> dict[str, Any]:
        self.asked.append(("navigate", (url, tab)))
        return {"tab": tab or "tab-1", "url": url}

    async def click(self, selector: str, *, tab: str | None = None) -> dict[str, Any]:
        self.asked.append(("click", (selector, tab)))
        return {"url": URL, "title": "asyncio", "clicked": {"tag": "button", "text": "Go"}}

    async def type_text(self, selector: str, text: str, *, tab: str | None = None) -> dict[str, Any]:
        self.asked.append(("type", (selector, text, tab)))
        return {"url": URL, "title": "asyncio", "typed": {"tag": "input", "chars": len(text)}}


class PageActionCase(ConductorTestCase):
    def executor_with(self, allowed: int | None) -> tuple[ExecutorService, RecordingBridge]:
        """An executor with a recording shell, and `page_actions` as given (None: never written)."""
        executor = self._executor(_ToolProvider())
        executor.settings = self.settings
        if allowed is not None:
            self.settings.set_int("page_actions", allowed)
        bridge = RecordingBridge()
        executor.bridge = bridge  # type: ignore[assignment]  # a stand-in shell, by duck type
        return executor, bridge

    def table(self, executor: ExecutorService) -> dict[str, Any]:
        from engine.skills import load_skills

        return executor._conductor_dispatch(
            self.goal.id, self.goals.get(self.goal.id), str(self.repo), load_skills(str(self.repo))
        )

    def offered(self, executor: ExecutorService) -> set[str]:
        return {t.name for t in executor.conductor_menu(self.goal.id)()}

    def logs(self) -> list[str]:
        return [
            str((event.payload or {}).get("message"))
            for event in self.goals.events_after(self.goal.id, 0)
            if event.type == "log"
        ]


class TestTheyAreOffUnlessAPersonTurnedThemOn(PageActionCase):
    async def test_a_fresh_install_has_them_off(self) -> None:
        self.assertEqual(self.settings.get_int("page_actions"), 0)
        executor, _bridge = self.executor_with(None)
        self.assertFalse(executor._page_actions_allowed())

    async def test_a_setting_that_cannot_be_read_is_off_not_on(self) -> None:
        executor = self._executor(_ToolProvider())
        # No store at all, which is every benchmark and most of the suite.
        executor.settings = None
        self.assertFalse(executor._page_actions_allowed())

        class Broken:
            def get_int(self, key: str) -> int:
                raise RuntimeError("the store is locked")

        executor.settings = Broken()  # type: ignore[assignment]  # a store that raises, by duck type
        self.assertFalse(executor._page_actions_allowed())

    async def test_each_verb_refuses_while_off_and_sends_and_announces_nothing(self) -> None:
        executor, bridge = self.executor_with(0)
        table = self.table(executor)
        for name, args in CALLS.items():
            with self.subTest(verb=name):
                answer = await table[name](args)
                self.assertIn("turned off", answer)
                self.assertIn("Settings", answer)
                self.assertIn("`read_page` still reads", answer)
        self.assertEqual(bridge.asked, [], "a verb that was refused still reached the shell")
        self.assertEqual(
            [m for m in self.logs() if m.startswith("conductor ")], [],
            "an action that was never taken was announced",
        )

    async def test_once_allowed_each_verb_reaches_the_shell_and_is_announced_first(self) -> None:
        executor, bridge = self.executor_with(1)
        table = self.table(executor)
        for name, args in CALLS.items():
            await table[name](args)
        self.assertEqual([verb for verb, _args in bridge.asked], ["navigate", "click", "type"])
        announced = [m for m in self.logs() if m.startswith("conductor ")]
        self.assertEqual(len(announced), 3, announced)


class TestTheMenuOffersThemOnlyWhenAllowed(PageActionCase):
    async def test_off_hides_all_three_and_keeps_read_page(self) -> None:
        executor, _bridge = self.executor_with(0)
        offered = self.offered(executor)
        self.assertEqual(offered & PAGE_ACTION_NAMES, set())
        self.assertIn("read_page", offered, "reading the open page sends nothing, so it is not gated")

    async def test_on_offers_all_three(self) -> None:
        executor, _bridge = self.executor_with(1)
        self.assertLessEqual(PAGE_ACTION_NAMES, self.offered(executor))

    async def test_they_are_still_tools_the_table_answers_to(self) -> None:
        # A menu that hides them is not a table that forgot them: switching the setting on needs no rebuild.
        executor, _bridge = self.executor_with(0)
        self.assertLessEqual(PAGE_ACTION_NAMES, set(self.table(executor)))
        self.assertLessEqual(PAGE_ACTION_NAMES, {t.name for t in TOOLS})

    async def test_a_model_that_names_one_while_it_is_hidden_is_told_why_and_nothing_moves(self) -> None:
        executor, bridge = self.executor_with(0)
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("navigate_page", url=f"{URL}?d=SECRET")]),
            ToolReply(text="I could not open it."),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), self.table(executor),
            system_prompt="s", menu=executor.conductor_menu(self.goal.id),
        )
        await conductor.run("open the asyncio docs")
        self.assertEqual(bridge.asked, [])
        result = [m for m in provider.seen_messages[1] if m.get("role") == "tool"][0]["content"]
        self.assertIn("not available right now", result)
        self.assertIn("only when the person has allowed acting on their browser tab", result)
        self.assertEqual(set(provider.seen_tools[0]) & PAGE_ACTION_NAMES, set())


class TestThePromptSaysTheSame(unittest.TestCase):
    def test_the_conductors_prompt_says_they_need_permission(self) -> None:
        from engine.chat_prompts import CONDUCTOR_SYSTEM_PROMPT

        text = " ".join(CONDUCTOR_SYSTEM_PROMPT.split())
        self.assertIn("`type_page` drive it when the person has allowed it", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
