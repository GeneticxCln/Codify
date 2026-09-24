"""Cross-goal statistics: what the engine's history adds up to.

The goal table and the event log already record everything this summarizes —
statuses, token spend per role and model, call durations, when each goal ran.
What was missing is the question a single goal cannot answer: *across* goals, is
this setup working, what does it cost, and is it getting better or worse? That
is the gap between the per-goal audit document (one goal's whole story) and a
trend (many goals' shape).

Pure by design, like `role_repair`: no DB, no HTTP, no clock. The endpoint hands
it plain dicts (goal rows and parsed event payloads) and the tests exercise the
arithmetic directly, so the numbers cannot drift between how they are computed
and how they are tested.

Honesty rules the window filter enforces:

- **Success is COMPLETED.** A cancelled goal was stopped, not finished; a failed
  goal failed. Collapsing those into "success-ish" would flatter a broken setup.
  Active goals count in totals but never in the rate — a run in flight is
  neither a success nor a failure yet.
- **A duration average is None when nothing measured it** — events written
  before durations existed must not read as instant calls.
- **Daily buckets are sparse**: only days with activity appear. A dense zero
  fill would claim "nothing happened on those days", which the window cannot
  prove (older events may sit outside it, and a fresh install has no days).
- **A window is anchored to the wall clock**, via the `now` the endpoint passes
  in, so "last 24h" means the last 24 hours even on an install that has been
  idle for months. An idle store gets an *empty* window and a null success
  rate — never its last run relabelled as recent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Window choices the endpoint accepts, in days. 0 means "all time" — the one
# window with no cutoff, and the only honest default for a fresh install.
WINDOW_CHOICES_DAYS = (1, 7, 30, 0)

# Statuses that mean the goal is still in play. They count in totals; they must
# never count in the success rate.
ACTIVE_STATUSES = frozenset({"PLANNING", "PENDING", "RUNNING", "PAUSED"})

# How many days the daily trend covers for a given window. "All time" trends by
# day are a table, not a chart, so the widest chart is the 30-day one; the
# caller can still read per-day rows from the raw totals if it wants more.
DAILY_WINDOW_DAYS = {1: 7, 7: 14, 30: 30, 0: 30}


def normalize_window(days: Any) -> int:
    """Clamp a requested window to a choice the aggregation understands."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return 7
    return days if days in WINDOW_CHOICES_DAYS else 7


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _within(ts: float, cutoff: float | None) -> bool:
    """Strictly inside the window: the instant the window opens is excluded.

    With the cutoff anchored to the data's newest timestamp, a goal sitting
    exactly `window_days` before that anchor is the boundary case; excluding it
    keeps "7 days" meaning *fewer than* 7 days old, and the comparison is
    deterministic where an inclusive boundary would be arbitrary.
    """
    return cutoff is None or ts > cutoff


def _avg(values: list[float]) -> int | None:
    return round(sum(values) / len(values)) if values else None


def _empty_bucket() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
        "durations": [],
    }


def _finish(bucket: dict[str, Any]) -> dict[str, Any]:
    durations = bucket.pop("durations")
    return {
        **bucket,
        # None on purpose: "no duration was recorded" is not "0ms".
        "avg_duration_ms": _avg(durations),
    }


def summarize_goals(goals: list[dict[str, Any]], window_days: int = 0, *, now: float | None = None) -> dict[str, Any]:
    """Goal outcomes, totals, and success rate for the window.

    The rate is terminal goals only: an active goal has not happened yet, and
    counting it as anything would make the number move for reasons that have
    nothing to do with whether goals succeed.
    """
    if now is not None:
        # Live endpoint: anchor to wall-clock so a 60-day-idle install asking
        # window=7 sees an empty window, not ancient data labelled "last 7 days".
        cutoff = None if window_days <= 0 else now - window_days * 86400
    else:
        # Pure/deterministic default: anchor to the data's newest timestamp so
        # tests construct timelines and get stable windows with no clock.
        cutoff = None if window_days <= 0 else _nowish(goals) - window_days * 86400
    in_window = [g for g in goals if _within(g.get("created_at") or 0.0, cutoff)]

    totals = {
        "goals": len(in_window),
        "active": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
    }
    for g in in_window:
        status = g.get("status")
        if status in ACTIVE_STATUSES:
            totals["active"] += 1
        elif status == "COMPLETED":
            totals["succeeded"] += 1
        elif status == "FAILED":
            totals["failed"] += 1
        elif status == "CANCELLED":
            totals["cancelled"] += 1

    terminal = totals["succeeded"] + totals["failed"] + totals["cancelled"]
    return {
        **totals,
        "success_rate": round(100 * totals["succeeded"] / terminal) if terminal else None,
    }


def _nowish(goals: list[dict[str, Any]]) -> float:
    """The newest goal timestamp in the data — the no-clock fallback anchor.

    This is what a caller gets when it does not pass `now`, and it exists for
    tests that build a synthetic timeline and want a deterministic window. It
    is **not** a safe default for a real request: the newest goal is recent
    only if something ran recently, and when nothing has, this anchor slides
    the window forward until the stale data lands inside it. A 60-day-idle
    install asking for 7 days would then be shown its last run as "this week"
    with a 100% success rate — the false claim this fallback used to make
    silently.

    Every live path passes `now` (see `stats_overview`), so the honest anchor
    is the one in production. Callers serving a real user should always pass it.
    """
    stamps = [g.get("created_at") or 0.0 for g in goals]
    return max(stamps) if stamps else 0.0


def summarize_usage(events: list[dict[str, Any]], window_days: int = 0, *, now: float | None = None) -> dict[str, Any]:
    """Token spend and call outcomes for the window, per role and per model.

    Expects one dict per `usage` or `agent_call_failed` event, already parsed:
    `{type, timestamp, payload}`. The payload schema is what the orchestrator
    writes — role, provider, model, the token fields, and duration_ms when the
    engine that wrote it measured one.
    """
    if now is not None:
        cutoff = None if window_days <= 0 else now - window_days * 86400
    else:
        cutoff = None if window_days <= 0 else _nowish_events(events) - window_days * 86400
    # Counters and a list in one mapping: without the annotation mypy reads the
    # literal as dict[str, object] and every `totals[...] += 1` becomes an error.
    totals: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
        "failures": 0,
        "durations": [],
    }
    by_role: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}

    for ev in events:
        if not _within(ev.get("timestamp") or 0.0, cutoff):
            continue
        p = ev.get("payload") or {}
        is_failure = ev.get("type") == "agent_call_failed"
        if ev.get("type") not in ("usage", "agent_call_failed"):
            continue
        role = p.get("role") or "unknown"
        model = f"{p.get('provider') or '?'}/{p.get('model') or '?'}"
        if is_failure:
            totals["failures"] += 1
            for b in (by_role.setdefault(role, _empty_bucket()),
                      by_model.setdefault(model, _empty_bucket())):
                b["calls"] += 1
            continue

        i = p.get("input_tokens") or 0
        o = p.get("output_tokens") or 0
        d = p.get("duration_ms")
        totals["input_tokens"] += i
        totals["output_tokens"] += o
        totals["total_tokens"] += i + o
        totals["calls"] += 1
        if isinstance(d, (int, float)):
            totals["durations"].append(d)
        for b in (by_role.setdefault(role, _empty_bucket()),
                  by_model.setdefault(model, _empty_bucket())):
            b["input_tokens"] += i
            b["output_tokens"] += o
            b["total_tokens"] += i + o
            b["calls"] += 1
            if isinstance(d, (int, float)):
                b["durations"].append(d)

    out_totals = _finish(totals)
    return {
        **out_totals,
        "by_role": {k: _finish(v) for k, v in sorted(by_role.items())},
        "by_model": {k: _finish(v) for k, v in sorted(by_model.items())},
    }


def _nowish_events(events: list[dict[str, Any]]) -> float:
    """The no-clock fallback anchor for spend — `_nowish` for the event log.

    Same caveat and same rule: live callers pass `now`; this is for tests
    building a synthetic timeline.
    """
    stamps = [ev.get("timestamp") or 0.0 for ev in events]
    return max(stamps) if stamps else 0.0


def daily_trend(goals: list[dict[str, Any]], events: list[dict[str, Any]], window_days: int = 0, *, now: float | None = None) -> list[dict[str, Any]]:
    """Per-day rows for the chart: goals started, outcomes, spend.

    Days are UTC. A goal is counted on the day it was created and, separately,
    on the day it last changed status — so a goal that ran across midnight
    contributes its creation to one row and its outcome to another, which is
    the truth of when each thing happened. Sparse by design: days with no
    activity produce no row.
    """
    # The chart covers the window's chart span ("all time" charts the last 30
    # days; see DAILY_WINDOW_DAYS). Goals and usage events share one cutoff so a
    # day-row can never mix a goal count from a window its spend is excluded
    # from — the two series must describe the same days.
    chart_days = DAILY_WINDOW_DAYS.get(window_days, 30)
    if now is not None:
        cutoff = now - chart_days * 86400
    else:
        cutoff = _nowish(goals) - chart_days * 86400
    rows: dict[str, dict[str, Any]] = {}

    def row(day: str) -> dict[str, Any]:
        return rows.setdefault(
            day,
            {"date": day, "created": 0, "succeeded": 0, "failed": 0, "cancelled": 0,
             "total_tokens": 0, "calls": 0},
        )

    outcome_key = {"COMPLETED": "succeeded", "FAILED": "failed", "CANCELLED": "cancelled"}
    for g in goals:
        created = g.get("created_at") or 0.0
        if _within(created, cutoff):
            row(_day(created))["created"] += 1
        status = g.get("status")
        if status in outcome_key:
            ended = g.get("updated_at") or created
            if _within(ended, cutoff):
                row(_day(ended))[outcome_key[status]] += 1

    for ev in events:
        if ev.get("type") != "usage":
            continue
        ts = ev.get("timestamp") or 0.0
        if not _within(ts, cutoff):
            continue
        p = ev.get("payload") or {}
        tokens = (p.get("input_tokens") or 0) + (p.get("output_tokens") or 0)
        r = row(_day(ts))
        r["total_tokens"] += tokens
        r["calls"] += 1

    return [rows[day] for day in sorted(rows)]


def build_overview(goals: list[dict[str, Any]], events: list[dict[str, Any]], window_days: int = 0, *, now: float | None = None) -> dict[str, Any]:
    """The whole cross-goal document the stats view renders."""
    return {
        "window_days": window_days,
        "goals": summarize_goals(goals, window_days, now=now),
        "usage": summarize_usage(events, window_days, now=now),
        "daily": daily_trend(goals, events, window_days, now=now),
    }
