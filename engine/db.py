from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from engine.models import DEFAULT_AGENTS

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  root_path TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 0,
  event_seq INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
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
  updated_at REAL NOT NULL
);
"""


def default_db_path() -> Path:
    override = os.environ.get("CODIFY_DB")
    if override:
        return Path(override)
    return Path.home() / ".codify" / "codify.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    seed_agents(conn)
    conn.commit()
    return conn


def seed_agents(conn: sqlite3.Connection) -> None:
    for cfg in DEFAULT_AGENTS:
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_configs (
              role, display_name, provider, protocol, model_name,
              api_key_ref, base_url, system_prompt_override,
              temperature, max_tokens, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                cfg.updated_at,
            ),
        )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))
