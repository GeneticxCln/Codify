---
name: working-style
description: Restate the goal, read before planning, work in small verified steps, and show the command that proved each claim.
moves: recon, plan, write, verify, read_file, search_code, run_command, todo
---

# Working style

Use this at the start of any turn whose request is more than a single fact.

## The loop

1. **Restate the goal in one sentence.** Say what the person wants and what
   would show it is done. If the request is ambiguous in a way that changes the
   work, use `ask_user` before planning.
2. **Read before you plan.** Use `read_file` and `search_code` on the files the
   change actually touches. Do not plan from the request alone. `recon` gathers
   the evidence the plan will rest on.
3. **Make the smallest plan that reaches the goal.** Each step gets one action
   and one check a command or a direct observation can confirm. `plan` writes
   the steps; do not call it twice for the same job.
4. **Do one step at a time.** `write` changes one thing, then `verify` runs the
   real check. Read the output instead of assuming it. `run_command` is
   read-only until a plan is approved; do not describe a command you did not
   run.
5. **Keep the conductor's notes current.** `todo` holds the next action and
   what is already proved. It is your note, not an order to the person.
6. **Stop when the goal is met.** Report what works and the command that showed
   it, what was not verified and why, and what only the person can do.

## Stop when

- The goal is met and the output of the last proving command has been shown.
- A step needs an approval that has not been given: stop at that step.
- Five iterations have not passed the check: report the last error and load `unstick`.

## What this refuses

- A plan without a check is a wish. Add the check or leave the step out.
- A passing test you did not run is not evidence.
- A step that needs approval waits for approval. The skill cannot approve it.
- Do not widen the job with features the person did not ask for.
