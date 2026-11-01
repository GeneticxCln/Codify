---
name: ship-with-a-person
description: Take a change to a proved, reviewed, merge-ready branch, then hand the push and the merge to the person; never merge or force-push.
moves: recon, plan, write, verify, review, summarize, read_ci
---

# Ship with a person

Own the work up to the edge of the irreversible actions. A person presses Push
& PR and a person merges.

## The loop

1. **Confirm the request grants the work, not the merge.** If the person asked
   to "ship it", say plainly that the engine can prepare the branch and the PR
   text but cannot push or merge.
2. **Make the smallest change that meets the goal.** `recon`, then `plan`. Fix
   the reported case, not every case that might exist.
3. **Prove the diff.** Reproduce the failure, apply the fix, run the same
   reproduction, then the project's wider checks. Paste the command and output.
4. **Review it.** Use `review-loop`. Re-prove anything the review changed.
5. **Prepare the PR text.** Use `prepare-a-pull-request`. Include how it was
   proved and what was not verified.
6. **Hand over the irreversible steps.** The person pushes, opens the PR and
   merges. The engine has no push, force-push, PR-create or merge tool.
7. **After the person pushes**, `read_ci` may read the checks and report them.
   A failure goes back through `plan` and `write`; the person pushes again.
8. **Report** the branch, the proof, the review verdict, the PR text, and every
   decision the person still has to make.

## What this refuses

- No merge, no auto-merge, no merge queue and no push to the default branch.
- No force-push. The person owns their branch history.
- No asking another agent to do any of those things.
- No claim that a check passed when `read_ci` says `unknown`.

## Stop when

- The change is proved and reviewed, the PR text is ready, and the person has
  been told exactly which buttons are theirs.
- The work cannot be proved, or the gate cannot pass. Report the blocker; do not
  paper over it with a merge.
