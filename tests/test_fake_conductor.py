"""A scripted conductor on the far end of a real socket, and the whole path through it.

`scripts/fake_ollama.py` answered the conductor's `/api/chat` with prose only, so nothing could drive Start ->
conductor -> steps without a real model: every conductor test used an in-process double that returns
`ToolReply` objects, which proves the loop and says nothing about the wire. With `FAKE_CONDUCTOR=1` the fake
now plays a conductor that always does the same sensible thing, as Ollama would send it (`message.tool_calls`
with an *object* for `arguments`), so the real provider, the real parser, the real executor, the real driver
and a real git repository all run.

The script is a pure function of the messages it is sent. The model on the other end has no memory, so the
conductor is reconstructed from the transcript each time: which calls it already made decide the next. The
*request text* picks a scenario, so one server serves them all:

* a question gets prose and no tool;
* `[ask]` makes it put one question to the person first;
* `[todo]` makes a step run keep a note before writing;
* `[stall]` makes a step run stop after `write`, which is the conductor that cannot finish.

The first block holds the policy; the second runs it live.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import types
import unittest
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import _run_steps_locked
from engine.db import connect
from engine.executor import ExecutorService
from engine.laya import LayaService
from engine.models import ROLES, AgentConfigUpdate, ConversationCreate, TurnCreate, WorkspaceCreate
from engine.providers import Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import (
    AgentRegistryService,
    ConversationService,
    GoalService,
    SettingsService,
    WorkspaceService,
)
from tests.test_fake_ollama import load_fake

TURN = "The user says: {request}"
STEP = (
    "The user has approved this plan and started it: {title}\n\nThe plan:\n1. [PENDING] Add banner   <- this step\n\n"
    "You are working on one step only: step {step_id}, 'Add banner'.\ncreate banner.txt\nSuggested paths: banner.txt"
)
ALL_TOOLS = ["read_file", "recon", "plan", "ask_user"]
STEP_TOOLS = ["read_file", "write", "verify", "review", "summarize", "todo"]


def _tools(*names: str) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": n}} for n in names]


def _called(name: str, **arguments: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def _result(text: str = "ok") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": "c", "content": text}


class TestThePolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = load_fake()
        patcher = mock.patch.dict(os.environ, {"FAKE_CONDUCTOR": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def step(self, user: str, *history: dict[str, Any], tools: list[str] | None = None) -> Any:
        payload = {
            "model": "m",
            "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": user}, *history],
            "tools": _tools(*(tools or ALL_TOOLS)),
        }
        return self.fake.conductor_move(payload)

    def test_without_the_switch_the_fake_stays_prose_only(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_CONDUCTOR": "0"}):
            reply = self.fake.chat_reply({
                "model": "m", "tools": _tools(*ALL_TOOLS),
                "messages": [{"role": "user", "content": TURN.format(request="add a banner file")}],
            })
        self.assertNotIn("tool_calls", reply["message"])
        self.assertIn("Fake Ollama here", reply["message"]["content"])

    def test_a_change_request_is_recon_then_plan_then_a_sentence_that_hands_over(self) -> None:
        user = TURN.format(request="add a banner file")
        self.assertEqual(("recon", {"task": "add a banner file"}), self.step(user))
        self.assertEqual(("plan", {"task": "add a banner file"}), self.step(user, _called("recon"), _result()))
        last = self.step(user, _called("recon"), _result(), _called("plan"), _result())
        self.assertIsInstance(last, str)
        self.assertIn("approve", last.lower())

    def test_a_question_gets_prose_and_no_tool(self) -> None:
        self.assertIsInstance(self.step(TURN.format(request="what does greet do?")), str)

    def test_ask_marks_a_request_that_puts_one_question_first(self) -> None:
        user = TURN.format(request="add a banner [ask]")
        name, arguments = self.step(user)
        self.assertEqual("ask_user", name)
        self.assertEqual(["banner.txt", "README.md"], arguments["options"])

    def test_it_does_not_ask_when_asking_is_not_offered(self) -> None:
        user = TURN.format(request="add a banner [ask]")
        self.assertEqual("recon", self.step(user, tools=["read_file", "recon", "plan"])[0])

    def test_a_step_run_is_write_verify_review_summarize_and_a_sentence(self) -> None:
        user = STEP.format(title="add a banner", step_id="s-1")
        history: list[dict[str, Any]] = []
        seen: list[Any] = []
        for _ in range(6):
            move = self.step(user, *history, tools=STEP_TOOLS)
            seen.append(move)
            if isinstance(move, str):
                break
            history += [_called(move[0]), _result()]
        self.assertEqual(["write", "verify", "review", "summarize"], [m[0] for m in seen[:-1]])
        for name, arguments in seen[:-1]:
            self.assertEqual("s-1", arguments["step_id"], name)
        self.assertIsInstance(seen[-1], str)

    def test_todo_marks_a_step_run_that_keeps_a_note_before_writing(self) -> None:
        user = STEP.format(title="add a banner [todo]", step_id="s-1")
        name, arguments = self.step(user, tools=STEP_TOOLS)
        self.assertEqual("todo", name)
        self.assertEqual("add", arguments["action"])
        self.assertEqual("write", self.step(user, _called("todo"), _result(), tools=STEP_TOOLS)[0])

    def test_stall_marks_a_step_run_that_stops_after_the_write(self) -> None:
        user = STEP.format(title="add a banner [stall]", step_id="s-1")
        self.assertEqual("write", self.step(user, tools=STEP_TOOLS)[0])
        self.assertIsInstance(self.step(user, _called("write"), _result(), tools=STEP_TOOLS), str)

    def test_it_never_calls_a_tool_it_was_not_offered(self) -> None:
        user = STEP.format(title="add a banner", step_id="s-1")
        for offered in (["read_file"], ["read_file", "write"], []):
            move = self.step(user, tools=offered)
            if isinstance(move, tuple):
                self.assertIn(move[0], offered)

    def test_the_history_of_earlier_turns_does_not_replay_their_calls(self) -> None:
        # The prompt of a later turn carries the thread's history, which says "The user says:" more than once.
        # The *last* request is the one being answered.
        user = "Earlier in this conversation:\nUser: add a banner [ask]\nAssistant: Which file?\n\n" + TURN.format(request="banner.txt")
        self.assertEqual("recon", self.step(user)[0])


# ── live: a real socket, the real provider, the real executor and a real repository ──────────────────────────


class LiveCase(unittest.IsolatedAsyncioTestCase):
    """The fake served on a port, the engine pointed at it, a git repository as the workspace."""

    async def asyncSetUp(self) -> None:
        self.fake = load_fake()
        env = mock.patch.dict(os.environ, {"FAKE_CONDUCTOR": "1"})
        env.start()
        self.addCleanup(env.stop)

        self.requests: list[tuple[str, dict[str, Any]]] = []
        outer = self

        class Recording(self.fake.Handler):  # type: ignore[misc, name-defined]
            def do_POST(self) -> None:  # noqa: N802
                # `Any`: the base class is the imported script's, which mypy cannot see into.
                handler: Any = self
                length = int(handler.headers.get("Content-Length", 0))
                body = handler.rfile.read(length) or b"{}"
                outer.requests.append((handler.path, json.loads(body)))
                real, handler.rfile = handler.rfile, _Replay(body)  # the real handler reads it again
                try:
                    super().do_POST()
                finally:
                    handler.rfile = real

        self.server = HTTPServer(("127.0.0.1", 0), Recording)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "greeter.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n', encoding="utf-8")
        for args in (["init", "-q"], ["add", "greeter.py"], ["commit", "-qm", "first"]):
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=self.repo, check=True,
                capture_output=True,
            )
        self.conn = connect(root / "live.db")
        self.addCleanup(self.conn.close)
        keychain = Keychain(secrets_path=root / "secrets.json")
        registry = AgentRegistryService(self.conn, ProviderFactory(keychain), keychain)
        for role in ROLES:
            registry.set_config(role, AgentConfigUpdate(provider="ollama", model_name="qwen2.5-coder:7b", base_url=self.url))
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        conversations = ConversationService(self.conn)
        executor = ExecutorService(
            self.goals, self.workspaces, registry, SandboxService(), laya=LayaService(registry=registry),
        )
        executor.settings = SettingsService(self.conn)
        self.executor = executor
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(executor=executor, goals=self.goals))
        ws = self.workspaces.create(WorkspaceCreate(name="demo", root_path=str(self.repo)))
        self.thread = conversations.create(ConversationCreate(workspace_id=ws.id, title="live"))

    async def turn(self, prompt: str) -> str:
        goal = self.goals.create_turn(self.thread.id, TurnCreate(prompt=prompt))
        await self.executor.run_chat(goal.id)
        return goal.id

    async def start(self, goal_id: str) -> None:
        current = self.goals.get(goal_id)
        self.goals.update_status(goal_id, current.version, "RUNNING")
        await _run_steps_locked(self.app, goal_id)  # type: ignore[arg-type]

    def events(self, goal_id: str, kind: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.goals.events_after(goal_id, 0) if e.type == kind]

    def conductor_calls(self, goal_id: str) -> list[str]:
        prefix = "conductor called "
        return [
            str(p["message"])[len(prefix):].split("(", 1)[0]
            for p in self.events(goal_id, "log") if str(p.get("message", "")).startswith(prefix)
        ]

    def commits(self) -> list[str]:
        out = subprocess.run(["git", "log", "--format=%s"], cwd=self.repo, capture_output=True, text=True, check=True)
        return out.stdout.split("\n")[:-1]

    def fixer_requests(self) -> int:
        return sum(1 for path, body in self.requests if path == "/api/generate" and "Suggested paths" in str(body.get("prompt")))


class _Replay:
    """The request body again, for the handler that was about to read it."""

    def __init__(self, body: bytes) -> None:
        self._buffer = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._buffer.read(n)


class TestAChangeEndToEnd(LiveCase):
    async def test_the_turn_plans_and_hands_over_without_touching_a_file(self) -> None:
        goal_id = await self.turn("add a banner file")

        self.assertEqual("PENDING", self.goals.get(goal_id).status)
        self.assertEqual(["Add banner"], [s.title for s in self.goals.steps(goal_id)])
        self.assertEqual(["recon", "plan"], self.conductor_calls(goal_id))
        self.assertFalse((self.repo / "banner.txt").exists())
        self.assertTrue(any(p.get("turn") for p in self.events(goal_id, "log")), "the turn said something")

    async def test_start_drives_the_step_to_a_commit_and_completes_the_goal(self) -> None:
        goal_id = await self.turn("add a banner file")
        before = self.fixer_requests()

        await self.start(goal_id)

        self.assertEqual("COMPLETED", self.goals.get(goal_id).status, self.events(goal_id, "error"))
        self.assertEqual(["COMPLETED"], [s.status for s in self.goals.steps(goal_id)])
        self.assertEqual("hello from codify\n", (self.repo / "banner.txt").read_text(encoding="utf-8"))
        self.assertEqual("feat: add banner.txt", self.commits()[0])
        self.assertEqual(["recon", "plan", "write", "verify", "review", "summarize"], self.conductor_calls(goal_id))
        self.assertEqual(1, self.fixer_requests() - before, "the fixer ran more than once for one step")

    async def test_every_conductor_call_went_over_the_wire_as_ollama_sends_it(self) -> None:
        goal_id = await self.turn("add a banner file")
        await self.start(goal_id)

        chats = [body for path, body in self.requests if path == "/api/chat"]
        self.assertGreaterEqual(len(chats), 6)
        for body in chats:
            self.assertFalse(body["stream"])
            self.assertTrue(body["tools"], "a conductor call carried no menu")
        later = next(b for b in chats if any(m.get("role") == "tool" for m in b["messages"]))
        for message in later["messages"]:
            for call in message.get("tool_calls") or []:
                self.assertIsInstance(call["function"]["arguments"], dict, "Ollama is sent an object, not a string")

    async def test_the_conductors_calls_were_booked_as_the_conductor(self) -> None:
        goal_id = await self.turn("add a banner file")
        await self.start(goal_id)

        roles = {p.get("role") for p in self.events(goal_id, "usage")}
        self.assertIn("conductor", roles)


class TestAConductorThatCannotFinish(LiveCase):
    async def test_a_step_it_stops_after_writing_pauses_the_goal_and_says_why(self) -> None:
        goal_id = await self.turn("add a banner file [stall]")
        await self.start(goal_id)

        self.assertEqual("PAUSED", self.goals.get(goal_id).status)
        pause = [p for p in self.events(goal_id, "goal_status") if p["status"] == "PAUSED"][-1]
        self.assertEqual("conductor_stopped", pause["reason_code"])
        self.assertTrue(pause["reason"])
        self.assertNotEqual("COMPLETED", self.goals.steps(goal_id)[0].status)
        self.assertEqual("hello from codify\n", (self.repo / "banner.txt").read_text(encoding="utf-8"), "it did write")
        self.assertEqual(["first"], self.commits(), "nothing was committed: the conductor never summarized")


class TestAskingThroughTheWire(LiveCase):
    async def test_the_question_ends_the_turn_and_the_next_turn_answers_it(self) -> None:
        asked = await self.turn("add a banner [ask]")

        self.assertEqual("COMPLETED", self.goals.get(asked).status)
        self.assertEqual([], self.goals.steps(asked))
        reply = next(p for p in self.events(asked, "log") if p.get("turn"))
        self.assertEqual(["banner.txt", "README.md"], reply["question"]["options"])
        self.assertEqual(["ask_user"], self.conductor_calls(asked))

        answered = await self.turn("banner.txt")

        self.assertEqual("PENDING", self.goals.get(answered).status)
        self.assertEqual(["Add banner"], [s.title for s in self.goals.steps(answered)])


class TestTheNoteSurvivesToTheStepRun(LiveCase):
    async def test_a_step_run_that_keeps_a_note_publishes_it_and_still_finishes(self) -> None:
        goal_id = await self.turn("add a banner file [todo]")
        await self.start(goal_id)

        notes = self.events(goal_id, "todo_updated")
        self.assertEqual(1, len(notes))
        self.assertEqual("pending", notes[-1]["items"][0]["status"])
        self.assertEqual("COMPLETED", self.goals.get(goal_id).status)
        self.assertEqual(["recon", "plan", "todo", "write", "verify", "review", "summarize"], self.conductor_calls(goal_id))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
