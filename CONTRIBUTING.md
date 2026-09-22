# Contributing to Codify

Thanks for helping. The bar for a change here is simple: **`make check` must pass.**

```
make check
```

runs everything in one pass:

| Step | What it does |
|---|---|
| `make test` | full Python suite (216 tests) |
| `make test-streams` | the concurrency/stream-isolation tests, **by name** (not just via discovery) |
| `make build-ui` | TypeScript check + Vite production build |
| `make check-tauri` | `cargo check` on the desktop shell |

## Rules of thumb

**Engine changes need a test that fails without them.** The suite exists to
catch the things this project has actually gotten wrong: hung test commands,
swept git commits, crossed event streams, credentials escaping to the real
`~/.codify`. If your fix can't fail a test, the next refactor will undo it.

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

## Manual verification

For UI-facing changes, a hermetic end-to-end run:

```
make run-engine-scratch   # isolated engine on :7498, real ~/.codify untouched
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
