# Codify — Security, Persistence & Roadmap (v2)

## 1. Security additions for the multi-agent/multi-provider system

### 1.1 API key storage

- Raw keys are NEVER persisted by the Engine in plaintext and NEVER echoed back in any API response — only `api_key_ref` (a keychain handle) is returned.
- Engine: Python `keyring` (macOS Keychain / Windows Credential Manager / Linux Secret Service) under `codify/agents/{role}` and `providers/{slug}`.
- Where no usable keyring exists (headless Linux, no Secret Service, `keyring` not installed), keys go to `~/.codify/secrets.json` at mode `0600` instead — still never SQLite, still never returned by the API. `GET /settings/keys` reports `storage` so the UI states which store is in use rather than promising a keychain it does not have. See `04` §7.
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

The token is created once per state directory and persisted at `<state dir>/boot_token` (`0600`), not rotated per spawn: a client holding it then survives an engine restart, which per-spawn rotation broke for every client that could not re-read the handshake itself. `CODIFY_BOOT_TOKEN` overrides the value for a caller that wants a per-process token. Lifetime, and what a longer-lived credential costs, are in `04` §6.

### 1.4 Retained from v1

- Command allowlist in `SandboxService` — additionally per-agent-scoped: only the Tester Agent's proposed commands ever reach `SandboxService.run_command`, never Coder or Planner raw output.
- Per-command argument policies (not `cmd[0]` only): e.g. `python` only with `-m pytest` / script-path-inside-workspace.
- `FileSystemService` path containment (`root_path` boundary check).
- Engine binds to `127.0.0.1` only.

### 1.5 Embedded browser: deny-by-default webviews

The workspace shell's browser (`src-tauri/src/browser.rs`) is the first surface in Codify that renders untrusted content. Its isolation has four layers, and the first three are asserted by Rust tests in that file rather than assumed:

- **Empty capability set.** `src-tauri/capabilities/browser.json` covers the `browser-*` webview labels with an empty `permissions` list. `browser.rs::the_browser_capability_set_is_empty` parses the committed capability files and fails if any permission reaches a browser label — through `browser.json` itself, a widened `*` pattern in any other capability, or an inline capability added to `tauri.conf.json` (`tauri_conf_inlines_nothing_for_browser_webviews`). The file must also exist and must match a label built by `webview_label`, so the declaration can neither be silently absent nor silently out of sync with the labels in use.
- **Loopback URL guard, on every navigation.** Only `http`/`https`, and never a loopback/unspecified host: `localhost`, `*.localhost` (which covers Windows' `http://tauri.localhost`), all of 127/8, `::1`, `0.0.0.0`, `::`, IPv4-mapped IPv6 spellings, and the integer/hex/octal forms the WHATWG parser canonicalises (`http://2130706433/` is 127.0.0.1). Checked in `open`/`navigate` before anything exists, and again by `WebviewWindowBuilder::on_navigation` for every navigation the page attempts, redirects included. This is §1.2's rule inverted: a local provider *must* point at loopback, a browser page *must not* be able to reach it.
- **An app ACL manifest, so the first layer has something to deny with.** Tauri enforces capabilities against application commands (`codify_*`) — not just plugin and core commands — only when the app defines an ACL manifest. With none defined it lets a **local**-origin invoke of any `codify_*` command through unchecked, where local means relative to the `devUrl` or the app's own assets (`tauri://localhost`, `http://tauri.localhost`). That pass is what `src-tauri/permissions/shell.json` closes: one `allow-` permission per `codify_*` command, collected into a `shell` set referenced by `capabilities/default.json` and by nothing else — and that file's `windows` is `["main"]`. Every app invoke is now resolved against the calling label whatever its origin, so a `browser-*` label resolves to nothing. Asserted by `the_app_acl_manifest_closes_the_local_origin_bypass` (the flag Tauri reads, through Tauri's own resolver), `the_grant_reaches_main_and_no_browser_label` and `the_app_manifest_is_referenced_by_exactly_one_capability`.
- **The origin guard, now defence in depth.** Remote-origin invokes were always rejected unless an explicit `remote` capability resolved them, and this app configures none. With layer 4 in place the loopback refusal is no longer the only thing between a page and `codify_get_engine_info`: navigating onto `http://localhost:5173` or `tauri://localhost` gains a page nothing (§1.3, `00` §6.3), but still seats untrusted content where the app's own scripts run, and the two checks fail independently. Keeping the guard costs nothing and keeps that independence.

The UI half is `docs/09` §7.3, and it does not move this boundary: the pane is an address bar for a webview that is a separate OS window, not a viewport embedded in the main one. That is why the grant can stay keyed on `windows: ["main"]` — an embedded child webview would report the same window label and would resolve the whole `codify_*` grant.

Honest limits: this is a navigation policy, not a network filter — subresource requests to loopback are not intercepted (they cannot reach an engine route without the bearer token, which a `browser-*` label cannot invoke for), and a DNS name that *resolves* to loopback (`127.0.0.1.nip.io`) passes a lexical guard by construction. The capability set is the boundary that does not care what the host resolves to, and the app ACL manifest is what makes that set mean anything. The manifest gates Tauri's `invoke` surface only; the engine subprocess, the PTYs and the webview windows are all created from Rust, where no capability reaches them. See `09` §7.2 for the built surface.

### 1.7 Commit scope

A step commits only the paths it wrote: `git commit -m <msg> -- <paths>`, staged with the same pathspec.
The engine never runs a bare `git add -A`, so a working tree with the user's own staged or half-finished
work is not swept into a commit named after the step, and their index is left as they left it. A step whose
proposal matched the file already commits nothing and says so (`04` §3.0).

## 2. Persistence layer

Single file. **No `agents.db`.**

```
~/.codify/codify.db   # workspaces, goals, plan_steps, events, agent_configs
~/.codify/boot_token  # loopback bearer token, owner-only (0600), created once per state dir
~/.codify/secrets.json  # only when no OS keyring is usable (0600)
```

`CODIFY_HOME` (or `CODIFY_DB` / `CODIFY_SECRETS`) redirects these, and a redirected run also stops using
the OS keychain so it cannot reach the real store — `04` §2.0, `04` §7. The test suite holds itself to
the same rule (`tests/hermetic.py`), and `make run-engine-scratch` does it for a manual smoke test.

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
| SQLite path | `~/.codify/codify.db` — or `CODIFY_DB` / `CODIFY_HOME`, resolved in `engine/home.py` only. |
| Redirected run | Never touches the real keychain (`04` §2.0). |

Argv table, WS/auth handshake, HTTP catalog: `04`.
