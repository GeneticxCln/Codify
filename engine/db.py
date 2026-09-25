from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from engine import home
from engine.models import DEFAULT_AGENTS, LEGACY_ROLE_RENAMES

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  root_path TEXT NOT NULL UNIQUE,
  -- Workspace-relative path of the pinned brand contract, '' when unpinned.
  design_contract_path TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 0,
  plan_only INTEGER NOT NULL DEFAULT 0,
  parallel INTEGER NOT NULL DEFAULT 0,
  -- What the goal is for: 'normal' pipeline or 'design' (the brand contract
  -- itself is the deliverable). One row, one mode; see docs/04 §4.0a.2.
  mode TEXT NOT NULL DEFAULT 'normal',
  version INTEGER NOT NULL DEFAULT 0,
  event_seq INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  provider TEXT,
  model TEXT
);

CREATE TABLE IF NOT EXISTS plan_steps (
  id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  suggested_paths TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  review_notes TEXT,
  commit_message TEXT,
  last_agent_role TEXT,
  UNIQUE (goal_id, ordinal)
);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  step_id TEXT,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  timestamp REAL NOT NULL,
  sequence INTEGER NOT NULL,
  UNIQUE (goal_id, sequence)
);

CREATE TABLE IF NOT EXISTS agent_configs (
  role TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  provider TEXT NOT NULL,
  protocol TEXT NOT NULL,
  model_name TEXT NOT NULL,
  api_key_ref TEXT,
  base_url TEXT,
  system_prompt_override TEXT,
  temperature REAL NOT NULL,
  max_tokens INTEGER NOT NULL,
  fallback_provider TEXT,
  fallback_model_name TEXT NOT NULL DEFAULT '',
  fallback_protocol TEXT,
  fallback_base_url TEXT,
  updated_at REAL NOT NULL
);

-- Files proposed by a dry-run goal, kept so "Apply" can write the exact
-- reviewed contents without re-asking the fixer.
CREATE TABLE IF NOT EXISTS proposed_files (
  id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  step_id TEXT NOT NULL,
  path TEXT NOT NULL,
  action TEXT NOT NULL,
  content TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS engine_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL
);

-- One frozen cross-goal statistics document per UTC day, written on the first
-- stats read after the day ends. Keeps the trend chartable across engine
-- restarts and independent of the event log's lifetime.
CREATE TABLE IF NOT EXISTS stats_snapshots (
  day TEXT PRIMARY KEY,
  document TEXT NOT NULL,
  created_at REAL NOT NULL
);

-- The currently-imported stats-history document, one row per frozen day.
-- Deliberately NOT a column on stats_snapshots: an imported day came from a
-- file the user chose on another machine, while a snapshot is a day this
-- engine froze itself. Merging them would make provenance unknowable and let
-- retention pruning silently discard data Codify never measured. One document
-- at a time, matching what the Stats panel holds in the UI, so importing
-- twice replaces rather than accumulates.
CREATE TABLE IF NOT EXISTS stats_imports (
  day TEXT PRIMARY KEY,
  document TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  imported_at REAL NOT NULL
);
"""


def default_db_path() -> Path:
    """Where the store lives, decided in `engine/home.py` and nowhere else."""
    return home.db_path()


def connect(
    path: Path | None = None,
    on_role_migrated: Any | None = None,
) -> sqlite3.Connection:
    """Open the store, bringing an existing one up to this build's role ids.

    `on_role_migrated(old_role, new_role)` is called for every role id that moved;
    the caller uses it to carry the role's stored credential across too. A failure
    in that callback must not stop the engine from starting, so it is swallowed.
    """
    db_path = path or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.is_dir():
        raise RuntimeError(f"database path is a directory: {db_path}")
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    # Migrate columns if existing db
    for col in ("provider", "model"):
        try:
            conn.execute(f"ALTER TABLE goals ADD COLUMN {col} TEXT")
        except Exception:
            pass
    try:
        conn.execute("ALTER TABLE goals ADD COLUMN plan_only INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE goals ADD COLUMN parallel INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    # A workspace's brand contract is a column on the workspace row, not a second
    # table: one row, one pin. An existing install gains it unset, so nothing is
    # requoted and no workspace loses its name or root.
    try:
        conn.execute(
            "ALTER TABLE workspaces ADD COLUMN design_contract_path TEXT NOT NULL DEFAULT ''"
        )
    except Exception:
        pass
    # Goal mode rides the same rule: an existing database gains the column
    # defaulted, so every old goal stays a 'normal' run.
    try:
        conn.execute(
            "ALTER TABLE goals ADD COLUMN mode TEXT NOT NULL DEFAULT 'normal'"
        )
    except Exception:
        pass
    # Adding a fallback target must not require wiping an install: an existing
    # database keeps every configured role and gains the columns unset. Same
    # shape as the `goals` migrations above — the column may already exist, and
    # that is the only failure SQLite reports here.
    for col, decl in (
        ("fallback_provider", "TEXT"),
        ("fallback_model_name", "TEXT NOT NULL DEFAULT ''"),
        ("fallback_protocol", "TEXT"),
        ("fallback_base_url", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE agent_configs ADD COLUMN {col} {decl}")
        except Exception:
            pass
    for old_role, new_role in migrate_agent_roles(conn):
        if on_role_migrated is not None:
            try:
                on_role_migrated(old_role, new_role)
            except Exception:
                pass
    seed_agents(conn)
    conn.commit()
    return conn


def migrate_agent_roles(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Carry configured roles over to the slot that inherited their job.

    The pipeline was renamed once: coder→fixer, tester→verifier, reviewer→critic,
    summarizer→scribe. An install that had configured its coder would otherwise
    come back with every new role unconfigured and a dead `coder` row sitting in
    the table forever, reported as "no model is configured" with no mention of
    the model it had actually been using.

    A row is moved only when the new slot is still the untouched seed
    (`updated_at = 0`); if the user has since configured the new slot, their
    choice wins and the stale row is simply dropped. Returns the pairs that moved
    so the caller can carry the keychain entry across as well.
    """
    known = {cfg.role for cfg in DEFAULT_AGENTS}
    # Keys are the role literals, but they are looked up with a plain `str`.
    display_names: dict[str, str] = {cfg.role: cfg.display_name for cfg in DEFAULT_AGENTS}
    moved: list[tuple[str, str]] = []

    for row in conn.execute("SELECT * FROM agent_configs").fetchall():
        try:
            role = row["role"]
        except (KeyError, IndexError, TypeError):
            continue
        if role in known:
            continue
        new_role = LEGACY_ROLE_RENAMES.get(role)
        target = (
            conn.execute(
                "SELECT updated_at FROM agent_configs WHERE role = ?", (new_role,)
            ).fetchone()
            if new_role
            else None
        )
        if new_role and target is None:
            # Nothing seeded under the new id yet: rename the row in place, so
            # every configured field survives untouched.
            conn.execute(
                "UPDATE agent_configs SET role = ?, display_name = ? WHERE role = ?",
                (new_role, display_names.get(new_role, new_role), role),
            )
            moved.append((role, new_role))
            continue
        if new_role and target is not None and not target["updated_at"]:
            conn.execute(
                """UPDATE agent_configs SET
                     provider = ?, protocol = ?, model_name = ?, api_key_ref = ?,
                     base_url = ?, system_prompt_override = ?, temperature = ?,
                     max_tokens = ?, fallback_provider = ?, fallback_model_name = ?,
                     fallback_protocol = ?, fallback_base_url = ?, updated_at = ?
                   WHERE role = ?""",
                (
                    row["provider"], row["protocol"], row["model_name"], row["api_key_ref"],
                    row["base_url"], row["system_prompt_override"], row["temperature"],
                    row["max_tokens"], row["fallback_provider"], row["fallback_model_name"],
                    row["fallback_protocol"], row["fallback_base_url"], row["updated_at"],
                    new_role,
                ),
            )
            moved.append((role, new_role))
        conn.execute("DELETE FROM agent_configs WHERE role = ?", (role,))

    conn.commit()
    return moved


def seed_agents(conn: sqlite3.Connection) -> None:
    for cfg in DEFAULT_AGENTS:
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_configs (
              role, display_name, provider, protocol, model_name,
              api_key_ref, base_url, system_prompt_override,
              temperature, max_tokens,
              fallback_provider, fallback_model_name, fallback_protocol, fallback_base_url,
              updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cfg.role,
                cfg.display_name,
                cfg.provider,
                cfg.protocol,
                cfg.model_name,
                cfg.api_key_ref,
                cfg.base_url,
                cfg.system_prompt_override,
                cfg.temperature,
                cfg.max_tokens,
                cfg.fallback_provider,
                cfg.fallback_model_name,
                cfg.fallback_protocol,
                cfg.fallback_base_url,
                cfg.updated_at,
            ),
        )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))
