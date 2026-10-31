---
name: unstick
description: Break a repeating failure - name the assumption two fixes shared, probe it once, then fix the cause or change mechanism. Load when the same error survives two attempts.
moves: recon, write, verify
---

# Unsticking a repeating failure

Two fixes that failed the same way are one wrong belief, not two bad edits. The
work is to find the belief and test it, because a third variation of the same
edit is a third sample of the same mistake.

## When this is for

Use it when the same error — the same message, the same failed assertion, the
same wrong behaviour — comes back after two attempts. Two *different* errors are
ordinary progress and are not this. One failed attempt is not this either.

## The loop

1. **Stop editing.** Do not try a third variation of the fix, not even a small
   one. A variation is the same assumption wearing a different diff.
2. **Write the attempts down**, one line each: what you changed, and the exact
   error it produced. Keep the first error, not the last line: the first one is
   the cause, and everything after it is usually fallout from it.
3. **Name the shared assumption** in one sentence. What did both attempts
   believe that they never checked? "This file is the one that runs", "the
   config is read from here", "the function receives a string", "the cache is
   cleared", "the test is the thing that is wrong" — pick the one both attempts
   depend on.
4. **Probe it once**, with the cheapest call that answers yes or no:
   `read_file` the real call site rather than the file you assumed;
   `search_code` for where a value is actually read, imported or defined;
   `recall` what an earlier run in this workspace already established; run
   `run_command` on a narrow, allowlisted check once a plan is approved. A probe
   is an observation, not an argument. Do not reason about what the command
   would print — run it and read the output.
5. **Read the probe literally.** If the assumption is false, that is the bug:
   fix what the output shows, not what you expected it to show.
6. **If the assumption holds**, the fix is in the wrong place. List two other
   mechanisms — a different layer, a different call path, a different tool the
   engine already offers — and say in one line why each might work. Pick one
   and change the mechanism, not the edit.
7. **Make one attempt at the new mechanism** through `write`, then `verify` for
   real. Do not report a step as fixed without the command and its output.

## What a probe cannot be

- It cannot be a command the allowlist refuses. A refusal is the engine's
  answer, and reaching for a different command to get around it is the thing
  `ship-a-change` already forbids.
- It cannot be a write. Before a plan exists, `recon` is read-only and `write`
  refuses; a probe that needs the workspace changed waits for approval.
- It cannot be a test you weakened. If the probe says the test is wrong, say so
  and let the person decide; do not edit the assertion to make it agree.

## Stop when

- The check passes and you have its output.
- Two rounds of this have failed. Stop and report: the attempts, the assumption
  you tested, the probe's exact output, and what you would try next. That report
  is the useful result — it is what lets the person choose, and it is more
  honest than a third guess.

## What to tell the person

The false assumption, in one sentence, and the output that showed it. Not the
story of the two attempts: the fact they were both built on.
