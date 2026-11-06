# Codify — Architecture Overview (v2)

Normative. If a later doc contradicts this file on product shape (8 roles, Settings-only mutation, loopback Engine), this file wins. Field-level contracts live in `01`–`04`.

## 1. What changed from the original OCDify/Codify draft

The original design was solid for a single-LLM, single-agent tool. The v2 requirement changes the core execution model:

- One orchestrating AI controls **8 roles**: the `laya` pre-flight gate plus 7 role slots
  (`librarian`, `design`, `planner`, `fixer`, `verifier`, `critic`, `scribe`). The slot count is
  fixed; the *order* is not — a conductor loop picks which run through **moves**, and the
  familiar librarian → design → planner → fixer → verifier → critic → scribe sequence is the
  built-in `ship-a-change` skill (`engine/builtin_skills/ship-a-change.md`) rather than a
  compiled path. See `01` §1.
- Each sub-agent MAY be assigned a different model from a different provider.
- Sub-agents are configurable **only** from the Settings screen. Nowhere else in the app MAY change which model a sub-agent uses.

| Surface | Change |
|---|---|
| Engine execution core | `ExecutorService` no longer calls one `LLMService`. It calls an `AgentOrchestrator` that routes to 7 role-specific agents, and a **conductor** that decides which of them run. |
| Data model | New `AgentConfig` entity, one per role, persisted. |
| API | New `/settings/agents` namespace, deliberately separate from `/goals` and `/workspaces`. |
| Desktop | New Settings screen is the **only** mutator of agent config. Every other screen only displays which agent/model ran (read-only). |
| Security | Multiple provider API keys, not one. |

## 2. Gaps found in the original plan

| Gap | Risk | Fix |
|---|---|---|
| All state (`_workspaces`, `_goals`) lives in in-memory Python dicts | Lost on Engine restart | SQLite `~/.codify/codify.db` through the standard-library `sqlite3` driver (no ORM) — `03` §2, `04` §2 |
| One monolithic `LLMService` | Cannot bind 5 providers | `AgentOrchestrator` + per-provider adapters — `01` §3–5 |
| No concurrency guard on Goal mutation | Lost updates across asyncio tasks | `Goal.version: int` check-and-increment — `04` §1.2 |
| No API key storage strategy | `.env` does not scale to 5×N | OS keychain; Engine NEVER returns raw keys — `03` §1.1 |
| Engine API has no auth | Any local tab can `POST /goals/{id}/start` | Per-launch Bearer token on stdout — `03` §1.3, `04` §6 |
| `sandbox_service.py` allowlist is `cmd[0]` only | `python -c "..."` bypasses | Per-command argv policies — `04` §5 |

## 3. Refined component list

| Component | Stack | Role |
|---|---|---|
| **Codify Desktop** | Rust + Tauri | UI, Engine HTTP/WS client, exclusive Settings/Agents screen |
| **Codify Engine** | Python + FastAPI | Workspace/goal registry, planning, execution, file/git/sandbox ops, event streaming |
| **Codify Orchestrator** | Inside Engine | A conductor loop that picks moves and dispatches each to the slot that has the ability for it |
| **Sub-Agents** | Fixed set of 7 (+ gate); the order they run in is not fixed | `librarian`, `design`, `planner`, `fixer`, `verifier`, `critic`, `scribe` |
| **Provider Adapters** | Inside Engine | OpenAI, Anthropic, Google, local/Ollama |

## 4. High-level flow (updated)

```
User creates Goal in Desktop
        │
        ▼
Engine: GoalService.create → Goal(status=PLANNING, version=0)
        │
        ▼
Orchestrator.plan(goal) → Librarian Agent (reconnaissance, ≤3 rounds, read-only)
        │                    evidence pack, checked against what it actually read
        ▼
                         Design Agent (lock the direction, no tools)
        │                    design contract, published and read back per step
        ▼
                         Planner Agent (goal + evidence + contract)
        │
        ▼
Goal has PlanStep[]  (goal.status=PENDING)
        │
User clicks Start → POST /goals/{id}/start  (no agent_config field)
        ▼
Orchestrator.run(goal)
   for each step (status PENDING → IN_PROGRESS):
     Phase 2  Fixer     → file proposals (+ the same evidence the planner had)
     apply via FileSystemService (dry_run skips writes)
     Phase 3  Verifier  → command + verdict via SandboxService (test mode)
     Phase 4  Critic    → approve | request-changes
              request-changes → step IN_PROGRESS; goal PAUSED; STOP. No auto-fix.
     Phase 5  Scribe    → summary + commit message (critic approved only)
        │
        ▼
WS events: goal_status, step_status, log, diff, test_result,
           file_change_summary, agent_assigned, library_evidence,
           design_contract, error
```

Critic rejection: Desktop click required to retry the step (`04` §4.3). Settings never appears on this path.

Who runs the loop after Start: on an install with a tool-capable conductor model, **the conductor takes each
open step through these phases, one run per step with its own budget, and pauses the goal with a stated reason
if it cannot finish one** (`04` §1.4, pause codes). The engine adds no second pass behind it. The fixed
sequence above is the driver when there is no such model, when `conductor_drives_execution` is 0, and for
`parallel` goals; benchmarks and replay measure it directly. See `09` §10.14.

**This diagram is the common path, not a guarantee.** It is the order the built-in `ship-a-change`
skill sequences, drawn out because it is what most goals do — not a schedule the engine keeps. The
conductor picks moves, so a goal that only needs a plan never reaches the scribe, and one that
changes a design surface may ask for the design move first. What *is* fixed is the ability each
slot has: only the fixer writes, only the verifier runs a command, only the critic can stop a
step. Those are the invariants this diagram is a picture of (`00` §6).

## 5. Document set

| File | Contents |
|---|---|
| `00-codify-architecture-overview.md` | This file |
| `01-subagent-orchestration-spec.md` | Roles, `AgentConfig`, providers, registry, orchestrator, `/settings/agents` |
| `02-settings-app-spec.md` | Settings UI + Tauri; only mutator |
| `03-security-and-roadmap.md` | Keyring, SSRF, boot token, persistence, phases, settled defaults |
| `04-engine-data-and-runtime.md` | Workspace/Goal/PlanStep/Event SQL, HTTP+WS, sandbox argv, boot handshake, errors, stage measurement, traces |
| `05-laya-system-1-gate.md` | The System-1 gate, its typed questions, and its thresholds |
| `06-model-discovery.md` | Live model discovery — why there is no catalog |
| `07-spawn-guard-and-deterministic-tests.md` | The spawn guard, the guarded choke points, the freeze that keeps them honest |
| `08-benchmarks.md` | The benchmark harness, what a number may claim, and the no-third-party-source policy |
| `09-workspace-shell.md` | Conversations, tabs, terminal, browser — and §10, what a turn is |
| `10-agent-memory.md` | The agent-memory model: the Hindsight audit, what was built (`recall`, `recall_threads`, keyword search, the durable observation store and its fading), and what was rejected |
| `11-ruflo-audit.md` | The Ruflo audit: which of its ideas were borrowed, which were refused and by which invariant, and what was not read |
| `13-grepify-audit.md` | The Grepify audit: `scan_code`, a built-in code-review scanner (curated rule profiles, comment and test skipping, candidates the model triages), its rules, and what of Grepify was and was not taken |
| `12-scrapling-audit.md` | The Scrapling audit: `fetch_page`, the engine's rules for a request it makes to an address a model chose, which of Scrapling's fetchers, spiders, browsers and MCP server were refused and why, and the native parser that later replaced its one useful part |
| `14-rea-audit.md` | The REA audit: what a reverse-engineering MCP server and its Ghidra provider teach this engine — the guarded one-shot Ghidra fallback (a dedicated door, engine-owned limits), two gaps found and closed (no resource limits on a child, a worker that cannot be cancelled), and the native ELF/PE/Mach-O reader that now answers first |
| `15-xerg-audit.md` | The Xerg audit: `audit_usage`, a native read-only audit of wasted model calls in tokens (retry waste and the fix loop, descriptive signals, coverage first), built from what the engine already records, with tool-loop waste reported as not measurable, cache reads and writes measured as a signal and no dollars (`04` §4.7); and what of Xerg's skill was and was not taken |
| `16-deepinfer-and-cmux-audit.md` | The DeepInfer and cmux audit: a serving framework this engine is a client of, and a terminal that hosts agents where this one hosts its own — what is portable (terminal notifications into the existing inbox, bounded replay of the loop's transcript, JSON-schema replies where offered, prompt-prefix reuse) and what is refused and by which invariant (a CLI/socket second door, resume commands that run on restore, project files that name argv, browser credential import), with the plan in three ships |
| `18-release-and-updates.md` | Release and updates: a release is a tag, what a deb and an AppImage hold (the shell and the engine, with its own pinned CPython and hashed lock), the workflow's deliberate decisions (the bundle, its smoke before publishing, the dry run), and what the About tab's update button does and refuses |
| `17-leanrea-audit.md` | The leanrea audit: the routing-table skill that argues against REA's tool surface — what of it is already this engine's design (a closed 31-call menu, skills as lazy data, coverage lines), the defect its trap list found and fixed here (`search_code` now names the directories its walk pruned instead of reporting an absence), the measured fixed cost of one conductor call, and the two small rows built instead of the mechanism |
| `19-deepseek-stack-audit.md` | The deepseek-stack audit: 29 skills for a cheap, literal model, read at the initial release `d4f1ebe` — the 24 native recipes built from them, what each does, and which actions stay with a person (push, merge, comments, screenshots, parallel models); per-role thinking effort remains a setting rather than a skill |
| `20-mcp-admission-design.md` | The MCP admission design: invariant 10 (a remote data source, never a capability), remote-only transport, per-tool Read/Act authority, and the tests that hold it. **Phase 3 is built: admission, probe, per-tool Read/Act authority, `list_mcp_tools` and `call_mcp`; act is gated on the goal's stored `RUNNING` status** |
| `21-improvement-plan.md` | The improvement plan, audited against the code: goldfish review lenses (parameters, not roles), person-recorded project health, content-free cache diagnostics, a size budget and skill contracts — the decisions, the phases, and each phase's status |
| `23-decisions.md` | Standing decisions: the owner's calls and where the repository already decided, the working defaults an agent follows (branches and pull requests, verification, evidence, when to ask and when to act), and the open decisions, each with a recommendation. |
| `22-motion-and-film-audit.md` | The motion and film audit: eight repositories about agents that make motion, video and sound as code, read at their 2026-11-02 heads — the six native recipes built from them (`match-a-reference`, `ui-motion`, `scoped-fix`, `claims-ledger`, `tune-to-a-number`, `film-as-code`), what was refused (capture and QA scripts, hearing, installs) and the open decisions left to a person |

## 6. Invariants (non-negotiable)

1. Exactly eight `AgentRole` values (`laya`, `librarian`, `design`, `planner`, `fixer`, `verifier`,
   `critic`, `scribe`). No create/delete of slots.
2. Agent configuration is written only by the settings routes, never by a goal or a turn: `PUT /settings/agents/{role}` for one role, `POST /settings/agents/repair` (the one bulk action, through the same `set_config`) and `PUT /settings/engine` (engine-wide settings, including which model the conductor uses). `POST /goals` and `POST /goals/{id}/start` MUST reject unknown fields including `agent_config`.
3. Engine binds `127.0.0.1`. Every HTTP/WS request requires `Authorization: Bearer <boot_token>`.
4. Responses NEVER include raw API keys.
5. `LocalProvider.base_url` MUST pass `validate_local_base_url` before every request.
6. Every `SandboxService.run_command` call goes through `validate_argv` first. Verifier-proposed
   argv reaches it in `test` mode, and so do the conductor's `run_command` and `verify` moves, but
   only for an approved goal (stored status `RUNNING`, not plan-only) — until then the conductor's
   `run_command` is `read_only`. The librarian's requests use `read_only` mode and cannot change
   the workspace or run its code. The conductor proposes argv through either move, it does not
   widen the allowlist — see `docs/01` §5. The engine may run that argv inside a bubblewrap jail: the
   filesystem read-only except the workspace, the person's home and this run's state directory absent
   (bar the read-only tool directories the command needs), and
   no network unless the goal's stored `allow_network` or the engine's `sandbox_network` setting opted in;
   `sandbox_isolation` says whether the jail is `required`, attempted (`auto`, which probes bwrap and, when it
   cannot build one, refuses a command that runs the project's own code unless a person has set `sandbox_unjailed_ok`) or `off`, and the jail never widens what `validate_argv` admits. The machine tab is the one other place an assistant's
   keystrokes run commands, and only inside a jail that a person alone can open: the person's
   files are never written (the machine edits its own copy of the project, or sees it read-only
   where a copy cannot be made), no credentials, no network unless the person opened it with
   one; it is not `SandboxService` and widens nothing here — see `docs/09` §14.
7. Single SQLite file: `~/.codify/codify.db`. There is no `agents.db`.
8. A turn is created only by `POST /conversations/{id}/turns`. `POST /goals` refuses
   `mode: "chat"`, and `TurnCreate` carries no pipeline flags, so a client chooses neither
   that a turn exists nor what it becomes — the gate classifies and the conductor disposes.
   See `docs/09` §10.
9. Only the fixer writes for an agent. The `write` move is the single path from a conductor run to the
   filesystem, it refuses while the goal is unapproved, and no skill, workspace file or
   conductor reply can widen that. A person's own Save in the editor is one other door,
   `PUT /workspaces/{id}/file`, and a person's New skill action is the second,
   `POST /settings/skills` and `POST /settings/skills/import`: both need the boot token, so no
   agent, skill or conductor tool can call either, and the creator may only create one new file
   under `.codify/skills/`, never replace or delete another file. A skill is instructions, never a
   capability. See `docs/09` §10.14.
10. An MCP server is a remote data source, never a capability the engine did not define. A server is
   reached only at an HTTPS endpoint a person added through `/settings/mcp`; there is no stdio
   transport, no child process, no localhost, loopback, link-local or private-network address, and no
   redirect off the admitted URL. Its tools never enter the model's tool namespace: every call goes
   through the engine's `call_mcp`, whose arguments name an admitted server and an admitted tool, and
   only the settings routes can install, enable or widen either. A person assigns each tool `read` or
   `act`; `read` tools may be offered on any turn, `act` tools only while the goal's stored status is
   `RUNNING`, and the engine refuses a call whose authority no longer matches, whatever the model was
   told. A result is bounded third-party text, labelled as data and never as instructions; it is not
   recallable and cannot create a plan step, write a file, run a command, change a setting or spend a
   model call on its own. A server's credential is write-only in the API, lives in the engine's secret
   store, and never appears in a response or an event; a URL is not a credential store.

Each of the ten is held by a test at the boundary it names (a route, the sandbox, the running app, the
engine's own source) and not only by a test of a constructor or a table. `tests/test_invariants_at_their_boundary.py`
keeps the ledger of which tests hold which invariant and fails if an invariant is added here without an
entry or a test it names goes missing.

Each was checked by removing its enforcement and watching the named test fail. 41 breaks were tried and 12
passed the suite as it stood: eleven real gaps (an unknown role no longer a 404, a role added to the type alone,
a route that creates a role slot, a goal or a turn copying the command bar's model onto a role, the engine
binding every interface, the keys routes returning a stored key, the provider's own `base_url` check, the
librarian asking in `test` mode, a conductor move calling `fs.apply`) and one that changes nothing at run time
(the `host=` in uvicorn's config, ignored while it is handed a socket). All are caught now. That shows the
breaks someone thought of are noticed, not that no other gap exists. The audit of 2026-09-29, which comments
across the tree refer to, was removed once its findings were fixed or moved into the docs that own them;
`git show 407982a:docs/audit-2026-09-29.md` has it.
