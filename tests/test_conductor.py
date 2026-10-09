"""The conductor dispatches; the engine decides what is allowed.

`engine/conductor.py` is a model that calls tools. The thing worth testing is
not that it loops — it is that **every tool it can reach is a call the pipeline
already makes, through the same service, with the same validation**. A conductor
is a new way for a model to reach the filesystem and the process table, and the
only thing standing between "it can choose" and "it can do anything" is that
each entry in its dispatch table is somebody else's already-checked door.

So these tests are mostly negative: a command the sandbox refuses is refused
through the conductor too, a git subcommand that writes is refused, and there
is no tool that writes at all.

The rest is the loop itself: it terminates, it terminates *usefully* when the
cap is hit, a tool name the model invented is a recoverable sentence rather
than a dead turn, and a provider that cannot do tools degrades to a plain
answer rather than failing the question.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.conductor import BROWSER_ACTION_TOOLS, TOOLS, Conductor, tool_names
from engine.db import connect
from engine.fs import PathEscapeError
from engine.git import GitService
from engine.models import (
    ROLES,
    AgentConfigUpdate,
    ConversationCreate,
    Event,
    TurnCreate,
    WorkspaceCreate,
)
from engine.providers import BaseProvider, Keychain, ProviderError, ProviderFactory
from engine.sandbox import CommandNotAllowed, SandboxService
from engine.services import (
    AgentRegistryService,
    ConversationService,
    GoalService,
    SettingsService,
    WorkspaceService,
)
from engine.toolcall import (
    ToolCall,
    ToolReply,
    ToolSpec,
    coerce_arguments,
    coerce_tool_reply,
    parse_anthropic_tool_calls,
    parse_openai_tool_calls,
    to_anthropic_messages,
    to_google_contents,
    to_ollama_messages,
    to_openai_messages,
)
from engine.executor import ExecutorService
from engine.skills import load_skills
from engine.laya import LayaDecision, LayaService


class _ToolProvider(BaseProvider):
    """A scripted model. `replies` is consumed one per `complete_with_tools`.

    `pipeline` is what the single-shot `complete` returns, so the same double
    can serve the conductor's loop *and* the eight roles' JSON calls — which is
    what lets the `delegate` test assert the delegated run is the pipeline's
    own plan rather than a second implementation of one.
    """

    def __init__(self, replies: list[ToolReply] | None = None, pipeline: Any = None) -> None:
        self.replies = list(replies or [])
        self.pipeline = pipeline if pipeline is not None else {
            "summary": "an empty repo", "enough": True,
        }
        self.seen_tools: list[list[str]] = []
        self.seen_messages: list[list[dict[str, Any]]] = []
        # (system, user) per role call. Recorded because a sub-agent's *prompt*
        # is where a caller's instructions either arrive or do not, and a
        # scripted double that only returns canned JSON cannot show the
        # difference — see `TestTheConductorCanDirectWhatItSummons`.
        self.seen_prompts: list[tuple[str, str]] = []

    @property
    def supports_tools(self) -> bool:
        return True

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> str:
        # Keyed on the role's own self-description rather than a loose
        # substring: the planner's prompt *mentions* a design contract, so a
        # `"design" in prompt` test returns the design role's reply to the
        # planner and the run dies with "planner must return 1..20 steps".
        self.seen_prompts.append((system_prompt, user_prompt))
        lowered = system_prompt.lower()
        if "you are codify librarian" in lowered:
            return json.dumps({"summary": "an empty repo", "enough": True})
        if "you are codify design" in lowered:
            return json.dumps({"applies": False})
        if "you are codify planner" in lowered:
            return json.dumps({"steps": [{"title": "S1", "description": "d", "suggested_paths": []}]})
        return json.dumps(self.pipeline)

    async def complete_with_tools(
        self, system_prompt: str, messages: list[dict[str, Any]],
        tools: list[ToolSpec], model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> ToolReply:
        self.seen_tools.append([t.name for t in tools])
        self.seen_messages.append([dict(m) for m in messages])
        if not self.replies:
            return ToolReply(text="I have run out of things to say.")
        return self.replies.pop(0)


class _PlainProvider(BaseProvider):
    """A provider with no tool support at all — the base class's answer."""

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> str:
        return "a plain answer"


class _DyingProvider(_ToolProvider):
    """A provider that fails the calls `fail_on` names, and behaves otherwise.

    Keyed by call number rather than a queue of codes, because the test that
    matters is the one where the provider served a call *first* and died on the
    second — the case where a fallback that restarted the run rather than the
    call would double every move already made.
    """

    def __init__(self, fail_on: dict[int, str] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail_on = dict(fail_on or {})
        self.attempts: list[str] = []
        self.served = 0

    async def complete_with_tools(
        self, system_prompt: str, messages: list[dict[str, Any]],
        tools: list[ToolSpec], model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> ToolReply:
        self.attempts.append(model)
        self.served += 1
        if self.served in self.fail_on:
            raise ProviderError(self.fail_on[self.served], "the endpoint refused the call")
        return await super().complete_with_tools(
            system_prompt, messages, tools, model, temperature, max_tokens,
        )


def _call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"c_{name}", name=name, arguments=arguments)


class ConductorTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("def parse(text):\n    return text\n")
        (self.repo / "README.md").write_text("# demo\n")
        self.conn: sqlite3.Connection = connect(self.root / "conductor.db")
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)
        self.conversations = ConversationService(self.conn)
        self.settings = SettingsService(self.conn)
        self.sandbox = SandboxService()
        self.git = GitService()
        self.ws = self.workspaces.create(
            WorkspaceCreate(name="WS", root_path=str(self.repo))
        )
        self.thread = self.conversations.create(ConversationCreate(workspace_id=self.ws.id))
        self.goal = self.goals.create_turn(self.thread.id, TurnCreate(prompt="hi"))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _executor(self, provider: BaseProvider, laya: LayaService | None = None) -> ExecutorService:
        registry = AgentRegistryService(self.conn, _Factory(provider), Keychain())
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(model_name="stub-model"))
        return ExecutorService(
            self.goals, self.workspaces, registry, self.sandbox,
            laya=laya or _QuestionGate(),
        )

    def _dispatch(self, goal_id: str, **overrides: Any) -> dict[str, Any]:
        # `_ToolProvider` so the `plan` test can drive the *real* pipeline
        # through the same double the loop uses.
        executor = self._executor(_ToolProvider())
        table = executor._conductor_dispatch(
            goal_id, self.goals.get(goal_id), str(self.repo),
            load_skills(str(self.repo)),
        )
        table.update(overrides)
        return table


class _Factory(ProviderFactory):
    def __init__(self, provider: BaseProvider) -> None:
        self.provider = provider

    def build(self, config: Any) -> BaseProvider:
        return self.provider


class _QuestionGate(LayaService):
    async def decide(self, state: dict[str, Any], on_call: Any = None) -> LayaDecision:
        return LayaDecision(
            engine="sdk",
            answers={"intent": {"choice": "question", "confidence": 0.99}},
        )


class TestTheLoop(ConductorTestCase):
    async def test_a_model_that_calls_a_tool_gets_the_result_and_can_finish(self) -> None:
        calls: list[dict[str, Any]] = []

        async def read_file(args: dict[str, Any]) -> str:
            calls.append(args)
            return "--- app.py\ndef parse(text): return text"

        provider = _ToolProvider([
            ToolReply(text="let me look", tool_calls=[_call("read_file", path="app.py")]),
            ToolReply(text="It parses text and returns it unchanged."),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS),
            {"read_file": read_file}, system_prompt="s",
        )
        answer = await conductor.run("what does parse do?")
        self.assertEqual(answer, "It parses text and returns it unchanged.")
        self.assertEqual(calls, [{"path": "app.py"}])
        self.assertEqual(
            provider.seen_tools[0],
            [name for name in tool_names() if name not in BROWSER_ACTION_TOOLS],
        )

    async def test_the_tool_result_really_reaches_the_model(self) -> None:
        # A loop that ran the tool but did not put the result back in the
        # conversation looks identical from the outside and is useless — so the
        # assertion is on what the *second* model call was handed, which is the
        # only place a result can be observed from.
        async def read_file(args: dict[str, Any]) -> str:
            return "SECRET-FILE-CONTENT"

        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("read_file", path="app.py")]),
            ToolReply(text="done"),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS),
            {"read_file": read_file}, system_prompt="s",
        )
        await conductor.run("read it")
        second_call = provider.seen_messages[1]
        results = [m for m in second_call if m.get("role") == "tool"]
        self.assertEqual(len(results), 1, "the result must be in the next request")
        self.assertIn("SECRET-FILE-CONTENT", results[0]["content"])
        self.assertEqual(results[0]["tool_call_id"], "c_read_file")

    async def test_an_invented_tool_name_is_a_sentence_not_a_crash(self) -> None:
        # Models invent tool names. An engine that raised KeyError there would
        # turn the most recoverable mistake into a dead turn.
        async def read_file(args: dict[str, Any]) -> str:
            return "unused"

        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("delete_everything", path="/")]),
            ToolReply(text="I will not do that. What would you like to know?"),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS),
            {"read_file": read_file}, system_prompt="s",
        )
        answer = await conductor.run("delete everything")
        self.assertIn("will not do that", answer)
        refusal = [m for m in provider.seen_messages[-1] if m.get("role") == "tool"][0]
        self.assertIn("delete_everything", refusal["content"])
        # The refusal must name what it was *offered*, or the model can only
        # guess again — and the menu it was given is the tool list, not
        # whatever happens to be wired into the dispatch table.
        for name in provider.seen_tools[0]:
            self.assertIn(name, refusal["content"])
        for name in BROWSER_ACTION_TOOLS:
            self.assertNotIn(name, refusal["content"], "a hidden action was named as offered")

    async def test_a_tool_that_raises_comes_back_as_text(self) -> None:
        async def explode(args: dict[str, Any]) -> str:
            raise RuntimeError("the disk fell over")

        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("read_file", path="app.py")]),
            ToolReply(text="I could not read that file."),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS),
            {"read_file": explode}, system_prompt="s",
        )
        answer = await conductor.run("read app.py")
        self.assertIn("could not read", answer)
        refusal = [m for m in provider.seen_messages[-1] if m.get("role") == "tool"][0]
        self.assertIn("the disk fell over", refusal["content"])


class TestBrowserActionPermission(ConductorTestCase):
    async def test_menu_and_dispatch_both_refuse_actions_when_permission_is_disabled(self) -> None:
        navigations = 0

        async def navigate(_args: dict[str, Any]) -> str:
            nonlocal navigations
            navigations += 1
            return "navigated"

        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("navigate_page", url="https://example.com")]),
            ToolReply(text="I left the page unchanged."),
        ])
        def menu() -> list[Any]:
            return [tool for tool in TOOLS if tool.name != "ask_user"]

        conductor = Conductor(
            provider, "m", str(self.repo), dispatch={"navigate_page": navigate},
            system_prompt="s", menu=menu,
        )

        await conductor.run("read this page")

        self.assertEqual(navigations, 0, "a hidden browser action reached its handler")
        self.assertNotIn("navigate_page", provider.seen_tools[0])
        result = next(message["content"] for message in provider.seen_messages[1] if message.get("role") == "tool")
        self.assertIn("Browser actions are turned off", result)

    async def test_permission_is_checked_again_at_dispatch_time(self) -> None:
        enabled = True
        navigations = 0

        async def navigate(_args: dict[str, Any]) -> str:
            nonlocal navigations
            navigations += 1
            return "navigated"

        async def turn_permission_off(_args: dict[str, Any]) -> str:
            nonlocal enabled
            enabled = False
            return "setting changed"

        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("read_file", path="app.py")]),
            ToolReply(tool_calls=[_call("navigate_page", url="https://example.com")]),
            ToolReply(text="done"),
        ])
        tools = [tool for tool in TOOLS if tool.name != "ask_user"]

        def menu() -> list[Any]:
            return tools

        conductor = Conductor(
            provider, "m", str(self.repo), dispatch={
                "read_file": turn_permission_off, "navigate_page": navigate,
            }, system_prompt="s", menu=menu,
            browser_actions_allowed=lambda: enabled,
        )

        await conductor.run("read this page")

        self.assertEqual(navigations, 0, "permission was only checked when the menu was built")
        result = next(
            message["content"]
            for message in provider.seen_messages[2]
            if message.get("role") == "tool" and message.get("name") == "navigate_page"
        )
        self.assertIn("Browser actions are turned off", result)


class TestTheCap(ConductorTestCase):
    async def test_a_model_that_never_stops_is_stopped(self) -> None:
        # The cap is on model calls, because that is what costs money. Without
        # it a confused model is a way to spend a key and a machine's time.
        calls = {"n": 0}

        async def read_file(args: dict[str, Any]) -> str:
            calls["n"] += 1
            return "still going"

        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("read_file", path="app.py")]) for _ in range(50)
        ] + [ToolReply(text="Here is everything I found.")])

        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS),
            {"read_file": read_file}, system_prompt="s", max_turns=4,
        )
        answer = await conductor.run("keep going")
        # max_turns=4 buys four tool rounds and then one more call — the one
        # that is told to answer, whose tool calls are then dropped. A turn
        # that stopped at four would end on a tool call with nothing said.
        self.assertEqual(len(provider.seen_messages), 5)
        self.assertTrue(conductor.exhausted)
        # The tool it asked for on that last round was not run.
        self.assertEqual(calls["n"], 4)
        # And the answer says it was cut off, rather than trailing off.
        self.assertIn("ran out of calls", answer)

    async def test_hitting_the_cap_still_answers_rather_than_stopping_dead(self) -> None:
        # A turn cut off mid-thought reads as broken; one that says what it has
        # reads as a limit. The nudge is a *prompt* to the model, so the
        # assertion is that it reached the model at all.
        async def read_file(args: dict[str, Any]) -> str:
            return "partial"

        provider = _ToolProvider([ToolReply(tool_calls=[_call("read_file", path="a.py")])])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS),
            {"read_file": read_file}, system_prompt="s", max_turns=1,
        )
        answer = await conductor.run("go")
        self.assertTrue(answer, "an exhausted turn must still say something")
        last = provider.seen_messages[-1][-1]
        self.assertIn("what you did not get to", last["content"])
        self.assertTrue(conductor.exhausted)

    async def test_a_model_that_finishes_early_is_not_pushed_to_the_cap(self) -> None:
        provider = _ToolProvider([ToolReply(text="already done")])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {}, system_prompt="s", max_turns=8,
        )
        await conductor.run("hi")
        self.assertFalse(conductor.exhausted)
        self.assertEqual(len(provider.seen_messages), 1)


class TestTheToolsAreThePipelinesDoors(ConductorTestCase):
    async def test_the_only_door_to_a_file_is_the_gated_write_move(self) -> None:
        # The single most important property of this feature, restated for a
        # conductor that can now drive stages itself. There is still no direct
        # file writer: a `write_file` tool would make the planner, the verifier
        # and the critic optional for any change the conductor felt like making
        # on its own. `write` exists because the conductor has to be able to say
        # "do this step next" — but it is the fixer's own method, and the next
        # test is the one that matters: it refuses until a person approves.
        names = set(tool_names())
        for forbidden in ("write_file", "edit_file", "apply_patch", "commit"):
            self.assertNotIn(forbidden, names)
        # The whole-pipeline button is gone: it ran all seven stages whether or
        # not the request needed them, which is exactly the hardcoding this
        # replaced.
        self.assertNotIn("delegate", names)
        self.assertEqual(
            names,
            {
                "read_file", "search_code", "scan_code", "git_history", "run_command",
                "read_page", "navigate_page", "click_page", "type_page", "fetch_page",
                "read_editor", "open_in_editor", "edit_editor",
                "read_machine", "run_in_machine", "key_in_machine", "reset_machine",
                "recall", "recall_threads", "use_skill", "recon", "design",
                "plan", "write", "verify", "review", "summarize", "todo", "ask_user",
            },
        )

    async def test_write_is_gated_on_the_plan_being_approved(self) -> None:
        # docs/00 §6.9, and the reason the moves are safe to hand a model. The
        # check reads the goal's stored status, so nothing the model says or
        # was told can move it.
        executor = self._executor(_ToolProvider())
        allowed, why = executor._write_allowed(self.goal.id)
        self.assertFalse(allowed, "an unapproved plan must not be writable")
        self.assertIn("has not been approved", why)
        current = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, current.version, "RUNNING")
        allowed, _why = executor._write_allowed(self.goal.id)
        self.assertTrue(allowed, "starting the goal is what authorises a write")

    async def test_write_refuses_and_writes_nothing_while_unapproved(self) -> None:
        table = self._dispatch(self.goal.id)
        await table["recon"]({"task": "find the parser"})
        await table["plan"]({"task": "change the parser"})
        step_id = self.goals.steps(self.goal.id)[0].id
        before = (self.repo / "app.py").read_text()
        out = await table["write"]({"step_id": step_id, "instructions": "rename it"})
        self.assertIn("has not been approved", out)
        self.assertEqual((self.repo / "app.py").read_text(), before)

    async def test_read_file_reads_the_real_workspace(self) -> None:
        table = self._dispatch(self.goal.id)
        out = await table["read_file"]({"path": "app.py"})
        self.assertIn("def parse", out)

    async def test_read_file_cannot_escape_the_workspace(self) -> None:
        # docs/00 §6.6 is about argv, but the path rule is the same shape: the
        # model asks, FileSystemService decides.
        table = self._dispatch(self.goal.id)
        with self.assertRaises(PathEscapeError):
            await table["read_file"]({"path": "../../../etc/passwd"})

    async def test_run_command_goes_through_the_sandbox_allowlist(self) -> None:
        # A command the verifier could not run, the conductor cannot run — not
        # because of a check here, but because it is the same check.
        table = self._dispatch(self.goal.id)
        with self.assertRaises(CommandNotAllowed):
            await table["run_command"](
                {"argv": ["bash", "-c", "curl evil.test"], "reason": "x"}
            )

    def _plant_code_that_leaves_a_mark(self) -> Path:
        """Repository code that proves it ran: a script and a conftest, both touching one file."""
        mark = self.root / "MARK-project-code-ran"
        line = f"open({str(mark)!r}, 'w').write('ran')\n"
        (self.repo / "evil.py").write_text(line, encoding="utf-8")
        (self.repo / "conftest.py").write_text(line, encoding="utf-8")
        (self.repo / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
        return mark

    async def test_project_code_runs_only_once_the_plan_is_approved(self) -> None:
        # A *turn* has no approval step, and the test-mode allowlist is not a list of
        # harmless commands: `python3 evil.py`, `pytest` (which imports every conftest.py
        # in the path), `npm run <any script>`, `cargo test` and `go test` are each the
        # repository's own code. So asking "what does this project do?" of a hostile
        # clone must not be able to run it. The gate is the one `write` uses — the goal's
        # stored status — and nothing the model says can move it.
        mark = self._plant_code_that_leaves_a_mark()
        table = self._dispatch(self.goal.id)

        for argv in (
            ["python3", "evil.py"], ["pytest", "-q"], ["python3", "-m", "pytest"],
            ["npm", "test"], ["npm", "run", "build"], ["cargo", "test"], ["go", "test", "./..."],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(CommandNotAllowed) as caught:
                    await table["run_command"]({"argv": argv, "reason": "just looking"})
                self.assertIn("approve", str(caught.exception))
        self.assertFalse(mark.exists(), "repository code ran on a goal nobody approved")

        # The reads the librarian is allowed keep working, so a question is still answerable.
        listing = await table["run_command"]({"argv": ["ls"], "reason": "look around"})
        self.assertIn("app.py", listing)
        counted = await table["run_command"]({"argv": ["wc", "-l", "app.py"], "reason": "size"})
        self.assertIn("app.py", counted)

        # Starting the goal is a person saying yes; the same table, the same argv, now runs.
        current = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, current.version, "RUNNING")
        out = await table["run_command"]({"argv": ["python3", "evil.py"], "reason": "approved"})
        self.assertIn("(exit 0)", out)
        self.assertTrue(mark.exists(), "an approved goal could not run its project's command")

    async def test_a_linter_runs_project_code_only_once_the_plan_is_approved(self) -> None:
        # The linters on the test allowlist are not read-only: mypy imports the plugins a
        # `mypy.ini` names, and `make lint` is whatever the Makefile says. Both are the
        # repository's own code, so both wait for the same approval `python3 evil.py` does
        # (docs/03 section 1.4), and a turn asking "does this type-check?" of a hostile clone
        # runs nothing.
        plugin_mark = self.root / "MARK-mypy-plugin-ran"
        make_mark = self.root / "MARK-make-ran"
        (self.repo / "plugin.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(plugin_mark)!r}).write_text('ran')\n"
            "def plugin(version):\n"
            "    from mypy.plugin import Plugin\n"
            "    return Plugin\n",
            encoding="utf-8",
        )
        (self.repo / "mypy.ini").write_text("[mypy]\nplugins = plugin.py\n", encoding="utf-8")
        (self.repo / "Makefile").write_text(
            f"lint:\n\t@touch {make_mark}\n", encoding="utf-8"
        )
        table = self._dispatch(self.goal.id)

        for argv in (["mypy", "app.py"], ["make", "lint"], ["ruff", "check"], ["tsc", "--noEmit"],
                     ["cargo", "check"], ["go", "vet", "./..."]):
            with self.subTest(argv=argv):
                with self.assertRaises(CommandNotAllowed) as caught:
                    await table["run_command"]({"argv": argv, "reason": "does it type-check?"})
                self.assertIn("approve", str(caught.exception))
        self.assertFalse(plugin_mark.exists(), "a mypy plugin ran on a goal nobody approved")
        self.assertFalse(make_mark.exists(), "`make lint` ran on a goal nobody approved")

        current = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, current.version, "RUNNING")
        out = await table["run_command"]({"argv": ["make", "lint"], "reason": "approved"})
        self.assertIn("(exit 0)", out)
        self.assertTrue(make_mark.exists(), "an approved goal could not run its project's lint")
        await table["run_command"]({"argv": ["mypy", "app.py"], "reason": "approved"})
        self.assertTrue(plugin_mark.exists(), "an approved goal could not run mypy with its plugin")

    async def test_a_plan_only_goal_never_runs_project_code_even_when_running(self) -> None:
        # `plan_only` switches execution off for the goal (`_write_allowed` says so for
        # writes); a command is execution, so the same switch covers it.
        mark = self._plant_code_that_leaves_a_mark()
        self.conn.execute("UPDATE goals SET plan_only=1 WHERE id=?", (self.goal.id,))
        self.conn.commit()
        current = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, current.version, "RUNNING")
        table = self._dispatch(self.goal.id)

        with self.assertRaises(CommandNotAllowed):
            await table["run_command"]({"argv": ["python3", "evil.py"], "reason": "x"})
        self.assertFalse(mark.exists())

    async def test_a_command_no_mode_allows_keeps_the_ordinary_refusal(self) -> None:
        # The approval explanation is for a command that *would* run once approved. A shell
        # is refused whatever the goal's state, and the message must not suggest that
        # approval would change that.
        table = self._dispatch(self.goal.id)
        with self.assertRaises(CommandNotAllowed) as caught:
            await table["run_command"]({"argv": ["bash", "-c", "id"], "reason": "x"})
        self.assertNotIn("approve", str(caught.exception))

    async def test_an_unknown_command_is_refused_rather_than_attempted(self) -> None:
        table = self._dispatch(self.goal.id)
        with self.assertRaises(CommandNotAllowed):
            await table["run_command"](
                {"argv": ["definitely-not-a-real-binary"], "reason": "x"}
            )

    async def test_a_slow_command_does_not_hold_the_event_loop(self) -> None:
        """The command runs in a thread, so the loop keeps turning while it does.

        `run_command` starts a real process for up to 120 seconds, and it used to
        start it *inside* the event loop: every WebSocket tick, `/health` probe
        and cancel request queued behind the command, so the app reported itself
        offline for exactly as long as the command ran. The ticker here is those
        requests — a task that only needs the loop to come back to it. It is the
        difference between ~12 ticks and none.
        """
        (self.repo / "slow.py").write_text(
            "import time\ntime.sleep(0.6)\n", encoding="utf-8"
        )
        # Running the project's code is what an approved plan is for (see
        # `test_project_code_runs_only_once_the_plan_is_approved`).
        current = self.goals.get(self.goal.id)
        self.goals.update_status(self.goal.id, current.version, "RUNNING")
        table = self._dispatch(self.goal.id)
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        ticker = asyncio.create_task(tick())
        try:
            out = await table["run_command"]({"argv": ["python3", "slow.py"], "reason": "t"})
        finally:
            ticker.cancel()

        self.assertIn("(exit 0)", out)
        self.assertGreaterEqual(
            ticks, 5, "the event loop was blocked for the whole command"
        )

    async def test_git_history_refuses_a_subcommand_that_writes(self) -> None:
        # The caller is a model. "The model asked for it" is not a reason to
        # run `git commit`.
        table = self._dispatch(self.goal.id)
        for argv in (["commit", "-m", "sneaky"], ["push"], ["reset", "--hard"]):
            out = await table["git_history"]({"args": argv})
            self.assertIn("not a read-only git command", out, f"{argv} was not refused")

    async def test_git_history_reads_a_real_repository(self) -> None:
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=False)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "add", "-A"], cwd=self.repo, check=False)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "first"], cwd=self.repo, check=False)
        table = self._dispatch(self.goal.id)
        out = await table["git_history"]({"args": ["log", "--oneline"]})
        self.assertIn("first", out)

    async def test_recon_then_plan_reaches_the_real_pipeline(self) -> None:
        # The moves are the pipeline's own stages: `plan` produces the same
        # steps a POST /goals produces, through the same planner.
        table = self._dispatch(self.goal.id)
        recon = await table["recon"]({"task": "find the parser"})
        self.assertIn("empty repo", recon, "recon must return the librarian's findings")
        out = await table["plan"]({"task": "add a test"})
        payload = json.loads(out)
        self.assertEqual(
            [s["title"] for s in payload["steps"]], ["S1"],
            "the plan must be the pipeline's own plan",
        )
        self.assertEqual(payload["status"], "PENDING")
        self.assertIn("step_id", payload["steps"][0])

    async def test_plan_refuses_without_evidence(self) -> None:
        # The one guard that has to be in code rather than in a prompt: a plan
        # written against a guessed file layout edits the wrong files.
        table = self._dispatch(self.goal.id)
        out = await table["plan"]({"task": "change something"})
        self.assertIn("no evidence", out)
        self.assertEqual(self.goals.steps(self.goal.id), [])

    async def test_a_move_addressed_to_an_unknown_step_says_which_exist(self) -> None:
        table = self._dispatch(self.goal.id)
        out = await table["write"]({"step_id": "nope", "instructions": "x"})
        self.assertIn("no steps yet", out)
        self.assertIn("`plan`", out)

    async def test_verify_refuses_before_a_write(self) -> None:
        # Verification judges a change. With no change there is nothing to
        # judge, and pretending otherwise would produce a passing verdict for
        # work that was never done.
        table = self._dispatch(self.goal.id)
        await table["recon"]({"task": "find the parser"})
        await table["plan"]({"task": "change the parser"})
        step_id = self.goals.steps(self.goal.id)[0].id
        out = await table["verify"]({"step_id": step_id})
        self.assertIn("Call `write` first", out)

    async def test_use_skill_returns_the_body_and_lists_the_menu(self) -> None:
        table = self._dispatch(self.goal.id)
        body = await table["use_skill"]({"name": "ship-a-change"})
        self.assertIn("recon", body)
        self.assertIn("approval", body.lower())
        out = await table["use_skill"]({"name": "not-a-skill"})
        self.assertIn("no skill called", out)
        self.assertIn("ship-a-change", out)


class TestGracefulDegradation(ConductorTestCase):
    async def test_a_provider_without_tools_still_answers_the_question(self) -> None:
        # The conductor is an upgrade, never a prerequisite. A user on a
        # provider that cannot do tool calling must still be able to ask a
        # question — that was the bug this whole feature fixes, and shipping it
        # with a new "your provider is too old" failure would have replaced one
        # unusable behaviour with another.
        provider = _PlainProvider()
        executor = self._executor(provider)
        await executor.run_chat(self.goal.id)
        self.assertEqual(self.goals.get(self.goal.id).status, "COMPLETED")
        replies = [
            p for p in [
                e.payload for e in self.goals.events_after(self.goal.id, 0) if e.type == "log"
            ] if p.get("turn")
        ]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["message"], "a plain answer")

    async def test_a_role_with_no_model_fails_the_turn_cleanly(self) -> None:
        # Not an unhandled exception in a background task — `run_chat` is
        # spawned by the route with `_spawn`, and a raise here would be a
        # coroutine that dies with nothing written. The goal is FAILED and the
        # reason is on the log, which is what the transcript renders.
        registry = AgentRegistryService(self.conn, _Factory(_PlainProvider()), Keychain())
        for role in ROLES:
            if role != "scribe":
                registry.set_config(role, AgentConfigUpdate(model_name="stub-model"))
        # scribe deliberately left unconfigured: it is the row a turn borrows.
        executor = ExecutorService(
            self.goals, self.workspaces, registry, self.sandbox, laya=_QuestionGate(),
        )
        goal = self.goals.create_turn(self.thread.id, TurnCreate(prompt="hi"))
        await executor.run_chat(goal.id)
        self.assertEqual(self.goals.get(goal.id).status, "FAILED")
        errors = [e for e in self.goals.events_after(goal.id, 0) if e.type == "error"]
        self.assertTrue(errors, "a failed turn must say why on the event log")
        self.assertIn("scribe", str(errors[0].payload))


class TestCancelStopsAConductorRun(ConductorTestCase):
    """M1: cancel did nothing for a chat turn (audit of 2026-09-29).

    The conductor had no cancellation check anywhere, so it kept calling the model and the tools
    up to `conductor_max_turns`, and `run_chat` then ended with an unconditional `COMPLETED` that
    overwrote `CANCELLED` and published the reply anyway. `run_planning` had guards for exactly
    this; `run_chat`, added later, did not.
    """

    async def test_a_cancel_is_noticed_before_the_next_tool_and_the_next_model_call(self) -> None:
        cancelled = {"now": False}
        ran: list[str] = []

        async def read_file(args: dict[str, Any]) -> str:
            ran.append(str(args["path"]))
            cancelled["now"] = True  # the user pressed Cancel while this tool ran
            return "body"

        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("read_file", path="a.py"), _call("read_file", path="b.py")]),
            ToolReply(text="an answer nobody asked for any more"),
        ])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {"read_file": read_file},
            system_prompt="s", max_turns=8, cancelled=lambda: cancelled["now"],
        )

        answer = await conductor.run("go")

        self.assertEqual(["a.py"], ran, "a second tool ran after the cancel")
        self.assertEqual(1, len(provider.seen_messages), "the model was called again after the cancel")
        self.assertTrue(conductor.was_cancelled)
        self.assertEqual("", answer)

    async def test_a_conductor_cancelled_before_it_starts_makes_no_call_at_all(self) -> None:
        provider = _ToolProvider([ToolReply(text="hello")])
        conductor = Conductor(
            provider, "m", str(self.repo), list(TOOLS), {}, system_prompt="s",
            max_turns=8, cancelled=lambda: True,
        )

        await conductor.run("go")

        self.assertEqual([], provider.seen_messages)

    async def test_without_a_predicate_nothing_changes(self) -> None:
        provider = _ToolProvider([ToolReply(text="hello")])
        conductor = Conductor(provider, "m", str(self.repo), list(TOOLS), {}, system_prompt="s", max_turns=8)

        self.assertEqual("hello", await conductor.run("go"))
        self.assertFalse(conductor.was_cancelled)

    async def test_a_runner_setting_a_status_on_a_cancelled_goal_is_a_quiet_no_op(self) -> None:
        # `_set_status` is what every runner goes through. Refusing the move is the service's job;
        # not crashing a background task over it, and not consolidating memory for a run that
        # never finished, is this one's.
        executor = self._executor(_ToolProvider())
        g = self.goals.get(self.goal.id)
        self.goals.update_status(g.id, g.version, "CANCELLED")
        before = len(self.goals.events_after(self.goal.id, 0))

        executor._set_status(self.goal.id, "COMPLETED", None)

        self.assertEqual("CANCELLED", self.goals.get(self.goal.id).status)
        statuses = [
            e for e in self.goals.events_after(self.goal.id, 0) if e.type == "goal_status"
        ]
        self.assertEqual("CANCELLED", statuses[-1].payload["status"])
        self.assertEqual(before, len(self.goals.events_after(self.goal.id, 0)), "a refused status was announced")

    async def test_a_chat_turn_cancelled_mid_run_stays_cancelled_and_publishes_no_reply(self) -> None:
        provider = _ToolProvider([
            ToolReply(tool_calls=[_call("read_file", path="app.py")]),
            ToolReply(text="the final answer"),
        ])
        executor = self._executor(provider)
        original_publish = self.goals.publish

        def publish_and_cancel(event: Event) -> Event:
            result = original_publish(event)
            # The engine logs "conductor called read_file(...)" as the tool starts: that is
            # the moment a person, watching the transcript, presses Cancel.
            if event.type == "log" and "conductor called read_file" in str(event.payload.get("message")):
                g = self.goals.get(self.goal.id)
                self.goals.update_status(g.id, g.version, "CANCELLED")
            return result

        self.goals.publish = publish_and_cancel  # type: ignore[method-assign]
        await executor.run_chat(self.goal.id)

        self.assertEqual("CANCELLED", self.goals.get(self.goal.id).status, "the goal was revived")
        replies = [
            e for e in self.goals.events_after(self.goal.id, 0)
            if e.type == "log" and e.payload.get("turn")
        ]
        self.assertEqual([], replies, "a reply was published for a turn the user had cancelled")
        self.assertEqual(1, len(provider.seen_messages), "the conductor kept calling the model after the cancel")
        # And it did not treat the cancel as a conductor that "could not finish", which would
        # run Codify's own sequence — a planner call — for a goal nobody wants any more.
        events = self.goals.events_after(self.goal.id, 0)
        self.assertFalse(
            [e for e in events if "did not finish" in str(e.payload) or "standard sequence" in str(e.payload)],
            "a cancelled turn fell through to the engine's own sequence",
        )
        self.assertEqual([], [p for p in provider.seen_prompts if "planner" in p[0].lower()])


class TestToolDialects(unittest.TestCase):
    """The translations, which are the part with no type checker.

    A `content` block that should be a `tool_result` is a 400 from someone
    else's API, not an error in our code — so these are the assertions that
    would otherwise only be made against a live endpoint.
    """

    def test_openai_tool_results_are_their_own_role(self) -> None:
        out = to_openai_messages([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [_call("read_file", path="a.py")]},
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "body"},
        ])
        self.assertEqual(out[2], {
            "role": "tool", "tool_call_id": "c1", "content": "body",
        })
        self.assertEqual(out[1]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(out[1]["tool_calls"][0]["function"]["arguments"]),
                         {"path": "a.py"})

    def test_anthropic_shares_one_turn_between_consecutive_results(self) -> None:
        # A `tool_result` per message is rejected by the API; they share a turn.
        out = to_anthropic_messages([
            {"role": "assistant", "content": "", "tool_calls": [
                _call("read_file", path="a"), _call("read_file", path="b"),
            ]},
            {"role": "tool", "tool_call_id": "1", "content": "x"},
            {"role": "tool", "tool_call_id": "2", "content": "y"},
        ])
        self.assertEqual(len(out), 2)
        self.assertEqual(len(out[1]["content"]), 2)
        self.assertTrue(all(b["type"] == "tool_result" for b in out[1]["content"]))

    def test_anthropic_turns_a_tool_use_into_a_block(self) -> None:
        out = to_anthropic_messages([
            {"role": "assistant", "content": "looking", "tool_calls": [_call("read_file", path="a")]},
        ])
        self.assertEqual(out[0]["content"][0], {"type": "text", "text": "looking"})
        self.assertEqual(out[0]["content"][1]["type"], "tool_use")
        self.assertEqual(out[0]["content"][1]["input"], {"path": "a"})

    def test_google_uses_model_and_function_response(self) -> None:
        out = to_google_contents([
            {"role": "assistant", "content": "", "tool_calls": [_call("read_file", path="a")]},
            {"role": "tool", "tool_call_id": "1", "name": "read_file", "content": "x"},
        ])
        self.assertEqual(out[0]["role"], "model")
        self.assertEqual(out[0]["parts"][0]["functionCall"]["name"], "read_file")
        self.assertEqual(out[1]["role"], "user")
        self.assertEqual(
            out[1]["parts"][0]["functionResponse"]["name"], "read_file",
        )

    def test_google_required_comes_from_the_schema_not_the_type(self) -> None:
        # All five tools declare their required parameter as a *string*, so the
        # old `type != "string"` rule sent Gemini: read_file with no required
        # path, and `offset`/`limit` required though they are optional. Gemini
        # accepts that document, so nothing failed — the model just called the
        # tool wrong. Pin the real specs, because a synthetic one would have
        # agreed with whichever rule wrote it.
        by_name = {t.name: t for t in TOOLS}
        sent = by_name["read_file"].to_google()["parameters"]
        self.assertEqual(sent["required"], ["path"])
        self.assertNotIn("offset", sent["required"])
        self.assertNotIn("limit", sent["required"])
        # A required non-string survives the other way: it was already right by
        # accident, and now it is right on purpose.
        self.assertEqual(
            by_name["run_command"].to_google()["parameters"]["required"],
            ["argv"],
        )
        self.assertEqual(
            by_name["search_code"].to_google()["parameters"]["required"],
            ["query"],
        )

    def test_a_json_string_of_arguments_is_parsed(self) -> None:
        # OpenAI hands back a string, Anthropic and Google hand back an object.
        # A caller that had to care would be three parsers in the conductor.
        calls = parse_openai_tool_calls({
            "tool_calls": [{
                "id": "x", "function": {"name": "r", "arguments": '{"path":"a.py"}'},
            }]
        })
        self.assertEqual(calls[0].arguments, {"path": "a.py"})
        self.assertEqual(parse_anthropic_tool_calls(
            [{"type": "tool_use", "id": "y", "name": "r", "input": {"path": "b.py"}}]
        )[0].arguments, {"path": "b.py"})

    def test_half_written_arguments_do_not_kill_the_loop(self) -> None:
        calls = parse_openai_tool_calls({
            "tool_calls": [{"id": "x", "function": {"name": "r", "arguments": '{"path":'}}]
        })
        self.assertEqual(calls[0].arguments, {})

    def test_google_stringly_types_are_coerced_back(self) -> None:
        # Google returns every argument as a string. Without this, an `offset`
        # the model sent as 10 arrives as "10" and a range read is nonsense.
        spec = ToolSpec(
            name="read_file", description="",
            parameters={"type": "object", "properties": {"offset": {"type": "integer"}}},
        )
        self.assertEqual(coerce_arguments(spec, {"offset": "10"}), {"offset": 10})

    def test_ollama_needs_arguments_as_an_object_not_a_string(self) -> None:
        # Measured: OpenAI's format says `arguments` is a JSON *string*;
        # Ollama's Go template parses it as an object and 400s with
        # `Value looks like object, but can't find closing '}' symbol` when it
        # gets the string. Sending the OpenAI shape to Ollama is a hard failure
        # of the second half of every conductor loop.
        out = to_ollama_messages([
            {"role": "assistant", "content": "", "tool_calls": [_call("read_file", path="a.py")]},
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "body"},
        ])
        self.assertEqual(out[0]["tool_calls"][0]["function"]["arguments"], {"path": "a.py"})

    def test_openai_still_gets_the_string(self) -> None:
        # The two disagree, and neither is "the" format — so this pins the
        # other half: sending an object to an OpenAI-compat server is the same
        # bug with the labels swapped.
        out = to_openai_messages([
            {"role": "assistant", "content": "", "tool_calls": [_call("read_file", path="a.py")]},
        ])
        self.assertEqual(out[0]["tool_calls"][0]["function"]["arguments"], '{"path": "a.py"}')

    def test_ollama_dialect_survives_unparseable_arguments(self) -> None:
        # A half-written argument object must degrade to {} rather than raise
        # inside a transport function, where the model cannot see it.
        out = to_ollama_messages([{
            "role": "assistant", "content": "",
            "tool_calls": [ToolCall("c", "read_file", {})],
        }])
        self.assertEqual(out[0]["tool_calls"][0]["function"]["arguments"], {})

    def test_a_tool_with_no_id_still_gets_one(self) -> None:
        # The id is the correlation handle; a missing one means the model
        # cannot match a result to its question.
        self.assertTrue(parse_openai_tool_calls(
            {"tool_calls": [{"function": {"name": "r", "arguments": "{}"}}]}
        )[0].id)


class TestProvidersThatWriteToolCallsAsText(unittest.TestCase):
    """Measured against a live model, not assumed.

    `qwen2.5-coder:7b` on Ollama was asked to call `read_file` and replied with
    `message.tool_calls` **empty** and the call written into `message.content`
    as a JSON document. A provider that read only `tool_calls` treated that JSON
    as the turn's final answer and showed it to the user. No test double finds
    this, because a double returns what the implementation expects.

    The reply below is the real body, verbatim.
    """

    LIVE_REPLY: dict[str, Any] = {
        "model": "qwen2.5-coder:7b",
        "message": {
            "role": "assistant",
            "content": '{"name": "read_file", "arguments": {"path": "app.py"}}',
        },
        "done": True,
    }

    def _tools(self) -> list[ToolSpec]:
        return list(TOOLS)

    def test_a_call_written_into_content_is_recovered(self) -> None:
        reply = coerce_tool_reply(
            self.LIVE_REPLY["message"]["content"], [], self._tools()
        )
        self.assertEqual(len(reply.tool_calls), 1)
        self.assertEqual(reply.tool_calls[0].name, "read_file")
        self.assertEqual(reply.tool_calls[0].arguments, {"path": "app.py"})
        # And it is NOT prose — the JSON must not also be shown to the user.
        self.assertEqual(reply.text, "")

    def test_a_fenced_call_is_recovered_too(self) -> None:
        reply = coerce_tool_reply(
            '```json\n{"name": "search_code", "arguments": {"query": "def parse"}}\n```',
            [], self._tools(),
        )
        self.assertEqual(reply.tool_calls[0].name, "search_code")

    def test_an_openai_shaped_call_written_into_content_is_recovered(self) -> None:
        reply = coerce_tool_reply(
            '{"function": {"name": "read_file", "arguments": {"path": "b.py"}}}',
            [], self._tools(),
        )
        self.assertEqual(reply.tool_calls[0].name, "read_file")
        self.assertEqual(reply.tool_calls[0].arguments, {"path": "b.py"})

    def test_a_json_reply_that_is_not_a_call_stays_prose(self) -> None:
        # The guard. A real answer can be JSON too, and inventing a call from
        # it would be worse than showing it: the model would be shown words
        # nobody said and asked to explain them.
        for body in (
            '{"summary": "the parser splits on commas"}',
            '{"name": "rm_rf_slash", "arguments": {"/": true}}',
            'just a normal sentence',
            '{"broken": ',
        ):
            reply = coerce_tool_reply(body, [], self._tools())
            self.assertEqual(reply.tool_calls, [], f"{body!r} was read as a call")
            self.assertEqual(reply.text, body)

    def test_a_structured_call_is_never_second_guessed(self) -> None:
        # When the provider did send tool_calls, `content` is whatever prose
        # came with them and must not be re-read as a call.
        calls = [ToolCall("x", "read_file", {"path": "real.py"})]
        reply = coerce_tool_reply("looking now", calls, self._tools())
        self.assertEqual(reply.tool_calls, calls)
        self.assertEqual(reply.text, "looking now")


class TestACallWrittenInsideProse(ConductorTestCase):
    """The third shape a model reaches for, found by a live run.

    The two already covered are a JSON document as the whole body, and a fence as
    the whole body. This is the one neither caught: prose that narrates the move
    and then writes the call underneath it in a fenced block. Against a 7B the
    conductor answered with the narration, the fence, and no tool call at all —
    so the move it had described was never made, and the description was shown to
    the user as the turn's answer.
    """

    LIVE = (
        "Based on the evidence, the workspace is empty, and there is no existing "
        "code to reference or modify. Let's proceed with the plan.\n\n"
        "2. **`design`** — Lock the direction for the new file and function.\n\n"
        "```json\n"
        '{"name": "design", "arguments": {"task": "Create utils.py with an '
        'add(a, b) function."}}\n'
        "```\n"
    )

    def test_it_is_recovered_rather_than_shown_to_the_user(self) -> None:
        reply = coerce_tool_reply(self.LIVE, [], list(TOOLS))
        self.assertEqual([c.name for c in reply.tool_calls], ["design"])
        self.assertEqual(
            reply.tool_calls[0].arguments["task"],
            "Create utils.py with an add(a, b) function.",
        )

    def test_the_narration_is_not_returned_as_the_answer(self) -> None:
        # A reply that both calls a tool and is shown as the final answer would
        # put the model's intentions in the transcript as if they were results.
        reply = coerce_tool_reply(self.LIVE, [], list(TOOLS))
        self.assertEqual(reply.text, "")
        self.assertTrue(reply.wants_tools)

    def test_a_last_fence_wins_over_an_earlier_one(self) -> None:
        # A model laying out two options settled on the second one, which is the
        # move it means to make.
        text = (
            "```json\n{\"name\": \"recon\", \"arguments\": {}}\n```\n"
            "Actually, on reflection:\n"
            "```json\n{\"name\": \"plan\", \"arguments\": {}}\n```\n"
        )
        reply = coerce_tool_reply(text, [], list(TOOLS))
        self.assertEqual([c.name for c in reply.tool_calls], ["plan"])

    def test_a_fence_naming_a_tool_that_was_not_offered_stays_prose(self) -> None:
        # The guard that makes the wider net safe. A model describing a call it
        # cannot make is still just talking.
        text = 'You could write\n\n```json\n{"name": "exfiltrate"}\n```\n'
        reply = coerce_tool_reply(text, [], list(TOOLS))
        self.assertEqual(reply.tool_calls, [])
        self.assertEqual(reply.text, text)

    def test_a_fence_that_is_not_json_is_left_alone(self) -> None:
        text = "Here is the function:\n\n```python\ndef add(a, b):\n    return a + b\n```\n"
        reply = coerce_tool_reply(text, [], list(TOOLS))
        self.assertEqual(reply.tool_calls, [])
        self.assertEqual(reply.text, text)


class TestTheConductorFallsBack(unittest.IsolatedAsyncioTestCase):
    """A conductor whose model is down should still answer.

    Every role has had a fallback target since before the conductor existed, and
    the conductor is the one component that had none: it is a loop with no
    single call to retry, so a dead provider ended a turn that had already made
    three moves. The fix is to retry the *call* on the fallback, which is only
    safe if the moves already made are carried into it rather than repeated.
    """

    def _loop(
        self, primary: _DyingProvider, fallback: _DyingProvider | None = None,
        on_fallback: Callable[[ProviderError, Any, str], None] | None = None,
    ) -> Conductor:
        reads: list[dict[str, Any]] = []

        async def read_file(args: dict[str, Any]) -> str:
            reads.append(args)
            return "def parse(text):\n    return text"

        return Conductor(
            primary, "primary-model", ".",
            [t for t in TOOLS if t.name == "read_file"],
            {"read_file": read_file},
            system_prompt="s",
            fallback=(fallback, "fallback-model") if fallback else None,
            on_fallback=on_fallback,
        )

    async def test_a_dead_primary_resumes_on_the_fallback(self) -> None:
        # Dies on the *second* call, so the first move really happened and its
        # result really has to survive the switch.
        primary = _DyingProvider(
            fail_on={2: "provider_unreachable"},
            replies=[
                ToolReply(tool_calls=[_call("read_file", path="app.py")]),
                ToolReply(text="never reached"),
            ],
        )
        fallback = _DyingProvider(replies=[ToolReply(text="answered by the fallback")])

        answer = await self._loop(primary, fallback).run("read it")

        self.assertEqual(answer, "answered by the fallback")
        self.assertEqual(primary.attempts, ["primary-model", "primary-model"])

    async def test_the_fallback_call_carries_the_moves_already_made(self) -> None:
        # The distinction the whole design rests on: a retry that restarted the
        # run would make `write` and `summarize` happen twice, and those have
        # effects outside the transcript.
        primary = _DyingProvider(
            fail_on={2: "provider_unreachable"},
            replies=[ToolReply(tool_calls=[_call("read_file", path="app.py")])],
        )
        fallback = _DyingProvider(replies=[ToolReply(text="done")])

        conductor = self._loop(primary, fallback)
        await conductor.run("read it")

        self.assertEqual(len(fallback.seen_messages), 1)
        results = [m for m in fallback.seen_messages[0] if m.get("role") == "tool"]
        self.assertEqual(len(results), 1, "the move the primary made must reach the fallback")
        self.assertIn("return text", results[0]["content"])
        self.assertEqual(fallback.attempts, ["fallback-model"])
        self.assertEqual(conductor.moves_made, 0, "read_file is not a stage move")

    async def test_only_a_provider_fault_moves_the_loop(self) -> None:
        # `internal_error` means our own code is wrong. Retrying it on a second
        # model is how a real defect gets buried under a lucky retry.
        primary = _DyingProvider(fail_on={1: "internal_error"})
        fallback = _DyingProvider(replies=[ToolReply(text="should not be reached")])

        with self.assertRaises(ProviderError):
            await self._loop(primary, fallback).run("go")
        self.assertEqual(fallback.attempts, [], "the fallback must not run our bug")

    async def test_the_fallback_is_tried_once_and_then_the_error_surfaces(self) -> None:
        # A loop that could hop between two providers forever is a coin toss
        # dressed as a recovery, and an unanswered turn is a real failure.
        primary = _DyingProvider(fail_on={1: "provider_http"})
        fallback = _DyingProvider(fail_on={1: "provider_http"})

        with self.assertRaises(ProviderError):
            await self._loop(primary, fallback).run("go")
        self.assertEqual(fallback.attempts, ["fallback-model"])
        self.assertEqual(len(fallback.attempts), 1)

    async def test_the_switch_is_announced_rather_than_silent(self) -> None:
        # A silent switch credits the turn's answer to a model that never
        # produced it, which is the one thing the usage books must not do.
        seen: list[tuple[str, str, str]] = []
        primary = _DyingProvider(fail_on={1: "provider_unreachable"})
        fallback = _DyingProvider(replies=[ToolReply(text="from the fallback")])

        await self._loop(
            primary, fallback,
            on_fallback=lambda exc, provider, model: seen.append((exc.code, str(provider), model)),
        ).run("go")

        self.assertEqual(seen, [("provider_unreachable", str(fallback), "fallback-model")])

    async def test_a_loop_with_no_fallback_still_raises(self) -> None:
        # The default install has no conductor fallback, and must fail the way
        # it always did rather than silently swallowing the error.
        primary = _DyingProvider(fail_on={1: "provider_unreachable"})
        with self.assertRaises(ProviderError):
            await self._loop(primary).run("go")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
