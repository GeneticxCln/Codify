# Codify — The REA audit: what a reverse-engineering MCP server, and its Ghidra provider, teach this engine

REA ("Reverse Engineer Anything", [`morluto/rea`](https://github.com/morluto/rea)) is an MIT-licensed TypeScript MCP
server and CLI that lets an agent inspect binaries, JavaScript and Electron apps, firmware and websites through
providers (Ghidra, IDA, Hopper, JADX, Frida and others). The request was an audit of all of it, **with Ghidra looked at
on its own**, and of the parts of this project that would change for the better because of it. The audit's result was a verdict
and a list, and on the owner's decision (section 8) it has since been built: sections 1 to 7 are the audit as written,
with their status marked where it changed, and section 8 is what was built and what was not. The document still says why,
what it took, and which smaller things REA does better than this engine. It follows the shape of `docs/10` to `docs/13`: what was read,
what is taken and refused, what a build would be, what the audit does not claim, and what reopens a row.

## 1. What was read, and what was not

* **Read:** a shallow clone of `morluto/rea` at `2c1dee1` (2026-10-16, package version 6.5.0). Three read-only passes:
  the Ghidra provider (`src/ghidra/`, `bridge/ghidra/*.java`, `docs/ghidra-provider.md`, the Ghidra sections of
  `docs/installation.md`); the cross-cutting design (`README.md`, `AGENTS.md`, `SECURITY.md`, `CONTRIBUTING.md`,
  `docs/mcp-contracts.md`, `docs/error-contract.schema.json`, `docs/testing.md`, `src/domain/`, `src/process/`);
  and this repository's matching parts (`engine/spawn_guard.py`, `sandbox.py`, `library.py`, `scan.py`,
  `conductor.py`, `conductor_tools.py`, `web_fetch.py`, `docs/03`, `docs/07`, `docs/12`, `docs/13`).
* **Checked here by hand:** the two gaps in this engine that the comparison rests on (section 3), by search.
* **Not run at audit time:** REA's tests, its `verify-real-*` scripts, and Ghidra itself. No Ghidra was installed here
  and nothing was launched, so every REA statement below is what its code and documents say, as three readers reported
  it, with their file references. None of it was tested. Section 8 records the later runs against a real Ghidra 12.1.4
  and 11.2.1, which test this engine's tool, not REA.
* **Not read:** the other providers' internals (IDA, Hopper, JADX, Frida, the browser and firmware paths), the
  website, the Windows-specific process code in depth, and the roughly 150 verification scripts beyond their
  descriptions.

## 2. The table

| REA's idea | Here | Why |
|---|---|---|
| Ghidra as a provider: headless import of a **private copy** into an ephemeral project, `-readOnly -deleteProject`, bridge run as the last post-script | **Partly taken and built** as `analyze_binary` (section 8): the private-copy, headless analysis is built; the live bridge is not | The capability is real and it is a new kind of tool: it runs a large foreign program on untrusted bytes. `docs/00` §6 item 6 allows that only through a door the engine builds, so the build added its own guarded spawn site rather than widening `SandboxService` or `validate_argv`. Whether Codify analyses binaries at all was the owner's scope decision, and the answer was yes |
| Ghidra run through the generic command runner | **Reject.** Cannot be allowlisted | `read_only` admits `ls`, `wc` and hardened `git`; `test` mode runs the project's own code and needs an approved goal. Ghidra's `-preScript` / `-postScript` run arbitrary code and its analyzers write a project directory. Putting it in `READ_ONLY_BINARIES` would break "the librarian cannot run its code" (item 6) |
| Bridge protocol: Unix socket 0600, per-session token in a 0600 descriptor deleted after read, constant-time compare, strict handshake that pins version, language, image base and target SHA-256 | **Not taken** (section 4 prefers a one-shot run) | A live session buys interactivity at the price of a socket, a token, a handshake and a lifecycle. The handshake's habit of **refusing to serve unless the analysed bytes hash to the admitted target** is the part worth keeping |
| Everything analysed is a private copy, bound to the admitted file by SHA-256, opened with `O_NONBLOCK` and `wx` | **Taken and built** (section 8) | The analysed bytes cannot be swapped between admission and use, and a FIFO swapped in cannot hang the start. `binary_analysis._snapshot` is that copy-and-hash door; `fs.resolve` still checks containment but is not the thing that copies the target |
| Installation checks as an ordered list, each failure with a typed code and a remediation; `GHIDRA_INSTALL_DIR` absolute and authoritative; a relative `JAVA_HOME` never executed; the JDK range read from the install's own properties; the decompiler's native header checked against the host | **Partly taken and built.** The `GHIDRA_INSTALL_DIR` half is done | `locate_ghidra` refuses a relative install path without running it, treats a configured location that is not an install as that failure rather than looking elsewhere, and gives every failure a code and a remediation. **Not implemented:** the relative-`JAVA_HOME` refusal (a set `JAVA_HOME` is passed to the child unchecked), the JDK range and the decompiler header. The rule is stated in `docs/07` §1 |
| One provider bound per target; "a selected provider is never replaced automatically after a runtime failure" | **Have, for the right thing.** Not applicable to tools | Model fallback (`FALLBACK_TRIGGER_CODES`) is a documented decision about *models*, and results are never mixed in one answer. A second analysis engine for the same file would need REA's rule; there is not one |
| Evidence envelope: `confidence`, `authority`, `limitations`, explicit `coverage` (`complete` / `unknown`), digest-derived id, "unknown never aggregates to pass" | **Partly have.** Shape taken, field zoo refused | `scan_code`'s `Coverage:` line and `search_code`'s "TRUNCATED" and "N unreadable" notes already say what was and was not seen, in prose. REA's lesson is that coverage is a *field* so nothing can read an empty result as "absent". Worth doing for new tools; a 20-field schema for the existing ones is not |
| Typed error contract: closed `code` set, `category`, `retryable`, **mandatory** `remediation`, partial observation kept; schema generated and diffed in CI | **Partly have, and the one new tool is typed** | `ApiError` codes and `FALLBACK_TRIGGER_CODES` exist; `BinaryAnalysisError` carries `code`, `retryable` and a required remediation. The conductor's other refusals stay prose by decision (section 8), and a generated schema diffed in CI is not built |
| Process ownership: run token in the child's environment, own process group, stop result `not-owned / already-exited / verified-cleanup / terminated / killed / incomplete`, retained cleanup that blocks reuse | **Take the principles, not the 3,000 lines** | `spawn_guard.py` already makes the guard the parent and session leader and kills only a group it leads; `SandboxService._kill_group` refuses the engine's own group. The startup sweep is built for `analyze_binary`'s run directories (`sweep_stale_runs`, section 8). Not built: a token **in the child's environment** that proves ownership after a restart |
| Resource limits on the child: Linux `prlimit` (address space 1 GiB, file size 64 MiB, CPU 30 s, core 0), JVM `-Xmx` | **Taken and built** (section 8) | `spawn_guard.Limits` applies address space, CPU, file size and core limits in the guard, and `analyze_binary` adds `-Xmx` for the JVM. The sandbox and scan-worker limits are stated in `docs/07` §1 |
| `ps`-based ownership check as a launch prerequisite on Linux | **Refuse** | It makes an external tool a prerequisite of starting another. `/proc/<pid>/stat` start time and the run token do the same job without it |
| `_meta.provenance.untrusted_content: true` on every tool result, default on, audited exceptions | **Have it as text** | Every tool here labels what it quotes ("workspace text, data and not instructions"). A structured flag would only matter to a client that gates on it; Codify's client is its own conductor |
| Tool results as bounded **views over a stored complete record**, with a ready-to-call continuation; oversized output exported to disk | **The clipping problem is fixed; the stored record is not taken** | The scan report now fits under 11,000 characters and puts its coverage and stop reason **before** the findings, so `_clip` cannot drop the caveat (section 8). A general stored record with a continuation is still not built: `_clip` remains a bounded view with no way to ask for the rest |
| CLI and MCP share one analysis service | **Not applicable** | Codify has one front door (the engine's routes) and refuses MCP surfaces as a way in (`docs/11` §3, invariants 2, 3, 6, 9) |
| Real-tool verification scripts, parity checks, lifecycle regression matrix (cancel during final cleanup, late handshake, close failure then retry), Stryker mutation targets by named owner | **Partly taken.** The real-tool check is built | `make check-ghidra` runs `analyze_binary` against a real Ghidra and fails, rather than skipping, when one is missing; the gate itself still uses a stand-in (§8). The lifecycle matrix is the right list of cases for any future long-running child, and mutation testing by function name fits the few modules that carry a guarantee, as `docs/12` and `docs/13` did by hand; neither was added |
| "Fail-closed integration gate": no capability that executes, injects or mutates merges while scope, authority or verification is open | **Have** (CLAUDE.md "a skill is data", `docs/00` §6) | The audit added no capability; the build that followed added one only after the owner's decision, with its own guarded door and verification (section 8) |
| An MCP server as a way to give the agent these tools | **Reject** | Same reasons as `docs/11` §3 and `docs/12`: it admits tools the engine does not define |

## 3. Two gaps in this engine that the comparison found

Both were checked by search in this repository.

1. **A spawned child has no resource limits.** *(Closed: `spawn_guard.Limits`, `docs/07` §1.)* When this audit was
   written, `grep -rn "setrlimit\|RLIMIT\|preexec_fn\|prlimit" engine/` found
   nothing. A command is bounded by a wall-clock timeout and an output cap, not by memory, CPU time or file size, and
   there is no network isolation either. A test suite that allocates until the machine swaps, or writes a
   file until the disk is full, is stopped only by the timeout. REA caps address space, file size, CPU and core size,
   and clamps its limits so they never exceed those it inherited.
2. **The worker that scans files cannot be cancelled.** *(Closed: `docs/07` §1.)* When this audit was written,
   `library._run_regex_worker` waited with
   `communicate(timeout=...)` and takes no cancel signal; `run_command` polls a per-goal `threading.Event` every 0.2 s
   and kills the group. A regex search or scan is short (2.5 s, 15 s), so the cost is small today. It becomes the
   cost the moment a worker can run for minutes, which an external analyser does.

*(Measured and fixed; see section 8.)* Reported, not yet confirmed by a run when this was written: `conductor._clip` keeps the head of an over-long tool result and drops the tail
(`…[N chars elided…]`), and `scan_code` writes its `Coverage:` and "stopped at its limit" lines after the findings.
Whether a real report reaches 12,000 characters with the lines at the end elided has not been measured here; if it can,
the coverage claim is exactly the sentence a reader needs and it is the one lost.

## 4. Ghidra, on its own

What REA's Ghidra provider does, as read (all of it a statement about REA's code, not a test):

* **Locating it.** `GHIDRA_INSTALL_DIR` must be absolute and is authoritative; otherwise `ghidra` / `ghidraRun` on
  `PATH` is resolved with `realpath` and checked for `application.properties` and `analyzeHeadless`, never executed.
  Version 12.1.4 is the verified one; other 12.1.x are accepted with a limitation attached to every result; other
  lines are rejected. The JDK range comes from the install's own properties. Every check failure has a typed code and
  a remediation.
* **Launching it.** A private owner-only directory; the target copied in with its SHA-256 checked against the
  admitted file; `analyzeHeadless <project> -import <copy> -readOnly -deleteProject` with the bridge as the last
  post-script by absolute path; on POSIX the JVM is started directly (not through Ghidra's shell wrapper) so the
  ownership check can read its environment; `-Xmx` from `GHIDRA_HEADLESS_MAXMEM`, else `2G`; JVM option variables
  blanked; its own process group; a run id in the environment.
* **Talking to it.** The bridge opens its socket only after auto-analysis, because headless runs post-scripts last.
  Newline-delimited JSON, a per-session token (in a 0600 descriptor, deleted after it is read), and a handshake the
  client checks field by field: capability set, language, compiler spec, image base, profile digest and SHA-256.
* **Lifecycle.** A 330 s startup deadline over snapshot, launch, connect and handshake; one shared startup with
  independently cancellable waiters, the last of which awaits cleanup; a stop that returns what it could prove; failed
  cleanup keeps the owner and the directories and is retried by the next start.
* **What it returns.** Every result carries limitations ("empty search does not prove absence"; string search covers
  only Ghidra-defined strings; analysis is complete relative to Ghidra's own body). Inventories are returned whole.
* **What "read-only" means.** The user's file is never opened by Ghidra, and the project is deleted. The analysis
  database *is* modified: seeds and a no-return repair run before serving, and three write operations
  (`annotate_native_function`, `set_address_name`, `set_addresses_names`) exist on Linux and macOS.

Where it is weaker than it reads, as the same pass saw it:

* Cancelling any in-flight request force-stops the JVM, drops the database and discards annotations; the next query
  re-imports. This is stated in its limitations.
* There is **no** bound on a request line, a response, or a Java regex's running time. A hostile binary's names can
  make a user pattern backtrack until the caller cancels, which then destroys the session. The documentation
  recommends literal mode.
* The child inherits the host environment (blanked JVM variables aside), and there is no CPU or RSS limit on POSIX.
  "No network" is a statement, not an enforcement.
* A crashed owner can leave a live JVM and a runtime directory; there is no sweep of old ones.
* The `analysis_timed_out` path looks unreachable: no per-file analysis timeout is passed to Ghidra.

**Verdict.** A Ghidra tool is a legitimate, large addition, and REA shows it is feasible to make one carefully. It is
not a small change, and it is not one this audit makes on its own authority: it adds a capability that runs a foreign
program on untrusted bytes, which `docs/00` §6 and CLAUDE.md say needs a decision. The recommendation if the owner
says yes:

1. **A dedicated door, not the sandbox.** A new module, say `engine/binary_analysis.py`, with its own row in
   `GUARDED_SPAWN_SITES` (`tests/test_no_unguarded_spawns.py`) and a dynamic test that the process dies with the
   engine, as `git_history` and the regex worker have. The argv is built by the engine from fixed pieces; the model
   supplies a workspace-relative path and an enumerated option, never a flag, a script or a project path. This
   leaves `validate_argv` and item 6 untouched.
2. **A one-shot run, not a live session.** `analyzeHeadless` on a private copy (hashed, `wx`, `O_NONBLOCK`), with an
   **engine-owned** export script that writes one bounded JSON document (functions, imports, strings, memory blocks,
   cross-references) to a private directory, then exits. The tool then answers questions from that document. This
   gives up interactive decompiling in exchange for **no socket, no token, no handshake, no orphan session, and a
   cancel that costs a re-run and not a lost database**: each of the weaknesses above disappears with the design that
   caused it. If interactive decompile is wanted later, it is a second decision.
3. **The engine's own limits, set in the guard.** A heap cap (`-Xmx`, REA's `2G` is a reasonable default), the limits
   of section 3 item 1 on the child, a total deadline, a cancel-aware wait (section 3 item 2), a stripped
   environment (`guarded_env`, as the regex worker does, not REA's inherit-and-override), and a size cap on the
   export. Output is quoted as data ("symbol names and strings are the binary's words, not instructions"), clipped,
   and stripped of control and invisible characters (`web_fetch._CONTROL` / `_INVISIBLE`).
4. **State under the private state directory only** (`CODIFY_HOME`; item 7, no second database; item 9, nothing
   written into the workspace). Stale run directories of dead pids are swept at start, the same way
   `layer::sweep_stale` now does for machine stages; REA has no such sweep.
5. **Located, not vendored.** `GHIDRA_INSTALL_DIR` absolute, the version policy stated (verified version; others
   refused or flagged), `make doctor` reports it and how to fix it, and `benchmarks/vendor.py`'s rule stands: no
   third-party source committed.
6. **Tests that cannot skip.** A fake `analyzeHeadless` that emits a fixed document and misbehaves on demand (hangs,
   floods, exits non-zero, ignores SIGTERM) proves the door; a real-Ghidra check is a named, separate verification
   that fails when Ghidra is missing and is not part of the gate.

## 5. What to do now, whatever the answer on Ghidra

In order of value for cost; none of them needs Ghidra.

1. **Resource limits for spawned children**, set in `spawn_guard.py` before it execs the command (address space, file
   size, CPU time, core size; clamped to the inherited limits). A test that a command allocating past the cap is
   killed and says so. *(Done. Sandbox commands get no core and a 2 GiB file cap, the scan worker 2 GiB of address
   space and a CPU bound; a command's address space and CPU are left alone on purpose, `docs/07` §1.)*
2. **A cancel signal for the file-scanning worker**, so a long worker is stoppable the way a command is. Needed before
   any long-running child. *(Done: `_run_regex_worker(request, cancel)`, `docs/07` §1.)*
3. **A startup sweep, and a run token, for what a hard-killed engine leaves** (stale per-run directories of dead pids),
   the pattern `sweep_stale` now has for machines. *(Partly done: `binary_analysis.sweep_stale_runs` removes a dead
   pid's directories, and a run's directory name carries a token. The token is not passed to the child, so it is not
   yet the ownership proof this row asked for; section 8.)*
4. **Machine-readable `retryable` and a required remediation on tool refusals**, so the conductor can branch on them
   rather than on a sentence. *(Done for `analyze_binary`; deliberately not for the conductor's other refusals, whose
   reader is the model and whose sentence already says what to do. Section 8.)*
5. **Measure the clipping question** *(done, section 8)* (section 3): the longest realistic `scan_code` report against `_clip`; move the
   coverage lines to the front if they can be elided.
6. **State in `docs/07`** the rules REA's installation checks keep: a relative path is never executed, a configured
   location is authoritative even when invalid, and a missing external tool is a named failure. *(Done: `docs/07` §1,
   "An external tool is located, never assumed"; `locate_ghidra` is the implementation.)*

## 6. What this does not claim

* That REA works as described: three readers reported its code and documents; nothing of it was run.
* That a Ghidra tool would be safe: Ghidra parses hostile bytes in a large Java program, and REA says plainly "this is
  not a sandbox". The recommendation reduces the surface (no model-supplied flag or script, no live socket, no
  inherited environment) and bounds it (heap, CPU, file size, deadline, output); it does not isolate it from the
  host. Network and filesystem isolation would need the same kind of jail as `docs/09` §14.
* That REA's weaknesses are bugs in the sense of a defect report: each is a documented or reasoned design choice, noted
  because a port should not copy it.
* A comparison of quality. This is about what applies here.

## 7. If a rejected or deferred row is reopened

* **Ghidra, or any binary-analysis tool.** This row was reopened: the owner said yes and it is built (section 8).
  A second analysis engine, or a change to what `analyze_binary` accepts, starts there: `docs/00` §6 (items 6, 7, 9),
  the section 4 design, the row in `docs/07` and the freeze; a setting only via `PUT /settings/engine` if one is
  needed (item 2).
* **Interactive decompilation / a live session.** Adds everything section 4 item 2 removed: a socket, a token, a
  handshake and a session that dies with a cancel. Its own decision, and its own lifecycle test matrix.
* **The structured evidence envelope.** Reopens `docs/04`'s tool-result shapes and every tool that formats text today;
  start with the coverage field on the one tool that needs it.
* **An MCP front door.** `docs/11` §3.

## 8. What was built, and what was not

The owner's answer to section 4's "if the owner says yes" was yes, so the recommendation was built as written:

| Item | Where | Status |
|---|---|---|
| Resource limits on spawned children | `spawn_guard.Limits`, `docs/07` §1 | Built and tested against real guarded children |
| A cancel signal for the scan worker | `library._run_regex_worker(request, cancel)`, `docs/07` §1 | Built and tested, and through the conductor's own tools |
| Verification against a real Ghidra (section 4 item 6) | `scripts/check_ghidra.py`, `make check-ghidra` | Built and **run** against Ghidra 12.1.4, including the cancel; not part of the gate, and it fails rather than skips without Ghidra |
| A Ghidra tool, `analyze_binary` | `engine/binary_analysis.py`, `engine/ghidra/CodifyExport.java`, `docs/04` §3.0.7 | Built: its own guarded spawn site, a one-shot `analyzeHeadless` on a hashed private copy, an engine-owned export script, the child limits, a stripped environment, a total deadline, a cancel-aware wait, a startup sweep of dead pids' run directories, a cache keyed by the file's bytes, output quoted as data. Tested against a stand-in `analyzeHeadless` (`tests/fake_ghidra.py`) |
| A startup sweep, and a run token in the directory name (item 3) | `binary_analysis.sweep_stale_runs` | Built for the one place that leaves directories behind today. The machine tab already had its own (`sweep_stale`). The token names the run; it is not in the child's environment, so the ownership proof remains item 3's open half |
| Machine-readable `retryable` and a remediation on refusals (item 4) | `BinaryAnalysisError` and the sentence `analyze_binary` returns | Built for the one tool where a transient failure (a timeout) differs from a permanent one. **Deliberately not applied to the conductor's other refusals**: REA's flag exists for MCP clients that are programs and branch on a field, but the consumer of every tool result here is the model, which reads the sentence, and those sentences already say what to do next (`Refused, and retrying will not help: ...`, `That call failed: ...`, "ask the person to open a machine"). Converting about 25 hand-written refusals to a structured shape would change text that tests and prompts quote and buy no behaviour. Reopen it if a program, not a model, ever consumes a tool result |
| The `_clip` question (item 5) | `scan.format_scan`, `docs/04` §3.0.6, `tests/test_scan_report_fits.py` | Measured with the real profiles: a scan with hits in about 20 rules was 14,000 characters and `_clip` dropped the coverage line, the stop line and the next step. The report is now built to fit under 11,000, puts coverage and the stop reason **before** the findings, and names the rule groups it held back |
| The rules REA's installation checks keep (section 5 item 6) | `locate_ghidra`, `docs/04` §3.0.7 | A relative path is never executed, a configured location is authoritative even when invalid, a missing tool is a named failure; `make doctor` reports the install |

**Not built, and said so.**

* **Only Ghidra 12.1.4 and 11.2.1 have run it.** The export script compiled and ran unchanged inside a real Ghidra 12.1.4 on the first
  attempt (`make check-ghidra`, `docs/04` §3.0.7): real functions, imports, strings and sections, correct callers and
  callees for a small C program, a cache hit on the second question, and a Cancel that killed the real JVM and its group
  (only defunct entries remained, reaped within 1.5 s). Ghidra 11.2.1 passed the same check, including the cancel; releases between them have not been run.
  The gate itself still drives a stand-in; `make check-ghidra` is the separate verification, outside `make ci`.
* **No decompiler.** The one-shot design gives up interactive decompiling by design (section 4, item 2); it is a second
  decision if it is wanted.
* **No network isolation of the JVM.** The child is given no socket by this code and its environment carries no proxy or
  credential, but nothing enforces "no network"; section 3 says the same of every other child.
* **POSIX only.** The guard's limits and group kill are, as the rest of the spawn guard is.
