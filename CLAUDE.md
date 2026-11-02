# CLAUDE.md — working rules for this repository

**This file is a distillation, not a second source of truth.** Everything below is
sourced from `CONTRIBUTING.md` and `docs/00`–`22`; where this file and either of
those disagree, **they win and this file is the bug**. `tests/test_claude_md_contracts.py`
fails if the invariants quoted here drift from `docs/00` §6, and caps this file's size, so
that is enforced.

## What this is

Codify is a local-first, multi-agent AI coding assistant and desktop app. Three tiers;
knowing which one you are in tells you what you may touch:

| Tier | Language | Owns |
|---|---|---|
| Desktop shell | Rust / Tauri v2 | App lifecycle, owns the engine process, boot token |
| UI | React 19 / TypeScript / Vite / Tailwind | Settings screen, goal chat, stats |
| Engine | Python 3.10+ / FastAPI | Orchestration, sandbox, git, providers, SQLite |

The engine runs 8 fixed roles. `laya` (pre-flight gate) guards every request; the
**conductor** — a loop, not a ninth role — decides which of the other seven run and in what
order, through *moves* (`recon`, `design`, `plan`, `write`, `verify`, `review`, `summarize`)
and *skills* it can load. `fixer` is the only writer and `verifier` the only role that runs a
command. The default recipe is the built-in skill `ship-a-change`; every built-in lives in
`engine/builtin_skills/` and is catalogued in `docs/01` and `docs/19`. A workspace can replace
a built-in.

## The gate is `make check` / `make ci`

`pytest` or `npm test` alone is **not** the gate; a green run of either does not mean the change is shippable.

| Target | What it runs |
|---|---|
| `make setup` / `make doctor` | Fresh clone: make `.venv` and install / check the machine and `.venv` against `pyproject.toml` |
| `make check` | Everything below on the interpreter you have, including `bench-smoke` |
| `make ci` | `make check`, then the same Python legs on the declared 3.10 floor (`make ci-python-floor` is that leg alone) |
| `make lint` / `make typecheck` | `ruff` / `mypy`; config pinned in `pyproject.toml` |
| `make test` / `make test-streams` | Python suite via `unittest` / stream-isolation tests **by name** |
| `make bench-smoke` | Hermetic benchmark tier, both drivers: no network, models or spend |
| `make test-ui` / `make typecheck-ui-tests` | `ui/tests/` via `node --test` (Node 22.22.2+, 24.15+ or 26+) / `tsc --noEmit` over `ui/src` **and** `ui/tests` |
| `make build-ui` | TypeScript check (`src` only) + Vite build; `make budgets` then checks its size (`ui/budgets.json`) |
| `make check-tauri` | `cargo check`/`test` (jail tests need `bwrap`, `unshare`, an unprivileged overlay mount; `make doctor` says which), clippy `-D warnings`, `scripts/check_msrv.py`, `cargo fmt --check` |
| `make check-history` | Every commit in `HISTORY_RANGE`, not just the tip; pre-push runs it second |
| `make ci-report`, `make check-ghidra` | `make ci`'s verdict as a GitHub status / the Ghidra fallback on a real install. Neither is in `check` or `ci` |

`make ci` is the real gate; `.github/workflows/check.yml` runs the same targets on a clean machine.
`make hooks` installs `.githooks/` (`pre-commit`: `make lint typecheck`; `pre-push`: `make ci`).
Do not add a second gate mechanism for the same checks.

## Invariants — non-negotiable

Quoted from `docs/00` §6, which is the owner. Do not weaken one to make a change fit.

1. Exactly eight `AgentRole` values (`laya`, `librarian`, `design`, `planner`, `fixer`, `verifier`, `critic`, `scribe`). No create/delete of slots. *(docs/00 §6.1)*
2. Agent configuration is written only by the settings routes, never by a goal or a turn: `PUT /settings/agents/{role}` for one role, `POST /settings/agents/repair` (the one bulk action, through the same `set_config`) and `PUT /settings/engine` (engine-wide settings, including which model the conductor uses). `POST /goals` and `POST /goals/{id}/start` MUST reject unknown fields including `agent_config`. *(docs/00 §6.2)*
3. Engine binds `127.0.0.1`. Every HTTP/WS request requires `Authorization: Bearer <boot_token>`. *(docs/00 §6.3)*
4. Responses NEVER include raw API keys. *(docs/00 §6.4)*
5. `LocalProvider.base_url` MUST pass `validate_local_base_url` before every request. *(docs/00 §6.5)*
6. Every `SandboxService.run_command` call goes through `validate_argv` first. Verifier-proposed argv reaches it in `test` mode, and so do the conductor's `run_command` and `verify` moves, but only for an approved goal (stored status `RUNNING`, not plan-only) — until then the conductor's `run_command` is `read_only`. The librarian's requests use `read_only` mode and cannot change the workspace or run its code. The conductor proposes argv through either move, it does not widen the allowlist — see `docs/01` §5. The engine may run that argv inside a bubblewrap jail: the filesystem read-only except the workspace, the person's home and this run's state directory absent (bar the read-only tool directories the command needs), and no network unless the goal's stored `allow_network` or the engine's `sandbox_network` setting opted in; `sandbox_isolation` says whether the jail is `required`, attempted (`auto`, which reports when it cannot) or `off`, and the jail never widens what `validate_argv` admits. The machine tab is the one other place an assistant's keystrokes run commands, and only inside a jail that a person alone can open: the person's files are never written (the machine edits its own copy of the project, or sees it read-only where a copy cannot be made), no credentials, no network unless the person opened it with one; it is not `SandboxService` and widens nothing here — see `docs/09` §14. *(docs/00 §6.6)*
7. Single SQLite file: `~/.codify/codify.db`. There is no `agents.db`. *(docs/00 §6.7)*
8. A turn is created only by `POST /conversations/{id}/turns`. `POST /goals` refuses `mode: "chat"`, and `TurnCreate` carries no pipeline flags, so a client chooses neither that a turn exists nor what it becomes — the gate classifies and the conductor disposes. See `docs/09` §10. *(docs/00 §6.8)*
9. Only the fixer writes for an agent. The `write` move is the single path from a conductor run to the filesystem, it refuses while the goal is unapproved, and no skill, workspace file or conductor reply can widen that. A person's own Save in the editor is one other door, `PUT /workspaces/{id}/file`, and a person's New skill action is the second, `POST /settings/skills` and `POST /settings/skills/import`: both need the boot token, so no agent, skill or conductor tool can call either, and the creator may only create one new file under `.codify/skills/`, never replace or delete another file. A skill is instructions, never a capability. See `docs/09` §10.14. *(docs/00 §6.9)*
10. An MCP server is a remote data source, never a capability the engine did not define. A server is reached only at an HTTPS endpoint a person added through `/settings/mcp`; there is no stdio transport, no child process, no localhost, loopback, link-local or private-network address, and no redirect off the admitted URL. Its tools never enter the model's tool namespace: every call goes through the engine's `call_mcp`, whose arguments name an admitted server and an admitted tool, and only the settings routes can install, enable or widen either. A person assigns each tool `read` or `act`; `read` tools may be offered on any turn, `act` tools only while the goal's stored status is `RUNNING`, and the engine refuses a call whose authority no longer matches, whatever the model was told. A result is bounded third-party text, labelled as data and never as instructions; it is not recallable and cannot create a plan step, write a file, run a command, change a setting or spend a model call on its own. A server's credential is write-only in the API, lives in the engine's secret store, and never appears in a response or an event; a URL is not a credential store. *(docs/00 §6.10)*

## Layout

```
engine/         Python: orchestration, providers, sandbox, git, db, trace
  models.py     ROLES + ROLE_JOB + ROLE_TIMING — the one place roles are defined
  conductor.py    the loop that decides the sequence; its moves are the pipeline's own doors
  executor.py     ExecutorService, built in layers (executor_core … _conduct); moves in conductor_tools
  skills.py       built-in + workspace recipes (`.codify/skills/`) loaded as data
  spawn_guard.py  every spawn routes through it
  surfaces.py     the window bridge; surface_machine.py holds `read_machine`, `run_in_machine`, `key_in_machine`, `reset_machine`
ui/             React 19 + TS + Vite; ui/tests/ run through node --test
src-tauri/      Tauri v2 Rust shell
tests/          Python suite; stream_isolation.py is the shared isolation helper
scripts/        fake_ollama.py (drive a goal with no API keys)
benchmarks/     tiered harness; see benchmarks/manifest.json before trusting a number
docs/           00–22, below
```

## Working rules

- **A skill is data, not a capability.** Skill bodies arrive from a cloned repository, so
  they are untrusted: a skill may sequence moves that already exist and nothing more. It
  cannot define a move, widen `validate_argv`, or reach the `write` gate, which reads the
  goal's stored status rather than anything the model was told (invariant 9).
- **Engine changes need a test that fails without them**, aimed at what this project has
  actually gotten wrong: hung test commands, swept git commits, crossed event streams,
  credentials escaping to a real `~/.codify`.
- **No test may skip itself out of a guarantee.** Declare the dependency and fail loudly.
- **Stream guarantees live in `tests/stream_isolation.py` only.**
- **Never touch the developer's real state.** Tests are hermetic via `tests/hermetic.py`;
  state goes under `CODIFY_HOME`.
- **No hardcoded model lists.** Models come from live provider discovery.
- **The gate must not depend on your machine.** Rules and config live in `pyproject.toml`.
- **A green tip is not a green history.** The UI suite reads source text as contract, so a
  commit that adds a component before the commit that mounts it is red; `make check-history` sees that.
- **Type annotations are checked.** A `type: ignore` carries its exact error code;
  `node --test` erases types, so `make typecheck-ui-tests` is what checks `ui/tests/`.
- **A new ruff `S` finding is a decision**: each `per-file-ignores` entry names its reason.
- **No new process start outside the spawn guard**; `tests/test_no_unguarded_spawns.py`
  freezes every spawn site.
- **No third-party source is committed here**; `benchmarks/vendor.py` fetches a pinned
  snapshot only when a person asks (`docs/08`).
- **3.10 is a real deployment target** (the shell boots `python3 -m engine`).

## Depth, on demand

| Doc | Read it for |
|---|---|
| `docs/00` | Architecture overview, component list, the invariants above |
| `docs/01` | Roles, `AgentConfig`, providers, the built-in skills |
| `docs/02` | Settings UI + Tauri; the only mutator |
| `docs/03` | Security and roadmap: keyring, SSRF, boot token |
| `docs/04` | SQL, HTTP+WS, sandbox argv, events, fallbacks |
| `docs/05` | The Laya gate and its thresholds |
| `docs/06` | Live model discovery — why there is no catalog |
| `docs/07` | Spawn guard and deterministic tests |
| `docs/08` | Benchmarks: what a number may claim |
| `docs/09` | Workspace shell; §10 what a turn is, §14 the machine |
| `docs/10` | Agent memory: `recall`, observations |
| `docs/11` | Ruflo audit |
| `docs/12` | Scrapling audit: `fetch_page` |
| `docs/13` | Grepify audit: `scan_code` |
| `docs/14` | REA audit: `analyze_binary` |
| `docs/15` | Xerg audit: `audit_usage` |
| `docs/16` | DeepInfer and cmux audit |
| `docs/17` | leanrea audit |
| `docs/18` | Release and updates |
| `docs/19` | deepseek-stack audit: native recipes |
| `docs/20` | MCP admission design: invariant 10 |
| `docs/21` | Improvement plan: review lenses, budgets, skill contracts |
| `docs/22` | Motion and film audit: six native recipes |

If your change alters a documented contract, update the matching doc in the same
change. The docs have lied before; don't add to it.
