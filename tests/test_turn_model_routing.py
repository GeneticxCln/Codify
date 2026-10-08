"""The model picked in the command bar routes the turn, and writes nothing.

The bar's picker used to be a decoration: the engine recorded the choice on the goal ("what the user asked
for") and ran the turn on the conductor model the settings named, or on the scribe's row. A person could pick
`qwen3:8b`, see it on screen, and be answered by something else. Now the pick is the conductor's *primary*
for that turn: its loop, its plain reply, and the run that resumes after a plan is approved.

What must stay true, and is held here:

- **It routes.** The model the provider receives is the pick, on the conductor path and on the plain-reply
  path, and the `agent_assigned` event says so (the usage books and the transcript read it).
- **It writes nothing** (docs/00 section 6.2 is about *writing* configuration). No `agent_configs` row, no
  `engine_settings` row changes, and the scribe's row still says what it said.
- **It reaches only the turn's own calls.** The eight roles a move calls run on their own configuration; a
  turn that goes on to the pipeline plans on the planner's model, not the pick.
- **Only turns.** A goal made by `POST /goals` records the same two columns and keeps running on the settings.
- **The settings still decide the fallback**, and a pick that cannot be reached is *said*, not ignored.
- **The door is bounded**: both halves or neither, and no longer than the settings would accept.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import unittest
from typing import Any

import httpx
from httpx import ASGITransport

from engine.app import BOOT_TOKEN, app
from engine.models import AgentConfigUpdate, GoalCreate, TurnCreate
from engine.providers import BaseProvider
from engine.toolcall import ToolReply, ToolSpec
from tests.test_conductor import ConductorTestCase, _ToolProvider
from tests.test_turns import TurnTestCase, _Gate

PICK = ("openai", "picked-model")


class _Recording(_ToolProvider):
    """The conductor's scripted model, remembering which model each call was made on."""

    def __init__(self, replies: list[ToolReply] | None = None) -> None:
        super().__init__(replies)
        self.models: list[str] = []

    async def complete_with_tools(
        self, system_prompt: str, messages: list[dict[str, Any]], tools: list[ToolSpec], model: str,
        temperature: float, max_tokens: int, *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> ToolReply:
        self.models.append(model)
        return await super().complete_with_tools(
            system_prompt, messages, tools, model, temperature, max_tokens, num_ctx=num_ctx, keep_alive=keep_alive,
        )


class _Plain(BaseProvider):
    """A provider that cannot take a tool list, which is the base class's answer."""

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int,
        *, num_ctx: int | None = None, keep_alive: str | None = None,
    ) -> str:
        return "ok"


class TestThePickRoutesTheConductor(ConductorTestCase):
    def _picked_turn(self, provider: str = PICK[0], model: str = PICK[1]) -> Any:
        return self.goals.create_turn(self.thread.id, TurnCreate(prompt="hi", provider=provider, model=model))

    async def test_the_conductors_loop_runs_on_the_pick(self) -> None:
        provider = _Recording([ToolReply(text="Hello.")])
        executor = self._executor(provider)
        goal = self._picked_turn()
        await executor.run_chat(goal.id)
        self.assertEqual(provider.models, ["picked-model"])
        assigned = [
            e.payload for e in self.goals.events_after(goal.id, 0)
            if e.type == "agent_assigned" and e.payload.get("conductor")
        ]
        self.assertEqual([(a["provider"], a["model"]) for a in assigned], [PICK], "the transcript names another model")

    async def test_without_a_pick_it_runs_on_the_borrowed_row(self) -> None:
        # The control. Without it the test above could pass because nothing else was configured.
        provider = _Recording([ToolReply(text="Hello.")])
        executor = self._executor(provider)
        await executor.run_chat(self.goal.id)
        self.assertEqual(provider.models, ["stub-model"])

    async def test_the_pick_wins_over_the_conductor_setting(self) -> None:
        provider = _Recording([ToolReply(text="Hello.")])
        executor = self._executor(provider)
        executor.settings = self.settings
        self.settings.set_str("conductor_provider", "groq")
        self.settings.set_str("conductor_model", "settings-model")
        goal = self._picked_turn()
        await executor.run_chat(goal.id)
        self.assertEqual(provider.models, ["picked-model"])
        # ...and the setting still holds for the next turn that picks nothing.
        other = self.goals.create_turn(self.thread.id, TurnCreate(prompt="again"))
        provider.replies.append(ToolReply(text="Again."))
        await executor.run_chat(other.id)
        self.assertEqual(provider.models, ["picked-model", "settings-model"])

    async def test_the_pick_follows_the_goal_into_a_resume(self) -> None:
        # The pick is read from the goal row, not from the request that made it, so the run that resumes after
        # Start (which only has the goal id) is routed the same way.
        executor = self._executor(_Recording())
        goal = self._picked_turn()
        reread = self.goals.get(goal.id)
        self.assertEqual(executor._picked_pair(reread), PICK)
        target = executor._conductor_target(executor._picked_pair(reread))
        assert target is not None
        self.assertEqual(target[1], "picked-model")

    async def test_the_turn_is_the_only_goal_a_pick_routes(self) -> None:
        executor = self._executor(_Recording())
        run = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="t", provider=PICK[0], model=PICK[1]))
        self.assertEqual((run.provider, run.model), PICK, "the run did not record what was asked for")
        self.assertIsNone(executor._picked_pair(run), "a goal made by POST /goals was routed by its recorded pair")

    async def test_half_a_pair_or_a_blank_one_picks_nothing(self) -> None:
        executor = self._executor(_Recording())
        goal = self._picked_turn()
        for provider, model in ((None, "m"), ("p", None), ("", ""), ("  ", " ")):
            copy = goal.model_copy(update={"provider": provider, "model": model})
            self.assertIsNone(executor._picked_pair(copy), (provider, model))


class TestTheTargetsAPickGets(ConductorTestCase):
    def _executor_with(self, provider: BaseProvider | None = None) -> Any:
        executor = self._executor(provider or _Recording())
        executor.settings = self.settings
        return executor

    def _targets(self, executor: Any, picked: tuple[str, str] | None = PICK) -> list[tuple[str, str]]:
        found, _ = executor._resolve_conductor_targets(picked)
        return [(t[2].provider, t[1]) for t in found]

    async def test_the_scribes_fallback_still_follows_a_pick(self) -> None:
        executor = self._executor_with()
        executor.orchestrator.registry.set_config("scribe", AgentConfigUpdate(
            provider="openai", model_name="scribe-model",
            fallback_provider="ollama", fallback_model_name="scribe-backup",
        ))
        self.assertEqual(self._targets(executor), [PICK, ("ollama", "scribe-backup")])
        self.assertEqual(self._targets(executor, None), [("openai", "scribe-model"), ("ollama", "scribe-backup")])

    async def test_the_conductors_own_fallback_follows_a_pick(self) -> None:
        executor = self._executor_with()
        self.settings.set_str("conductor_provider", "groq")
        self.settings.set_str("conductor_model", "conductor-model")
        self.settings.set_str("conductor_fallback_provider", "ollama")
        self.settings.set_str("conductor_fallback_model", "conductor-backup")
        self.assertEqual(self._targets(executor), [PICK, ("ollama", "conductor-backup")])

    async def test_a_pick_takes_the_rows_window_and_not_its_endpoint_or_key(self) -> None:
        executor = self._executor_with()
        registry = executor.orchestrator.registry
        registry.set_config("scribe", AgentConfigUpdate(
            provider="ollama", model_name="local", num_ctx=12288, temperature=0.37,
            base_url="http://127.0.0.1:11434",
        ))
        self.assertEqual(registry.get_config("scribe").base_url, "http://127.0.0.1:11434", "the row has no endpoint to leak")
        found, _ = executor._resolve_conductor_targets(("openai", "gpt-x"))
        cfg = found[0][2]
        self.assertEqual((cfg.provider, cfg.protocol, cfg.model_name), ("openai", "openai_compat", "gpt-x"))
        self.assertEqual(cfg.num_ctx, 12288, "the borrowed row's window is the conductor's, whichever model it runs")
        self.assertEqual(cfg.temperature, 0.37)
        self.assertIsNone(cfg.base_url, "the local row's endpoint was carried to a different provider")
        self.assertIsNone(cfg.api_key_ref, "a credential reference belongs to the provider the row points at")
        # The same provider keeps its row's endpoint: only a move drops it.
        same, _ = executor._resolve_conductor_targets(("ollama", "other-local"))
        self.assertEqual(same[0][2].base_url, "http://127.0.0.1:11434")

    async def test_a_pick_nobody_can_reach_is_said_and_not_silently_replaced(self) -> None:
        executor = self._executor_with()
        found, problems = executor._resolve_conductor_targets(("nowhere", "m"))
        self.assertEqual(found, [], "an unreachable pick fell through to some other model")
        self.assertTrue(any("you picked" in p and "'nowhere'" in p for p in problems), problems)

    async def test_a_pick_whose_provider_cannot_call_tools_is_said(self) -> None:
        executor = self._executor(_Plain())
        found, problems = executor._resolve_conductor_targets(PICK)
        self.assertEqual(found, [])
        self.assertTrue(any("model you picked" in p and "cannot call tools" in p for p in problems), problems)
        # The same provider as a *setting* is still the documented quiet degradation, worded as before.
        _, quiet = executor._resolve_conductor_targets(None)
        self.assertEqual(quiet, [])


class TestThePickOnThePlainReply(TurnTestCase):
    def _models(self) -> list[str]:
        return [c["model"] for c in self.provider.calls]

    def _picked(self, prompt: str = "hi") -> Any:
        return self.goals.create_turn(self.thread.id, TurnCreate(prompt=prompt, provider=PICK[0], model=PICK[1]))

    async def test_the_reply_is_made_on_the_pick(self) -> None:
        goal = self._picked()
        await self.executor.run_chat(goal.id)
        self.assertEqual(self._models(), ["picked-model"])
        self.assertEqual(self._reply(goal.id), "Hello! What would you like to work on?")

    async def test_without_a_pick_it_is_made_on_the_borrowed_row(self) -> None:
        await self.executor.run_chat(self._turn().id)
        self.assertEqual(self._models(), ["stub-model"])

    async def test_a_pick_writes_no_configuration(self) -> None:
        def state() -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
            return (
                [tuple(r) for r in self.conn.execute("SELECT * FROM agent_configs ORDER BY role")],
                [tuple(r) for r in self.conn.execute("SELECT * FROM engine_settings ORDER BY 1")],
            )

        before = state()
        goal = self._picked()
        await self.executor.run_chat(goal.id)
        self.assertEqual(self._models(), ["picked-model"], "the turn did not run on the pick, so the check below proves nothing")
        self.assertEqual(before, state(), "a turn that was routed on a pick changed configuration")
        self.assertEqual(self.registry.get_config("scribe").model_name, "stub-model")

    async def test_the_roles_a_pipeline_turn_calls_keep_their_own_models(self) -> None:
        self.executor = self._executor(_Gate(intent="code_change"))
        self.executor.settings = self.settings
        app.state.executor = self.executor
        goal = self._picked("add a test for the parser")
        await self.executor.run_chat(goal.id)
        self.assertEqual(self.goals.get(goal.id).status, "PENDING")
        self.assertTrue(self.goals.steps(goal.id), "the turn did not reach the pipeline, so this proves nothing")
        self.assertTrue(self._models(), "no role called a model")
        self.assertEqual(set(self._models()), {"stub-model"}, "the pick leaked into a role's call")


class TestThePickAtTheDoor(TurnTestCase):
    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", headers={"Authorization": f"Bearer {BOOT_TOKEN}"},
        ) as client:
            return await client.post(f"/conversations/{self.thread.id}/turns", json=body)

    async def test_a_pair_is_recorded_on_the_goal_and_stripped(self) -> None:
        resp = await self._post({"prompt": "hi", "provider": " openai ", "model": " picked-model "})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual((body["provider"], body["model"]), PICK)

    async def test_neither_and_blank_are_the_same_as_no_pick(self) -> None:
        for body in ({"prompt": "hi"}, {"prompt": "hi", "provider": "", "model": "  "}):
            resp = await self._post(body)
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual((resp.json()["provider"], resp.json()["model"]), (None, None))

    async def test_half_a_pair_is_refused_because_it_names_nothing_to_call(self) -> None:
        for body in ({"prompt": "hi", "provider": "openai"}, {"prompt": "hi", "model": "m"}):
            resp = await self._post(body)
            self.assertEqual(resp.status_code, 422, f"{body}: {resp.text}")

    async def test_nothing_longer_than_the_settings_would_accept_is_taken(self) -> None:
        for body in (
            {"prompt": "hi", "provider": "p" * 65, "model": "m"},
            {"prompt": "hi", "provider": "p", "model": "m" * 129},
        ):
            resp = await self._post(body)
            self.assertEqual(resp.status_code, 422, resp.text)
        ok = await self._post({"prompt": "hi", "provider": "p" * 64, "model": "m" * 128})
        self.assertEqual(ok.status_code, 200, ok.text)


class _FakeCatalog:
    """A catalog that answers without discovering anything, so the route is tested and not the network."""

    async def get(self, refresh: bool = False) -> dict[str, Any]:
        return {"models": [], "providers": [], "fetched_at": 0.0, "cached": False}


class TestWhatATurnRunsOnWhenNothingIsPicked(TurnTestCase):
    """`GET /models` says which model answers by default, so the bar can show it instead of guessing."""

    def _summary(self) -> dict[str, Any]:
        return self.executor.conductor_summary()

    async def test_the_borrowed_row_is_the_default_and_its_window_comes_with_it(self) -> None:
        self.registry.set_config("scribe", AgentConfigUpdate(provider="ollama", model_name="local-7b", num_ctx=12288))
        self.assertEqual(
            self._summary(),
            {"provider": "ollama", "model": "local-7b", "source": "scribe", "num_ctx": 12288},
        )

    async def test_the_settings_pair_wins_and_the_window_is_still_the_rows(self) -> None:
        self.registry.set_config("scribe", AgentConfigUpdate(provider="ollama", model_name="local-7b", num_ctx=12288))
        self.settings.set_str("conductor_provider", "groq")
        self.settings.set_str("conductor_model", "big-model")
        self.assertEqual(
            self._summary(),
            {"provider": "groq", "model": "big-model", "source": "settings", "num_ctx": 12288},
        )

    async def test_nothing_configured_says_so_in_empty_strings(self) -> None:
        self.registry.set_config("scribe", AgentConfigUpdate(model_name=""))
        got = self._summary()
        self.assertEqual((got["provider"], got["model"]), ("", ""))

    async def test_it_is_names_and_a_number_and_nothing_a_secret_could_hide_in(self) -> None:
        self.registry.set_config("scribe", AgentConfigUpdate(
            provider="ollama", model_name="m", base_url="http://127.0.0.1:11434", api_key="sk-secret-value",
        ))
        got = self._summary()
        self.assertEqual(set(got), {"provider", "model", "source", "num_ctx"})
        self.assertNotIn("sk-secret-value", repr(got))
        self.assertNotIn("11434", repr(got))

    async def test_the_route_returns_it_fresh_each_time_and_does_not_cache_it(self) -> None:
        # `app.state` is the one process-wide object these tests share, so what this replaces is put back.
        missing = object()
        before = getattr(app.state, "models", missing)
        self.addCleanup(lambda: delattr(app.state, "models") if before is missing else setattr(app.state, "models", before))
        app.state.models = _FakeCatalog()
        self.registry.set_config("scribe", AgentConfigUpdate(provider="ollama", model_name="first"))
        headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", headers=headers) as client:
            first = (await client.get("/models")).json()
            self.registry.set_config("scribe", AgentConfigUpdate(model_name="second"))
            second = (await client.get("/models")).json()
        self.assertEqual(first["conductor"]["model"], "first")
        self.assertEqual(second["conductor"]["model"], "second", "the conductor block was served from a cache")
        self.assertIn("models", first, "the catalog itself is still in the answer")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
