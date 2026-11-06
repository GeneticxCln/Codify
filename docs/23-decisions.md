# 23. Standing decisions: what is settled, how to work here, and what is still open

This file exists so that nobody (an agent least of all) has to ask what was decided, or why, or where it is written. It does
not replace the documents that own a decision. It says where each one lives, adds the owner's own calls and the working
rules that were only ever said in conversation, and lists what is still genuinely open, each with a recommendation, so a
question is asked once and answered once.

## 1. How to use it

* **Look here before asking.** If a decision is in §2 or §3, act on it. If it is in §5 as *open*, do not act on it: ask once,
  with the recommendation attached, and record the answer here in the same change as the work.
* **Status words.** *Decided* is the owner's call or the repository's own text. *Default* is a working rule an agent proposed
  and the owner confirms by merging this file; a default is one edit away from being overruled. *Open* needs the owner.
* **Authority.** Where this file and the document it points at disagree, the other document wins and this file is the bug,
  exactly as for `CLAUDE.md`. `tests/test_docs_references.py` fails when a path or section named here stops existing.
* **Answering an open decision.** Change its status to *Decided*, write the answer and the date in the row, and merge. That
  is the whole procedure; no other message is needed.

## 2. Decided by the owner

Each was an explicit answer. The pull request that carries it is the source: `git log` finds it, and it is not repeated here.

| # | Decision | Where it landed |
|---|---|---|
| D1 | **Unjailed runs are refused until a person allows them.** With `sandbox_isolation=auto` and no working jail, a command that runs the project's own code is refused unless `sandbox_unjailed_ok` is set. | PR #95; `docs/03` §1.4 |
| D2 | **A profile import skips skills, memory and notes unless asked, and says why.** Skills are opt-in like the other two. | PR #96; `docs/09` §10.24 |
| D3 | **An audit's first deliverable is a report, and the owner chooses the fixes.** Scope of the first audit was bugs and security gaps. Nothing is fixed in passing during a read-only pass. | the audit passes |
| D4 | **An agent may install what it needs to verify rather than assume** (bubblewrap, a Python 3.10 environment, WebKit/GTK development libraries) and must say what it could not verify. | the audit passes |
| D5 | **Standing notes get a Settings card** (the switch, and list, add and delete). Not CLI-only, not removed. | PR #100; `docs/02` §3.0c |
| D6 | **A goal's own `allow_network` stays API-only and documented.** The person's control is the engine-wide `sandbox_network` setting. | PR #99; `docs/02` §3.0a, `docs/04` |
| D7 | **The executor and conductor are read in full, counted, report first.** Thirteen files, coverage stated as lines read over lines total, unread parts named, fixes only after the owner picks. | the audit log; §5 O1 |
| D8 | **Goal status: a narrow rule, not a strict table.** The service refuses any move back into `PLANNING`, `RUNNING → PENDING` and `PAUSED → COMPLETED`, and leaves the `PLANNING → RUNNING` shortcut that tests and the benchmark harness use. | PR #101; `docs/04` §1.2 |
| D9 | **Work is not finished at "branch pushed".** A change that is meant to land has a real pull request (a draft is the default), and a bare branch that equals `master` is not one. | owner feedback |
| D10 | **Say what was not done.** A partial pass is reported as partial, with the unread parts named. Stopping to wait is allowed; calling it finished is not. | owner feedback |

## 3. Already decided in the repository

The owner of each is the document named; read that, not this row.

| Topic | Decision, in a line | Owner |
|---|---|---|
| Roles | Exactly eight; the conductor is a loop, not a ninth role | `docs/00` §6.1 |
| Configuration | Changed only by the settings routes, never by a goal or a turn | `docs/00` §6.2 |
| Engine access | Binds `127.0.0.1`; a bearer boot token on every request; CORS limited to the app's own origins | `docs/00` §6.3, `docs/03` §1.3 |
| Secrets | No raw key in any response; keys live in the OS keychain or an owner-only file | `docs/00` §6.4, `docs/03` §1.1 |
| Local providers | The address must pass the loopback check before every request | `docs/00` §6.5, `docs/03` §1.2 |
| Commands | Every command goes through `validate_argv`; a jail by default; no network unless a person opened it | `docs/00` §6.6, `docs/03` §1.4 |
| Accepted risk | An allowlisted command still runs the project's own code; approving a goal is approving that | `docs/03` §1.4 |
| Storage | One SQLite file under `~/.codify`; no second database | `docs/00` §6.7 |
| Turns | Created only by `POST /conversations/{id}/turns` | `docs/00` §6.8, `docs/09` §10 |
| Writes | Only the fixer writes; the `write` move is the one door; a skill is data, never a capability | `docs/00` §6.9, `docs/09` §10.14 |
| MCP | A remote data source, never a capability the engine did not define | `docs/00` §6.10, `docs/20` |
| Pause and Cancel | Pause stops between steps and Start resumes; Cancel also stops running work; a terminal status is final except by the two reopening moves | `docs/04` §1.2 |
| Review depth | Review lenses are parameters, not roles; depth is the person-only `review_deep` switch | `docs/21` §2 |
| Budgets | Baselines with a tolerance, raised only with a written reason in the file that holds them | `docs/21` §2, `ui/budgets.json` |
| Settled defaults | Prompt override size, the SQLite path, the redirected-run rule and the rest | `docs/03` §4 |
| Browser | Deny-by-default webviews; no way to open the system browser; loopback addresses refused; model-driven actions off until a person allows them | `docs/03` §1.5, §1.6 |
| Fetching pages | `fetch_page` is off until a person allows it, reads public addresses only, and is bounded | `docs/03` §1.6, `docs/12` |
| The machine tab | The one place a command runs without `validate_argv`, and only inside a jail a person alone can open | `docs/03` §1.11, `docs/09` §14 |
| Platforms | Linux x86_64 only; a macOS or Windows build is a spike, not a commitment | `docs/18` |
| Benchmarks | Hermetic smoke tier only in the gate; nothing third-party is committed | `docs/08`, `CONTRIBUTING.md` |
| Tests | A test that fails without the change; no skipping out of a guarantee; hermetic state; types checked; no hardcoded model lists | `CONTRIBUTING.md` |
| The gate | `make check`; `make ci` adds the Python 3.10 floor and is the real gate | `CONTRIBUTING.md`, `CLAUDE.md` |
| Documents | A change that alters a documented contract updates its document in the same change | `CLAUDE.md` |

## 4. How an agent works here (defaults)

These are defaults: proposed here, confirmed by merging this file. Change a row in a pull request to overrule it.

### 4.1 Branches and pull requests

* Develop only on the branch the session names, based on `master`. After that branch's pull request merges, restart it from
  `origin/master` (`git fetch origin master && git checkout -B <branch> origin/master`). Never force-push, amend or rewrite
  history that has been pushed.
* **Push only when there is a commit that is not on `master`.** Once a pull request merges, GitHub deletes the remote branch,
  and the session's "unpushed commits" notice then counts the merge commit that is already on `master`. Check
  `git log origin/master..HEAD`: if it is empty or holds only a merge commit, there is nothing to push, and pushing would only
  recreate a branch equal to `master` (D9).
* Open the pull request as a draft, in the same step as the push, and subscribe to it. One concern per pull request, small
  enough to review. Do not approve or merge, and do not mark a draft ready.
* **The owner merges within seconds of marking a pull request ready.** Two of the first four (#98, #101) were merged while CI was
  still running; the other two (#99, #100) had finished first. So a pull request must be correct at its first push: run the
  whole local verification (§4.2) before pushing, not after. When a merge lands before CI does, check CI on the merge commit
  afterwards instead of assuming.
* The body states why, what, verification (with what could not be verified), and what is deliberately not done. Commit
  messages and the body carry the attribution lines the session supplies. A check-in about 50 minutes after opening, cancelled
  when the pull request merges, is the safety net for events that do not arrive.

### 4.2 Verification before a push

* `ruff` and `mypy` clean; the full Python suite on 3.13 **and** on the 3.10 floor; for `ui/` changes also `npm test`,
  `tsc` over `ui/src` and `ui/tests`, `make build-ui` and `make budgets`; always `make test-streams` and `make bench-smoke`;
  the docs tests when a document changed (`tests/test_docs_references.py`, `tests/test_claude_md_contracts.py`).
* **Run the two Python suites one after the other, not together.** Python 3.10 runs reported failures only while another suite
  was running on the same machine, and passed alone; the failing tests were never captured. Treat that as load-sensitive and
  unidentified, never as a pass, and never as fixed.
* **Keep the full output of a long run in a file.** A `| tail` over a failing run hides which tests failed, which is how that
  failure stayed unnamed. A failure is named, reproduced alone, and then either fixed or reported with its name.
* A new test is shown failing first, against the code without the change, and then mutation-checked: break the behaviour it
  guards and watch that test fail. A UI control is tested mounted (`ui/tests/dom.ts`), not by reading its source.

### 4.3 Evidence and honesty

* A finding carries a tag: *reproduced* (a command or test that fails), *read* (traced in code, not run) or *suspected*. A
  hypothesis that cannot be traced is dropped or tagged, not reported as a fact.
* Coverage is counted. A report says lines read over lines total, names what was only skimmed, and lists what was not read.
* A check that errors is fixed and run again, not skipped. "Flaky" is a cause only after the failing test has been named.
* A scan or script that reports a problem is checked by hand before it is reported (several false positives in the coherence
  audit were caught that way).

### 4.4 Ask first, or act

**Act without asking:**

* read-only investigation, running tests and linters, and writing probes in the scratch directory;
* correcting a document that contradicts the code, when a test or a read shows which is right;
* adding a test that pins behaviour that already holds, and fixing a test that depends on another test or on the machine;
* a bug whose contract a document states and whose failing probe exists, when the fix is local and the owner has not left the
  area open in §5;
* the items listed *Default: proceed* in §5.

**Ask first, once, with a recommendation:**

* anything that weakens or widens an invariant (`docs/00` §6), the command allowlist, the jail, or what a model may read, write
  or send;
* new network access, a new dependency, or a change to a default that protects a person (a safety setting, a retention
  default, an off-by-default switch);
* deleting or overwriting a person's data, or any change a person cannot undo;
* raising a size budget, a baseline or `MAX_CLAUDE_MD_BYTES`, or changing what the gate runs;
* where the code and the document disagree and neither is clearly right;
* approving or merging, and anything outside this repository.

When something has to be asked, ask it as one question with the options, the recommendation first, and write the answer into
§2 in the same pull request as the work that needed it.

## 5. Open decisions

Each needs the owner unless its *until decided* column says the agent may proceed.

| # | Question | Options | Recommendation | Until decided |
|---|---|---|---|---|
| O1 | **Executor and conductor findings.** The owner chose to fix the first three, each as its own pull request, and the failed-sibling item as O2 below. Done, each as its own pull request: `verify` asks again before the process starts, so a Pause or Cancel during the verifier's model call or the lock wait stops the project's tests; one reply has at most 16 tool calls run; and the recipe's write check refuses a goal that is `PLANNING` or `PENDING`. The five low items stay as documented. The read found: `verify` ignored a Pause that landed during the verifier's model call (the project's tests still ran); no cap on tool calls in one model reply (20,000 requested, 20,000 run under `max_turns=3`, about 350 MB); `run_step` writes for a goal nobody approved (reachable by no caller today); a healthy parallel sibling is left written but unreviewed and uncommitted when another step fails; and five low items. The rest of the read added no engine finding, only a cosmetic one (a model call cancelled mid-stream leaves its streamed snapshots un-compacted) and a note under the tool-call item: the replay bound counts a reply's text and tool results but not its tool-call arguments. | done | done | Closed |
| O2 | **The failed-sibling policy.** `docs/04` §1.2 says a healthy step in a parallel batch finishes when another fails; the code stopped it after its write and said "cancelled". The owner chose to follow the document. Done: the healthy step is reviewed and committed, a real cancel still stops it, the log names what actually stopped it, and a critic's request for changes on an already failed goal leaves the step open for a Retry instead of raising an illegal pause. | done | done | Closed |
| O3 | **Merging before CI.** A pull request can merge within seconds of being marked ready, so CI results can arrive after the code is in (#98 and #101 did). Branch protection is not visible from a session. | require the checks before merge, or accept it | Require the six checks, so green means green | Verify locally before every push (§4.1) |
| O4 | **The jail's abstract sockets with the network on.** An abstract unix socket belongs to the network namespace, so a jail run with the network opted in can reach the host's, X11's among them (`docs/03` §1.4). | accept and document (today), give the jail its own namespace with no host sockets, or refuse the network while such a socket exists | Keep documented until a person needs the network inside the jail | Leave it |
| O5 | **Browser navigation to private-network addresses.** The guard refuses only loopback; a page can go to a LAN, link-local or cloud-metadata address (`docs/03` §1.5). | leave, block link-local and metadata only, or give model-proposed navigation the same public-only rule `fetch_page` has | Block link-local and metadata for everyone, and public-only for model-proposed navigation; leave a person's own LAN browsing alone | Leave it |
| O6 | **Host header validation.** Nothing checks `Host`; the bearer token on every request is the only defence against a rebinding page. | add a check, or accept | Add a `Host` allowlist (cheap, and independent of the token) | Leave it |
| O7 | **The CSP allows `style-src 'unsafe-inline'`** (`src-tauri/tauri.conf.json`) and no document records why. | document the reason, or tighten | Document it if the UI needs it; otherwise tighten | Leave it |
| O8 | **No coverage gate and no formatter.** Neither exists; `ruff` only lints. | add either, or record that neither is wanted | Record "none": a coverage percentage misleads, and a formatter would reformat the repository | Leave it |
| O9 | **Very large files.** `engine/app.py` is over 4,000 lines and `engine/services.py` over 2,600. | a dedicated split, or split when touched | Split only when a change already touches the part | Do not split |
| O10 | **The five open decisions in `docs/22` §8** (workspace instruction files, pinning a note to a place, kinds of remembered fact, skill on versus off, a media probe). | each is its own design | Owner's order | Do nothing |
| O11 | **The unidentified Python 3.10 failure under load** (§4.2). Two loaded runs reported failures (1 error; 1 failure and 1 error) and their test names were never captured. Since then four runs with the output kept all passed: the 3.10 and 3.13 suites together (twice), the 3.10 suite beside the stream and benchmark legs, and the 3.10 suite with eight busy loops on four cores. CPU load alone does not trigger it. One suspect was read and set aside: tests that ask the OS for a port, release it and bind it again (`tests/test_home.py`) could collide with a second process, but that window is microseconds. | keep trying, or leave | Leave it, and the next time a run fails keep the full output and name the test before anything else | **Default: proceed when a failure appears**, with the output kept |
| O12 | **`tests/test_versioned.py` failed when run alone** (11 errors on an untouched `master`, because it relied on an earlier test having left an executor on the shared `app`). Fixed: the test supplies its own stand-in and puts back the app state it borrows. | done | done | Closed |
| O13 | **The ResourceWarnings** the suite printed, whose source was never pinned down. Found by running every test module in its own process with all of them shown, then `tracemalloc`: three sources, all fixed. `tests/test_run_tests_guard.py` never closed a `Popen`'s pipes; `scripts/tabs_smoke.py` never closed its child's pipes and kept every run's child alive through an `atexit` callback and two signal handlers; and one test in `tests/test_model_catalog.py` closed a server connection only after a sleep that the runner cancels, which Python 3.10 reported as six unclosed transports. `tests/leaks.py` fails a sync test that leaves a resource open. | done | done | Closed |
| O14 | **The rest of the executor and conductor read.** Done: all thirteen files, 11,514 lines. Not read word for word: 299 lines of `conductor.py` that are tool descriptions (skimmed for code; there is none) and 3 lines of `conductor_tools.py`. Nothing beyond O1 changes behaviour. | done | done | Closed |

## 6. Adding a decision

Write one row in the right table, with what was decided, the date, and where the work landed. If the decision belongs to an
existing document (an invariant, a setting, a security rule), write it there first and add only an index row here. If it
changes what an agent may do without asking, change §4 in the same pull request. Never record a decision here that nobody
made: an *open* row stays open until the owner answers.
