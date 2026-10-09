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
# direction from one (§4.0a.2), and the workspace's own written-down knowledge
# from the other (§4.9).
GoalMode = Literal["normal", "design", "knowledge"]

class Goal(BaseModel):
    id: str
    workspace_id: str
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=20000)
    status: GoalStatus
    dry_run: bool = False
    mode: GoalMode = "normal"
    # Record this run's model calls so it can be replayed (§8). Set when the
    # goal is created, or turned on with `PUT /goals/{id}/trace` while it is
    # still PLANNING. It never turns itself on, and turning it off is allowed
    # at any status.
    trace: bool = False
    version: int = Field(0, ge=0)
    created_at: float
    updated_at: float
```

`mode` is orthogonal to `dry_run` and `plan_only`: it says what the goal is *for*, not how it
executes. `GoalCreate.mode` defaults to `"normal"` and is validated by the model, so a client cannot
send a fourth value and get a goal that quietly runs the default pipeline — the literal rejects it
with a `422`.

`"design"` and `"knowledge"` are one shape pointed at two files, and the difference is only which
one: a **deliverable** goal has the design agent author a document, a step write it verbatim, and
the critic review it before anyone relies on it. `"design"` writes `DESIGN.md`, the contract every
later goal obeys; `"knowledge"` writes `CODIFY.md`, the prior every later run's librarian reads
(§4.9). `DELIVERABLE_FILES` and `DELIVERABLE_ROLE` in `engine/executor_design.py` hold both, so the fixer,
the verifier and the critic cannot disagree about which file is being delivered.

`trace` is orthogonal to all three: it says whether the run's model calls are *recorded* (§8).
`GoalCreate.trace` defaults to `False`, and because it is a copy of the model's output about the
user's code there is no way to set it anywhere except explicitly — it is never inferred from a mode,
a setting, or a previous run.

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

**A terminal status is final, with two documented exceptions.** `GoalService.update_status` refuses to move a
goal out of `COMPLETED`, `FAILED` or `CANCELLED` — except `COMPLETED → RUNNING` and `FAILED → RUNNING`, which
are how `POST /goals/{id}/apply` and the step retry re-open a finished goal. Nothing re-opens `CANCELLED`:
retry, apply and start all refuse it. Repeating the status a goal already has is not a move. This is the
rule that keeps a runner that finishes after a Cancel from undoing it: `run_chat` used to end with an
unconditional `COMPLETED` that overwrote `CANCELLED` and published the reply anyway (audit of 2026-09-29,
M1). Alongside it, `ExecutorService._set_status` is a quiet no-op on a cancelled goal (so a runner that lost
the race does not crash a background task), the conductor asks "cancelled?" before every model call and every
tool call (`Conductor(cancelled=...)`, so a Cancel takes effect within one call), and the step driver only
drives a `RUNNING` goal (M2: a cancel during a retried step used to be followed by a full conductor run).

**A Cancel also stops what is already running.** Every background run of a goal (a turn, planning, a start, a
retry, an apply) is held by the goal it works for (`_spawn`), and `POST /goals/{id}/cancel`, once the status is
stored, cancels them: a model call that has not answered is abandoned, a stage records its outcome as
`cancelled`, and the driver is released by its own `finally`. A command on a worker thread is not reached by
that, so each goal has a cancel signal (`ExecutorService.cancel_signal`) that every sandbox call it makes is
handed — the verifier's, the critic's read-only probes and the conductor's `run_command` — and the sandbox stops
the command's whole process group as soon as it is set (exit `130`, below). A step that was mid-way stays as it
was left; nothing re-opens `CANCELLED`. A refused cancel (`version_conflict`, `illegal_status`) stops nothing.
**Pause does not do this**: it asks for a stop between steps, and Start resumes there
(`tests/test_cancel_stops_work.py`).

**One driver per goal, held for the whole run** (review of 2026-09-29, finding 2). `run_chat` and `run_planning`
claim the goal's driver (`claim_driver`) for as long as they run, as `start`, `retry` and `apply` already did.
The conductor's `plan` move leaves the goal `PENDING` while the turn goes on to write its answer, and `PENDING` is
what Start accepts — so without the claim, Start then began a second driver on a goal whose turn was still going
(two conductors writing the same steps, the turn's own `write` gate opening the moment the status read `RUNNING`),
and Delete was allowed. Now `POST /goals/{id}/start` answers `409 driver_busy` while a turn or a planning run
holds the goal, `DELETE` answers `409 goal_in_progress`, and Cancel — deliberately — stays allowed.

**A goal has one plan.** `plan_steps` is unique on `(goal_id, ordinal)`, so a second plan did not append: it
raised a raw `IntegrityError` after the first plan was written and failed the goal as `internal_error`. It is now
refused before it starts: the pipeline skips a goal that has steps, the conductor's `plan` move answers "this goal
already has a plan", and a conductor whose provider fails *after* `plan` (`_Conducted.planned` is read from the
rows, not assumed) leaves the plan standing — `PENDING`, waiting for approval — instead of running the standard
sequence over it.

**The write gate is asked at the write.** The conductor's `write` and the step runner check approval before the
fixer's model call, and a local model takes minutes to answer. `_fixer` asks again immediately before `fs.apply`
(`_approval_withdrawn`): a goal the person has `CANCELLED` or `PAUSED`, deleted, or switched back to plan-only
gets no write — the reply is discarded, nothing is written, the step stays as it was, and it is not reported as
the model's failure (`WriteWithdrawn`, stage outcome `cancelled`). Narrower than the first check on purpose: a goal
that is `FAILED` because a parallel sibling failed has not had its approval taken back, and the batch still lets
its healthy steps finish.

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
    "library_evidence", "design_contract", "stage_result", "plan_updated",
    "laya_decision", "fix_retry", "fixer_pass", "plan_consult",
    "agent_call_failed", "usage", "model_delta", "error", "todo_updated",
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
`publish` sites in `executor_*.py` / `services.py`; this table has drifted
before, so a new event type means a new row here in the same change). The
literal above, `ui/src/types.ts`'s `EventType` and the table below are three
copies of one list, and `tests/test_event_type_contract.py` fails when they
diverge — the `Literal` quoted here had fallen a member behind the union, and
the UI's copy a member behind both.

| type | `step_id` | payload |
|---|---|---|
| `goal_status` | — | `{status, version}`, plus `{reason_code, reason}` on a pause the *engine* made (see below), and on nothing else |
| `step_status` | step | `{status, review_notes?, commit_message?}` — `commit_message` appears **only once a commit has landed** (a plain folder, a dry run, a cancel and a step whose files already match the last commit never carry one; the scribe's message is in the step's `log` instead, with the reason), so a client may show it as "Commit: …" without checking anything else — republished with `status: "IN_PROGRESS"` at every role transition inside a step (fixer → verifier → critic → scribe); only a start from a not-running state is a real attempt |
| `log` | any | `{level: "info"\|"warn"\|"error", message}`, plus `turn: true` on the reply that is a turn's answer (docs/09 §10), and `question: {text, options: [str]}` on that reply when the conductor ended the turn with `ask_user` (docs/09 §10.19): `message` is the question as prose (numbered options included, so history and speech carry it) and `question` is the same as data so a window can offer the options as buttons. At most 4 options of at most 80 characters, never exactly one |
| `diff` | step | `{path, unified_diff, note?}` — `note` says why a real change has an empty diff (binary, or over the 1 MB cap) |
| `test_result` | step | `{argv, verdict, explanation, exit_code?, refused: [str], ran: bool, brand_drifts: [str], output_tail: str}` — `ran: false` with `argv: null` means nothing executed; `refused` lists every command the sandbox rejected; `brand_drifts` lists the engine's mechanical findings against a binding brand contract (empty unless one governs — see §4.3); `output_tail` is the last ~2000 characters of what the command printed (stderr up to half, stdout the rest; empty when nothing ran or it printed nothing) — what the fixer's retry and the conductor's `verify` show so a failure says *why*. It is output of the repository's own code, so it is third-party text: `recall` projects a stored `test_result` to its verdict alone and never returns it |
| `file_change_summary` | step | `{paths: [str], dry_run: bool, unchanged: [str]}` — `unchanged` are paths whose proposal already matched the file ("already matched — left alone") |
| `agent_assigned` | any (`null` for laya) | `{role, provider, model}` — the model about to be called, published before the call |
| `provider_fallback` | any | `{role, from: {provider, model}, to: {provider, model}, code, detail}` |
| `library_evidence` | — | the checked evidence pack: `{summary, files: [{path, why, evidence}], symbols, conventions, test_command, risks, rounds, counts: {opened, matched, considered}, dropped_paths, capped}` (`04` §4.0) — `capped` is true when the round cap stopped the search with material still outstanding, the difference between "this workspace had nothing more to say" and "the engine stopped asking" | A workspace's `CODIFY.md`, when it has one, is carried separately as `knowledge: {path, chars, truncated, stale_paths}` — a prior, never a cited file (§4.9).
| `design_contract` | — | the locked direction: `{applies, artifact, direction, design_system: {name, source, origin}, tokens: {colors: [{name, value}], typography: [{name, value}], spacing: [str], radii: [str]}, components: [{name, purpose}], conventions, constraints, acceptance, design_md, mode?, revises?}` — `origin` is `pinned`\|"discovered"\|`null` (`04` §4.0a). `mode: "design"` or `"knowledge"` with a non-empty `design_md` marks a deliverable goal, where the body is the artifact rather than advice — `DESIGN.md` for `design` (§4.0a.2), `CODIFY.md` for `knowledge` (§4.9). `revises: {path, text, chars, truncated, stale_paths}` is present on a knowledge goal whose workspace already had a `CODIFY.md`, and carries the exact copy the drafter was shown so the transcript can render what the run is replacing; its `stale_paths` is the *pack's* verdict on that file, not the drafter's own (which is empty — that read has no tree listing) |
| `stage_result` | any | `{stage, role, step_id, ordinal, outcome, detail, duration_ms, tokens, calls}` — what one role stage achieved, what it spent, and how long it took (§4.7). `ordinal` disambiguates repeated stages in one scope: librarian rounds, planner consults, fixer attempts and passes. `outcome` is from the closed per-stage vocabulary in §4.7; `detail` is a short engine-authored note (a skip reason, a block reason), never model prose |
| `plan_updated` | step | `{step_id, step_title, fields: [str], changes: {field: {before, after}}}` — only fields the patch edited, only those whose value actually changed |
| `laya_decision` | — | the gate's full verdict — on a turn (`mode: "chat"`) published only when the gate blocked or warned, because a turn's log is a conversation rather than a run's audit trail (docs/09 §10.15); a benign turn is measured as a `laya` stage and not narrated: `{engine, answers, routing, blocked, block_reason, warnings, skipped_reason, provider, model, policy: {injection_block_threshold, risk_warn_level, clarify_warn_threshold}}` (`05`) |
| `fix_retry` | step | `{attempt, max_attempts, reason}` — a failing test run fed back to the fixer (bounded by `MAX_FIX_ATTEMPTS`) |
| `fixer_pass` | step | `{attempt, max_passes, passes_left}` — the fixer asked for another pass of its own (bounded by `MAX_FIXER_PASSES`) |
| `plan_consult` | — | `{refused, material_chars}` — the planner reopened the frozen evidence pack (`MAX_PLANNER_CONSULTS`) |
| `agent_call_failed` | any | `{role, provider, model, target: "primary"\|"fallback", code, message, duration_ms}` — a provider call that failed; the record the Settings screen's "last error" reads. `role` is one of the eight roles or `conductor` (the loop's own call, which has no role row; the Settings screen lists only the eight) |
| `usage` | any | `{role, provider, model, duration_ms, input_tokens, output_tokens, total_tokens}` — one per successful model call; feeds `/goals/{id}/usage`, the audit document, and the stats rollups. `duration_ms` is absent on events written before it existed. `role` is one of the eight roles or `conductor`: the conductor loop borrows the scribe's *configuration* but is booked under its own name, so `/goals/{id}/usage` and the Stats "by role" table show what the loop spent apart from the scribe's own calls. Before this a conductor-driven goal booked none of its loop's calls at all |
| `model_delta` | any | `{role, provider, model, text, final}` — a streaming snapshot of the reply so far (self-contained, ~every 400 ms); `final: true` closes the card, and once it lands the stream's earlier snapshots have their `text` blanked and `compacted: true` set (each repeats all the text before it, so keeping them was quadratic: 88 events / 106 KB for a 561-token reply). The rows stay: a goal's sequence is dense from 1 and a client tells a lost event by a gap. Chat-render only |
| `error` | any | `{code: str, message: str, role: str \| null}` |
| `todo_updated` | — | `{items: [{id, text, status}]}` — the conductor's own todo list (`engine/todo.py`), published whole every time the conductor changes it, so the newest event *is* the list. `status` is `pending`\|`doing`\|`done`\|`dropped`; an item is one line of at most 160 characters, a list holds at most 20 items, and one run changes it at most 40 times. It is the model's note to its next run, put back in its prompt as *its own notes, not instructions*: nothing in the engine reads it to decide anything, it is never shown to a sub-agent, and `recall` cannot return it (not in `RECALLABLE`). Item text can echo what the model read, so it is third-party text and the UI draws it as plain text |

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

`goal_status.reason_code` and `reason` say why the *engine* paused a goal. `PAUSED` has
several causes and the status alone names none of them: the person pressed Pause (no code,
they know), the critic asked for changes, or the conductor could not finish a step. The code
is from a closed set (`models.PAUSE_CODES`) and the sentence is the engine's own
(`models.PAUSE_REASONS`), never a quotation of the critic or a model: a pause can be caused by
text a model wrote about a repository, which is third-party, and this event and the `paused:`
log line beside it are read by other tools. Where to look is part of the sentence (the critic's
reasons are on the step's `review_notes`). The two keys come together, only with `PAUSED`, and
every other status change carries neither, so the newest `goal_status` event is the current
reason: `RUNNING` after a Start has none. `update_status` refuses an unknown code, a code
without a reason, or either on a status other than `PAUSED`. A pause never widens the write
gate: `PAUSED` refuses `write`, and only the person's Start moves it (`docs/00` §6.9).

**Pause codes**: `conductor_budget`, `conductor_provider`, `conductor_stopped`, `critic_rejected`.

`GoalService.next_sequence(goal_id)` is atomic (`UPDATE goals SET event_seq = event_seq + 1 ... RETURNING`).

### 1.4.1 Recall: the events table, read by a model

Everything above is written for the statistics screen and the transcript. `recall`
is the one tool that reads this table back, and it is a conductor tool — the same
loop that can read a file can now ask *has this happened here before, and did a
retry get past it?* The security shape is §1.8 in `03`; what lives here is the
query half:

- **`GoalService.recall_events(workspace_id, *, window_days=0, limit=None,
  now=None)`** returns recent allow-listed events for one workspace, newest
  first, as plain dicts: `goal_id`, `step_id`, `type`, `payload` (still a JSON
  string — parsing is `recall`'s job), `timestamp`, and the goal's `title` for
  context. It lives on `GoalService` because that class owns `events` and already
  queries across them for the stats rollups; a second service would be a second
  reader of the same table.
- **The type list is interpolated from the allow-list.** The query's `IN` clause
  is built from `recall.RECALLABLE`'s keys rather than repeated here, so a type
  added to the allow-list cannot be forgotten in the SQL, and a type left out of
  the allow-list is unreachable no matter what is stored.
- **The workspace scope is in the `JOIN` on goals**, not a Python filter — the
  other workspace's rows are never read in order to be discarded (§1.8 in `03`).
- **The window is optional and defaults to all time.** `window_days > 0` adds a
  `timestamp >= now - days` cutoff; `limit` caps the scan (default
  `MAX_SCAN_EVENTS = 2 000`). Both are bounded so a question on the hot path of a
  turn cannot become a full-table scan on a workspace with 200k events.
- **`recall.search` does the rest** (`engine/recall.py`): `project` turns a row
  into fixed, clipped, allow-listed fields or `None`; `recovered_pairs` delegates
  to `metrics.recovered_steps`; `format_recall` renders the result the model
  reads. The split keeps the SQL testable against a real database and the
  projection testable against plain dicts.
- **The thread grain: `GoalService.thread_recall(workspace_id, *, limit=50)`** is
  the query half of the sibling tool `recall_threads` — what a *new* conversation
  can learn from the workspace's earlier ones. One grouped query over
  `conversations LEFT JOIN goals` returns, per live (non-archived) thread:
  `title`, `last_touched`, `runs`, and the `completed`/`failed`/`cancelled`
  counts, plus the thread's most recent asks (goal `description`s, newest first,
  clipped at `MAX_ASK_CHARS = 120` in the query — an oversized prompt is never
  read whole to decide a match). Scope is the same `WHERE c.workspace_id = ?` as
  `recall_events`: the other workspace's threads are never read in order to be
  discarded. `recall.search_threads` optionally narrows by matching the query
  against thread names and asks, caps at `MAX_THREADS = 5`, and
  `format_thread_recall` labels the asks as what was *asked for* — not a claim
  that any of it was done. Deliberately not `turn_history`: that serves a
  continuing thread its own (prompt, reply) pairs, chat goals only; this serves
  a new thread its workspace neighbours, pipeline runs and chat turns both.

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
  -- itself is the deliverable, §4.0a.2; 'knowledge' makes CODIFY.md the
  -- deliverable instead, §4.9). Added by ALTER TABLE for an existing
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
  -- Ollama's context window for the role; NULL = the server default.
  num_ctx INTEGER,
  -- How long Ollama keeps the model resident; NULL = the server's own window.
  keep_alive TEXT,
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

-- One row per model call in a goal that was run with tracing on (§8). The
-- request is stored as a digest rather than as text: a digest is what a replay
-- matches on, and the prompt is the most sensitive thing in a run — the goal,
-- the evidence pack and the user's own source. Responses are stored whole
-- because a replay can serve nothing else.
CREATE TABLE trace_calls (
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

-- `seq` orders a replay; `created_at` is what the retention sweep deletes on.
CREATE INDEX idx_trace_calls_goal ON trace_calls(goal_id, seq);
CREATE INDEX idx_trace_calls_created ON trace_calls(created_at);

CREATE TABLE engine_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL
);

-- A day THIS engine froze from its own goals and events: the numbers as that day
-- ENDED (only the goals created and the calls logged before it was over; a goal
-- whose status last changed after it counts as still in flight). Subject to the
-- count-based retention policy (`stats_retention_days`); only the newest
-- `stats_retention_days` active days are ever frozen, so a pruned day is not
-- frozen again on the next read, and one read freezes at most 31 days, newest first.
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

Bind `127.0.0.1`. Port: first free in `7430-7440`, printed on stdout (`04` §6). Binding is retried on the next free port of that range if another process takes the chosen one between the probe and the bind; a port asked for with `CODIFY_PORT` is never swapped for another — the engine fails to start instead.

All routes: `Authorization: Bearer <boot_token>` or `401` `unauthorized`.

Error body: `{ "code": str, "message": str }` — `ErrorBody` in `engine/models.py`, the
one place that shape is defined. Extra keys are part of the contract, not a
loophole: a refusal may attach the facts a caller needs to act
(`workspace_not_empty` carries the goal count), and `code`/`message` are what
every caller may rely on. It is declared to OpenAPI via `ERROR_RESPONSES` in
`engine/app.py`, so `/openapi.json` and `/docs` describe every refusal rather than
only the successes; `tests/test_error_contract.py` fails if a status the engine
raises is not declared there. A body rejected by validation answers in the same
shape (`code: invalid_request`, field errors under `detail`) rather than
FastAPI's own `{detail: [...]}`, so there is one error shape to read, not two.

| Method | Path | Body | Success |
|---|---|---|---|
| `GET` | `/health` | — | `{ok:true}` (still requires Bearer) |
| `POST` | `/workspaces` | `{name, root_path}` extra=forbid | `Workspace` |
| `POST` | `/workspaces/browse` | — | `{cancelled:true}` (the person closed the dialog — the only silent outcome) \| `{cancelled:false, workspace}` \| **503 `picker_unavailable`** with the reason and the way out (no dialog could open, or one did not answer within `PICKER_TIMEOUT_S`, 120 s, and was killed with everything it started). See "Folder dialog" below |
| `GET` | `/workspaces` | — | `Workspace[]` |
| `GET` | `/workspaces/{id}` | — | `Workspace` |
| `PUT` | `/workspaces/{id}/design-contract` | `{path}` extra=forbid (`""` unpins) | `Workspace`. 400 `design_contract_escape` (outside the root), `design_contract_missing` (no such file / a directory), `design_contract_binary`, `design_contract_unreadable`. Refused means untouched |
| `GET` | `/workspaces/{id}/files?limit=` | `limit` 1–10 000, default 5 000 | `{files, truncated, limit}` — every file path under the root, sorted, skipping `SKIP_DIRS` and links out of the workspace. See §3.0.3 |
| `GET` | `/workspaces/{id}/file?path=` | — | `{path, content, version, size}`. 400 `file_escape` / `workspace_protected`, 404 `file_missing`, 422 `file_binary` / `file_not_text` / `file_too_large` / `file_access`. See §3.0.3 |
| `PUT` | `/workspaces/{id}/file` | `{path, content, base_version}` extra=forbid | `{path, version, size}` — **a person's Save, the one writer besides the fixer** (`00` §6.9). Replaces a file that exists; never creates or deletes one. 409 `file_changed` (carries `current_version`) when the file is not the version that was opened; the same 400/404/422 refusals as the read. Refused means untouched. Commits nothing |
| `DELETE` | `/workspaces/{id}?delete_goals={bool}` | — | forgets the folder; **never touches `root_path`**. 409 `workspace_not_empty` (with the goal count) unless the cascade is requested, 409 `workspace_has_active_goals` if anything is PLANNING/RUNNING |
| `POST` | `/goals` | `{workspace_id, title, description?, dry_run?, plan_only?, parallel?, mode?, provider?, model?, trace?}` extra=forbid — `mode` is `"normal"` \| `"design"` (`04` §4.0a.2) \| `"knowledge"` (`04` §4.9); `trace` records the run's model calls (`04` §8) | `Goal` |
| `GET` | `/goals` | query: `workspace_id?`, `status?`, `limit` (1–200, default 50), `offset` | `Goal[]` — active goals first, then newest |
| `GET` | `/goals/{id}` | — | `Goal` + `steps: PlanStep[]` |
| `DELETE` | `/goals/{id}` | — | deletes the run record; events/steps/proposals cascade, counts returned. 409 `goal_in_progress` while PLANNING/RUNNING or a driver holds it. Never touches files |
| `POST` | `/goals/{id}/start` | `{expected_version}` extra=forbid | `Goal`; the goal is `RUNNING` from the response, and the conductor (or the recipe, where it still drives: no tool-capable model, `conductor_drives_execution = 0`, or a `parallel` goal) takes the open steps in the background. A conductor that cannot finish a step pauses the goal with a `goal_status` reason (§1.4); Start resumes it at that step |
| `POST` | `/goals/{id}/pause` | `{expected_version}` | `Goal` |
| `POST` | `/goals/{id}/cancel` | `{expected_version}` | `Goal` |
| `PATCH` | `/goals/{id}/steps/{step_id}` | `{expected_version, title?, description?, suggested_paths?}` extra=forbid | `PlanStep` (PENDING goals only) |
| `POST` | `/goals/{id}/steps/{step_id}/retry` | `{expected_version}` | the re-opened `PlanStep`, returned **at once**: everything refusable (409 `illegal_status`, `step_not_retryable`, `driver_busy`, `retry_collides_with_running`, `version_conflict`) is decided in the request, the goal's driver is claimed there (`is_driving` is true from the response until the run ends), and the step runs in the background — progress is on the goal stream, not in this response. Where the conductor drives, the retried step is a conductor run with that step in focus and the critic's notes are kept for it; where the recipe drives, it is `run_step` and the notes are cleared |
| `GET` | `/goals/{id}/events?after={seq}` | — | `Event[]` where `sequence > after` |
| `GET` | `/goals/{id}/usage` | — | token totals + `parallel_peak`/`parallel_waves` (from `usage` events) |
| `GET` | `/goals/{id}/audit` | — | the goal's audit document (plan edits, fallbacks, fix retries, errors, outcomes, usage, silent roles) |
| `POST` | `/goals/{id}/apply` | `{expected_version}` | replays a completed dry-run's stored proposals for real |
| `POST` | `/goals/{id}/enable-execution` | `{expected_version}` | lifts the `plan_only` guard (`Goal`) |
| `GET` | `/goals/{id}/trace` | — | the recording's summary: `calls`, `by_role`, `prompts_kept`, `recording_error`, and `recorded[]` (`seq`, `role`, `model`, `prompt_hash`, tokens, `duration_ms`). Never the prompt text — that is a deliberate second step, not something a panel opens on load (`04` §8). 404 `unknown_goal` |
| `PUT` | `/goals/{id}/trace` | `{enabled}` extra=forbid | `Goal`. 409 `trace_locked` when enabling a goal that is no longer PLANNING — a recording that starts halfway is a trace of half a run. Disabling is always allowed |
| `DELETE` | `/goals/{id}/trace` | — | `{deleted}`. Idempotent and never refused on status: deleting a copy of your own run is the user's call |
| `GET` | `/stats/overview?window={1\|7\|30\|0}` | — | cross-goal outcomes, success rate, spend, daily trend (`engine/stats.py`), plus `by_stage` and `by_role_outcome` from the `stage_result` events (`engine/metrics.py`, §4.7). Bounded windows are anchored to the request's wall clock, so an idle install sees an empty window rather than its last run relabelled as recent. Both stage blocks are optional and their absence degrades the view rather than failing the read. `coverage` says how much of the history the read covered: `{goals, goal_cap, events, event_cap, truncated, since}`. The read takes the newest 5,000 goals and the newest 20,000 model-call events, so past that an "All" window counts from `since` (epoch seconds, null when nothing was cut) and `truncated` is true; the Statistics drawer says so |
| `GET` | `/stats/failures?window={1\|7\|30\|0}` | — | what went wrong: `by_code`, `by_role`, `by_stage`, the ranked `causes` with their most recent message, and how many `fix_retry` steps the loop then got past (`recovery_rate` is `null` with no retries — no retries is not a perfect record). An install that has never failed reads `total: 0` with no rows, never a table of zeros |
| `GET` | `/stats/history?limit={0..730}` | — | frozen daily stats documents, oldest first; `limit=0` returns every stored day for JSON export |
| `GET` | `/stats/import` | — | the currently-imported history document (`{imported: false, days: []}` when none — a normal state, not a 404) |
| `POST` | `/stats/import` | `{exported_at, days[], source?}` | validates and **replaces** the stored import (`engine/stats_import.py`). 422 with a specific `code` (`duplicate_day`, `out_of_order`, `bad_day`, `empty`, `too_many_days`, `missing_exported_at`, `storage_failed`). Stored separately from `stats_snapshots` so retention can never prune imported data |
| `DELETE` | `/stats/import` | — | forgets the stored import; idempotent, returns the day count cleared |
| `GET` | `/settings/providers` | — | `{builtins, custom}` (`01` §2.1) |
| `GET` | `/settings/laya` | — | gate capability report (`sdk` \| `llm-fallback` \| `skipped`) |
| `GET` | `/settings/runtime` | — | the engine's self-check (`engine/capabilities.py`): which interpreter is running, whether *it* can import the gate's SDK, and any warnings. Separate from `/settings/laya` because that route also honours `CODIFY_LAYA_SDK` and this one is a fact about the interpreter — a gate can be off because it was told to be or because the package is not there, and those need different fixes. Names filesystem paths, so it is behind the boot token like everything else |
| `GET` | `/settings/keys` | — | per-provider key status + `storage`/`storage_detail`/`storage_reason` (`04` §7) |
| `POST` | `/settings/keys` | `{provider, api_key}` | `{ok, provider, storage}` |
| `GET` | `/settings/agents` | — | `AgentConfig[]`, fixed role order |
| `GET` | `/settings/agents/stats` | query: `limit` | per-role last call / last error / counts, from the event log, plus `runs` / `success_rate` / `outcomes` / `tokens` measured over every run the log holds (§4.7). The rate is about the *role* — did it do its job — not about the goal, which is the other question and is answered by `/stats/overview` |
| `POST` | `/settings/agents/repair` | — | `RepairReport` (`04` §3.1) |
| `GET` | `/settings/agents/{role}` | — | `AgentConfig` |
| `PUT` | `/settings/agents/{role}` | `AgentConfigUpdate` | `AgentConfig` |
| `POST` | `/settings/agents/{role}/test-connection` | — | `{ok, message}` — a 15s liveness probe (`01` §3) |
| `GET` | `/settings/roles` | — | each role's `job` + `timing` (`01` §1) |
| `GET` | `/settings/engine` | — | every key in `SettingsService.SPEC` / `STRING_SPEC`, each with its clamp band (or its max length, for a string) |
| `PUT` | `/settings/engine` | `{parallel_width, …}` | `{saved: {…}}` — echoes what was actually stored, clamped for numbers |
| `GET` | `/models?refresh=` | — | live-discovered catalog, per-provider status, each model's `context_tokens` (or `null`), and a `conductor` block `{provider, model, source, num_ctx}` — the model a conversation runs on when nothing is picked, computed per request and not cached (`06` §2.1) |
| `GET` | `/models/recent?limit={1..25}` | — | `[{provider, model, role, ran_at}]`, newest first (`06` §3.1) |
| `GET` | `/audio/status` | — | `{dictation, read_aloud, recorder, auto_read}`: whether each can run now and, if not, the reason (§3.0.2) |
| `GET` | `/audio/inputs` | — | `{available, reason, inputs: [{name, description, default}]}` from PipeWire (§3.0.2) |
| `POST` | `/audio/dictation/start` | — | `{recording, max_seconds}`; `409 stt_not_configured` / `already_recording`, `503 recorder_unavailable` / `recorder_failed` |
| `POST` | `/audio/dictation/stop` | — | `{text, seconds}`; `409 not_recording`, `422 no_audio`, `502` with the provider's own code |
| `POST` | `/audio/dictation/cancel` | — | `{cancelled}`; the recording is deleted and sent nowhere |
| `POST` | `/audio/speak` | `{text}` | `audio/wav`; `400 text_empty` / `text_too_long`, `409 tts_not_configured`, `502` with the provider's own code |

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

**"A credential is stored" means the role's own key *or* the provider's.** A custom provider's key is stored
*for the role* (`api_key_ref` — what the role card's key field writes, and what the factory reads first), not
under the provider's name, and the key table (`AgentRegistryService.provider_key_status`) lists the built-in
providers plus any custom slug a role points at. Judging from the provider table alone called every role on a
custom provider uncallable — on every goal (the "8 of 8 agent roles cannot be called" line, while all eight
were being called) and in this endpoint, which then repointed working roles. The preflight and this endpoint
now read the same rows and the same table (`configs_with_key_state`, `provider_key_status`), and the role's
own key counts for its **primary** target only: a fallback is a different provider and never inherits it
(`tests/test_role_preflight.py`, `tests/test_api.py::test_repair_leaves_a_custom_provider_role_that_holds_its_own_key_alone`).

**A fallback is only forgiven for what discovery could answer.** A role whose primary cannot run is left alone when its fallback may still serve it — but "may" is decided by whether the fallback's own problem is *provable without the network*. No model, or a credential that is not stored, is already true of the store we hold, so a fallback with either is not treated as a possible save; a model the provider may no longer list is, because only asking settles that. Without the distinction, a role whose primary and fallback both need a key reported as "may run on its fallback", and neither this screen nor the preflight below named the one thing the user could act on.

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

### 3.0.1 Folder dialog

`POST /workspaces/browse` runs a native folder dialog in a subprocess (GTK is never imported into the engine)
under the spawn guard, and answers with one of three things — only one of which is silent (audit of
2026-09-29, M8: a dialog that had *crashed* was reported as `{"cancelled": true}`, so a picker that could not
open looked exactly like one the person closed, in the API and on screen):

| outcome | answer |
|---|---|
| a folder chosen | `{cancelled:false, workspace}` — the existing workspace for that path, or a new one |
| the dialog closed with nothing chosen | `{cancelled:true}` |
| no dialog could open, it crashed, or it did not answer | **503 `picker_unavailable`**, the reason, and "type the folder's path instead" |

The UI acts on that code rather than only showing it: on `picker_unavailable` it shows the reason **and opens
its "Enter workspace path" form**, so a desktop with no dialog helper (a bare window manager, a container) is
never left holding an instruction and a hunt for the control it names. Any other failure is shown and opens
nothing; a real cancel is silent (`ui/tests/folderPicker.test.ts`).

The GTK script (PyGObject, GTK 3) is tried first and speaks in exit codes: `0` with a path is a choice, `0`
with nothing is a cancel, `3` is "PyGObject is not importable" and `4` is "GTK could not open a display".
When it cannot run — any venv, conda or pyenv Python, which is most machines with a desktop — `zenity`, then
`kdialog`, are tried if installed. Both exit `1` for Cancel, and zenity exits `1` for "cannot open display" as
well, so a `1` whose stderr mentions the display is a failure, not a cancel. A real cancel from any of them
ends the search. A dialog that has not answered within `PICKER_TIMEOUT_S` is killed with its whole process
group, so a dialog the engine gave up on does not stay open on the screen. The UI shows the 503's message
in its error banner; typing a path in the folder menu always works without any dialog.

### 3.0.3 The editor's files

Three routes, for a *person* in the editor tab (`09` §13). They are the only place the engine reads a workspace file for
someone other than a model, and `PUT` is the only place it writes one for someone other than the fixer.

**What a file is.** UTF-8 text with no NUL byte anywhere in it, at most `MAX_EDIT_BYTES` (1 000 000). Anything else is a
422 that says which, because opening a PNG in a text editor and saving it back is how a file is corrupted. The read
translates nothing (CRLF and a byte-order mark come back as they are) and the write stores exactly what was sent, so a
save never changes a line the person did not touch.

**`version`.** The SHA-256 of the bytes on disk. A save names the version it read in `base_version`; a file that is no
longer that version is a **409 `file_changed`** carrying `current_version`, never a silent overwrite of what the fixer, a
terminal or another editor wrote in the meantime. The check and the replace are two steps rather than one lock, so the
window is the length of one `os.replace`. A client that wants to overwrite anyway saves again, naming `current_version`.

**What Save is not.** It replaces a file that exists. It does not create a file or a folder, delete, rename, `chmod`
(the mode is kept), commit (that is the scribe's, behind an approved goal) or follow a path out of the workspace or into
`.git`. A workspace rooted somewhere `protected_root_reason` refuses is a 400 `workspace_protected`.

**Why it is safe to add.** Containment, `.git`, the protected-root check and the temp-file-and-rename are the code
`apply` uses. The claim that no agent can reach it is held by the engine's own source:
`test_invariants_at_their_boundary.TestAPersonsSaveIsTheOneOtherDoor` fails if any module but `services.py` names
`save_text`, any but `app.py` names `save_file`, or any conductor, executor, skill or surface module names either.

The listing is not `LibraryService.tree` (two levels, 120 names, orientation for a model): it walks to the bottom, is
sorted, and is cut at `limit` with `truncated: true` rather than silently. All three do their file work off the event
loop, after the workspace is looked up on it.

### 3.0.2 Voice: dictation and read-aloud

Two features, one module (`engine/speech.py`), configured in Settings → Audio:

- **Dictation** turns speech into text in the composer. The engine records the microphone, sends the recording
  to the dictation provider's `POST {base_url}/audio/transcriptions` (multipart: `model`, `file`, optional
  `language`), and answers with the text. The text goes into the prompt; it is never sent as a turn by
  itself.
- **Read-aloud** turns an answer into speech: `POST {base_url}/audio/speech` with
  `{model, voice, input, response_format: "wav"}`, and the WAV goes straight back to the UI, which plays it.

**Engine settings** (`PUT /settings/engine`, the only writer): `stt_provider`, `stt_model`, `stt_language`,
`tts_provider`, `tts_model`, `tts_voice`, `audio_input` (a PipeWire node name; empty is the session's default
source), `stt_base_url` and `tts_base_url` (a custom provider's own address, below), and the switch
`tts_auto_read` (0/1, default 0). The two provider keys are slug-checked like the conductor's. The two
addresses must be `http(s)` with a host, or empty to clear them (`422 invalid_value` otherwise).

A `PUT /settings/engine` is all or nothing: every key in the body is checked first, and a request that answers `400` or
`422` has stored none of its keys (a card that sends two together, such as Web pages' mode and list, is never left with
one saved). A number that is not an integer, `Infinity` and `1e999` included, is a `422 invalid_value`.

**Which provider.** A built-in slug resolves to the catalogue's address (`BUILTIN_PROVIDERS`) and ignores
any typed address, so an address saved beside `openai` can never carry the OpenAI key somewhere else. A
custom slug resolves to its own address (`stt_base_url` / `tts_base_url`) when one is saved, because a local
speech server is nothing an agent role should have to define. Without one, it resolves to the address of the
role row that names it (`services.custom_provider_address`, the helper the conductor uses too). With neither,
it is refused with `409 speech_provider_unknown`. It is spoken to over the OpenAI audio API only, which a local speech server
(speaches, LocalAI) and the hosted providers that offer speech (OpenAI, Groq) answer. Codify keeps no list of
providers believed to have audio: a provider whose protocol is not `openai_compat` is refused with
`409 speech_protocol_unsupported` and a sentence saying why, and the model id is the provider's own,
discovered or typed. A built-in that needs a key and has none is `409 speech_key_missing`. A custom provider
with no key is called with no `Authorization` header, so a keyless local speech server works. A stored key is
never sent to a plain-http, non-loopback address (`invalid_base_url`, the same rule as `03` §1.2), however the
two came together. A provider that refuses or cannot be reached is a `502` carrying the provider error's own
code, with any credential the request carried redacted from the message.

**The recording.** The microphone is recorded by the engine, not the webview: PipeWire's `pw-record`
(`--rate 16000 --channels 1 --format s16 [--target <audio_input>]`), started under the spawn guard in a session
of its own, like the folder dialog above. Why not the webview: WebKitGTK through wry neither enables media
capture nor answers a permission request, and granting the microphone to the app's webview, beside an in-app
browser, is a door nobody needs. Lifecycle:

| step | what happens |
|---|---|
| start | refused **before anything is spawned** if dictation has no target (`409 stt_not_configured`) or `pw-record` is not installed (`503 recorder_unavailable`); a recorder that exits within 0.3 s is `503 recorder_failed` with its own reason; one recording at a time (`409 already_recording`) |
| record | into `<state dir>/dictation/<random>.wav`, in a directory created `0700` |
| stop | a group SIGTERM (the guard forwards nothing to a live engine's TERM and pw-record finishes the file), SIGKILL after 2 s; the file is read, **deleted**, and sent once; a header with no samples is `422 no_audio` and is sent nowhere |
| cap | a recording nobody stops ends at `MAX_DICTATION_S` (120 s); what was recorded is kept for the stop that collects it |
| cancel | the recording is ended and deleted and nothing is sent |
| engine stop | the lifespan's shutdown ends any recording and deletes its file |

`GET /audio/inputs` lists sources from `pw-dump` (`media.class` `Audio/Source…`, with the session's
`default.audio.source` marked). Without PipeWire's tools both routes say so and name the package; `make doctor`
reports it as a note, never a failure, because the gate does not need a microphone.

### 3.0.4 Browser actions: `navigate_page`, `click_page`, `type_page`

`browser_actions` is an engine setting with a 0/1 range and a fresh-install default of 0. Only
`PUT /settings/engine` writes it (`docs/00` §6.2). Until a person enables it in Settings → Engine,
the conductor does not receive these three tools in its menu. `read_page` stays independent, and
`fetch_page` has its own `web_fetch` permission: neither reading permission enables actions on the
person's tab. The conductor's dispatch path checks the setting again, so a previously offered action
is refused if consent is withdrawn before it runs. This is an explicit consent switch, not an egress
filter: navigation URLs and typed values can carry anything the turn has read, and a click may submit
that data or operate on a logged-in account (`docs/03` §1.6).

### 3.0.5 Web pages: `fetch_page`

The one tool whose request the *engine* makes to an address a model chose (`engine/web_fetch.py`; the audit,
and every rule's reason, is `docs/12`). It is a conductor tool and has no route of its own.

**Engine settings** (`PUT /settings/engine`, the only writer, `docs/00` §6.2): `web_fetch` (0 off, 1 only the
sites in `web_fetch_hosts`, 2 any public site; default 2; clamped to 0–2; a boolean is `422`) and
`web_fetch_hosts` (site names, comma-separated, each covering its subdomains; at most 200 characters, which is
what `SettingsService.set_str` stores). The list is stored in the one spelling the fetch parses (`parse_hosts`:
lower-cased, `*.` and a leading or trailing dot dropped, a name in another script written as its ASCII form
(`münchen.de` is stored `xn--mnchen-3ya.de`, which is what an address's host is compared in), each name once,
joined with `, `). The 200 applies to that stored form, not to what was typed, which is shorter: a list that is
too long once written out is `422` and nothing is stored, because the store would otherwise cut it and chop the
last site to a name that matches nothing. An entry that is not a
site name (an address, a URL, a path, a single label such as `intranet`) is `422 invalid_value` naming up to five
of them, and **nothing is stored**: a list that quietly allowed less than it was shown would be a bug nobody
could see. `POST /goals` and a turn cannot carry either key (`tests/test_invariants_at_their_boundary.py`).

**What the tool does** is `docs/12` §3's table. In short: nothing is fetched unless `web_fetch` is 1 or 2; the
tool is left off the menu while it cannot act (mode 0, or mode 1 with an empty list); an unreadable setting is
off; GET only; the name is resolved and every answer must be a public address, and the connection is made to
the address that was checked; each redirect (at most 5) passes every rule again; 20 s overall, 1 MB of body,
text content types only; the address is logged (`conductor is fetching <address>`) before the request, and a run
makes at most 8. What comes back is `Fetched`: the final address, status, content type, title, text (default
12,000 characters, 200–40,000 as asked), whether it was truncated or its body capped, the redirect route and up
to 40 links (each address at most 500 characters, so the links cannot outweigh the text), formatted as a quotation of the website (`format_fetch`).

**Errors the model is told in a sentence** (`FetchRefused`, as `That page was not fetched: <reason>`): off, a
scheme, user-info, a port, a length, an address that resolves to a non-public one (naming it), a host not on
the list, too many redirects, a non-text content type, a site that cannot be reached, a selector the parser
rejects, the 20 s budget. None is a traceback, and text that came from the far side (a content type, a host in a
redirect) is bounded before it is put in a sentence.

### 3.0.6 Code scanning: `scan_code`

A conductor tool with no route and no setting (`engine/scan.py`; the audit and every limit's reason is `docs/13`). It is
`search_code` with curated patterns, held to what `search_code` is: read-only, confined to the workspace (the same
walk, the same `SKIP_DIRS`, the same symlink rule), bounded, honest about what it skipped.

**The call.** `scan_code(profile?, glob?, include_tests?, include_comments?)`. No `profile` returns the list of
profiles (name, source, rule count, description, any workspace shadowing, any problems reading a profile). A name, or
`all`, scans. A name that does not exist is the refusal `there is no profile called 'x'` with the names that do.

**A profile** is a JSON file: `{"name", "description", "rules": [...]}`. The built-ins are
`engine/builtin_profiles/*.json`; a workspace's are `.codify/profiles/*.json`, and one named like a built-in replaces
it (reported). A rule:

| Field | |
|---|---|
| `id` | `[a-z0-9][a-z0-9_.-]{0,63}`, unique across profiles for the built-ins |
| `severity` | `high`, `medium`, `low`, `info` |
| `pattern`, `flags` | a Python regex of at most 200 characters; flags from `i`, `m`, `s` |
| `languages` | file extensions (at most 20), or `[]` for every text file |
| `where` | `code` (comments and string contents blanked), `strings` (comments blanked), `raw` |
| `title`, `why`, `fix`, `cwe` | prose for the report (clipped to 120 / 240 / 240), and `CWE-123` |
| `examples` | `{"match": [...], "clean": [...]}`; required of the built-ins by a test, ignored in a scan |

A rule that does not parse or compile is dropped and named in the problems; it never raises, and one bad rule does
not take its profile. At most 200 rules per profile, 20 profile files per workspace, 128,000 bytes per file; a
symlinked profile file is not read. A profile can name nothing else: the worker is handed `id`, `pattern`, `flags`,
`languages` and `where`.

**The scan** runs in `engine/regex_worker.py` with `op: "scan"`, started by `library._run_regex_worker` (no new spawn
site) and killed at the request's `budget_s` (15 s) plus the margin a search has. It walks in sorted order, skips test
files (`is_test_path`; `include_tests` brings them back), generated and minified files, binary files, files over 2 MB
and symlinks that leave the workspace, and stops, with the reason, at 800 files or the time budget. Comments (and, for
`code` rules, string contents) are blanked by a heuristic tokenizer per language family, keeping newlines and columns.
A line is scanned to 2,000 characters. A rule applies to the whole text and a hit is reported at the line it starts on;
several on one line are one hit. At most 8 hits are kept per rule and 60 in a report; the rest are counted, up to 500
per rule.

**The result** is text: the caveat first (candidates, not vulnerabilities, and the quoted text is workspace text), a
label if a workspace profile was used, the counts by severity, each rule's group (severity, CWE, title, why, fix, then
`path:line  excerpt`), and the **coverage** line: files scanned and a number for each skip, files read in part, lines
cut, the file types it cannot read comments in, and why it stopped early. Errors the model is told in a sentence:
`That scan did not run: <reason>`.

### 3.1.1 The same rule at the start of every goal

`POST /settings/agents/repair` is an action, and an action nobody thinks to take
is an action that never runs. So `ExecutorService._preflight_roles` asks the same
question at the top of every run, through `config_problems` in
`engine/role_repair.py` — the identical rule, minus discovery, because a provider
that has not been asked proves nothing and this line has to be cheap enough to
always print.

The engine could already diagnose this correctly; `AgentNotConfigured` says "no
model is configured for the librarian role" and names the screen. What it could
not do was tell you about the roles it had not reached yet. You found out one dead
run at a time, and never heard about the other six. One line naming all of them,
before the first model call, is the difference between a diagnosis and a mystery:

```
4 of 8 agent roles cannot be called, so this goal will fail when it reaches them —
librarian: anthropic needs a credential and none is stored; design: no model is
chosen; verifier: openai needs a credential and none is stored; critic: deepseek
needs a credential and none is stored. Open Settings → Agent Roles, or press
Repair to point them at a model that is reachable.
```

A **warning, not a block**, for the reason laya, the librarian and the designer are
warnings: the roles that do work should still do their work, and a run killed by a
setting is a run nobody can inspect. A fully configured store prints nothing at
all — a preflight that always speaks is a preflight that gets ignored.

`config_problems` takes the full `ROLES` tuple, not just the stored rows. It is
the one thing here the repair screen cannot see: the repair plan iterates stored
configs, so a role with no row is invisible to it, and `get_config` answers such a
role with a `404` about an "unknown" role that is one of the engine's own eight
slots. The schema backfills a row for every role on every connection, so this is
defence rather than a live path — but a preflight that cannot see the case is the
bug it was written to fix.

`tests/test_role_preflight.py` covers the rule, the line, the silence, and the
agreement with the repair plan on the same database — one rule and two screens is
exactly the arrangement that drifts.

## 4. Agent JSON contracts

Extra keys ignored. Missing required keys → `agent_output_invalid`.

**Reading a reply** (`engine/replies.py` `extract_json`, re-exported by `engine.executor`; audit of 2026-09-29, H4). It was "first `{` or `[` to last
`}` or `]`, then `json.loads`", which right-answered 10 of the 23 shapes in `tests/test_extract_json.py` and
none of the eight cut-off replies: a `<think>` block that mentions braces, an example object before the real
one, a trailing comma, single quotes and Python literals, comments. Every top-level object or array in the
reply is now read — after stripping reasoning blocks and applying the repairs a near-miss needs (comments and
trailing commas; Python triple-quoted `"""…"""` values, whose closing delimiter is the first one that ends a value, since the text inside is often Python with docstrings of its own; raw newlines and tabs inside strings; then Python's spelling of the same document via `ast.literal_eval`, which executes nothing) —
and the one the role asked for is chosen: the **last dict carrying any of the role's contract keys**
(`replies.REPLY_KEYS`), else the last dict, else the last list. A reply that stops before its document does is
refused, unless the role tolerates dropping an unfinished tail (`REPLY_TOLERATES_TRUNCATION`, **not the
fixer**, whose reply is file contents and must never become a half-written file): then what was finished is
kept and the element that was not is dropped — never completed, never a string closed.

**One re-ask, then the fallback.** A reply that still cannot be read is asked for **once more from the same
target** before the fallback is considered: the original task unchanged, then what was wrong (the parser's
reason) and what the model said, then "reply again with the corrected JSON document only". The first bad
reply is recorded as a failed call (`agent_call_failed`, `code: agent_output_invalid`, `retrying: true`) —
a model that needs this is something to see — and the second call has books of its own. A valid reply is never
re-asked; prose (`raw_output`) never is; a provider error on the re-ask is a provider failure like any other;
after a second bad reply the failure reads "…after one repair attempt" and the fallback rule is exactly what
it was. The bound is structural: no target is asked more than twice for one call.

**The reason it quotes is where the document breaks *after* the repairs.** A string with no closing quote
also "never closes" — every quote after it pairs the wrong way round — and in a *complete* reply that is a
syntax error, not a truncation. The parser's own complaint pointed at the start of a `"""` value the repairs
read without trouble (char 100 of a 467-character reply), and the sentence said the reply "ends before the
document does", which is the wrong clue to hand a model that is asked to fix it. The reason is now taken from
the repaired text, with a short quote of the neighbourhood (line numbers count the repaired text, where a
multi-line `"""` value is one line): `Expecting ',' delimiter … near “if name else 'hello world', "count": 1 }”`.
"Ends before the document does" is kept for a reply whose parser really ran out of text, or stopped inside a
string that never ends (`tests/test_extract_json.py::TestAReplyThatIsNotCutOffIsNotCalledCutOff`, from a real
`Qwen2.5-1.5B` capture).

The same re-ask covers a reply that **parsed but could not be used** (`run_agent(..., accept=)`): the
fixer's edit that matches the wrong number of times or not at all (`old_text appears 2 time(s), expected 1`),
an entry the contract refuses. The caller knows exactly what is wrong, so the model is told — the same
prompt, with that reason and the reply it refers to — instead of a person being. The first failure is a
failed call with `retrying: true` (`agent_call_failed`, "returned a reply that could not be used"); the second
reads "…after one repair attempt"; and because `fs.apply` resolves every edit before it writes anything, a
refused reply has written nothing and asking again is safe. A fixer with a fallback target now reaches it for
these failures too, as it does for any other `agent_output_invalid`.

Three more slips a real small model made are handled the same way, each found by running it:

- **A reply that is a list, not an object.** Every contract is one object. A list used to escape the parser as
  `AttributeError` (reaching a user as `internal_error`); it is now refused with "the reply must be a JSON
  object, not a list" and asked for again. The one exception is the contract's own array with nothing around it —
  `[{"path": …}]` for the fixer, `[{"title": …}]` for the planner, every entry carrying the field only that
  array's entries carry — which is read as `{"files": […]}` / `{"steps": […]}` (`replies.coerce_object`):
  nothing is invented, and Qwen2.5-1.5B repeated the bare list when asked again.
- **A planner reply that parsed but is not a plan** (`accept=` too: no steps, a step with no title). A *consult*
  — no steps, a request for the librarian — is still a good reply.
- **A path the workspace refuses.** An absolute path (`/src/app.py`), one that climbs out (`../`), one inside
  `.git`: the refusal names exactly what to change, so it goes to the model once. Nothing outside the workspace is
  ever written, an absolute path is never quietly made relative, and a step that still cannot name a legal path
  fails with `path_escape`, the code it always had. A protected workspace *root* is not put to the model — asking
  again cannot change where the workspace is.

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
  model-supplied pattern is untrusted input, so it is bounded at `MAX_REGEX_PATTERN` (200 chars),
  and an invalid or oversized pattern comes back as a refusal, not a crash. The *match* runs in a
  worker process (`engine/regex_worker.py`, under the spawn guard) with two clocks: a soft budget
  of `REGEX_BUDGET_S` (2s) checked between files inside the worker, and a hard limit of
  `REGEX_HARD_LIMIT_S` (2.5s) after which the worker's whole group is `SIGKILL`ed and the model gets
  "pattern too expensive". It is a process because CPython's `re` cannot be interrupted and holds
  the GIL: on a thread, `(a+)+$` over one 28-character line stalled the event loop — HTTP, WebSockets,
  `/health`, Cancel — for 14 seconds, and each extra character doubles it. A conductor `search_code`
  call may also opt into the second strategy with `mode: "keyword"`: the same walk under the same
  caps, indexed into an **in-memory** FTS5 table (one row per file) and ranked by BM25 — for
  multi-word questions no single line answers, so matches are whole files (`line: 0`, the result says
  to pass the path to `read_file`) and the ranking, not the path sort, decides the order. FTS5 is a
  SQLite compile-time option, so the strategy degrades rather than refuses: a probe establishes
  availability, and when it is absent — or the connection fails, or FTS5 refuses the query — the
  literal substring answer is computed and returned labelled `strategy: "substring_fallback"`. The
  result always says which strategy ran; substring (the default) carries no marker, so the evidence
  checker's read of `matches[].path` / `files_scanned` is unchanged.
- `git` / `run` → `SandboxService.run_command(mode="read_only")`: `ls`, `wc`, and read-only git — an
  **exact-match table** of subcommands and the options each accepts (`engine/git_readonly.py`, §5
  below), so an option the table does not name is refused whatever git would have made of its
  spelling.

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
 "counts":{"opened":1,"matched":1,"considered":3},"dropped_paths":["src/ghost.py"],
 "capped":false}
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

Bounds — each list is trimmed, never dropped whole (`MAX_DESIGN_*` in `engine/executor_design.py`): 24 colors,
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
them in `engine/executor_design.py` and `engine/executor_steps.py`:

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

`action=edit` takes `edits: [{old_text, new_text, count}]` and no `content`. One spelling is read rather than
refused: **`edit` with a non-empty `content` and no `edits`** is a whole-file `update` — the model wrote the
file it wants and used the wrong name for it (13 of the 43 file entries Qwen2.5-1.5B produced in the recorded
baseline, `08` §8; asking again did not help, since it does not know what `edit` is for). The engine logs
`fixer sent action=edit with content and no edits for <path>; treated as a whole-file update`, never does it for
an empty `content` (that would blank the file), and never when `edits` is present — those keep their meaning and
`content` is ignored. A reply the engine cannot apply (an `old_text` that matches the wrong number of times, a
path outside the workspace) is put to the model once, with the reason and, for an edit, the alternatives
(`04` §4 "One re-ask").

**A batch is applied completely or not at all** (`FileSystemService.apply`; audit of 2026-09-29, M4).
Phase one resolves and validates every operation before anything is written — containment, `.git`, the
action name, a target that is a directory, each `edit`'s search text — against a virtual view that reflects
the operations before it in the same batch, so an `edit` of a file the batch just created sees it. Only
then are the writes made, and a write that fails half-way (a full disk, a permission) puts back what the
batch had already touched: content, mode, deleted files and any directories it created. So a refused batch,
or one that failed while writing, leaves the tree byte-identical, which is what makes it safe for the
step to fail loudly instead of leaving half a change uncommitted and unannounced. A dry run performs phase
one only. Nothing is written into a protected workspace root at all (`03` §1.4).

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
| `unknown_protocol`, `invalid_base_url`, `secrets_unwritable` | the target cannot be constructed (`invalid_base_url` also covers a stored key that would be sent over plain http to a non-loopback host — docs/03 §1.2) |
| `provider_http` | the provider answered with an error status (401, 404, 429, 5xx) — reported *after* the retries below, and carrying the provider's own reason |
| `provider_unreachable` | the connection was refused, timed out, or dropped mid-stream |
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
that blames Codify for a provider that is merely not listening. `providers.post_json` (and `open_stream`, for
the two streamed paths) is the single transport path that names both.

#### 4.6.1 Transient failures: retried before they are failures

A fallback is for a target that cannot be used; a provider that said "busy, ask again in two seconds"
can be, so the transport asks again before anything above it hears about a failure (audit 2026-09-29, H5).

| | |
|---|---|
| **Retried** | `408`, `425`, `429`, `500`, `502`, `503`, `504`, `529`, and a connection that was refused, timed out *connecting*, was reset, or hit a protocol error before any answer |
| **Never retried** | `400`, `401`, `403`, `404`, `422` — the same request gets the same answer, and repeating a rejected key is how an account is locked. Nor a *read* timeout: the server may still be generating, and a second request doubles the work |
| **Attempts** | `MAX_ATTEMPTS = 3`: the first and two retries |
| **Wait** | exponential (`1 s`, `2 s`, capped at `20 s`) with a jitter factor in `[0.5, 1)`; a `Retry-After` header — seconds or an HTTP date — replaces it. One longer than `RETRY_AFTER_CAP_S = 30` is **reported, not waited for**: the message says how long the provider asked for |
| **Streams** | retried until the response opens; **never after the first byte**. Deltas are already on screen, so a reset is `provider_unreachable` reading `<label> stream interrupted after N characters`, and the reply is not run again |
| **Probe** | `test_connection` makes one attempt. It reports the first answer inside its 15 s deadline; a 429 is an answer |

What the error says changed with it. `provider_http` used to read `anthropic 400` and nothing else,
because the body — the only place a provider says *why* — was dropped. It now reads
`<label> <status>: <the provider's message>` (`error.message` for Anthropic, OpenAI and Google, the
`error` string for Ollama, the text for a proxy), one line, at most 300 characters, and with anything
credential-shaped (`sk-…`, `AIza…`, `Bearer …`, `api_key=…`) and the exact credential the request
carried replaced by `[redacted]` — so invariant 4 holds for text a provider wrote as well as text we did.
An exhausted retry says so (`… (after 3 attempts)`). `ProviderError.status` carries the number for the
two callers that ask "did the server reject *this request*" — the OpenAI-compatible `response_format` and
`stream` fallbacks — which now fire only on a 4xx that repeating cannot change (`refused_the_request`),
not on a 429 or a 5xx that has already had its retries.

### 4.7 Measuring the stages

Every role stage publishes one `stage_result` when it finishes — including when
it raised, because a stage that cannot run is one of the answers a per-role
number has to contain. The measurement is taken where the stage runs, not
reconstructed afterwards from whatever side effects it left behind.

**What a stage covers** is the awaited role call and the engine work inside it:
for the fixer, the `fs.apply` and the writes; for the verifier, the sandboxed
command; for the librarian, its bounded rounds. The status writes and summary
logs that follow are not counted — they are microseconds, and including them
would make the number depend on where the stage boundary was drawn rather than
on what the stage did.

**`duration_ms` is wall clock, not the sum of its model calls.** A stage's cost
is the call *and* the work around it, so the per-stage latency is larger than
the per-call `duration_ms` on the `usage` events, and both are labelled for
what they measure. **`tokens` and `calls` are read back from the `usage` events
the orchestrator published during the stage**, matched on role *and* step: under
a parallel goal another step's calls land in the same goal's log between the
same two sequence numbers, and attributing them here would move cost between
steps that ran at the same time.

**`outcome` is a closed vocabulary**, so a per-role rate is a count of declared
outcomes rather than a guess at what a missing event meant:

| stage | outcomes |
|---|---|
| `laya` | `skipped` \| `allow` \| `block` \| `cancelled` \| `unavailable` |
| `librarian` | `pack` \| `incomplete` \| `invalid` \| `cancelled` \| `unavailable` |
| `design` | `contract` \| `declined` \| `invalid` \| `cancelled` \| `unavailable` |
| `planner` | `plan` \| `consult` \| `invalid` \| `cancelled` \| `unavailable` |
| `fixer` | `wrote` \| `no_change` \| `replayed` \| `invalid` \| `cancelled` \| `unavailable` |
| `verifier` | `pass` \| `fail` \| `skip` \| `refused` \| `invalid` \| `cancelled` \| `unavailable` |
| `critic` | `approve` \| `request_changes` \| `invalid` \| `cancelled` \| `unavailable` |
| `scribe` | `committed` \| `nothing_to_commit` \| `not_a_repo` \| `skipped` \| `invalid` \| `cancelled` \| `unavailable` |

`invalid` is a reply the engine could not use; `unavailable` is a call that
could not be made or completed. They are different problems to the person
choosing what to fix, and a rate that merged them would hide a role whose
prompt needs work behind a role that has no key. `incomplete` and `skipped` are
outcomes the stage chose, not failures: a librarian stopped by its round cap
looked and ran out of budget, and a scribe in a dry run was told not to commit.

**A stage that raises still publishes.** `TestsFailed` and `CriticRejection` are
control flow rather than faults — a verifier that returned `fail` and a critic
that requested changes have both done their job — so a stage that declared
nothing and raised one of those is published as `fail` / `request_changes`, not
as `invalid`. A rate that counted those as broken replies would report a
healthy pipeline as broken precisely when it is working.

`engine/metrics.py` aggregates these into what the settings and stats views
render, as pure functions over parsed events (the same shape and the same
reason as `engine/stats.py`). Three rules govern every number it produces:

1. **A rate is never computed from a denominator that has not finished.**
   `cancelled` is reported on its own, exactly as an active goal is (§3), so
   the number moves only for reasons that have to do with the role working.
2. **A rate is never the only number.** The outcome histogram ships with every
   rate, so "the verifier did its job 91% of the time" can be checked against
   `fail: 9` rather than taken on trust.
3. **Tokens are tokens.** There is no price table and no currency on any
   surface that reads these numbers, because a price that silently stops
   matching the provider is worse than no price at all.

### 4.9 Workspace knowledge (`CODIFY.md`, `Goal.mode = "knowledge"`)

Every goal's librarian starts from nothing: a title, a description and a depth-2 tree listing. A
repository that has already worked this out — which module owns what, where the entry points are,
what the commands are — pays to have it re-derived, and the derivation is the part that is most
often confidently wrong.

`CODIFY.md` at the workspace root is where a workspace writes that down. It is in the repository
rather than in `~/.codify` because it is knowledge *about this repository*: it belongs in a diff, it
is reviewed like code, and it dies with the clone.

#### 4.9.1 It is a prior, and never evidence

The evidence pack is built on one promise — a path in `files` is there because the engine actually
opened it, matched a line in it, or saw it listed. `CODIFY.md` cannot make that promise about
itself, so it never enters `files`, `symbols` or `dropped_paths`. It arrives in its own `knowledge`
block and in the prompt, wrapped in `format_knowledge` (`engine/library.py`), which says in the
prompt itself that it is *"a prior, NOT evidence"* and that every path in it is a claim about a file
nobody opened.

The test that matters is `tests/test_workspace_knowledge.py::LibrarianPriorTests::test_a_prior_never_becomes_evidence`:
a pack that cited the prior would look identical to a pack built on a real reconnaissance pass, and
that is the failure this section exists to prevent.

Three further bounds, all enforced in `engine/library.py`:

| Bound | Rule |
|---|---|
| Capped | `MAX_KNOWLEDGE_CHARS` (8 000), and the truncation is stated in the prompt and in the pack — a silently half-read prior is worse than none |
| Staleness-checked | every backticked path-shaped token in the file is compared against the tree listing; those absent are reported as `stale_paths`, named in a `warn` log, and listed in the prompt as *"treat every claim about them as wrong"* |
| Repeated every round | a later librarian round is a fresh model call, and a librarian that forgets the architecture note halfway through is worse than one that never read it |

Only *backticked* tokens are treated as paths, and URLs are excluded. "Stale" means "not in the
listing we were shown", so a path deeper than the depth-2 listing is a mild false positive — which
is why the wording says "deleted, renamed or moved, **or deeper than the listing reaches**" rather
than asserting the file is gone. Passing `tree_files=None` is *no opinion* and yields no stale paths
at all; it must not degrade into an empty listing, which would report every path as stale.

#### 4.9.2 Knowledge-deliverable goals

`POST /goals` with `mode: "knowledge"` makes `CODIFY.md` the goal's deliverable, through the same
four stages as §4.0a.2 and with the same rules: `_knowledge_deliverable` runs instead of `_design`
(with `KNOWLEDGE_BRIEF_PROMPT`), a non-empty `design_md` is mandatory, a step writes it verbatim,
and the critic reviews it before the user has it.

| Stage | What changes |
|---|---|
| design | `_knowledge_deliverable` (`engine/executor_design.py`) appends `KNOWLEDGE_BRIEF_PROMPT` to the goal and the evidence pack, and shows the current `CODIFY.md` as **revision material, not a prior to trust** — read fresh rather than taken from the pack, because a prior is exactly what the pack must not carry. The reply is parsed by `_design_contract` and the body is mandatory, so a drafter that returns nothing raises `AgentOutputInvalid` and the goal still plans with a `warn` |
| planner | `_design_text` names `CODIFY.md body (the file a step must produce)` — the filename comes from `DELIVERABLE_FILES[mode]`, not a literal, so a knowledge goal cannot plan a step that writes the wrong document |
| fixer | the step whose `suggested_paths` names `CODIFY.md` is handed the reviewed draft verbatim. `_deliverable_write_path` matches on the mode's file, so a design goal is never handed a knowledge body and vice versa |
| verifier | reviews the document instead of running a command — identical to §4.0a.2, and already mode-agnostic |
| critic | told the step delivers the workspace's `CODIFY.md` itself, and why that matters here: a wrong claim in it misdirects the next run rather than the next goal. Given the full content, read through the same helper the verifier uses |

**The fixer remains the only writer.** No role writes `CODIFY.md` outside a step: the design agent
authors a body in its reply, `_knowledge_deliverable` publishes it as a `design_contract` event, and
the file appears on disk only when a step writes it. A dry run proposes it and writes nothing, and
the critic reviews the stored proposal — the same bytes Apply would replay.

**The file is the mechanism, so the chain is held end to end.** `tests/test_workspace_knowledge.py::KnowledgeDeliverableEndToEndCase`
drives it over real HTTP — a drafted body becomes `CODIFY.md` on disk, the verifier and the critic
are shown *that file* rather than the draft the engine published, the scribe commits it, and the
next run's librarian is handed it as a prior (and, because the staleness check compares it against
the tree, is told which of its paths no longer exist). Unlike `DESIGN.md`, `CODIFY.md` needs no pin
to bind, so a run that published a perfect draft and wrote nothing would satisfy every unit test in
this file and leave the mode inert.

**Revision is the mode's other half, and it is not the design one.** A design goal revises a
contract it is *bound* by; a knowledge goal rewrites a prior it is *superseding*, so a body that
merely restates the existing file is worth less than none — it looks current, and the next run will
aim its reads at whatever it says. The existing `CODIFY.md` therefore reaches the drafter under a
label of its own, `--- current CODIFY.md — revision material, NOT a prior you may trust ---`, and
the drafter is told to check it against the evidence pack rather than polish it.

That block is read fresh (`read_knowledge` with no tree listing), not quoted out of the pack, so
that the drafter is not spending the pack's credibility on a file the pack never opened. The cost
is that **the fresh copy carries no staleness annotation of its own** — the annotation rides the
*other* copy, the `knowledge` block in the evidence summary, which names the absent paths and says
*"Do not plan a step against those"*. Both copies matter and only one is annotated;
`tests/test_workspace_knowledge.py::KnowledgeRevisionCase` holds each half separately, and its
end-to-end sibling holds the part only the full chain shows: the next run is holding this run's
conclusion and none of the one it replaced.

**The file is read once and published twice.** The same `read_knowledge` result feeds the prompt and
the `revises` block on the published `design_contract`, because a second read is a second file: the
day something writes to `CODIFY.md` between the two, the transcript would show the user a document
the drafter never read. `revises.stale_paths` is taken from the pack rather than from that read, so
publishing it says which of the old claims the engine had *already* distrusted — the same warning
the drafter was given, rather than the fresh read's `[]`, which would read as "nothing here is
stale" and be the opposite of the truth.

**A dry run is the half where the reviewed draft is not yet the file.** The same case holds
`nothing on disk, a review that still had an artifact, and Apply writing exactly the bytes that
were reviewed`: a dry-run `CODIFY.md` exists only as the step's stored proposal, which is what both
judges are shown and what Apply replays — the two are asserted to be the same record, not two
readings of the same draft. And because the file binds by existing, the prior follows the disk
rather than the goal: a second goal planned before Apply is told the workspace has written nothing
down, and a third planned after is handed the file. There is no pin to be refused in the meantime,
so the disk is the only thing that can say the file is not there.

**The mode is one-off, not a setting.** Rewriting what Codify believes about a repository is a
decision with a review step attached, so it is a goal mode rather than a workspace preference. The
composer exposes it as *Knowledge Deliverable* beside *Design Deliverable*.

**In the transcript it is a card of its own, not a diff.** The `design_contract` event carries the
body in `design_md` for both modes, but the two files are drawn differently. A design deliverable
is shown with the design vocabulary it came from and one action: pin it. A knowledge deliverable
has no pin — it binds by being written — so the card renders the body itself, open and height-
capped, and carries one sentence saying what makes it real: not yet written, proposed and awaiting
Apply (in which case the next run still holds whatever an earlier run left), or written, in which
case every later run's librarian reads it as a prior. `ui/src/designDeliverable.ts` mirrors the
engine's `DELIVERABLE_FILES` so a knowledge goal resolves `CODIFY.md` and never `DESIGN.md`, and
both cards share one vocabulary of body labels.

## 5. Sandbox argv allowlist

`SandboxService.run_command(workspace, argv: list[str], timeout_s: int = 120, mode="test")`.

It returns `{argv, exit_code, stdout, stderr}` and reads the command's output as **bytes, capped while it is read**
(`engine/sandbox.py`, `_Drain`): each stream keeps at most `MAX_COMMAND_OUTPUT_CHARS * 4` bytes (the cap is 200,000
characters and a UTF-8 character is at most four bytes), is still read to its end so a chatty command is never
blocked on a full pipe, and ends in `… (output truncated)` when anything was dropped. Decoding is lenient: a byte
that is not UTF-8 (`git show` of a Latin-1 file, a test printing a blob) becomes U+FFFD instead of raising, and CRLF
and a lone CR read as a newline, as they did in the text mode this replaced. Before this, the cap was applied to the
finished string, so 50 MiB of output was held (150 MiB) until the command ended, and one invalid byte was a
`UnicodeDecodeError` that no caller catches.

Two callers, two modes, one validator:

- `mode="test"` — the verifier's argv, and the conductor's `run_command` and `verify` moves **for an
  approved goal only** (stored status `RUNNING`, not plan-only: `ExecutorService._write_allowed`, the same
  gate `write` uses). Before approval the conductor's `run_command` runs in `read_only` mode, because this
  allowlist admits the repository's own code and a turn has no approval step. Planner / fixer / critic /
  scribe output NEVER reaches this function with an executable argv.
- `mode="read_only"` — the librarian's `git` / `run` requests. `ls`, `wc` and read-only git
  only, so nothing the librarian can do changes the workspace. Read-only git is specified in
  "Read-only git" below.

Every argv is a list of strings with no control character in it (`NUL` cannot be passed to a process
at all, and the other control characters are newlines and escapes no allowlisted command has a reason
to receive) — a refusal, not a crash, in both modes. Every command runs with **no stdin** (`/dev/null`):
inherited, the engine's own stdin would be the command's, and `git shortlog` with no revision, or a
test that calls `input()`, would wait out its whole timeout.

`argv[0]` basename only (no `/`). Resolved as `shutil.which` then executed with `cwd=workspace.root_path`, `env` stripped to `PATH`,
`HOME`, `LANG`, `TERM`, `VIRTUAL_ENV`, `PYTHONPATH`, `PYTHONHOME`, and `shell=False`, in a new session so a timeout can kill the
whole process group. A command that times out reports exit `124` (`timeout(1)`'s code); one stopped because its goal
was cancelled reports `130` and says so in its stderr, so the two are never confused with each other or with an exit the
command chose.

| argv[0] | Allowed remaining args |
|---|---|
| `pytest` | flags from `{ -q, -v, --tb=short, --no-header, --maxfail=N }` + paths under root |
| `python` | exactly `-m pytest` + pytest-allowed tail; OR exactly one script path under root ending `.py`. **FORBIDDEN:** `-c`, `-m` other than `pytest`, `-` |
| `npm` | `test` or `run` + script name matching `^[A-Za-z0-9_:-]+$` |
| `pnpm` | same as npm |
| `cargo` | `test` + optional `--`, `--lib`, `--bins`, `--quiet`; or `check` / `clippy` + optional `--lib`, `--bins`, `--all-targets`, `--quiet` (no `--`: see below) |
| `go` | `test` or `vet` + `./...` or paths under root |
| `ruff` | `check` + paths under root, **no flags** |
| `mypy` | `--strict`, `--ignore-missing-imports` + paths under root |
| `tsc` | `--noEmit` (required) + optional `-p` / `--project` PATH under root |
| `make` | exactly `make lint` or `make typecheck` — no flags, no variables, no other target, bare `make` refused |
| `git` | `status`, `diff`, `log -1` only (no write); run hardened like read-only git, below |

Anything else → `command_not_allowed`. No shell (`shell=False`).

**Linters and type-checkers (`ruff`, `mypy`, `tsc`, `cargo check|clippy`, `go vet`, `make lint|typecheck`)
are `test`-mode commands, so they sit behind the same approval gate as `pytest`**: `read_only` refuses all
of them, and the conductor's `run_command` is `read_only` until the goal is `RUNNING`. They are admitted because
of what the *engine* adds after validation (`sandbox.hardened_args`), never because of anything the model
supplies: ruff runs with `--no-cache --output-format=concise`, mypy with `--cache-dir=/dev/null`, tsc with
`--pretty false` (and the model must name `--noEmit`), and clippy as `cargo clippy … -- -D warnings`, so a
warning fails the run. The model is given no way to ask a checker to *edit* (`ruff --fix`, `ruff format`,
`ruff --add-noqa`, `cargo clippy --fix`), to *install* (`mypy --install-types`), to *name* an interpreter, a
config, a plugin, a vet tool, a makefile or a directory (`--python-executable`, `--config-file`, `-vettool`,
`make -f`, `make -C`, `--manifest-path`), or to *build* (`tsc --build`, `--incremental`, `--outDir`). Flags are
compared as exact spellings, because argparse-style tools accept any unambiguous prefix of a long option.
What they still run is repository code — a mypy plugin named in `mypy.ini`, a `build.rs`, a cgo build under
`go vet`, whatever a `Makefile` says — which is the accepted risk in `03` §1.4: approving a goal is approving
the project's own tooling. Proven by `tests/test_sandbox_lint.py` (an accept/refuse table, the flags the child
is handed, and real `ruff`, `mypy` and `make` runs that assert on the workspace afterwards) and, for the
approval gate, `tests/test_conductor.py::test_a_linter_runs_project_code_only_once_the_plan_is_approved`.

### Read-only git

`engine/git_readonly.py` is the one owner of what a model may ask git to read. Both doors call it — the
librarian's `run_command(mode="read_only")` and the conductor's `git_history` (`GitService.read_only`) —
so the two cannot drift, and `sandbox.READ_ONLY_GIT_SUBCOMMANDS` and `GitService.READ_ONLY_ARGV` are
both the table's own key set, not literals.

**An option is refused unless the table names it, exactly.** This replaced a denylist of flag spellings,
which failed four ways when it was run against real git: git accepts any unambiguous prefix of a long
option (`git grep --open-files-in-pa="touch X"` started `touch`; `git branch -v --del NAME` deleted a
ref); `git branch -v NAME` and `git tag --sort=x NAME` created refs, because the bare word is not a flag
a denylist can see; `git diff <file outside the tree> /dev/null` printed a file from outside the
workspace, because git turns a `diff` with a path outside the tree into `--no-index` on its own; and
nothing looked at positionals at all. The table lists only history, diff, blame, search and ref
listing options. It contains no option that writes (`--output`, `-d`, `-m`, `-c`,
`--set-upstream-to`, `--edit-description`), runs a program (`--ext-diff`, `--textconv`, `-O`,
`--open-files-in-pager`, `--show-signature`, `tag -v`, and a `--format`/`--pretty` that asks for a
signature, which makes git run gpg), names a file (`--file`, `-f`, `--exclude-from`, `--orderfile`,
`--contents`, `--ignore-revs-file`) or points git at another tree (`--no-index`, `--git-dir`,
`--work-tree`, `-C`, `-c`, global options at all). A refusal names the options the subcommand does accept.

- Value options are attached (`--since=DATE`) unless they are short and git itself consumes the next
  word (`-n 5`, `-e PATTERN`), so the validator and git never disagree about which word was a value.
- Every positional must stay inside the workspace (`FileSystemService.resolve`: no absolute path, no
  `..`, no `.git`, symlinks followed), including the path half of `REV:path`. `HEAD~1..HEAD` is a range
  and is allowed. The one exception is `git grep`'s own pattern.
- `branch` and `tag` accept a positional only with `--list`/`-l`, where it is a pattern.

**The child process.** Read-only git — and the verifier's `git status`/`diff`/`log -1` — starts as
`git --no-pager -c core.fsmonitor=false SUBCOMMAND [--no-ext-diff --no-textconv] ARGS…` with an
environment of `PATH`, `LANG`, `LC_ALL`, `LC_CTYPE` and `TZ` plus `GIT_CONFIG_GLOBAL=/dev/null`,
`GIT_CONFIG_NOSYSTEM`, `GIT_OPTIONAL_LOCKS=0` (a `status` otherwise rewrites the index), `GIT_TERMINAL_PROMPT=0`
and `GIT_CEILING_DIRECTORIES` set to the workspace's parent, so a workspace that is not itself a
repository cannot read the history of one above it. No provider key or boot token is in that
environment: `GitService.read_only` used to inherit the engine's whole one. The conductor's door is
also **bounded**: 60 seconds (`GitService.read_only_timeout_s`), after which the command's whole
process group is stopped and the model gets a sentence saying so.

**What this does not cover, on purpose.** The repository's *own* config (`.git/config`,
`.gitattributes`) is trusted, exactly as it is when a developer runs git in that repository. A
`core.fsmonitor`, `diff.external` or textconv driver there is neutralised above because those are the
ones plain `status`/`diff`/`log -p` start; a `filter.<name>.clean` command in that config still runs
when git compares the working tree, and there is no single switch for those. A workspace whose
`.git/config` came from someone else is not made safe by this table.

Proven by `tests/test_sandbox_read_only_git.py`, which runs real git against a real repository and
asserts on the repository afterwards; its property tests are the ones that would have caught the class
(no abbreviation of any listed option is accepted).

**What this is, plainly, because the name oversells it: an argv-shape allowlist and nothing more.** It is
not a sandbox in the containment sense, and the table above is not a boundary around the code that runs
inside it. An allowlisted command is *the repository's own code* — `npm test` runs whatever `package.json`
says, `python3 script.py` runs the workspace's script, `pytest` imports every `conftest.py` in the path — and
it runs as the user, with the user's `HOME` in the environment, able to read and write anything the user can.
The properties this service does provide, each asserted in `tests/test_sandbox.py`: credentials are stripped
from the environment (`GitService._workspace_env` and the list above, so a checked-in test script cannot read
a provider key out of `os.environ`), no shell is involved, the argv cannot name a path outside the workspace,
and the command's whole process tree dies with its timeout or with the engine.

So the honest reading of `mode="test"` is that it admits the repo's own code, whoever proposed the argv — the
verifier's proposal or the conductor's `run_command` / `verify` moves (invariant 6, `docs/00` §6.6), which
share this allowlist rather than a wider one. Running a repository's tests means running the repository's code.
Containment, if it is ever wanted, is a container/namespace decision that belongs above this function
(`03` §1), not a longer argv table here.

A refusal is **not** a step failure: the command never executed, and the verifier is the agent that can pick
another one, so the rejection goes back to it as feedback (`04` §4.3). The step only fails if the verifier
then reports an actual failure, or if it keeps proposing refused commands past the retry cap.

## 6. Boot handshake

Engine stdout, first line, exactly:

```
CODIFY_ENGINE token=<hex> port=<int>
```

The line is printed **only once the engine can serve**: `serve()` opens the SQLite file — running its migrations — *before* it announces, so an unopenable or corrupt database ends the process with a message on stderr naming the file (and the `sqlite3` error, re-raised) instead of a `CODIFY_ENGINE` line the shell would then trust for the rest of a boot that could never finish (`tests/test_boot_announces_ready_late.py`). `lifespan` closes that connection on every road out — shutdown, a failed startup, and a migration that raises inside `db.connect` — so a boot that fails leaves no descriptor behind (`tests/test_connections_are_closed.py`).

`token` = 32 bytes CSPRNG hex (64 chars), created once and kept at `<state dir>/boot_token` (`0600`). Desktop reads this line, then attaches `Authorization: Bearer <token>` to HTTP and `?token=` is **forbidden** (query leakage). WS: first text frame from client `{"type":"auth","token":"<hex>"}` or HTTP header on the Upgrade.

WS URL: `ws://127.0.0.1:<port>/ws/goals/{id}`. After auth, server sends events with `sequence > 0` live; client SHOULD `GET /goals/{id}/events?after=` for gap fill.

The handler **reads its socket as well as writing it**, and a client that leaves ends the handler at
once. It used to write only, so a departed client was noticed when a `send` failed — which a goal with
no new events never attempts — and every finished goal that was ever viewed (the UI closes the socket
on each terminal status) left a poller re-reading the goal four times a second for nobody: idle engine
CPU went 0.2 % → 3.6 % → 7.0 % → 13.9 % of a core over 50, 200 and 500 views, until restart (audit of
2026-09-29, M9; `tests/test_ws_goal_lifecycle.py`). Frames a client sends are ignored. Close codes the
client may act on: `4401` bad token, `4404` no such goal (checked only *after* auth, so an
unauthenticated peer cannot probe ids). Neither changes by asking again, so `ui/src/goalStream.ts`
treats both as final — no reconnect, `onGone(code)` — while any other close reconnects with backoff.

### 6.0 Engine-level frames

`ws://127.0.0.1:<port>/ws/engine` carries what belongs to *no* goal, on the same auth contract (boot
token on the Upgrade, or the `{"type": "auth"}` first frame; `4401` on a bad one — invariant 3, docs/00
§6.3, holding on a socket as on every route). It is a sibling of `/ws/goals/{id}`, not an extension of
it: the goal stream is a durable sequenced log that replays from 0, and a frame with no `goal_id` and no
`sequence` in it is a frame a deduping client drops.

One frame exists today:

| frame | payload | when |
|---|---|---|
| `model_catalog_changed` | `{added: {provider: [id]}, removed: {provider: [id]}, fetched_at}` | a provider's discovered model list differs from the previous sweep |
| `model_catalog_checked` | `{fetched_at}` | every sweep that found no change — a time, not a diff |

The payload is a **diff, never the catalogue** — eight providers at 500 models each is a payload no
screen asked for, sent on every change. The client re-reads `GET /models` with `refresh` **false**,
which is a cache hit because the engine's watcher is what discovered the change; asking for a refresh
would ask all eight providers again for something the client was just told. A provider that stopped
answering is reported as everything it had, `removed` — a provider gone quiet and a provider with
nothing new are otherwise identical, and only one of them is a change.

`model_catalog_checked` is the other half, and it is what lets a client stop polling. A screen
showing the age of its list ("checked 40s ago") can only do that honestly if somebody is keeping the
number true, and the engine is the only party that can. Both frames mean one thing to a client —
"there is a current answer, re-read it" — and are told apart only so a test can say which arrived.

The engine sweeps on the same period as the catalogue's own cache TTL (`model_catalog.DEFAULT_TTL_S`),
so every sweep is a real answer rather than an echo, and **only while at least one client is
connected** — an idle engine must not spend a provider's rate limit on a list nobody is reading. The
first sweep after a client connects is a silent baseline; announcing it would tell someone opening the
app after an hour away that every model everywhere is new.

Token lifetime is the state directory, not the process. It is written once, with `O_EXCL`, so two engines booting against the same state dir converge on one value rather than each minting its own. This replaced a per-spawn rotation: a client that cached the token was rejected with 401 after every restart, and only the desktop shell could recover, by re-reading the live handshake over IPC — a browser tab pointed at a dev engine has no shell to ask and stayed broken until a human reloaded it. The cost is a longer-lived credential, affordable only because the socket is `127.0.0.1`: anything able to present this token could read the file, and the database beside it, without it. `CODIFY_BOOT_TOKEN` overrides the value for a caller that wants a token scoped to one process, and a state directory that cannot be written falls back to a per-boot token rather than refusing to boot.

### 6.1 Death is bounded

Closing the app ends the engine; so does a signal to the shell, which is why the engine watches the
shell's pid instead of trusting the shell's own exit handler (`CODIFY_PARENT_PID`, `engine/watchdog.py`).
`SIGTERM` and `SIGINT` are now handled by the shell itself, which asks the engine to stop on the way out
and kills it only if it outlasts the bound below (`09` §5.4.1), so the watchdog is the backstop behind
that rather than the mechanism — the cases it exists for are the ones no handler reaches: `SIGKILL`, a
crash, and a shell that dies mid-request.

Being *told* to stop is only the start of a shutdown, and the shutdown is uvicorn's: it waits for
in-flight work, and its default is to wait **without any deadline**. That default is the orphan this
subsection exists for. With one event stream open — or one turn still generating — `SIGTERM` closed the
listening socket within a second and the process then held the SQLite file for as long as the work ran.
Repeating the signal changed nothing: uvicorn's handler is still installed and only sets the same flag
again. Both ways in therefore arm a deadline **before** the graceful path starts: a `SIGTERM` aimed at
the engine itself (a stray `kill`, a supervisor, the shell asking it to stop), and the death of the
parent it watches — which is the backstop behind that request rather than a substitute for it, since a
shell killed mid-request never gets to send one.
`timeout_graceful_shutdown` bounds the in-flight wait at `GRACEFUL_SHUTDOWN_S` (3 s — enough for a commit,
short enough that nobody is waiting on it), and a daemon timer at `HARD_EXIT_GRACE_S` (6 s, deliberately
outside that window) ends the process if the graceful path has not finished. That 6 s is announced to the
shell on the boot handshake as `hard_exit_s`, because the shell now asks the engine to stop and waits
exactly this long before escalating to `SIGKILL` (`09` §5.4.1) — the one number both sides hold, rather
than two copies free to drift.

`os._exit` and not `sys.exit` or a return from `main()`, because finalization joins every non-daemon
thread and the threads left here are exactly the ones nobody can cancel: the `asyncio.to_thread` workers
behind a sandboxed command or the native folder picker. A deadline that ran through that join would have
no bound at all. The engine's own children are the other half of the same contract: `07` §1.

Two holes the first version of this machinery left, both the shape of "the guard died guarding". The
handler's first act was the receipt — the stderr line naming the dead parent — and the engine's stderr
is a pipe whose reader is the shell: after a `SIGKILL` that reader is gone, so the write raised
`BrokenPipeError`, the watchdog thread died with the deadline unarmed and the signal unsent, and the
engine outlived its shell forever, with no log to explain it because the log *was* the pipe. The
delivery order is therefore fixed — deadline, signal, then the receipt, guarded so a write can never
undo a delivered stop — and the second hole closed with it: the watch armed only in `serve()`, after
the imports, so a shell dying mid-boot orphaned an engine that never armed anything. `python3 -m engine`
now starts the watch before the first import (`engine/__main__.py`); the later call from `serve()` is
a no-op while that one lives. Pinned end to end by `tests/test_watchdog_dead_pipe.py`, which runs the
death handler against a stderr pipe with no reader, in a real child, on both engines.

The backstop says so on the way out, on stderr: `shutdown unfinished after 6s — exiting anyway`. That
line is the entire explanation of an engine that vanished holding a hung websocket, which is why the
shell tails the engine's stderr and shows the tail in the app rather than inheriting it into a console
(`09` §5.5) — a bounded shutdown nobody can read is indistinguishable from a crash.

## 7. Key storage

Two backends, one namespace. Names: `codify` service + username `providers/{slug}` for a provider key,
and `codify/agents/{role}` for a role-scoped key. Keys are never written to SQLite and never echoed back.

1. **OS keychain** (preferred): `keyring` over the Linux Secret Service (GNOME Keyring, KWallet).
   A `fail.Keyring` backend counts as *unavailable*: it accepts writes and raises on read, so
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

## 8. Tracing and replay

The event log records *what happened* — a verdict, a diff, a commit. It does not record *what was
said*, so a run that misbehaved could not be reproduced: the prompt that produced the bad reply was
gone, and a bug report was a story. A trace is the recording that makes it a reproduction.

### 8.1 What is kept, and what is deliberately not

Opt-in per goal and off by default (`Goal.trace`, `GoalCreate.trace`). A recording is a copy of the
model's output about the user's code, so nothing at all is kept for a goal that did not ask.

| Kept (`trace_calls`) | Not kept |
|---|---|
| role, provider, model, temperature, `max_tokens` | any file in the workspace |
| the response, whole — a replay can serve nothing else | the prompt **text**, unless `CODIFY_TRACE_PROMPTS=1` |
| `prompt_hash` / `system_hash`: 32 hex chars of SHA-256 over `system \x00 user` | the goal's source, evidence pack, or diff |
| `input_tokens`, `output_tokens`, `duration_ms` | anything at all for a goal with `trace = 0` |

A conductor-driven goal records its loop's calls under `role = "conductor"`, one row per call. Its
`user_prompt` is the JSON list of the messages the model was *newly* shown since the previous call and its
`response` is `{text, tool_calls}`: the rows in order hold the whole conversation, and resending the history in
every row would be quadratic in the length of the run.

The digest is the point rather than an omission. It is what a replay *matches on*, and the prompt is
the most sensitive thing in a run: the goal, the evidence pack, and the user's own source. Keeping
the digest means a replay can prove it is replaying the same request; not keeping the text means a
stolen database is not a transcript of the user's code. `CODIFY_TRACE_PROMPTS=1` opts into the text,
for the case where "exactly what was the model handed" is the question being asked.

Both halves of that separator matter: `("ab", "c")` and `("a", "bc")` must not produce the same
digest, so the system and user prompts are joined with a byte neither can contain.

### 8.2 Enabling, stopping, deleting

* **Armed at creation** (`POST /goals` with `trace: true`) — the usual path, and the only one the
  composer offers.
* **`PUT /goals/{id}/trace`** while the goal is still `PLANNING`. Enabling a goal that has already
  started is `409 trace_locked`: a recording that begins halfway is a trace of half a run, and the
  replay it implies would be missing the calls that shaped the first half.
* **Turning it off is always allowed**, at any status. Stopping is not a state change; refusing it
  would make a user cancel a run to stop recording it.
* **`DELETE /goals/{id}/trace`** removes the recording and nothing else — not the goal, not its
  events, not a single file. It is idempotent and never refused on status, because deleting a copy
  of your own run is your call.

Recording is checked **per call**, not once when the run starts, so a user who notices a problem
mid-planning can switch it off and the very next call is not stored.

### 8.3 Retention

`trace_retention_days` (Settings → Engine, default **30**, band 0–730; `0` keeps everything) bounds
how long a recording lives, so the table's growth is a decision rather than an accident. It is
enforced on the `/stats/overview` read — the same moment and on the same terms as
`stats_retention_days` — so a lowered policy takes effect on the next read instead of at the next
run of a job that may never be scheduled. `trace_calls(created_at)` is indexed for it: the sweep
walks a table with one row per model call of every recorded run.

Deleting a goal deletes its recording through `ON DELETE CASCADE`, so a recording never outlives the
run it describes.

### 8.4 Replay

```
python3 scripts/replay_trace.py --goal <goal-id> [--db PATH] [--from TREE] [--into DIR]
```

Exit codes: `0` the recording replayed, `1` it diverged, `2` it could not be read.

Three rules, and each is the difference between a replay and a story:

* **Nothing is written to your workspace.** The tree is copied into a scratch directory and the
  replay goal is pointed at the copy, so a fixer writing files inside a replay cannot touch the code
  the recording came from. The scratch path is printed so a divergence can be inspected.
* **`--from` names the tree as it was when the run started.** It defaults to the recorded goal's own
  workspace, which is only still that tree if the run wrote nothing. A run that fixed a file left
  the workspace one line ahead of its own prompts, so replaying *that* run wants a pristine copy
  here — a checkout at the recorded commit, typically.
* **A mismatch refuses rather than answers.** `ReplayProvider` matches on `(role, prompt_hash)` in
  recording order and raises `TraceMismatch` on the first call its recording does not hold. Serving
  the recorded reply to a different question would produce a green run that proves nothing, which is
  worse than no run at all: it looks like evidence. The message names the role and both digests.

A recording that holds `conductor` rows (a goal the conductor drove) still replays: the replay serves the typed
role calls, not a tool-calling loop, so the conductor's rows are counted on their own line
(`conductor_calls_skipped`) and left out of the served-versus-recorded arithmetic, as the gate's are.

The refusal arrives as a *failed goal* rather than as an exception, because `TraceMismatch` is a
`ProviderError` and the orchestrator absorbs it into `agent_call_failed` before the retries give up.
The CLI therefore reports the **first** `trace_mismatch` in the stream, not the fatal stage: a run
that drifted usually refuses at the librarian and only fails later at the planner, and naming the
planner would send the reader to the wrong call.

The Laya gate is replayed, not re-run — its recorded `laya_decision` verdict is read back and
returned, because a fresh gate decision would change what the run did before its first model call.

### 8.5 A recording that cannot be written

A failed write **never fails the run**: a trace is evidence, not a deliverable, and the goal it was
recording is the thing that matters. It is also not silent. The reason is kept and returned as
`recording_error` on `GET /goals/{id}/trace`, keyed to the goal it happened on.

Without that, "I never armed it" and "I armed it and the write failed" are the same row of zeros,
and the only one a user can act on is the second. The summary reports `null` normally, so the
field's shape does not change depending on whether something broke.

## 9. The browser bridge

Three routes, all behind the same bearer token as everything else (`01` §5, `03` §1.3), and deliberately
not a WebSocket. Every socket in this engine carries exactly one goal's events and nothing else, and a
second long-lived socket on the same paths is how two conversations end up interleaved
(`tests/stream_isolation.py`). A long poll is a request with a reply: it cannot outlive its caller and
it cannot land a frame on anybody's event stream.

| Route | Who calls it | What it is |
|---|---|---|
| `GET /bridge/state` | shell | `{attached, inflight, claimed, timeout_s, max_chars}` — read-only, so the shell can decide whether to start polling at all |
| `GET /bridge/next?wait=<s>` | shell | the oldest unanswered question, or `{"id": null}` when `wait` seconds pass with none. `op` is `read_page` or `navigate`; `navigate` carries `url` |
| `POST /bridge/answer` | shell | `{"id", "ok", "result" \| "error"}`; `{"accepted": false}` for an id nobody issued |

`wait` is clamped to 0–60 s. The shell reads the base URL and boot token on **every** poll rather than
capturing them at startup, because the engine can be restarted underneath a running shell and a base URL
remembered from boot is wrong by the time it is next used.

Two facts about liveness and refusal, both asserted in `tests/test_webview_bridge.py`:

- **Calling `/bridge/next` is the liveness signal.** There is no "connect" step; a poll that arrives
  refreshes the bridge's idea of when a shell was last seen, and three polls of silence means it is gone.
  A sticky `attached` flag would outlive the app that set it — the user quits Codify mid-turn, and every
  later turn spends its read timeout waiting for a window that no longer exists.
- **The in-process queue is one queue.** The routes and `ExecutorService`'s `read_page` share one
  `WebviewBridge` on `app.state`. A read waits on a future only the answer route can land, so a second
  bridge instance would be a read that times out forever, and the refusal has to arrive *before* the
  wait rather than after it: an engine with no shell behind it — a benchmark, a CLI turn, a headless test
  — answers in one sentence instead of spending a conductor turn's budget on silence.
- **One operation at a time, whichever it is.** `_busy` covers reads *and* navigations, because the thing
  being serialised is the user's one visible page. Queueing a navigation behind a read would let the read
  return the text of a page the move had already left.

The librarian does not browse. Its reply contract is a fixed JSON document (`04` §4.0) and adding a
network-shaped field to it would be a change to every role's prompt for a capability the conductor
already has. `read_page` is a conductor tool instead, and evidence it gathered can be quoted in a
`recon` task like anything else.

### 9.1 The surface bridge: the app window's own surfaces

The browser's pages belong to the Tauri shell, so the shell polls `/bridge/*`. The editor belongs to the UI process,
and so will whatever surface comes next; for those the **window itself** polls. Same shape, same reasons (a long poll,
not a socket; a poll is the liveness signal; one in-process queue on `app.state`), and the part that does not change from
surface to surface is `engine/surfaces.py`:

| Route | Who calls it | What it is |
|---|---|---|
| `GET /surfaces/state` | window | `{attached, inflight, timeout_s, surfaces: {name: [ops]}}` |
| `GET /surfaces/next?wait=<s>` | window | the oldest question not yet handed out, `{id, surface, op, workspace_id, args}`, or `{"id": null}` after `wait` seconds (0–60) |
| `POST /surfaces/answer` | window | `{id, ok, result? \| error?}` **`extra: forbid`** (the browser bridge's answer ignores extras; this one does not); `{"accepted": false}` for an id nobody issued, a second answer, or one after the timeout |

- **An operation is a fixed string from a table**, `{surface: {op: Op}}`, and an `Op` pairs the shape of what the engine may
  send with the shape of what may come back. A model cannot name an op. Adding a surface is one table entry and its
  vocabulary (`engine/surface_editor.py` is the editor's); the bridge does not change.
- **Arguments are validated on the engine's side before they cross.** A refusal costs no round trip, and a field the op does
  not name never leaves the engine.
- **An answer is validated into its result model**: the fields it names and nothing else, everything that can be long
  capped (a file read is at most 400 lines of 2 000 characters; at most 50 editors; a selection's text 2 000), and a value
  of the wrong type refused rather than repaired. What the editor holds is workspace text, which is third-party text, so
  the model is shown it as a quotation, framed before it arrives.
- **Several questions may be in flight**, unlike the browser's one visible page: an editor answers in milliseconds, and the
  conductor may ask while a person is typing. They are handed out once each, in the order asked.
- **No window is a sentence, not a hang.** A window that has not polled in three poll intervals is not attached, and the
  tool says so at once; a question that goes unanswered for `ASK_TIMEOUT_S` (15 s) says what to do instead. An op may name
  a timeout of its own (`Op.timeout_s`, read when the question is asked): the machine's `run` types a command and waits for
  its output to settle, so it is given 45 s against a wait of at most 30.
- **The `machine` surface** (`engine/surface_machine.py`) has four ops, `read`, `run`, `key` and `reset`, and **no `open`**: opening
  a machine is a person's act, as it is for a terminal, and a table with no entry for it is how that is true. `run` refuses a
  command with a control character (that is a key pressed without a name; `key` takes one of twelve fixed names and the
  engine never sends bytes). `reset` takes a machine's id and nothing else and remakes that machine from the recipe the person
  opened it with (it has a timeout of its own: it ends a jail, makes one and waits for its prompt); every machine the engine is
  told about says whether its project is the machine's own copy. What a machine prints is program output, so every answer is framed as a quotation before it
  arrives. `tests/test_machine_surface.py` holds all of it, including that the modules which define this door import nothing
  that can start a process or write a file (docs/00 §6.6).
- **Questions and answers, never authority.** There is no path from this module to the filesystem, the sandbox, the boot
  token or a Tauri command. `edit` changes the text in an open buffer; the editor's own Save, a person's, is what writes
  (`§3.0.3`, docs/00 §6.9), and `test_invariants_at_their_boundary.TestAPersonsSaveIsTheOneOtherDoor` fails if this module or
  any conductor module names that writer.

