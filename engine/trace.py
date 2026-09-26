"""Record a run's model calls, and serve them back without a provider.

The event log records *what happened* — a verdict, a diff, a commit. It does not
record *what was said*, so a run that misbehaved cannot be reproduced: the
prompt that produced the bad reply is gone, and a bug report is a story. This
is the recording that makes it a reproduction.

The rules, each one a decision about somebody's data:

- **Opt-in per goal, off by default.** A recording is a copy of model output
  about the user's code. It is switched on for a run someone is debugging, and
  it is deletable at any time (`DELETE /goals/{id}/trace`).
- **The request is stored as a digest.** `prompt_hash` is what a replay has to
  match on, and the prompt is the most sensitive thing in a run: the goal, the
  evidence pack, and the user's own source. `CODIFY_TRACE_PROMPTS=1` opts into
  keeping the text too, for when "exactly what was the model handed" is the
  question being asked.
- **A replay that does not match refuses.** Serving the wrong recorded reply
  would produce a green run that proves nothing, which is worse than no replay
  at all: it looks like evidence. A mismatch names the call that was expected
  and the one that arrived, so the failure says what diverged.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid
from typing import Any

from engine.providers import BaseProvider, ProviderError

# Whether to keep the prompt text beside its digest. An environment variable
# rather than a setting: it is a debugging switch for a single process, and a
# per-goal "store the whole prompt" checkbox in a settings screen would be an
# invitation rather than a deliberate act.
_TRACE_PROMPTS_ENV = "CODIFY_TRACE_PROMPTS"


def keep_prompts() -> bool:
    return (os.environ.get(_TRACE_PROMPTS_ENV) or "").strip() not in ("", "0", "false")


def prompt_digest(system_prompt: str, user_prompt: str) -> str:
    """A stable identity for one request, without keeping the request.

    Both prompts go in, separated by a byte that cannot appear in either, so
    `("ab", "c")` and `("a", "bc")` cannot collide. Truncated to 32 hex chars:
    enough to make a collision implausible for one goal's worth of calls, and
    a fixed width a human can compare by eye while debugging a mismatch.
    """
    joined = f"{system_prompt}\x00{user_prompt}".encode("utf-8", "replace")
    return hashlib.sha256(joined).hexdigest()[:32]


class TraceMismatch(ProviderError):
    """A replay was asked for a call its recording does not contain.

    A distinct code so a test or a benchmark can tell "the run diverged" from
    "the provider was down", which would otherwise both look like a failed run.
    """

    def __init__(self, message: str) -> None:
        super().__init__("trace_mismatch", message)


class TraceService:
    """Reads and writes the recording of one goal's model calls."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._db = conn
        # The last write that failed, as (goal_id, reason). In memory and only
        # the latest one: this is a diagnostic for "why is my recording
        # empty?", not a log to keep, and a table to record failures of the
        # recording table would be its own way of failing.
        self._last_error: tuple[str, str] | None = None

    def enabled(self, goal_id: str) -> bool:
        """Is this goal being recorded? One row, read per call.

        Read rather than cached: a run can be traced after it was created (a
        user noticing a problem mid-planning), and a cached answer would keep
        recording a run the user has switched off.
        """
        row = self._db.execute(
            "SELECT trace FROM goals WHERE id = ?", (goal_id,)
        ).fetchone()
        return bool(row and row["trace"])

    def record(
        self,
        goal_id: str,
        step_id: str | None,
        *,
        role: str,
        provider: str,
        model: str,
        temperature: float,
        max_tokens: int,
        system_prompt: str,
        user_prompt: str,
        response: str,
        usage: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Store one call. Never raises: a recording is a convenience, and a goal
        that failed because its debug trace could not be written is a goal that
        failed for the wrong reason."""
        try:
            row = self._db.execute(
                "SELECT coalesce(max(seq), 0) AS seq FROM trace_calls WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
            tokens = usage or {}
            self._db.execute(
                """INSERT INTO trace_calls (
                       id, goal_id, step_id, seq, role, provider, model, temperature,
                       max_tokens, prompt_hash, system_hash, system_prompt, user_prompt,
                       response, input_tokens, output_tokens, duration_ms, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()), goal_id, step_id, int(row["seq"]) + 1, role,
                    provider, model, temperature, max_tokens,
                    prompt_digest(system_prompt, user_prompt),
                    prompt_digest(system_prompt, ""),
                    system_prompt if keep_prompts() else None,
                    user_prompt if keep_prompts() else None,
                    response,
                    _int_or_none(tokens.get("input_tokens")),
                    _int_or_none(tokens.get("output_tokens")),
                    duration_ms,
                    time.time(),
                ),
            )
            self._db.commit()
        except Exception as exc:
            # Swallowed on purpose (docs/04 §8): a trace is evidence, not a
            # deliverable, and the goal it was recording is the thing that
            # matters — so a write failure never fails the run.
            #
            # Not *silently*, though. This is the only place a user can end up
            # holding a goal they armed for recording with an empty summary,
            # and "no calls recorded" says nothing about whether they were
            # never made or could not be stored. The reason rides along to
            # `GET /goals/{id}/trace` so the panel can say which.
            self._last_error = (goal_id, f"{type(exc).__name__}: {exc}")
            return

    def calls(self, goal_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM trace_calls WHERE goal_id = ? ORDER BY seq", (goal_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self, goal_id: str) -> int:
        row = self._db.execute(
            "SELECT count(*) AS n FROM trace_calls WHERE goal_id = ?", (goal_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def delete(self, goal_id: str) -> int:
        """Forget a goal's recording. Idempotent, and the user's call."""
        row = self._db.execute("DELETE FROM trace_calls WHERE goal_id = ?", (goal_id,))
        self._db.commit()
        return int(row.rowcount or 0)

    def delete_older_than(self, keep_days: int) -> int:
        """Prune recordings older than the retention window. 0 keeps everything."""
        if keep_days <= 0:
            return 0
        row = self._db.execute(
            "DELETE FROM trace_calls WHERE created_at < ?",
            (time.time() - keep_days * 86400,),
        )
        self._db.commit()
        return int(row.rowcount or 0)

    def summary(self, goal_id: str) -> dict[str, Any]:
        """What a goal recorded, for the UI: per role, in order, without bodies."""
        calls = self.calls(goal_id)
        by_role: dict[str, int] = {}
        for call in calls:
            role = str(call.get("role") or "unknown")
            by_role[role] = by_role.get(role, 0) + 1
        return {
            "goal_id": goal_id,
            "calls": len(calls),
            "by_role": by_role,
            # Non-null only when a write for THIS goal failed, so a summary
            # that says zero calls can say why. None is the normal case, and
            # is stated as null rather than omitted so the field's shape does
            # not depend on whether something broke.
            "recording_error": (
                self._last_error[1]
                if self._last_error is not None and self._last_error[0] == goal_id
                else None
            ),
            # True only when the text was actually kept. Stated so the UI can
            # say "digests only" rather than implying a transcript exists.
            "prompts_kept": any(call.get("user_prompt") for call in calls),
            "recorded": [
                {
                    "seq": call["seq"],
                    "role": call["role"],
                    "model": call["model"],
                    "prompt_hash": call["prompt_hash"],
                    "input_tokens": call["input_tokens"],
                    "output_tokens": call["output_tokens"],
                    "duration_ms": call["duration_ms"],
                    "at": call["created_at"],
                }
                for call in calls
            ],
        }

    def replay_provider(self, goal_id: str) -> ReplayProvider:
        """A provider that answers from this goal's recording.

        Handed to a `ProviderFactory` in place of a real one, which is how a
        test or a benchmark runs a goal it has already run once: the wiring is
        the same, the prompts are the same, and the replies are the ones the
        recorded run actually got.
        """
        return ReplayProvider(self.calls(goal_id))


class ReplayProvider(BaseProvider):
    """Serves recorded replies, and refuses the ones it does not have.

    Matched on (role, prompt digest) in recording order. The digest is the
    point: it proves the run is the run that was recorded. A goal whose
    workspace changed by one line produces a different prompt, refuses here,
    and says so — which is the correct answer, because the recorded replies
    would be answering a different question.
    """

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = list(calls)
        self._served: set[str] = set()
        self.served: list[str] = []
        # Which role this instance is serving. Set by the factory that builds
        # it, from the `AgentConfig` it was asked for — the same signal the
        # real providers get, rather than a guess from the prompt's wording
        # (these prompts name each other on purpose, so a keyword scan hands
        # back another role's script).
        self.current_role: str | None = None

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
        role = self.current_role or "unknown"
        digest = prompt_digest(system_prompt, user_prompt)
        for call in self._calls:
            key = f"{call['role']}:{call['prompt_hash']}"
            if key in self._served or call["role"] != role or call["prompt_hash"] != digest:
                continue
            self._served.add(key)
            self.served.append(role)
            return str(call.get("response") or "")
        raise TraceMismatch(
            self._explain(role, digest)
        )

    def _explain(self, role: str, digest: str) -> str:
        """Say what diverged, not just that something did."""
        want = f"{role}:{digest}"
        for call in self._calls:
            if call["role"] == role and call["prompt_hash"] != digest:
                return (
                    f"replay: {role} was handed a different prompt than the recording "
                    f"holds (recorded {call['prompt_hash'][:12]}…, now {digest[:12]}…) — "
                    "the run has diverged, so its recorded replies would answer a "
                    "different question"
                )
        if not any(call["role"] == role for call in self._calls):
            return (
                f"replay: the recording holds no call for {role} — it was not run, or it "
                "was run before tracing was enabled"
            )
        return (
            f"replay: {role} was called more times than the recording holds "
            f"(expected {want}) — the run is not reproducible from this trace"
        )


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
