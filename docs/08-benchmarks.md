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

The eleven `repo_scale` tasks that ship are written against that same committed
fixture, so they need no fetch and no third-party code. A task written against a
*vendored* repository (§4) is the other kind: until one has been fetched, the tier
fails with a diagnosis naming the missing repository rather than scoring an empty
run as a perfect one.

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
`--engine-db <path>` (read a specific engine store for a configured run). For a
real model, three more matter:

* `--repeat N` runs every task N times, each in a workspace and store of its own.
  One run of a model is an anecdote; the spread is the measurement.
* `--min-pass-rate PCT` exits 1 when fewer than PCT % of the runs pass. It is how
  a recorded baseline is enforced (§8), and a run that beats it still prints the
  number, so a floor that is too low is visible.
* `--record DIR` traces every model call and keeps each task's store and
  workspace in DIR instead of deleting them, so a model's raw replies can be read
  (`trace_calls.response`) and committed as fixtures
  (`tests/fixtures/model_replies/`). They are copied **as each task finishes**, not
  once at the end: a real-model run takes an hour on a CPU, and one killed
  part-way (a container restart, an OOM) used to keep nothing.

A task that crashes is recorded as `ERRORED` with its cause and the run goes on;
each task prints as it finishes. A failed task carries the error that ended it, and
the summary counts failures by code.

### A task's workspace is a git repository

Each run gets a scratch copy of its fixture, and that copy is `git init`-ed with the fixture committed
(`chore: benchmark fixture`), so it starts as a clean checkout the way a person's does. It used to be a plain
directory, which changes what a model sees: the scribe's commit is skipped (`not_a_repo`), and a verifier that
proposes `git diff` — small models reach for it constantly — is told "not a git repository", which reads as the
change failing. Git's own files are not counted as files the run wrote (`_files` skips `.git`), and the init
goes through `GitService`, so it is a guarded spawn like every other. **Numbers recorded before this change came
from plain directories** and are not comparable with numbers after it.

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

## 8. A real-model baseline, and what it does and does not show

**Read the limits before the numbers.** This is one small model per run, on one CPU-only machine, one run per
task. It measures *those models*, not Codify: a hosted model (or a 30B local one) would do very differently, and
none was available. A single run is an anecdote (§3, `--repeat`), so treat a difference of one task as noise.
The workspaces in these runs were **plain directories, not git repositories** (§3 now says they are), which
changes what a verifier sees; the numbers below are not comparable with later ones.

### Setup, so someone else can rebuild it

| | |
|---|---|
| Server | `llama-cpp-python` 0.3.35, `python -m llama_cpp.server --n_ctx 8192 --n_threads 4`, CPU only |
| Models | `Qwen2.5-1.5B-Instruct` Q4_K_M and `Qwen2.5-Coder-3B-Instruct` Q4_K_M (GGUF) |
| Roles | all eight on the same endpoint, written by `python3 -m benchmarks.seed_endpoint` (a placeholder key: `openai_compat` requires one, a local server ignores it) |
| Run | `python3 -m benchmarks.runner --tier repo_scale --repeat 1 --record DIR --report FILE`, with `CODIFY_HOME`, `CODIFY_DB` and `CODIFY_SECRETS` pointed at the seeded directory |
| Tasks | the 11 `repo_scale` tasks: small edits to a committed fixture, each with a behavioural `python3 -c` check |

### Results

| Model | Passed | Failed in the fixer (`agent_output_invalid`) | Verifier caught a bad change (`tests_failed`) | Critic paused the goal | Completed but wrong |
|---|---|---|---|---|---|
| Qwen2.5-1.5B | **2 of 11** (18%) | 7 | 0 | 0 | 2 |
| Qwen2.5-Coder-3B | **0 of 11** (0%) | 4 | 5 | 2 | 0 |

**1.5B, per task.** Passed: `repo-add-clamp`, `repo-changelog`. Fixer failures: `repo-add-version`,
`repo-default-name`, `repo-readme-usage` (reply not valid JSON — in two of them a `new_text` string with no
closing quote); `repo-add-whisper`, `repo-shout-exclaim` (an `edit` with an empty `old_text`);
`repo-rename-greeting`, `repo-close-the-gap` (`old_text` that does not match the file, or matches more than
once). Completed but failed the quality check: `repo-remove-shout` (the module was no longer importable),
`repo-word-count` (the repository's own tests errored, though the behaviour check passed).

**3B, per task.** None passed. The verifier caught a bad change (`tests_failed`) in `repo-rename-greeting`
(renamed `greet` without updating what uses it), `repo-close-the-gap` (`app.py` no longer importable),
`repo-default-name`, `repo-add-version` (an em dash written into Python source) and `repo-changelog` (the
verifier proposed a `git` write, the sandbox refused it, and the verifier reported the refusal as a failed test).
Fixer failures after the re-ask: `repo-add-whisper` (empty `old_text`), `repo-add-clamp` (a `create` whose path was
the directory `src`), `repo-shout-exclaim` and `repo-remove-shout` (invalid JSON). Paused by the critic:
`repo-readme-usage` (an empty README) and `repo-word-count` (the diff did not add the requested function to the
file). The 3B planner also turned these one-edit tasks into **2 to 4 steps each**, so there were more places to go
wrong than the task needed.

### What the runs show

* **The pipeline held under two weak models.** Every stage the engine owns ran; nothing broken was committed
  (a change that broke the repository was caught by the verifier as `tests_failed`, and the goal failed instead of
  committing it); the sandbox refused the writes the 3B verifier tried in `repo-readme-usage` (`touch README.md`,
  `git add`, `git commit`) and in `repo-changelog` (a `git` write) — only the fixer writes; and the critic paused
  goals whose README was empty or whose diff missed the requested file.
* **The dominant failure is the model's edit, not the engine.** For 1.5B, seven of nine failures are the fixer's
  reply after its one re-ask. Formatting slips are repaired (`docs/04` §4), so what is left is a model that cannot
  hold a JSON document and a file's exact text in mind together. The 3B model writes valid JSON but often edits
  the wrong thing: an unimportable `app.py`, a path that is a directory, an em dash inside Python source.
* **Small models make the verifier flail when there is nothing to run.** For a README or changelog task the 3B
  verifier proposed commands the sandbox refuses instead of giving a verdict directly, and read the refusal as a
  failed test (`repo-changelog`); `repo-readme-usage` spent 446 s and 25 model calls that way. That is the model
  ignoring the "no command needed" path the verifier prompt offers, and worth a prompt change tested against a real
  model before anyone trusts it.

### The conductor, end to end

`scripts/drive_a_turn.py` drove three turns through the real engine (a question about a file, a listing, a change
request) against `Qwen2.5-Coder-3B` behind `llama_cpp.server --chat_format chatml-function-calling`, with
`--provider openai --base-url … --api-key-env …`. **Before the fix in `9100b14` all three turns failed
`internal_error`**: `complete_with_tools() got an unexpected keyword argument 'num_ctx'` — the conductor passed
`num_ctx` and `keep_alive` on every call and every provider but Ollama's rejected them, so it had only ever worked
on Ollama. **After it, all three turns ran without an error, and none executed a tool**: each answer was a bare
tool-call stub (`functions.read_file:`, `functions.recon:`), and the engine reported it honestly — the change
request finished with "the gate read this as 'code_change' and the conductor finished without planning anything: no
file was changed".

**The stubs were the server's, not the model's or the conductor's.** That is what the experiment below shows: the
same model, the same script and the same three kinds of request, against a different server. The mechanism is from
reading the first server's source rather than from isolating it: in `tool_choice=auto`, `llama-cpp-python`'s
`chatml-function-calling` handler appears to constrain a reply meant as prose to a follow-up grammar that only allows
`functions.X:` or the end-of-turn token (`llama_chat_format.py`), which would leave a model that wants to answer in
words nothing to emit but a stub. The same model served by llama.cpp's own `llama-server --jinja` (built from the source
`llama-cpp-python` 0.3.35 vendors, commit `4df29be`, using the model's own chat template) made real calls. Three
turns through the same script, same three kinds of request:

| Turn | Tools the conductor called | Outcome |
|---|---|---|
| *What does greeter.py do?* | `read_file` | answered, correctly, from the file it read |
| *List the files in this workspace.* | `recon` (the librarian) | answered, correctly: one file |
| *Add a comment above greet() explaining what it returns.* | `read_file` | **answered, and did not make the change** |

Totals: 3 answered, **0 failed calls, 0 re-asks, 0 fallbacks**. What this shows and what it does not: the tool loop
works end to end on an OpenAI-compatible endpoint with a small model, and the engine acts on what the model calls.
It does not show that a small model can *do the work*: on the change request the 3B model read the file and then
described the function instead of planning the edit (`steps: 0`, nothing written), so it drives `read_file` and
`recon` and never reaches `plan`. Whether the server returned the calls as structured `tool_calls` or as JSON in the
message that the engine's own text-call fallback (`coerce_tool_reply`) recovered was not distinguished. One model,
one server, three turns, one run each, so no spread.

### What reading the traces found in Codify itself

A baseline is only useful if someone reads the failures. These were fixed as a result (`tests/` names each one):

* a role's own stored key was ignored when deciding whether it could be called, so every role on a custom
  provider was reported "needs a credential" on every goal, **and the Repair button repointed working roles**;
* a string missing its closing quote was reported as a *truncated* reply, pointing at the wrong place — and that
  sentence is what the re-ask shows the model;
* `--record` copied only at the end, so a killed run kept nothing;
* the console summary printed `FAIL` with no reason for a task that completed and did the wrong thing;
* the workspaces were not repositories (see above);
* (from the conductor run, above) every provider but Ollama rejected the keywords the conductor passes, so the
  conductor worked on Ollama only.

### Not measured

Repeats (one run each, so no spread), the conductor beyond those three turns and one small model, any hosted
model, and any hardware but one CPU. `--min-pass-rate` exists to enforce a floor, but a floor is only worth setting
against a baseline recorded with the model *you* use: 18% is a fact about a 1.5B model, not a target.
