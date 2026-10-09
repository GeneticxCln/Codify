"""What this repository has already taught us.

Codify records every stage outcome, every failure classification, every retry
and every recovery in `events` — and then uses them for one thing: a
statistics screen. `engine/metrics.py` turns them into dashboards and
`engine/stats_history.py` freezes a day's document, and no model ever sees a
byte of it. A turn therefore starts from the user's prompt and the files on
disk, every single time, having learned nothing from the last four hundred
runs in this repository.

That is the gap this module closes, and it is worth being precise about what
it does and does not do. It does not add memory *storage* — the rows are
already there, written for the stats screen and kept for it. It adds
**retrieval**: a way for the conductor to ask a question of that history and
get specific past events back, rather than a rollup.

The distinction matters because it decides the shape of the answer.
`metrics.failure_breakdown` answers "what fails here and how often", which is
the dashboard's question. Recall answers "**has this specific thing happened
before, and did we ever get past it**", which is the question a model is
actually stuck on. The second needs the individual events, their order, and
whether a retry that followed them worked — none of which survive being
aggregated. So this returns events; `metrics` keeps doing aggregates.

## Where the halves live

`GoalService` owns the `events` table and already queries it, so the SQL and
the workspace scoping are a method there (`recall_events`). This module is the
part that decides what a remembered thing *is*: which fields may be read, how
a row becomes a fixed shape, and what the model is told it is holding. Both
halves are there because either alone is untestable — the projection is pure
and can be driven from plain dicts, and the query can be driven from a real
database.

## The retrieval is bounded, and so is the answer

Four caps, each for its own reason and none of them interchangeable:

- **Scanning is capped** (`MAX_SCAN_EVENTS`, applied by the query). A question
  is asked on the hot path of a turn, and a workspace with 200k events must
  not turn "what broke last week?" into a table scan of everything recorded.
- **A window is optional and defaults to all time.** A model asking about "last
  week's failures" should be able to say so, and one asking about a bug report
  from six months ago should still find it.
- **Matches are capped** (`MAX_MATCHES`), newest first. Twenty recent events is
  already more than fits a turn's budget once each carries a message.
- **Every field is clipped**, and only fields on the allow-list are read at all.

## Why an allow-list of event types, and not a payload search

This is the part that decides whether the tool is safe to offer a model that
reads hostile text all day.

`events.payload` holds whatever the engine put there, and some of it is
untrusted in exactly the way a file in a cloned repository is: a `diff` is file
content, `library_evidence` is text the librarian read from disk. If recall
searched payloads and returned them, it would be a channel that puts stored
third-party text into the model's context with less ceremony than `read_file` —
and a channel that *looks* like memory, so the model would weight it as its
own recollection rather than as a quote.

So recall reads **named fields from named event types** ([`RECALLABLE`]) and
projects them into a fixed return shape. A `diff` cannot appear. A page's text
cannot appear. What can appear is what the engine itself classified: a stage,
an outcome, a failure code, a step title, a message the engine wrote about a
call that failed. The allow-list is the security boundary, and it is the same
shape as `_clean_result` in `engine/webview_bridge.py` — a return type this
file decides, which no stored row can widen by naming another key.

It is defence in depth over something already true: recall grants nothing that
`read_file` and `git_history` do not already grant, because every row it
returns came from this machine's own record of running this pipeline. The risk
being managed is not access, it is *salience* — an injected string arriving
labelled "past outcome" is more persuasive than the same string arriving
labelled "a file you just read". [`format_recall`] says what the model is
holding for exactly that reason.
"""

from __future__ import annotations

import json
import time
from typing import Any

#: The event types recall will read, and the payload fields it may read from
#: each. Everything absent from this table is invisible to recall — including
#: `diff` and `library_evidence`, which is the point. See the module docstring.
RECALLABLE: dict[str, tuple[str, ...]] = {
    "error": ("code", "message"),
    "agent_call_failed": ("role", "code", "message"),
    "agent_output_invalid": ("role", "message"),
    "agent_not_configured": ("role", "message"),
    "stage_result": ("stage", "role", "outcome"),
    "test_result": ("verdict",),
    "step_status": ("step_title", "status"),
    "goal_status": ("status",),
    # Fetched for the recovery pairing and never matched on: a bare retry
    # carries no field to search, and it is not a thing anybody would ask to
    # see. It has to be in this table anyway, because the query filters by it
    # — leaving it out means the scan never fetches the retry that proves a
    # failure was recovered from, and every past failure reads as permanent.
    # That is exactly the bug the allow-list test caught.
    "fix_retry": (),
    # Progress lines are deliberately not recalled: they are the chattiest
    # prose in the log and the least likely to answer "what broke". Only the
    # two levels that describe something going wrong, and only because a log
    # line is where a refusal usually lands with its reason attached.
    "log": ("message",),
}

#: `log` rows below this level are skipped. See the entry above.
LOG_LEVELS = ("warn", "error")

#: How many events one question may read. A turn asks this on the hot path and
#: a workspace with a long history must not turn a question into a full scan.
MAX_SCAN_EVENTS = 2_000

#: How many matches come back. Twenty is already more than fits a turn's
#: budget once each carries a message, and a longer list reads as thorough
#: while being read by nobody past the first few.
MAX_MATCHES = 20

#: The longest any one recalled field may be. A message is a sentence, and a
#: blob that reached a `message` field would otherwise spend the whole window
#: on the one row that matched.
MAX_FIELD_CHARS = 300

#: A query shorter than this matches nothing. It is not a broad search: recall
#: is for a specific thing, and `failure_breakdown` already answers "what
#: fails here" for the broad case.
MIN_QUERY_CHARS = 3

# ── the thread grain ──────────────────────────────────────────────────────────
#
# `recall` reads events: what happened inside a run. The tool beside it reads
# the other unit of history this database already keeps — the conversation. A
# new thread's conductor can then learn that three earlier threads worked this
# same workspace and how their runs ended, without any of it being re-derived
# from step outcomes one event at a time.

#: How many threads one answer may name. A thread summary is a sentence; five
#: recent ones is what fits a turn's budget, and the recency order means the
#: cut falls on the oldest — the least likely to still describe this code.
MAX_THREADS = 5

#: How many of a thread's own asks (goal descriptions, newest first) are shown
#: under its name. One is usually the whole story; two distinguish a thread
#: that drifted from one that stayed on a question.
MAX_ASKS = 2

#: The longest one ask may be, clipped by the query (so an oversized prompt is
#: never even read whole) and again here for a row written before that clip.
MAX_ASK_CHARS = 120

#: The longest the asks line may be in total — the summary stays a summary.
MAX_ASKS_CHARS = 240

#: How many observations the brief may carry. Proof counts make lines
#: compelling, which is exactly why they are capped: five is what a turn's
#: opening prompt can absorb without crowding out the request itself.
MAX_BRIEF_OBSERVATIONS = 5


def project(row: dict[str, Any], needle: str) -> dict[str, Any] | None:
    """One event row as a fixed set of capped strings, or None if it misses.

    The projection is the security property, so it is worth reading twice: a
    row matches when a field **on the allow-list** contains the query, and
    nothing else about the row is read. A payload with a `diff` in it is
    scanned for its `stage`, and the diff is never touched.
    """
    kind = str(row.get("type") or "")
    fields = RECALLABLE.get(kind)
    if fields is None:
        return None
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    values: dict[str, str] = {}
    for field in fields:
        raw = payload.get(field)
        text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
        text = text[:MAX_FIELD_CHARS]
        if text:
            values[field] = text
    if kind == "log":
        # The level gate lives here rather than in SQL so that a `log` row at
        # `info` cannot be recalled by its message — the allow-list says which
        # rows exist, this says which of them mean anything.
        level = str(payload.get("level") or "").strip().lower()
        if level not in LOG_LEVELS:
            return None
        values["level"] = level

    haystack = " ".join(values.values()).lower()
    title = str(row.get("goal_title") or "")
    if needle not in haystack and needle not in title.lower():
        return None

    return {
        "goal_id": str(row.get("goal_id") or ""),
        "goal_title": title[:MAX_FIELD_CHARS],
        "step_id": str(row.get("step_id") or ""),
        "type": kind,
        "when": float(row.get("timestamp") or 0.0),
        "fields": values,
    }


def recovered_pairs(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """The (goal, step) pairs a retry got past, decided from these rows.

    Delegates to `engine.metrics` rather than pairing events here, because
    `_recovery`'s whole argument is about **order** — a finish only counts when
    its step had already been retried by the time it arrived — and a second
    implementation would be a second answer to "was this ever recovered" that
    could disagree with the statistics screen.

    It is computed over **every scanned row**, not over the matches. A
    `fix_retry` that arrived after the matching failure is the evidence that
    matters, and it is not itself a match — scoring recovery from the matching
    rows alone would report every known failure as unrecovered.
    """
    from engine.metrics import recovered_steps

    # `_recovery`'s other caller hands it events whose payload is already a
    # dict, because `_sweep_metrics` parses on the way out of the database.
    # These rows come straight off `recall_events` with the JSON still a
    # string, so it is parsed here rather than by loosening `_recovery` to
    # accept both — one caller dealing with both shapes is how a metrics
    # function starts raising `AttributeError` on a row nobody tested.
    normalised: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw = item.get("payload")
        if isinstance(raw, str):
            try:
                item["payload"] = json.loads(raw or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
        normalised.append(item)
    return recovered_steps(normalised)


def search(
    rows: list[dict[str, Any]], query: str, *, limit: int = 8
) -> dict[str, Any]:
    """Past events matching a query, newest first, with recovery marked.

    The caller has already scoped the rows to one workspace and applied the
    scan cap; this is the part that decides what comes back.
    """
    needle = (query or "").strip().lower()
    if len(needle) < MIN_QUERY_CHARS:
        return {
            "query": (query or "").strip(),
            "scanned": 0,
            "matched": 0,
            "recovered": 0,
            "goals": 0,
            "matches": [],
            "too_short": True,
        }

    recovered = recovered_pairs(rows)
    matches: list[dict[str, Any]] = []
    for row in rows:
        projected = project(row, needle)
        if projected is None:
            continue
        key = (projected["goal_id"], projected["step_id"])
        projected["recovered"] = key in recovered
        matches.append(projected)
        if len(matches) >= max(1, min(int(limit or 1), MAX_MATCHES)):
            break

    return {
        "query": (query or "").strip(),
        "scanned": len(rows),
        "matched": len(matches),
        "recovered": sum(1 for m in matches if m["recovered"]),
        "goals": len({m["goal_id"] for m in matches}),
        "matches": matches,
        "too_short": False,
    }


def format_recall(result: dict[str, Any]) -> str:
    """Past outcomes, as the tool result the model reads.

    The framing is the same one `webview_bridge.format_page` uses, for the
    same reason and with a sharper edge: this is *recollection*, and a model
    holding a string that claims to be its own past memory will weight it
    accordingly. A stored failure message can contain whatever the text it was
    written about contained, and a page once did.
    """
    if result.get("too_short"):
        return (
            "Give recall something to look for — a symbol, an error code, a "
            "path, a phrase from the failure. It searches this workspace's "
            "past step outcomes, not the web and not the files."
        )
    matches = result.get("matches") or []
    query = str(result.get("query") or "")
    if not matches:
        return (
            f"Nothing in this workspace's history matches {query!r}. "
            f"(read {result.get('scanned', 0)} recent outcome events.) That is "
            "an absence, not a proof it never happened: recall covers step "
            "outcomes, failures and retries, and only the most recent "
            f"{MAX_SCAN_EVENTS} of them."
        )

    header = (
        f"{len(matches)} past event(s) in this workspace matching {query!r}, "
        f"newest first — across {result.get('goals', 0)} goal(s), of which "
        f"{result.get('recovered', 0)} were later recovered by a retry that "
        "passed. These are recorded outcomes, not evidence about the current "
        "code: check the file before you believe one."
    )
    lines = [header]
    for match in matches:
        facts = ", ".join(f"{k}={v!r}" for k, v in (match.get("fields") or {}).items())
        step = f" step {match['step_id']}" if match.get("step_id") else ""
        lines.append(
            f"- {match['type']}{step} in goal "
            f"{match.get('goal_title') or match['goal_id']!r} — {facts} — "
            f"{'recovered' if match.get('recovered') else 'NOT recovered'}"
        )
    return "\n".join(lines)


# ── the thread grain ──────────────────────────────────────────────────────────


def project_thread(
    row: dict[str, Any],
    needle: str | None = None,
) -> dict[str, Any] | None:
    """One conversation row as a fixed summary, or None when it does not match.

    With `needle` the projection is a *match*: the thread counts only when the
    query appears in the thread's name or in one of its asks. Without one, the
    row projects unconditionally — the same fixed shape, for the no-query
    overview.
    """
    asks: list[str] = []
    for ask in row.get("asks") or []:
        # Clipped twice: at the query (so an oversized prompt is never read
        # whole to decide a match) and again here, for a row written before
        # the query-side clip existed.
        text = str(ask or "")[:MAX_ASK_CHARS]
        if text:
            asks.append(text)
    title = str(row.get("title") or "")
    if needle is not None:
        hay = " ".join([title, *asks]).lower()
        if needle.lower() not in hay:
            return None
    return {
        "id": str(row.get("id") or ""),
        "title": title[:MAX_ASK_CHARS],
        "asks": asks,
        "runs": int(row.get("runs") or 0),
        "completed": int(row.get("completed") or 0),
        "failed": int(row.get("failed") or 0),
        "cancelled": int(row.get("cancelled") or 0),
        "last_touched": float(row.get("last_touched") or 0.0),
    }


def search_threads(
    rows: list[dict[str, Any]],
    query: str | None = None,
    *,
    limit: int = MAX_THREADS,
) -> dict[str, Any]:
    """Threads of this workspace, recency-first, with their run outcomes.

    Matching is optional: a query narrows to the threads whose name or asks
    mention it, and no query (or a too-short one) means "this workspace's
    recent threads" — the overview is a legitimate first call for a thread
    that has just been opened and knows nothing.
    """
    needle = (query or "").strip()
    # No MIN_QUERY_CHARS gate here: the whole point of the no-query shape is
    # that it needs nothing from the caller, and a one-letter query is a
    # narrower overview, not a broader search than the tool can answer.
    projected: list[dict[str, Any]] = []
    for row in rows:
        item = project_thread(row, needle if needle else None)
        if item is not None:
            projected.append(item)
        if len(projected) >= max(1, min(int(limit or 1), MAX_THREADS)):
            break
    return {
        "query": needle,
        "threads": projected,
        "count": len(projected),
    }


def _describe_run_counts(thread: dict[str, Any]) -> str:
    runs = thread["runs"]
    parts: list[str] = []
    for key, label in (("completed", "completed"), ("failed", "failed"), ("cancelled", "cancelled")):
        n = int(thread.get(key) or 0)
        if n:
            parts.append(f"{n} {label}")
    detail = ", ".join(parts) if parts else "none finished"
    return f"{runs} run(s): {detail}"


def format_thread_recall(result: dict[str, Any]) -> str:
    """Thread history, as the tool result the model reads.

    The framing is deliberately near `format_recall`'s: this is recollection,
    and the asks are what people *said* they wanted, not a spec of what the
    code now does — an ask may have failed, or been cancelled, or described
    work that later runs redid differently.
    """
    threads = result.get("threads") or []
    if not threads:
        return (
            "No earlier thread in this workspace matches. That is an absence, "
            "not a proof: recall_threads covers the most recent threads and "
            "their run outcomes, not every conversation ever held."
        )
    header = (
        f"{len(threads)} earlier thread(s) in this workspace, newest first. "
        "Asks are what was asked for, not a claim that it was done — check "
        "the run's outcome and the code itself before you rely on one."
    )
    lines = [header]
    for thread in threads:
        name = thread.get("title") or "(untitled thread)"
        lines.append(f"- {name} — {_describe_run_counts(thread)}")
        for ask in (thread.get("asks") or [])[:MAX_ASKS]:
            lines.append(f"    asked: {ask}")
    return "\n".join(lines)


# ── the observation grain ─────────────────────────────────────────────────────
#
# docs/10 §6's `reflect` half, mechanical. `recall` reads events and
# `recall_threads` reads conversations; this is the third grain — what the
# history has *taught* — derived from the first one so it can never disagree
# with it. The word "distill" is chosen over "summarize": nothing here reads
# model prose, because a summary of prose inherits its trustworthiness.


#: How many stored observations one answer may carry. Proof counts make
#: observations compelling, which is exactly why the answer is capped.
MAX_OBSERVATIONS = 8

#: An observation fades: its strength is its proof count halved every this many days since the failure
#: was last seen. A one-off failure from last year, long fixed, used to carry the same weight and the same
#: words as one from yesterday, and sat in the brief until five newer ones pushed it out. A stale lesson is
#: worse than none, because the model is told it is what this workspace's history has *taught*.
OBSERVATION_HALF_LIFE_DAYS = 30.0

#: The brief leaves out an observation whose strength has fallen *under* this. One proof is exactly the
#: floor at one half-life, so a failure seen once is in the brief for a month. Only the brief reads it:
#: `recall` over the raw events still finds the old failure when someone asks.
BRIEF_FLOOR = 0.5

_DAY = 86_400.0


def observation_age_days(seen_at: float | None, now: float) -> float | None:
    """How many days before `now` a failure was last seen, or None when nothing says.

    A time that was never recorded (0, negative, missing) is an *unknown* age, which costs nothing and is
    not dated, rather than an age of fifty years. A time in the future is age zero, never a boost.
    """
    if seen_at is None or seen_at <= 0:
        return None
    return max(0.0, (now - seen_at) / _DAY)


def observation_strength(proof: int, age_days: float | None) -> float:
    """The proof count, halved every `OBSERVATION_HALF_LIFE_DAYS` of age. An unknown age is full strength."""
    if age_days is None:
        return float(proof)
    return float(proof) * float(0.5 ** (age_days / OBSERVATION_HALF_LIFE_DAYS))


def seen_ago(age_days: float) -> str:
    """How long ago, for a line a model reads: `last seen today`, `last seen 1 day ago`, `last seen 12 days ago`."""
    days = int(age_days)
    if days <= 0:
        return "last seen today"
    return f"last seen {days} day{'s' if days != 1 else ''} ago"


def search_observations(
    rows: list[dict[str, Any]],
    query: str | None = None,
    *,
    limit: int = MAX_OBSERVATIONS,
) -> dict[str, Any]:
    """The durable beliefs about this workspace, most recently refined first.

    The rows come from `GoalService.observation_rows` — the store the
    consolidation pass refines after each run — so these survive the event
    scan window and carry proof counts accumulated across every scan that
    confirmed them. `query` is a substring filter over subject, lesson and
    example; like `search_threads`, an absent or short query is the overview,
    not a refused search.
    """
    needle = (query or "").strip().lower()
    out: list[dict[str, Any]] = []
    for row in rows:
        if needle:
            hay = " ".join([
                str(row.get("subject") or ""),
                str(row.get("lesson") or ""),
                str(row.get("example") or ""),
            ]).lower()
            if needle not in hay:
                continue
        out.append({
            "subject": str(row.get("subject") or ""),
            "lesson": str(row.get("lesson") or ""),
            "example": str(row.get("example") or "")[:MAX_FIELD_CHARS],
            "proof": int(row.get("proof") or 0),
            "evidence": [str(e) for e in (row.get("evidence") or [])][:MAX_MATCHES],
            "refined_at": float(row.get("refined_at") or 0.0),
        })
        if len(out) >= max(1, min(int(limit or 1), MAX_OBSERVATIONS)):
            break
    return {"query": query or "", "observations": out, "count": len(out)}


def _observation_subject(error: dict[str, Any]) -> str | None:
    """The key a failure groups under, or None when it has no key worth keeping.

    Code-first, message-second: a code is what the engine itself classified,
    a message is prose that happens to travel with the event. A failure with
    neither is a row, not an observation.
    """
    code = str(error.get("code") or "").strip()
    if code:
        return f"code:{code}"
    message = str(error.get("message") or "").strip()
    if message:
        return f"message:{message[:80].lower()}"
    return None


def _one_observation(
    subject: str, group: list[dict[str, Any]],
    recovered: set[tuple[str, str]] | None = None,
) -> dict[str, Any] | None:
    """The observation one subject's events support, or None when it teaches
    nothing.

    `recovered` is `recovered_pairs`' answer over the same scan, and it is the
    only thing allowed to flip a failure lesson positive: "a retry got past
    this" is a claim about *the same goal and step*, which is a link the rows
    actually carry. A `goal_status COMPLETED` from another goal proves nothing
    about this subject's code, so status events form no cross-goal lesson here
    — that signal lives in the thread grain, where it belongs.
    """
    kinds: set[str] = set()
    messages: list[str] = []
    for ev in group:
        kinds.add(str(ev.get("type") or ""))
        msg = str((ev.get("payload") or {}).get("message") or "").strip()
        if msg:
            messages.append(msg)

    failure_kinds = kinds & {"error", "agent_call_failed", "fix_retry", "agent_output_invalid"}
    if failure_kinds:
        if recovered:
            lesson = "A retry got past this failure before."
        else:
            lesson = "This failure has happened here, with no retry recorded past it."
    else:
        return None

    example = ""
    for msg in messages:
        if msg and msg not in ("", "None"):
            example = msg[:MAX_FIELD_CHARS]
            break

    return {
        "subject": subject,
        "proof": len(group),
        "lesson": lesson,
        "example": example,
        "evidence": [
            str(ev.get("id") or "") for ev in reversed(group) if str(ev.get("id") or "")
        ],
        # The newest event behind it, so a reader can age the lesson by what happened and not by when a
        # pass last ran. Not stored (there is no column for it, and none is needed): it is read from the
        # same rows the scan already holds.
        "last_seen": max((float(ev.get("timestamp") or 0.0) for ev in group), default=0.0),
    }


def form_observations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group raw outcome events into observation-shaped dicts, one per subject.

    The formation half of reflect: it decides what an observation *is* (docs/10
    §6) — a subject, the events that prove it, and the lesson — while staying
    pure over the rows it is handed. Currently driven by `distill_observations`
    over one scan's worth of rows; a later pass can hand it other scans without
    the rules moving.

    Grouping is by *subject*, not by goal: every failure naming one subject —
    across every goal that hit it — lands in one group, so the proof count is
    how often this engine has seen that exact failure, wherever it happened in
    the workspace. The event types here are exactly `RECALLABLE`'s keys, for
    the same allow-list reason the tools obey.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(str(payload or "{}"))
            except (TypeError, ValueError):
                continue
        if not isinstance(payload, dict):
            continue
        kind = str(ev.get("type") or "")
        row = {**ev, "payload": payload}
        subject: str | None = None
        if kind == "error":
            subject = _observation_subject(payload)
        elif kind == "stage_result":
            # A stage that *failed* is history; a stage that passed is the
            # pipeline working, which is not an observation yet. Pass ratios
            # are the statistics screen's job.
            if str(payload.get("outcome") or "") not in ("invalid",):
                continue
            subject = f"stage:{payload.get('stage') or payload.get('role') or ''}"
        elif kind == "fix_retry":
            # `step_id` is a column of the row, not a payload key — reading the
            # payload here grouped every bare retry under `step:unknown`.
            subject = f"step:{ev.get('step_id') or 'unknown'}"
        elif kind == "test_result":
            # No subject of its own — the retry pairing reads it. It still has
            # to reach `grouped`, because the pass that proves a failure was
            # recovered is exactly this row; dropping it here would report
            # every known failure as unrecovered (the same bug the allow-list
            # test caught in `recall` itself).
            subject = f"step:{ev.get('step_id') or 'unknown'}"
        if subject is None:
            continue
        grouped.setdefault(subject, []).append(row)

    # One definition of "got past it", inherited like `recall`'s own mark:
    # pairing is order-sensitive per goal-and-step, so it is computed over the
    # whole scan and looked up per subject's evidence rows. `recovered_steps`
    # wants parsed payloads (its other caller normalises on the way out of the
    # database), so the grouped rows — whose payloads `form_observations`
    # already parsed — are what it gets, not the raw rows.
    #
    # The rows are re-ordered **oldest-first** for the pairing, because that is
    # the order its argument assumes: `recall_events` returns newest-first, and
    # feeding it straight in lets the pass arrive before the retry, scoring
    # every recovered step unrecovered. `_sweep_metrics` orders on timestamp
    # then sequence; the same key is used here.
    from engine.metrics import recovered_steps

    chronological = sorted(
        (row for group in grouped.values() for row in group),
        key=lambda r: (float(r.get("timestamp") or 0.0), int(r.get("sequence") or 0)),
    )
    recovered_pairs = recovered_steps(chronological)
    evidence_by_subject: dict[str, list[tuple[str, str]]] = {}
    for subject, group in grouped.items():
        pairs: list[tuple[str, str]] = []
        for ev in group:
            goal_id = str(ev.get("goal_id") or "")
            step_id = str(ev.get("step_id") or "")
            if step_id:
                pairs.append((goal_id, step_id))
        evidence_by_subject[subject] = pairs

    out: list[dict[str, Any]] = []
    for subject in sorted(grouped):
        group_recovered = {
            pair for pair in evidence_by_subject[subject] if pair in recovered_pairs
        }
        observation = _one_observation(
            subject, grouped[subject], recovered=group_recovered or None,
        )
        if observation is not None:
            out.append(observation)
    return out


def distill_observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The reflect pass over one scan of `recall_events` rows.

    Formation only — it derives observations from this scan and returns them.
    There is deliberately **no persistence yet**: no observation table is
    written, so there is nothing to go stale, and every answer is computed
    from the rows that still exist. docs/10 §6's durable, refined store is the
    open half and lands as an engine write path, not here.

    Rows arrive newest-first (the query's order); the scan is read as-is and
    the proof count is the number of scanned rows that support the subject —
    bounded by the scan cap, and honest about it (`MAX_SCAN_EVENTS`).
    """
    return form_observations(rows)


def format_observations(observations: list[dict[str, Any]], now: float | None = None) -> str:
    """Observations as prose for the brief. Empty input gets no line at all —
    the brief stays silent rather than explaining an absence twice.

    A line says how long ago the failure was last seen when that is known (`last_seen`), because "this has
    happened here" means something different a day after it than a quarter after it.
    """
    if not observations:
        return ""
    clock = time.time() if now is None else now
    lines = []
    for obs in observations[:MAX_BRIEF_OBSERVATIONS]:
        example = f' — e.g. {obs["example"]!r}' if obs.get("example") else ""
        age = observation_age_days(obs.get("last_seen"), clock)
        when = f", {seen_ago(age)}" if age is not None else ""
        lines.append(
            f"- {obs['subject']}: {obs['lesson']} (proof: {obs['proof']} event(s)"
            f" in the most recent {MAX_SCAN_EVENTS} scanned{when}{example})"
        )
    return "\n".join(lines)


# ── the brief ─────────────────────────────────────────────────────────────────


def build_brief(
    thread_rows: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
    *,
    observation_rows: list[dict[str, Any]] | None = None,
    current_thread_id: str | None = None,
    now: float | None = None,
) -> str:
    """What a new turn should know before it starts, as one bounded string.

    The injection half of the memory feature: threads, observations, and —
    only when the store is empty or older than the scan — events, without
    anyone having to call a tool first. Composed from the same reads the tools
    serve — `thread_recall`'s rows, `observation_rows`, and `recall_events`'
    rows — so a brief and a tool answer cannot disagree about the same
    history.

    The current thread is **excluded by id, not by position**: `thread_recall`
    returns the most recent threads and the current one is usually the newest,
    but a goal created before its conversation (or a concurrent turn) would
    break that, and "rows[0] is mine" is an assumption the data does not make.

    Returns an empty string when there is nothing to say, so the prompt gains
    no section at all rather than a header over an absence.
    """
    sections: list[str] = []

    threads = [
        t for t in search_threads(
            thread_rows, None, limit=MAX_THREADS + 1,
        )["threads"]
        if current_thread_id is None or t["id"] != current_thread_id
    ][:MAX_THREADS]
    if threads:
        lines = [
            "Earlier threads in this workspace (newest first). Asks are what "
            "was asked for, not a claim that it was done:"
        ]
        for thread in threads:
            name = thread.get("title") or "(untitled thread)"
            lines.append(f"- {name} — {_describe_run_counts(thread)}")
            for ask in (thread.get("asks") or [])[:1]:
                lines.append(f"    asked: {ask}")
        sections.append("\n".join(lines))

    clock = time.time() if now is None else now
    stored = [
        o for o in search_observations(
            observation_rows or [], None, limit=MAX_BRIEF_OBSERVATIONS,
        )["observations"]
        if not o["subject"].startswith("step:")
    ]
    derived: list[dict[str, Any]] = []
    # Refinement lag: the store is refined after each run, so a just-finished
    # run's lessons may not be in it yet. The scan tops the stored proofs up
    # (never down — the scan window is smaller than the store) and supplies
    # subjects the store has never seen.
    fresh = [
        o for o in distill_observations(event_rows)
        if not o["subject"].startswith("step:")
    ]
    by_subject = {o["subject"]: o for o in stored}
    for o in fresh:
        known = by_subject.get(o["subject"])
        if known is None:
            derived.append(o)
        elif o["proof"] > known["proof"]:
            known["proof"] = o["proof"]
    # When each lesson was last seen. The newest event behind it when the scan can see one, which is the
    # truth; the row's `refined_at` only when it cannot. The consolidation pass refines every subject still
    # in its scan window after every run, so a quiet workspace's year-old failure has a row that says
    # "yesterday", and trusting it would make the decay a no-op exactly where it is needed.
    newest = {o["subject"]: float(o.get("last_seen") or 0.0) for o in fresh}
    for o in stored:
        o["last_seen"] = newest.get(o["subject"]) or o.get("refined_at")
    # Decided on the lesson's own age, so a lesson the decay drops here is not brought back below as a new
    # one: `derived` only holds subjects the store has never seen.
    faded = [
        o for o in (*stored, *derived)
        if observation_strength(
            int(o.get("proof") or 0), observation_age_days(o.get("last_seen"), clock),
        ) < BRIEF_FLOOR
    ]
    gone = {id(o) for o in faded}
    observations = [o for o in (*stored, *derived) if id(o) not in gone][:MAX_BRIEF_OBSERVATIONS]
    if observations:
        sections.append(
            "What this workspace's own history has already taught us:\n"
            + format_observations(observations, clock)
        )

    if not sections:
        return ""
    return (
        "\n\n".join(sections)
        + "\n(Recorded history, not evidence about the current code — "
        "`recall` and `recall_threads` can go deeper on any of it.)"
    )
