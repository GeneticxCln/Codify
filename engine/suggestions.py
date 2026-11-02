"""The suggestions inbox: things the engine noticed that a person may want to act on.

Four detectors read rows the engine already holds (and, if a person opted in, GitHub's public checks for a commit)
and write **offers** to one table. They are the proactive half of the assistant and the part most worth being
suspicious of, so the guarantees are the point of the file:

* **No model call.** Nothing here is handed a provider, a registry or an executor. A detector is a SQL query and a
  comparison; the only thing it can spend is a few milliseconds, and the CI one a single HTTPS request.
* **No turn, no goal, no write.** A detector inserts into `suggestions` and nothing else. A turn is created only by
  `POST /conversations/{id}/turns` (docs/00 §6.8) and only the fixer writes (§6.9); an offer reaches neither. The
  accept route records which turn a person *made* from an offer, after they made it. It does not make one.
* **Rate-limited by construction.** One row per (workspace, kind, key) however often the sweep runs; at most
  `OPEN_CAP` open per workspace; at most one new offer of a kind per `KIND_GAP_S`; a dismissed or accepted one is not
  offered again for `COOLDOWN_S`. A detector that is wrong costs a person five rows, not a flood.
* **An offer goes away when its reason does.** A paused goal that was resumed, a command that passes again: the open
  row is removed, so the inbox is a list of what is true now and not a log of what once was.
* **Quoted text is bounded and flat.** A command line or a goal title is the text of a repository or a model. It is
  collapsed to one line, stripped of backticks and capped, and it appears in `evidence` and in a prompt the person
  reads before sending, never as an instruction to the engine.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from engine import github_remote
from engine.db import row_to_dict
from engine.models import PAUSE_REASONS, Suggestion, SuggestionKind, SuggestionList
from engine.services import ApiError, SettingsService, WorkspaceService

#: Open offers a workspace may hold at once (snoozed ones count: they come back).
OPEN_CAP = 5
#: The shortest time between two new offers of one kind in one workspace.
KIND_GAP_S = 3600.0
#: How long a dismissed or accepted offer is left alone before the same finding may be offered again.
COOLDOWN_S = 7 * 86400.0
#: How often the watcher looks, and how long after boot it waits before the first look.
SWEEP_INTERVAL_S = 600.0
FIRST_SWEEP_DELAY_S = 60.0
#: The CI detector asks GitHub about one workspace at most this often, and only if the workspace did something today.
CI_INTERVAL_S = 1800.0
CI_ACTIVITY_WINDOW_S = 86400.0

FAILING_WINDOW_S = 86400.0
FAILING_MIN = 3
STALE_REVIEW_S = 14 * 86400.0
ACTIVE_WINDOW_S = 7 * 86400.0
MAX_EVIDENCE = 5
#: Rows of recent test results one workspace's detector reads. A bound on the work, not on the finding.
MAX_EVENTS_READ = 500

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "action_required", "startup_failure"})


@dataclass
class Candidate:
    """What a detector found, before the inbox decides whether to show it."""

    kind: SuggestionKind
    key: str
    title: str
    detail: str
    prompt: str
    evidence: list[dict[str, Any]] = field(default_factory=list)


def one_line(text: object, limit: int) -> str:
    """Text from a repository or a model, made safe to quote: one line, no backticks, no controls, capped."""
    flat = _SPACE.sub(" ", _CONTROL.sub(" ", str(text))).replace("`", "'").strip()
    return flat if len(flat) <= limit else flat[: max(0, limit - 1)].rstrip() + "…"


# ── detectors: pure reads ───────────────────────────────────────────────────────────────────────────────────────


def detect_failing_commands(conn: sqlite3.Connection, workspace_id: str, now: float) -> list[Candidate]:
    """A command the verifier keeps running that keeps failing, and has not passed since.

    Read from `test_result` events: the engine's own record of a command it ran and what it exited with. A
    command that failed `FAILING_MIN` times in the last day is raised; one whose newest run passed is not, because
    it was fixed. (A command typed in a terminal is not in this record: the terminal belongs to the desktop shell,
    which reports no exit codes. docs/09 §10.22 says so.)
    """
    rows = conn.execute(
        "SELECT e.id, e.goal_id, e.payload, e.timestamp FROM events e JOIN goals g ON g.id = e.goal_id "
        "WHERE g.workspace_id = ? AND e.type = 'test_result' AND e.timestamp >= ? "
        "ORDER BY e.timestamp DESC LIMIT ?",
        (workspace_id, now - FAILING_WINDOW_S, MAX_EVENTS_READ),
    ).fetchall()
    newest_exit: dict[tuple[str, ...], int] = {}
    failures: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        argv = payload.get("argv") if isinstance(payload, dict) else None
        code = payload.get("exit_code") if isinstance(payload, dict) else None
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
            continue
        if not payload.get("ran") or isinstance(code, bool) or not isinstance(code, int):
            continue
        shape = tuple(argv)
        newest_exit.setdefault(shape, code)  # rows are newest first, so the first one seen is the newest
        if code != 0:
            failures.setdefault(shape, []).append(
                {"event_id": row["id"], "goal_id": row["goal_id"], "exit_code": code, "at": row["timestamp"]}
            )
    found: list[Candidate] = []
    for shape, hits in failures.items():
        if newest_exit.get(shape) == 0 or len(hits) < FAILING_MIN:
            continue
        command = one_line(" ".join(shape), 120)
        found.append(Candidate(
            kind="failing_command",
            key=json.dumps(list(shape))[:300],
            title=f"`{command}` has failed {len(hits)} times today",
            detail=f"It last exited {hits[0]['exit_code']} and has not passed since.",
            prompt=(
                f"The command `{command}` has failed {len(hits)} times in this workspace in the last day, most "
                f"recently with exit code {hits[0]['exit_code']}. Find out why it keeps failing and propose a plan "
                "to fix it. Do not change anything until I approve the plan."
            ),
            evidence=hits[:MAX_EVIDENCE],
        ))
    return found


def detect_paused_goals(
    conn: sqlite3.Connection, workspace_id: str, now: float, hours: int,
) -> list[Candidate]:
    """A goal that has been waiting for a person's Start for longer than `hours`."""
    rows = conn.execute(
        "SELECT id, title, updated_at FROM goals WHERE workspace_id = ? AND status = 'PAUSED' AND updated_at <= ? "
        "ORDER BY updated_at",
        (workspace_id, now - hours * 3600.0),
    ).fetchall()
    found: list[Candidate] = []
    for row in rows:
        last = conn.execute(
            "SELECT payload FROM events WHERE goal_id = ? AND type = 'goal_status' ORDER BY sequence DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        code = None
        if last is not None:
            try:
                body = json.loads(last["payload"])
                raw = body.get("reason_code") if isinstance(body, dict) else None
                code = raw if isinstance(raw, str) and raw in PAUSE_REASONS else None
            except (TypeError, ValueError):
                code = None
        waited = max(1, int((now - float(row["updated_at"])) // 3600))
        title = one_line(row["title"], 80)
        why = f" ({code.replace('_', ' ')})" if code else ""
        found.append(Candidate(
            kind="paused_goal",
            key=row["id"],
            title=f"“{title}” has been paused for {waited} h",
            detail=PAUSE_REASONS[code] if code else "It was paused and is waiting for you to press Start.",
            prompt=(
                f"My goal “{title}” has been paused for about {waited} hours{why}. Look at where it stopped and "
                "tell me what it needs before I press Start. Do not change anything."
            ),
            evidence=[{"goal_id": row["id"], "paused_hours": waited, "reason_code": code}],
        ))
    return found


def detect_stale_health_review(conn: sqlite3.Connection, workspace_id: str, now: float) -> list[Candidate]:
    """A workspace that is being worked in and has not had a health review for two weeks (or ever).

    A review records nothing by design (the skill says so), so the only trace is the log line the conductor writes
    when it loads the skill. An idle workspace is not raised: a review of a project nobody has touched is noise.
    """
    span = conn.execute(
        "SELECT MIN(created_at) AS first, MAX(updated_at) AS last FROM goals WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    if span is None or span["first"] is None:
        return []
    if now - float(span["last"]) > ACTIVE_WINDOW_S or now - float(span["first"]) < STALE_REVIEW_S:
        return []
    seen = conn.execute(
        "SELECT MAX(e.timestamp) AS at FROM events e JOIN goals g ON g.id = e.goal_id "
        "WHERE g.workspace_id = ? AND e.type = 'log' AND e.payload LIKE '%loaded the health-review skill%'",
        (workspace_id,),
    ).fetchone()
    last_review = seen["at"] if seen is not None else None
    if last_review is not None and now - float(last_review) < STALE_REVIEW_S:
        return []
    detail = (
        "No health review has run in this workspace."
        if last_review is None
        else f"The last one was {int((now - float(last_review)) // 86400)} days ago."
    )
    return [Candidate(
        kind="stale_health_review",
        key="health-review",
        title="This project is due a health review",
        detail=detail,
        prompt=(
            "Do a health review of this project: walk the checklist with your read-only tools and give me a "
            "proposed verdict with evidence for each item, and what to fix first. Do not change anything."
        ),
        evidence=[{"last_review_at": last_review}],
    )]


async def detect_red_ci(
    root_path: str, checker: Callable[[str], Awaitable[dict[str, Any]]] = github_remote.read_checks,
) -> tuple[Candidate | None, bool]:
    """Whether the workspace's current commit is red on GitHub, and whether the answer settles the question.

    The second value is False when GitHub could not be asked or did not say (`unknown`, `none`, `pending`): an
    offer already open is then left as it is rather than retired on no evidence.
    """
    answer = await checker(root_path)
    state = answer.get("state")
    sha = answer.get("sha")
    if state == "success":
        return None, True
    if state != "failure" or not isinstance(sha, str) or not sha:
        return None, False
    failing = [
        one_line(c.get("name", ""), 80) for c in answer.get("checks", [])
        if isinstance(c, dict) and c.get("conclusion") in _FAILED_CONCLUSIONS
    ][:MAX_EVIDENCE]
    short = sha[:8]
    return Candidate(
        kind="red_ci",
        key=sha,
        title=f"CI is failing on {short}",
        detail=f"{len(failing)} check(s) failed." if failing else "A check failed.",
        prompt=(
            f"CI is failing on commit {short}. Read the failing checks, find out why, and propose a plan to fix "
            "them. Do not change anything until I approve the plan."
        ),
        evidence=[{"commit": sha, "failing_checks": failing, "url": str(answer.get("url") or "")[:300]}],
    ), True


# ── the inbox ───────────────────────────────────────────────────────────────────────────────────────────────────


def _suggestion(row: sqlite3.Row) -> Suggestion:
    data = row_to_dict(row)
    data["evidence"] = json.loads(data.get("evidence") or "[]")
    return Suggestion.model_validate(data)


class SuggestionService:
    """The rows, and the rules that keep them few. It has no way to start work: it holds a connection and a clock."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._db = conn

    def offer(self, workspace_id: str, c: Candidate, now: float) -> bool:
        """Record a finding if the rules admit it. True when a row was created or reopened."""
        row = self._db.execute(
            "SELECT id, status, updated_at FROM suggestions WHERE workspace_id = ? AND kind = ? AND key = ?",
            (workspace_id, c.kind, c.key),
        ).fetchone()
        if row is not None and (row["status"] == "open" or now - float(row["updated_at"]) < COOLDOWN_S):
            return False
        open_now = self._db.execute(
            "SELECT COUNT(*) AS n FROM suggestions WHERE workspace_id = ? AND status = 'open'", (workspace_id,),
        ).fetchone()["n"]
        if open_now >= OPEN_CAP:
            return False
        last = self._db.execute(
            "SELECT MAX(created_at) AS at FROM suggestions WHERE workspace_id = ? AND kind = ?",
            (workspace_id, c.kind),
        ).fetchone()["at"]
        if last is not None and now - float(last) < KIND_GAP_S:
            return False
        evidence = json.dumps(c.evidence)
        if row is None:
            self._db.execute(
                "INSERT INTO suggestions (id, workspace_id, kind, key, title, detail, evidence, prompt, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                (str(uuid.uuid4()), workspace_id, c.kind, c.key, c.title, c.detail, evidence, c.prompt, now, now),
            )
        else:
            self._db.execute(
                "UPDATE suggestions SET title = ?, detail = ?, evidence = ?, prompt = ?, status = 'open', "
                "snoozed_until = NULL, goal_id = NULL, created_at = ?, updated_at = ? WHERE id = ?",
                (c.title, c.detail, evidence, c.prompt, now, now, row["id"]),
            )
        self._db.commit()
        return True

    def retire(self, workspace_id: str, kind: str, live_keys: set[str]) -> int:
        """Remove the open offers of a kind whose finding no longer holds. Dismissed and accepted rows stay."""
        rows = self._db.execute(
            "SELECT id, key FROM suggestions WHERE workspace_id = ? AND kind = ? AND status = 'open'",
            (workspace_id, kind),
        ).fetchall()
        gone = [r["id"] for r in rows if r["key"] not in live_keys]
        for suggestion_id in gone:
            self._db.execute("DELETE FROM suggestions WHERE id = ?", (suggestion_id,))
        if gone:
            self._db.commit()
        return len(gone)

    def inbox(self, now: float, workspace_id: str | None = None) -> SuggestionList:
        where, args = ("AND workspace_id = ?", (workspace_id,)) if workspace_id else ("", ())
        rows = self._db.execute(
            f"SELECT * FROM suggestions WHERE status = 'open' {where} ORDER BY updated_at DESC", args,  # noqa: S608 — `where` is one of two literals above
        ).fetchall()
        shown = [_suggestion(r) for r in rows if r["snoozed_until"] is None or float(r["snoozed_until"]) <= now]
        return SuggestionList(suggestions=shown, snoozed=len(rows) - len(shown))

    def get(self, suggestion_id: str) -> Suggestion:
        row = self._db.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_suggestion", "suggestion not found")
        return _suggestion(row)

    def _open(self, suggestion_id: str) -> Suggestion:
        found = self.get(suggestion_id)
        if found.status != "open":
            raise ApiError(409, "illegal_status", f"this suggestion is already {found.status}")
        return found

    def snooze(self, suggestion_id: str, hours: int, now: float) -> Suggestion:
        self._open(suggestion_id)
        self._db.execute(
            "UPDATE suggestions SET snoozed_until = ?, updated_at = ? WHERE id = ?",
            (now + hours * 3600.0, now, suggestion_id),
        )
        self._db.commit()
        return self.get(suggestion_id)

    def dismiss(self, suggestion_id: str, now: float) -> Suggestion:
        self._open(suggestion_id)
        self._db.execute(
            "UPDATE suggestions SET status = 'dismissed', snoozed_until = NULL, updated_at = ? WHERE id = ?",
            (now, suggestion_id),
        )
        self._db.commit()
        return self.get(suggestion_id)

    def accept(self, suggestion_id: str, goal_id: str, now: float) -> Suggestion:
        """Record that a person made a turn from this offer. The goal must exist, here, and be newer than the offer:
        the route takes the id of work somebody already did, so it cannot be used to claim an unrelated goal."""
        found = self._open(suggestion_id)
        goal = self._db.execute(
            "SELECT workspace_id, created_at FROM goals WHERE id = ?", (goal_id,),
        ).fetchone()
        if goal is None or goal["workspace_id"] != found.workspace_id:
            raise ApiError(409, "goal_not_in_workspace", "that goal is not in this suggestion's workspace")
        if float(goal["created_at"]) < found.created_at:
            raise ApiError(409, "goal_predates_suggestion", "that goal was made before this suggestion was")
        self._db.execute(
            "UPDATE suggestions SET status = 'accepted', goal_id = ?, snoozed_until = NULL, updated_at = ? WHERE id = ?",
            (goal_id, now, suggestion_id),
        )
        self._db.commit()
        return self.get(suggestion_id)


# ── the watcher ─────────────────────────────────────────────────────────────────────────────────────────────────


class SuggestionWatch:
    """Looks every few minutes and fills the inbox. It is handed no provider, executor or turn service.

    A sweep is the only thing it does, and the sweep's reach is the whole of this file: read rows, ask GitHub if
    (and only if) `suggest_ci` is on, write offers. `tests/test_suggestions.py` runs it over a month of simulated
    time with a model that raises if called and asserts no goal or conversation appears.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: SettingsService,
        workspaces: WorkspaceService,
        *,
        clock: Callable[[], float] = time.time,
        ci_checker: Callable[[str], Awaitable[dict[str, Any]]] = github_remote.read_checks,
    ) -> None:
        self._db = conn
        self._settings = settings
        self._workspaces = workspaces
        self._clock = clock
        self._ci_checker = ci_checker
        self.service = SuggestionService(conn)
        self._ci_checked: dict[str, float] = {}

    def _sweep_database(self, now: float) -> int:
        hours = self._settings.get_int("suggest_paused_hours")
        made = 0
        for ws in self._workspaces.list_workspaces():
            for kind, found in (
                ("failing_command", detect_failing_commands(self._db, ws.id, now)),
                ("paused_goal", detect_paused_goals(self._db, ws.id, now, hours)),
                ("stale_health_review", detect_stale_health_review(self._db, ws.id, now)),
            ):
                self.service.retire(ws.id, kind, {c.key for c in found})
                made += sum(1 for c in found if self.service.offer(ws.id, c, now))
        return made

    def _recently_active(self, workspace_id: str, now: float) -> bool:
        """Whether anything happened in this workspace today. A workspace nobody is using is not worth a request."""
        row = self._db.execute(
            "SELECT 1 FROM goals WHERE workspace_id = ? AND updated_at >= ? LIMIT 1",
            (workspace_id, now - CI_ACTIVITY_WINDOW_S),
        ).fetchone()
        return row is not None

    async def _sweep_ci(self, now: float) -> int:
        made = 0
        for ws in await asyncio.to_thread(self._workspaces.list_workspaces):
            if now - self._ci_checked.get(ws.id, 0.0) < CI_INTERVAL_S:
                continue
            if not await asyncio.to_thread(self._recently_active, ws.id, now):
                continue
            self._ci_checked[ws.id] = now
            candidate, settled = await detect_red_ci(ws.root_path, self._ci_checker)
            if candidate is not None:
                await asyncio.to_thread(self.service.retire, ws.id, "red_ci", {candidate.key})
                made += int(await asyncio.to_thread(self.service.offer, ws.id, candidate, now))
            elif settled:
                await asyncio.to_thread(self.service.retire, ws.id, "red_ci", set())
        return made

    async def sweep(self, now: float | None = None) -> int:
        """One look. Returns how many offers were created. Does nothing at all when the setting is off."""
        if not self._settings.get_int("suggestions"):
            return 0
        moment = self._clock() if now is None else now
        made = await asyncio.to_thread(self._sweep_database, moment)
        if self._settings.get_int("suggest_ci"):
            made += await self._sweep_ci(moment)
        return made

    async def run(self) -> None:
        await asyncio.sleep(FIRST_SWEEP_DELAY_S)
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a failed look is a line on stderr, never a reason to stop looking
                print(f"suggestions sweep failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            await asyncio.sleep(SWEEP_INTERVAL_S)
