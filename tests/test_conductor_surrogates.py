"""A lone surrogate in a tool call's arguments is healed, except where the text becomes the person's buffer or a typed command.

A model's reply can spell `"\\ud800"`; once parsed that is a lone surrogate, a `str` Python carries happily and nothing
that encodes UTF-8 accepts. The conductor takes a model's tool-call arguments straight from the reply, hands them to a
handler, and echoes them into the next request. Before this, a lone surrogate in ANY argument of ANY tool made the next
model call raise a bare `UnicodeEncodeError` out of `Conductor.run` on the anthropic, ollama and google protocols (the
openai one stringifies arguments, and failed only where the engine echoed the text in a result), and the goal ended
`internal_error`. A `type_page` text made `GET /bridge/next` answer 500 and left the request pending until it timed out.

The rule (docs/04 section 6) is one sentence: text a model wrote is HEALED (a lone surrogate becomes U+FFFD) wherever it
becomes prose, a title, a note, a message, a prompt or a command line that goes through `validate_argv`, and REFUSED
wherever it becomes the person's file, path, editor buffer or a command typed into the machine shell. So:

  * every tool's arguments are healed in `Conductor._run_tool`, before the handler, EXCEPT `VERBATIM_TOOLS`;
  * `run_command`'s argv is healed BEFORE `validate_argv`, so the list that is validated is the list that runs;
  * `edit_editor` and `run_in_machine` are never healed. The first is the person's buffer. The second types the model's
    command into a shell that has no `validate_argv` on purpose (docs/00 section 6.6), where healing would silently
    rewrite the command that runs. `_run_tool` refuses the call itself, naming the field, before any handler or the
    window is asked ("That edit was not made", "That was not run in the machine"); the handlers' own refusals stay
    as the second layer. `type_page` is healed on purpose (typed into a web page, not a shell or a buffer).

The request path is healed separately, in `engine/providers.py` (`tests/test_provider_requests_surrogates.py`); the
end-to-end test here drives the conductor through the four real providers, so it needs both halves.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import httpx
from httpx import ASGITransport

from engine import conductor_tools
from engine.app import BOOT_TOKEN, app
from engine.conductor import TOOLS, VERBATIM_TOOLS, Conductor
from engine.executor import ConductorTools
from engine.providers import (
    AnthropicProvider,
    BaseProvider,
    GoogleProvider,
    OllamaProvider,
    OpenAICompatProvider,
)
from engine.sandbox import CommandNotAllowed
from engine.toolcall import ToolCall, ToolReply, ToolSpec
from engine.webview_bridge import WebviewBridge
from tests.test_editor_tools import ToolCase

POISON = "a\ud800b"
HEALED = "a�b"
LOW = "c\udc80d"
LOW_HEALED = "c�d"


class Scripted(BaseProvider):
    """A model that asks for the tool calls it is given, one reply at a time, then says it is done."""

    def __init__(self, *replies: ToolReply) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict[str, Any]]] = []

    @property
    def supports_tools(self) -> bool:
        return True

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None, json_schema: dict[str, Any] | None = None,
    ) -> str:
        return "unused"

    async def complete_with_tools(
        self, system_prompt: str, messages: list[dict[str, Any]], tools: list[ToolSpec], model: str,
        temperature: float, max_tokens: int, *, num_ctx: int | None = None, keep_alive: str | None = None,
        cache_prefix: bool = False,
    ) -> ToolReply:
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else ToolReply(text="done")


def calling(tool: str, **arguments: Any) -> ToolReply:
    return ToolReply(tool_calls=[ToolCall(f"c_{tool}", tool, arguments)])


def loop(provider: BaseProvider, dispatch: dict[str, Any]) -> Conductor:
    return Conductor(provider, "m", "/tmp", list(TOOLS), dispatch, system_prompt="s")


def tools_over(root: str, **service: Any) -> ConductorTools:
    """The real tool handlers over a stand-in service holding only what the handlers under test touch."""
    stand_in = SimpleNamespace(git=None, workspaces=SimpleNamespace(get=lambda _id: SimpleNamespace(root_path=root)), **service)
    return ConductorTools(cast(Any, stand_in), "goal-1", cast(Any, SimpleNamespace(workspace_id="ws-1")), root, cast(Any, None))


class Recorder:
    """A tool handler that remembers what it was handed."""

    def __init__(self, answer: str = "ok") -> None:
        self.answer = answer
        self.got: list[dict[str, Any]] = []

    async def __call__(self, args: dict[str, Any]) -> str:
        self.got.append(args)
        return self.answer


class TestEveryOrdinaryToolIsHandedHealedArguments(unittest.IsolatedAsyncioTestCase):
    async def test_a_string_a_list_and_a_nested_value_are_each_healed(self) -> None:
        shapes: dict[str, dict[str, Any]] = {
            "read_file": {"path": POISON, "offset": 1},
            "search_code": {"query": POISON, "glob": LOW},
            "run_command": {"argv": ["ls", POISON], "reason": LOW},
            "recall": {"query": POISON},
            "use_skill": {"name": POISON},
            "git_history": {"args": [POISON, {"deep": [LOW]}]},
        }
        expected: dict[str, dict[str, Any]] = {
            "read_file": {"path": HEALED, "offset": 1},
            "search_code": {"query": HEALED, "glob": LOW_HEALED},
            "run_command": {"argv": ["ls", HEALED], "reason": LOW_HEALED},
            "recall": {"query": HEALED},
            "use_skill": {"name": HEALED},
            "git_history": {"args": [HEALED, {"deep": [LOW_HEALED]}]},
        }
        for name, arguments in shapes.items():
            with self.subTest(tool=name):
                handler = Recorder()
                answer = await loop(Scripted(calling(name, **arguments)), {name: handler}).run("go")
                self.assertEqual("done", answer)
                self.assertEqual([expected[name]], handler.got)

    async def test_a_json_string_of_arguments_spelling_the_escape_is_healed_too(self) -> None:
        """The OpenAI dialect hands arguments back as a JSON string; `coerce_arguments` parses it into the surrogate."""
        handler = Recorder()
        call = ToolCall("c1", "read_file", cast(Any, json.dumps({"path": POISON})))
        await loop(Scripted(ToolReply(tool_calls=[call])), {"read_file": handler}).run("go")
        self.assertEqual([{"path": HEALED}], handler.got)

    async def test_clean_arguments_arrive_as_they_were_written(self) -> None:
        handler = Recorder()
        arguments = {"path": "café/\U0001f600.py", "argv": ["ls", "x\U0001f600y"]}
        await loop(Scripted(calling("read_file", **arguments)), {"read_file": handler}).run("go")
        self.assertEqual([arguments], handler.got)

    async def test_the_run_goes_on_and_the_next_request_carries_the_result(self) -> None:
        provider = Scripted(calling("read_file", path=POISON))
        answer = await loop(provider, {"read_file": Recorder("--- a�b\ncontents")}).run("go")
        self.assertEqual("done", answer)
        tool_results = [m for m in provider.seen[-1] if m["role"] == "tool"]
        self.assertEqual(["--- a�b\ncontents"], [m["content"] for m in tool_results])


class TestRunCommandIsValidatedAfterItIsHealed(unittest.IsolatedAsyncioTestCase):
    async def test_validate_argv_sees_the_list_the_sandbox_would_run(self) -> None:
        validated: list[list[str]] = []

        def refuse(argv: list[str], fs: Any, mode: str = "test") -> None:
            validated.append(list(argv))
            raise CommandNotAllowed("stop here: this test only watches what was validated")

        run = mock.Mock(side_effect=AssertionError("nothing may run"))
        provider = Scripted(calling("run_command", argv=["ls", POISON], reason=LOW))
        with tempfile.TemporaryDirectory() as root:
            tools = tools_over(root, _write_allowed=lambda goal_id: (False, ""), sandbox=SimpleNamespace(run_command=run))
            with mock.patch.object(conductor_tools, "validate_argv", refuse):
                answer = await loop(provider, {"run_command": tools.run_command}).run("go")

        self.assertEqual("done", answer)
        # `run_command` asks twice before approval (read-only, then test, to word the refusal): both see the healed list.
        self.assertTrue(validated, "validate_argv was never asked")
        self.assertEqual([["ls", HEALED]] * len(validated), validated, "what was validated must be what would run")
        self.assertIn("Refused", [m for m in provider.seen[-1] if m["role"] == "tool"][0]["content"])
        run.assert_not_called()


class TestTheTwoVerbatimToolsAreNeverHealed(ToolCase):
    """`VERBATIM_TOOLS` holds both, and each keeps its own refusal. Healing either would rewrite what the person ends up with."""

    def test_the_set_is_exactly_these_two_and_both_are_tools_the_conductor_offers(self) -> None:
        self.assertEqual(frozenset({"edit_editor", "run_in_machine"}), VERBATIM_TOOLS)
        self.assertLessEqual(VERBATIM_TOOLS, {t.name for t in TOOLS})

    async def test_the_call_is_refused_by_the_conductor_before_any_handler_and_names_the_field(self) -> None:
        """The check is the conductor's own and explicit, not whatever a handler or pydantic happens to do with the text."""
        for name, field, arguments in (
            ("edit_editor", "new_text", {"path": "src/a.py", "old_text": "1", "new_text": "x\ud800y"}),
            ("edit_editor", "old_text", {"path": "src/a.py", "old_text": "1\udc80", "new_text": "x"}),
            ("edit_editor", "path", {"path": "src/a.py\ud800", "old_text": "1", "new_text": "x"}),
            ("edit_editor", "count", {"path": "src/a.py", "old_text": "1", "new_text": "x", "count": "1\ud800"}),
            ("run_in_machine", "command", {"command": "echo \ud800"}),
            ("run_in_machine", "machine", {"command": "ls", "machine": "m\ud800"}),
            ("run_in_machine", "wait_s", {"command": "ls", "wait_s": "1\ud800"}),
            ("run_in_machine", "y", {"command": "ls", "machine": "m", "wait_s": 2, "x": ["ok"], "y": {"k": "\ud800"}}),
        ):
            with self.subTest(tool=name, field=field):
                handler = Recorder()
                provider = Scripted(calling(name, **arguments))

                answer = await loop(provider, {name: handler}).run("go")

                self.assertEqual("done", answer)
                self.assertEqual([], handler.got, "a handler was handed text that holds a lone surrogate")
                result = [m for m in provider.seen[-1] if m["role"] == "tool"][0]["content"]
                self.assertTrue(result.startswith(
                    "That edit was not made." if name == "edit_editor" else "That was not run in the machine."
                ), result)
                self.assertIn("lone surrogate", result)
                self.assertIn(f"`{field}`", result)
                result.encode("utf-8")

    async def test_clean_arguments_reach_a_verbatim_handler_untouched(self) -> None:
        for name, arguments in (
            ("edit_editor", {"path": "src/a.py", "old_text": "caf\u00e9", "new_text": "x\U0001f600y"}),
            ("run_in_machine", {"command": "echo \U0001f600"}),
        ):
            with self.subTest(tool=name):
                handler = Recorder()
                await loop(Scripted(calling(name, **arguments)), {name: handler}).run("go")
                self.assertEqual([arguments], handler.got)

    async def test_run_in_machine_keeps_its_refusal_and_the_window_is_never_asked(self) -> None:
        self.bridge.note_ui()
        provider = Scripted(calling("run_in_machine", command="echo \ud800"))

        answer = await loop(provider, {"run_in_machine": self.tools.run_in_machine}).run("go")

        self.assertEqual("done", answer)
        result = [m for m in provider.seen[-1] if m["role"] == "tool"][0]["content"]
        self.assertIn("That was not run in the machine.", result)
        self.assertIn("command", result)
        self.assertEqual(0, self.bridge.state()["inflight"], "the window was asked to type a command that was rewritten")
        self.assertEqual([], [line for line in self.logged if "�" in line])

    async def test_run_in_machine_refusal_is_the_tool_direct_too(self) -> None:
        self.bridge.note_ui()
        text = await self.tools.run_in_machine({"command": "echo \ud800"})
        self.assertIn("That was not run in the machine.", text)
        self.assertEqual(0, self.bridge.state()["inflight"])

    async def test_edit_editor_text_is_still_refused_not_rewritten(self) -> None:
        self.bridge.note_ui()
        for field, arguments in (
            ("new_text", {"path": "src/a.py", "old_text": "1", "new_text": "x\ud800y"}),
            ("old_text", {"path": "src/a.py", "old_text": "1\ud800", "new_text": "x"}),
        ):
            with self.subTest(field=field):
                provider = Scripted(calling("edit_editor", **arguments))
                answer = await loop(provider, {"edit_editor": self.tools.edit_editor}).run("go")
                self.assertEqual("done", answer)
                result = [m for m in provider.seen[-1] if m["role"] == "tool"][0]["content"]
                self.assertIn("That edit was not made.", result)
                self.assertIn(field, result)
                self.assertEqual(0, self.bridge.state()["inflight"])
                self.assertEqual({}, self.edits)


class TestAPageVerbTextDoesNotBreakTheBridge(unittest.IsolatedAsyncioTestCase):
    async def test_type_page_with_a_lone_surrogate_answers_bridge_next_200(self) -> None:
        bridge = WebviewBridge()
        bridge.note_shell()
        app.state.bridge = bridge
        app.state.token = BOOT_TOKEN
        tools = tools_over("/tmp", bridge=bridge, _log=lambda *a, **k: None)
        conductor = Conductor(
            Scripted(calling("type_page", selector="#q", text=POISON)), "m", "/tmp", list(TOOLS),
            {"type_page": tools.type_page}, system_prompt="s", browser_actions_allowed=lambda: True,
        )
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            poll = asyncio.create_task(client.get("/bridge/next?wait=5", headers={"Authorization": f"Bearer {BOOT_TOKEN}"}))
            running = asyncio.create_task(conductor.run("type it"))
            response = await asyncio.wait_for(poll, 10)
            self.assertEqual(200, response.status_code)
            request = response.json()
            self.assertEqual(HEALED, request["args"]["text"] if "args" in request else request["text"])
            bridge.answer(request["id"], True, {"ok": True})
            await asyncio.wait_for(running, 10)


def _anthropic(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "tool_use", "id": "tu1", "name": name, "input": arguments}], "usage": {}}


def _openai(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    call = {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
    return {"choices": [{"message": {"content": None, "tool_calls": [call]}}], "usage": {}}


def _ollama(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    call = {"function": {"name": name, "arguments": arguments}}
    return {"message": {"role": "assistant", "content": "", "tool_calls": [call]}, "done": True}


def _google(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"candidates": [{"content": {"parts": [{"functionCall": {"name": name, "args": arguments}}]}}]}


DONE: dict[str, dict[str, Any]] = {
    "anthropic": {"content": [{"type": "text", "text": "done"}], "usage": {}},
    "openai_compat": {"choices": [{"message": {"content": "done"}}], "usage": {}},
    "ollama": {"message": {"role": "assistant", "content": "done"}, "done": True},
    "google": {"candidates": [{"content": {"parts": [{"text": "done"}]}}]},
}
ASKS = {"anthropic": _anthropic, "openai_compat": _openai, "ollama": _ollama, "google": _google}


def build(kind: str) -> BaseProvider:
    if kind == "anthropic":
        return AnthropicProvider("sk-test", "https://api.anthropic.com")
    if kind == "openai_compat":
        return OpenAICompatProvider("sk-test", "https://api.openai.com/v1")
    if kind == "ollama":
        return OllamaProvider("http://127.0.0.1:11434")
    return GoogleProvider("g-test", "https://generativelanguage.googleapis.com/v1beta")


class TestTheLoopSurvivesOnEveryProtocol(unittest.IsolatedAsyncioTestCase):
    """Through the four real providers, with the model's reply as the bytes it would send: `\\ud800` in a JSON string."""

    async def asyncSetUp(self) -> None:
        self.bodies: list[bytes] = []
        self.responses: list[bytes] = []

        def answer(request: httpx.Request) -> httpx.Response:
            if request.method != "POST":
                return httpx.Response(404)
            self.bodies.append(request.content)
            # `json.dumps` defaults to ASCII, so a lone surrogate is written as the six characters `\ud800` the way a
            # model sends it. (httpx's own `json=` would try to encode the surrogate and raise.)
            return httpx.Response(200, content=self.responses.pop(0), headers={"content-type": "application/json"})

        real = httpx.AsyncClient

        def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(answer)
            return real(*args, **kwargs)

        patcher = mock.patch.object(httpx, "AsyncClient", client)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_lone_surrogate_in_a_tool_argument_neither_kills_the_run_nor_reaches_the_handler(self) -> None:
        for kind, ask in ASKS.items():
            with self.subTest(provider=kind):
                self.bodies.clear()
                self.responses = [
                    json.dumps(ask("read_file", {"path": POISON})).encode("ascii"),
                    json.dumps(DONE[kind]).encode("ascii"),
                ]
                handler = Recorder()

                answer = await loop(build(kind), {"read_file": handler}).run("go")

                self.assertEqual("done", answer)
                self.assertEqual([{"path": HEALED}], handler.got)
                self.assertEqual(2, len(self.bodies), "the second request is the one that raised")
                second = self.bodies[1].decode("utf-8")
                self.assertNotIn("\ud800", second)


if __name__ == "__main__":
    unittest.main()
