---
name: resume-long-run
description: Keep a long run from drifting by writing proved facts into the conductor's notes and packaging a fresh handoff instead of keeping state in the chat.
moves: todo, recall, recall_threads, summarize, read_file
---

# Resume a long run

A long transcript forgets its own reasons. The fix is a small, current state
that a fresh context can continue from.

## Notice the drift

Use this when the model repeats an earlier answer, forgets a fact it already
established, retries a failed fix, rereads the same file, or the thread has
become long enough that the early decisions are no longer reliable.

## The loop

1. **Finish or revert the current step** so the workspace is in a known state.
   Do not leave a half-edit behind for the next context to inherit.
2. **Collect the facts that are proved.** Use `recall` and `recall_threads` for
   what earlier runs established, and `read_file` to confirm the current state
   rather than trusting memory. A fact with no evidence is a question, not a
   fact.
3. **Write the state into `todo`.** Keep four parts: the goal and its acceptance
   check, what is done with the command that proved it, the exact next action,
   and the open questions. `todo` is the conductor's own note and carries into
   its next run; it is not a file and not an instruction to the person.
4. **Package a handoff when the thread is the problem.** Load
   `context-transfer` and follow it. It produces one pasteable block for a new
   thread, with the gate's real result, the uncommitted work and the keys it
   must not carry.
5. **Start the fresh context from the state, not the transcript.** Give it the
   goal, the last proved step, the next action and the acceptance check. Make it
   confirm the baseline before editing.
6. **Keep the note short.** A summary that takes longer to read than the work
   it describes is another transcript.

## What this refuses

- No `PROGRESS.md`. A skill cannot write a file; the only writer is `write`
  behind an approved plan. `todo`, `recall`, `recall_threads` and
  `context-transfer` are the mechanisms this engine has.
- Do not put secrets, keys or tokens in the notes.
- Do not call an unverified step done. Move it to the next action instead.

## Stop when

- The goal's acceptance check passes and the note has no open work.
- Three resets on one next action make no progress. Stop and report the state,
  the evidence and what you would try next.
