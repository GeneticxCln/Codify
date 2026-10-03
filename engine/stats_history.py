"""Daily snapshots of the cross-goal statistics.

`/stats/overview` recomputes from the live log every time, which answers "how
does it look now" perfectly — and nothing at all once the details age out of
view. Two things a live sweep cannot do:

1. **Survive pruning.** A long-lived install will eventually tidy its event log
   (or a user will clear old goals); rows frozen the day they happened keep the
   trend chartable regardless.
2. **Show a stable yesterday.** A day's numbers keep moving while the day is
   live, which is correct for the day being lived and wrong for the day being
   reviewed.

So each day, the first stats read of that day freezes the previous day's
*final* numbers into a snapshot row keyed by date. Reads serve the live
computation for the current day (still moving, still correct) and snapshot
rows for every day before it — the join gives one series that reaches back
past any log pruning, computed by one implementation.

Snapshot rows are never written lazily deep in a read path's arithmetic: the
persistence service owns the table and the "is today's already frozen" check,
and the endpoint calls it explicitly, so the tests can drive the boundary
directly.
"""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_left
from datetime import datetime, timezone

from engine.stats import ACTIVE_STATUSES as _ACTIVE, build_overview
from typing import Any


def utc_day(ts: float) -> str:
    """The UTC calendar day a timestamp belongs to — one function, so the
    snapshot boundary and the test doubles agree to the hour."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


_SECONDS_PER_DAY = 86400.0


def active_days(goals: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[str]:
    """The UTC days any goal or event falls on, oldest first.

    Timestamps are put into whole-day buckets *before* a date is formatted, so the cost is one division per
    record and one `utc_day` per distinct day, not one `datetime` and one `strftime` per record. This runs on
    every stats read over up to 20,000 events, on reads where every past day is already frozen, and the
    per-record version measured 40 to 100 ms on the event loop for nothing. POSIX time counts whole days
    from a UTC midnight, so a bucket and the day `utc_day` names for any moment in it cannot disagree.
    """
    buckets = {int((g.get("updated_at") or g.get("created_at") or 0.0) // _SECONDS_PER_DAY) for g in goals}
    buckets |= {int((e.get("timestamp") or 0.0) // _SECONDS_PER_DAY) for e in events}
    return sorted({utc_day(bucket * _SECONDS_PER_DAY) for bucket in buckets})


def _day_end(day: str) -> float:
    """The first instant after a UTC day: the exclusive upper bound of what that day's document may see."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    return start + _SECONDS_PER_DAY


# How many days one read may freeze. A first read over a long history could owe ninety documents, each a
# full overview over up to 20,000 events (about 70 ms each); that is six seconds spent inside one request. The
# newest days go first, so the chart is useful at once, and the rest follow on the next reads.
MAX_FREEZE_PER_READ = 31


class StatsSnapshotService:
    """Freezes one document per UTC day, on the first read after it ends."""

    def __init__(self, conn: sqlite3.Connection):
        self._db = conn

    def pending_days(self, goals: list[dict[str, Any]], events: list[dict[str, Any]], now: float, keep: int | None = None) -> list[str]:
        """The past days with activity that have no frozen row yet, newest first, at most `MAX_FREEZE_PER_READ`.

        With a retention policy (`keep` > 0) only the newest `keep` active days are candidates, which is exactly
        the set `prune` leaves standing. Without that bound, every read froze every older active day and `prune`
        deleted them again straight away, so the next read froze them again: with 400 days of history and the
        default 90 that was about 25 seconds of work thrown away on each Stats open, on the event loop.
        """
        today = utc_day(now)
        past = [day for day in active_days(goals, events) if day < today]
        if keep is not None and keep > 0:
            past = past[-keep:]
        if not past:
            return []
        stored = {row["day"] for row in self._db.execute("SELECT day FROM stats_snapshots")}
        return [day for day in reversed(past) if day not in stored][:MAX_FREEZE_PER_READ]

    @staticmethod
    def build_documents(goals: list[dict[str, Any]], events: list[dict[str, Any]], days: list[str]) -> list[tuple[str, dict[str, Any]]]:
        """One overview per day, each seeing only what had happened by the end of that day. Pure; runs on a worker thread.

        A day's document is "the numbers as that day ended", so it is built from the goals created and the
        events logged before the day was over, anchored at that moment. It used to be built from everything,
        which put today's activity into yesterday's frozen numbers and, for any day older than the trend's
        30-day span, left the day's own row out of its document, so the chart read "0 started" for days that
        had real goals. A goal whose status last changed after the day is shown as still in flight: its
        outcome had not happened yet, and the log does not keep the status it had then.
        """
        ordered_goals = sorted(goals, key=lambda g: g.get("created_at") or 0.0)
        goal_times = [g.get("created_at") or 0.0 for g in ordered_goals]
        ordered_events = sorted(events, key=lambda e: e.get("timestamp") or 0.0)
        event_times = [e.get("timestamp") or 0.0 for e in ordered_events]
        documents: list[tuple[str, dict[str, Any]]] = []
        for day in days:
            end = _day_end(day)
            seen_goals = []
            for g in ordered_goals[: bisect_left(goal_times, end)]:
                changed = g.get("updated_at") or g.get("created_at") or 0.0
                seen_goals.append({**g, "status": "RUNNING"} if changed >= end and g.get("status") not in _ACTIVE else g)
            documents.append((day, build_overview(seen_goals, ordered_events[: bisect_left(event_times, end)], window_days=0, now=end)))
        return documents

    def store(self, documents: list[tuple[str, dict[str, Any]]], now: float) -> str | None:
        """Write the frozen rows. The newest day written, or None when there was nothing to write."""
        newest: str | None = None
        for day, doc in documents:
            self._db.execute(
                "INSERT OR REPLACE INTO stats_snapshots (day, document, created_at) VALUES (?, ?, ?)",
                (day, json.dumps(doc), now),
            )
            newest = day if newest is None or day > newest else newest
        if newest is not None:
            self._db.commit()
        return newest

    def maybe_snapshot(self, conn: sqlite3.Connection, goals: list[dict[str, Any]], events: list[dict[str, Any]], now: float, keep: int | None = None) -> str | None:
        """Freeze the past days that have none, the first time anyone looks.

        Judged from each day's own activity (a goal or event stamped inside it), not from the wall clock's
        midnight: a machine that sleeps through the rollover still catches the day on its next read. A day
        with no activity has nothing to freeze and gets no row. The whole step in one synchronous call, for
        tests and for a caller with no loop to protect; the stats route does the same three steps itself
        (`pending_days`, `build_documents` on a worker thread, `store`) so the arithmetic never runs on the loop.
        """
        if not goals and not events:
            return None
        due = self.pending_days(goals, events, now, keep)
        return self.store(self.build_documents(goals, events, due), now) if due else None

    def get_day(self, day: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT document FROM stats_snapshots WHERE day = ?", (day,)
        ).fetchone()
        if row is None:
            return None
        try:
            document: dict[str, Any] = json.loads(row["document"])
            return document
        except (TypeError, ValueError):
            return None

    def list_days(self, before: str | None = None, limit: int = 365) -> list[str]:
        """Snapshot days oldest first, optionally only strictly before a day."""
        if before is None:
            rows = self._db.execute(
                "SELECT day FROM stats_snapshots ORDER BY day LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT day FROM stats_snapshots WHERE day < ? ORDER BY day LIMIT ?",
                (before, limit),
            ).fetchall()
        return [r["day"] for r in rows]

    def history(self, limit: int = 120) -> list[dict[str, Any]]:
        """Snapshot rows oldest first, documents already parsed.

        A limit of zero means every stored day, for the full-history export;
        positive limits keep the chart's bounded request behavior.

        A corrupt document reads as skipped rather than crashing the series —
        one bad row must not take the trend chart down with it.
        """
        if limit == 0:
            rows = self._db.execute(
                "SELECT day, document, created_at FROM stats_snapshots ORDER BY day DESC"
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT day, document, created_at FROM stats_snapshots ORDER BY day DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                doc = json.loads(row["document"])
            except (TypeError, ValueError):
                continue
            out.append({"day": row["day"], "document": doc, "created_at": row["created_at"]})
        out.reverse()
        return out

    def prune(self, keep: int | None = None) -> int:
        """Enforce the retention policy: keep the N most recent days.

        Count-based, not date-based, on purpose: a machine that sat idle for a
        month has three rows and nothing to prune, and "90 days of history"
        should mean 90 rows of history, not "whatever happened to fall inside
        the last 90 calendar days". 0 (or a negative) keeps everything — the
        value is "how many to keep", and the user who wants forever says so
        with a number the UI offers, not by pushing the bound to a magic
        sentinel the semantics then have to explain around. Returns the number
        of rows deleted, so a caller can log or test the effect; deletion runs
        in the same transaction shape as every write here (one statement, then
        commit), and a failure propagates — pruning runs in the same guarded
        block as freezing, so it can never fail a stats read.
        """
        if keep is None or keep <= 0:
            return 0
        rows = self._db.execute(
            "SELECT day FROM stats_snapshots ORDER BY day DESC LIMIT -1 OFFSET ?",
            (max(0, int(keep)),),
        ).fetchall()
        if not rows:
            return 0
        days = [r["day"] for r in rows]
        placeholders = ",".join("?" for _ in days)
        cur = self._db.execute(
            f"DELETE FROM stats_snapshots WHERE day IN ({placeholders})",
            days,
        )
        self._db.commit()
        return cur.rowcount
