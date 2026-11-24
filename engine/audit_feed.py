"""App-wide audit feed: cross-project list of what agents changed (commits, files, goals).

Read-only aggregation over the tables the engine already keeps (`goals`, `workspaces`,
`plan_steps`, and `events`). Runs off the event loop via `_read_off_the_loop`, the same way
`GET /stats/overview` does, and never touches the filesystem.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

from engine.models import loads_payload

_CANDIDATE_GOAL_CAP = 500
_COMMIT_LOG_RE = re.compile(r"^git committed ([0-9a-f]{7,40}): (.+)$")
_AUDIT_EVENT_TYPES = ("file_change_summary", "diff", "log")


def load_audit_changes(
    conn: sqlite3.Connection,
    *,
    workspace_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return recent goals that changed files or created commits across workspaces."""
    capped_limit = max(1, min(int(limit), 200))

    if workspace_id:
        goal_rows = conn.execute(
            """SELECT g.id, g.workspace_id, g.title, g.status, g.mode, g.dry_run,
                      g.created_at, g.updated_at,
                      COALESCE(w.name, g.workspace_id) AS workspace_name
               FROM goals g
               LEFT JOIN workspaces w ON w.id = g.workspace_id
               WHERE g.workspace_id = ?
               ORDER BY g.updated_at DESC, g.created_at DESC
               LIMIT ?""",
            (workspace_id, _CANDIDATE_GOAL_CAP),
        ).fetchall()
    else:
        goal_rows = conn.execute(
            """SELECT g.id, g.workspace_id, g.title, g.status, g.mode, g.dry_run,
                      g.created_at, g.updated_at,
                      COALESCE(w.name, g.workspace_id) AS workspace_name
               FROM goals g
               LEFT JOIN workspaces w ON w.id = g.workspace_id
               ORDER BY g.updated_at DESC, g.created_at DESC
               LIMIT ?""",
            (_CANDIDATE_GOAL_CAP,),
        ).fetchall()

    if not goal_rows:
        return {
            "generated_at": time.time(),
            "workspace_id": workspace_id,
            "total_goals": 0,
            "total_commits": 0,
            "total_files": 0,
            "entries": [],
        }

    goal_ids = [str(r["id"]) for r in goal_rows]
    placeholders = ",".join("?" for _ in goal_ids)

    step_rows = conn.execute(
        f"""SELECT id, goal_id, ordinal, title, status, commit_message
            FROM plan_steps
            WHERE goal_id IN ({placeholders})
            ORDER BY ordinal""",  # noqa: S608 — placeholders only
        goal_ids,
    ).fetchall()
    steps_by_goal: dict[str, list[dict[str, Any]]] = {}
    step_title_by_id: dict[str, str] = {}
    for row in step_rows:
        sid = str(row["id"])
        gid = str(row["goal_id"])
        title = str(row["title"] or "")
        step_title_by_id[sid] = title
        steps_by_goal.setdefault(gid, []).append(dict(row))

    ev_placeholders = ",".join("?" for _ in _AUDIT_EVENT_TYPES)
    event_rows = conn.execute(
        f"""SELECT goal_id, step_id, type, payload, timestamp, sequence
            FROM events
            WHERE goal_id IN ({placeholders})
              AND type IN ({ev_placeholders})
            ORDER BY sequence""",  # noqa: S608 — placeholders only
        [*goal_ids, *_AUDIT_EVENT_TYPES],
    ).fetchall()

    files_by_goal: dict[str, list[str]] = {}
    commits_by_goal: dict[str, list[dict[str, Any]]] = {}
    seen_commit_steps: dict[str, set[str]] = {}

    for ev in event_rows:
        gid = str(ev["goal_id"])
        step_id: str | None = str(ev["step_id"]) if ev["step_id"] else None
        ev_type = str(ev["type"])
        try:
            payload = loads_payload(ev["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue

        if ev_type == "file_change_summary":
            paths = payload.get("paths")
            if isinstance(paths, list):
                bucket = files_by_goal.setdefault(gid, [])
                for p in paths:
                    ps = str(p or "").strip()
                    if ps and ps not in bucket:
                        bucket.append(ps)
        elif ev_type == "diff":
            ps = str(payload.get("path") or "").strip()
            if ps:
                bucket = files_by_goal.setdefault(gid, [])
                if ps not in bucket:
                    bucket.append(ps)
        elif ev_type == "log":
            msg = str(payload.get("message") or "")
            m = _COMMIT_LOG_RE.match(msg)
            if m:
                sha, commit_msg = m.group(1), m.group(2)
                commits_by_goal.setdefault(gid, []).append(
                    {
                        "step_id": step_id,
                        "step_title": step_title_by_id.get(step_id, "") if step_id else None,
                        "sha": sha,
                        "message": commit_msg,
                        "at": ev["timestamp"],
                    }
                )
                if step_id:
                    seen_commit_steps.setdefault(gid, set()).add(step_id)

    entries: list[dict[str, Any]] = []
    for row in goal_rows:
        gid = str(row["id"])
        files = files_by_goal.get(gid, [])
        commits = list(commits_by_goal.get(gid, []))
        recorded_steps = seen_commit_steps.get(gid, set())

        # Include step-level commit_message entries when the log event was pruned by retention.
        for step in steps_by_goal.get(gid, []):
            sid = str(step["id"])
            cmsg = str(step.get("commit_message") or "").strip()
            if cmsg and sid not in recorded_steps:
                commits.append(
                    {
                        "step_id": sid,
                        "step_title": str(step.get("title") or "") or None,
                        "sha": None,
                        "message": cmsg,
                        "at": row["updated_at"],
                    }
                )

        if not files and not commits:
            continue

        entries.append(
            {
                "goal_id": gid,
                "workspace_id": str(row["workspace_id"]),
                "workspace_name": str(row["workspace_name"]),
                "title": str(row["title"] or ""),
                "status": str(row["status"] or ""),
                "mode": str(row["mode"] or "code"),
                "dry_run": bool(row["dry_run"]),
                "updated_at": float(row["updated_at"]),
                "commits": commits,
                "files": files,
                "files_count": len(files),
            }
        )
        if len(entries) >= capped_limit:
            break

    return {
        "generated_at": time.time(),
        "workspace_id": workspace_id,
        "total_goals": len(entries),
        "total_commits": sum(len(e["commits"]) for e in entries),
        "total_files": sum(int(e["files_count"]) for e in entries),
        "entries": entries,
    }
