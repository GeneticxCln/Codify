"""What the pipeline's modules share: the exceptions a stage can end with, and the stage-outcome vocabulary.

These used to sit at the top of `engine/executor.py`. They are here because the exceptions are raised by
the orchestrator, the conductor's tools and the stages alike, and a definition that three modules need
cannot live in any one of them without the others importing it back.
"""

from __future__ import annotations

from typing import Any

from engine.providers import ProviderError


class AgentOutputInvalid(Exception):
    def __init__(self, message: str, role: str | None = None):
        super().__init__(message)
        self.code = "agent_output_invalid"
        # Same shape as ProviderError, so a caller reporting "what happened" on
        # either one does not need to know which it is holding.
        self.message = message
        # Which role produced it. Carried on the error event so the UI can show
        # that role's config, credential, and discovered models instead of
        # making the user work out from the message which agent to go and check.
        self.role = role


class PathRefused(AgentOutputInvalid):
    """A fixer path the workspace refused, carried through the re-ask with its own code.

    An `AgentOutputInvalid` in every way that matters to `run_agent` (one re-ask, then the fallback rule),
    but its `code` is `path_escape`, so a step that still cannot name a legal path fails with the code the UI
    and the stats already know that failure by.
    """

    def __init__(self, message: str, role: str | None = None):
        super().__init__(message, role=role)
        self.code = "path_escape"


class WriteWithdrawn(Exception):
    """The goal stopped being writable while the fixer was answering, so its reply was not applied.

    A person pressed Cancel or Pause during a model call that takes minutes. It is not a failure of the
    model or the engine and must not be reported as one, and it must not be re-asked or handed to a
    fallback — hence a class of its own, outside `AgentOutputInvalid`. The message is what the conductor is
    told: nothing was written.
    """


class TestsFailed(AgentOutputInvalid):
    """The verifier ran and reported a failure.

    Distinct from AgentOutputInvalid because the agent behaved correctly: the
    code under test is what failed. Reporting this as "agent output invalid"
    sent users looking for a malformed model reply that never existed.
    """

    def __init__(self, message: str, role: str | None = "verifier"):
        super().__init__(message, role)
        self.code = "tests_failed"


class AgentNotConfigured(ProviderError):
    """A role cannot be called at all: no model chosen, or no credential.

    That is a setup problem, not a bad model reply. Reporting it as
    `agent_output_invalid` sent the user hunting for malformed JSON that was
    never produced, while the real fix — pick a model / add a key — was two
    clicks away in Settings. Since no model names are seeded (see
    `models.DEFAULT_AGENTS`), a fresh install hits this on its first prompt.
    """

    def __init__(self, role: str, message: str):
        super().__init__("agent_not_configured", message)
        self.role = role


# ── stage outcomes (docs/04 §4.7) ───────────────────────────────────────────
# One closed vocabulary for "what did this stage achieve", so a per-role
# success rate is a count of declared outcomes rather than a guess at what a
# missing event meant. The eight stages, and nothing outside them:
#
#   laya        skipped | allow | block | cancelled | unavailable
#   librarian   pack | incomplete | invalid | cancelled | unavailable
#   design      contract | declined | invalid | cancelled | unavailable
#   planner     plan | consult | invalid | cancelled | unavailable
#   fixer       wrote | no_change | replayed | invalid | cancelled | unavailable
#   verifier    pass | fail | skip | refused | invalid | cancelled | unavailable
#   critic      approve | request_changes | invalid | cancelled | unavailable
#   scribe      committed | nothing_to_commit | not_a_repo | invalid | cancelled | unavailable
#
# `invalid` is a reply the engine could not use; `unavailable` is a call that
# could not be made or completed. They are different failures to the person
# choosing what to fix, and a per-role rate that merged them would hide a role
# whose prompt needs work behind a role that has no key.
STAGE_OUTCOMES: dict[str, tuple[str, ...]] = {
    "laya": ("skipped", "allow", "block", "cancelled", "unavailable"),
    "librarian": ("pack", "incomplete", "invalid", "cancelled", "unavailable"),
    "design": ("contract", "declined", "invalid", "cancelled", "unavailable"),
    "planner": ("plan", "consult", "invalid", "cancelled", "unavailable"),
    "fixer": ("wrote", "no_change", "replayed", "invalid", "cancelled", "unavailable"),
    "verifier": ("pass", "fail", "skip", "refused", "invalid", "cancelled", "unavailable"),
    "critic": ("approve", "request_changes", "invalid", "cancelled", "unavailable"),
    "scribe": ("committed", "nothing_to_commit", "not_a_repo", "skipped", "invalid", "cancelled", "unavailable"),
}


class CriticRejection(AgentOutputInvalid):
    def __init__(self, message: str, reasons: list[str], role: str | None = "critic"):
        super().__init__(message, role)
        self.reasons = reasons


def _verifier_outcome(result: dict[str, Any]) -> str:
    """The verifier's stage outcome, from the verdict it published.

    Checked against the vocabulary rather than passed through: a verdict the
    engine did not already reject (`_test_result` refuses anything outside
    pass/fail/skip) would otherwise put a name in the metrics that no reader
    of the table can interpret, which is how a rate stops meaning anything.
    """
    verdict = str((result or {}).get("verdict") or "")
    return verdict if verdict in STAGE_OUTCOMES["verifier"] else "invalid"
