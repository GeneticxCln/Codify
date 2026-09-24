# Codify — Architecture Overview (v2)

Normative. If a later doc contradicts this file on product shape (7 roles, Settings-only mutation, loopback Engine), this file wins. Field-level contracts live in `01`–`04`.

## 1. What changed from the original OCDify/Codify draft

The original design was solid for a single-LLM, single-agent tool. The v2 requirement changes the core execution model:

- One orchestrating AI controls **7 roles**: the `laya` pre-flight gate plus 6 pipeline stages
  (`librarian`, `planner`, `fixer`, `verifier`, `critic`, `scribe`).
- Each sub-agent MAY be assigned a different model from a different provider.
- Sub-agents are configurable **only** from the Settings screen. Nowhere else in the app MAY change which model a sub-agent uses.

| Surface | Change |
|---|---|
| Engine execution core | `ExecutorService` no longer calls one `LLMService`. It calls an `AgentOrchestrator` that routes to 7 role-specific agents. |
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
| **Codify Orchestrator** | Inside Engine | Decomposes a goal into phases; dispatches each phase to the slot that has the ability for it |
| **Sub-Agents** | Fixed set of 6 (+ gate) | `librarian`, `planner`, `fixer`, `verifier`, `critic`, `scribe` |
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
                         Planner Agent (goal + evidence)
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
           file_change_summary, agent_assigned, library_evidence, error
```

Critic rejection: Desktop click required to retry the step (`04` §4.3). Settings never appears on this path.

## 5. Document set

| File | Contents |
|---|---|
| `00-codify-architecture-overview.md` | This file |
| `01-subagent-orchestration-spec.md` | Roles, `AgentConfig`, providers, registry, orchestrator, `/settings/agents` |
| `02-settings-app-spec.md` | Settings UI + Tauri; only mutator |
| `03-security-and-roadmap.md` | Keyring, SSRF, boot token, persistence, phases, settled defaults |
| `04-engine-data-and-runtime.md` | Workspace/Goal/PlanStep/Event SQL, HTTP+WS, sandbox argv, boot handshake, errors |

## 6. Invariants (non-negotiable)

1. Exactly seven `AgentRole` values (`laya`, `librarian`, `planner`, `fixer`, `verifier`,
   `critic`, `scribe`). No create/delete of slots.
2. Only `PUT /settings/agents/{role}` mutates agent config. `POST /goals` and `POST /goals/{id}/start` MUST reject unknown fields including `agent_config`.
3. Engine binds `127.0.0.1`. Every HTTP/WS request requires `Authorization: Bearer <boot_token>`.
4. Responses NEVER include raw API keys.
5. `LocalProvider.base_url` MUST pass `validate_local_base_url` before every request.
6. Only verifier-proposed argv reaches `SandboxService.run_command` in `test` mode; the librarian's
   requests use the same validator in `read_only` mode and cannot change the workspace.
7. Single SQLite file: `~/.codify/codify.db`. There is no `agents.db`.
