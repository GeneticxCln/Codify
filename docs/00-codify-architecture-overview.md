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
| All state (`_workspaces`, `_goals`) lives in in-memory Python dicts | Lost on Engine restart | SQLite `~/.codify/codify.db` via SQLModel — `03` §2, `04` §2 |
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
| `10-agent-memory.md` | The agent-memory model: the Hindsight audit, what was built (`recall`, `recall_threads`, keyword search), what was rejected, and the open `reflect` half |

## 6. Invariants (non-negotiable)

1. Exactly eight `AgentRole` values (`laya`, `librarian`, `design`, `planner`, `fixer`, `verifier`,
   `critic`, `scribe`). No create/delete of slots.
2. Only `PUT /settings/agents/{role}` mutates agent config. `POST /goals` and `POST /goals/{id}/start` MUST reject unknown fields including `agent_config`.
3. Engine binds `127.0.0.1`. Every HTTP/WS request requires `Authorization: Bearer <boot_token>`.
4. Responses NEVER include raw API keys.
5. `LocalProvider.base_url` MUST pass `validate_local_base_url` before every request.
6. Every `SandboxService.run_command` call goes through `validate_argv` first. Verifier-proposed
   argv reaches it in `test` mode, and so do the conductor's `run_command` and `verify` moves, but
   only for an approved goal (stored status `RUNNING`, not plan-only) — until then the conductor's
   `run_command` is `read_only`. The librarian's requests use `read_only` mode and cannot change
   the workspace or run its code. The conductor proposes argv through either move, it does not
   widen the allowlist — see `docs/01` §5.
7. Single SQLite file: `~/.codify/codify.db`. There is no `agents.db`.
8. A turn is created only by `POST /conversations/{id}/turns`. `POST /goals` refuses
   `mode: "chat"`, and `TurnCreate` carries no pipeline flags, so a client chooses neither
   that a turn exists nor what it becomes — the gate classifies and the conductor disposes.
   See `docs/09` §10.
9. Only the fixer writes. The `write` move is the single path from a conductor run to the
   filesystem, it refuses while the goal is unapproved, and no skill, workspace file or
   conductor reply can widen that. A skill is instructions, never a capability. See `docs/09` §10.14.
