"""A turn whose only change is to the person's open editor is not "no file was changed".

Found by the first live run of the editor (a real engine, the scripted conductor, the built UI in Chromium): the
assistant opened `hello.txt`, changed `hello` to `howdy` in the editor, said so, and the turn ended with the warning
"the gate read this as 'code_change' and the conductor finished without planning anything: no file was changed. If you
wanted this done, ask again and say so plainly." Both halves of the warning were wrong. The conductor had done what was
asked, in the one place it may (the open buffer, unsaved, for the person to keep or revert; docs/09 §13.4), and "ask
again" sent the person back to a request that had been carried out.

What the warning exists for is unchanged and asserted here as well: a request the gate read as a change, answered
without a plan *and without touching anything*, still says so (a weak model narrating a sequence it never ran). So
the warning is withheld only when an edit actually landed in the editor: a refused edit, or no edit, still warns.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio

from tests.test_conductor import ConductorTestCase, _call, _ToolProvider
from tests.test_one_writer_per_goal import _ChangeGate

from engine.executor import ExecutorService
from engine.providers import Keychain
from engine.models import ROLES, AgentConfigUpdate
from engine.services import AgentRegistryService
from engine.surfaces import SurfaceBridge
from engine.toolcall import ToolReply

from tests.test_conductor import _Factory

EDIT = _call("edit_editor", path="app.py", old_text="return text", new_text="return text.strip()")
EDIT_RESULT = {"path": "app.py", "replaced": 1, "from_line": 2, "to_line": 2, "opened": False, "dirty": True}


class TestAnEditorEditIsAChange(ConductorTestCase):
    def _executor_with_window(self, provider: _ToolProvider, bridge: SurfaceBridge) -> ExecutorService:
        registry = AgentRegistryService(self.conn, _Factory(provider), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(model_name="stub-model"))
        return ExecutorService(
            self.goals, self.workspaces, registry, self.sandbox, laya=_ChangeGate(), surfaces=bridge,
        )

    async def _window(self, bridge: SurfaceBridge, *, ok: bool, error: str | None = None) -> None:
        """The app window, already polling (see `_attach`): it answers the one edit."""
        request = await bridge.next_request(wait_s=5.0)
        assert request is not None, "the conductor never asked the window anything"
        bridge.answer(request["id"], ok, EDIT_RESULT if ok else None, error)

    @staticmethod
    def _attach(bridge: SurfaceBridge) -> None:
        """The window is polling *before* the run starts: a run asks at once, and a window that has not polled yet is no window."""
        bridge.note_ui()

    def _warnings(self) -> list[str]:
        return [
            str(e.payload.get("message"))
            for e in self.goals.events_after(self.goal.id, 0)
            if e.type == "log" and e.payload.get("level") == "warn"
        ]

    async def _turn(self, *, edit: bool, window_ok: bool = True, error: str | None = None) -> list[str]:
        replies = [ToolReply(text="", tool_calls=[EDIT])] if edit else []
        replies.append(ToolReply(text="I changed app.py in your editor. It is not saved."))
        bridge = SurfaceBridge()
        self._attach(bridge)
        executor = self._executor_with_window(_ToolProvider(replies), bridge)
        window = asyncio.create_task(self._window(bridge, ok=window_ok, error=error)) if edit else None
        await executor.run_chat(self.goal.id)
        if window is not None:
            await window
        return self._warnings()

    async def test_an_edit_that_landed_in_the_editor_is_not_called_no_change(self) -> None:
        warnings = await self._turn(edit=True)

        self.assertEqual([], [w for w in warnings if "no file was changed" in w], warnings)
        self.assertEqual("COMPLETED", self.goals.get(self.goal.id).status)

    async def test_a_change_answered_without_touching_anything_still_says_so(self) -> None:
        warnings = await self._turn(edit=False)

        self.assertTrue(any("no file was changed" in w for w in warnings), warnings)

    async def test_an_edit_the_editor_refused_is_not_a_change(self) -> None:
        warnings = await self._turn(edit=True, window_ok=False, error="That text is not in app.py.")

        self.assertTrue(any("no file was changed" in w for w in warnings), warnings)

    async def test_one_turns_edit_does_not_excuse_the_next_turn(self) -> None:
        executor = self._executor_with_window(
            _ToolProvider([ToolReply(text="", tool_calls=[EDIT]), ToolReply(text="Done. Not saved.")]),
            bridge := SurfaceBridge(),
        )
        self._attach(bridge)
        window = asyncio.create_task(self._window(bridge, ok=True))
        await executor.run_chat(self.goal.id)
        await window
        self.assertEqual({}, {k: v for k, v in executor._editor_edits.items() if v}, "the count outlived the run")

