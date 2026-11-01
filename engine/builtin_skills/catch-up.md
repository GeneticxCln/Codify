---
name: catch-up
description: Give a short, read-only catch-up on what changed since the person's last message, then list every decision still waiting on them.
moves: recall, recall_threads, git_history, read_ci, todo, summarize, read_file
---

# Catch up

Bring the person back up to speed in under a minute, and hand them every call
only they can make.

## The loop

1. **Keep the turn read-only.** Do not change code, start a job, push or post.
   This recipe reads and reports.
2. **Collect what happened** since the person's last message: the conductor's
   own work, completed steps, failed checks and changes in the workspace. Use
   `recall` and `recall_threads` for the thread's history, `git_history` for
   commits and diffs, `read_file` for the current state, and `read_ci` for the
   checks of a pushed commit.
3. **Check the state instead of trusting memory.** If a number in the summary
   does not match the source, fix the number and say you did.
4. **Write what changed in under 100 words.** Say what it means for the person:
   "the login bug is fixed and its test passes", not a list of hashes.
5. **List the decisions waiting on them.** A decision is a product choice, an
   approval, an irreversible action, or access the engine lacks. Include old
   ones still open. For each: what it changes, the evidence, the risk, your
   recommendation, and `unknown` for a missing fact.
6. **If nothing waits on them, say so plainly.** Do not invent a decision to
   fill the list.

## Rules

- Work the engine can still do itself is not a decision for the person.
- Name things the way the person would, not by internal labels.
- Short sentences, no jargon.
- Do not call a check passed when `read_ci` says `unknown`.
- `todo` may hold the next action, but this turn does not start it.
