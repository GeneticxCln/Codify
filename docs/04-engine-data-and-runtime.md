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

**Nothing creates a workspace implicitly.** Startup never seeds one — not from the engine's working
directory, not from a default path. The engine's cwd is an implementation detail (for a source checkout
it is this repository, so an auto-seeded workspace would aim an agent at the app's own source tree),
and a target nobody chose is a target nobody reviewed. A workspace exists only because a user picked a
folder, so a fresh install has none and the UI asks for one before it will send anything.

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
    "file_change_summary", "agent_assigned", "provider_fallback", "error",
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
| `provider_fallback` | `{role, from: {provider, model}, to: {provider, model}, code, detail}` |
| `error` | `{code: str, message: str, role: str \| null}` |

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
  fallback_provider TEXT,
  fallback_model_name TEXT NOT NULL DEFAULT '',
  fallback_protocol TEXT,
  fallback_base_url TEXT,
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
| `POST` | `/settings/agents/repair` | — | `RepairReport` (`04` §3.1) |
| `GET` | `/goals/{id}/events?after={seq}` | — | `Event[]` where `sequence > after` |
| `GET` | `/settings/providers` | — | `{builtins, custom}` (`01` §2.1) |
| `GET` | `/models/recent?limit={1..25}` | — | `[{provider, model, role, ran_at}]`, newest first (`06` §3.1) |

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

`notes` entries are per provider, not per role: seven roles on an unreachable provider produce one caveat, not seven. An unverified role reads `left as configured: <provider>/<model> (not verified — <error>)`, never `usable` — the provider never said it works.

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
6 git calls, 4 inspect commands, and `MAX_ROUND_CHARS` of material.

Requests are executed by `engine/library.py`:

- `reads` → `LibraryService.read`, capped at `MAX_READ_CHARS` and reporting `truncated`.
- `searches` → literal case-insensitive substring search (never a model-supplied regex), skipping
  VCS internals and package caches, capped at `MAX_MATCHES` / `MAX_FILES_SCANNED` and reporting both.
- `git` / `run` → `SandboxService.run_command(mode="read_only")`: `ls`, `wc`, and a read-only git
  subcommand allowlist, with `-C`, `--git-dir`, `--output`, `-o`, `--ext-diff` and `--no-index`
  refused.

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

### 4.1 Planner

```json
{"steps":[{"title":"…","description":"…","suggested_paths":["src/foo.py"]}]}
```

`1 ≤ len(steps) ≤ 20`. Paths relative, no `..`.

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
and the verifier is the role that finds out.

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
 "refused":["touch marker — binary not allowed: touch"],"ran":true}
```

`ran` is `false` and `argv` `null` when nothing executed, and `refused` lists every rejected command — so
a "pass" with no command behind it is distinguishable from a suite that actually ran. Each refusal also
emits a `log` event at level `warn`.

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

`token` = 32 bytes CSPRNG hex (64 chars). Desktop reads this line, then attaches `Authorization: Bearer <token>` to HTTP and `?token=` is **forbidden** (query leakage). WS: first text frame from client `{"type":"auth","token":"<hex>"}` or HTTP header on the Upgrade.

WS URL: `ws://127.0.0.1:<port>/ws/goals/{id}`. After auth, server sends events with `sequence > 0` live; client SHOULD `GET /goals/{id}/events?after=` for gap fill.

Token lives one Engine process. Rotated every spawn. Not written to disk.

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
