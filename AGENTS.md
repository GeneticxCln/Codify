# AGENTS.md — workspace rules for coding agents

`CONTRIBUTING.md`, `CLAUDE.md`, and `docs/00`–`23` remain the source of truth for
this repository (where this file and `CONTRIBUTING.md` or `docs/` disagree, they
win). Read `CLAUDE.md` for the architecture tiers, invariants, and `make ci`
gate before editing code.

## Change workflow

Follow these rules on every task:

- **Audit first:** Establish existing behaviour from the code, tests, and runtime
  instead of assuming how the application works.
- **One issue at a time:** Prevent unrelated changes from getting mixed together.
  Keep each branch and pull request focused on one issue, and avoid stacking
  parallel PRs that touch overlapping files before the earlier PR is merged.
- **Targeted regression tests:** Prove the specific bug is fixed. Preserve the
  original failure as a reproducible regression test whenever possible.
- **Integration tests:** Check that neighbouring components and repository
  contracts still work — run the full `make ci` gate (including
  `make typecheck-ui-tests`, `make budgets`, and `make check-tauri`) and
  `make check-history` before pushing.
- **Runtime smoke test:** Verify the actual application end to end (for example
  with `make run-engine-scratch`, `scripts/fake_ollama.py`, or the running UI),
  not just that the code compiles or unit tests pass.
- **Merge last:** Keep unverified changes out of `master`. Push to a dedicated
  branch, verify checks pass, and open a pull request rather than committing
  directly to `master`.
