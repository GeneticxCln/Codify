# Codify — Sub-Agent Orchestration Spec

Normative for agent slots, providers, registry, and `/settings/agents`. Persistence: `04`. Security: `03`.

## 1. The 7 fixed pipeline slots + the gate slot

Exactly 7 pipeline slots, plus one pre-flight **gate** slot (`laya`). Users cannot add or remove
**roles**. Provider/model/key/`base_url` per slot are Settings-only.

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

SQL: `04` §2 (`fallback_provider`, `fallback_model_name`, `fallback_protocol`, `fallback_base_url`;
an existing database gains them by `ALTER TABLE`, keeping every configured role).
```python
DEFAULT_AGENTS = [
    _role("laya",       "Laya — System-1 Gate", 0.0,  512),
    _role("librarian",  "Librarian Agent",     0.1, 8192),
    _role("design",     "Design Agent",        0.4, 8192),
    _role("planner",    "Planner Agent",       0.3, 4096),
    _role("fixer",      "Fixer Agent",         0.1, 8192),
    _role("verifier",   "Verifier Agent",      0.0, 2048),
    _role("critic",     "Critic Agent",        0.2, 4096),
    _role("scribe",     "Scribe Agent",        0.4, 1024),
]
```

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

`test_connection`: `max_tokens=8`, prompt `ping`, 15s — enforced with `asyncio.wait_for` around the provider's own `complete` (`TEST_CONNECTION_TIMEOUT_S` in `engine/providers.py`), because a probe that inherits the generation client's 120s+ timeout is the settings screen hanging. Never echo keys.

## 4. Registry / Orchestrator / API

`AgentRegistryService` only mutator. `list_configs` = 8 rows (gate + 7 pipeline slots), fixed role
order as in `ROLES`.

`GET /settings/providers` → `{builtins: [...], custom: [slugs on rows not in builtins]}`.

`PUT /settings/agents/{role}` patch as `AgentConfigUpdate`. Extra keys `422`. No raw key in responses.

`POST /goals*` : `extra=forbid`, no agent fields.

Critic rejection: no auto-fix. Retry `POST /goals/{id}/steps/{step_id}/retry`.
