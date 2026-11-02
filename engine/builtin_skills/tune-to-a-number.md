---
name: tune-to-a-number
description: Improve a measured number - speed, size, pass rate, a judge's score - without fooling yourself: fix the winner rule first, check with something independent, log every round.
moves: recon, read_file, search_code, plan, write, verify, run_command, todo, recall
---

# Tune to a number

When success is a number, the number can be satisfied without the thing getting
better. A search that optimises a measure will find the measure's gaps first.
This recipe keeps the gain real.

## Before the first change

Write these in `todo` before you look at any candidate:

1. **The quantity and the baseline** — measured now, with the command and its
   output, not remembered.
2. **The selection rule** — how a winner is chosen. "Median of five runs", not
   "the best run I saw". Fixed before the results exist.
3. **An independent check** — something the number does not see: a different
   measurement of the same thing, a held-out input set, other seeds, the real
   workload, or a person looking. The tuned number alone is never the evidence.
4. **Guards** — what must not get worse: correctness tests, memory, tail
   latency, the neighbouring paths.
5. **Reference points** — the same measure applied to what you would call good
   already. If the tuned thing now scores above the real one, the measure is
   satisfied; that is not the same as better.

Paid or networked runs (a model-judged benchmark, a load test) cost something.
Say what a run costs and ask before the first. `docs/08-benchmarks.md` says what
a number from the project's own harness may claim.

## The loop

- **One structural change per round.** When the evidence shows a missing step or
  the wrong shape, change the structure. Sweeping parameters is for the last
  mile.
- **Read what the measure says in words** — where the time goes, which case
  fails, which band is off — and act on that, not on the total.
- **Re-measure on inputs the change did not see,** and report the middle and the
  spread, not the lucky best run. Open a held-out set once, at the end.
- **Log each round** in `todo`: the change, the number, the independent check,
  the guards.
- **Notice saturation.** When every automatic check passes, they have stopped
  telling you anything. Only the independent check can still tell, so move to
  it or stop.

## Report

Baseline to final with the spread; the selection rule; the independent check and
what it said; the guards; what was not measured. If it is true, one sentence on
how this number could be satisfied without a real gain.

## Stop when

- The target is met under the selection rule **and** the independent check
  agrees.
- Three rounds in a row move the number and leave the independent check where it
  was. The number is being gamed or is the wrong one; report it.
- A guard regresses past what the person accepted, or the next round costs more
  than they approved: stop and ask.

## What this is not

It is not `prove`, which settles one claim with one check, and it is not a
license to run an expensive harness unasked. Changes still go through the
approved plan and `write`.
