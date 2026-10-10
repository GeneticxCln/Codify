# Codify — The Grepify audit: a code-review scanner, built in and native

Grepify is NCC Group's scanner for code reviewers. The request was a tool *like it*, **inside the assistant**, and
not an integration with their product. The result is one conductor tool, `scan_code`, and a built-in
`security-review` skill that tells the model what to do with what the tool finds. This document follows the shape
of `docs/10`, `docs/11` and `docs/12`: what was read, what was taken and refused, what was built, and what it does
not claim.

## 1. What was read, and what was not

* **Read:** the README of [`nccgroup/grepify`](https://github.com/nccgroup/grepify) and its wiki front page, and a
  web search result describing it. They are thin. What they say: "the GUI Regex Text Scanner for Code Reviewers", a
  Windows GUI that scans ASCII files for matches to **profiles** (a profile is a list of regexes you supply), can
  skip comments in the code and skip files with "test" in their name or path, opens a result in an editor at its
  line, shows the file in Explorer, and exports CSV. It ships profiles for several platforms (Windows, iOS, generic
  C and others). AGPL-3.0.
* **Not read:** its source, its profile files, its profile syntax (the documentation I could reach does not give
  one), its blog post. Nothing was installed or run. **So no part of it can have been copied, and none was.** The
  rules here are written from public weakness names (CWE), the example strings are original, and nothing was derived
  from another scanner's rule set either (Bandit, Semgrep, gosec and the rest were not read for this).
* **Other tools with a similar name** turned up in the same search: an unrelated news aggregator also called
  Grepify, and two tools with different names (Graphify, a code knowledge graph; grepai, semantic search). They are
  not what was asked for, and `docs/10` §4.4 already says why embeddings and a second store are not built.
* **Every Grepify statement above is from those pages.** None of it was tested.

## 2. What was taken, and what was not

| Grepify, as its pages describe it | Here | Why |
|---|---|---|
| Profiles: named lists of regexes | **Have.** `engine/builtin_profiles/*.json`, and a workspace's own in `.codify/profiles/*.json`, which replace a built-in of the same name (announced). | The idea is the point. The format and every rule are this repository's own. JSON because the 3.10 floor has no `tomllib`. |
| Skip comments | **Have.** `scan.mask`: a small tokenizer per language family that blanks comments (and, for rules about code, the inside of strings) while keeping every newline and column. | A rule about `eval(` must not fire on a log line that names it. Line numbers stay true. |
| Skip test files | **Have.** `scan.is_test_path`, by directory and by file name; `include_tests` brings them back. | Test code is full of deliberate bad examples. |
| Results at their line, opened in an editor | **Partly.** Every hit is `path:line`, and the assistant already has `open_in_editor` to point a person at one. **No results pane.** | A pane touches the shell and the settings screens while the Tailwind migration is in flight; it can be added on the same engine API. |
| CSV export | **Not taken.** | A model reads text; a person can ask for the report. |
| The Windows GUI | **Not taken.** | This is a tool for the assistant, in an app that already has its surfaces. |
| Its code and its profile contents | **Not used** (not read; AGPL-3.0). | `CLAUDE.md`: no third-party source is committed here. |

What Grepify does not do, and this does: **the model triages.** A scanner's output is a list of lines to read.
`security-review` makes reading them the job: pick profiles, scan, read each high and medium hit with `read_file`,
classify (confirmed, needs context, false positive, with the reason), report with the evidence, and say what was not
scanned. Fixing goes through `plan` and the person's approval, like every other change (`docs/00` §6.9).

## 3. What was built

`engine/scan.py`, `engine/builtin_profiles/` (6 profiles, 56 rules), `engine/builtin_skills/security-review.md`, the
`scan_code` tool in `engine/conductor.py` and `engine/conductor_tools.py`, and a second operation in
`engine/regex_worker.py`.

* **A rule** is a regex with the file extensions it applies to (none means every text file), a severity, a CWE, a
  `where` (`code`: comments and the inside of strings blanked; `strings`: comments blanked; `raw`: nothing blanked, for
  a committed key, which is a leak in a comment too), a sentence on why it matters, a fix, and **an example it must
  match and one it must not**. `tests/test_scan.py` runs every example through the same pipeline a scan uses, and has a
  negative control so the check can fail.
* **The profiles:** `secrets` (7), `injection` (16: SQL by concatenation, shell, `eval`, DOM sinks, across Python,
  JavaScript, Go, Java, PHP, shell), `crypto` (12: weak hashes and ciphers, switched-off certificate checks, guessable
  tokens), `deserialization` (6), `unsafe-c` (7), `web` (8: debug on, wildcard CORS, CSRF off, markup trusted).
* **The tool:** `scan_code(profile?, glob?, include_tests?, include_comments?)`. No profile lists them. A profile name
  (or `all`) scans. The result starts by saying the hits are **candidates, not vulnerabilities**, then groups hits by
  rule, most severe first, with the rule's reason and fix and each `path:line`, and ends with **coverage**: files
  scanned, and a number for each thing skipped (tests, generated or minified files, binary, over the size cap,
  symlinks out of the workspace), files read only in part, lines cut, which file types it cannot read comments in,
  and why a scan stopped early. It is in `BASE_TOOLS` and has no setting: it reads, like `search_code`.
* **Bounds:** 800 files, 200 KB read per file, a line is scanned to 2,000 characters, 8 hits kept per rule and 60 in a
  report (the rest counted, never silent), a rule dropped after 500 hits, 15 s. Several matches on one line are one hit.
* **A workspace profile is data, never a capability**, the rule `engine/skills.py` states for skills. It arrives with a
  cloned repository and is untrusted. It can add or replace rules; the worker is handed only `id`, `pattern`, `flags`,
  `languages` and `where`, so a profile has no path to name, no command and nothing to write. Its wording (title, why,
  fix) is clipped, and a scan that used one says so before the hits, because that text is the repository's, not ours.
  A bad rule is dropped and reported (never raised); a symlinked profile file is not read, and neither is a directory
  that is: `.codify/profiles` must resolve to exactly `<workspace>/.codify/profiles` (a link at `.codify` or at
  `profiles` is refused and reported, the way `engine/skills.py` refuses the same for `.codify/skills`), because once a
  link is followed every file behind it looks like a plain file and the repository would choose which of the machine's
  directories is read as rules; there are caps on its size, on rules per profile and on profiles per workspace.
* **Where the regexes run: not in the engine.** A workspace's patterns are an attacker's regexes, and CPython's `re`
  cannot be interrupted. The walk runs in the regex worker (`op: "scan"`), the process `search_code(regex=true)` already
  uses, under `guarded_argv`, in its own session, killed with `SIGKILL` at a hard limit. **No new spawn site**:
  `GUARDED_SPAWN_SITES` is unchanged. The hard limit now follows the request's own budget plus the same margin a search
  has always had; a search's limit is unchanged. `scan.py` imports no process, network or loop module and writes
  nothing, and a test reads the source to say so.
* **The prompt** gains one clause (`CEILING` 3100 → 3200): its hits are candidates, so read one before calling it real.

## 4. Measured, and what it shows

* On this repository (311 files scanned, 317 test files skipped, 56 rules): **4.3 s**; rule matching is about 3 s of
  that and no single rule is more than about 0.5 s. A larger repository meets the 15 s budget, and stops saying so.
* The `injection` profile on this repository reports six candidates, all `py-sql-format`, all in five files
  (`app.py`, `db.py`, `executor_core.py`, `services.py`, `stats_history.py`): SQL with an engine-owned identifier or
  placeholder list interpolated. They are the five files `pyproject.toml` already exempts from ruff's `S608` after the
  project's own triage. An independent rule landing on the same sites is the nearest thing to a check that the rule is
  pointed at the right thing; it is not proof it finds what a person would not.
* Scanning `all` on the repository also reports the profiles' own examples (a private-key header, an AWS example key),
  because the scanner scans its own rule files. Any repository that keeps rule or fixture text outside a test
  directory will see the same: a hit is a line that matches, and the model is told to read it.
* **28 deliberate breaks** of the scanner's safeguards (newlines blanked, strings not lexed, test paths ignored,
  symlinks followed, the per-rule cap or the sort removed, a workspace's wording unclipped or unlabelled, the worker's
  limit reverted) were tried against the tests, and each was caught. Two defects of mine turned up on the way, before
  anyone else could find them: a built-in skill that named `todo`, a step tool a plain turn is not offered (a test of
  the engine's own note on the skill caught it); and, while listing the breaks, a bound test that was a tautology on
  constants, now a test of what the worker is actually given.

## 5. What this does not claim

* **A hit is a line that matches.** A regex cannot see where a value came from, so `py-sql-format` flags SQL built from
  a constant, and a clean scan does not mean clean code. The report says so in its first sentence and
  `security-review` says it in its last. "No rule matched" is not a bill of health.
* **No data flow, no cross-file view.** A rule sees one file's text. It will not find a taint that crosses a call, and
  it matches a few lines at most.
* **The masking is a heuristic lexer, not a parser.** Template literals, heredocs, Rust raw strings and lifetimes,
  Java text blocks and regex literals are approximate. A language it has no table for (SQL, CSS, Markdown, JSON, ...) is
  scanned as written, and the coverage line names those by count.
* **The test-file rule is a guess about names.** A real source file in a `fixtures/` or `spec/` directory is skipped
  until `include_tests` is set, and the skip is counted, not hidden.
* **A scan hard-killed at its limit cannot say which rule hung**, so it says the scan was stopped and that a rule in a
  workspace profile may be too expensive. The built-in patterns are held to adversarial lines at the length a scan reads
  (`tests/test_scan.py`); a workspace's are held only by the kill.
* **Coverage is what the rules cover.** Fifty-six rules in six profiles, in the languages named, is a start. They are not
  a substitute for a dependency audit, a dynamic test or a person's review, and the skill says to say so.
* **Speed.** Plain Python over every applicable rule, with no literal prefilter. If a repository needs it, a per-rule
  required literal is the next step, and it must be checked against the rule's own examples so it cannot silently
  hide a hit.

## 6. If a rejected or deferred row is reopened

* **A results pane** needs a read-only route in `docs/04` (boot token, like every route), a pane that lists hits by
  severity and opens one at its line through the editor surface, and CSV if a person wants it. The engine side is
  `scan.run_scan` and the data is already structured; only `format_scan` turns it to text. Do it after the Tailwind
  migration so it is written once.
* **The librarian** (`recon`) could request scans, which changes the librarian's JSON contract (`docs/04` §4.0).
* **More languages and rules** are data: a profile file and its examples, checked by the same test.
