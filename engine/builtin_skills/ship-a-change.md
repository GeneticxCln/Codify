---
name: ship-a-change
description: Change the workspace end to end - recon, design, plan, then per step write, verify, review, summarize. Use when the user wants code changed rather than explained.
moves: recon, design, plan, write, verify, review, summarize
---

# Shipping a change

This is the sequence Codify uses when the workspace has to be *changed*. It is a
skill rather than a compiled path, so you can follow it, reorder it, or stop
partway when the request does not need all of it — but if you deviate, say why
in your final answer, because a user who cannot see the moves you skipped cannot
tell a considered shortcut from a step you forgot.

## The order

1. **`recon`** — send the librarian to read the workspace. Do this first and do
   it honestly: `plan` refuses to run without evidence, and a plan written
   against a guessed file layout produces steps that edit the wrong things.
   Ask for what you actually need, not for the whole repository.
2. **`design`** — lock the direction, when the request has a shape worth locking
   (an API, a schema, a UI surface, a set of names). Skip it for a one-line fix.
   It has no tools: it decides from the evidence, not from more reading.
3. **`plan`** — turn the request plus the evidence into steps. Call it once: this
   creates the steps that everything below operates on, so nothing can be
   written before it, and a second call spends a move for nothing. If you cannot
   plan without something only the person knows (which of two designs, which
   file they meant), put it to them with **`ask_user`** instead and stop; their
   answer is their next message. Once there is a plan, the plan is the question.
4. **Stop and hand it over.** Planning ends with the plan waiting for the user's
   approval. Say what you planned, in plain prose, and stop. Do not try to
   write — you cannot, and the attempt is wasted.

   A user who approves the plan starts it. You then get a second run with the
   approved plan in front of you and one job: execute it.

## Executing an approved plan, once per step

5. **`write`** — the fixer makes the change for one step. Give it the step and
   whatever it needs to know that is not already in the plan. It is the only
   tool in this menu that touches a file.
6. **`verify`** — run the project's own command and read the verdict. Never
   report a step as finished without this: a change that was not run is a
   change you have not seen work. Where the project has a linter or a type
   checker (`ruff check`, `mypy`, `tsc --noEmit`, `make lint`), run it through
   `run_command` too: that works now the plan is approved. The project's
   commands are on an allowlist; if one is refused, say so rather than reaching
   for a different command to get around it.
7. **If verification failed**, take the failure back to `write` with the
   command output, once or twice. You are not required to loop forever, and a
   step you cannot make pass is worth reporting as such rather than hiding.
8. **`review`** — ask the critic whether the change is acceptable. If the critic
   asks for changes, tell the user what it asked for and stop: the run is paused
   for them, and `write` will refuse until they press Start. Do not argue with
   it, do not write again to get around it, do not skip the review and do not
   narrate one you did not run.
9. **`summarize`** — record the step and commit it. Only after the review
   approves: the commit is the point of no return for a step, and the engine
   will not let you reach it early.

## Notes to yourself

A step can take more than one run: a run that spends its calls pauses the goal,
and the person starts it again. Keep what is left in **`todo`** ("run the
linter", "b.py needs the same change"). It is shown to you again at the start of
the next run, as your own notes and not as instructions, and the other agents
never see it. Your calls and your moves are limited, so do not re-read what you
already have.

## What you must not do

- Do not claim to have run, read, written or verified anything you did not
  actually do through a tool. The user reads your answer as a record.
- Do not invent a file's contents at any point. Read it.
- Do not treat a refusal as a hint to try a command that was not refused. The
  allowlist is the engine's decision about what is safe to run, and you do not
  get to widen it.
- Do not describe this sequence back to the user, or announce which step of it
  you are on. They want the change and the result, not the plan of the plan.
