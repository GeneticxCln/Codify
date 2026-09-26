# Codify — Benchmarks

Normative for `benchmarks/` (`manifest.json`, `runner.py`, `provider.py`,
`vendor.py`), the `make bench*` targets, and what a benchmark number is allowed
to claim. Roles/pipeline: `01`. Data and events: `04`. The spawn guard: `07`.

---

## 1. What a benchmark here is allowed to measure

A benchmark answers one question: *does this pipeline still do its job?* Every
other question — is the model smart, is the prompt good, is the task interesting
— is somebody else's instrument. The distinction is not pedantry; it is the whole
design, because the failure mode this project has already hit is a number that
looks better than the system actually is.

So there are two kinds of check, and they are never mixed in a single line:

| Kind | Question | Measured by |
|---|---|---|
| **Harness** | Did the pipeline run the way it claims to? | `goal_completed`, `stages`, `files_written`, `file_exists` |
| **Quality** | Did it do the task correctly? | `test_command`, `file_contains` |

A harness check that fails is a bug in Codify. A quality check that fails is
information about a model. Reporting them in one number would mean that a
provider outage looked like a regression in the orchestrator, or — worse — that a
regression in the orchestrator looked like a bad model.

**Under the canned provider every quality check is reported `skipped`, never
`passed`.** This is enforced in `run_task` and asserted by
`tests/test_benchmark_runner.py::test_a_canned_run_reports_quality_as_skipped_not_passed`.
A canned run has opinions about nothing; the honest report is "4 skipped — a
canned run cannot claim task quality", and the token count that comes with it is
labelled `tokens_are_synthetic` for the same reason.

## 2. Tiers

| Tier | Provider | What it is for | What it costs |
|---|---|---|---|
| `smoke` | canned (`benchmarks/provider.py`) | Proves the harness. Deterministic, offline, ~90 ms. | nothing |
| `repo_scale` | configured (your models) | Cross-file, multi-step refactors with a real test command at the end. | real tokens |

The synthetic fixture under `benchmarks/fixtures/synthetic-repo/` is what makes
`smoke` hermetic: a small Python module with a test suite, committed here, so
the whole harness is exercisable by anyone on a fresh clone with no network and
no API key. That is the same reasoning behind `scripts/fake_ollama.py` — a
pipeline that can only be tested with a provider to hand is a pipeline that is
tested when it is convenient to be broken.

`repo_scale` tasks are written against a vendored repository (see §4). Until one
has been fetched, the tier fails with a diagnosis naming the missing repository
rather than scoring an empty run as a perfect one.

## 3. Running them

```
make bench-smoke     # hermetic; safe anywhere
make bench           # your configured models; spends tokens
python3 -m benchmarks.runner --tier smoke --report /tmp/bench.json
```

Benchmarks are **not** part of `make check` or `make ci`, and the reason is in
the Makefile: a gate that costs money on every push is a gate people learn to
bypass, and then it protects nothing. The smoke tier *is* hermetic and would be
free in the gate; it is left out because a benchmark number in CI stops being
read the first time it is red for reasons unrelated to the change.

Useful flags: `--only <task-id>` (one task), `--report <path>` (JSON), and
`--engine-db <path>` (read a specific engine store for a configured run).

### The configured tier never writes into your history

A configured run uses **your** models but does not run in **your** database. It
reads `agent_configs` out of the engine store (`_read_engine_configs`), copies
them into a scratch database inside a temp directory, and runs the goal there
(`seed_agent_configs`). Keys are never copied — they resolve through the same
`Keychain` the engine reads, so the benchmark never handles a credential. The
store you point at is opened read-only and is asserted unchanged by
`tests/test_benchmark_runner.py::test_seeding_the_scratch_store_leaves_the_engine_store_untouched`.

The pre-flight is a pre-flight: missing roles, a missing database and a file
that is not the engine's store are all diagnosed **before** the first goal, with
exit code 2. Discovering a missing model configuration after a goal has run means
paying for that goal to learn something `SELECT` would have told you for free.

## 4. Third-party repositories: the default is absence

**No third-party source is committed to this repository.** `manifest.json` ships
with `"repos": []`, and `resolve_repo` raises a `BenchmarkError` naming
`benchmarks.vendor` when a task needs something that has not been fetched. A
benchmark whose inputs appear by magic on CI is a benchmark nobody can reproduce.

Fetching is a deliberate, licensed act:

```
python3 -m benchmarks.vendor --repo owner/repo@<sha> \
    --license MIT --subpath src/thing --why "what this task needs it for"
```

Three things are enforced, not promised:

1. **A permissive licence, named by the operator.** The licence is a required
   argument and must be an SPDX id in `PERMISSIVE_LICENSES` (`MIT`,
   `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC`, `0BSD`). It is *not*
   detected from the tarball, because a licence file is not reliably present,
   not reliably named, and a guessed answer in either direction is expensive.
   GPL, AGPL, LGPL, MPL, BUSL and the source-available licences are refused:
   attaching those obligations to Codify's own distribution is a judgement for a
   person, made in the open, not a flag on a command line.
2. **A pin.** `owner/repo@sha`, never a branch. A benchmark that re-measures a
   moving target is not a benchmark. `parse_spec` refuses a spec with no `@`.
3. **A reason.** `--why` is required and is written into the snapshot's
   `NOTICE.md` next to the upstream URL, the SHA, the licence and the date.

Alongside each snapshot the tool writes a `NOTICE.md` stating what the directory
is, where it came from, exactly what was removed, and the one command that
recreates it. `benchmarks/repos/` is git-ignored, so a fetched tree lives on the
machine that fetched it; the manifest entry is the reproducible half.

### What the fetch refuses

* **Anything unsafe in the archive.** Every member is vetted *before* a byte is
  written: absolute paths, `..` components and drive-qualified paths are
  refused, and so are symlinks, devices and other non-regular entries. A member
  that escapes is a reason to refuse the fetch, not one to skip past while
  writing the rest.
* **Files over 1 MB**, and archives with more than 20 000 entries — vendor a
  subtree with `--subpath` instead.
* **Build output, VCS metadata, editor state and `__pycache__`** — pruned by
  `PRUNED_DIRS` / `PRUNED_SUFFIXES`.

`--source <path>` vendors from a local tarball or directory instead of the
network, which is how the tests run and how an air-gapped checkout works.

## 5. The one process the harness starts

A task's `test_command` is a real process that can fork, background a worker and
ignore `SIGTERM`. `benchmarks/runner.py::_run_test_command` therefore reproduces
the sandbox's three facts exactly (`docs/07`): the guard leads via
`guarded_argv`/`guarded_env`, the command leads a session of its own via
`start_new_session=True`, and a timeout kills the **whole group** — SIGTERM,
then SIGKILL — rather than the direct child. A timeout is reported as exit 124
(`timeout(1)`'s code), which is the same contract the sandbox gives the verifier,
so a hang is never mistaken for a red test.

It is deliberately **not** routed through `SandboxService.run_command`. That
allowlist is a security boundary for model-proposed argv (`docs/00` §6.6);
widening it so a reviewed manifest could use it would weaken it for every agent
in the pipeline. Instead `argv[0]` must be a bare basename resolved through
`PATH`, and the argv itself is manifest-owned — a committed, reviewed file.

The spawn is enumerated in `tests/test_no_unguarded_spawns.py`'s freeze, which
now scans `benchmarks/` and `scripts/` as well as `engine/`. A freeze scoped to
one directory is a freeze the next directory walks around; the harness lives
outside `engine/` by accident of layout, not by decision.

## 6. Reading a number

* **Harness rate** is the number to gate on. Below 100%, something in the
  pipeline changed shape.
* **Quality outcomes** are per-task, per-check, and only meaningful under
  `repo_scale`. A change here is a change in a model, a prompt or a role
  config — see `docs/01` before blaming the orchestrator.
* **Tokens** are a *count*, never a cost. This project ships no price table
  (`docs/00` §6.4 is about credentials, but the same discipline applies): a
  price is a claim about a vendor's current rates, and a stale one is worse than
  none. Under the canned provider the count is synthetic and labelled so.
* **Stage timings** come from the same `stage_result` events the Stats screen
  shows, so a slow benchmark and a slow goal are measured the same way
  (`docs/04` §4.7).
* **Wall time** is per task, and excludes model latency under the canned
  provider entirely. It measures the harness, which is the point of the smoke
  tier.

Before quoting any of it, read `benchmarks/manifest.json`'s `_read_this_first`.

## 7. Adding a task

1. Put the repository under `benchmarks/fixtures/<name>/` (committed, and it
   must be yours) or vendor one per §4.
2. Add a task to `manifest.json` with an `id`, a `tier`, a `repo`, a `title`, a
   `description` written the way a user would write it, and its `checks`.
3. `canned_write` is the canned provider's answer for tasks in the `smoke` tier
   only. It is not a fallback: under `repo_scale` the real models do the work,
   and there is no scripted answer to fall back to.
4. `python3 -m benchmarks.runner --tier <tier> --only <id>` and read the check
   that failed, not just the summary line.

`tests/test_benchmark_runner.py` asserts that every task names a known tier, an
existing repository, and a check type the runner can actually run — so a typo in
the manifest is a test failure rather than a silently skipped check.
