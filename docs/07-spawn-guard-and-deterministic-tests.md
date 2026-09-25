# Codify — The Spawn Guard and Deterministic Concurrency Tests

Normative for `engine/spawn_guard.py`, every process the engine starts, and the
versioned-action contract the suite asserts against (`tests/versioned.py`).
Roles/pipeline: `01`. Data: `04`. Security: `03`. The Laya gate: `05`.

---

## 1. The contract: no process outlives the engine that started it

Every process the engine starts — a verifier's sandboxed command, a git invocation
(and, through it, any hook the repository runs), the native folder picker — dies
with the engine. Kill the engine (a closed window SIGKILLs it, the watchdog TERMs
it, a crash ends it) and nothing it spawned keeps a session, a port, or a write
handle to a workspace nobody supervises.

Both halves of the contract live in `engine/spawn_guard.py`, and no caller builds
the shape by hand:

* **Launcher side** — `guarded_argv(argv)` returns
  `[sys.executable, SPAWN_GUARD, *argv]` (the guard leads, the command follows);
  `guarded_env(env=None)` returns the environment with
  `CODIFY_SANDBOX_PARENT_PID` set to `os.getpid()` **at call time** — the process
  spawning the guard is the one whose death the guard must notice, and that is not
  the process that imported the module.
* **Guard side** — the guard process execs the command and kills its whole group
  when the engine dies. Two detectors, because neither is enough alone:
  `prctl(PR_SET_PDEATHSIG, SIGTERM)` (Linux-only, instant, armed per-thread —
  the poll covers the rest) and a `getppid()` check every `POLL_INTERVAL_S`
  (0.5 s, everywhere). The pid handed over in the environment closes the fork/exec
  window: a guard that starts after the engine already died compares parents and
  kills the group immediately.

**The caller's half** is `start_new_session=True`: the guard must lead the session
— and so the process group — it kills. A guard that is not a group leader cannot
take the tree down with it.

**What is inherited, unchanged:** the command's stdin/stdout/stderr, working
directory, and environment pass through the guard verbatim; the guard prints
nothing on the success path. Captured output is the command's own.

**The command's outcome is reproduced exactly.** A normal exit is re-reported as
the same code. A death by signal N is re-raised on the guard process — the engine
reads a negative `Popen.returncode` exactly as it did before the guard existed,
so a verifier's verdict text quoting "killed by 9" stays truthful. SIGKILL and
SIGSTOP are the two signals with no disposition to reset; calling `signal.signal`
for either raises, so they are skipped. A parent death is the one outcome the
command does not get to choose: that is a group kill.

**A TERM has two meanings, and `Guard.on_term` tells them apart.** A group timeout
signals the whole group, guard included; escalating to SIGKILL there would cut
short the grace a test runner deliberately gets to shut its workers down, so a
TERM whose sender is still alive forwards nothing — the command answers it. A TERM
that arrives because the engine died is the one case that gets the group kill.

### The four guarded spawn sites

One table in the code (`tests/test_no_unguarded_spawns.py::GUARDED_SPAWN_SITES`)
freezes them, each with its justification and its dynamic test:

| Site | Spawns | Guarded by | Proven by |
|---|---|---|---|
| `engine/sandbox.py` `run_command` | model-asked commands | `guarded_argv` + `guarded_env` + `start_new_session=True`; timeout kills the group, reports exit 124 | `tests/test_sandbox.py`, the live-engine e2e |
| `engine/git.py` `_run_bytes` | every git command the engine runs | same, through the single choke point all callers route through | `tests/test_git.py`, the commit-hook e2e |
| `engine/app.py` `_picker_command` | the GTK folder picker behind `POST /workspaces/browse` | same; env inherited because DISPLAY/WAYLAND put the dialog on screen | `tests/test_sandbox.py`, the picker e2e |
| `engine/spawn_guard.py` `main` | the guard's own `Popen` of the command | is the guard | every one of the above |

The e2e scenarios in `tests/test_sandbox_orphans_e2e.py` run the same proofs
through a real `python3 -m engine` subprocess: a fake Ollama provider drives a
goal, the engine is SIGKILLed mid-run (exactly what closing the window does), and
the process table plus the filesystem must show the tree gone and the heartbeat
frozen. Nothing is patched or imported; every assertion reads the kernel's view,
because a dead engine cannot report anything.

## 2. The static freeze: a spawn cannot appear unannounced

`tests/test_no_unguarded_spawns.py` parses every `engine/**/*.py` (AST only — no
import, no execution) and fails on any process-starting call outside the frozen
table. It resolves all three spellings (`subprocess.run`, `import subprocess as
sp`, `from subprocess import run`), the bypass family that never spells
"subprocess" (`os.system/popen/spawn*/exec*/posix_spawn*/fork*`,
`asyncio.create_subprocess_*`), and rejects star-imports of those modules.
`subprocess.CompletedProcess` is deliberately not flagged — it constructs a result
object and starts nothing; a scanner with false positives stops being read.

The second test keeps the table honest in the other direction: an entry whose
spawn no longer exists is reported stale, so the freeze cannot outlive the code
it names.

The review conversation for a new spawn is written where the change happens:
route it through an existing choke point, or add a justified entry and give it a
dynamic orphan test. There is no third option that passes CI.

## 3. Markers: naming processes the kernel can see

Every dynamic test asserts against `pgrep -f` (which reads
`/proc/<pid>/cmdline`) and heartbeat files — never against the engine's own
reporting. That only works if the processes carry markers nothing else on the box
has, and the vocabulary lives once in `tests/process_probe.py`
(`pids_matching`, `wait_until`, `sigkill_matching`, `file_text`,
`wait_for_text`). Three shapes:

* **A file path in argv** names the process itself: the sandbox sleeper
  (`e2e_orphan_probe_sleeper.py`), the git hook body
  (`codify_git_hook_body.py`), the picker shim directory
  (`codify-live-picker-probe`). "The guard, with this argument" is one ERE:
  `spawn_guard\.py.*<marker>` matches the guard and nothing else, because the
  command's own command line has no `spawn_guard.py` in it.
* **A comment in the source** is the only way to mark a `python3 -c` process, and
  the picker's marker (`PICKER_MARKER = "codify-folder-picker"`) lives there. The
  e2e shim pattern-matches this marker — if it ever changes, the scenario fails
  at its precondition instead of passing silently.
* **A grandchild's argument** names the thing the command spawned:
  `# ORPHAN_PROBE` inside a 300-second sleep.

Two discipline rules the tests enforce on themselves. First, **prove the
precondition before asserting the absence**: every "gone afterwards" is preceded
by a "present now" assertion on the same pattern, or the test would pass for a
process that was never there. Second, **no marker in an ancestor's command
line**: `pgrep -f` matches the test process tree too, so a marker embedded in a
driver's argv reads as a survivor for exactly as long as the test itself lives —
the stand-in source travels by file, not by command line.

Cleanup is registered before the kill (last-registered-first): the SIGKILL sweeps
for every marker run *before* the engine's kill-and-collect, so a failed
assertion cannot leave a stray writer behind to make the next run's probes lie.

## 4. Determinism in the live-engine concurrency tests

`tests/test_concurrent_streams_ws.py` holds one genuine WebSocket per concurrent
goal and asserts what arrives on each wire. Three techniques keep it honest
about races instead of lucky through them.

### 4.1 Pacing: make the mid-run window a certainty

A fake provider that answers instantly finishes a goal in milliseconds, and
"pause mid-run" becomes a lottery: by the time the request lands, the goal is
COMPLETED and `/pause` correctly answers 409. That is a real failure mode — the
test lost it on 3.10, whose slower startup lost a race the 3.14 box won, and
again whenever the box was loaded. The fix is
`_REPLY_PACE_SECONDS = 0.2`: every fake reply is held briefly, so a goal is
RUNNING long enough for the mid-run interaction to be a certainty rather than a
coin flip. The fake is a `ThreadingHTTPServer`, so concurrent goals still run
concurrently — a serialising fake would make the goals take turns, which is not
the interleaving the test claims to isolate.

### 4.2 Race-code retries: aim the retry at the legal race

`/pause`, `/cancel` and `/start` are version-protected
(`expected_version`, `409 version_conflict` on mismatch), and pause/cancel also
refuse the wrong status (`409 illegal_status`). Both refusals can be *lost
races* rather than broken premises: the status check and the version check are
two reads the client cannot make atomic, and the executor legally moves the goal
between them (a step finished; an event bumped the version).

`_versioned()` retries exactly those two codes — re-read the version, re-send,
deadline-bounded — and returns anything else for the caller to judge. A retry
keyed on "it was a 409" would paper over real refusals; one keyed on the codes
means "the goal moved under you, which is legal" and nothing else.

A retry cannot save a broken premise, so the callers do not ask it to:
`_await_step_started()` returns the goal's status, and a goal that reached a
terminal state before the pause/cancel could matter fails the test with that
status named — beta and delta cannot finish in their windows by design, so a
terminal status there is a bug, not a race.

### 4.3 Stderr-on-failure: the cause of a 1011 is server-side

A WebSocket that dies mid-drain surfaces to the client as
`1011 internal error` — the symptom, not the cause. The harness therefore never
divines: the drain helpers catch `websockets.ConnectionClosed` and raise an
assertion naming the goal's wire and pointing at the diagnosis, and the
engine's `finally` block prints the engine's stderr tail **only when the test
has failed** (`sys.exc_info()`), because a pass leaves the pipe unread and a
printed tail on success is noise that trains nobody to read it.

The deeper rule the suite follows: assertions read the process table, the
filesystem, and the wire — the kernel's view. The engine's own reporting is
input for humans reading a failure, never the thing a guarantee is asserted
against.

## 5. What each layer proves

| Layer | Proves | Cannot see |
|---|---|---|
| `test_sandbox.py` / `test_git.py` stand-ins | the guard mechanics: argv shape, pid handover, group kill, outcome reproduction | whether the engine actually reaches the guarded call |
| `test_no_unguarded_spawns.py` | that the set of spawn sites is still the set the dynamic tests cover | semantics — whether a spawn is guarded *correctly* |
| `test_sandbox_orphans_e2e.py` | the whole path: real engine, real pipeline, real SIGKILL, kernel-view assertions | anything the fake provider's replies did not drive |
| `test_concurrent_streams_ws.py` | stream isolation over the wire while goals pause, cancel and re-attach | provider behavior beyond the fake's pacing |
| `tests/versioned.py` + `tests/test_versioned.py` | that a versioned act cannot lose a race silently, and that the helper's idea of "legal" is the engine's own | whether a test's *premise* was right — that is the test's own assertion |

The layers are deliberately redundant. The static scan is cheap enough to run on
every commit and freezes decision sites; the dynamic tests are expensive enough
to be few and aimed, and they prove what the scan cannot.

## 6. Versioned actions: the fetch-then-act a caller can lose

Every mutating goal route takes an `expected_version` and compares it against the
row as it stands **when the request lands** — `GoalService.update_status` and
`update_step` in `engine/services.py`, plus the explicit checks in
`engine/app.py` for `/apply` and `/enable-execution`. That guard is what stops
two clients acting on one view of a goal. It is also what makes every client —
the chat, the CLI, and this suite — a fetch-then-act: read a version, then act on
it. Anything that moves the goal in between is a race the caller can lose, and the
engine reports the loss in exactly two ways, with a third answer that is not a
race at all:

| Answer | Meaning | Correct response |
|---|---|---|
| `409 version_conflict` | the version moved; the intent did not | re-read, re-send |
| `409 illegal_status` | the *status* moved | the act may already be in place, or the goal went somewhere the act cannot follow |
| any other refusal (`plan_only`, `not_dry_run`, `nothing_to_apply`, `goal_in_progress`, `step_not_pending`) | a premise error: the request cannot succeed as written | raise at once, with the body |

### 6.1 The audit

Every read-then-act against a versioned route in `tests/`, classified. *Mover*
means something other than the test can bump the version between the read and the
act.

| Site | Shape | Can lose it | Verdict |
|---|---|---|---|
| `test_concurrent_streams_ws.py` `_cancel_midrun`, `_pause_resume` | mid-run `/pause` and `/cancel` over a real socket with the executor live | **yes** — the executor is the mover by design | hardened: `_versioned` (4.2), and the shared helper for the starts |
| `test_sandbox_orphans_e2e.py` `_start_goal` | a live engine: `GET /goals/{id}`, then `POST /start` with the version just read | no mover once the goal is PENDING | hardened: one `post_versioned(...)` call replacing a hand-rolled poll loop |
| `test_apply_flow.py` (4 acts) | in-process ASGI, real planner and executor, version read from the service | no mover at PENDING or after a terminal status | hardened: `post_versioned_async` |
| `test_api.py` (24 sites) | the version comes from the in-process `GoalService` with no yield point, and `run_planning` is stubbed to a no-op, so the test is the only writer | no | left alone: each site is either a deliberate refusal probe (the test's subject) or a synchronous read-then-act |
| `test_apply_flow.py` (3 sites) | `expected_version: 0`, or a fresh version on a goal with no proposals | n/a | left alone on purpose: they assert `not_dry_run`, `illegal_status` and `nothing_to_apply` |
| `test_executor.py`, `test_concurrent_streams.py`, `test_db_and_services.py` (~25 `update_status` / `retry_step` calls) | direct service calls, no `await` between the read and the write | no — no yield point exists | left alone |
| `test_design_role.py` `_start`, `/apply` (another contributor's in-flight file) | `GET /goals/{id}` then `POST /start` with the read version | no mover, same as the e2e | reported, not edited |

The honest summary: **one site could actually lose a race, and it is the one that
already did** — a `409 illegal_status` on a mid-run `/pause` in the WS test on a
3.10 box, not a version conflict. Everything else is safe by an invariant of the
engine's write order rather than by anything the test does: `run_planning` ends
with its `PENDING` write, and a driver's last write is the terminal status
(`_run_steps_locked` sets `COMPLETED`; `release_driver` only clears an in-memory
set). Those invariants are why the remaining sites are left as they are, and they
are load-bearing — a new background writer on any of those paths would turn all of
them flaky at once, which is the argument for routing new code through the helper
rather than copying the pattern again.

### 6.2 The helper

`tests/versioned.py` is that one place: `post_versioned` (sync, for a real engine
over a socket) and `post_versioned_async` (for the in-process tests), differing
only in their awaits — every decision lives in `_verdict`, so the twins cannot
drift. It reads the goal, decides from `LEGAL_FROM` / `ALREADY_DONE` /
`LEGAL_SOON`, and acts.

Four outcomes, four different failures, which is the whole point: a bare
`assertEqual(200, response.status_code)` cannot tell a lost race from a dead
engine, and the four need four different fixes.

* **sent** — a 2xx, with `attempts` and `races` counted, so an absorbed race is
  visible rather than hidden.
* **moot** (`RaceLost`) — the act is already in place. A `RUNNING` goal is one
  `/start` away from a second step run, so this is named and never retried.
* **broken premise** (`PremiseBroken`) — the goal went terminal, or the route
  refused for a reason no re-read fixes. Raised at once, with the body attached.
* **deadline** (`RaceLost`) — bounded, naming the last code and version. An
  unbounded retry is how a suite hangs instead of reporting.

`diagnose=` runs only on a failure path: it is two more requests, and the e2e
spends them on the goal's own event log and the engine's exit status, which is
what turns "start failed" into "the engine exited with 1 before the process ran".

### 6.3 The tables are the engine's, not this repo's memory of them

`tests/test_versioned.py` re-derives `LEGAL_FROM` from the running FastAPI app: a
fresh goal per (action, status) pair, one real request each, quoting a version no
goal has. Every route checks the status before the version and quotes the version
before it spawns or mutates anything — so a status the route considers legal
answers `version_conflict`, proof it got past the status check, and a status it
refuses answers `illegal_status`. The whole 5×7 matrix is therefore derivable with
no side effects at all, and both drift directions are caught: a table calling a
legal status illegal would report a benign lost race as a broken premise, and one
calling an illegal status legal would quietly refuse a call the engine would have
made while a test asserting that refusal kept passing. `LEGAL_SOON` is checked
against the engine too, and the three tables are checked for overlap, because
`_verdict` reads them in a fixed order.

**What is not proven:** no real-surface test currently reaches the `LEGAL_SOON`
wait. On this machine the goal is already PENDING by the time the harness reads
it, so that branch is covered by a deterministic unit test and nothing else. It
stays because the timing it defends against is exactly the kind that bit on 3.10:
a goal still PLANNING is *refused* by `/start`, not queued behind it.
