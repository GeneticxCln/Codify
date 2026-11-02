from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, overload

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

-- Rolling summaries of a thread's earlier turns (engine/compaction.py). One
-- row per compaction, newest is live; the turns themselves are never rewritten.
-- `covers_through` is the created_at of the last replaced turn, so the boundary
-- survives a deleted turn and the prompt filter is a timestamp comparison.
CREATE TABLE IF NOT EXISTS conversation_compactions (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  summary TEXT NOT NULL,
  turns INTEGER NOT NULL,
  covers_through REAL NOT NULL,
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_compactions_thread
  ON conversation_compactions(conversation_id, created_at DESC);

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
  -- This goal's test commands may reach the network; off unless the person opted in (`sandbox_network`
  -- is the engine-wide half of the same switch).
  allow_network INTEGER NOT NULL DEFAULT 0,
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
  -- low / medium / high, or NULL for the provider's own default. A provider without a thinking control
  -- accepts the keyword and ignores it; the value is the role's request, not a guarantee.
  thinking_effort TEXT,
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

-- The skills one workspace has switched off. Per workspace rather than an
-- `engine_settings` row because the same name means different things in two
-- repositories: a project may want `ship-a-change` off while another keeps it,
-- and a workspace-only skill exists in one menu and not the other. Only the
-- disabled names are stored; enabled is the absence of a row, which is what a
-- fresh workspace and a re-enabled skill both are. The cascade is what makes
-- deleting a workspace leave no switches behind.
CREATE TABLE IF NOT EXISTS workspace_disabled_skills (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (workspace_id, name)
);

-- Remote MCP servers a person added in Settings. This is the admission list,
-- not a capability: no conductor tool reads these rows yet. `url` is always
-- https and the secret itself lives in the keychain under `api_key_ref`.
CREATE TABLE IF NOT EXISTS mcp_servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  auth_kind TEXT NOT NULL DEFAULT 'none',
  auth_header TEXT NOT NULL DEFAULT '',
  api_key_ref TEXT NOT NULL DEFAULT '',
  protocol_version TEXT NOT NULL DEFAULT '',
  server_name TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  last_probe_at REAL,
  last_probe_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

-- What the last successful `tools/list` found. A tool is listed here, never
-- callable: authority is NULL and enabled is 0 until the call path exists.
CREATE TABLE IF NOT EXISTS mcp_tools (
  id TEXT PRIMARY KEY,
  server_id TEXT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  input_schema_json TEXT NOT NULL DEFAULT '{}',
  schema_hash TEXT NOT NULL DEFAULT '',
  authority TEXT,
  enabled INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'new',
  last_seen_at REAL NOT NULL,
  UNIQUE (server_id, name)
);

CREATE INDEX IF NOT EXISTS idx_mcp_tools_server ON mcp_tools(server_id, name);

-- Things the engine noticed and a person may act on (docs/09 §10.22). A row is an offer, never an action: nothing
-- reads one except the inbox, and the only way one becomes work is a person sending its `prompt` as a turn.
-- One row per (workspace, kind, key), for good: a repeat of the same finding is the same row, so a detector that
-- runs every ten minutes cannot make ten of them, and a dismissed one stays dismissed for its cooldown.
-- `status` is open, accepted or dismissed; `snoozed_until` hides an open one until then. `goal_id` is the turn a
-- person made from it, set only by the accept route.
CREATE TABLE IF NOT EXISTS suggestions (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '[]',
  prompt TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  snoozed_until REAL,
  goal_id TEXT REFERENCES goals(id) ON DELETE SET NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (workspace_id, kind, key)
);

CREATE INDEX IF NOT EXISTS idx_suggestions_open ON suggestions(workspace_id, status, updated_at DESC);

-- Notes a person keeps for every project (engine/notes.py, docs/10 §8). Deliberately not tied to a workspace and
-- deliberately not the observations table: observations are what one workspace's own runs taught, and they never
-- leave it; a note is something the person wrote down to apply everywhere, and it reaches a run only when the
-- `cross_workspace_notes` switch is on. `source` says whether the person wrote it here or a profile brought it.
CREATE TABLE IF NOT EXISTS global_notes (
  id TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'person',
  created_at REAL NOT NULL
);
"""


#: Which shape of the store this build writes, kept in `PRAGMA user_version`.
#:
#: Every migration here is `ALTER TABLE ... ADD COLUMN` or `CREATE ... IF NOT EXISTS`, which an older engine sharing
#: the same `~/.codify` (a checkout beside the installed app) reads without harm, so the number does NOT move for
#: those. It moves, by one, in the commit that adds a migration an older build could misread: a dropped or retyped
#: column, a rewritten table, a changed meaning. `_prepare` refuses a file stamped higher than this, before it
#: touches it, and stamps an older or unstamped one after migrating it (docs/04).
SCHEMA_VERSION = 1


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


_CursorT = TypeVar("_CursorT", bound=sqlite3.Cursor)


class _ThreadSlot(threading.local):
    """The connection one thread owns; `None` until its first statement."""

    connection: sqlite3.Connection | None = None


class _PerThreadConnection(sqlite3.Connection):
    """The store's one handle, backed by one SQLite connection per thread.

    `connect` hands services a single object, and they use it from the event loop, from `asyncio.to_thread`
    workers, and from the daemon threads behind the gate and the conductor. SQLite serialises each C call,
    but a transaction is several of them — the implicit BEGIN, the statement, the COMMIT — and two threads
    interleaving on one connection mean one thread's `commit()` ends another's half-finished transaction,
    its `rollback()` undoes the wrong work, or (with an explicit `BEGIN`) the second thread is refused
    outright. A connection per thread makes each transaction its author's, and SQLite's own write lock plus
    the busy timeout order the commits across connections.

    A `sqlite3.Connection` subclass rather than a wrapper because every service and test types its store as
    `sqlite3.Connection`, and because the handle they pass around has to stay one object. The thread that
    created this one uses the connection it already is; any other thread gets a connection opened on first
    use, with the per-connection PRAGMAs (`foreign_keys`, `busy_timeout`) that `_prepare` set on the
    creator's. `cursor`, `in_transaction` and `close` are routed too, so no caller reaches the
    creator's connection from another thread through an API this wrapper forgot; `close` closes every
    connection this object opened, whichever thread asks.
    """

    def __init__(self, database: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(database, *args, **kwargs)
        self._database = database
        self._creator = threading.get_ident()
        self._local = _ThreadSlot()
        self._opened: list[sqlite3.Connection] = []
        self._opened_lock = threading.Lock()
        self._closed = False

    def _created_here(self) -> bool:
        return threading.get_ident() == self._creator

    def _other_thread_connection(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use and remembered for its next statement."""
        with self._opened_lock:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            connection = self._local.connection
            if connection is None:
                connection = sqlite3.connect(self._database, check_same_thread=False)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("PRAGMA foreign_keys = ON")
                self._local.connection = connection
                self._opened.append(connection)
            return connection

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        if self._created_here():
            return super().execute(*args, **kwargs)
        return self._other_thread_connection().execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        if self._created_here():
            return super().executemany(*args, **kwargs)
        return self._other_thread_connection().executemany(*args, **kwargs)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        if self._created_here():
            return super().executescript(*args, **kwargs)
        return self._other_thread_connection().executescript(*args, **kwargs)

    @overload
    def cursor(self, factory: None = ...) -> sqlite3.Cursor: ...

    @overload
    def cursor(self, factory: Callable[[sqlite3.Connection], _CursorT]) -> _CursorT: ...

    def cursor(
        self, factory: Callable[[sqlite3.Connection], sqlite3.Cursor] | None = None,
    ) -> sqlite3.Cursor:
        """A cursor on this thread's connection, not the creator's.

        `execute` was routed, but code that keeps a cursor and calls it later (`cursor.execute`,
        `cursor.executemany`) bypassed that and reached the creator's connection from a worker. A
        cursor belongs to the connection that made it, and a transaction begun through one belongs
        to the thread that owns that connection.
        """
        # `super().cursor`, not `self.cursor`: CPython before 3.12 implements
        # `Connection.execute` by calling the bound `cursor` method, so routing the
        # creator's thread back through `self` made every `execute` recurse on 3.10.
        if self._created_here():
            return super().cursor() if factory is None else super().cursor(factory)
        connection = self._other_thread_connection()
        return connection.cursor() if factory is None else connection.cursor(factory)

    @property
    def in_transaction(self) -> bool:
        """Whether *this thread's* connection has an open transaction.

        The inherited property reads the creator's connection. A worker that started `BEGIN` and
        asked the shared handle was told `False`, which is exactly the kind of answer that makes a
        caller commit or roll back at the wrong moment.
        """
        if self._created_here():
            return super().in_transaction
        return self._other_thread_connection().in_transaction

    def commit(self) -> None:
        if self._created_here():
            super().commit()
        else:
            self._other_thread_connection().commit()

    def rollback(self) -> None:
        if self._created_here():
            super().rollback()
        else:
            self._other_thread_connection().rollback()

    def close(self) -> None:
        with self._opened_lock:
            self._closed = True
            opened, self._opened = self._opened, []
        for connection in opened:
            connection.close()
        super().close()


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
    if db_path.parent == home.codify_home():
        # The state directory is ours: private. Anywhere else the *file* is ours and the
        # directory is whoever's it was (see `home.ensure_private_dir`).
        home.ensure_private_dir(db_path.parent)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.is_dir():
        raise RuntimeError(f"database path is a directory: {db_path}")
    home.ensure_private_file(db_path)
    conn = sqlite3.connect(
        db_path, check_same_thread=False, factory=_PerThreadConnection,
    )
    try:
        _prepare(conn, db_path, on_role_migrated)
    except BaseException:
        # A schema or migration that raises must not leave the handle it opened behind — nothing else
        # holds it, so nothing else will ever close it (review of 2026-09-29, finding 5).
        conn.close()
        raise
    return conn


def _prepare(conn: sqlite3.Connection, db_path: Path, on_role_migrated: Any | None) -> None:
    """Everything `connect` does to an open connection: the schema, the migrations, the seed."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    # Before the schema script, so a file from a newer build is refused untouched rather than migrated by a build
    # that does not know what the newer one changed.
    stamped = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if stamped > SCHEMA_VERSION:
        raise RuntimeError(
            f"the database at {db_path} was written by a newer Codify (schema {stamped}; this build reads up to "
            f"{SCHEMA_VERSION}). Update Codify, or point CODIFY_HOME at a different folder; nothing in the file was changed."
        )
    conn.executescript(SCHEMA)
    # SQLite gives the `-wal` and `-shm` files it creates the main file's mode, so a fresh
    # database needs nothing here. An older build's did not, and they exist by now.
    for suffix in ("-wal", "-shm"):
        home.tighten_to_owner(Path(f"{db_path}{suffix}"))
    # ── migrations: an existing database gains this build's columns ──────
    # Every one of these goes through `_add_column`, whose only quiet outcome is
    # "the column is already there" — see its docstring for what the hand-rolled
    # `except Exception: pass` blocks hid.
    _add_column(conn, "goals", "provider", "TEXT")
    _add_column(conn, "goals", "model", "TEXT")
    _add_column(conn, "goals", "plan_only", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "goals", "parallel", "INTEGER NOT NULL DEFAULT 0")
    # Networking is off unless a goal opts in; an existing install gains the column defaulted.
    _add_column(conn, "goals", "allow_network", "INTEGER NOT NULL DEFAULT 0")
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
    # A dry-run proposal names the version of the file it was resolved against, so Apply can
    # refuse to overwrite an edit made after the person reviewed it. `""` means the path did
    # not exist; NULL is a row an older build stored, with no version to compare — refusing
    # every old proposal would make a reviewed change unapplyable for no reason the person
    # could act on.
    _add_column(conn, "proposed_files", "base_version", "TEXT")
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
        # How much thinking the role asks for, where its provider has a control; NULL = provider default.
        ("thinking_effort", "TEXT"),
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
    # `PRAGMA` takes no bound parameter; the value is this module's own integer.
    conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")


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
