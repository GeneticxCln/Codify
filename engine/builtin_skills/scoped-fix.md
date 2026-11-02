---
name: scoped-fix
description: Fix exactly what was pointed at - write a fix card, change only that scope, and prove everything outside it is unchanged before saying it is done.
moves: read_file, search_code, git_history, plan, write, verify, run_command, todo, ask_user
---

# Scoped fix

A note, a review comment or a bug report points at a place. The usual failure is
not a wrong fix; it is a right fix with three other things moved. This recipe
makes the boundary part of the work and part of the proof.

## The fix card

Write it in `todo` before any edit:

- **Scope** — the place the note points at: `path:line`, a component, a route, a
  step, a commit range. The place is the scope.
- **Must not change** — what has to stay exactly as it is: neighbouring
  behaviour, a public signature, copy, formatting, a snapshot, a lockfile.
- **The failure now** — the observable, reproduced or quoted. If the note is a
  feeling ("it is slow", "it looks off"), turn it into the mechanism and the
  measure behind it first.
- **The target** — an observable that says it is fixed: a command and its
  output, a value, a state.
- **Shortcuts that would fake it** — the cheap changes that reach the target
  without fixing the cause: loosening the assertion, special-casing the input,
  a longer timeout, a skipped test, an updated snapshot, a swallowed error, a
  suppressed lint. Write them down so you can refuse them.

## Ambiguity

If a note has two readings, take the more sensible, do it, and say in the
report which reading you took so it can be reopened. Ask once, with `ask_user`,
only when the readings lead to different work and the wrong one is costly.

## Make the change

Change one cause at a time and keep it small. With several notes, take the ones
that block understanding or use first, structure before surface, one per step.
`write` still needs the approved plan.

## Prove the boundary

1. **The target**, with the exact command, before and after.
2. **Nothing outside the scope moved.** `git_history` the diff and list every
   changed path and hunk. Each one is inside the scope or named with the reason
   it had to change (an import, a type).
3. **Compare sources, not products.** A rebuilt bundle, a regenerated file, a
   reordered lockfile or a re-encoded asset differs for reasons that are not
   your edit. Diff before the lossy or generated step, and explain generated
   differences separately.
4. **The neighbours still pass.** Run the checks that cover the code next to the
   scope, and re-check the original failure and the seam where the change
   meets what it did not touch.

## Report

The card; the target before and after; the paths changed; the reading taken;
what was not checked.

## Stop when

- The target is observed and every change outside the scope is named and
  justified.
- Two attempts show the cause is outside the scope. Report it; widening the
  scope is the person's call, not a quiet extra.
- The only fix needs a change the card lists under must-not-change: stop and ask.

## What this is not

It is not `blast-radius`, which asks what else a change could break. It is the
promise that the change was only where it was pointed. It does not push or reply
to the note; a person does.
