# Codify — Engine Data & Runtime Contracts (v2)

Normative schemas, SQL, HTTP/WS, sandbox argv, boot handshake. Completes ultra-high detail for implementers.

## 1. Domain models

IDs: UUID v4 strings. Timestamps: Unix float seconds UTC.

### 1.1 Workspace

```python
class Workspace(BaseModel):
    id: str
    name: str = Field(..., min_length=1, max_length=120)
    root_path: str  # absolute, realpath'd, must exist and be a directory
    # Workspace-relative path of the pinned brand contract, "" when unpinned.
    # A column on this row, not a second table: one row, one pin, and an existing
    # install gains it unset (see §4.0a for how a contract is resolved).
    design_contract_path: str = ""
    created_at: float
```

`FileSystemService` MUST reject any path whose `realpath` is not under `root_path` (`03` §1.4).

**Nothing creates a workspace implicitly.** Startup never seeds one — not from the engine's working
directory, not from a default path. The engine's cwd is an implementation detail (for a source checkout
it is this repository, so an auto-seeded workspace would aim an agent at the app's own source tree),
and a target nobody chose is a target nobody reviewed. A workspace exists only because a user picked a
folder, so a fresh install has none and the UI asks for one before it will send anything.

### 1.2 Goal

```python
GoalStatus = Literal["PLANNING", "PENDING", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"]

# What the goal is for. "design" makes the workspace's own brand contract the
# deliverable — the design agent authors DESIGN.md instead of deriving a
# direction from one (§4.0a.2).
GoalMode = Literal["normal", "design"]

class Goal(BaseModel):
    id: str
    workspace_id: str
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=20000)
    status: GoalStatus
    dry_run: bool = False
    mode: GoalMode = "normal"
    version: int = Field(0, ge=0)
    created_at: float
    updated_at: float
```

`mode` is orthogonal to `dry_run` and `plan_only`: it says what the goal is *for*, not how it
executes. `GoalCreate.mode` defaults to `"normal"` and is validated by the model, so a client cannot
send a third value and get a goal that quietly runs the default pipeline — the literal rejects it
with a `422`.

`update_goal(id, expected_version, **fields)`:

```
UPDATE goals SET ..., version = version + 1, updated_at = :now
 WHERE id = :id AND version = :expected_version
```

0 rows → `409` `version_conflict` with current row. Callers re-read and retry or fail the step.

Transitions:

| From | To | Trigger |
|---|---|---|
| — | `PLANNING` | `POST /goals` |
| `PLANNING` | `PENDING` | planner succeeded |
| `PLANNING` | `FAILED` | planner invalid/error |
| `PENDING` | `RUNNING` | `POST /goals/{id}/start` |
| `RUNNING` | `COMPLETED` | all steps terminal success |
| `RUNNING` | `FAILED` | unrecoverable step / agent error |
| `RUNNING` | `PAUSED` | `POST /goals/{id}/pause` |
| `PAUSED` | `RUNNING` | `POST /goals/{id}/start` |
| `RUNNING`/`PAUSED`/`PENDING` | `CANCELLED` | `POST /goals/{id}/cancel` |

Illegal transition → `409` `illegal_status`.

### 1.3 PlanStep

```python
StepStatus = Literal["PENDING", "IN_PROGRESS", "COMPLETED", "FAILED"]

class PlanStep(BaseModel):
    id: str
    goal_id: str
    ordinal: int = Field(..., ge=0)
    title: str
    description: str
    suggested_paths: list[str] = []
    status: StepStatus = "PENDING"
    review_notes: Optional[str] = None
    commit_message: Optional[str] = None
    last_agent_role: Optional[AgentRole] = None
```

Unique `(goal_id, ordinal)`. Max 20 steps per goal (planner contract).

### 1.4 Event

```python
EventType = Literal[
    "goal_status", "step_status", "log", "diff", "test_result",
    "file_change_summary", "agent_assigned", "provider_fallback",
    "library_evidence", "design_contract", "plan_updated", "laya_decision",
    "fix_retry", "fixer_pass", "plan_consult",
    "agent_call_failed", "usage", "model_delta", "error",
]

class Event(BaseModel):
    id: str
    goal_id: str
    step_id: Optional[str] = None
    type: EventType
    payload: dict
    timestamp: float
    sequence: int  # per-goal, starts at 1, +1 per publish
```

Payloads — one row per type the engine publishes (verified against the
`publish` sites in `executor.py` / `services.py`; this table has drifted
before, so a new event type means a new row here in the same change):

| type | `step_id` | payload |
|---|---|---|
| `goal_status` | — | `{status, version}` |
| `step_status` | step | `{status, review_notes?, commit_message?}` — republished with `status: "IN_PROGRESS"` at every role transition inside a step (fixer → verifier → critic → scribe); only a start from a not-running state is a real attempt |
| `log` | any | `{level: "info"\|"warn"\|"error", message}` |
| `diff` | step | `{path, unified_diff, note?}` — `note` says why a real change has an empty diff (binary, or over the 1 MB cap) |
| `test_result` | step | `{argv, verdict, explanation, exit_code?, refused: [str], ran: bool, brand_drifts: [str]}` — `ran: false` with `argv: null` means nothing executed; `refused` lists every command the sandbox rejected; `brand_drifts` lists the engine's mechanical findings against a binding brand contract (empty unless one governs — see §4.3) |
| `file_change_summary` | step | `{paths: [str], dry_run: bool, unchanged: [str]}` — `unchanged` are paths whose proposal already matched the file ("already matched — left alone") |
| `agent_assigned` | any (`null` for laya) | `{role, provider, model}` — the model about to be called, published before the call |
| `provider_fallback` | any | `{role, from: {provider, model}, to: {provider, model}, code, detail}` |
| `library_evidence` | — | the checked evidence pack: `{summary, files: [{path, why, evidence}], symbols, conventions, test_command, risks, rounds, counts: {opened, matched, considered}, dropped_paths}` (`04` §4.0) |
| `design_contract` | — | the locked direction: `{applies, artifact, direction, design_system: {name, source, origin}, tokens: {colors: [{name, value}], typography: [{name, value}], spacing: [str], radii: [str]}, components: [{name, purpose}], conventions, constraints, acceptance, design_md, mode?}` — `origin` is `pinned`\|"discovered"\|`null` (`04` §4.0a). `mode: "design"` and a non-empty `design_md` mark a design-deliverable goal, where the body is the artifact rather than advice (`04` §4.0a.2) |
| `plan_updated` | step | `{step_id, step_title, fields: [str], changes: {field: {before, after}}}` — only fields the patch edited, only those whose value actually changed |
| `laya_decision` | — | the gate's full verdict: `{engine, answers, routing, blocked, block_reason, warnings, skipped_reason, provider, model, policy: {injection_block_threshold, risk_warn_level, clarify_warn_threshold}}` (`05`) |
| `fix_retry` | step | `{attempt, max_attempts, reason}` — a failing test run fed back to the fixer (bounded by `MAX_FIX_ATTEMPTS`) |
| `fixer_pass` | step | `{attempt, max_passes, passes_left}` — the fixer asked for another pass of its own (bounded by `MAX_FIXER_PASSES`) |
| `plan_consult` | — | `{refused, material_chars}` — the planner reopened the frozen evidence pack (`MAX_PLANNER_CONSULTS`) |
| `agent_call_failed` | any | `{role, provider, model, target: "primary"\|"fallback", code, message, duration_ms}` — a provider call that failed; the record the Settings screen's "last error" reads |
| `usage` | any | `{role, provider, model, duration_ms, input_tokens, output_tokens, total_tokens}` — one per successful model call; feeds `/goals/{id}/usage`, the audit document, and the stats rollups. `duration_ms` is absent on events written before it existed |
| `model_delta` | any | `{role, provider, model, text, final}` — a streaming snapshot of the reply so far (self-contained, ~every 400 ms); `final: true` closes the card. Chat-render only |
| `error` | any | `{code: str, message: str, role: str \| null}` |

`step_id` column: `—` marks goal-level events that never carry a step; `step`
marks step-scoped ones; `any` marks types published both ways (agent-level
events exist per role call, with `null` when the role runs at goal scope — the
librarian and laya — or per step, with the step's id when it runs inside one).

`error.role` names the role responsible when the engine knows it (`librarian`, `planner`,
`fixer`, `verifier`, `critic`, `scribe`), and is `null` for failures raised outside a role phase, such as a
sandbox refusal. It is what makes *why did this fail?* a lookup instead of a guess:
the chat's error block offers a **Why did this fail?** action that reads that role's
stored config, its provider's credential state, and the provider's live discovered
catalog, then ranks the findings so the cause is first and its symptoms below it.
See `ui/src/failureDiagnosis.ts` for the rules (a failed discovery proves nothing, so
it is never reported as "your model was retired").

`EventBus.next_sequence(goal_id)` is atomic (`UPDATE goals SET event_seq = event_seq + 1 ... RETURNING`).

## 2. SQL (`~/.codify/codify.db`)

### 2.0 Where the state directory is, and the isolated-run guarantee

Both stores resolve through `engine/home.py` — one module, one home — because resolving `~/.codify`
separately in two places made a redirected run only half hermetic. Precedence:

| Narrowest first | Effect |
|---|---|
| `CODIFY_DB`, `CODIFY_SECRETS` | that one store moves |
| `CODIFY_HOME` | **both** stores move under it |
| *(nothing set)* | `~/.codify` |

A run pointed at its own store also stops using the OS keychain (`home.keyring_allowed`): an isolated
run that can still read or write the developer's real credential store is not isolated. The same rule
covers a store handed straight to `Keychain(secrets_path=…)`, which is how the test suite builds one —
"use this file" means *this file*, not "prefer it".

`CODIFY_DB` alone deliberately does **not** disable the keychain: relocating a database says nothing
about where credentials belong, and silently moving someone's keys because they moved their database
would be a surprise rather than a safety net. Instead, `home.startup_notice()` — printed to stderr at
boot, since the Tauri shell parses the stdout handshake — states it outright:

```
state dir ~/.codify: database ~/.codify/codify.db
CODIFY_HOME /tmp/scratch — isolated: database /tmp/scratch/codify.db, credentials /tmp/scratch/secrets.json (the OS keychain is left untouched)
warning: CODIFY_DB is set but CODIFY_HOME is not — the database is redirected while credentials still resolve to the OS keychain and ~/.codify/secrets.json. Set CODIFY_HOME to redirect both.
```

Regression coverage: `tests/test_home.py` installs a *working* fake `keyring` module and asserts it
records zero reads and zero writes whenever the run was redirected, then asserts the real home
directory is still empty.

The suite obeys the same rule, because it is a scratch run too. `tests/hermetic.py` sets `CODIFY_HOME`
to a temp directory before any test asks the engine for a path — it has to be imported per module,
because `unittest discover -s tests` loads test modules as top level modules and never imports
`tests/__init__.py`. Before it existed, `test_provider_fallback` (which saves a role key to prove a
fallback does not carry the credential) rewrote a developer's real `secrets.json` on every `make test`,
which is where stray `codify/agents/*` entries in a real store came from.

WAL mode. `foreign_keys=ON`.

```sql
CREATE TABLE workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  root_path TEXT NOT NULL UNIQUE,
  -- Workspace-relative brand contract path, '' when unpinned. Added by
  -- ALTER TABLE for an existing database, keeping every workspace's name and
  -- root: a pin is an addition to a row, never a reason to rebuild it.
  design_contract_path TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

CREATE TABLE goals (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 0,
  plan_only INTEGER NOT NULL DEFAULT 0,
  parallel INTEGER NOT NULL DEFAULT 0,
  -- What the goal is for: 'normal' pipeline or 'design' (the brand contract
  -- itself is the deliverable, §4.0a.2). Added by ALTER TABLE for an existing
  -- database — every old goal stays a 'normal' run.
  mode TEXT NOT NULL DEFAULT 'normal',
  version INTEGER NOT NULL DEFAULT 0,
  event_seq INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE plan_steps (
  id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  suggested_paths TEXT NOT NULL DEFAULT '[]', -- JSON array
  status TEXT NOT NULL,
  review_notes TEXT,
  commit_message TEXT,
  last_agent_role TEXT,
  UNIQUE (goal_id, ordinal)
);

CREATE TABLE events (
  id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  step_id TEXT,
  type TEXT NOT NULL,
  payload TEXT NOT NULL, -- JSON object
  timestamp REAL NOT NULL,
  sequence INTEGER NOT NULL,
  UNIQUE (goal_id, sequence)
);

CREATE TABLE agent_configs (
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

CREATE TABLE proposed_files (
  id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  step_id TEXT NOT NULL,
  path TEXT NOT NULL,
  action TEXT NOT NULL,
  content TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE engine_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL
);

-- A day THIS engine froze from its own goals and events. Subject to the
-- count-based retention policy (`stats_retention_days`).
CREATE TABLE stats_snapshots (
  day TEXT PRIMARY KEY,
  document TEXT NOT NULL, -- the full overview document, JSON
  created_at REAL NOT NULL
);

-- The stats-history document the user imported from a file, one row per frozen
-- day. Deliberately NOT part of stats_snapshots: an imported day is another
-- machine's measurement, so merging the tables would make provenance unknowable
-- and let retention pruning delete data Codify never produced. One document at a
-- time — a new import replaces the previous set outright.
CREATE TABLE stats_imports (
  day TEXT PRIMARY KEY,
  document TEXT NOT NULL, -- one day entry: day, day_stats, goals, usage
  source TEXT NOT NULL DEFAULT '',
  imported_at REAL NOT NULL
);
```

Alembic revision `0001_init` creates these. Startup seeder inserts missing `DEFAULT_AGENTS` rows only.

## 3. HTTP (Engine)

Bind `127.0.0.1`. Port: first free in `7430-7440`, printed on stdout (`04` §6).

All routes: `Authorization: Bearer <boot_token>` or `401` `unauthorized`.

Error body: `{ "code": str, "message": str }`.

| Method | Path | Body | Success |
|---|---|---|---|
| `GET` | `/health` | — | `{ok:true}` (still requires Bearer) |
| `POST` | `/workspaces` | `{name, root_path}` extra=forbid | `Workspace` |
| `POST` | `/workspaces/browse` | — | `{cancelled} \| {cancelled:false, workspace}` (native folder picker) |
| `GET` | `/workspaces` | — | `Workspace[]` |
| `GET` | `/workspaces/{id}` | — | `Workspace` |
| `PUT` | `/workspaces/{id}/design-contract` | `{path}` extra=forbid (`""` unpins) | `Workspace`. 400 `design_contract_escape` (outside the root), `design_contract_missing` (no such file / a directory), `design_contract_binary`, `design_contract_unreadable`. Refused means untouched |
| `DELETE` | `/workspaces/{id}?delete_goals={bool}` | — | forgets the folder; **never touches `root_path`**. 409 `workspace_not_empty` (with the goal count) unless the cascade is requested, 409 `workspace_has_active_goals` if anything is PLANNING/RUNNING |
| `POST` | `/goals` | `{workspace_id, title, description?, dry_run?, plan_only?, parallel?, mode?, provider?, model?}` extra=forbid — `mode` is `"normal"` \| `"design"` (`04` §4.0a.2) | `Goal` |
| `GET` | `/goals` | query: `workspace_id?`, `status?`, `limit` (1–200, default 50), `offset` | `Goal[]` — active goals first, then newest |
| `GET` | `/goals/{id}` | — | `Goal` + `steps: PlanStep[]` |
| `DELETE` | `/goals/{id}` | — | deletes the run record; events/steps/proposals cascade, counts returned. 409 `goal_in_progress` while PLANNING/RUNNING or a driver holds it. Never touches files |
| `POST` | `/goals/{id}/start` | `{expected_version}` extra=forbid | `Goal` |
| `POST` | `/goals/{id}/pause` | `{expected_version}` | `Goal` |
| `POST` | `/goals/{id}/cancel` | `{expected_version}` | `Goal` |
| `PATCH` | `/goals/{id}/steps/{step_id}` | `{expected_version, title?, description?, suggested_paths?}` extra=forbid | `PlanStep` (PENDING goals only) |
| `POST` | `/goals/{id}/steps/{step_id}/retry` | `{expected_version}` | `PlanStep` |
| `GET` | `/goals/{id}/events?after={seq}` | — | `Event[]` where `sequence > after` |
| `GET` | `/goals/{id}/usage` | — | token totals + `parallel_peak`/`parallel_waves` (from `usage` events) |
| `GET` | `/goals/{id}/audit` | — | the goal's audit document (plan edits, fallbacks, fix retries, errors, outcomes, usage, silent roles) |
| `POST` | `/goals/{id}/apply` | `{expected_version}` | replays a completed dry-run's stored proposals for real |
| `POST` | `/goals/{id}/enable-execution` | `{expected_version}` | lifts the `plan_only` guard (`Goal`) |
| `GET` | `/stats/overview?window={1\|7\|30\|0}` | — | cross-goal outcomes, success rate, spend, daily trend (`engine/stats.py`). Bounded windows are anchored to the request's wall clock, so an idle install sees an empty window rather than its last run relabelled as recent |
| `GET` | `/stats/history?limit={0..730}` | — | frozen daily stats documents, oldest first; `limit=0` returns every stored day for JSON export |
| `GET` | `/stats/import` | — | the currently-imported history document (`{imported: false, days: []}` when none — a normal state, not a 404) |
| `POST` | `/stats/import` | `{exported_at, days[], source?}` | validates and **replaces** the stored import (`engine/stats_import.py`). 422 with a specific `code` (`duplicate_day`, `out_of_order`, `bad_day`, `empty`, `too_many_days`, `missing_exported_at`, `storage_failed`). Stored separately from `stats_snapshots` so retention can never prune imported data |
| `DELETE` | `/stats/import` | — | forgets the stored import; idempotent, returns the day count cleared |
| `GET` | `/settings/providers` | — | `{builtins, custom}` (`01` §2.1) |
| `GET` | `/settings/laya` | — | gate capability report (`sdk` \| `llm-fallback` \| `skipped`) |
| `GET` | `/settings/keys` | — | per-provider key status + `storage`/`storage_detail`/`storage_reason` (`04` §7) |
| `POST` | `/settings/keys` | `{provider, api_key}` | `{ok, provider, storage}` |
| `GET` | `/settings/agents` | — | `AgentConfig[]`, fixed role order |
| `GET` | `/settings/agents/stats` | query: `limit` | per-role last call / last error / counts, from the event log |
| `POST` | `/settings/agents/repair` | — | `RepairReport` (`04` §3.1) |
| `GET` | `/settings/agents/{role}` | — | `AgentConfig` |
| `PUT` | `/settings/agents/{role}` | `AgentConfigUpdate` | `AgentConfig` |
| `POST` | `/settings/agents/{role}/test-connection` | — | `{ok, message}` — a 15s liveness probe (`01` §3) |
| `GET` | `/settings/roles` | — | each role's `job` + `timing` (`01` §1) |
| `GET` | `/settings/engine` | — | engine-wide settings with clamp bounds (currently `parallel_width`) |
| `PUT` | `/settings/engine` | `{parallel_width}` | `{saved: {…}}` — echoes clamped values |
| `GET` | `/models?refresh=` | — | live-discovered catalog, per-provider status (`06`) |
| `GET` | `/models/recent?limit={1..25}` | — | `[{provider, model, role, ran_at}]`, newest first (`06` §3.1) |

Declaration order matters for the parameterised settings routes: `/settings/agents/stats` and `/settings/agents/repair` are registered **before** `/settings/agents/{role}`, or FastAPI's in-order matching would read `stats` and `repair` as role ids and 404/422 them.

`retry`: only if step `FAILED` or (`IN_PROGRESS` and last review was `request-changes`). Resets step to `PENDING` then the Executor runs fixer → verifier → critic → scribe again. No agent overrides in body.

### 3.0 What a step changes, reports, and commits

A step's file operations carry a `changed` flag, and only `changed` entries reach the chat or a commit:

| Case | `changed` | `unified_diff` | Commit |
|---|---|---|---|
| proposal matches the file's current contents | `false` | empty | none |
| delete of a file that is not there | `false` | empty | none |
| delete of an **empty** file | `true` | empty | yes |
| binary or > 1 MB file | `true` | empty, with `diff_note` saying why | yes |

So `file_change_summary` lists the changed paths under `paths` and the redundant ones under `unchanged`
("already matched — left alone"). A fixer can propose contents a file already has without knowing it; a
report that counted that as a touch, and handed the scribe a commit message for it, would be a claim the
engine had no evidence for.

`GitService.commit(root_path, message, paths)` takes the paths **and requires them**. It stages exactly
those (`git add -A -- <paths>`, filtered to paths that exist or are tracked, because a pathspec matching
nothing is fatal), then commits with the pathspec (`git commit -m <message> -- <paths>`) so a file the
user had staged for their own commit stays staged. An empty path list commits nothing at all. See `03` §1.7.

Reading for a prompt goes through `FileSystemService.read_text_or_none`, which answers `None` for a path
that escapes the workspace, a directory, a binary file (a NUL byte in the first 8 KB — decoding alone
cannot tell, since `b"\x00\x01binary"` is valid UTF-8), or undecodable bytes. The fixer is told which
suggested paths could not be read instead of the step dying on a planner's bad guess.

### 3.1 `POST /settings/agents/repair`

Points every role that **cannot run** at a model this engine has discovered, in one action, and changes nothing else. The rule lives in `engine/role_repair.py` as pure functions over (stored configs, provider key status, discovered catalog) — it is the engine's decision, not the client's, so the desktop app and the standalone browser build cannot disagree about which roles count as broken.

A role needs repair when **any** of these holds:

| Condition | Reason reported |
|---|---|
| no model id is stored | `no model is chosen` |
| its provider requires a credential and none is stored | `<provider> needs a credential and none is stored` |
| its provider answered **and** does not list the stored model | `<provider> no longer reports "<model>"` |

A role is left alone when it is usable — a model the provider still lists, or any model on a provider that needs no credential. **A provider that failed discovery proves nothing**: an unanswered provider is an unknown, not a fault, so its roles are neither repaired nor reported as retired (`<provider> did not answer, so nothing here is proven`). Repair is idempotent: a second call changes nothing and reports all roles as `left alone`.

The target is chosen from the catalog, never hardcoded:

1. the model that the working roles **already use**, if one model is used by more than one of them — consistency beats novelty;
2. otherwise the first model on a provider that **needs no credential** — the target that cannot fail for want of a key;
3. otherwise the first discovered model of any provider that answered.

Non-chat models a provider reports (embeddings, for instance) are not candidates. With an empty catalog nothing is changed and `target_reason` says so. Every role's `temperature`, `max_tokens`, endpoint, protocol, and system-prompt override are preserved: only provider and model move.

The response reports what it did **and** what it skipped, because an action that reports only its changes leaves you unable to tell "nothing needed fixing" from "it skipped something":

```jsonc
{
  "changed": true,
  "target": {"provider": "ollama", "model": "qwen2.5-coder:7b"},
  "target_reason": "the first model on ollama, which needs no credential",
  "repaired":   [{"role": "planner", "reason": "anthropic needs a credential and none is stored",
                  "provider": "ollama", "model": "qwen2.5-coder:7b"}],
  "unfixable":  [],
  "left_alone": [{"role": "fixer", "reason": "usable: ollama/qwen3:8b (verified in the catalog)"}],
  "notes": []
}
```

`unfixable` is what keeps the report from lying. Roles that **need** a model but that no discovered model can be pointed at appear there with their reasons; without it, a broken install with an empty catalog and a healthy install look identical, because both have `changed: false` and `repaired: []`. When nothing was proven broken (every role has a model and a usable provider) `target_reason` is empty — a target only matters to roles that need one, so its absence explains nothing.

`notes` entries are per provider, not per role: eight roles on an unreachable provider produce one caveat, not eight. An unverified role reads `left as configured: <provider>/<model> (not verified — <error>)`, never `usable` — the provider never said it works.

A successful repair **invalidates the model catalog cache** (the catalog is keyed by role configs) and bumps `version` on every changed role, so the `409` version guard still protects concurrent updates. Only `provider`, `model_name`, and the protocol the catalog reports are written; `temperature`, `max_tokens`, `base_url`, and any system-prompt override are preserved. The endpoint is deliberately **not** sent, so an unchanged provider keeps the endpoint the user configured (a proxy, say) while a provider switch resets it to that provider's default.

## 4. Agent JSON contracts

Parse with `json.loads`. Extra keys ignored. Missing required keys → `agent_output_invalid`.

### 4.0 Librarian

```json
{"summary":"…","files":[{"path":"src/foo.py","why":"…"}],
 "symbols":[{"name":"greet","path":"src/foo.py"}],"conventions":["…"],
 "test_command":["python","-m","pytest","-q"],"risks":["…"],
 "reads":["src/foo.py"],"searches":["greet"],"git":[["log","-5","--oneline"]],
 "run":[["ls","-la"]],"enough":false}
```

Every key is optional. The round ends when `enough` is `true` **or** all four request lists
(`reads` / `searches` / `git` / `run`) are empty; otherwise the engine serves the requests and calls
again, at most `MAX_LIBRARY_ROUNDS` (3) calls per goal. Per-round request caps: 12 reads, 6 searches,
6 git calls, 4 `run` commands, and `MAX_ROUND_CHARS` of material.

Two request entries may be plain values or small objects: a `reads` entry is a path string or
`{path, offset, limit}` (the line-range form for reaching the bottom half of a big file), and a
`searches` entry is a query string or `{query, regex, glob}`.

Requests are executed by `engine/library.py`:

- `reads` → `LibraryService.read`, capped at `MAX_READ_CHARS` and reporting `truncated`.
- `searches` → literal case-insensitive substring search by default, skipping VCS internals and
  package caches, capped at `MAX_MATCHES` / `MAX_FILES_SCANNED` and reporting both. A request may
  opt into regex with `{"query": …, "regex": true}` (and may narrow it with `glob`): a
  model-supplied pattern is untrusted input, so it is bounded at `MAX_REGEX_PATTERN` (200 chars)
  with a `PER_LINE_REGEX_SECONDS` (0.5s) per-file-line deadline against catastrophic backtracking,
  and an invalid or oversized pattern comes back as a refusal, not a crash.
- `git` / `run` → `SandboxService.run_command(mode="read_only")`: `ls`, `wc`, and a read-only git
  subcommand allowlist, with `-C`, `--git-dir`, `--work-tree`, `--output`, `-o`, `--ext-diff` and
  `--no-index` refused.

A refused request is **feedback, not failure**: the refusal is returned to the librarian (so it can
ask for something else) and logged at `warn`. The goal is unaffected.

Evidence is checked before it is published. A path in `files` is kept only when the librarian opened
it (`opened`), a search showed a matching line in it (`matched`), or it exists in the tree listing
(`listed`); anything else is dropped into `dropped_paths`, logged as `librarian cited N path(s) it
never saw`, and shown to the planner as unverified. `symbols` entries whose path is unsupported are
dropped the same way. `test_command` is carried **unverified** and the verifier is told so.

The pack is published as a `library_evidence` event:

```json
{"summary":"…","files":[{"path":"README.md","why":"…","evidence":"opened"}],
 "symbols":[{"name":"banner","path":"README.md"}],"conventions":["…"],
 "test_command":["python","-m","pytest","-q"],"risks":["…"],"rounds":2,
 "counts":{"opened":1,"matched":1,"considered":3},"dropped_paths":["src/ghost.py"]}
```

The planner and the fixer both receive `_evidence_text(pack)`, so a step is written against what the
repository actually says. The fixer still reads each `suggested_paths` file in full — the pack is
context, not a substitute for the file itself.

A librarian that fails (provider error, malformed reply) does **not** fail the goal: it is logged as a
warning and the planner is told there is no reconnaissance.

### 4.0a Design

```json
{"applies":true,"artifact":"web_prototype","direction":"…",
 "design_system":{"name":"acme-brand","source":null},
 "tokens":{"colors":[{"name":"ink","value":"#0d1117"}],
           "typography":[{"name":"body","value":"Inter, system-ui, sans-serif"}],
           "spacing":["4px","8px"],"radii":["6px"]},
 "components":[{"name":"KpiTile","purpose":"one metric with its trend"}],
 "conventions":["…"],"constraints":["…"],"acceptance":["…"],
 "design_md":"# acme-brand\n…"}
```

Runs once per goal, after the librarian and before the planner, so it locks a direction from evidence
rather than from a blank page. It has no tools: it decides, it does not touch the disk, so the fixer
stays the only writer. `applies: false`, or a reply that names no `direction`, is a legitimate answer
meaning *this goal changes no rendered surface* — the engine publishes nothing and the planner is told
there is no direction rather than handed an invented one.

**A brand contract the workspace already has wins over a proposal.** Resolution order, and the
`design_system.origin` each state publishes:

| `origin` | where it came from |
|---|---|
| `pinned` | `workspaces.design_contract_path` — a workspace-relative path set through `PUT /workspaces/{id}/design-contract`. Validated at set time, while the settings screen is open: an escape, a path that is not a file, or a binary blob is refused (`design_contract_escape` / `design_contract_missing` / `design_contract_binary`) rather than surfacing mid-goal as a mysterious absence of brand |
| `discovered` | a non-empty `DESIGN.md` at the workspace root, with nothing pinned. Zero-config is the point — a repository that already documents its brand should not have to be told twice — and one name in one place keeps it predictable: anything else (another name, a nested path, a tokens JSON) is what the pin is for |
| `null` | neither. The agent proposes a brand, and `design_md` may carry a body for the fixer to write |

The file's own text is handed over in full (`MAX_CONTRACT_FILE_CHARS`, truncation logged) and framed
as binding. Two things are then the engine's fact rather than the model's claim:

1. `design_system.source` is **stamped** with the path the engine resolved — a reply naming some
   other file does not get to relabel where the brand came from.
2. `design_md` is **dropped** whenever a contract file governs. A goal cannot answer a contract that
   exists as a file by writing a second one over it, which is exactly the drift the pin exists to
   stop.

A *pinned* path that has since become unreadable is a `warn` and a fall back to proposing (origin
`null`) — never a silent downgrade to convention, and never a failed goal. The planner, the fixer and
the critic all read the resolved contract through `_design_text`, where `pinned` and `discovered` are
labelled differently: one is the user's instruction, the other is a convention the engine noticed. The
**verifier** additionally checks the written artifacts against the binding contract mechanically (see
§4.3), so a drift is caught by evidence rather than by the critic's judgment alone.

Bounds — each list is trimmed, never dropped whole (`MAX_DESIGN_*` in `engine/executor.py`): 24 colors,
12 typography entries, 12 spacing steps, 8 radii, 40 components, 12 each of `conventions` /
`constraints` / `acceptance`, and `design_md` at 8000 chars. `artifact` is one of
`web_prototype|page|dashboard|deck|mobile|document|component|style_system|other`; anything else reads
as `other`, because that vocabulary is rendered by the app and is not a place to pass a model's
invention through. A row with no `name` is dropped — an unnamed token is a value nothing can
reference — and `design_system.source` is `null`, never `""`, when the workspace has no brand
contract, because "there is none" and "one I did not record" are different claims.

The normalized contract is published as a `design_contract` event and read back from the event log by
every step (`_design_for`), so the direction a step was written against survives the planning prompt —
including across processes. `_design_text(contract)` renders it for the roles that need it:

- the **planner** plans the steps that realize it, `DESIGN.md` step included when `design_md` is set;
- the **fixer** is told its tokens, components and `conventions` are binding;
- the **critic** judges the diff against `acceptance`, not against its own taste;
- the **verifier** is the one that checks the artifacts against it mechanically (§4.3), so the
  critic's review starts from the facts the engine already proved or disproved.

A design call that fails (provider error, malformed reply) does **not** fail the goal: it is logged as a
warning and the planner proceeds without a contract — the same rule the librarian gets. The design
agent cannot write, so a `DESIGN.md` body it emits is a file the fixer writes in its own step.

A goal whose `mode` is `"design"` inverts this relationship rather than varying it: the contract is
the deliverable, so the design agent authors the file instead of deriving a direction from one. That
path has its own rules — see §4.0a.2.

#### 4.0a.1 The verifier's mechanical check (`_brand_drifts`)

The verifier is the role that finds out what actually happened, so it is also the one that compares the
written artifacts against the contract — mechanically, engine-side, published on the same `test_result`
record as the verdict. Only what text comparison can *prove* is reported; everything else stays the
critic's judgment. The check runs when `design_system.origin` is `pinned` or `discovered` (the engine
resolved that file; a source the model merely claimed — origin `null` — is not enforced) and reads what
the fixer actually wrote: the applied diffs, falling back to the artifacts on disk when a change's diff
carries no text (a binary write), per-file bounded by `MAX_DRIFT_DIFF_CHARS` and capped at
`MAX_BRAND_DRIFTS` findings. The evidence is the *changes*, never the whole tree: a workspace already
full of the brand does not pass a step for that reason.

What it reports, each as a string in `brand_drifts`:

| Finding | When |
|---|---|
| `token color <name> <value> appears nowhere in the changes` | a contract color's value is absent from every written file (case-insensitive) |
| `typography <name> (<stack>) appears nowhere in the changes` | no word of the type stack (nor the token's name) appears |
| `none of the contract's spacing/radius tokens appear in the changes` | not one spacing or radius token is present |
| `acceptance not evidenced by any change: <line>` | no substantive word of the line (stopwords and ≤2-char tokens dropped, contract token names excluded) appears in any change |
| `constraint not evidenced by any change: <line>` | the same, for a constraint line |

**Advisory, by design.** The findings are stated — a `log` event at level `warn`, the `test_result`
payload, and the critic's prompt ("already checked — do not re-litigate") — but they never flip the
verdict: only the tests (or the critic) fail a step, and a text-matching heuristic is not the evidence
that should stop one. A proposed brand (origin `null`) is advice, not law, and draws no mechanical
findings.

#### 4.0a.2 Design-deliverable goals (`Goal.mode = "design"`)

A normal goal's design stage declares a direction the work realizes. A **design-deliverable goal**
inverts that: the workspace's own brand contract *is* what the goal produces, and the design agent is
its author. `POST /goals` with `mode: "design"` runs the same pipeline with four differences, all of
them in `engine/executor.py`:

| Stage | What changes |
|---|---|
| design | `_design_deliverable` runs instead of `_design`, with `DESIGN_BRIEF_PROMPT` (exported from `engine/default_prompts.py`) appended to the goal, the evidence pack, and — when `_brand_contract` resolves one — the current contract's text as *revision material*, not a law to obey. `_design_contract(out)` is called **without** the brand argument, so `design_md` survives: the body is the deliverable. A non-empty `design_md` is mandatory here — without one the stage raises `AgentOutputInvalid`, which the caller converts to the usual non-fatal `warn`, so the goal still plans but publishes no `design_contract` event. The event it does publish carries `mode: "design"`, and that is what every later stage keys on |
| planner | unchanged except for what it is handed: `_design_text` renders the body ("write it verbatim in its own step, do not paste it into other files"), so the plan includes the step that writes it |
| fixer | the step whose `suggested_paths` names a `design.md` path is handed the reviewed draft verbatim, with an explicit "write exactly this — add, drop or reword nothing" instruction. `suggested_paths` is the only honest signal a plan gives about intent, and it is the same signal the verifier reads |
| verifier | reviews the artifact instead of proposing a command: the librarian's `test_command` is not offered at all (there is no code to falsify), the DESIGN.md content is included in the prompt capped at `MAX_DESIGN_MD_CHARS`, and the verdict is asked for directly — `pass` when it is a complete, faithful realization of the contract, `fail` with the specific gaps when it is not, or `skip` when nothing was written *and* nothing proposed. The content comes from disk when it is there, and otherwise from the step's stored proposal (`GoalService.proposed_content`) — the same bytes Apply replays. Reviewing prose runs nothing, so the step still records `ran: false` with `argv: null` |
| critic | told the step delivers the workspace's DESIGN.md itself and that its approval is what puts the draft in front of the user — and **given the full content**, not only the diff, because a `+`-prefixed unified diff is a poor thing to approve a document from. It is read through the same helper the verifier uses (§4.0a.2), so the role that decides the step and the role that reports on it judge the same bytes. Brand drifts the verifier already proved are still handed over as settled (§4.0a.1) |

**The pin stays a user action.** Nothing in this pipeline writes `workspaces.design_contract_path`: the
engine drafts the file, a step writes it, the critic reviews it, and the user pins it
(`PUT /workspaces/{id}/design-contract`, §3) — review before authority, never a model awarding itself
one. The app holds the same line rather than trusting the pipeline to: the `design_contract` card
carries the draft and the pin action, and that action stays disabled until a step whose
`suggested_paths` names the file has reached `COMPLETED` — which is a state only the critic's approval
produces, since `request-changes` leaves the step `IN_PROGRESS` and pauses the goal. That covers the
*review*; the other precondition — that there is a file to bind — is checked separately, and both the
UI and the engine say so:

| State | What the card offers |
|---|---|
| no completed step naming the file | pin disabled, *"waiting on the review — a step must write it and the critic must approve it first"* |
| reviewed, but the goal was a dry run | pin disabled, *"this run proposed `<path>` without writing it — apply the goal, then pin it"* |
| reviewed and written | pin offered |

The two blocked states are deliberately distinct: the first is waiting, the second has a next action,
and one message for both would leave the user looking for a review that already happened. The engine
is still the authority — a path that is not a readable file in the workspace is refused
(`design_contract_missing`) whether the UI asked or a client did. A design goal in a workspace that already has a pinned or discovered contract is a *revision*: it
is shown what the workspace has today and may replace it, which is precisely what a normal goal may
not do (there, `design_md` is dropped so a goal cannot answer an existing brand with a competing
file, §4.0a).

Everything else holds unchanged: the design call is one bounded model call with no tools, the fixer is
still the only writer, and the critic is still the only role that can stop a step.

**A dry run reaches the disk not at all**, and a design goal makes that visible rather than subtle: the
draft is authored and planned as usual, but there is no file on disk to read. Neither judge is told to
skip, though — `_store_proposed_files` already persisted this step's proposal, so it is read back
(`GoalService.proposed_content`) and reviewed under a heading that says what it is: `DESIGN.md as
proposed by this step (nothing was written to disk)`. **Both judges read it that way**, through the one
helper `_deliverable_artifact(goal_id, step, ws_root)`: it returns the content and where it came from
(`"written"` or `"proposed"`), or `None` when there is genuinely nothing. The critic gets the full
content beside the diff, because the diff is the only place a dry run's content otherwise appears and
approving a document from `+`-prefixed lines is not a review. The reviewed bytes are byte-for-byte what
`POST /goals/{id}/apply` writes, so the review is of the real deliverable; `skip` is reserved for the
genuine absence, when the step wrote nothing *and* proposed nothing (an empty proposal is still a
proposal — an empty contract is a failure the reviewer has to be able to state). The change is recorded as a
proposal (`file_change_summary` with `dry_run: true`) and **nothing is committed** — Apply is what
writes the reviewed content and commits it, and only then does the pin hold. Pinning before that fails
the same way it fails mid-planning: `design_contract_missing`.

### 4.1 Planner

```json
{"steps":[{"title":"…","description":"…","suggested_paths":["src/foo.py"]}]}
```

`1 ≤ len(steps) ≤ 20`. Paths relative, no `..`. When a design contract is present it is binding
(§4.0a): the plan must realize its direction and tokens rather than substitute its own.

### 4.2 Fixer

```json
{"files":[{"path":"src/foo.py","action":"update","content":"…"}]}
```

`action=delete` ⇒ `content` null. Paths contained by workspace.

### 4.3 Verifier

```json
{"argv":["pytest","-q"],"verdict":"pass","explanation":"…"}
```

If `argv` non-null, Engine runs it **once** then calls the verifier again with output and `argv: null` for the verdict. After a command has run, a further `argv` MUST be `null` (else `agent_output_invalid`).

When the evidence pack carries a `test_command`, the verifier is offered it as *"the librarian reports
this repository's test command as … (unverified)"* — reading it out of a manifest is not proof it runs,
and the verifier is the role that finds out. **A design-deliverable step is the exception**: there is
no code to falsify, so no command is offered or allowed and the verifier reviews the DESIGN.md content
instead (§4.0a.2).

**A command the sandbox refuses is not a test failure — it is feedback.** Nothing ran, so the refusal is
handed back to the verifier exactly like command output is, and the verifier may propose a permitted
command instead or answer `verdict: "skip"` saying no permitted runner exists. Failing the goal here was
both unfair and destructive: a real run died with `command_not_allowed` over `touch …` *after* the fixer
had already written the change.

Bounds: a refusal executes nothing, so it does not consume the single-execution budget; refusals are
capped at `MAX_REFUSED_TEST_COMMANDS` (2), after which the verifier must answer with a verdict, so a step
costs at most 4 verifier calls.

The `test_result` payload records what really happened:

```json
{"argv":["git","status"],"verdict":"pass","explanation":"…","exit_code":0,
 "refused":["touch marker — binary not allowed: touch"],"ran":true,
 "brand_drifts":[]}
```

`ran` is `false` and `argv` `null` when nothing executed, and `refused` lists every rejected command — so
a "pass" with no command behind it is distinguishable from a suite that actually ran. Each refusal also
emits a `log` event at level `warn`.

`brand_drifts` is the engine's mechanical check of the written artifacts against a binding brand
contract (§4.0a.1): advisory findings, never a verdict change, and `[]` unless the contract's origin is
`pinned` or `discovered`.

### 4.4 Critic

```json
{"decision":"approve","reasons":[]}
```

or `request-changes` with ≥1 reason. See `01` §5.1.

### 4.5 Scribe

```json
{"summary":"…","commit_message":"fix: …"}
```

### 4.6 Which target a role is called on

Every agent call goes through `AgentOrchestrator.run_agent`, which builds the targets it may use — the
stored config first, then the role's fallback if it has one (`01` §2.2.1) — and walks them in order:

```
for each target:
    no model chosen?                  -> record; try the next target
    provider cannot be built?         -> record; try the next target
    publish agent_assigned(target)    -> the model about to be called, named
    call it
        ProviderError named in FALLBACK_TRIGGER_CODES?          -> record; try the next target
        ProviderError NOT named there?                          -> stop, keep its code
        reply is not the contract's JSON?                       -> record; try the next target
    parsed? return it
raise one error naming every attempt
```

`FALLBACK_TRIGGER_CODES` — the closed list of failures that mean "this target could not be used":

| Code | Means |
|---|---|
| `missing_api_key` | the provider needs a credential and none is stored |
| `unknown_protocol`, `invalid_base_url`, `secrets_unwritable` | the target cannot be constructed |
| `provider_http` | the provider answered with an error status (401, 404, 429, 5xx) |
| `provider_unreachable` | the connection was refused or timed out |
| `provider_bad_response` | the endpoint answered with something that is not JSON |
| `agent_output_invalid` | a model answered, but not in the shape the contract requires |

A failure outside that list is a defect in this engine, and running it on a second model would bury the
defect under a retry — so it stops the role exactly as it did before fallbacks existed. When a fallback
target is attempted, a `provider_fallback` event is published first and `agent_assigned` names the model
actually being called, so the transcript never credits an answer to the model that did not produce it.
The fallback is tried **at most once per agent call**: two targets, at most two calls.

If every target fails, the error keeps the **primary's** code and message, with the attempts appended:

```
planner cannot call anthropic: no key. Open Settings → Provider Keys to store a credential. Both
targets failed — primary anthropic (agent_not_configured): …; fallback ollama (provider_http): ….
```

With no fallback configured the raised error is byte-identical to the pre-fallback behaviour, code and
message included.

`provider_unreachable` and `provider_bad_response` are new codes from the same work: a refused
connection used to escape as a raw `httpx` exception and reach the goal as `internal_error` — a code
that blames Codify for a provider that is merely not listening. `providers.post_json` is the single
transport path that names both.

## 5. Sandbox argv allowlist

`SandboxService.run_command(workspace, argv: list[str], timeout_s: int = 120, mode="test")`.

Two callers, two modes, one validator:

- `mode="test"` — the verifier path only. Planner / fixer / critic / scribe output NEVER reaches this
  function with an executable argv.
- `mode="read_only"` — the librarian's `git` / `run` requests. `ls`, `wc` and a read-only git
  subcommand allowlist only, so nothing the librarian can do changes the workspace.

`argv[0]` basename only (no `/`). Resolved as `shutil.which` then executed with `cwd=workspace.root_path`, `env` stripped to `PATH`, `HOME`, `LANG`, `TERM`.

| argv[0] | Allowed remaining args |
|---|---|
| `pytest` | flags from `{ -q, -v, --tb=short, --no-header, --maxfail=N }` + paths under root |
| `python` | exactly `-m pytest` + pytest-allowed tail; OR exactly one script path under root ending `.py`. **FORBIDDEN:** `-c`, `-m` other than `pytest`, `-` |
| `npm` | `test` or `run` + script name matching `^[A-Za-z0-9_:-]+$` |
| `pnpm` | same as npm |
| `cargo` | `test` + optional `--`, `--lib`, `--bins`, `--quiet` |
| `go` | `test` + `./...` or paths under root |
| `git` | `status`, `diff`, `log -1` only (no write) |

Anything else → `command_not_allowed`. No shell (`shell=False`).

A refusal is **not** a step failure: the command never executed, and the verifier is the agent that can pick
another one, so the rejection goes back to it as feedback (`04` §4.3). The step only fails if the verifier
then reports an actual failure, or if it keeps proposing refused commands past the retry cap.

## 6. Boot handshake

Engine stdout, first line, exactly:

```
CODIFY_ENGINE token=<hex> port=<int>
```

`token` = 32 bytes CSPRNG hex (64 chars), created once and kept at `<state dir>/boot_token` (`0600`). Desktop reads this line, then attaches `Authorization: Bearer <token>` to HTTP and `?token=` is **forbidden** (query leakage). WS: first text frame from client `{"type":"auth","token":"<hex>"}` or HTTP header on the Upgrade.

WS URL: `ws://127.0.0.1:<port>/ws/goals/{id}`. After auth, server sends events with `sequence > 0` live; client SHOULD `GET /goals/{id}/events?after=` for gap fill.

Token lifetime is the state directory, not the process. It is written once, with `O_EXCL`, so two engines booting against the same state dir converge on one value rather than each minting its own. This replaced a per-spawn rotation: a client that cached the token was rejected with 401 after every restart, and only the desktop shell could recover, by re-reading the live handshake over IPC — a browser tab pointed at a dev engine has no shell to ask and stayed broken until a human reloaded it. The cost is a longer-lived credential, affordable only because the socket is `127.0.0.1`: anything able to present this token could read the file, and the database beside it, without it. `CODIFY_BOOT_TOKEN` overrides the value for a caller that wants a token scoped to one process, and a state directory that cannot be written falls back to a per-boot token rather than refusing to boot.

## 7. Key storage

Two backends, one namespace. Names: `codify` service + username `providers/{slug}` for a provider key,
and `codify/agents/{role}` for a role-scoped key. Keys are never written to SQLite and never echoed back.

1. **OS keychain** (preferred): `keyring` — Linux Secret Service, macOS Keychain, Windows Credential
   Manager. A `fail.Keyring` backend counts as *unavailable*: it accepts writes and raises on read, so
   the engine probes it once and falls back rather than reporting a key as saved that it cannot read.
2. **Local file** (fallback): `~/.codify/secrets.json`, mode `0600`, parent directory `0700`, written
   atomically (temp file + `os.replace`) so a crash cannot truncate the store. `CODIFY_SECRETS`
   overrides the path, and `CODIFY_HOME` moves this file together with the database (`04` §2.0).

The keychain is skipped entirely — and `storage_detail` says so — when the process was handed its own
store (`CODIFY_HOME` / `CODIFY_SECRETS`, or an explicit path). Without that wording the settings screen
would report the more familiar claim, "this machine has no usable keychain", which is a different and
wrong explanation for the same `storage: file`.

The fallback is not optional. Without it, a machine with no usable keyring — headless Linux, no Secret
Service, or `keyring` simply not installed in the interpreter that runs the engine (the usual state of
this checkout) — could never store a key at all: every provider reports "no API key configured"
forever and the only feedback is an error from the settings screen, which reads as "the app is broken"
rather than "this box has no keyring".

`GET /settings/keys` reports `storage` (`keyring` | `file`), a human `storage_detail`, and a
machine-readable `storage_reason` (`keyring` | `no_keyring` | `isolated_run`) per provider, and the
settings screen picks its wording from the reason, so it explains the store in force rather than
promising a keychain it did not use. A corrupted store reads as empty instead of crashing the
engine; an unwritable one raises `secrets_unwritable`; a keyring that fails at write time falls through
to the file instead of failing the save.
