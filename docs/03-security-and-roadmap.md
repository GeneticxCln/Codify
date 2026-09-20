# Codify — Security, Persistence & Roadmap (v2)

## 1. Security additions for the multi-agent/multi-provider system

### 1.1 API key storage

- Raw keys are NEVER persisted by the Engine in plaintext and NEVER echoed back in any API response — only `api_key_ref` (a keychain handle) is returned.
- Engine: Python `keyring` (macOS Keychain / Windows Credential Manager / Linux Secret Service) under `codify/agents/{role}`.
- Desktop: key typed into `ApiKeyField`, held in component state, sent once over the loopback HTTP call, dropped immediately after. NEVER written into `localStorage`, Tauri's store plugin, or logs.

### 1.2 Local-provider SSRF guard

`AgentConfig.base_url` (used only when `provider == "local"`) MUST be validated against an allowlist before every request:

```python
def validate_local_base_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("Local provider base_url must point at localhost")
    if parsed.scheme != "http":
        raise ValueError("Local provider must use http (loopback only)")
```

Without this, a malicious or careless `base_url` could turn Coder/Tester/etc. into an SSRF vector against internal network services.

### 1.3 Engine–Desktop auth token

On boot, the Engine generates a random token, writes it to stdout, and requires `Authorization: Bearer <token>` on every request. Desktop reads it from the child process stdout when it spawns the Engine and attaches it to every `BackendClient` call — including `/settings/agents/*`, the most sensitive routes (attacker-controlled local `base_url`, key-reference overwrite).

### 1.4 Retained from v1

- Command allowlist in `SandboxService` — additionally per-agent-scoped: only the Tester Agent's proposed commands ever reach `SandboxService.run_command`, never Coder or Planner raw output.
- Per-command argument policies (not `cmd[0]` only): e.g. `python` only with `-m pytest` / script-path-inside-workspace.
- `FileSystemService` path containment (`root_path` boundary check).
- Engine binds to `127.0.0.1` only.

## 2. Persistence layer

Single file. **No `agents.db`.**

```
~/.codify/codify.db   # workspaces, goals, plan_steps, events, agent_configs
```

SQL DDL, `Goal.version` / `event_seq`, Alembic `0001_init`: `04` §2.

- SQLModel maps Pydantic models in `04` §1.
- `update_goal` is check-and-increment; `409` `version_conflict` on miss.
- `agent_configs` seeded with `DEFAULT_AGENTS`; never deleted at runtime.

## 3. Updated phased roadmap

### Phase 0 — Skeleton (persisted from day one)

- `WorkspaceService`, `GoalService`, `EventBus` backed by SQLite (not in-memory).
- Basic FastAPI endpoints + Engine boot token auth.
- `ExecutorService` with hard-coded steps, no agents yet.
- Desktop: workspace selection, goal creation, log streaming.

### Phase 1 — Sub-agent system (core of this revision)

- `AgentConfig` model + SQLite table + 5 seeded defaults.
- `BaseProvider` + Anthropic/OpenAI adapters (Google/local can follow).
- `AgentRegistryService`, `ProviderFactory`, `AgentOrchestrator`.
- `/settings/agents` API (list, get, update, test-connection).
- Desktop: Settings → Agents (only editor); `agent_assigned` → read-only badges elsewhere.
- `PlannerService` and `ExecutorService` refactored to `orchestrator.run_agent(role, ...)` instead of a single `LLMService`.

### Phase 2 — Git, diffs, Reviewer Agent

- Full `GitService`.
- Reviewer Agent phase between "apply changes" and "finalize step" — MAY send a step back to `FAILED`/`IN_PROGRESS` with change requests. MUST NOT auto-rerun Coder; Desktop click restarts the step.
- Diff view + "Approve changes" / "Commit" flow in Desktop.

### Phase 3 — Local provider + polish

- `LocalProvider` (Ollama) with the SSRF allowlist from §1.2.
- Google provider adapter.
- Per-agent cost/latency stats on Agents settings cards (last response time, last error).
- Filterable logs, per-step detail, dry-run indicator throughout.

## 4. Settled defaults

| Topic | Decision |
|---|---|
| `system_prompt_override` | Free-text, **32768** chars. Blank → `NULL` / `DEFAULT_PROMPTS`. |
| Reviewer → Coder | Human click: `POST /goals/{id}/steps/{step_id}/retry`. No auto-loop. |
| `max_calls_per_goal` | Not on `AgentConfig`. Tester: at most one argv run + one verdict call per step attempt. |
| SQLite path | `~/.codify/codify.db` only. |

Argv table, WS/auth handshake, HTTP catalog: `04`.
