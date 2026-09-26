---
description: Run the real gate for what changed, and read the failure back against the doc that owns the contract.
argument-hint: "[optional: a path or target name — defaults to routing on the diff]"
allowed-tools: Bash(python3 -m unittest:*), Bash(make:*), Bash(git:*), Bash(ruff:*), Bash(mypy:*), Read, Grep
---

# /gate

`pytest` and `npm test` are not the gate. `make check` is, and `make ci` is the real
one — it adds the declared Python 3.10 minimum, which a single local interpreter
cannot cover. Passing the wrong command is the most common way a broken change looks
finished.

## 1. Route on what changed

```bash
git diff --stat HEAD
git status --short
```

| What changed | Run |
|---|---|
| `engine/`, `tests/`, `scripts/` | `make lint typecheck test` |
| `ui/` | `make test-ui build-ui` |
| `src-tauri/` | `make check-tauri` |
| `Makefile`, `pyproject.toml`, a hook, CI | `make ci` |
| Two or more tiers, or you are unsure | `make ci` |

`make test-streams` is worth naming separately: the stream-isolation tests are run
*by name* in `make check`, not merely by the discovery pattern, so a narrowed
pattern cannot quietly drop them. If the change touches `engine/services.py`,
`engine/app.py` or anything streaming over WebSocket, include it even when the
routing above would not.

A `$ARGUMENTS` path, if given, wins over the diff — the author knows better than a
heuristic.

## 2. Run it

Expect the cheap legs first. `make lint` and `make typecheck` are seconds and catch
the most common failures; running the full suite first wastes a minute on a typo.

A missing tool must never read as a pass. The Makefile falls back to `.venv/bin/mypy`
when mypy is not on `PATH`, and the dev tools are not engine dependencies — so
`pip install -e ".[dev]"` (or `pip install "ruff>=0.6" "mypy>=1.11"`) if a linter
cannot be found. Report it as not checked, never as checked.

## 3. Read the failure back

A wall of output is not a diagnosis. For each failure, find the doc that owns the
contract it broke, then say which one:

| Symptom | Where to look |
|---|---|
| A test about spawns, orphans or liveness | `tests/test_no_unguarded_spawns.py`, `docs/07` |
| A sandbox or allowlist assertion | `engine/sandbox.py`, `docs/04` |
| Stream, WebSocket or event-ordering | `tests/stream_isolation.py`, `docs/04` |
| A role's prompt, timing or abilities | `engine/models.py`, `engine/default_prompts.py`, `docs/01` |
| A provider, keyring or model-discovery failure | `docs/03`, `docs/06` |
| A credential or `~/.codify` escape | `tests/hermetic.py`, `docs/03` |
| A `SyntaxError` only on old Python | The 3.10 floor — `make ci-python-floor` |
| A UI type error or bundle failure | `make build-ui`, `ui/tsconfig.json` |

Then state the cause, the smallest fix, and whether that fix changes a documented
contract — if it does, the matching doc is part of the fix, not a follow-up.

## 4. Report

- Which targets ran, and which were skipped, and why.
- Each failure as: cause → owner doc → smallest fix.
- **"Green" only if every routed target actually ran.** A gate that skipped a leg
  because a tool was missing is not a pass; say so.
