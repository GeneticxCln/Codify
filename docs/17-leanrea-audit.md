# Codify — The leanrea audit: a routing table that argues against the tool surface this engine builds

[`aicodeking/leanrea`](https://github.com/aicodeking/leanrea) is the counter-argument to REA, and REA is the repository
`docs/14` audited. Where REA ships 207 MCP tools (3.35 MB of definitions) and a Ghidra provider, leanrea ships **one
8,374-byte `SKILL.md`**: the model already has the commands, it says, so hand it the fastest command for each target
and the traps, and stop installing a tool layer. Its own benchmark puts a lean skill at 388 s, $2.17 and 55 turns
against REA's 627 s, $3.36 and 124 turns at the same accuracy.

That is worth auditing here because it is an argument about *this* engine's shape, not about REA's. Codify has a
conductor with a fixed menu, skills that are data, and one allowlist — which means it has already made half of
leanrea's argument by construction, and the interesting question is the other half: **does the content of a routing
table buy anything an engine like this does not already have, and did leanrea's list of traps find a defect in a
tool this engine already ships?** The second half of that found one. It is in the reply header of `search_code`.

The document follows `docs/11`–`16`: what was read, what is taken and refused (and by which invariant), what was
measured here, what is proposed, and what reopens a row.

**Nothing was copied.** leanrea is MIT (`AICodeKing`, 2026), so copying its text would be permitted, and it is still
not done: `docs/08` and `CONTRIBUTING.md` say no third-party source is committed to this repository, and `docs/16`
set the precedent of expressing an idea in this project's own words. What follows is a comparison of ideas.

## 1. What was read, and what was not

* **Read:** a shallow clone at `09ba4aa` (2026-10-17, the tip of the default branch — the clone holds one commit).
  `leanrea/SKILL.md` **in full**, which is the whole artefact: the rules, two paste-and-run "first move" blocks, a
  router of nineteen rows (Electron/asar, minified web bundles, Hermes/bytenode, Mach-O, native disassembly, ELF/PE,
  Go buildinfo, Rust, .NET, Android APK, Flutter snapshot hash, WASM, SQLite, plist/car, pcap/HAR, firmware, website,
  running app), the speed patterns, and the install list. Plus `README.md`, `bench/BENCHMARK.md`, `bench/run.py`,
  `bench/ctx.sh`, `bench/mcp_list.py`, `bench/tasks.json`, `bench/setup.sh`, `.gitignore`, the licence, and
  `bench/results/final.jsonl` in full (36 rows: 4 tasks × 3 arms × 3 reps).
* **Checked here by hand, and it is not a reading:** every cell of `bench/BENCHMARK.md`'s per-task table and its three
  per-arm totals were recomputed from `final.jsonl` — medians for wall time, means for turns, cost and tokens. They
  reproduce exactly, including the one `none/go` score the document corrects in a footnote (the raw row says 11/12;
  the author's regex wanted `go1.27.1` and the answer said `Go 1.27.1`, which is a grader false negative and is
  stated as one). This engine's own fixed per-call surface and skill menu were measured (§4.2), and `search_code`
  was run over a four-file tree (§4.1).
* **Not run:** leanrea's benchmark, the skills CLI at skills.sh, and any command in the skill. Nothing of leanrea's
  was executed — the harness needs macOS, `Notion.app` 7.35.1 for its `app.asar`, a Homebrew `gh` built with
  go1.27.1, `claude -p` with a Sonnet key, and `rea-agents@6.5.0` from npm. Every number in its tables is its own
  claim; what is checked here is that its tables and its committed rows agree, which is what `docs/08` asks of any
  benchmark anybody quotes.
* **Not read:** REA itself (`docs/14` read it), the four answer keys' grading beyond the regexes in `tasks.json`, the
  benchmark's answers and workspaces (gitignored, and `setup.sh` fetches them from this machine's own `/Applications`),
  and leanrea's history behind its single commit.
* **Unverified by construction:** its machine's measurements. 35.3 s and 196k tokens for one Electron task on one
  Mac is not reproducible here, and this document never treats a leanrea number as a fact about this engine.

## 2. What leanrea is, in this project's terms

leanrea is **a document, not a program**: no server, no socket, no tool definitions, no code. Its mechanism is that a
model already has `Bash`, `Read`, `Grep` and `Glob`, and a short note that tells it the order to use them in and what
goes wrong on each kind of target. The four things it claims to buy are: a smaller context (the note is 8 KB against
207 tool definitions), fewer turns (route by `file` and magic bytes, batch the probes, stop re-verifying), a cheaper
first move per target, and a smaller blast radius than an MCP layer.

Its headline measurement, as published (`bench/BENCHMARK.md`; Claude Code headless, Sonnet 5.5, macOS arm64, 36 runs,
arms side by side, answers graded by regex against hand-made keys):

| arm | wall time | cost | turns | tokens | score |
|---|---|---|---|---|---|
| **leanrea** (8 KB skill, no MCP) | **388 s** | **$2.17** | **55** | **2.40 M** | 74/75 |
| no skill | 479 s | $2.21 | 81 | 3.11 M | 67/75 (one refusal) |
| REA (MCP + its skill) | 627 s | $3.36 | 124 | 5.93 M | 75/75 |

and the context probe ("reply with exactly OK", one call per configuration): 31.4k tokens with nothing loaded, 34.5k
with the lean skill invoked, 34.9k for REA's MCP with Claude Code's deferred tool search, **400.7k with tool search
off** — that is the 207 definitions loaded into every request, at $1.60 for the empty prompt.

Its own caveats are the part that makes it usable, and it states them itself: three reps per cell, one model, one
machine, three targets, Ghidra/Hopper/jadx not installed (so both arms ran on Xcode tools), the REA arm measured in
its *best* case, and — the sentence that matters most for what follows — **its gain over no skill at all is about
19% wall time and 32% turns**, much smaller than its gain over REA.

So the transferable thing is the *rule*, never the ratio: **pay for the tool surface you use, route by target type
before reading code, disclose what you could not see, and let the evidence decide.** Three of those four are already
this engine's doctrine. The audit's work is in the fourth.

## 3. The table

| leanrea's idea | Here | Why |
|---|---|---|
| Work on a copy, never the original | **Have** | `analyze_binary` copies the target into a private run directory (`wx`, `O_NONBLOCK`, bound to the admitted file by SHA-256), and the machine tab edits its own copy of the project (`docs/09` §14, `docs/14` §4 item 2) |
| Route by `file` and magic bytes first; the cheapest inventory next; decompile or read code only after that | **Partly have — in the tools, nowhere in words** | `analyze_binary`'s one-shot export *is* the cheap inventory, and `read_file`/`search_code` are the targeted reads. What does not exist is a skill that orders them for an artifact question, and §5 item 2 proposes one. The order is not enforceable here and should not be: it is instructions, which is what a skill is for |
| Slice a minified or huge file, never `cat` it; cap every output | **Partly have** | `read_file` caps at 8,000 characters and 400 lines and says when it truncated. A **single-line** minified file cannot be paged by line at all, and the tool that can slice it — `search_code` with `regex`, the `.{0,200}NEEDLE.{0,200}` idiom — is not named for that use in any prompt or skill. Instructions again, not capability |
| "Always search with `rg -uu`; `rg` skips hidden and ignored files, and bundlers hide code in `.webpack/` and `.vite/`" | **Have, the other way round — and one real gap** | This engine's own walkers do *not* skip hidden directories (measured, §4.1: a needle in `.webpack/bundle.js` is found), and the exclusions they do apply are a fixed list of 20 names. But they apply them **silently**, which is §4.1's finding — and note the direction of it: leanrea's trap is about a search that hides files, and this engine's is about a search that hides its *own* hiding |
| Mark every claim **seen** (cite file/offset or a symbol) or **inferred**; a fact you did not find is **unknown**, not "absent" | **Have, in four places** | The scan's `Coverage:` line, `analyze_binary`'s "an empty result does not prove absence", `security-review`'s Confirmed / Needs context / False positive, and `usage-audit`'s "unmeasured is not zero". This is `docs/13`–`15`'s doctrine; leanrea arrives at the same rule from a benchmark, which is a second opinion and not a new one |
| Grant the tool no more than it needs; keep the interface small | **Have, structurally** | The conductor's menu is a closed set of 31 engine-defined calls, narrowed by state (the five step moves are not offered until a plan exists; browser actions only when the person allowed them), and a skill can sequence those calls and never add one. It is the property `engine/skills.py`'s docstring is written to state |
| Turns cost more than bytes: aim for two to four, batch the probes, stop re-verifying what you saw | **Have** | The conductor prompt's "your calls are limited… do not re-read what you already have", `DEFAULT_MAX_TURNS`, and `audit_usage` measuring what a turn actually spent (`docs/15`) |
| Reuse an extraction if one exists | **Have** | `analyze_binary`'s cache is keyed by the target's bytes |
| A missing tool is not a blocker: use the fallback in the same row, or install it if the user allows | **Refused** | A missing external tool is a **named failure** here, never a silent substitute: `locate_ghidra` says what is missing and how to fix it, `make doctor` checks the machine, and `make check-ghidra` fails rather than skips without Ghidra (`docs/14` §5 item 6). Nothing installs packages on a model's behalf. This is the row where the two projects genuinely disagree, and this engine is right to: a fallback command a model chose is a capability nobody audited |
| Batch independent probes into one shell call | **Refused** | The conductor's `run_command` is one argv through `validate_argv`, `read_only` until an approved plan (`docs/00` §6.6). There is no composer here, and a shell string a model assembled is exactly what the allowlist exists to prevent |
| The agent runs the real tools itself through Bash | **Refused** | Same door, stated by the project rather than by this audit: a command runs only through `validate_argv`, writes belong to one role behind an approval (`docs/00` §6.6, §6.9). The librarian's ability to inspect *is* real here; it is bounded and typed rather than free |
| An MCP server (or a CLI) as the way in | **Refused** | `docs/11` §3, `docs/12`, `docs/14` — it admits tools the engine does not define |
| Answer keys, wall time, turns, cost and tokens per arm, arms side by side, caveats stated | **Had, differently, and not for answers** | `benchmarks/manifest.json` grades a goal's *effects* (`goal_completed`, `stages`, `files_written`, `test_command`, `file_exists`, `file_contains`) and `docs/08` says what a number may claim. No task's deliverable is an answer, so there is no place for a key — §5 item 3 |
| A context-overhead probe ("reply with exactly OK" per configuration) | **Not had — and §4.2 answers the question it asks, once** | Its value here is as a *measurement*, not a feature: this engine had never put a number on what one of its own calls costs before it does any work. §4.2 is that number |

## 4. What the comparison found here

### 4.1 `search_code` does not say which directories it never walked

**Measured.** Four files holding one needle, in a tree this audit made:

```
src/app.js              MARKER_NEEDLE_XYZ …
dist/bundle.js          MARKER_NEEDLE_XYZ …
.webpack/bundle.js      MARKER_NEEDLE_XYZ …
node_modules/pkg/index.js  MARKER_NEEDLE_XYZ …
```

`LibraryService.search` found it in `src/` and in `.webpack/`, and returned:

```
--- search 'MARKER_NEEDLE_XYZ': 2 matches in 2 files
--- search 'nothing_here_at_all': 0 matches in 2 files
```

with `files_scanned: 2, files_skipped: 0` in both cases. Two of the four files were never opened, and the header says
that **nothing** was skipped. A model asking "where is this used" gets an absence with no asterisk on it.

Why this is a defect and not a preference, in this project's own words:

* An unscanned file is not a clean file. That is the `security-review` skill's sentence; `library.py`'s docstring
  states the same rule from the other side ("a librarian that silently sees one third of a repository produces
  confident wrong answers"), and `docs/14`'s coverage row made it a field requirement. It is the rule the whole
  librarian is built around, and the search header is the one place it is not kept.
* `_search_regex`'s docstring claims the search and the scan "report identically". They do not: `scan_code` prints a
  `Coverage:` line **before** its findings naming every reason a file was left out (test files, generated or
  minified, binary, over the size cap, symlinks out of the workspace, unreadable, names that are not valid UTF-8),
  and `format_search` prints one merged count, `N unreadable or binary files skipped`.
* Neither tool counts a **directory** pruned from the walk. `library.SKIP_DIRS` holds 20 names — `.git`, `.hg`,
  `.svn`, `node_modules`, `__pycache__`, `.venv`, `venv`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.tox`,
  `dist`, `build`, `target`, `.next`, `.nuxt`, `.cache`, `vendor`, `.idea`, `.vscode` — and every strategy
  (`_search_substring`, `_search_keyword`, `scan_regex`, and `scan_tree` beside them) filters `dirnames` against it
  and reports nothing back. A repository that keeps code in `dist/`, a Next.js tree whose real modules are under a pruned
  `.next/`, a workspace whose only consumer of a symbol is inside `vendor/`: every one of those answers is an
  absence. The scan's "generated or minified" count does not cover it — that counter is a *file-name* pattern
  (`*.min.js`, `*.map`, `*.lock`, `*_pb2.py`), and a plain `.py` under `dist/` is in no category at all.

This matters more here than it would in leanrea, because leanrea's reader is a model that then runs its own command
and can look again. A conductor reading `0 matches in 412 files` has no second look: `docs/13` and `docs/14` both
recorded that this project's failure mode is a confident empty result, and this is one of the few remaining places
where one can be produced.

The **shape of the fix** is a sentence, not a capability, and it does not widen anything *(built, for both tools; §8)*: name the directories the
walk pruned, without counting their contents — walk cost is what `SKIP_DIRS` exists to save, and a reader needs to
know *that* `vendor` was not searched and *where* the answer might be, not how many files are in it. One set union
per walk, bounded to a handful of names, and `format_search` grows a clause next to the skip count it already prints.
The scan's coverage line can carry the same clause for the same reason. If the walk ever needs to *include* one of
these directories again, that is a different decision that would move `SKIP_DIRS` itself, and this audit does not
propose it — the fix is to stop reporting an empty answer as a complete one.

### 4.2 The fixed cost of one conductor call, measured

leanrea's sharpest number is a *cost* number, and this engine had never put one on itself. Measured on this checkout
by reading the prompts and the tool table directly (characters, then ÷4 as the crude token estimate `docs/15` also
warns about):

| what one conductor call pays before any work | characters |
|---|---|
| `CONDUCTOR_SYSTEM_PROMPT` | 3,370 |
| the 25 base tool schemas (name, description, parameters) | 21,019 |
| the skill menu — every built-in's name and description, never a body | 774 |
| **fixed surface, plan not yet approved** | **≈ 25,163 (~6.3k tokens)** |
| the 5 step moves' schemas, added once a plan exists | + 3,138, ≈ 28,301 (~7.1k tokens) |
| the four built-in skill bodies, in total, and only on `use_skill` | 15,805 (2,705–5,440 each) |

Two things follow, and both are the answer to leanrea's headline rather than a criticism of it. First, **the tool
surface is not this engine's problem**: 31 engine-defined calls against REA's 207, and the whole fixed surface is
about 1.6% of the 400.7k-token row leanrea measured for REA with tool search off — and about twice what invoking
leanrea's own 8 KB skill added to its context (+3.1k). There is no argument here for a cheaper routing mechanism on
cost grounds alone, which is worth having written down before somebody builds one. Second, **83% of what a call pays is tool definitions**
(21,019 of 25,163 characters), so if that number ever does need to come down, that is where it lives — schema
prose, not the system prompt and not the skills.

The skill design is already leanrea's, and slightly better: a body costs nothing until `use_skill` reads it, so all
four built-ins together cost the menu's 774 characters, while leanrea pays its whole 8 KB to invoke its one skill.

### 4.3 The parts of leanrea that do not transfer at all

Its router is a map from *target kind* to *command*, and the commands are macOS-shaped throughout: `lipo`, `otool`,
`codesign`, `xcrun`, `plutil`, `assetutil`, `brew install`. This project's engine runs under the Tauri shell on
Linux and macOS and is not a reverse-engineering tool: it inspects a workspace's own text, scans it with curated
rules, fetches pages the person allowed, and runs Ghidra on one binary when asked. Its rules ("work on a copy",
"route before you read", "mark what you did not see") apply here; its table of nineteen rows does not, and copying
it in would be a document about somebody else's machine.

## 5. What could be built, if the owner wants any of it

In order of value for cost. None of the three adds a capability, and the first two are the two halves of one idea.
Items 1 and 2 are built (§8); item 3 is not.

1. **Disclose the pruned directories in `search_code`'s (and the scan's) header** — §4.1. Touches
   `engine/library.py` (one counter and one clause in `format_search`), `engine/scan.py`'s coverage line, and
   `docs/04` §4.0 (the search strategies) with §3.0.6 (the scan's coverage line), plus a test that a needle living only under `dist/` is reported as *not searched there* rather
   than as absent. No new tool, no new argument, no widening: the walk is unchanged, only its account of itself.
   This is the one row this audit would build.
2. **A built-in skill that orders an artifact question** — `engine/builtin_skills/inspect-an-artifact.md`, in the
   shape of `security-review` and `usage-audit` (`moves: analyze_binary, read_file, search_code, scan_code, recall`).
   What it would say, in this engine's terms and this repo's own words:
   * route first — ask the workspace what a file *is* (`read_file` a few bytes, or the `file`-shaped facts the walk
     already gives) before reading it as source;
   * the cheap inventory before the reading: `analyze_binary` for a compiled file, `search_code` for a symbol's
     callers, `scan_code` for what the rules recognise;
   * slice, do not cat: a minified or generated file is read through `search_code` with `regex`, which is the only
     tool that can take a window out of a single-line file;
   * mark each claim seen (a symbol, a string, a file:line) or inferred, and say "not found" rather than "absent" —
     and say it *because* the tools say it, quoting the coverage line and the "an empty result does not prove
     absence" note instead of inventing the caveat;
   * when `analyze_binary` refuses because Ghidra is not installed, that is the answer: say so and stop, do not
     substitute a different tool and present it as the same result (the row §3 refuses);
   * never widen: this is an order, and every step of it is a call the conductor already had.
   Cost: one file plus a test that it loads, declares only real moves, and names no capability. Value: the *order*
   is the part of leanrea's result that is plausibly causal, and this engine already made that order expressible.
3. **Grading an answer in the benchmark harness** (deferred, not recommended yet). leanrea scores 75 facts against
   hand-made keys, which is how a six-turn analysis can be compared on wall time *and* accuracy at once. `benchmarks/runner.py`
   has no check type for an answer, because every task in `benchmarks/manifest.json` is a code change and its checks
   are effects (`test_command`, `file_exists`, `file_contains`). A `answer_contains` check over a turn's reply would
   let a task ask a question and be graded on the answer — useful the day somebody wants to compare a conductor
   setting or a model by accuracy rather than by whether the pipeline completed. It needs `runner.py` to drive a
   *turn* (`POST /conversations/{id}/turns`) rather than a goal, which is new machinery in the harness, and `docs/08`
   would own the claim. Deferred: the engine has no such comparison today, and inventing the benchmark before the
   question is the wrong order.
4. **The rest is refused, in §3's rows**, and the invariants are the reasons: items 6 and 9 for the shell and the
   writes, `docs/14` §5 item 6 for install-on-demand, `docs/11` §3 for an MCP front door.

## 6. What this does not claim

* That leanrea's numbers are right. They are its own measurements on one Mac with one model, three reps per cell,
  and this audit could only reproduce its *table from its rows*, which is what it did. Nothing of it was run.
* That a routing table is what made leanrea faster. It reports its own control, and it is the honest half of its
  result: against **no skill at all** its edge is 19% less wall time and 32% fewer turns (479 s / 81 turns → 388 s /
  55), and the rest of its advantage over REA is REA's tool-surface cost. The causal half of the number is "a shorter
  surface and fewer turns", not "a router".
* That §4.1's fix makes a search complete. It makes the tool honest about its own boundaries; the directories stay
  unsearched, deliberately, and a model that needs `vendor/` still cannot reach it — which is a separate decision
  this audit does not open.
* That the numbers in §4.2 are a measurement of tokens. They are characters of prompt text, a proxy, and `docs/15`
  already refuses to turn a figure like this into money.
* That this engine needs a skill for anything. Every row in §5 is optional, and the first is a sentence in a string
  that already exists.

## 7. If a rejected or deferred row is reopened

* **Letting a search or scan walk `SKIP_DIRS`.** A new argument to a tool, or a change to the constant, is a change
  to what the librarian *sees*: it reopens `docs/04`'s tool contracts, the "unscanned is not clean" guarantee, and
  the cost model those 20 names exist to keep. Its own decision, not a follow-up to this one.
* **Grading an answer (item 3).** Starts with `docs/08`: a new check type, what a number may claim, and a task whose
  deliverable is a sentence. It also needs the harness to drive a turn, which is the first time `benchmarks/` would
  touch the conversation path.
* **A routing skill for anything else** (`fetch_page`'s spokes, the machine tab, git archaeology) is the same
  argument as §5 item 2 with a different subject, and lands in the same place: a skill that orders moves, never one
  that adds them.
* **Anything in this document that would have the model compose a command.** That is `docs/00` §6.6 and §6.9, and it
  is closed; `docs/09` §14 is the only place a person's jailed shell accepts a typed command, and only a person can
  open one.

## 8. What was built, and what was not

The owner asked for §5 item 1, then for the scan's Coverage line to carry it too, then for the walkers to report
their other structural silences — the file cap and the byte cap — in the same header, then for §5 item 2. All of those
are built; item 3 is not.

| Item | Where | Status |
|---|---|---|
| `search_code` names the directories its walk refused | `library._walkable`, `skipped_dirs` on the result of all three strategies, the clause in `format_search`; `docs/04` §4.0; `tests/test_library.py` (`TestTheSearchesNameWhatTheWalkRefused`) | Built and tested |
| `scan_code` does the same, in its `Coverage:` line | `scan.scan_tree` (the same `_walkable`), `skipped_dirs`, `format_scan`; `docs/04` §3.0.6; `tests/test_scan.py` | Built and tested |
| The other two structural silences of the search: which cap ended the walk, and the files read only in part | `stopped` (`"files"` / `"matches"`), `byte_capped`, the two sentences in `format_search`; `docs/04` §4.0; `tests/test_library.py` (`TestTheSearchesNameTheirOtherStructuralSilences`) | Built and tested |
| A built-in skill that orders an artifact question (§5 item 2) | `engine/builtin_skills/inspect-an-artifact.md` (`moves: analyze_binary, read_file, search_code, scan_code, recall`), named in `docs/01` §5.1, `docs/09` §10.14 and `CLAUDE.md`; `tests/test_skills.py` | Built and tested |

The shape is the one §4.1 argued for: a name, not a count — the walk is unchanged, no argument was added to any
tool, and `SKIP_DIRS` is still exactly what it was. Five tests hold the first row: all three strategies report the same names
(the regex one through the worker's JSON reply, which is the path that could have dropped the field), a needle that
lives only under `dist/` is reported as *not searched* rather than as absent, a directory that is merely unusual is
still searched and is not named, the list is capped at `SHOWN_SKIPPED_DIRS` with a count of what was held back, and a
workspace with none of these directories gets the header it always got.

They fail without the change, which is the only reason to believe them: with the reporting removed and everything
else left in place, the needle case renders `--- search 'ONLY_IN_DIST': 0 matches in 1 files` — a string that is in
the workspace, reported as an absence — and the four others fail on the missing field. With the whole change in
place, `tests/test_library` is 54/54 and `tests/test_scan` is 65/65.

The two later rows are the same argument one layer out. `scan_code` shares the walker, so a `dist/` full of minified
bundles was in no skip category at all — its "generated or minified" counter is a *file-name* pattern — and the
Coverage line now names those directories beside the per-file reasons it already had. And a search had two more
silences of exactly the kind this document is about: `MAX_FILES_SCANNED` and the match cap both rendered as one
`TRUNCATED — results are partial`, so "listed every match there was" and "never opened the later files" read alike
(the reader's next move differs: a `glob` narrows the walk, nothing narrows a list that is already everything); and
every file was read only to its first `MAX_SCAN_BYTES` bytes with nothing saying so, so a needle in the tail of a
large file was an absence. The header now names which cap ended the walk and counts the files read in part.

Two properties were worth making exact while naming them. The match cap is now checked one row past the cap — the
extra row proves there is more and is dropped, not shown — so a list that fills `MAX_MATCHES` exactly is complete and
is not called cut; naming a cap made a wrong claim louder. And the scan's word for a read-in-part file (`N large files
read only in part`) was already there and is unchanged: that was the one silence the scan did not have.

The fourth row is §5 item 2, and it is a skill and nothing else: `engine/builtin_skills/inspect-an-artifact.md`, one
file of instructions, declaring `moves: analyze_binary, read_file, search_code, scan_code, recall`. What it orders is
the part of leanrea's result this audit believed was causal — route by what the file *is* before reading it (a
lockfile is text; a compiled file read as source comes back as replacement characters, and the walkers skip binaries
and count them), one wide `analyze_binary` call before any narrow one (the first call on a file runs Ghidra for
minutes, every later question about the same bytes is answered from a cache), `search_code` with a regex window for a
minified or generated file because a line window cannot page one line, and then the tools' own caveats quoted rather
than paraphrased: an empty export does not prove absence, a search's counts describe the files it opened and it names
what it skipped, a missing Ghidra is a sentence to report and never a tool to substitute for it, and static reading is
not observed execution. `tests/test_skills.py` pins those, the description length, and that every move it declares is
one that exists — the property that keeps a skill data rather than a capability.

**Not built, and said so.**

* **Nothing was widened.** No tool argument, no change to `SKIP_DIRS`, and no change to which files any strategy
  reads: the walk still refuses exactly the directories it always did, and says so. A result carries `skipped_dirs`,
  `stopped` and `byte_capped` only when there is something to report, so a small, whole workspace's search result is
  exactly the shape it always was.
* **The keyword strategy's word-count semantics are untouched.** It ranks whole files, so its cap counts files and
  its matches carry `line: 0`; the disclosure is about coverage, not about the strategy.
