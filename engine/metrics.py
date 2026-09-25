"""What each stage of a goal achieved, what it cost, and how it went wrong.

`engine/stats.py` answers "how are this install's goals going" from the goals
table and the call log. This answers the question one level down: **per role,
per stage** — did it do its job, what did it spend, how long did it take, and
when it went wrong, what was the code and did the retry recover it.

Three rules everything here follows, and they exist because the easier thing to
do is to lie:

1. **A rate is never computed from a denominator that has not finished.** A
   cancelled stage, like an active goal, has not happened yet; counting it
   either way makes the number move for reasons that have nothing to do with
   whether the role works. Cancelled is reported on its own.
2. **A rate is never the only number.** Every aggregate here also carries the
   outcome histogram it was computed from, so "the verifier's success rate is
   91%" can be checked against "verifier fail: 9" rather than taken on trust.
   A percentage with its evidence hidden is a claim.
3. **Tokens are tokens.** There is no price table here and no currency on any
   surface that reads these numbers, because a price that silently stops
   matching the provider is worse than no price at all (docs/04 §4.4).

Pure functions over parsed event dicts, the same shape `engine/stats.py` takes
and for the same reason: the arithmetic is testable without an HTTP server and
cannot drift between how it is computed and how it is tested. Live callers
pass `now`; the no-clock anchor exists only for tests building a timeline.
"""

from __future__ import annotations

from typing import Any

# Which outcomes mean "this role did its job", per stage. Deliberately a
# statement about the ROLE, not about the goal: a verifier that returns "fail"
# and a critic that requests changes have both worked exactly as specified,
# and counting those as role failures would report a healthy pipeline as
# broken. The goal-level rate already exists (`engine/stats.py`) and answers
# the other question. The outcome histogram ships alongside every rate so a
# reader who means the other thing can compute it without asking the engine.
STAGE_SUCCESS_OUTCOMES: dict[str, frozenset[str]] = {
    "laya": frozenset({"allow", "skipped"}),
    "librarian": frozenset({"pack", "incomplete"}),
    "design": frozenset({"contract", "declined"}),
    "planner": frozenset({"plan", "consult"}),
    "fixer": frozenset({"wrote", "no_change", "replayed"}),
    "verifier": frozenset({"pass", "skip"}),
    "critic": frozenset({"approve"}),
    "scribe": frozenset({"committed", "nothing_to_commit", "not_a_repo", "skipped"}),
}
# The stages, in pipeline order, so a table reads as the pipeline and not as an
# alphabetical accident. A stage absent from the event log reads as never-run
# rather than as missing, which is a different thing and is shown as such.
STAGE_ORDER: tuple[str, ...] = (
    "laya", "librarian", "design", "planner", "fixer", "verifier", "critic", "scribe",
)
# Outcomes that mean the stage never got to finish, and so are excluded from
# the success denominator rather than counted against the role.
UNCANCELLED_ONLY: frozenset[str] = frozenset({"cancelled"})


def _within(ts: float, cutoff: float | None) -> bool:
    return cutoff is None or ts >= cutoff


def _window_cutoff(window_days: int, stamps: list[float], now: float | None) -> float | None:
    """The window's lower bound, or None for "all time".

    `now` is the honest anchor and every live path passes it. Without it the
    newest event stands in, which is right for a test building a synthetic
    timeline and wrong for an idle install: the window would slide forward
    until stale data landed inside it and be reported as this week.
    """
    if window_days <= 0:
        return None
    if now is not None:
        return now - window_days * 86400
    return (max(stamps) if stamps else 0.0) - window_days * 86400


def percentile(values: list[float], pct: float) -> int | None:
    """The `pct` percentile of `values`, nearest-rank, or None when empty.

    Nearest-rank rather than an interpolated percentile because these are small
    samples of real runs: an interpolated p95 of four runs invents a number
    between two that never happened, and a latency table that reports a
    duration nothing took is a number nobody can act on.
    """
    if not values:
        return None
    ordered = sorted(values)
    if pct <= 0:
        return int(ordered[0])
    rank = int(-(-pct * len(ordered) // 100)) - 1  # ceil, no float import
    return int(ordered[min(max(rank, 0), len(ordered) - 1)])


def _mean(values: list[float]) -> int | None:
    # None, not 0: "no run was recorded" is not "every run was instant".
    return int(sum(values) / len(values)) if values else None


def role_success_rate(
    events: list[dict[str, Any]], window_days: int = 0, *, now: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Per role: how often it did its job, and what that cost.

    Reads the `stage_result` events the executor publishes (docs/04 §4.4) and
    the `usage` events for spend. A role that has never run is present with
    zero counts and a `null` rate — an install that has only ever run
    `/start` has not proved its librarian broken, and a row of 0% would say
    exactly that.
    """
    cutoff = _window_cutoff(
        window_days, [float(e.get("timestamp") or 0.0) for e in events], now,
    )
    by_role: dict[str, dict[str, Any]] = {}
    for stage in STAGE_ORDER:
        by_role[stage] = {
            "role": stage,
            "runs": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "outcomes": {},
            "avg_duration_ms": None,
            "p95_duration_ms": None,
            "tokens": 0,
        }
    durations: dict[str, list[float]] = {stage: [] for stage in STAGE_ORDER}
    tokens: dict[str, int] = {stage: 0 for stage in STAGE_ORDER}

    for ev in events:
        if not _within(float(ev.get("timestamp") or 0.0), cutoff):
            continue
        payload = ev.get("payload") or {}
        kind = ev.get("type")
        if kind == "usage":
            role = str(payload.get("role") or "")
            if role in tokens:
                for key in ("input_tokens", "output_tokens"):
                    value = payload.get(key)
                    if isinstance(value, (int, float)):
                        tokens[role] += int(value)
            continue
        if kind != "stage_result":
            continue
        role = str(payload.get("role") or payload.get("stage") or "")
        if role not in by_role:
            continue
        entry = by_role[role]
        outcome = str(payload.get("outcome") or "unavailable")
        entry["runs"] += 1
        histogram: dict[str, int] = entry["outcomes"]
        histogram[outcome] = histogram.get(outcome, 0) + 1
        if outcome in UNCANCELLED_ONLY:
            entry["cancelled"] += 1
        elif outcome in STAGE_SUCCESS_OUTCOMES.get(role, frozenset()):
            entry["succeeded"] += 1
        else:
            entry["failed"] += 1
        duration = payload.get("duration_ms")
        if isinstance(duration, (int, float)):
            durations[role].append(float(duration))

    for stage, entry in by_role.items():
        terminal = entry["succeeded"] + entry["failed"]
        entry["success_rate"] = (
            round(100 * entry["succeeded"] / terminal) if terminal else None
        )
        entry["tokens"] = tokens.get(stage, 0)
        entry["avg_duration_ms"] = _mean(durations[stage])
        entry["p95_duration_ms"] = percentile(durations[stage], 95)
    return by_role


def _shares(tokens: dict[str, int]) -> dict[str, int]:
    """Whole-percent shares that add up to 100 (largest-remainder rounding).

    Plain `round()` does not: six equal stages each round 16.67 to 17 and the
    column sums to 102, which is the kind of arithmetic error that makes a
    reader distrust every other number in the table. The remainder is handed
    out largest-first, so the rounding lands on the stages that were rounded
    down hardest.
    """
    total = sum(tokens.values())
    if total <= 0:
        return {stage: 0 for stage in tokens}
    exact = {stage: 100 * value / total for stage, value in tokens.items()}
    floored = {stage: int(value) for stage, value in exact.items()}
    left = 100 - sum(floored.values())
    order = sorted(tokens, key=lambda stage: (-(exact[stage] - floored[stage]), stage))
    for stage in order[:left]:
        floored[stage] += 1
    return floored


def stage_costs(
    events: list[dict[str, Any]], window_days: int = 0, *, now: float | None = None,
) -> list[dict[str, Any]]:
    """Per stage: runs, tokens, latency, and its share of the goal's spend.

    A stage, not a call: a step's cost is the model call *and* the engine work
    between them (diff rendering, `fs.apply`, a sandboxed command), which is
    why this reads the stage's own `duration_ms` rather than summing the
    `usage` events' durations. `token_share` is against the window's total, so
    the rows add up to 100 and a stage that is 80% of the bill says so.
    """
    cutoff = _window_cutoff(
        window_days, [float(e.get("timestamp") or 0.0) for e in events], now,
    )
    rows: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.get("type") != "stage_result":
            continue
        if not _within(float(ev.get("timestamp") or 0.0), cutoff):
            continue
        payload = ev.get("payload") or {}
        stage = str(payload.get("stage") or payload.get("role") or "unknown")
        entry = rows.setdefault(stage, {
            "stage": stage,
            "role": str(payload.get("role") or stage),
            "runs": 0,
            "tokens": 0,
            "calls": 0,
            "durations": [],
            "outcomes": {},
        })
        entry["runs"] += 1
        tokens = payload.get("tokens")
        if isinstance(tokens, (int, float)):
            entry["tokens"] += int(tokens)
        calls = payload.get("calls")
        if isinstance(calls, (int, float)):
            entry["calls"] += int(calls)
        duration = payload.get("duration_ms")
        if isinstance(duration, (int, float)):
            entry["durations"].append(float(duration))
        outcome = str(payload.get("outcome") or "unavailable")
        histogram: dict[str, int] = entry["outcomes"]
        histogram[outcome] = histogram.get(outcome, 0) + 1

    shares = _shares({stage: int(entry["tokens"]) for stage, entry in rows.items()})
    out: list[dict[str, Any]] = []
    for stage in STAGE_ORDER:
        known: dict[str, Any] | None = rows.get(stage)
        if known is None:
            out.append({
                "stage": stage, "role": stage, "runs": 0, "tokens": 0, "calls": 0,
                "avg_duration_ms": None, "p95_duration_ms": None,
                "token_share": shares.get(stage, 0), "outcomes": {},
            })
            continue
        out.append({
            "stage": stage,
            "role": known["role"],
            "runs": known["runs"],
            "tokens": known["tokens"],
            "calls": known["calls"],
            "avg_duration_ms": _mean(known["durations"]),
            "p95_duration_ms": percentile(known["durations"], 95),
            "token_share": shares.get(stage, 0),
            "outcomes": known["outcomes"],
        })
    # A stage nobody has heard of still belongs in the table, at the end, in
    # the order it runs — the alternative is a stage that appears only once it
    # has data, which reads as "it is not part of the pipeline".
    for stage, entry in rows.items():
        if stage in STAGE_ORDER:
            continue
        out.append({
            "stage": stage, "role": entry["role"], "runs": entry["runs"],
            "tokens": entry["tokens"], "calls": entry["calls"],
            "avg_duration_ms": _mean(entry["durations"]),
            "p95_duration_ms": percentile(entry["durations"], 95),
            "token_share": shares.get(stage, 0),
            "outcomes": entry["outcomes"],
        })
    return out


def failure_breakdown(
    events: list[dict[str, Any]], window_days: int = 0, *, now: float | None = None,
) -> dict[str, Any]:
    """Failures by cause, by role, by stage — and how many the retries got back.

    The recovery number is the one worth building a dashboard around: a
    `fix_retry` that ends in a step that passes is the fix→verify loop doing
    its job, and a run of unrecovered ones is a loop that is burning tokens
    without finishing anything. Both failure events are counted — an `error`
    (the goal or the step failed) and an `agent_call_failed` (one call did not)
    — because they fail at different scales and a provider that has been
    refused for a week should not hide behind goals that happened to pass.
    """
    cutoff = _window_cutoff(
        window_days, [float(e.get("timestamp") or 0.0) for e in events], now,
    )
    in_window = [
        e for e in events if _within(float(e.get("timestamp") or 0.0), cutoff)
    ]

    by_code: dict[str, int] = {}
    by_role: dict[str, int] = {}
    causes: dict[str, dict[str, Any]] = {}
    for ev in in_window:
        kind = ev.get("type")
        if kind not in ("error", "agent_call_failed"):
            continue
        payload = ev.get("payload") or {}
        code = str(payload.get("code") or "unknown")
        by_code[code] = by_code.get(code, 0) + 1
        role = payload.get("role")
        if role:
            role_key = str(role)
            by_role[role_key] = by_role.get(role_key, 0) + 1
        seen = causes.get(code)
        entry: dict[str, Any] = {
            "code": code,
            "count": (int(seen["count"]) + 1) if seen else 1,
            "message": (
                str(seen["message"]) if seen
                else str(payload.get("message") or "")[:300]
            ),
            "last_seen": ev.get("timestamp"),
        }
        causes[code] = entry

    by_stage: dict[str, dict[str, Any]] = {}
    for ev in in_window:
        if ev.get("type") != "stage_result":
            continue
        payload = ev.get("payload") or {}
        stage = str(payload.get("stage") or payload.get("role") or "unknown")
        outcome = str(payload.get("outcome") or "unavailable")
        if outcome in STAGE_SUCCESS_OUTCOMES.get(stage, frozenset()) or outcome in UNCANCELLED_ONLY:
            continue
        if stage not in by_stage:
            by_stage[stage] = {"stage": stage, "failed": 0, "outcomes": {}}
        entry = by_stage[stage]
        entry["failed"] += 1
        histogram: dict[str, int] = entry["outcomes"]
        histogram[outcome] = histogram.get(outcome, 0) + 1

    retried, recovered = _recovery(in_window)
    total = sum(by_code.values())
    return {
        "total": total,
        "by_code": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
        "by_role": dict(sorted(by_role.items(), key=lambda kv: -kv[1])),
        "by_stage": dict(sorted(by_stage.items())),
        "causes": sorted(causes.values(), key=lambda c: -int(c["count"]))[:20],
        "retries": retried,
        "recovered": recovered,
        # Null, not 100: no retries is not a perfect record, it is no evidence.
        "recovery_rate": round(100 * recovered / retried) if retried else None,
    }


def _recovery(events: list[dict[str, Any]]) -> tuple[int, int]:
    """(retries, retries the step then got past) — per (goal, step).

    A retry counts as recovered when a later `test_result` on the same step
    reports a verdict that is not a failure, or the step reaches COMPLETED.
    Both are read from the same log, and the pairing is what makes the number
    mean "the loop worked" rather than "some other step passed later".
    """
    retried: set[tuple[str, str]] = set()
    finished: set[tuple[str, str]] = set()
    seen_order: list[tuple[str, str]] = []
    for ev in events:
        goal_id = str(ev.get("goal_id") or "")
        step_id = str(ev.get("step_id") or "")
        if not step_id:
            continue
        key = (goal_id, step_id)
        kind = ev.get("type")
        payload = ev.get("payload") or {}
        if kind == "fix_retry":
            if key not in retried:
                retried.add(key)
                seen_order.append(key)
        elif kind == "test_result" and payload.get("verdict") in ("pass", "skip"):
            finished.add(key)
        elif kind == "step_status" and payload.get("status") == "COMPLETED":
            finished.add(key)
    return len(retried), sum(1 for key in seen_order if key in finished)
