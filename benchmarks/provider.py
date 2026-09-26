"""A deterministic provider for the smoke tier.

The point is that the smoke tier must never cost money, never need a network,
and never produce a different answer than it did yesterday — otherwise a green
run tells you nothing about whether the last change moved the needle. So this
provider answers from a fixed script rather than from a model.

**It cannot measure model quality, and it must not be read as if it did.** A
canned reply is written by the harness, so "the task succeeded" under this
provider says the pipeline moved the bytes it was asked to move. The manifest
marks which of a task's checks are harness checks (stage sequence, completion,
tokens, files reaching disk) and which are quality checks (`file_contains`,
`test_command`); quality checks are *skipped* under this provider rather than
reported as passes, because a skipped check is honest and a passing one here
would be a lie. That split is why `repo_scale` declares a real provider.
"""

from __future__ import annotations

import json
from typing import Any

from engine.models import AgentConfig
from engine.providers import BaseProvider, Keychain, ProviderFactory

# What one canned write from the manifest looks like: a path and the bytes the
# fixer should put there.
CannedWrite = dict[str, str]

# Tokens a canned call reports. Fixed rather than zero so a run still has
# something for the per-stage cost table to add up — the numbers are synthetic
# and are labelled as such in the report, but a `0` would read as "this stage
# was free" instead of "this stage was not measured against a model".
_CANNED_USAGE = {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140}


class CannedProvider(BaseProvider):
    """Answers by role, and has the fixer write the task's scripted files."""

    def __init__(self, writes: list[CannedWrite] | None = None) -> None:
        self.writes = writes or []
        # Set by `CannedFactory.build`, from the `AgentConfig` the engine asked
        # for — the same signal the real providers get, rather than a guess
        # from the prompt's wording.
        self.current_role: str | None = None
        self.calls: list[str] = []

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        role = self.current_role or "unknown"
        self.calls.append(role)
        if self.usage_sink is not None:
            self.usage_sink(dict(_CANNED_USAGE))
        return json.dumps(self._reply(role))

    def _reply(self, role: str) -> dict[str, Any]:
        if role == "librarian":
            return {"summary": "canned reconnaissance", "files": [], "conventions": [], "enough": True}
        if role == "design":
            # This is not a design goal, and saying so is the correct answer
            # rather than a shortcut: `declined` is a role that worked.
            return {"applies": False}
        if role == "planner":
            return {
                "steps": [
                    {
                        "title": "Make the requested change",
                        "description": "Apply the change the goal asks for.",
                        "suggested_paths": [w["path"] for w in self.writes] or ["README.md"],
                    }
                ]
            }
        if role == "fixer":
            return {
                "files": [
                    {"path": w["path"], "action": "create", "content": w["content"]}
                    for w in self.writes
                ]
            }
        if role == "verifier":
            return {"argv": None, "verdict": "pass", "explanation": "canned pass"}
        if role == "critic":
            return {"decision": "approve", "reasons": []}
        if role == "scribe":
            return {"summary": "canned change", "commit_message": "chore: benchmark run"}
        # Laya never reaches the provider (the gate is skipped in a benchmark
        # run), but an unknown role must fail loudly rather than return "{}".
        raise ValueError(f"canned provider has no reply for role {role!r}")


class CannedFactory(ProviderFactory):
    """Hands every role the same scripted provider, tagged with its role."""

    def __init__(self, provider: CannedProvider) -> None:
        super().__init__(Keychain())
        self.provider = provider

    def build(self, config: AgentConfig) -> BaseProvider:
        self.provider.current_role = config.role
        return self.provider
