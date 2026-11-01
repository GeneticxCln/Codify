---
name: wide-audit
description: Split a wide but shallow audit into non-overlapping slices, check each with the engine's own search and read tools, and report evidence or a recorded gap.
moves: search_code, scan_code, read_file, recall, summarize
---

# Wide audit

This is for coverage, not depth: every file, endpoint, configuration key or
call site in a named set. The work is wide enough that one search is not enough
and deep enough that a list of filenames is not an answer.

## The loop

1. **State the done condition and the report.** What set is being covered, and
   what does a slice's result look like? A result is PASS, ISSUES or BLOCKED,
   with the command or file that showed it.
2. **Partition into non-overlapping slices.** By directory, module, route,
   configuration or symbol. Write the slices down in `plan` so a missing one is
   visible.
3. **Check one slice at a time.** Use `search_code` for names and call sites,
   `scan_code` for a rule that could be violated, `read_file` for the code the
   hit points at, and `recall` for what an earlier run already established.
   Do not count a hit as a finding until you have read it.
4. **Record each slice's result with evidence.** ISSUES lists every provable
   issue, not only the first. BLOCKED says what could not be read and why. A
   slice with no evidence is not PASS.
5. **Merge by root cause.** `summarize` the results into one report: the
   verdict, each slice with its evidence, the gaps, and duplicates collapsed.
   A missing slice is never counted as passing.

## Rules

- No parallel workers. The engine has one loop; slices run one after another.
- A search's count describes the files it opened. Say what it skipped rather
  than turning a bounded search into "none exist".
- A scanner hit is a candidate. Read the code before reporting it.
- Do not fix anything here. Hand real findings to `plan`; `write` is the only
  way a fix lands.
