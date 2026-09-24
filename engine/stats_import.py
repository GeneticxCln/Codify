"""Imported stats-history documents, kept so a download outlives the session.

The Stats panel could already *read* an exported history file: validate it,
merge its frozen days into the chart, and export again. What it could not do
was keep them. The import lived in React state, so closing the tab — or
restarting the engine — silently threw away every day the user had just
handed the app. This module is the durable half of that flow.

Two decisions carry most of the weight here.

**The import is a separate store, not extra rows in `stats_snapshots`.** A
snapshot is a day *this* engine froze from its own goals and events; an
imported day is a number another machine produced. Putting them in one table
would make the two indistinguishable, would let the retention policy
(`StatsSnapshotService.prune`, count-based over the newest N) quietly delete
data Codify never measured, and would let `maybe_snapshot` write a
same-day row over a user's imported one. They are different claims about the
past and stay apart.

**One document at a time.** The panel holds a single imported file, so the
store does too: importing again replaces the previous set outright rather than
accumulating. That keeps the table bounded, matches the UI's mental model
exactly, and means "clear imported" is a real operation with a real endpoint
rather than a client-side fiction.

Validation is deliberately strict and deliberately *pure* — no clock, no DB —
so the rules can be tested directly and cannot drift between what the engine
accepts and what the UI's own validator accepts. The engine re-validates
everything the client sends: a client that skipped the check, or a hand-edited
file, must not be able to inject a document the chart would then render as
measured fact.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

# Same shape the UI draws and the exporter writes. Kept as a small explicit
# allowlist rather than "whatever the object happens to contain": the document
# is persisted and later re-served, so anything accepted here becomes stored
# state that the chart will read back as a real day.
DAY_KEYS = ("day", "day_stats", "goals", "usage")
# `date` is deliberately absent: it is the string the day is keyed by, checked
# for agreement with `day` separately rather than as a number.
DAY_STATS_FIELDS = ("created", "succeeded", "failed", "cancelled", "total_tokens", "calls")
GOALS_FIELDS = ("goals", "active", "succeeded", "failed", "cancelled", "success_rate")
USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "calls", "avg_duration_ms")

# A UTC calendar day, exactly as `stats_history.utc_day` writes it. Validated
# here rather than trusted, because the day is the primary key: a malformed one
# would be stored and then silently sort wrong in every chart afterwards.
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A file name is user-supplied and gets echoed back into the UI. Cap it and
# keep it printable so it can never be used to smuggle control characters into
# a label, and so one absurdly long name cannot bloat a row.
MAX_SOURCE_CHARS = 200
# A bounded number of days per import. A file with a million rows is either
# corrupt or hostile; either way it is not a stats history.
MAX_IMPORT_DAYS = 730


class StatsImportInvalid(ValueError):
    """A submitted document is not a Codify stats-history export.

    Carries a `code` so the route can answer 422 with a stable machine-readable
    reason rather than a generic 400, matching how the rest of the engine names
    a refusal.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_count(value: Any) -> bool:
    """A non-negative integer. Booleans are rejected: `True` is not a count."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_number_or_none(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float)) and value == value and value not in (float("inf"), float("-inf"))


def _check_fields(raw: Any, fields: tuple[str, ...], label: str, *, nullable: tuple[str, ...] = ()) -> None:
    if not isinstance(raw, dict):
        raise StatsImportInvalid("bad_day", f"each day needs a {label} object")
    for key in fields:
        if key not in raw:
            raise StatsImportInvalid("bad_day", f"{label} is missing {key!r}")
        if key in nullable:
            if not _is_number_or_none(raw[key]):
                raise StatsImportInvalid("bad_day", f"{label}.{key} must be a number or null")
        elif not _is_count(raw[key]):
            raise StatsImportInvalid("bad_day", f"{label}.{key} must be a non-negative whole number")


def validate_import(value: Any) -> dict[str, Any]:
    """Validate a submitted export document and return a normalized copy.

    The returned `days` are rebuilt from the known keys only, so an unexpected
    field in a submitted file is dropped rather than persisted and re-served.

    Raises `StatsImportInvalid` with a specific reason — the same contract the
    UI's importer enforces, so a file that passes the panel's check cannot then
    be rejected by the engine for a different reason.
    """
    if not isinstance(value, dict):
        raise StatsImportInvalid("not_an_object", "expected a JSON object")
    exported_at = value.get("exported_at")
    if not isinstance(exported_at, str) or not exported_at.strip():
        raise StatsImportInvalid("missing_exported_at", "exported_at is required")
    days = value.get("days")
    if not isinstance(days, list):
        raise StatsImportInvalid("missing_days", "days must be an array")
    if not days:
        raise StatsImportInvalid("empty", "this export contains no frozen days")
    if len(days) > MAX_IMPORT_DAYS:
        raise StatsImportInvalid(
            "too_many_days",
            f"an import may hold at most {MAX_IMPORT_DAYS} frozen days (got {len(days)})",
        )

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, day in enumerate(days):
        if not isinstance(day, dict):
            raise StatsImportInvalid("bad_day", f"day #{index + 1} is not an object")
        name = day.get("day")
        if not isinstance(name, str) or not DAY_RE.match(name):
            raise StatsImportInvalid(
                "bad_day", f"day #{index + 1} needs a YYYY-MM-DD day, got {name!r}"
            )
        if name in seen:
            raise StatsImportInvalid("duplicate_day", f"day {name} appears more than once")
        seen.add(name)
        day_stats = day.get("day_stats")
        if not isinstance(day_stats, dict) or "date" not in day_stats or day_stats["date"] != name:
            raise StatsImportInvalid(
                "bad_day", f"day {name}: day_stats.date must be {name!r}"
            )
        _check_fields(day_stats, DAY_STATS_FIELDS, "day_stats")
        _check_fields(day.get("goals"), GOALS_FIELDS, "goals", nullable=("success_rate",))
        _check_fields(day.get("usage"), USAGE_FIELDS, "usage", nullable=("avg_duration_ms",))
        normalized.append({key: day[key] for key in DAY_KEYS})

    # Oldest-first is the exporter's contract and what the chart assumes. A
    # file in any other order is rejected rather than quietly sorted, because
    # reordering a user's data without being asked is exactly the kind of
    # silent repair this codebase refuses elsewhere.
    for index in range(1, len(normalized)):
        previous, current = normalized[index - 1]["day"], normalized[index]["day"]
        if current < previous:
            raise StatsImportInvalid(
                "out_of_order",
                f"days must be oldest first, but {current} appears after {previous}",
            )

    return {"exported_at": exported_at.strip(), "days": normalized}


def clean_source(name: Any) -> str:
    """A file name safe to store and display, or '' when there isn't one."""
    if not isinstance(name, str):
        return ""
    # Strip control characters and path separators: this is a label, not a path,
    # and it is echoed into a UI heading.
    text = "".join(ch for ch in name if ch.isprintable())
    text = text.replace("/", " ").replace("\\", " ").strip()
    return text[:MAX_SOURCE_CHARS]


class StatsImportService:
    """One imported document at a time, stored per day."""

    def __init__(self, conn: sqlite3.Connection):
        self._db = conn

    def get(self) -> dict[str, Any] | None:
        """The stored document, or None when nothing has been imported.

        Oldest first, matching the exporter and the chart's own order. A row
        that will not parse is skipped rather than taking the whole document
        down — one bad day should cost that day, not the import.
        """
        rows = self._db.execute(
            "SELECT day, document, source, imported_at FROM stats_imports ORDER BY day"
        ).fetchall()
        if not rows:
            return None
        days: list[dict[str, Any]] = []
        for row in rows:
            try:
                parsed = json.loads(row["document"])
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                days.append(parsed)
        if not days:
            return None
        return {
            "exported_at": days[0].get("exported_at", ""),
            "source": rows[0]["source"],
            "imported_at": rows[0]["imported_at"],
            "days": days,
        }

    def replace(self, document: Any, source: Any = "") -> dict[str, Any]:
        """Validate, then make `document` the current import.

        Replace-in-place rather than merge: the panel shows one imported file,
        and an import that accumulated would grow without bound and make
        "clear imported" ambiguous about what it clears.
        """
        clean = validate_import(document)
        now = time.time()
        label = clean_source(source)
        # One transaction: a failed write must not leave a half-replaced
        # document behind, which would render as a mysteriously shorter history.
        try:
            self._db.execute("DELETE FROM stats_imports")
            self._db.executemany(
                "INSERT INTO stats_imports (day, document, source, imported_at) VALUES (?, ?, ?, ?)",
                [
                    (
                        day["day"],
                        json.dumps({**day, "exported_at": clean["exported_at"]}),
                        label,
                        now,
                    )
                    for day in clean["days"]
                ],
            )
            self._db.commit()
        except sqlite3.Error as exc:
            self._db.rollback()
            raise StatsImportInvalid("storage_failed", f"could not store the import: {exc}") from exc
        return {"days": len(clean["days"]), "source": label, "imported_at": now}

    def clear(self) -> int:
        """Forget the current import. Returns the number of days removed."""
        cur = self._db.execute("DELETE FROM stats_imports")
        self._db.commit()
        return int(cur.rowcount or 0)

    def count(self) -> int:
        row = self._db.execute("SELECT count(*) FROM stats_imports").fetchone()
        return int(row[0]) if row else 0
