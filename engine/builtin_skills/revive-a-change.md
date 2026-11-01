---
name: revive-a-change
description: Judge whether an old change still fits the current code, then recommend revive, rework or close; rebasing, force-pushing and closing stay with the person.
moves: git_history, read_file, search_code, read_ci, summarize
---

# Revive a change

A clean rebase does not prove an old change still belongs. Update the idea, not
only the branch.

## The loop

1. **Read the change in full.** Its goal, base, head, author, checks, review and
   diff. `git_history` what landed on the base since then; `read_ci` the checks
   for the commit if there is one.
2. **Write the goal in one sentence.** If the change cannot state it, that is a
   finding.
3. **Ask the questions that decide whether it still fits**, each with evidence:
   - Has the problem already been solved by something on the base?
   - Did the APIs, types, schemas or structure it relies on change?
   - Was the code it edits moved, deleted or simplified?
   - Would landing it bring back old behaviour or create two ways to do one
     thing?
   - Is the benefit still worth the upkeep?
4. **Pick one verdict:**
   - **REVIVE**: the goal and shape still fit. Rebase and re-prove.
   - **REWORK**: the goal fits, the shape does not. Describe the smallest
     rewrite and stop unless the person asked for the rework.
   - **CLOSE**: superseded or no longer worth it. Cite the evidence. Do not
     close it yourself.
5. **Hand the git actions over.** The engine cannot fetch, rebase,
   force-push or close. The person does those; the engine can review the result
   in this workspace and re-run the checks.
6. **Report the verdict first**, then the base changes, the conflicts, what was
   proved and the remaining decision.

## Rules

- Treat a dirty worktree as the person's. Never discard it.
- Never merge, close or change the base branch.
- No force-push, with or without a lease.
- Separate facts you checked from your judgment. "No conflict" is not "current".
