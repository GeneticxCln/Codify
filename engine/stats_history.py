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
from datetime import datetime, timezone

from engine.stats import build_overview


def utc_day(ts: float) -> str:
    """The UTC calendar day a timestamp belongs to — one function, so the
    snapshot boundary and the test doubles agree to the hour."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class StatsSnapshotService:
    """Freezes one document per UTC day, on the first read after it ends."""

    def __init__(self, conn: sqlite3.Connection):
        self._db = conn

    def maybe_snapshot(self, conn: sqlite3.Connection, goals: list[dict], events: list[dict], now: float) -> str | None:
        """Freeze yesterday's final numbers, the first time anyone looks today.

        Yesterday is judged from its own last activity (a goal or event stamped
        inside it), not from the wall clock's midnight: a machine that sleeps
        through the rollover still catches the day on its next read. A day with
        no activity has nothing to freeze and gets no row — an empty day is
        already faithfully represented by the days that do exist.
        """
        if not goals and not events:
            return None
        today = utc_day(now)
        # Backfill every unfrozen past day with activity, oldest first — the
        # old single-day version froze only latest_activity's day, so two
        # missed days left the older one unfrozen forever.
        active_days = sorted({
            utc_day(g.get("updated_at") or g.get("created_at") or 0.0) for g in goals
        } | {
            utc_day(e.get("timestamp") or 0.0) for e in events
        })
        frozen: str | None = None
        for day in active_days:
            if day >= today:
                continue
            if self.get_day(day) is not None:
                continue
            doc = build_overview(goals, events, window_days=0)
            self._db.execute(
                "INSERT OR REPLACE INTO stats_snapshots (day, document, created_at) VALUES (?, ?, ?)",
                (day, json.dumps(doc), now),
            )
            frozen = day
        if frozen is not None:
            self._db.commit()
        return frozen

    def get_day(self, day: str) -> dict | None:
        row = self._db.execute(
            "SELECT document FROM stats_snapshots WHERE day = ?", (day,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["document"])
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

    def history(self, limit: int = 120) -> list[dict]:
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
        out: list[dict] = []
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
