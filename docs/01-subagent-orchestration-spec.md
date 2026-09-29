# Codify — Sub-Agent Orchestration Spec

Normative for agent slots, providers, registry, and `/settings/agents`. Persistence: `04`. Security: `03`.

## 1. The 7 role slots + the gate slot

Exactly 7 role slots, plus one pre-flight **gate** slot (`laya`). Users cannot add or remove
**roles**. Provider/model/key/`base_url` per slot are Settings-only.

The slot count is fixed; the **order is not**. These seven are abilities the conductor chooses
between through *moves* (`recon`, `design`, `plan`, `write`, `verify`, `review`, `summarize`) and
skills it loads — it is a loop, not an eighth kind of agent. The sequence
librarian → design → planner → fixer → verifier → critic → scribe is the built-in `ship-a-change`
skill (`engine/builtin_skills/ship-a-change.md`), which a workspace may replace. Calling them a
fixed pipeline was true of the compiled path this replaced and is not true of the engine now:
"the scribe never ran" is a statement about a move, not about a stage that was skipped.

A slot is a **different ability**, not a different persona. One role reads the workspace, one locks a
direction, one writes it, one runs commands, one judges, one records — and the engine enforces that
split rather than asking the prompts to be well behaved:

| Slot (`role` id) | Ability | Job | Output schema |
|---|---|---|---|
| `laya` | typed decisions only | Typed pre-flight decisions (intent / risk / injection) — may fail the goal | `05` §5 |
| `librarian` | **read** files, **search** the tree, read-only git, read-only inspect commands. Cannot write. | The evidence pack everything downstream plans from | `04` §4.0 |
| `design` | reasons only, no tools; cannot write | The locked direction (artifact, design system, tokens, components, acceptance) that the planner plans against and the fixer obeys — or, in a deliverable goal, the workspace's own `DESIGN.md` (`design`) or `CODIFY.md` (`knowledge`) in draft | `04` §4.0a, §4.0a.2, §4.9 |
| `planner` | reasons only, no tools | Ordered `PlanStep[]` from the goal + the evidence pack | `04` §4.1 |
| `fixer` | **the only writer** | File edits | `04` §4.2 |
| `verifier` | **the only role that executes a command** | Argv + verdict + what actually ran | `04` §4.3 |
| `critic` | judgement only; **the only role that can stop a step** | approve / request-changes | `04` §4.4 |
| `scribe` | wording only | summary + commit | `04` §4.5 |

Timing: `laya`, `librarian`, `design` and `planner` run **once per goal** (the design agent after the
librarian, so it locks a direction from evidence rather than from a blank page); `fixer`, `verifier`,
`critic` and `scribe` run **once per step**. `GET /settings/roles` returns each slot's `job` and `timing`, and the
settings screen renders that rather than keeping its own description — a second copy is how a screen
ends up promising an ability the engine no longer grants.

The gate is not a pipeline stage: it runs once per goal before the librarian and never receives or
produces a `PlanStep`. See `05` for its typed contract, engines, and blocking policy.

### 1.1 Why the librarian exists

Before it, the planner received a title and a description **and nothing else**, and the fixer read
only the paths that blind planner guessed — so a wrong guess meant no agent ever saw the right file.
The librarian is a bounded reconnaissance pass (`MAX_LIBRARY_ROUNDS = 3`): it asks for material
(reads / searches / git / inspect commands), the engine fetches it, it asks again, and it finishes by
setting `enough` or by asking for nothing.

Two rules make its evidence usable:

1. **Nothing it can do changes the workspace.** Reads and searches are pure Python; `git` and inspect
   commands go through `SandboxService` in `read_only` mode — the one place command allowlisting
   lives, so there is no second validator to drift. `git commit`, `git add`, `rm`, `pytest` and
   `-C`/`--output` redirections are all refused there.
2. **Every claim is checked.** A path is kept only if the librarian actually opened it, a search
   showed a matching line in it, or it exists in the tree listing; anything else is dropped and
   logged (`librarian cited N path(s) it never saw`). A confident list of files that do not exist is
   how "planning from the repository" becomes planning from a hallucination.

**One thing is read *before* all of that, and is deliberately not evidence.** A workspace's
`CODIFY.md` (`04` §4.9) is handed to the librarian as a **prior**: it is capped, its backticked
paths are checked against the tree and the ones that no longer resolve are reported as stale, and it
never enters the evidence pack's `files` — the pack's whole value is that a path in it was actually
seen, and a note somebody wrote months ago cannot make that promise. It is there to aim reads, not
to be cited.

**What the conductor asked for is in its prompt.** A `recon` move carries a `task`, and it reaches
the librarian beside the goal, labelled as an addition (§5.1a). A reconnaissance pass that ignored
what it had been sent to look for was not doing the job it was dispatched for.

### 1.1a Why the design agent exists

The librarian fixed the *where*; the design agent fixes the *what it should look like*. Without it,
each step's fixer invented its own palette, type scale and component names, so a multi-step UI goal
drifted — the first step's `#2f81f7` and the fourth step's `#3b82f6` were both "the accent" — and the
critic had no standard to judge against beyond the request's wording.

It is one bounded call between the librarian and the planner, with no tools: it decides, it writes
nothing, so the fixer stays the only role whose changes reach the disk. The contract it locks is
published (`design_contract`) and read back per step (`_design_for`), so the planner, the fixer and
the critic work from the same direction rather than from the prompt that happened to produce it.

Two rules keep it from becoming a tax on goals with no visual surface:

1. **It may decline.** `applies: false` (or any reply that names no direction) is an answer, not a
   failure: nothing is published, and the planner is told there is no direction instead of being
   handed an invented one.
2. **It can never fail a goal.** A provider error or a malformed reply is logged as a warning and
   planning continues without a contract — the same rule the librarian gets, for the same reason: an
   aid that can kill the goal is a liability, not an aid.

Its output contract, vocabulary and bounds live in `04` §4.0a.

**Two goal modes invert this without changing the slot.** A goal created with `mode: "design"`
(`04` §4.0a.2) makes the workspace's brand contract the deliverable: the same agent, the same single
bounded call, the same no-tools rule — but its `design_md` body is what a planned step writes to
`DESIGN.md`, verbatim and whole. `mode: "knowledge"` (`04` §4.9) is the same shape pointed at
`CODIFY.md`, the file the *next* run's librarian reads as a prior. The invariants are untouched in
both cases, which is the point: the fixer is still the only writer, the verifier still reviews
instead of running something it does not have, the critic still decides whether the step stands, and
the pin is still the user's own action. Nothing in the engine ever sets
`workspaces.design_contract_path` from a model's output, and nothing outside a step ever writes
`CODIFY.md`.

### 1.2 Legacy role ids

An earlier build shipped `planner` / `coder` / `tester` / `reviewer` / `summarizer`. On startup
`db.migrate_agent_roles` moves a configured legacy row onto the slot that inherited the job
(`coder`→`fixer`, `tester`→`verifier`, `reviewer`→`critic`, `summarizer`→`scribe`) — including the
keychain entry, via `Keychain.rename_role_key`. A row moves only when the new slot is still the
untouched seed; if the user has since configured the new slot, their choice wins and the stale row is
dropped. Without this, an upgrade would report every role as unconfigured with no mention of the
model the user had actually been running.

## 2. Data model

### 2.1 Provider is agnostic

`provider` is a **slug string**, not a closed enum. Engine ships four **built-in** slugs. Settings MAY save any other slug if `protocol` + `base_url` are set.

```python
AgentRole = Literal["laya", "librarian", "design", "planner", "fixer", "verifier", "critic", "scribe"]
ProviderProtocol = Literal["anthropic", "openai_compat", "ollama"]
SYSTEM_PROMPT_OVERRIDE_MAX = 32768

BUILTIN_PROVIDERS: dict[str, dict] = {
    "anthropic": {"protocol": "anthropic", "base_url": "https://api.anthropic.com", "needs_key": True, "local_only": False},
    "openai":    {"protocol": "openai_compat", "base_url": "https://api.openai.com/v1", "needs_key": True, "local_only": False},
    "deepseek":  {"protocol": "openai_compat", "base_url": "https://api.deepseek.com", "needs_key": True, "local_only": False},
    "ollama":    {"protocol": "ollama", "base_url": "http://127.0.0.1:11434", "needs_key": False, "local_only": True},
}
```

| Slug | Protocol | Default `base_url` | Key |
|---|---|---|---|
| `anthropic` | Messages API | `https://api.anthropic.com` | yes |
| `openai` | Chat Completions | `https://api.openai.com/v1` | yes |
| `deepseek` | Chat Completions (OpenAI-compatible) | `https://api.deepseek.com` | yes |
| `ollama` | Ollama `/api/generate` | `http://127.0.0.1:11434` | no |

Custom slug (e.g. `openrouter`, `groq`): `protocol` MUST be `openai_compat` or `anthropic` or `ollama`. `base_url` REQUIRED. `ollama` / `local_only` → `validate_local_base_url`. Remote custom URLs are allowed (single-user); still no query-token, still Bearer.

**A key is required on every `openai_compat` call path, local servers included.** `complete` and `complete_with_tools` both refuse an empty key with `missing_api_key` before any request is made (`complete_with_tools` once sent an empty bearer token, so a keyless server answered the conductor and refused every role). A local OpenAI-compatible server — llama.cpp, LM Studio, vLLM — ignores the key, so any placeholder works (`benchmarks/seed_endpoint.py` stores one and says so). Genuinely keyless local servers would be a product change (discovery, repair and the settings screen all read `needs_key`), not made here.

Every protocol's `complete` takes the same keyword-only `num_ctx` and `keep_alive` (see
2.2), and **only Ollama reads either**. `num_ctx` is the one parameter these APIs expose as a
request option, so it becomes an entry in the `options` dict sent to `/api/generate` and
`/api/chat`, on the blocking and the streaming path alike. `keep_alive` is *not* — Ollama
reads it as a **sibling** of `options`, and nesting it there is accepted-but-ignored, which
would be the worst outcome available: the setting saves, the UI shows it, and the model is
reloaded exactly as before with nothing reporting a problem. It is sent only when the role
configured one; omission leaves the server's own five-minute window alone. The other
providers accept both keywords and ignore them, because they are part of the signature every
provider in this repo shares and a protocol that did not take them would be a different
signature to stub at every call site.

`keep_alive` is not a latency setting for a single call. Role calls inside one goal are
seconds apart and already warm; what it covers is the gap *between* goals, where a role used
every ten minutes pays a full model reload each time.

`GET /settings/providers` → built-in catalog + any extra slugs already stored on the agent rows.

### 2.2 `AgentConfig`

```python
class AgentConfig(BaseModel):
    role: AgentRole
    display_name: str = Field(..., min_length=1, max_length=80)
    provider: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    protocol: ProviderProtocol
    # Empty = "not chosen yet" (no model list is compiled in; see 2.3).
    model_name: str = Field("", max_length=128)
    api_key_ref: Optional[str] = None
    base_url: Optional[str] = None
    system_prompt_override: Optional[str] = Field(None, max_length=SYSTEM_PROMPT_OVERRIDE_MAX)
    temperature: float = Field(0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(4096, gt=0, le=200000)
    # Ollama's context window for this role; None = the server's default.
    # Only Ollama consumes it (see 2.1); the other protocols accept and
    # ignore the keyword, because the field is a property of the target.
    num_ctx: Optional[int] = Field(None, gt=0, le=1000000)
    # How long Ollama holds the model resident after a request: a duration
    # ("30m"), bare seconds, "-1" (until the server stops) or "0" (unload
    # next request). None sends nothing and leaves Ollama's own five minutes.
    # Validated by KEEP_ALIVE_RE rather than passed through, because the value
    # goes straight into a request body.
    keep_alive: Optional[str] = Field(None, max_length=32, pattern=KEEP_ALIVE_RE.pattern)
    # The second target this role may be called on (see 2.2.1).
    fallback_provider: Optional[str] = Field(None, pattern=r"^[a-z][a-z0-9_-]*$")
    fallback_model_name: str = ""
    fallback_protocol: Optional[ProviderProtocol] = None
    fallback_base_url: Optional[str] = None
    updated_at: float

class AgentConfigUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    display_name: Optional[str] = None
    provider: Optional[str] = Field(None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    protocol: Optional[ProviderProtocol] = None
    model_name: Optional[str] = None
    api_key: Optional[str] = Field(None, min_length=1, max_length=4096)
    base_url: Optional[str] = None
    system_prompt_override: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # Explicit null clears back to the server default; it is one of the few
    # fields where "absent" and "null" mean different things.
    num_ctx: Optional[int] = None
    keep_alive: Optional[str] = Field(None, max_length=32, pattern=KEEP_ALIVE_RE.pattern)
    # "" (or null) clears the fallback; the pattern allows exactly that.
    fallback_provider: Optional[str] = Field(None, max_length=64, pattern=r"^([a-z][a-z0-9_-]*)?$")
    fallback_model_name: Optional[str] = Field(None, max_length=128)
    fallback_protocol: Optional[ProviderProtocol] = None
    fallback_base_url: Optional[str] = None
```

`set_config` merge rules (applied to the primary target and, identically, to the fallback —
`_normalize_target` is one function called twice, because a second copy of these rules is how a
fallback ends up speaking the wrong wire format):

1. Prompt override: strip; empty → `NULL`; `>32768` → `400` `prompt_too_long`.
2. If `provider` is built-in and `protocol` omitted: fill from catalog. If `base_url` omitted: fill catalog default.
3. If `provider` is **not** built-in: `protocol` and `base_url` REQUIRED after merge.
4. `protocol==ollama` OR catalog `local_only`: `validate_local_base_url`.
5. `api_key` present → keychain `codify` / `codify/agents/{role}`; store `api_key_ref` only (see `04` §7 for the two backends).
6. Built-in `needs_key=False`: key optional.
7. `updated_at = time.time()`.

Nullable fields where an explicit `null` means *clear*: `system_prompt_override`, `base_url`,
`fallback_provider`, `fallback_protocol`, `fallback_base_url`. Everything else treats `null` as "no
change". Clearing `fallback_provider` clears its protocol, endpoint, and model with it — a fallback
that is half removed is a target nothing points at.

### 2.2.1 Fallback target

A role may name a **second** target: `fallback_provider` + `fallback_model_name` (plus
`fallback_protocol` / `fallback_base_url` for a custom slug). It exists so a goal keeps running when
the primary cannot be used at all — no credential stored, the endpoint down, the model retired, or a
reply the contract cannot parse. It is per role, because the honest fallback differs by job: a local
model is fine for the scribe and a bad idea for the fixer.

**Both fields are required for it to exist** (`AgentConfig.has_fallback`). Half a fallback fails at
exactly the moment it is needed, so the engine treats an incomplete pair as unset rather than promising
a rescue it cannot perform. `ProviderFactory` builds it through `AgentRegistryService.build_provider`,
from the same config with provider/protocol/model/endpoint swapped — so credentials, protocol, and
endpoint resolution have one implementation. `api_key_ref` is deliberately **not** carried over: the
role's stored key belongs to its primary provider, and carrying the ref would look up a key for the
fallback provider under the primary's reference. Temperature and max tokens stay the role's own — they
describe the job, not the model answering it.

When it is tried (`04` §4.6) is a closed list, and the reasons it is *not* tried matter as much as the
ones it is: a failure the list does not name is a bug in this engine, and running it on another model
would bury the defect under a retry. It is tried at most **once per agent call** — an outage must not
become a retry loop — and the transcript gets a `provider_fallback` event naming both targets and the
failure, so a reply is never credited to a model that did not produce it.

Removal from the desktop shell sends `fallback_provider: ""`, not `null`: the shell passes the patch
through a typed Rust struct where an explicit `null` and an absent field deserialize identically.

### 2.3 Store

`~/.codify/codify.db` table `agent_configs`. **No `agents.db`.** Column `protocol` TEXT NOT NULL.

**Seeded roles name no model.** Every role is seeded on the local, keyless provider (`ollama`) with
`model_name = ""`. Three reasons:

1. A compiled default is a hardcoded model list — wrong within weeks (retired, renamed), and it cannot
   know what this machine can reach (see `06`).
2. Seeding remote providers meant a fresh install had every role pointed at a provider whose key the
   user had not added, so the first prompt failed with nothing to explain it.
3. Which model to call is discovered live, so the choice belongs to the user (Settings → Agent Roles,
   which offers one model for every role in a single action).

A role with an empty `model_name` is **refused before any provider call**: the goal fails with
`agent_not_configured` naming the role and the screen that fixes it — never with a protocol error from
an endpoint asked to run an empty model id. `PUT /settings/agents/{role}` accepts an empty
`model_name` for the same reason (clearing a choice is legitimate). A role with no primary model but a
configured fallback is the exception: it runs on the fallback, because a rescue that works is a working
role.

SQL: `04` §2 (`fallback_provider`, `fallback_model_name`, `fallback_protocol`, `fallback_base_url`,
`num_ctx`, `keep_alive`; an existing database gains them by `ALTER TABLE`, keeping every configured
role).```python
DEFAULT_AGENTS = [
    _role("laya",       "Laya — System-1 Gate",  0.0,  512, num_ctx= 8192),
    _role("librarian",  "Librarian Agent",     0.1, 8192, num_ctx=32768),
    _role("design",     "Design Agent",        0.4, 8192, num_ctx=32768),
    _role("planner",    "Planner Agent",       0.3, 4096, num_ctx=32768),
    _role("fixer",      "Fixer Agent",         0.1, 8192, num_ctx=32768),
    _role("verifier",   "Verifier Agent",      0.0, 2048, num_ctx=16384),
    _role("critic",     "Critic Agent",        0.2, 4096, num_ctx=16384),
    _role("scribe",     "Scribe Agent",        0.4, 1024, num_ctx= 8192),
]
```

`num_ctx` is **seeded, not left to the server**: Ollama's own default is 4096 and it truncates
without erroring, so a fresh install that never opened Settings ran its whole pipeline on a
partial prompt and nothing said so. The allocation follows *what the role is handed* rather than
how important it is — the four at 32768 (librarian, design, planner, fixer) all receive payloads
built from the librarian's evidence pack, and the fixer's additionally inlines the current
contents of every file its step touches. 32768 is qwen2.5-coder's ceiling, so it is as large as
this is worth asking for. The window costs KV cache whether or not it is needed (~56 KB/token for a
7B GQA model, so ~1.8 GB against 4096's 230 MB), which is why the gate and the scribe stay at
8192 — the gate's state is clipped to 4000 characters by `build_state`, and the scribe answers
with a summary.

Seeding reaches **new installs only**. A migration that backfilled existing rows would silently
raise memory use under a running user who never chose it, and on an install that already exists
this value is the user's to set; an older install keeps Ollama's own window until someone raises
it in Settings, which is the honest state — the field says what is in force. Clearing the field
still returns a role to the server default.

`seed_agents` derives its INSERT column list from `AgentConfig.model_fields` rather than writing
one out. It was hand-written, and it went on naming fifteen columns while the model had seventeen,
so every seeded role came up with `num_ctx` NULL — which is not a null, it is 4096 and a truncated
pipeline. A new field now reaches a fresh install by being declared on the model, which is the one
place it has to be added regardless.

SQL: `04` §2 (includes `protocol`).

### 2.4 `DEFAULT_PROMPTS`

One prompt per slot, differing in the ability granted and the shape returned (`04` §4). Invalid JSON →
`agent_output_invalid`, step `FAILED`.

Prompts mention each other by design (the planner is handed "the librarian's evidence pack", the
scribe is told never to claim a test "the verifier did not run"), so **never route a reply by scanning
the system prompt for a role name** — one prompt naming another role is enough to hand back the wrong
script. The test doubles route on the role whose `AgentConfig` built the provider.

## 3. Adapters (by protocol, not slug)

`ProviderFactory.build(config)` switches on `config.protocol`:

- `anthropic` → Anthropic Messages (`anthropic` SDK or HTTP).
- `openai_compat` → `AsyncOpenAI(api_key=..., base_url=config.base_url)` — covers OpenAI, DeepSeek, Groq, OpenRouter, user slugs.
- `ollama` → POST `{base_url}/api/generate` after SSRF check.

Adding a **harness** later = one catalog row, not a new class. Adding a new **wire format** = one protocol class.

`test_connection`: `max_tokens=8`, prompt `ping`, 15s — enforced with `asyncio.wait_for` around the provider's own `complete` (`TEST_CONNECTION_TIMEOUT_S` in `engine/providers.py`), because a probe that inherits the generation client's 120s+ timeout is the settings screen hanging. It makes exactly one HTTP attempt — generation calls retry transient failures (`04` §4.6.1), the probe reports the first answer. Never echo keys.

## 4. Registry / Orchestrator / API

`AgentRegistryService` only mutator. `list_configs` = 8 rows (gate + 7 role slots), fixed role
order as in `ROLES` — that order is the *display* order, not a schedule; see §1.

`GET /settings/providers` → `{builtins: [...], custom: [slugs on rows not in builtins]}`.

`PUT /settings/agents/{role}` patch as `AgentConfigUpdate`. Extra keys `422`. No raw key in responses.

`POST /goals*` : `extra=forbid`, no agent fields.

Critic rejection: no auto-fix. Retry `POST /goals/{id}/steps/{step_id}/retry`.

## 5. The conductor — a loop, not a slot

`engine/conductor.py` is a model that calls tools. It is deliberately **not** a
ninth `AgentRole`, and that is a structural decision rather than a naming one:

- `ROLES` is iterated by `config_problems`, by `_preflight_roles` and by the
  Settings screen. A ninth entry in `DEFAULT_PROMPTS` would be a ninth role the
  moment any of them read it, and docs/00 §6.1 fixes the count at eight.
- So its prompt lives in `engine/chat_prompts.py` (with the turn's), and its
  configuration lives in `engine_settings` — `conductor_provider`,
  `conductor_model`, `conductor_fallback_provider`, `conductor_fallback_model`,
  `conductor_max_turns`, `conductor_max_moves`, `conductor_drives_execution` —
  not in `agent_configs`. When those are unset a turn borrows the `scribe` row,
  which is the one role whose job is already writing prose for a person.
- Those keys are the conductor's **only** mutator, and it is `GET`/`PUT
  /settings/engine` — not `PUT /settings/agents/{role}`, which has no conductor
  row to patch. They were declared and read for a long time before anything
  could write them, which meant the conductor silently ran on the scribe's row
  on every install; the surface now exists and the Conductor card in Settings →
  Agent Roles is where it is set. A provider saved without a model is stored and
  then ignored, because clearing has to be one call — so the card refuses to
  save a half pair rather than storing a setting that will not take effect.
- Naming a provider **drops** the borrowed row's `base_url` and `api_key_ref`.
  Both name the provider being left behind: `ProviderFactory` prefers
  `config.base_url` over the built-in catalog, and `keychain.get(api_key_ref)`
  returns a key by reference without asking which provider it is for.
  `AgentRegistryService.fallback_config_for` drops the same field for the same
  reason.
- A provider slug that is neither built in nor the borrowed row's own is **not a
  target**. A custom provider's address lives on the row that defines it, and the
  conductor's pair has no `base_url` to carry one, so `_conductor_targets` skips
  it rather than building a provider that posts to an empty address (which read
  as a dead endpoint, wearing the scribe's protocol as a primary and
  `openai_compat` as a fallback). A conductor pointed at one gets no target from
  it and the chain keeps whatever else is reachable — `test_turns.py`,
  `TestTheConductorsTargets`. A custom provider is reachable by naming the row's
  own slug, which keeps its address.
- It is measured through the ordinary `agent_assigned` / `usage` events, so
  stats and the Settings screen need no new case.

### 5.0 The chain: one fallback, tried per call

A role's fallback is a column on its own row. The conductor has no row, so
`ExecutorService._conductor_targets` assembles a chain of up to two targets and
returns *every* target it can build — filtered, not checked in order, so a
primary that cannot serve a tool-calling loop at all does not hide a fallback
that can. The two sources are a rule rather than a merge:

| Conductor | Fallback comes from |
|---|---|
| its own `conductor_provider` + `conductor_model` | `conductor_fallback_provider` + `conductor_fallback_model` |
| still borrowing the `scribe` row | the scribe's own `fallback_provider` / `fallback_model_name` |

A borrowing conductor never reads the `conductor_fallback_*` keys, and an
own-pair conductor never reads the scribe's — one chain, not a merge of two.
An install with both configured has one fallback, exactly as a role has one.

`Conductor._call` retries the **call**, not the run. A conductor is a loop with
no single call to re-issue, and a provider that dies on the fourth call has
already made three moves whose effects are outside the transcript: restarting
would make `write` and `summarize` happen twice. The messages carry every tool
result, so the fallback resumes where the primary stopped. It moves targets only
for a code in `FALLBACK_TRIGGER_CODES` — the same set the roles use, defined in
`engine/providers.py` so both layers share one answer — and exactly once. A
switch publishes `provider_fallback` then `agent_assigned`, because a silent
switch credits the turn's answer to a model that never produced it.

### 5.1 Moves are the pipeline's own doors

| Move | Routed through | Guarantee it inherits |
|---|---|---|
| `read_file` | `LibraryService.read` | `FileSystemService` refuses a path escape |
| `search_code` | `LibraryService.search` | same |
| `git_history` | `GitService.read_only` | `sandbox.validate_argv(mode="read_only")` — the librarian's own validator (`engine/git_readonly.py`'s exact-match table), so the subcommand list and its option rules have one owner (`04` §5) |
| `run_command` | `SandboxService.run_command` | `validate_argv` in `test` mode (docs/00 §6.6) **once the goal is approved** — the same stored-status gate as `write` (`_write_allowed`). Before that the mode is `read_only` (`ls`, `wc`, git history), because the test allowlist admits the repository's own code and a turn has no approval step; a command that would run once approved is refused with that reason, not deferred |
| `read_page` | `WebviewBridge.read_page` | read-only; the page is the user's, the model cannot choose or change the URL, and the text returns quoted as untrusted (docs/03 §1.6) |
| `recall` | `GoalService.recall_events` + `recall.search` | read-only over this workspace's own `events`; the `RECALLABLE` allow-list decides what can be returned and nothing stored can widen it (docs/03 §1.8) |
| `recall_threads` | `GoalService.thread_recall` + `recall.search_threads` | read-only over this workspace's own conversation threads; scoped by workspace in the query, archived threads excluded (docs/03 §1.8) |
| `recon` | `ExecutorService._librarian` | read-only, `MAX_LIBRARY_ROUNDS` |
| `design` | `ExecutorService._design` | no tools at all |
| `plan` | `_plan_steps` | refuses without evidence; writes steps, never files |
| `write` | `ExecutorService._fixer` | docs/00 §6.9 — the only move that writes, gated on approval |
| `verify` | `ExecutorService._verifier` | `validate_argv` in `test` mode — second door, same list |
| `review` | `ExecutorService._critic` | approve or request changes; cannot write |
| `summarize` | `ExecutorService._scribe` | commits only after `review` approved |
| `use_skill` | `engine/skills.py` | none — a skill is data, never a capability |

Judgement is the model's; authority is the engine's. The conductor chooses among
doors that already exist — it cannot open one. **There is still no `write_file`
and no `commit`**: the move that writes is the fixer's own method under the
fixer's own validation, and the move that commits is the scribe's, after the
critic approved. What makes `write` safe is not that it is narrow but that it
reads the goal's stored status before it does anything, so a conductor running
inside a turn — where no plan has been approved — writes nothing and says so.

`tests/test_conductor.py::TestTheToolsAreThePipelinesDoors` is mostly negative
for that reason: it asserts the absence of a file writer by name, that `write`
is refused while the goal is unapproved, and that a refused write changed
nothing.

### 5.1a What a move can say

A move's *text* is part of its door, not decoration on top of it. Four moves take
one and all four deliver it:

| Move | Field | Reaches |
|---|---|---|
| `recon` | `task` | `_librarian(task=)` — in the prompt, beside the goal |
| `design` | `task` | `_design(task=)` → `_design_prompt(task=)` |
| `plan` | `task` | `_plan_steps(task=)` |
| `write` | `instructions` | `_fixer(guidance=)` |

The rule is the one `_fixer` already states for its `guidance`: the ask goes
**beside** what the user asked for, labelled as an addition, never in place of
it. A conductor that could rewrite the goal could send a sub-agent after
something the user never requested, with the user's own words gone from the
prompt. For `design` the placement carries one more step: the ask is inserted
*before* the brand-contract block, so the workspace's own `DESIGN.md` is still
the last thing in the prompt and still outranks it.

Every parameter defaults to empty, and empty is not a special case — the
librarian's prompt with no `task` is byte-identical to the one from before the
conductor existed, so a goal that never involved a conductor is unchanged.

**The two honest gaps.** `verify`, `review` and `summarize` are addressed by
`step_id` alone: the conductor says *which* step and not *what to check*, which
costs something most at `review`, where a rejection is a judgement it can
neither focus nor answer. And the eighth role is out of reach entirely — the
gate runs at the top of a turn, before the loop is constructed, so a conductor
can summon seven of the eight roles and never the gate.

**`delegate` is gone, and its absence is the point.** It ran the whole recipe —
librarian, design, planner — whether or not the request needed them, which made
the sequence a property of the code rather than a decision of the decider. The
seven stage moves replaced it, and the sequence they are used in is a *skill*
(`engine/builtin_skills/ship-a-change.md`) rather than control flow. It is one
of two built-ins in that directory, beside `context-transfer.md`, which is a
recipe for handing a degraded thread to a new one. See docs/09
§10.14.

### 5.2 Why `complete_with_tools` is a separate method

`BaseProvider.complete` is single-shot — system prompt, user prompt, one JSON
reply — and every one of the eight roles depends on that contract. Widening its
signature would put a `tools` parameter on a path whose entire design is "no
tools", and would break every test double in the suite for nothing.

So each provider grows a second method. The neutral shape and the four
translations live in `engine/toolcall.py`, together, because they are the part
with no type checker: a `content` block that should be a `tool_result` is a 400
from someone else's API, not an error in our code. Written once and read once.

`supports_tools` is a property, not a caught exception, because
`Conductor`/`_conduct` asks it *first* — a provider that cannot do tools must
degrade to a plain answer, not fail the question.

### 5.3 What the loop guarantees

- **It terminates.** `conductor_max_turns` bounds model calls. On the last one
  the loop's tool calls are *dropped* and the reply's text is returned with a
  sentence saying it was cut off. An earlier version nudged the model to stop
  and then honoured the next request anyway; `TestTheCap` found it.
- **Spend is bounded twice.** `conductor_max_moves` bounds *stage* moves
  separately from model calls, because they are not the same currency: a model
  call costs seconds, a `write` or a `plan` is a whole sub-agent run that can
  take minutes and touch files. When the move budget is spent the stage moves
  are taken off the menu rather than refused at call time — a refusal a model
  can retry costs a turn every time.
- **The menu narrows with the state.** `write`, `verify`, `review` and
  `summarize` are only offered once `plan` has produced a step for them to act
  on. Eight tools choose better than twelve, and this costs no prompt work.
- **A bad call is recoverable.** An invented tool name, a malformed argument, a
  tool that raised: each returns text naming what *is* available. `ApiError` and
  `CommandNotAllowed` are the exceptions — the engine refused, and the sentence
  says so, because a recoverable-looking refusal teaches the model to retry.
- **Partial arguments do not kill it.** Providers disagree about shape (a JSON
  string, a parsed object, Google's stringly dict); `coerce_arguments` is the
  one place that knows, and it repairs the types Google's round trip costs. A
  half-written object becomes `{}` so the *tool* can refuse with something the
  model can read.
