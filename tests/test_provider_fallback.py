"""A role's fallback target: the goal keeps running when the primary cannot be used.

The restraint is the feature here as much as anywhere else. A fallback that
swallowed every failure would turn a bug in the engine into a silent detour
through a second model, and a fallback that could be tried repeatedly would turn
one outage into an unbounded bill. So the tests below are as much about what the
fallback refuses to do as about what it rescues.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import json
import tempfile
import unittest
from pathlib import Path

from engine.db import connect
from engine.executor import ExecutorService
from engine.laya import LayaDecision, LayaService
from engine.models import ROLES, AgentConfigUpdate, GoalCreate, WorkspaceCreate
from engine.providers import BaseProvider, Keychain, ProviderError, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, GoalService, WorkspaceService


class TargetProvider(BaseProvider):
    """One provider per target slug: replies by role, or fails as scripted."""

    def __init__(self, name: str, fail_with: ProviderError | None = None, reply: str | None = None):
        self.name = name
        self.fail_with = fail_with
        self.reply = reply
        self.role: str | None = None
        self.calls: list[dict] = []

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
        self.calls.append({
            "role": self.role,
            "provider": self.name,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        if self.fail_with is not None:
            raise ProviderError(self.fail_with.code, self.fail_with.message)
        if self.reply is not None:
            return self.reply
        if self.role == "planner":
            return json.dumps({"steps": [{"title": "S1", "description": "d", "suggested_paths": []}]})
        return json.dumps({})


class FallbackFactory(ProviderFactory):
    """Builds the provider for whichever target asked, and can refuse to.

    A build that raises is how a missing credential actually behaves, so the
    fallback has to be reachable *through* that failure, not around it.
    """

    def __init__(self, providers: dict[str, TargetProvider], keychain: Keychain | None = None):
        super().__init__(keychain or Keychain())
        self.providers = providers
        self.build_failures: dict[str, ProviderError] = {}
        self.built: list[tuple[str, str]] = []

    def build(self, config):
        self.built.append((config.role, config.provider))
        if config.provider in self.build_failures:
            raise self.build_failures[config.provider]
        provider = self.providers[config.provider]
        provider.role = config.role
        return provider


class SkippedGate(LayaService):
    async def decide(self, state):
        return LayaDecision(engine="skipped", skipped_reason="test double")


class FallbackTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "t.db")
        self.goals = GoalService(self.conn)
        self.workspaces = WorkspaceService(self.conn)
        self.ws = self.workspaces.create(WorkspaceCreate(name="WS", root_path=str(self.root)))
        self.primary = TargetProvider("anthropic")
        self.second = TargetProvider("ollama")
        self.factory = FallbackFactory({"anthropic": self.primary, "ollama": self.second})
        self.registry = AgentRegistryService(self.conn, self.factory, Keychain())
        self.executor = ExecutorService(
            self.goals, self.workspaces, self.registry, SandboxService(), laya=SkippedGate()
        )
        for role in ROLES:
            self.registry.set_config(role, AgentConfigUpdate(provider="anthropic", model_name="primary-model"))
        self.registry.set_config("planner", AgentConfigUpdate(temperature=0.15, max_tokens=1234))

    async def asyncTearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def _plan(self):
        goal = self.goals.create(GoalCreate(workspace_id=self.ws.id, title="T", description=""))
        return goal

    def _events(self, goal_id, type_):
        return [e.payload for e in self.goals.events_after(goal_id, 0) if e.type == type_]

    def _errors(self, goal_id):
        return self._events(goal_id, "error")

    def _give_planner_a_fallback(self, model: str = "fallback-model") -> None:
        self.registry.set_config(
            "planner", AgentConfigUpdate(fallback_provider="ollama", fallback_model_name=model)
        )


class TestItRescuesTheGoal(FallbackTestCase):
    async def test_a_primary_with_no_credential_runs_on_the_fallback(self):
        """The reported case: the key was never stored, so nothing could run.

        The role has a model and a provider; what it lacks is a credential. That is
        a setup gap the user may not want to fix on the spot, and a second target
        they configured is exactly the answer to it.
        """
        self.factory.build_failures["anthropic"] = ProviderError(
            "missing_api_key", "Anthropic API key is not set"
        )
        self._give_planner_a_fallback()

        goal = self._plan()
        await self.executor.run_planning(goal.id)

        self.assertEqual(self._errors(goal.id), [], "a configured fallback must not fail the goal")
        self.assertEqual([s.title for s in self.goals.steps(goal.id)], ["S1"])
        self.assertEqual(len(self.second.calls), 1)
        self.assertEqual(self.second.calls[0]["model"], "fallback-model")

        fallbacks = self._events(goal.id, "provider_fallback")
        self.assertEqual(len(fallbacks), 1)
        payload = fallbacks[0]
        self.assertEqual(payload["role"], "planner")
        self.assertEqual(payload["from"], {"provider": "anthropic", "model": "primary-model"})
        self.assertEqual(payload["to"], {"provider": "ollama", "model": "fallback-model"})
        self.assertEqual(payload["code"], "agent_not_configured")
        self.assertIn("Anthropic API key is not set", payload["detail"])

    async def test_a_dead_endpoint_runs_on_the_fallback(self):
        """`is down` is a transport failure, which used to escape unnamed.

        A refused connection reached the goal as `internal_error` — a code that
        blames Codify for a provider that is simply not listening — and no
        fallback could have recognised it.
        """
        self.primary.fail_with = ProviderError(
            "provider_unreachable", "openai_compat unreachable: ConnectError: refused"
        )
        self._give_planner_a_fallback()

        goal = self._plan()
        await self.executor.run_planning(goal.id)

        self.assertEqual(self._errors(goal.id), [])
        self.assertEqual(len(self.second.calls), 1)
        payload = self._events(goal.id, "provider_fallback")[0]
        self.assertEqual(payload["code"], "provider_unreachable")

    async def test_a_reply_the_contract_cannot_parse_runs_on_the_fallback(self):
        """A model that answered badly is the other half of "cannot be used"."""
        self.primary.reply = "I am not JSON at all."
        self._give_planner_a_fallback()

        goal = self._plan()
        await self.executor.run_planning(goal.id)

        self.assertEqual(self._errors(goal.id), [])
        self.assertEqual(len(self.second.calls), 1)
        payload = self._events(goal.id, "provider_fallback")[0]
        self.assertEqual(payload["code"], "agent_output_invalid")

    async def test_a_role_with_no_model_but_a_fallback_still_runs(self):
        self.registry.set_config("planner", AgentConfigUpdate(model_name=""))
        self._give_planner_a_fallback()

        goal = self._plan()
        await self.executor.run_planning(goal.id)

        self.assertEqual(self._errors(goal.id), [])
        payload = self._events(goal.id, "provider_fallback")[0]
        self.assertEqual(payload["from"]["model"], "")
        self.assertEqual(self.second.calls[0]["model"], "fallback-model")

    async def test_the_fallback_keeps_the_roles_temperature_and_max_tokens(self):
        """Temperature describes the job, not the model answering it.

        A second copy of these settings per target would mean the scribe's careful
        0.4 turned into whatever the fallback provider defaulted to.
        """
        self.factory.build_failures["anthropic"] = ProviderError("missing_api_key", "no key")
        self._give_planner_a_fallback()

        goal = self._plan()
        await self.executor.run_planning(goal.id)

        call = self.second.calls[0]
        self.assertEqual(call["temperature"], 0.15)
        self.assertEqual(call["max_tokens"], 1234)

    async def test_the_transcript_says_which_model_answered(self):
        """Both attempts are announced, so the reply is credited to the right one."""
        self.factory.build_failures["anthropic"] = ProviderError("missing_api_key", "no key")
        self._give_planner_a_fallback()

        goal = self._plan()
        await self.executor.run_planning(goal.id)

        assigned = self._events(goal.id, "agent_assigned")
        self.assertEqual(
            [(a["provider"], a["model"]) for a in assigned],
            [("ollama", "fallback-model")],
            "a target that was never called must not be announced as assigned",
        )


class TestItStaysNarrow(FallbackTestCase):
    async def test_without_a_fallback_the_failure_is_exactly_what_it_was(self):
        """No fallback configured means no change at all: same code, same message."""
        self.factory.build_failures["anthropic"] = ProviderError(
            "missing_api_key", "Anthropic API key is not set"
        )

        goal = self._plan()
        await self.executor.run_planning(goal.id)

        errors = self._errors(goal.id)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["code"], "agent_not_configured")
        self.assertIn("Anthropic API key is not set", errors[0]["message"])
        self.assertIn("Provider Keys", errors[0]["message"])
        self.assertNotIn("Both targets failed", errors[0]["message"])
        self.assertEqual(self._events(goal.id, "provider_fallback"), [])
        self.assertEqual(self.second.calls, [])

    async def test_one_fallback_attempt_and_no_more(self):
        """Two failures, two calls. An outage must not become a retry loop."""
        self.factory.build_failures["anthropic"] = ProviderError("missing_api_key", "no key")
        self.second.fail_with = ProviderError("provider_http", "ollama 500")
        self._give_planner_a_fallback()

        goal = self._plan()
        await self.executor.run_planning(goal.id)

        self.assertEqual(len(self.second.calls), 1, "the fallback is tried once")
        errors = self._errors(goal.id)
        self.assertEqual(len(errors), 1)
        # The primary's code leads, because that is the target the role is set to.
        self.assertEqual(errors[0]["code"], "agent_not_configured")
        self.assertIn("Both targets failed", errors[0]["message"])
        # Both attempts are named, with the code each failed under, so the user can
        # tell "the key is missing" from "the local server said 500".
        self.assertIn("primary anthropic (agent_not_configured)", errors[0]["message"])
        self.assertIn("fallback ollama (provider_http)", errors[0]["message"])

    async def test_a_failure_that_is_not_the_provider_stops_the_role(self):
        """An unknown failure is a bug; running it on another model hides it."""
        self.primary.fail_with = ProviderError("engine_bug", "something we did wrong")
        self._give_planner_a_fallback()

        goal = self._plan()
        await self.executor.run_planning(goal.id)

        errors = self._errors(goal.id)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["code"], "engine_bug")
        self.assertEqual(self.second.calls, [], "the fallback must not be reached")
        self.assertEqual(self._events(goal.id, "provider_fallback"), [])

    async def test_an_incomplete_fallback_is_not_promised(self):
        """Half a fallback fails exactly when it is needed, so it is not one."""
        self.factory.build_failures["anthropic"] = ProviderError("missing_api_key", "no key")
        self.registry.set_config(
            "planner", AgentConfigUpdate(fallback_provider="ollama", fallback_model_name="")
        )

        goal = self._plan()
        await self.executor.run_planning(goal.id)

        self.assertEqual(len(self._errors(goal.id)), 1)
        self.assertEqual(self.second.calls, [])
        self.assertEqual(self._events(goal.id, "provider_fallback"), [])


class TestSavingAFallback(unittest.IsolatedAsyncioTestCase):
    """The fallback is resolved by the same rules as the primary target.

    A second copy of these rules is how a fallback ends up speaking the wrong wire
    format, or keeping the previous provider's endpoint after a switch.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.temp_dir.name) / "t.db")
        self.registry = AgentRegistryService(self.conn, ProviderFactory(Keychain()), Keychain())

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_a_builtin_fallback_inherits_its_protocol_and_endpoint(self):
        cfg = self.registry.set_config(
            "planner", AgentConfigUpdate(fallback_provider="ollama", fallback_model_name="qwen3:8b")
        )
        self.assertEqual(cfg.fallback_protocol, "ollama")
        self.assertEqual(cfg.fallback_base_url, "http://127.0.0.1:11434")
        self.assertTrue(cfg.has_fallback)

    def test_switching_the_fallback_provider_drops_the_old_endpoint(self):
        self.registry.set_config(
            "planner", AgentConfigUpdate(fallback_provider="ollama", fallback_model_name="qwen3:8b")
        )
        cfg = self.registry.set_config(
            "planner", AgentConfigUpdate(fallback_provider="groq", fallback_model_name="llama-3.3-70b")
        )
        self.assertEqual(cfg.fallback_base_url, "https://api.groq.com/openai/v1")
        self.assertEqual(cfg.fallback_protocol, "openai_compat")

    def test_a_custom_fallback_needs_a_protocol_and_an_endpoint(self):
        from engine.services import ApiError

        with self.assertRaises(ApiError) as caught:
            self.registry.set_config(
                "planner", AgentConfigUpdate(fallback_provider="myproxy", fallback_model_name="m")
            )
        self.assertEqual(caught.exception.code, "invalid_provider")

    def test_a_local_only_provider_stays_local_as_a_fallback(self):
        from engine.services import ApiError

        with self.assertRaises(ApiError) as caught:
            self.registry.set_config(
                "planner",
                AgentConfigUpdate(
                    fallback_provider="ollama",
                    fallback_model_name="m",
                    fallback_base_url="http://api.example.com:11434",
                ),
            )
        self.assertEqual(caught.exception.code, "invalid_base_url")

    def test_clearing_the_provider_clears_the_whole_target(self):
        self.registry.set_config(
            "planner", AgentConfigUpdate(fallback_provider="ollama", fallback_model_name="qwen3:8b")
        )
        cfg = self.registry.set_config("planner", AgentConfigUpdate(fallback_provider=None))
        self.assertFalse(cfg.has_fallback)
        self.assertIsNone(cfg.fallback_protocol)
        self.assertIsNone(cfg.fallback_base_url)

    def test_the_fallback_is_built_from_the_same_config_without_the_roles_key(self):
        """The role's stored key belongs to its primary provider.

        Carrying the ref across would look up a key for the fallback provider
        under the primary's reference, which is how a fallback ends up sending one
        provider's credential to another's endpoint.
        """
        cfg = self.registry.set_config(
            "planner",
            AgentConfigUpdate(
                provider="deepseek",
                model_name="deepseek-chat",
                fallback_provider="ollama",
                fallback_model_name="qwen3:8b",
            ),
        )
        cfg = self.registry.set_config("planner", AgentConfigUpdate(api_key="sk-role-secret"))
        target = self.registry.fallback_config_for(cfg)
        assert target is not None, "the role has a fallback configured"
        self.assertEqual(target.provider, "ollama")
        self.assertEqual(target.protocol, "ollama")
        self.assertEqual(target.model_name, "qwen3:8b")
        self.assertIsNone(target.api_key_ref)
        self.assertEqual(target.temperature, cfg.temperature)

    def test_a_role_without_a_fallback_has_no_second_target(self):
        cfg = self.registry.get_config("planner")
        self.assertIsNone(self.registry.fallback_config_for(cfg))


if __name__ == "__main__":
    unittest.main()
