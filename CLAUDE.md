# CLAUDE.md — working rules for this repository

**This file is a distillation, not a second source of truth.** Everything below is
sourced from `CONTRIBUTING.md` and `docs/00`–`10`; where this file and either of
those disagree, **they win and this file is the bug**. `tests/test_claude_md_contracts.py`
fails if the invariants quoted here drift from `docs/00` §6, so that is enforced, not
merely intended.

It stays short on purpose. An agent pays this file's length on every session, so depth
is a pointer below rather than a copy of a 24 KB README.

## What this is

Codify is a local-first, multi-agent AI coding assistant and desktop app. Three tiers,
and knowing which one you are in tells you what you may touch:

| Tier | Language | Owns |
|---|---|---|
| Desktop shell | Rust / Tauri v2 | App lifecycle, owns the engine process, boot token |
| UI | React 19 / TypeScript / Vite / Tailwind | Settings screen, goal chat, stats |
| Engine | Python 3.10+ / FastAPI | Orchestration, sandbox, git, providers, SQLite |The engine runs 8 fixed roles. `laya` (pre-flight gate) guards every request; the **conductor** — a loop, not a ninth role — then decides which of the other seven run and in what order, through *moves* (`recon`, `design`, `plan`, `write`, `verify`, `review`, `summarize`) and *skills* it can load. The recipe that used to be compiled in — `librarian` → `design` → `planner` → `fixer` → `verifier` → `critic` → `scribe` — is now the built-in skill `ship-a-change` (`engine/builtin_skills/`), and a workspace can replace it. The directory's other built-in, `context-transfer`, is a recipe for packaging a degraded thread into one pasteable block for a new thread — load it when the thread is long enough that the model is losing track. `fixer` is still the only writer and `verifier` still the only role that runs a command.

## The gate is `make check` / `make ci`

`pytest` or `npm test` alone is **not** the gate, and a green run of either does not
mean the change is shippable.

| Target | What it runs |
|---|---|
| `make setup` / `make doctor` | From a fresh clone: create `.venv` + install everything / check the machine and say how to fix what is missing (read-only). The Makefile prefers `./.venv/bin` automatically, except inside the 3.10 floor leg |
| `make check` | Everything below, on the interpreter you have |
| `make ci` | `make check`, then the same Python legs again on the declared 3.10 minimum |
| `make lint` | `ruff check engine tests scripts` — rules and target Python pinned in `pyproject.toml` |
| `make typecheck` | `mypy` over `engine`, `tests`, `scripts` — config pinned in `pyproject.toml` |
| `make test` | Full Python suite via `unittest` |
| `make test-streams` | Stream-isolation tests **by name**, not just by discovery |
| `make test-ui` | `ui/tests/` through `node --test` (needs Node 22.22.2+, 24.15+ or 26+; checked before it runs) |
| `make typecheck-ui-tests` | `tsc --noEmit` over `ui/src` **and** `ui/tests` — config `ui/tsconfig.test.json` |
| `make build-ui` | TypeScript check (`src` only) + Vite production build |
| `make check-tauri` | `cargo check` + `cargo fmt --check` |
| `make ci-python-floor` | Only the 3.10 leg; never skips — it fails with install instructions |
| `make ci-report` | `make ci`, then publishes its verdict on the commit as a GitHub status (context local/make-ci) via `gh`. Not a second gate: it runs the one gate and reports it, and refuses a dirty tree — Actions does not run here, so this is how a PR shows a result |
| `make check-history` | Every commit in `HISTORY_RANGE` (default `origin/<branch>..HEAD`), not just the tip. Separate from the gate on purpose: `make ci` asks about the tree you are about to share, this asks about the commits on the way to it. Pre-push runs it second, and it declines a one-commit range — that has no middle |

`make ci` is the real gate. `.github/workflows/check.yml` describes the same targets
split by toolchain, but GitHub Actions is unavailable to this repository, so
`make ci` in the Makefile is what actually runs.

`make hooks` installs the versioned hooks in `.githooks/` (`pre-commit` runs
`make lint typecheck`; `pre-push` runs `make ci`). Do not add a second gate mechanism
for the same checks.

## Invariants — non-negotiable

Quoted from `docs/00` §6, which is the owner. Do not weaken one to make a change fit.

1. Exactly eight `AgentRole` values (`laya`, `librarian`, `design`, `planner`, `fixer`, `verifier`, `critic`, `scribe`). No create/delete of slots. *(docs/00 §6.1)*
2. Only `PUT /settings/agents/{role}` mutates agent config. `POST /goals` and `POST /goals/{id}/start` MUST reject unknown fields including `agent_config`. *(docs/00 §6.2)*
3. Engine binds `127.0.0.1`. Every HTTP/WS request requires `Authorization: Bearer <boot_token>`. *(docs/00 §6.3)*
4. Responses NEVER include raw API keys. *(docs/00 §6.4)*
5. `LocalProvider.base_url` MUST pass `validate_local_base_url` before every request. *(docs/00 §6.5)*
6. Every `SandboxService.run_command` call goes through `validate_argv` first. Verifier-proposed argv and the conductor's `run_command` and `verify` moves reach it in `test` mode; the librarian's requests use it in `read_only` mode and cannot change the workspace. The conductor proposes argv through either move, it does not widen the allowlist — see `docs/01` §5. *(docs/00 §6.6)*
7. Single SQLite file: `~/.codify/codify.db`. There is no `agents.db`. *(docs/00 §6.7)*
8. A turn is created only by `POST /conversations/{id}/turns`. `POST /goals` refuses `mode: "chat"`, and `TurnCreate` carries no pipeline flags, so a client chooses neither that a turn exists nor what it becomes — the gate classifies and the conductor disposes. See `docs/09` §10. *(docs/00 §6.8)*
9. Only the fixer writes. The `write` move is the single path from a conductor run to the filesystem, it refuses while the goal is unapproved, and no skill, workspace file or conductor reply can widen that. A skill is instructions, never a capability. See `docs/09` §10.14. *(docs/00 §6.9)*

## Layout

```
engine/         Python: orchestration, providers, sandbox, git, db, trace
  models.py     ROLES + ROLE_JOB + ROLE_TIMING — the one place roles are defined
  default_prompts.py   one system prompt per role
  chat_prompts.py  the turn + conductor prompts — no AgentRole, so not in the file above
  conductor.py    the loop that decides the sequence; its moves are the pipeline's own doors
  skills.py       built-in + workspace recipes (`.codify/skills/`) discovered and loaded as data
  toolcall.py     the neutral tool-calling shape and its four protocol translations
  spawn_guard.py process guard; every spawn routes through it
ui/             React 19 + TS + Vite; ui/tests/ run through node --test
src-tauri/      Tauri v2 Rust shell
tests/          Python suite; stream_isolation.py is the shared isolation helper
scripts/        fake_ollama.py (drive a goal with no API keys), replay_trace.py
                 drive_a_turn.py (drive a turn against a real local model),
                 make_logo.py, check_history.py (does every commit in a range build)
benchmarks/     tiered harness; see benchmarks/manifest.json before trusting a number
docs/           00–10, below
.githooks/      versioned pre-commit / pre-push
```

## Working rules

- **A skill is data, not a capability.** Skill bodies arrive from a cloned repository, so
  they are untrusted: a skill may sequence moves that already exist and nothing more. It
  cannot define a move, widen `validate_argv`, or reach the `write` gate, which reads the
  goal's stored status rather than anything the model was told (invariant 9).
- **Engine changes need a test that fails without them.** The suite exists to catch what
  this project has actually gotten wrong: hung test commands, swept git commits, crossed
  event streams, credentials escaping to a real `~/.codify`.
- **No test may skip itself out of a guarantee.** If a test needs a dependency, declare
  it and let the suite fail loudly without it.
- **Stream guarantees live in `tests/stream_isolation.py` only.** Both concurrency layers
  assert through it so they cannot drift apart.
- **Never touch the developer's real state.** Tests are hermetic via `tests/hermetic.py`;
  state goes under `CODIFY_HOME`, never a real `~/.codify`.
- **No hardcoded model lists.** Models come from live provider discovery. "The provider
  knows its models" is a design decision, not an oversight.
- **Do not let the gate depend on your machine.** Rules, target Python and the mypy
  config live in `pyproject.toml`, not in a command line.
- **A green tip is not a green history.** The UI suite reads source text as contract,
  so a commit that adds a component and a test asserting the shell mounts it is red
  until the commit that mounts it. `make ci` cannot see that; `make check-history` is
  the leg that can, and a split is only worth making if every commit in it builds.
- **Type annotations are checked, not decorative.** mypy must pass on every type you
  write. A `type: ignore` carries its exact error code, and an ignore that stops being
  needed is itself an error. The same is true of TypeScript, in `ui/tests/` as much as
  in `ui/src/`: `node --test` runs the suite with `--experimental-strip-types`, which
  *erases* types rather than checking them, so a test that builds a value an interface
  no longer accepts passes. `make typecheck-ui-tests` is the leg that says so.
- **A new ruff `S` finding is a decision, not noise.** Each `per-file-ignores` entry
  names its exemption with the reason it is safe; widen deliberately, with the fixes.
- **No new process start outside the spawn guard.** `tests/test_no_unguarded_spawns.py`
  freezes every spawn site in `engine/`, `benchmarks/` and `scripts/`; route through an
  existing choke point or justify a new entry in that table.
- **No third-party source is committed here.** `benchmarks/vendor.py` fetches a
  permissively licensed, SHA-pinned snapshot only when a person asks, and writes a
  notice beside it recording upstream, SHA, licence and reason. A benchmark that
  needs a repo it does not have fails and says so; it never scores the absence as
  a pass. See `docs/08`.
- **3.10 is a real deployment target.** The shell boots the engine as `python3 -m engine`,
  so syntax the newest interpreter accepts can still be a `SyntaxError` there.

## Depth, on demand

| Doc | Read it for |
|---|---|
| `docs/00` | Architecture overview, component list, the invariants above |
| `docs/01` | Subagent orchestration spec, roles, `AgentConfig`, providers |
| `docs/02` | Settings UI + Tauri; the only mutator |
| `docs/03` | Security and roadmap: keyring, SSRF, boot token |
| `docs/04` | Workspace/Goal/PlanStep/Event SQL, HTTP+WS, sandbox argv, fallbacks |
| `docs/05` | The Laya System-1 gate and its thresholds |
| `docs/06` | Live model discovery — why there is no catalog |
| `docs/07` | Spawn guard and deterministic tests |
| `docs/08` | Benchmarks: what a number may claim, and the no-third-party-source policy |
| `docs/09` | The workspace shell: conversation model, tab rules, both panes, the browser pane's separate-window decision, and §10 on what a turn is |
| `docs/10` | Agent memory: the Hindsight audit — what was built (`recall`, `recall_threads`, keyword search), what was rejected, and the open `reflect` half |

If your change alters a documented contract, update the matching doc in the same
change. The docs have lied before; don't add to it.
