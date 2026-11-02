"""Notes a person keeps for every project, and the only memory that crosses a workspace boundary.

Per-workspace memory (`recall`, `recall_threads`, the observations table; docs/10) is scoped by `workspace_id` in SQL,
so one project's failures are never read into another's turns. That wall is the point of it, and these notes are
deliberately **not** a hole in it:

* They are written by a person (the settings routes, which need the boot token) or by a profile a person imported,
  never by the engine or a model. Nothing the engine observes becomes a note.
* They live in their own table and are read by their own tool, `read_notes`. `recall` cannot return one and
  `read_notes` cannot return an observation, so what a workspace learned stays in that workspace.
* They reach a run only while the `cross_workspace_notes` switch is on, and it is off on a fresh install. With it
  off the tool is not even offered.
* They are plain text a person can read, edit and delete; the list is short on purpose (`MAX_NOTES`).
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from typing import Any

from engine.services import ApiError

MAX_NOTES = 50
MAX_NOTE_CHARS = 500


class NotesService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._db = conn

    def list(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT id, text, source, created_at FROM global_notes ORDER BY created_at, id"
        ).fetchall()
        return [{"id": r["id"], "text": r["text"], "source": r["source"], "created_at": r["created_at"]} for r in rows]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) AS n FROM global_notes").fetchone()["n"])

    @staticmethod
    def clean(text: object) -> str:
        """A note as stored: trimmed, with runs of blank lines collapsed. Refused, never cut, when too long."""
        flat = "\n".join(line.rstrip() for line in str(text).replace("\r\n", "\n").strip().split("\n"))
        while "\n\n\n" in flat:
            flat = flat.replace("\n\n\n", "\n\n")
        if not flat:
            raise ApiError(422, "invalid_note", "a note needs some text")
        if len(flat) > MAX_NOTE_CHARS:
            raise ApiError(422, "invalid_note", f"a note is at most {MAX_NOTE_CHARS} characters")
        return flat

    def add(self, text: object, *, source: str = "person", now: float | None = None) -> dict[str, Any]:
        clean = self.clean(text)
        if self.count() >= MAX_NOTES:
            raise ApiError(409, "too_many_notes", f"at most {MAX_NOTES} notes are kept; delete one first")
        if self._db.execute("SELECT 1 FROM global_notes WHERE text = ?", (clean,)).fetchone() is not None:
            raise ApiError(409, "note_exists", "that note is already kept")
        note_id = str(uuid.uuid4())
        moment = time.time() if now is None else now
        self._db.execute(
            "INSERT INTO global_notes (id, text, source, created_at) VALUES (?, ?, ?, ?)",
            (note_id, clean, source if source in ("person", "imported") else "person", moment),
        )
        self._db.commit()
        return {"id": note_id, "text": clean, "source": source, "created_at": moment}

    def delete(self, note_id: str) -> None:
        cur = self._db.execute("DELETE FROM global_notes WHERE id = ?", (note_id,))
        self._db.commit()
        if cur.rowcount == 0:
            raise ApiError(404, "unknown_note", "note not found")
