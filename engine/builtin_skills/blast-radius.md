---
name: blast-radius
description: Find what a change could break beyond its own diff, then prove the one safety fact by running real code. Use before shipping a risky change or when asked what it could break.
moves: git_history, search_code, read_file, scan_code, verify, run_command
---

# Blast radius

A list of callers is the easy half. The part that ships bugs is the fact the
change is safe *because of*, and the places a text search does not reach.

## The loop

1. **Read the change, not the summary.** Use `git_history` (`diff`, `show` or
   `log`) to see the actual patch. Name the symbols it adds, changes or removes,
   and the behaviour it now has that the diff does not spell out.
2. **Name the one or two facts the change is safe because of.** "Only entries
   that have already expired are removed." "The new field is optional
   everywhere it is read." "This path is only reached after the lock is held."
   If one of those is false, the change is unsafe. Spend the effort there, not
   on another caller list.
3. **Look where a search stops.** `search_code` for callers and for the old
   spelling of a renamed thing. `read_file` the code at the version this
   workspace actually uses, including a vendored or generated file. Follow data
   that leaves the code: an API response, a database column, a file format,
   configuration, another language reading the same bytes. `scan_code` for a
   rule this change could trip.
4. **Grade each fact, and say the grade.**
   - **asserted** — you said so; worth nothing on its own;
   - **located** — a real `path:line`;
   - **traced** — you followed the failure path and showed it cannot be
     reached;
   - **ran** — a command against the real code that fails loudly if the fact is
     false;
   - **reproduced** — the running app.
5. **Push a cheap fact to *ran*.** After the plan is approved, `verify` is the
   move that runs the project's own check; `run_command` can run what the
   allowlist permits. Before approval it is read-only, so a proof that needs
   code to run waits, and you report the fact as unproven instead of describing
   a run you did not make. A write belongs to `write`; this skill changes
   nothing.
6. **Report** the facts, their grades, the exact command and its output for
   every *ran* fact, any breakage with a reproduction, and what stayed
   unproven. Hand fixes to the normal flow (`plan`), not to this skill.

## Stop when

- Every key fact is *ran* or *reproduced*, or you have said why it could not
  be, and the report is written.
- You found a real break. Report the reproduction and stop.

## Rules

- A convincing explanation is not proof. Show the output.
- Do not fix anything here. This skill finds the risk; `write` and `verify`
  handle the change through the approved plan.
- "Not found" is not "not reachable". Say which one you actually established.
- A proof level you did not earn is worse than no proof, because it reads like
  evidence.
