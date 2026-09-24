# Contributing to Codify

Thanks for helping. The bar for a change here is simple: **`make check` must pass.**

```
make check
```

runs everything in one pass:

| Step | What it does |
|---|---|
| `make lint` | `ruff check engine tests scripts` — rules and target Python pinned in `pyproject.toml` |
| `make typecheck` | `mypy` over the same files — config (3.10 floor, pydantic plugin) pinned in `pyproject.toml` |
| `make test-ui` | the React/TypeScript unit tests in `ui/tests/` (needs Node 22.6+) |
| `make test` | full Python suite (411 tests today — the number moves, so trust the run) |
| `make test-streams` | the concurrency/stream-isolation tests, **by name** (not just via discovery) |
| `make build-ui` | TypeScript check + Vite production build |
| `make check-tauri` | `cargo check` + `cargo fmt --check` on the desktop shell |

CI runs those same targets on every push and pull request, split by toolchain
(`.github/workflows/check.yml`). The Python suite runs on **3.10 and 3.14** — 3.10
because `pyproject.toml` declares it and the desktop shell boots the engine with
whatever `python3` is on `PATH`, 3.14 because that is what the newest interpreter
does with this code. A version-specific break has shipped before: a PEP 701
f-string parsed on 3.14 and was a `SyntaxError` on 3.10, in a module no test could
even import.

## Rules of thumb

**Engine changes need a test that fails without them.** The suite exists to
catch the things this project has actually gotten wrong: hung test commands,
swept git commits, crossed event streams, credentials escaping to the real
`~/.codify`. If your fix can't fail a test, the next refactor will undo it.

**No test may skip itself out of a guarantee.** A test that quietly skips when a
dependency, a binary or a platform is missing is a guarantee nobody is checking:
the wire-level stream-isolation test used to skip whenever `websockets` was
absent, and `websockets` was not declared. If a test needs something, declare it
(and let the suite fail loudly without it).

**Isolation guarantees live in `tests/stream_isolation.py`.** Both concurrency
test layers (service-level in `test_concurrent_streams.py`, over-the-wire in
`test_concurrent_streams_ws.py`) assert through that one shared helper. If you
change a stream guarantee, change it *there* — both layers pick it up and
cannot drift apart.

**Never touch the developer's real state.** Tests run hermetically via
`tests/hermetic.py`; an isolated engine moves *both* the database and the
credentials with `CODIFY_HOME` and disables the OS keychain (see
`engine/home.py`). If your change needs state, put it under `CODIFY_HOME`.

**No hardcoded model lists.** Models come from live provider discovery
(`docs/06-model-discovery.md`). "The provider knows its models" is a design
decision, not an oversight.

**Don't let the gate depend on your machine.** `make lint` reads its rule set and
target Python from `pyproject.toml`, because a linter's defaults are not a
contract: the ruff on one machine checked ~400 rules and reported 189 findings no
one had triaged, while a stock ruff reported none of them. Pin the behaviour, then
widen the ruleset deliberately — with the fixes — rather than inheriting whatever
the installed version does today. That is how the wider sets landed: bugbear,
the bandit-style security rules and pyupgrade are all on, and every security
finding they raise is triaged per site — the `per-file-ignores` block in
`pyproject.toml` names each exemption with the reason it is safe (argv-list
subprocess, engine-owned SQL identifiers, documented best-effort paths). A new
`S` finding is a decision to make, not noise to silence. The same logic pins
`make typecheck`: mypy's config (the 3.10 floor, the pydantic plugin, the file
set) lives in `pyproject.toml`, not in anyone's command line.

**Type annotations are checked, not decorative.** `make typecheck` only sees what
is already annotated, but that is the point: an annotation like
`tuple[int, callable]` names the *builtin* `callable`, not a type, and no runtime
test fires on it — it ships wrong for years until a caller trusts it. If you
write a type, mypy must pass on it; if you silence a finding with `type:
ignore`, the exact error code goes on the line (`# type:
ignore[attr-defined]`), and an ignore that stops being needed is itself an error
(`warn_unused_ignores`).

## Manual verification

For UI-facing changes, a hermetic end-to-end run:

```
make run-engine-scratch   # isolated engine on :7430 (first free in 7430-7440), real ~/.codify untouched
make dev-ui               # Vite dev server
```

`scripts/fake_ollama.py` serves a fake AI provider (role-aware canned replies,
model catalog) so you can drive a full goal without any API keys:

```
python3 scripts/fake_ollama.py   # serves :11435
```

then point the roles' base_url at `http://127.0.0.1:11435` in Settings.

## Docs

Architecture lives in `docs/00`–`06`. If your change alters a documented
contract — orchestration, settings, security, model discovery, the Laya gate —
update the matching doc in the same PR. The docs have lied before; don't add to
it.
