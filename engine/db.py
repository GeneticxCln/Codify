from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from engine import home
from engine.models import DEFAULT_AGENTS, LEGACY_ROLE_RENAMES, AgentConfig

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

-- A conversation is a thread of turns, and it is the thing a tab points at.
-- It exists because the transcript used to be one array of React state: every
-- send was one goal, flat and unlabelled, and reloading the window lost the
-- thread. A goal is a *run* — a plan, steps, a verifier — while a conversation
-- is the question those runs answer, and they are not the same row.
--
-- Owned by the engine rather than the client, for the same reason the goal
-- history is: a conversation that lived in localStorage would vanish with the
-- browser profile, which is the exact defect this table replaces.
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  -- Empty until a turn gives it a name; the UI shows a placeholder rather than
  -- inventing a title.
  title TEXT NOT NULL DEFAULT '',
  -- 0 for an active conversation, 1 once it is archived. Archived rather than
  -- deleted so a goal's history stays attributable to the thread it came from.
  archived INTEGER NOT NULL DEFAULT 0,
  -- The conversation this one was branched from, or NULL when it is a
  -- top-level thread. A "new thread" made while another thread is showing is
  -- *that* thread's child: it is where "a thread on the current chat" is
  -- recorded, and without this column a new thread is indistinguishable from
  -- a brand-new chat. `ON DELETE SET NULL` so archiving a parent orphans its
  -- children rather than taking them with it — an archived thread's history
  -- stays readable, and so does the record of where each child came from.
  parent_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  -- Nullable: a goal that predates conversations reads as its own thread, so
  -- existing history is not orphaned by the migration.
  conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
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

-- Newest-first reads of the measurement events (`/stats/failures`, the stage and
-- role metrics) order by time across every goal. Without this they sort the
-- whole table on each request; with it SQLite walks back from the newest row and
-- stops once it has the rows it wants (235 ms -> 29 ms on 300,000 events).
CREATE INDEX IF NOT EXISTS idx_events_time ON events(timestamp, sequence);

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
  -- Ollama's context window for the role; NULL = the server default. Added
  -- after the fallback columns, so existing installs gain it by the ALTER below.
  num_ctx INTEGER,
  -- How long Ollama keeps the model resident; NULL = the server default.
  keep_alive TEXT,
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

-- One row per model call in a goal that was run with tracing on, so the run
-- can be replayed without a provider (docs/04 §8). The request is stored as
-- a digest rather than as text: a digest is what a replay has to match on, and
-- the prompt is the most sensitive thing in the run — the goal, the evidence
-- pack and the user's own source. `CODIFY_TRACE_PROMPTS=1` opts into storing
-- the text as well, for the case where "what exactly was the model handed" is
-- the question being asked. Responses are stored whole: a replay cannot serve
-- anything else, and a response is model prose about the user's code rather
-- than the code itself.
CREATE TABLE IF NOT EXISTS trace_calls (
  id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  step_id TEXT,
  seq INTEGER NOT NULL,
  role TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  temperature REAL,
  max_tokens INTEGER,
  prompt_hash TEXT NOT NULL,
  system_hash TEXT,
  system_prompt TEXT,
  user_prompt TEXT,
  response TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  duration_ms INTEGER,
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trace_calls_goal ON trace_calls(goal_id, seq);
-- The retention sweep deletes on created_at alone. Without this it walks the
-- whole table every time the stats screen opens, and the table is one row per
-- model call of every recorded run.
CREATE INDEX IF NOT EXISTS idx_trace_calls_created ON trace_calls(created_at);

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

-- The tabs that are open, in the order they are in. The one piece of the
-- window's state that means something *outside* it, so a second window opens
-- onto the same strip rather than onto its own copy of a profile.
--
-- A table and not a blob of JSON, because a shared strip has more than one
-- writer and a blob cannot say which tab a write meant: `key` is the tab's
-- durable identity (minted by the client, and the reason the strip is not keyed
-- by position — two windows have their own order until they sync), `position` is
-- the reading order, and a delete is a delete of one tab rather than a rewrite
-- of everything anybody else had open.
--
-- `payload` is opaque here on purpose, and that is the line this feature draws:
-- the engine holds *which tabs exist and in what order* — a fact it can reason
-- about, and the only one two windows share — while the client holds *what each
-- tab is showing* (an address, a back/forward stack), which is a fact only the
-- window showing it can use. The engine bounds it and checks that it is JSON;
-- it does not learn a tab's shape, because a store that learns a view's shape
-- is how the two get out of step. `localStorage` remains the mirror of this row
-- for the same reason it existed before this table: the strip must come back
-- when the engine does not answer.
CREATE TABLE IF NOT EXISTS shell_tabs (
  key TEXT PRIMARY KEY,
  position INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shell_tabs_position ON shell_tabs(position);

-- Agent memory, the durable half (docs/10 §6): one observation per workspace
-- and subject, refined by later runs rather than appended to. `proof` is how
-- many supporting events every scan including the first has seen; `evidence`
-- carries the most recent supporting event ids, newest first, so a claim can
-- always be checked against the rows that back it.
CREATE TABLE IF NOT EXISTS observations (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  subject TEXT NOT NULL,
  lesson TEXT NOT NULL,
  example TEXT NOT NULL DEFAULT '',
  proof INTEGER NOT NULL DEFAULT 1,
  evidence TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  refined_at REAL NOT NULL,
  UNIQUE (workspace_id, subject)
);

CREATE INDEX IF NOT EXISTS idx_observations_workspace
  ON observations(workspace_id, refined_at DESC);
"""


def default_db_path() -> Path:
    """Where the store lives, decided in `engine/home.py` and nowhere else."""
    return home.db_path()


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """`ALTER TABLE ... ADD COLUMN`, where "it is already there" is the quiet outcome.

    Migrations here are idempotent, so SQLite refusing because the column exists
    is expected and must not be an error. **Everything else it can say is.** The
    eight hand-rolled blocks this replaced all wrote `except Exception: pass`,
    which made "the column is already there" and "the database is locked", "the
    disk is full" and "the file is not a database" the same event: the engine
    started, and then read a column that had never been added, with the real
    cause gone. A migration that cannot run belongs at startup, where the
    message can still name the file.

    `table` and `column` are literals in this module, never model or user input —
    the SQL is assembled rather than parameterised for exactly that reason.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


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
    # ── migrations: an existing database gains this build's columns ──────
    # Every one of these goes through `_add_column`, whose only quiet outcome is
    # "the column is already there" — see its docstring for what the hand-rolled
    # `except Exception: pass` blocks hid.
    _add_column(conn, "goals", "provider", "TEXT")
    _add_column(conn, "goals", "model", "TEXT")
    _add_column(conn, "goals", "plan_only", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "goals", "parallel", "INTEGER NOT NULL DEFAULT 0")
    # A workspace's brand contract is a column on the workspace row, not a second
    # table: one row, one pin. An existing install gains it unset, so nothing is
    # requoted and no workspace loses its name or root.
    _add_column(conn, "workspaces", "design_contract_path", "TEXT NOT NULL DEFAULT ''")
    # Goal mode rides the same rule: an existing database gains the column
    # defaulted, so every old goal stays a 'normal' run.
    _add_column(conn, "goals", "mode", "TEXT NOT NULL DEFAULT 'normal'")
    # Tracing is opt-in per goal and off by default: a recording holds model
    # output about the user's code, so an existing install gains the column
    # defaulted to "record nothing" rather than starting to keep copies.
    _add_column(conn, "goals", "trace", "INTEGER NOT NULL DEFAULT 0")
    # A conversation is a thread of turns, and an install that predates it keeps
    # every goal with no thread: the column is nullable on purpose, so old
    # history renders as single-turn threads rather than disappearing.
    #
    # It carries the same foreign key the fresh schema declares. SQLite lets
    # `ADD COLUMN` take a `REFERENCES` clause as long as the default is NULL, and
    # without it an upgraded install kept dangling ids after a thread was
    # deleted while a fresh install nulled them.
    _add_column(
        conn, "goals", "conversation_id",
        "TEXT REFERENCES conversations(id) ON DELETE SET NULL",
    )
    # The index is created unconditionally — `CREATE INDEX IF NOT EXISTS` is the
    # idiom the schema already uses — because listing a conversation's turns is
    # the hot read of a tab switch and must not scan every goal.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_goals_conversation "
        "ON goals(conversation_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_workspace "
        "ON conversations(workspace_id, updated_at)"
    )
    # A thread on another thread needs somewhere to live. Nullable, and added
    # the same way as `goals.conversation_id` above: an existing install gains
    # the column unset, so every thread that already exists reads as a
    # top-level thread rather than disappearing or needing a wipe.
    #
    # This one was written strictly first, and for a reason that outlives it:
    # `_row_to_conversation` reads `parent_id` on *every* conversation read, so a
    # migration that quietly failed turned into an `IndexError` — a 500 on the
    # first request that lists a thread. That is now the rule for all of them
    # rather than the exception for one of them; see `_add_column`.
    _add_column(
        conn, "conversations", "parent_id",
        "TEXT REFERENCES conversations(id) ON DELETE SET NULL",
    )
    # The read a panel does on every render: a conversation's children.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_parent "
        "ON conversations(parent_id)"
    )
    # Adding a fallback target must not require wiping an install: an existing
    # database keeps every configured role and gains the columns unset. Same
    # shape as the `goals` migrations above — the column may already exist, and
    # that is the only failure SQLite reports here.
    for col, decl in (
        ("fallback_provider", "TEXT"),
        ("fallback_model_name", "TEXT NOT NULL DEFAULT ''"),
        ("fallback_protocol", "TEXT"),
        ("fallback_base_url", "TEXT"),
        # Ollama's context window for the role; NULL = the server default.
        ("num_ctx", "INTEGER"),
        # How long Ollama keeps the model resident after a request; NULL = the
        # server's own window, which is what an install that never set this wants.
        ("keep_alive", "TEXT"),
    ):
        _add_column(conn, "agent_configs", col, decl)
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
    """Insert one untouched row per role, ignoring any that already exist.

    The column list is **derived from the model**, not written out. It was a
    hand-written list, and a hand-written list is a second thing to forget: it
    went on naming fifteen columns when the model had seventeen, so `num_ctx`
    and `keep_alive` were silently absent and every seeded role came up NULL —
    which for `num_ctx` is not a null, it is Ollama's 4096 and a pipeline that
    truncates without saying so. Nothing failed; the field simply never arrived.

    Two symptoms of the same omission appeared in this repo before it was
    noticed, so the list is gone rather than corrected: a new field now reaches
    a fresh install by being declared on `AgentConfig`, which is the one place
    it has to be added anyway. `model_fields` is in declaration order and the
    values are read off the config by the same names, so the two cannot drift
    in step — only by both being wrong, which is the model being wrong, and
    that is the failure this is for.
    """
    columns = list(AgentConfig.model_fields)
    placeholders = ",".join("?" for _ in columns)
    # S608: the only interpolated values are `AgentConfig`'s own field names —
    # Python identifiers fixed at import time, never a request, an environment
    # variable or anything a user can reach. Every *value* is still a bound
    # parameter. Inlining the names to quiet this instead would be the copy that
    # rots, which is the thing the docstring above is about.
    sql = f"INSERT OR IGNORE INTO agent_configs ({','.join(columns)}) VALUES ({placeholders})"  # noqa: S608
    for cfg in DEFAULT_AGENTS:
        conn.execute(sql, tuple(getattr(cfg, name) for name in columns))


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))
