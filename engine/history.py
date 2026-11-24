"""Delete all history: everything the engine recorded from runs, and nothing a person set.

A fresh profile was the request ("a new user starts clean"). The line between the two halves is who wrote the row:

* **Cleared** — what the engine recorded while it worked: threads and their turns, every turn's events, plan steps,
  proposals and recordings, thread summaries, what a workspace learned (`observations`, docs/10), the suggestions
  derived from all of that, and the daily stats frozen from it. With the turns gone, a frozen day would be the one
  number left describing work that no longer exists. Shared chat tabs in `shell_tabs` stay open in their workspace
  with their `conversationId` cleared so no deleted thread id survives in the strip.
* **Kept** — what a person set or wrote: API keys, the eight roles, engine settings, MCP servers, workspaces, notes,
  imported stats, browser tabs and disabled skills. Files in a workspace are the person's and are never touched.

Every table is emptied explicitly rather than through the schema's cascades, so the counts are what was removed and
nothing depends on a per-connection `foreign_keys` PRAGMA.

**Deleted means gone from the file, not hidden.** A `DELETE` leaves the text in SQLite's free pages: measured with
the setting most SQLite builds ship (`secure_delete` off), 200 deleted turns left 10,690 copies of their text in
`codify.db`. `secure_delete` alone is not enough either: with it on, a bare `DELETE` left a reply on a table's root
page, and a row-by-row one left 21 copies where b-tree rebalancing had moved cells. So the file is rebuilt from what
remains (`VACUUM`) and the write-ahead log is truncated (`scrub_file`). That rewrite can be refused by another
connection still reading, and then the result says so rather than claiming a clean file.

Refused while a turn is running: a live driver would keep publishing events for goals that no longer exist.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from engine.services import ApiError

#: The phrase a person types to confirm. Checked by the engine as well as the screen, so no client clears
#: everything by sending an empty request.
CONFIRM_PHRASE = "delete all history"

# (key in the result, table). Children before parents, so the order also holds with foreign keys enforced.
_CLEARED: tuple[tuple[str, str], ...] = (
    ("events", "events"),
    ("plan_steps", "plan_steps"),
    ("proposals", "proposed_files"),
    ("recordings", "trace_calls"),
    ("summaries", "conversation_compactions"),
    ("suggestions", "suggestions"),
    ("observations", "observations"),
    ("stats_days", "stats_snapshots"),
    ("turns", "goals"),
    ("threads", "conversations"),
)


def _clear_chat_tab_threads(conn: sqlite3.Connection) -> None:
    """Drop `conversationId` from shared `chat` tab payloads so open tabs stay as clean slates with no stale ids."""
    rows = conn.execute("SELECT key, payload FROM shell_tabs WHERE kind = 'chat'").fetchall()
    for row in rows:
        try:
            parsed = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and "conversationId" in parsed:
            parsed.pop("conversationId", None)
            conn.execute(
                "UPDATE shell_tabs SET payload = ? WHERE key = ?",
                (json.dumps(parsed, separators=(",", ":")), row["key"]),
            )


def clear_history(conn: sqlite3.Connection, is_driving: Callable[[str], bool] | None = None) -> dict[str, Any]:
    """Empty every history table in one transaction and return how many rows each lost.

    `is_driving` is the executor's live driver check (`ExecutorService.is_driving`): a stored status can say a turn
    finished a moment before its driver lets go, as `DELETE /conversations/{id}` already knows. The rows are gone
    when this returns; `scrub_file` is what takes their text out of the file.
    """
    busy = [
        row["id"] for row in conn.execute("SELECT id, status FROM goals").fetchall()
        if row["status"] in ("PLANNING", "RUNNING") or (is_driving is not None and is_driving(row["id"]))
    ]
    if busy:
        raise ApiError(
            409, "thread_in_progress",
            "a turn is still running — cancel it before deleting all history",
            {"goal_ids": busy},
        )
    removed: dict[str, int] = {}
    try:
        for key, table in _CLEARED:
            removed[key] = int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608 — fixed names
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed names
        _clear_chat_tab_threads(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"deleted": True, "removed": removed, "files_touched": False}


def scrub_file(conn: sqlite3.Connection) -> bool:
    """Rebuild the database from its live rows and truncate the log, so deleted text is not left in either file.

    True when both happened. False when another connection held the file (SQLite's busy timeout ran out): the rows
    are still deleted, and the free pages are rewritten by the next successful `VACUUM`.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.execute("VACUUM")
        busy = int(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])
    except sqlite3.OperationalError:
        return False
    return busy == 0
