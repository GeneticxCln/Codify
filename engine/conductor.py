"""The conductor: one model that decides which of the other components to use.

Until this existed the pipeline was unconditional. Every message became a
`POST /goals`, so typing "hi" started a librarian, a designer, a planner and a
fixer and produced a plan for a greeting — and the gate that had correctly
classified the request as a question (`LayaDecision.intent`, computed on every
goal since `engine/laya.py` existed) had its answer turned into a warning
string and discarded.

This module is the other half of that fix. The gate routes; the conductor
*dispatches*. It is a loop, not a role — docs/00 §6.1 fixes `AgentRole` at
exactly eight and this is a ninth thing that holds a model, so it is not an
`AgentConfig` and not in `ROLES`. It is configured through `engine_settings`
instead (see `SettingsService.STRING_SPEC`), and it is measured through the
ordinary `agent_assigned` / `usage` events so the stats and the Settings
screen need no new case.

## The rule this file is built around

**Judgement is the model's; authority is the engine's.** Every tool below is a
call the pipeline already makes, through the same service object, with the same
validation:

| Tool | Routed through | The invariant it inherits |
|---|---|---|
| `read_file` | `LibraryService.read` | paths are workspace-relative; an escape raises |
| `search_code` | `LibraryService.search` | same |
| `git_history` | `GitService` | read-only argv, under the spawn guard |
| `run_command` | `SandboxService.run_command` | docs/00 §6.6 — the allowlist, verbatim |
| `delegate` | `ExecutorService.run_planning` | the whole 8-role pipeline, unchanged |

So the conductor gains *choice* over which powers to use, never *new* powers.
There is deliberately no write tool: editing a file is the fixer's job and is
reached only through `delegate`, which means a code change still goes through
the librarian, the planner, the fixer, the verifier and the critic exactly as
it did before this file existed. A tool that wrote files directly would make
every one of those stages optional, which is the opposite of what a conductor
is for.

## Failure is a sentence, not a crash

A tool the model miscalled, an argument of the wrong shape, a tool that raised —
each returns text the model can read and recover from, because a loop that dies
on a malformed call is a loop that stops the first time a model is slightly
wrong. The exceptions are the two that must *not* be recoverable: `ApiError` and
`CommandNotAllowed` are the engine refusing, and the model cannot retry its way
past a refusal — it is told so and the refusal stands.
"""

from __future__ import annotations

import json
from typing import Any
from collections.abc import Awaitable, Callable

from engine.sandbox import CommandNotAllowed
from engine.services import ApiError
from engine.toolcall import ToolReply, ToolSpec, coerce_arguments

# How much of a tool result the model is shown. A whole file read into a
# conversation is fine once; pasted back four times it is most of the context
# and none of the answer. The same class of bound as MAX_LIBRARY_ROUNDS and
# MAX_PLANNER_CONSULTS, for the same reason.
MAX_TOOL_RESULT_CHARS = 12000

# The cap is on *model calls*, not tool calls, because a model call is what
# costs money and time. Eight is enough for "look at this, look at that, now
# answer" and low enough that a model which has lost the thread cannot spin a
# key for an hour. Configurable via `conductor_max_turns`.
DEFAULT_MAX_TURNS = 8

# Refusals the model is shown but cannot argue with. Everything else is
# recoverable and comes back as text.


def _clip(value: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(value) <= limit:
        return value
    head = limit * 2 // 3
    return f"{value[:head]}\n…[{len(value) - head} chars elided by the engine]…"


def _result(payload: Any) -> str:
    """A tool result as the model reads it: JSON, always, and always bounded.

    JSON rather than prose because a model parses it more reliably than it
    follows prose, and bounded because the failure mode of an unbounded result
    is a turn that runs out of context and answers about the truncation.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return _clip(text)


class Conductor:
    """The loop. One instance per turn; it holds no state between turns."""

    def __init__(
        self,
        provider: Any,
        model: str,
        workspace_root: str,
        tools: list[ToolSpec],
        dispatch: dict[str, Callable[[dict[str, Any]], Awaitable[str]]],
        *,
        system_prompt: str,
        max_turns: int = DEFAULT_MAX_TURNS,
        on_text: Callable[[str], None] | None = None,
        on_tool: Callable[[str, str], None] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.workspace_root = workspace_root
        self.tools = tools
        self.dispatch = dispatch
        self.system_prompt = system_prompt
        self.max_turns = max(1, int(max_turns))
        # Progress callbacks, both optional. `on_text` streams the prose as it
        # arrives so the transcript fills in rather than appearing at the end;
        # `on_tool` is how the UI shows *which* sub-agent the conductor reached
        # for, which is the difference between a dispatcher and a black box.
        self.on_text = on_text
        self.on_tool = on_tool
        self.calls_made = 0
        self.tools_used: list[str] = []

    @property
    def exhausted(self) -> bool:
        """True when the loop stopped because it hit the cap, not because the
        model finished. The caller says so in the answer, because a turn that
        was cut off and one that completed read identically otherwise."""
        return self.calls_made >= self.max_turns

    async def run(self, user_prompt: str, history: list[dict[str, str]] | None = None) -> str:
        """Run the loop to a final answer, or to the cap. Always returns text.

        Never raises for anything a model did: a tool that raised, an argument
        of the wrong shape and a reply with neither text nor calls all come back
        as something the model is shown, because a conductor that dies on a bad
        call is a conductor that fails exactly when the model is least sure of
        itself. A provider-level failure *does* propagate, because there is no
        answer to give without one.
        """
        messages: list[dict[str, Any]] = []
        for turn in history or []:
            if turn.get("prompt"):
                messages.append({"role": "user", "content": turn["prompt"]})
            if turn.get("reply"):
                messages.append({"role": "assistant", "content": turn["reply"]})
        messages.append({"role": "user", "content": user_prompt})

        while True:
            # `exhausted` is read *before* the call, and the answer below is
            # returned whether or not the model cooperates. An earlier version
            # appended a "you are out of calls" nudge and then went on to honour
            # whatever the model asked for anyway, so a model that kept asking
            # kept the loop running forever — the cap was advice, not a bound.
            exhausted = self.calls_made >= self.max_turns
            if exhausted:
                messages.append({
                    "role": "user",
                    "content": (
                        "You have used every call you were given. Answer now with "
                        "what you have already found, and say plainly what you did "
                        "not get to. Do not ask for another tool."
                    ),
                })
            self.calls_made += 1
            reply: ToolReply = await self.provider.complete_with_tools(
                self.system_prompt, messages, self.tools, self.model,
                temperature=0.2, max_tokens=2048,
            )

            if reply.text and self.on_text is not None:
                self.on_text(reply.text)
            messages.append({
                "role": "assistant", "content": reply.text, "tool_calls": reply.tool_calls,
            })

            if not reply.wants_tools:
                return reply.text.strip()

            if exhausted:
                # Out of budget and it still wants tools. Its calls are dropped
                # and its text is the answer — with the limit stated, because a
                # reply that trails off mid-thought reads as a bug rather than
                # as the bound it is.
                said = reply.text.strip()
                tail = (
                    "\n\n(I stopped there: that was the last call I had. "
                    "Ask me to continue and I will pick it up.)"
                )
                return (said + tail) if said else (
                    "I ran out of calls before I could answer. "
                    "Ask me to continue and I will pick it up."
                )

            for call in reply.tool_calls:
                content = await self._run_tool(call, messages)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": content,
                })

    async def _run_tool(self, call: Any, messages: list[dict[str, Any]]) -> str:
        """One tool call, and the text handed back for it.

        The unknown-name branch is the one that matters most. A model will
        invent a tool name — it is what models do — and an engine that raised
        `KeyError` there would turn a recoverable slip into a dead turn. The
        model is told what it *can* call and gets to try again.
        """
        handler = self.dispatch.get(call.name)
        if handler is None:
            # The menu the model was *given*, not the dispatch table. They are
            # the same set in a correct build, and a refusal that listed the
            # dispatch table instead would tell the model to call a tool it was
            # never offered while hiding the five it was — so a tool that is
            # offered but not wired is named as such rather than silently
            # missing from the list.
            offered = [t.name for t in self.tools] or sorted(self.dispatch)
            unwired = [n for n in offered if n not in self.dispatch]
            available = ", ".join(offered) or "(none)"
            note = (
                f" (currently unavailable: {', '.join(unwired)})" if unwired else ""
            )
            return (
                f"There is no tool called {call.name!r}. The tools are: {available}{note}. "
                "Call one of those, or answer without a tool."
            )
        if self.on_tool is not None:
            try:
                self.on_tool(call.name, json.dumps(call.arguments, default=str))
            except Exception:
                pass
        spec = next((t for t in self.tools if t.name == call.name), None)
        try:
            return _result(await handler(coerce_arguments(spec, call.arguments)))
        except (ApiError, CommandNotAllowed) as exc:
            # The engine refused. Recoverable-looking text would teach the model
            # to keep asking, so the refusal says it is final.
            return f"Refused, and retrying will not help: {exc}"
        except Exception as exc:  # noqa: BLE001 — a model's bad call is not our bug
            return f"That call failed: {type(exc).__name__}: {exc}"


# ── the tool menu ───────────────────────────────────────────────────────────
#
# The descriptions are the only thing telling the model when to reach for a
# tool, so they say what the tool is *for* rather than restating its name, and
# each one names the mistake it exists to prevent. A tool called
# `read_file(path)` with a one-line description gets guessed at; the same tool
# described as "read a file you have not been shown" gets used.

READ_FILE = ToolSpec(
    name="read_file",
    description=(
        "Read one file from the workspace, as text. Use it whenever an answer "
        "depends on what a file actually contains — never guess a file's "
        "contents. Paths are relative to the workspace root. For a large file "
        "pass offset and limit to read a line range."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "workspace-relative path"},
            "offset": {"type": "integer", "description": "first line, 1-based"},
            "limit": {"type": "integer", "description": "how many lines"},
        },
        "required": ["path"],
    },
)

SEARCH_CODE = ToolSpec(
    name="search_code",
    description=(
        "Search the workspace for a string or a pattern. Use it to find where "
        "something is defined or referenced before reading a file. Set regex "
        "for a pattern, glob to restrict to a file type."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "the text or pattern to find"},
            "regex": {"type": "boolean", "description": "treat query as a regex"},
            "glob": {"type": "string", "description": "restrict to e.g. *.py"},
        },
        "required": ["query"],
    },
)

GIT_HISTORY = ToolSpec(
    name="git_history",
    description=(
        "Read this repository's history — for example recent commits, who "
        "touched a file, or what a commit changed. Read-only. It cannot commit, "
        "revert or modify anything."
    ),
    parameters={
        "type": "object",
        "properties": {
            "args": {
                "type": "array",
                "description": "argv after 'git', e.g. [\"log\", \"-5\", \"--oneline\"]",
            },
        },
        "required": ["args"],
    },
)

RUN_COMMAND = ToolSpec(
    name="run_command",
    description=(
        "Run one of the project's own commands — its tests, its type checker, "
        "its build. This is how you find out whether something is actually "
        "true. It cannot start a shell, install anything or reach the network, "
        "and an unlisted command is refused. Always give the reason you want it "
        "run, so the reason is recorded with the run."
    ),
    parameters={
        "type": "object",
        "properties": {
            "argv": {
                "type": "array",
                "description": "the command, e.g. [\"pytest\", \"-q\"]",
            },
            "reason": {"type": "string", "description": "what this run is meant to show"},
        },
        "required": ["argv"],
    },
)

DELEGATE = ToolSpec(
    name="delegate",
    description=(
        "Hand a change to Codify's full pipeline: it researches the workspace, "
        "plans the steps, writes the code, runs the tests and reviews its own "
        "work. Use it when the user wants the workspace *changed*. Do not use it "
        "to answer a question, and do not try to edit files yourself — you have "
        "no way to. It can take several minutes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "the change to make, in full sentences",
            },
        },
        "required": ["task"],
    },
)

TOOLS: tuple[ToolSpec, ...] = (READ_FILE, SEARCH_CODE, GIT_HISTORY, RUN_COMMAND, DELEGATE)


def tool_names() -> list[str]:
    """The menu, as names. Exported so the refusal branch and the tests agree
    with the dispatch table without repeating it."""
    return [t.name for t in TOOLS]
