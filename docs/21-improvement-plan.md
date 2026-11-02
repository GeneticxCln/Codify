# Codify — The improvement plan: goldfish review, project health, cache diagnostics, a size budget, skill contracts

This is a plan, audited against the code before it was written down, in the shape of `docs/11`–`20`: what was claimed,
what the repository showed, what is decided, what is built, and what reopens a row. **Every phase below carries a
status. A phase is "planned" until the commit that builds it flips its status in the same change** (`CLAUDE.md`: the
docs have lied before).

The plan began as a seven-workstream proposal of 21–24 working days. Checked against the repository it became six
workstreams of roughly 13–14 days, because four of its premises did not hold (§1).

## 1. What the audit changed

| The proposal assumed | The repository shows | Consequence |
|---|---|---|
| The step pipeline and the conductor's `review` move build review prompts independently | `ConductorTools.review` calls `ExecutorService._critic`. There is one critic path | Review work is inside `_critic`; there is no second call site to unify |
| Deep review costs "4 calls" | The critic may already ask for up to `MAX_CRITIC_COMMANDS` (2) read-only commands, each a model round | The cap is stated as measured: see §3 |
| The entry chunk is 640 KB | An in-flight change lazy-loads Settings and Stats; the entry chunk is about 448 KB and `ui/dist` about 2.03 MB. `ui/tests/entryChunk.test.ts` already guards the split | A budget baseline is set after that change lands, not from the old number |
| The UI suite needs real DOM smoke tests | About 90 of the 194 UI test files already mount through `ui/tests/dom.ts` / `appHarness.ts` | Dropped |
| Startup time can be budgeted from the existing smoke script | `scripts/tabs_smoke.py` needs a display and times seeding, not startup | Dropped; no startup budget |
| `CLAUDE.md` can be ≤10 KB | The invariants and the gate table are about 8.5 KB; `tests/test_skills.py` also required `CLAUDE.md` to name every built-in | The cap is 12 KB, and that test names `docs/01` and `docs/09` only |
| A new `docs/README.md`, `docs/adr/` and `CONTEXT.md` | Registration of every numbered doc is already enforced in three tables (`docs/00` §5, `CLAUDE.md`, the README). The repository's idiom for a decision is one design doc (`docs/20`) | One doc: this one |
| A `drift-report` skill sequencing `summarize` | `summarize` is the commit move. `wide-audit` already checks claims against code with search and read tools | No new skill |
| `settings_rev` in the usage event | It does not exist. `cache_read_tokens` and `cache_write_tokens` are already recorded and audited (`docs/16` items 6–7) | Fingerprint and length only |
| Every built-in must say "Use when" and "Do not use" | 2 of 33 descriptions say "use when"; none say "do not use". A description is clipped at 200 characters and is in every conductor call | The contract asks for "Use when" only |

## 2. Decisions

1. **Review lenses are parameters, never roles.** `AgentRole` stays eight values (invariant 1). A lens is a prompt
   section passed to the existing `critic` role; its calls are booked under `critic`.
2. **A review packet excludes author context.** The packet holds the step title and request, the acceptance lines, the
   diff and the cited paths. It holds no librarian evidence, no fixer summary and no plan rationale — that is what makes
   the reader a goldfish. The standard critic prompt is unchanged, byte for byte, and a golden test holds it.
3. **The review move takes no `depth` argument.** Depth is the `review_deep` switch (0 or 1, default 0) in
   `engine_settings`, written only by `PUT /settings/engine` (invariant 2). A model or a skill cannot raise spend.
   It is a 0/1 switch like `laya_required` rather than a `standard`/`deep` string, because it has two states.
4. **A health verdict is recorded by a person, not an agent.** An agent may propose verdicts in a plan-only goal; it
   records nothing, and no tool reaches the recording route. (Invariants 2 and 9 in spirit: the person owns the outcome.)
5. **Health records, if built, live in SQLite** (`~/.codify/codify.db`, invariant 7), never in a workspace file, because
   no agent writes a report or checklist file.
6. **Cache diagnostics are content-free.** A hash and a length are recorded, never prompt text; a test asserts no
   diagnostic field can hold content.
7. **Budgets are baselines with a tolerance, not absolute limits.** A baseline is raised only with a written reason in
   the file that holds it.

None of this adds a gate mechanism: a new check joins `make check` or an existing target.

## 3. Phases

**P0 — Clarity. Status: built in this change.** `CLAUDE.md` trimmed from 21.8 KB to under 12 KB with every invariant
quote byte-identical; a byte cap in `tests/test_claude_md_contracts.py`; this document registered in all four places.

**P1 — Goldfish review. Status: engine and UI built (`engine/review.py`, deep path in `_critic`, `review_deep`, the two events, `ReviewReport.tsx`, `DeepReviewCard.tsx`, the audit lane); the review tier is built (`benchmarks/review_tier.py`, `make bench-review`). The measured recall is not yet: it needs a real provider, and the default stays `standard` until it is.**
- `engine/review.py`, pure: `ReviewPacket`, one `build_packet`, `parse_findings` (severity `blocker|major|nit`, non-empty
  scope and recommendation), a deterministic `synthesize` (dedupe by file, line and category; severity max; lens list kept),
  `render_report`. Lenses: *zoom-out* (YAGNI, reuse, premature abstraction), *comprehension* (can a fresh reader follow
  the change from the packet alone?), *readiness* (paths, symbols and tests exist).
- Deep mode is inside `_critic`: two single-shot model lenses (zoom-out, comprehension) with no command rounds; readiness
  is a deterministic engine check with no model call; then the existing critic runs once with the report appended as
  independent findings. **The critic still decides.** A lens that fails is recorded `not_run` and not retried, so a
  failure cannot raise the cost; if all fail, the standard single call runs and the event says so.
- Cost, stated as it is: typically 3 model calls per step, at most 5 (two lenses plus the critic's own three rounds).
- New events `review_lens` and `review_report` join `EventType` and `docs/04`.
- Proof: a review tier of seeded-defect tasks (off-by-one, missing call site, path escape, untested branch), several
  attempts each. Under the canned provider only the wiring is checked and quality checks report `skipped`, never `passed`.
  The default stays `standard`; the measured recall and false-positive counts are reported as they come out.
- Rollback: set `review_deep` to 0. No migration.

**P2 — Project health. Status: planned, deliberately small.** A `builtin_health.json` of checklist items, each mapped to
a capability that exists (`scan_code`, `audit_usage`, `design-review`, `security-review`, `wide-audit`, `prove`), and a
`health-review` skill that returns proposed verdicts with evidence and records nothing. The `health_checks` table, its
routes and a Settings tab are **not** built until the skill has been used enough to show they are wanted.

**P3 — Cache and context diagnostics. Status: planned.** `usage` events gain `prompt_fingerprint` (SHA-256 over system
prompt, sorted tool names, model and protocol) and `prompt_chars`. A pure `cache_diagnostics.explain(previous, current)`
names only reasons the engine can observe (`first_request`, `model_switch`, `system_changed`, `tools_changed`, `idle_ttl`,
`none`). `audit_usage` gains a cache section; Stats shows cache read and write by role and model. Tokens are never
converted to money. A prefix-stability test fails on an injected volatile string.

**P4 — Size budget. Status: planned.** `scripts/check_budgets.py` reads `ui/dist` after `make build-ui` and fails when
the entry chunk or the total grows more than 10% over a recorded baseline. It runs inside `make check`; it needs no
display and no network.

**P5 — Skill contracts. Status: planned.** A test over every built-in: the loader reports no problems, names and
descriptions are unique, a description is at most 200 characters and starts with a "Use when" clause, and a skill that
loops has `## Stop when`. The six `builtin_profiles/*.json` gain `reviewed` and `sources`, validated for built-ins only.

**P6 — Parked, with gates.** A deferred tool tier (only if tools exceed 25% of prompt tokens after P3; reordering tools
is forbidden because it invalidates the prefix), a skill A/B (needs benchmark variants), and a reference corpus (a new
trust boundary; only with the `benchmarks/vendor.py` pattern). Cross-platform and subscription OAuth are out of scope.

## 4. What reopens a row

- P1's default changes only if a measured run, on a real provider and with enough seeded defects to mean something, shows a
  recall gain at an acceptable false-positive rate. That is a new decision recorded here, not a flag flip.
- P2's table and tab are built when someone uses the skill and asks for a record.
- The 10% tolerance in P4 is revisited when a deliberate dependency upgrade moves the baseline; the reason goes in the
  baseline file.
