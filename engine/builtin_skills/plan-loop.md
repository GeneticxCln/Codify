---
name: plan-loop
description: Read the files first, write a numbered plan with one check per step, critique it once, then execute one verified step at a time.
moves: recon, plan, review, write, verify, todo
---

# Plan loop

## The loop

1. **Restate the goal and read the files.** List the files and commands the
   work will touch, then `read_file` them. `recon` gathers the evidence; do not
   plan from memory.
2. **Write a numbered plan.** Each step has one action and one acceptance check
   a command or a direct observation can confirm. Keep each step small enough
   for one diff. `plan` writes the steps and waits for the person.
3. **Critique it once.** Run `review` on the plan as a fresh pass: which setup,
   migration, config, test or cleanup step is missing; which assumption was not
   checked; is there a shorter path? Do not run a third critique.
4. **Revise the plan before executing it.** Check each risky assumption now
   with `read_file` or a read-only `run_command`; do not carry it as a guess.
5. **Execute step one only.** Use `write` for the change and `verify` for its
   check. Read the real output. If the step reveals the plan is wrong, stop and
   revise the remaining steps instead of improvising around it.
6. **Record the evidence.** Put the step, its check and the result in `todo`,
   then take the next step.

## What this refuses

- No step without a check. "Looks right" is not a check.
- Do not batch steps to save calls. One step per diff is what makes the check
  mean something.
- Do not write a `PLAN.md` or a `PROGRESS.md`. A skill cannot write a file; the
  `write` move is the only writer and it needs an approved plan.
- Stop after two critique rounds. A third almost never finds a new problem.

## Stop when

- Every step is done and its check passed, and the whole goal holds when checked
  end to end.
- A step fails its check twice in a row. Load `unstick` instead of adding a
  third variation.
