"""`audit_usage`: where this workspace's model calls were spent on work that had to be done again, in tokens.

The method is borrowed from Xerg's "find wasted AI spend" skill (`docs/15`) and nothing else of it is: not its CLI, its
pricing catalogue, its hosted features or its code. Two of its rules matter more than its detectors and are the spine of this
module:

* **What was not seen comes before what was found.** The report opens with a coverage block (how many events were read, whether
  that was the cap, which detectors could not run and why), because a clean-looking result is exactly the one a reader must be able
  to check against what it could not see. A result with no findings never says "no waste".
* **A finding needs evidence of the whole chain.** A re-ask is called *waste* only when the step then went on to succeed. One that
  ended in a fallback or an error is spend the retry did not recover, reported but not classified; one with nothing after it yet
  is unfinished work and is left out of every rate (`engine/metrics.py` rule 1).

**Tokens are tokens.** There is no price table and no currency here, as in `engine/metrics.py` and `docs/04` §4.7: a price that
silently stops matching the provider is worse than no price.

What the engine records decides what can be measured (`docs/15` §3), and the report says so instead of pretending:

* *Retry waste* (a same-model re-ask, and a fix-loop attempt a later retry got past): measurable.
* *Tool-loop waste* and *cache churn*: **not measurable today.** Tool results are never persisted and no provider adapter parses
  cache tokens, so those two are reported as unavailable, with the capture that would enable them.
* *Signals* (context growth, context outliers, deep conductor loops, re-ask rate, token concentration): descriptive. They carry the
  numbers they came from, are never classified as waste, and never recommend a change.

Pure functions over parsed event dicts (`{type, payload, timestamp, goal_id, step_id, sequence}`), the shape `engine/metrics.py`
takes, so the arithmetic is tested without a server. It reads nothing, writes nothing and starts nothing.
"""

from __future__ import annotations

from typing import Any

from engine.metrics import (
    STAGE_SUCCESS_OUTCOMES,
    _window_cutoff,
    _within,
    percentile,
    recovered_steps,
)

#: The event types the audit reads. `test_result` and `step_status` are what `metrics.recovered_steps` decides "got past it" from,
#: so the fix-loop answer is the one the statistics screen shows.
AUDIT_EVENT_TYPES: tuple[str, ...] = (
    "usage", "agent_call_failed", "stage_result", "fix_retry", "test_result", "step_status", "error",
    "provider_fallback", "agent_assigned",
)

#: The most events one audit reads (newest first), the same bound the stats screens use; reaching it is reported.
MAX_AUDIT_EVENTS = 20_000

#: The report must reach the model whole: the conductor cuts any tool result over 12,000 characters (`conductor.MAX_TOOL_RESULT_CHARS`)
#: to its first two thirds. A test holds this below that cap.
REPORT_CHARS = 11_000

# Thresholds for the descriptive signals. They only decide whether a signal is *shown*; each shown signal carries its own numbers.
MIN_CALLS_FOR_OUTLIER = 8       # fewer calls than this say nothing about a spread
OUTLIER_P90_OVER_MEDIAN = 3     # p90 of a role's input tokens at least this many times its median
OUTLIER_MIN_EXCESS = 2_000      # ... and at least this many tokens above it
MIN_LOOP_CALLS = 4              # a conductor loop shorter than this has no growth to speak of
GROWTH_FACTOR = 3               # last call's input at least this many times the first's
DEEP_LOOP_SHARE = 0.8           # a loop that used this share of the turn cap
REASK_MIN = 2                   # re-asks needed before a rate is a signal
REASK_RATE = 0.2                # ... and the share of that role/model's calls
CONCENTRATION_SHARE = 0.6       # one role holding at least this share of all tokens
EXAMPLES = 5                    # worked examples listed per finding
MAX_SIGNAL_LINES = 12           # signal lines shown; the rest are counted, never silently dropped
DEFAULT_TURN_CAP = 14           # `conductor_max_turns`' default; the tool passes the live setting

UNAVAILABLE: dict[str, str] = {
    "tool-loop-waste": (
        "tool results are never stored and arguments survive only as a clipped log line, so an identical repeated call with an "
        "identical result cannot be shown. Enabling it would mean recording a hash of each call's arguments and result."
    ),
    "cache-churn": (
        "no provider adapter reads prompt-cache token counts and no request asks for caching, so there is no cache lifecycle to "
        "price. Enabling it would mean capturing cache read and write tokens."
    ),
}


def _int(value: Any) -> int:
    """A token count from a payload, 0 for anything that is not a plain non-negative integer."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _tokens(payload: dict[str, Any]) -> tuple[int, int]:
    return _int(payload.get("input_tokens")), _int(payload.get("output_tokens"))


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("payload")
    return raw if isinstance(raw, dict) else {}


def _by_goal(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Events per goal, in the order they were published. A goal's `sequence` is its order; timestamps tie across goals."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(str(event.get("goal_id") or ""), []).append(event)
    for goal_events in grouped.values():
        goal_events.sort(key=lambda e: (e.get("sequence") or 0, e.get("timestamp") or 0.0))
    return grouped


def _outcome_after(goal_events: list[dict[str, Any]], at: int, step_id: Any, role: str) -> str:
    """What became of a re-ask: `recovered`, `unrecovered`, or `unknown` when nothing has followed yet.

    The first event after the marker that settles this role's work on this step decides it: a normal `stage_result` means the
    re-asked reply was usable; a fallback or an error means it was not; a `stage_result` outcome the stage does not count as doing
    its job means it was not. Events about other roles or steps are skipped, not read as an answer.
    """
    for event in goal_events[at + 1:]:
        payload = _payload(event)
        kind = event.get("type")
        if kind == "provider_fallback" and payload.get("role") == role and event.get("step_id") == step_id:
            return "unrecovered"
        if kind == "error" and event.get("step_id") == step_id:
            return "unrecovered"
        if kind == "stage_result" and payload.get("role") == role and event.get("step_id") == step_id:
            ok = STAGE_SUCCESS_OUTCOMES.get(str(payload.get("stage")), frozenset())
            return "recovered" if payload.get("outcome") in ok else "unrecovered"
    return "unknown"


def _reasks(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Every same-model re-ask: the call that was wasted, its tokens, and how it ended. Also how many had no `usage` to price."""
    found: list[dict[str, Any]] = []
    unpriced = 0
    for goal_id, goal_events in _by_goal(events).items():
        last_usage: dict[tuple[Any, str], dict[str, Any]] = {}
        for at, event in enumerate(goal_events):
            payload = _payload(event)
            key = (event.get("step_id"), str(payload.get("role") or ""))
            if event.get("type") == "usage":
                last_usage[key] = event
            elif event.get("type") == "agent_call_failed" and payload.get("retrying") is True:
                call = last_usage.get(key)
                if call is None:
                    unpriced += 1
                    continue
                tokens_in, tokens_out = _tokens(_payload(call))
                found.append({
                    "goal_id": goal_id,
                    "step_id": event.get("step_id"),
                    "role": key[1],
                    "provider": str(payload.get("provider") or ""),
                    "model": str(payload.get("model") or ""),
                    "tokens": tokens_in + tokens_out,
                    "outcome": _outcome_after(goal_events, at, event.get("step_id"), key[1]),
                })
    return found, unpriced


def _fix_loops(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fix attempts a later retry got past: the fixer and verifier tokens spent before the step's last `fix_retry`.

    Which steps were retried and then got past it is `metrics.recovered_steps`, so this agrees with the statistics screen. Only
    attempts *before* the last retry are counted; the attempt that worked is the work, not the waste.
    """
    recovered = recovered_steps(events)
    out: list[dict[str, Any]] = []
    for goal_id, goal_events in _by_goal(events).items():
        last_retry: dict[Any, int] = {}
        for event in goal_events:
            if event.get("type") == "fix_retry":
                last_retry[event.get("step_id")] = int(event.get("sequence") or 0)
        for step_id, until in last_retry.items():
            if (goal_id, str(step_id)) not in recovered:
                continue
            spent = [
                _int(_payload(e).get("tokens"))
                for e in goal_events
                if e.get("type") == "stage_result" and e.get("step_id") == step_id
                and _payload(e).get("role") in ("fixer", "verifier") and int(e.get("sequence") or 0) < until
            ]
            if spent:
                out.append({"goal_id": goal_id, "step_id": step_id, "attempts": len(spent), "tokens": sum(spent)})
    return out


def _share(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole > 0 else 0.0


def _finding_retry(reasks: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = {"recovered": 0, "unrecovered": 0, "unknown": 0}
    tokens = {"recovered": 0, "unrecovered": 0, "unknown": 0}
    by_role: dict[str, int] = {}
    for item in reasks:
        outcomes[item["outcome"]] += 1
        tokens[item["outcome"]] += item["tokens"]
        if item["outcome"] == "recovered":
            by_role[item["role"]] = by_role.get(item["role"], 0) + item["tokens"]
    examples = sorted((r for r in reasks if r["outcome"] == "recovered"), key=lambda r: -r["tokens"])[:EXAMPLES]
    return {
        "id": "retry-waste",
        "assessed": len(reasks),
        "wasted_tokens": tokens["recovered"],
        "outcomes": outcomes,
        "tokens_by_outcome": tokens,
        "by_role": sorted(by_role.items(), key=lambda kv: -kv[1]),
        "examples": examples,
    }


def _loops(events: list[dict[str, Any]], turn_cap: int) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Conductor loops: how many there were, which grew their prompt a lot, and which used most of the turn cap.

    A loop is the run of conductor `usage` events after one `agent_assigned{conductor: true}` marker in a goal.
    """
    seen = 0
    growth: list[dict[str, Any]] = []
    deep: list[dict[str, Any]] = []

    def close(goal_id: str, sizes: list[int]) -> None:
        nonlocal seen
        if not sizes:
            return
        seen += 1
        if len(sizes) >= MIN_LOOP_CALLS and sizes[0] > 0 and sizes[-1] >= sizes[0] * GROWTH_FACTOR:
            growth.append({"goal_id": goal_id, "calls": len(sizes), "first_input": sizes[0], "last_input": sizes[-1]})
        if turn_cap > 0 and len(sizes) >= turn_cap * DEEP_LOOP_SHARE:
            deep.append({"goal_id": goal_id, "calls": len(sizes), "turn_cap": turn_cap})

    for goal_id, goal_events in _by_goal(events).items():
        sizes: list[int] = []
        for event in goal_events:
            payload = _payload(event)
            if event.get("type") == "agent_assigned" and payload.get("conductor") is True:
                close(goal_id, sizes)
                sizes = []
            elif event.get("type") == "usage" and payload.get("role") == "conductor":
                sizes.append(_tokens(payload)[0])
        close(goal_id, sizes)
    return seen, growth, deep


def _signals(
    events: list[dict[str, Any]], reasks: list[dict[str, Any]], calls: dict[str, list[int]], turn_cap: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    # Context outliers: a role whose largest prompts dwarf its usual ones.
    for role in sorted(calls):
        sizes = [float(n) for n in calls[role]]
        if len(sizes) < MIN_CALLS_FOR_OUTLIER:
            continue
        median, p90 = percentile(sizes, 50), percentile(sizes, 90)
        if median and p90 and p90 >= median * OUTLIER_P90_OVER_MEDIAN and p90 - median >= OUTLIER_MIN_EXCESS:
            out.append({"id": "context-outlier", "role": role, "calls": len(sizes), "median_input": median, "p90_input": p90})

    seen, growth, deep = _loops(events, turn_cap)
    if growth:
        out.append({"id": "context-growth", "loops": len(growth), "of": seen,
                    "examples": sorted(growth, key=lambda g: -g["last_input"])[:EXAMPLES]})
    if deep:
        out.append({"id": "deep-conductor-loop", "loops": len(deep), "of": seen,
                    "examples": sorted(deep, key=lambda g: -g["calls"])[:EXAMPLES]})

    # Re-ask rate per role and model, with the counts it came from.
    asked: dict[tuple[str, str], int] = {}
    for item in reasks:
        asked[(item["role"], item["model"])] = asked.get((item["role"], item["model"]), 0) + 1
    for (role, model), n in sorted(asked.items()):
        total_calls = len(calls.get(role, []))
        if n >= REASK_MIN and total_calls and n / total_calls >= REASK_RATE:
            out.append({"id": "reask-rate", "role": role, "model": model, "reasks": n, "calls": total_calls,
                        "rate": round(n / total_calls, 3)})
    return out


def audit(
    events: list[dict[str, Any]],
    window_days: int = 0,
    *,
    now: float | None = None,
    turn_cap: int = DEFAULT_TURN_CAP,
    limit: int = MAX_AUDIT_EVENTS,
) -> dict[str, Any]:
    """The audit of `events` (already read, already workspace-scoped), as a plain dict. See the module docstring for what it means."""
    cutoff = _window_cutoff(window_days, [float(e.get("timestamp") or 0.0) for e in events], now)
    # Chronological, whatever order the caller read them in: `metrics.recovered_steps` decides "got past it" by what came first.
    inside = sorted(
        (e for e in events if _within(float(e.get("timestamp") or 0.0), cutoff)),
        key=lambda e: (float(e.get("timestamp") or 0.0), int(e.get("sequence") or 0)),
    )

    calls: dict[str, list[int]] = {}
    totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    role_tokens: dict[str, int] = {}
    failed_without_tokens = 0
    for event in inside:
        payload = _payload(event)
        if event.get("type") == "usage":
            tokens_in, tokens_out = _tokens(payload)
            role = str(payload.get("role") or "")
            calls.setdefault(role, []).append(tokens_in)
            totals["calls"] += 1
            totals["input_tokens"] += tokens_in
            totals["output_tokens"] += tokens_out
            role_tokens[role] = role_tokens.get(role, 0) + tokens_in + tokens_out
        elif event.get("type") == "agent_call_failed" and payload.get("retrying") is not True:
            failed_without_tokens += 1
    total = totals["input_tokens"] + totals["output_tokens"]

    reasks, unpriced = _reasks(inside)
    retry = _finding_retry(reasks)
    fix_loops = _fix_loops(inside)
    findings = [
        retry,
        {
            "id": "fix-loop-retry-waste",
            "assessed": len(fix_loops),
            "wasted_tokens": sum(f["tokens"] for f in fix_loops),
            "examples": sorted(fix_loops, key=lambda f: -f["tokens"])[:EXAMPLES],
        },
    ]
    signals = _signals(inside, reasks, calls, turn_cap)
    if total > 0:
        top_role, top_tokens = max(role_tokens.items(), key=lambda kv: kv[1])
        if _share(top_tokens, total) >= CONCENTRATION_SHARE:
            signals.append({"id": "token-concentration", "role": top_role, "tokens": top_tokens, "share": _share(top_tokens, total)})

    stamps = [float(e.get("timestamp") or 0.0) for e in inside if e.get("timestamp")]
    return {
        "coverage": {
            "events_read": len(inside),
            "event_cap": limit,
            "truncated": len(events) >= limit,
            "window_days": window_days,
            "goals": len({e.get("goal_id") for e in inside}),
            "span_days": round((max(stamps) - min(stamps)) / 86400, 1) if len(stamps) > 1 else 0.0,
            "failed_calls_without_tokens": failed_without_tokens,
            "reasks_without_usage": unpriced,
            "unavailable": dict(UNAVAILABLE),
        },
        "totals": {**totals, "total_tokens": total, "by_role": sorted(role_tokens.items(), key=lambda kv: -kv[1])},
        "findings": findings,
        "signals": signals,
    }


def _n(value: int) -> str:
    return f"{value:,}"


def _text(value: Any, limit: int = 60) -> str:
    """A name from a payload, made safe to print: printable characters only, clipped. Roles are fixed; models are not."""
    clean = "".join(ch for ch in str(value) if ch.isprintable())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _step(value: Any) -> str:
    return _text(value, 8) if value else "-"


def format_audit(result: dict[str, Any]) -> str:
    """The audit as the text a model reads: what was not seen first, then what was found, then the signals, within `REPORT_CHARS`."""
    cov, totals = result["coverage"], result["totals"]
    window = f"the last {cov['window_days']} days" if cov["window_days"] else "all recorded time"
    lines = [
        f"Usage audit for this workspace over {window}, in TOKENS (this engine keeps no prices, so no dollar figure is given or implied). "
        "A finding is a pattern in recorded events, not a verdict; read the evidence before recommending anything.",
        "",
        "Coverage — what this could and could not see:",
        f"- {_n(cov['events_read'])} events from {cov['goals']} goal(s) over {cov['span_days']} days"
        + (f"; the newest {_n(cov['event_cap'])} were read and older ones were NOT" if cov["truncated"] else "; every matching event was read"),
        f"- {_n(cov['failed_calls_without_tokens'])} failed model calls booked no tokens (a provider error returns no usage), so they are not in any total",
    ]
    if cov["reasks_without_usage"]:
        lines.append(f"- {_n(cov['reasks_without_usage'])} re-asks had no recorded usage to measure and are not counted")
    lines.append("- HTTP-level retries inside one call are not recorded; only a same-model re-ask and the fix loop are visible")
    for name, why in cov["unavailable"].items():
        lines.append(f"- {name}: UNAVAILABLE — {why}")
    lines += [
        "",
        f"Total recorded: {_n(totals['calls'])} calls, {_n(totals['total_tokens'])} tokens "
        f"({_n(totals['input_tokens'])} in, {_n(totals['output_tokens'])} out).",
    ]
    if totals["by_role"]:
        shown = ", ".join(f"{role or '?'} {_n(tokens)}" for role, tokens in totals["by_role"][:6])
        lines.append(f"By role: {shown}.")

    by_id = {f["id"]: f for f in result["findings"]}
    retry, fix = by_id["retry-waste"], by_id["fix-loop-retry-waste"]
    lines += ["", "Findings:"]
    if retry["assessed"]:
        o, t = retry["outcomes"], retry["tokens_by_outcome"]
        lines.append(
            f"- retry-waste: {_n(retry['wasted_tokens'])} tokens in {o['recovered']} re-asked call(s) whose step then succeeded. "
            f"Also {o['unrecovered']} re-ask(s) that did NOT recover ({_n(t['unrecovered'])} tokens: spend, not classed as waste) and "
            f"{o['unknown']} with no outcome yet ({_n(t['unknown'])} tokens: left out of any rate)."
        )
        if retry["by_role"]:
            lines.append("  by role: " + ", ".join(f"{role} {_n(tokens)}" for role, tokens in retry["by_role"][:6]))
        for ex in retry["examples"]:
            lines.append(f"  e.g. goal {_text(ex['goal_id'], 8)} step {_step(ex['step_id'])} {_text(ex['role'])} ({_text(ex['model']) or 'model unknown'}): {_n(ex['tokens'])} tokens")
    else:
        lines.append("- retry-waste: no same-model re-ask was recorded in this window. That says nothing about the detectors marked UNAVAILABLE above.")
    if fix["assessed"]:
        lines.append(f"- fix-loop-retry-waste: {_n(fix['wasted_tokens'])} tokens in fixer and verifier attempts that a later retry got past, across {fix['assessed']} step(s).")
        for ex in fix["examples"]:
            lines.append(f"  e.g. goal {_text(ex['goal_id'], 8)} step {_step(ex['step_id'])}: {ex['attempts']} earlier attempt(s), {_n(ex['tokens'])} tokens")
    else:
        lines.append("- fix-loop-retry-waste: no fix attempt that a retry then got past was recorded in this window.")

    lines += ["", "Signals (descriptive; none of these is classed as waste or recommends a change):"]
    if not result["signals"]:
        lines.append("- none above their thresholds in this window.")
    for sig in result["signals"][:MAX_SIGNAL_LINES]:
        kind = sig["id"]
        if kind == "context-outlier":
            lines.append(f"- context-outlier: {_text(sig['role'])}'s largest prompts (p90 {_n(sig['p90_input'])} tokens) are far above its usual (median {_n(sig['median_input'])}) over {sig['calls']} calls")
        elif kind == "context-growth":
            lines.append(f"- context-growth: {sig['loops']} of {sig['of']} conductor loops ended with a prompt at least {GROWTH_FACTOR}x the size of their first")
        elif kind == "deep-conductor-loop":
            lines.append(f"- deep-conductor-loop: {sig['loops']} of {sig['of']} conductor loops used at least {int(DEEP_LOOP_SHARE * 100)}% of the turn cap")
        elif kind == "reask-rate":
            lines.append(f"- reask-rate: {_text(sig['role'])} on {_text(sig['model']) or 'an unknown model'} was re-asked on {sig['reasks']} of {sig['calls']} recorded calls ({int(sig['rate'] * 100)}%)")
        elif kind == "token-concentration":
            lines.append(f"- token-concentration: {_text(sig['role'])} holds {int(sig['share'] * 100)}% of all recorded tokens ({_n(sig['tokens'])})")
    if len(result["signals"]) > MAX_SIGNAL_LINES:
        lines.append(f"- ({len(result['signals']) - MAX_SIGNAL_LINES} more signal(s) not shown, to keep this result whole; ask for a shorter window.)")
    lines += ["", "Next: to check a finding, look at its steps with `recall` before saying what to change; changing settings is the person's act."]

    return "\n".join(lines)
