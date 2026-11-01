---
name: merge-ready
description: Prepare a change until it is ready for a person to merge, with evidence for ready, waiting or blocked, and never merge it yourself.
moves: recon, plan, write, verify, review, summarize, read_ci
---

# Merge ready

Do the work of `ship-with-a-person` except the merge, then give the person one
honest verdict about the PR.

## The loop

1. **Make and prove the change.** `recon`, `plan`, `write`, `verify`. The proof
   covers the full diff, not only the happy path.
2. **Review it.** `review-loop`. Every blocker or should finding is fixed or
   answered with evidence.
3. **Prepare the PR text.** `prepare-a-pull-request`. State the proof and the
   unverified parts.
4. **The person pushes and opens the PR.** The engine has no push or create
   tool. After the push, `read_ci` reads the checks.
5. **Pick one verdict and support it:**
   - **Ready**: checks pass, no change request is open, the branch is current.
   - **Waiting on approval**: only a required human approval is missing.
   - **Blocked**: name what stops it, what was tried, and what is needed.
6. **Report the verdict, the head commit, the checks, the proof, the review
   outcome and the remaining risks.** Say the PR is open and unmerged.

## Rules

- Never merge, turn on auto-merge, join a merge queue or push to the default
  branch.
- Do not ask another agent to do any of those things.
- `unknown` is not green. If the checks cannot be read, the verdict is not
  Ready.
- A missing human approval is not a failure of the code; say which one it is.
