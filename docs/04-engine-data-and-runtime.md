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
    created_at: float
```

`FileSystemService` MUST reject any path whose `realpath` is not under `root_path` (`03` §1.4).

### 1.2 Goal

```python
GoalStatus = Literal["PLANNING", "PENDING", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"]

class Goal(BaseModel):
    id: str
    workspace_id: str
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=20000)
    status: GoalStatus
    dry_run: bool = False
    version: int = Field(0, ge=0)
    created_at: float
    updated_at: float
```

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
    "file_change_summary", "agent_assigned", "error",
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

Payloads:

| type | payload |
|---|---|
| `goal_status` | `{status, version}` |
| `step_status` | `{status, review_notes?}` |
| `log` | `{level: "info"|"warn"|"error", message}` |
| `diff` | `{path, unified_diff}` |
| `test_result` | `{argv, verdict, explanation, exit_code?}` |
| `file_change_summary` | `{paths: [str], dry_run: bool}` |
| `agent_assigned` | `{role, provider, model}` |
| `error` | `{code: str, message: str}` |

`EventBus.next_sequence(goal_id)` is atomic (`UPDATE goals SET event_seq = event_seq + 1 ... RETURNING`).

## 2. SQL (`~/.codify/codify.db`)

WAL mode. `foreign_keys=ON`.

```sql
CREATE TABLE workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  root_path TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL
);

CREATE TABLE goals (
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
  updated_at REAL NOT NULL
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
| `GET` | `/workspaces` | — | `Workspace[]` |
| `GET` | `/workspaces/{id}` | — | `Workspace` |
| `POST` | `/goals` | `{workspace_id, title, description?, dry_run?}` extra=forbid | `Goal` |
| `GET` | `/goals/{id}` | — | `Goal` + `steps: PlanStep[]` |
| `POST` | `/goals/{id}/start` | `{expected_version}` extra=forbid | `Goal` |
| `POST` | `/goals/{id}/pause` | `{expected_version}` | `Goal` |
| `POST` | `/goals/{id}/cancel` | `{expected_version}` | `Goal` |
| `POST` | `/goals/{id}/steps/{step_id}/retry` | `{expected_version}` | `PlanStep` |
| `GET` | `/goals/{id}/events?after={seq}` | — | `Event[]` where `sequence > after` |
| `GET` | `/settings/providers` | — | `{builtins, custom}` (`01` §2.1) |

`retry`: only if step `FAILED` or (`IN_PROGRESS` and last review was `request-changes`). Resets step to `PENDING` then Executor runs Coder→… again. No agent overrides in body.

## 4. Agent JSON contracts

Parse with `json.loads`. Extra keys ignored. Missing required keys → `agent_output_invalid`.

### 4.1 Planner

```json
{"steps":[{"title":"…","description":"…","suggested_paths":["src/foo.py"]}]}
```

`1 ≤ len(steps) ≤ 20`. Paths relative, no `..`.

### 4.2 Coder

```json
{"files":[{"path":"src/foo.py","action":"update","content":"…"}]}
```

`action=delete` ⇒ `content` null. Paths contained by workspace.

### 4.3 Tester

```json
{"argv":["pytest","-q"],"verdict":"pass","explanation":"…"}
```

If `argv` non-null, Engine runs it **once** then calls Tester again with output and `argv: null` for the verdict. Second call MUST have `argv: null` (else `FAILED`, no third call).

### 4.4 Reviewer

```json
{"decision":"approve","reasons":[]}
```

or `request-changes` with ≥1 reason. See `01` §5.1.

### 4.5 Summarizer

```json
{"summary":"…","commit_message":"fix: …"}
```

## 5. Sandbox argv allowlist

`SandboxService.run_command(workspace, argv: list[str], timeout_s: int = 120)`.

Caller MUST be Tester path only. Coder/Planner/Reviewer/Summarizer output NEVER reaches this function.

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

Anything else → `403` `command_not_allowed`, step `FAILED`. No shell (`shell=False`).

## 6. Boot handshake

Engine stdout, first line, exactly:

```
CODIFY_ENGINE token=<hex> port=<int>
```

`token` = 32 bytes CSPRNG hex (64 chars). Desktop reads this line, then attaches `Authorization: Bearer <token>` to HTTP and `?token=` is **forbidden** (query leakage). WS: first text frame from client `{"type":"auth","token":"<hex>"}` or HTTP header on the Upgrade.

WS URL: `ws://127.0.0.1:<port>/ws/goals/{id}`. After auth, server sends events with `sequence > 0` live; client SHOULD `GET /goals/{id}/events?after=` for gap fill.

Token lives one Engine process. Rotated every spawn. Not written to disk.

## 7. Keyring

Service name `codify`. Username `codify/agents/{role}`. Linux: Secret Service; fallback error `keyring_unavailable` on Settings save (do not SQLite-store the key).
