---
name: adopt-a-change
description: Take over an existing branch or thread, preserve the original credit, and finish its goal in this workspace without fetching, rebasing or pushing.
moves: git_history, read_file, search_code, plan, write, verify, review, summarize
---

# Adopt a change

Finish work that already exists, without pretending the engine can move another
person's branch.

## The loop

1. **Read the source in full.** The branch's diff, commits, description, review
   comments and any later correction. `git_history` and `read_file`; treat
   handoff notes as hints, not facts.
2. **Find the real state.** The current `HEAD`, uncommitted changes and open
   findings. Say what is actually here in this workspace.
3. **Write the original goal in one sentence.** If it is unclear, ask before
   changing anything.
4. **Keep the credit.** Preserve the original authors and co-author trailers.
   Name the contributors and link the original change in the PR text. Never
   guess an identity or an email.
5. **Work in this checkout.** The engine cannot fetch another branch, create a
   worktree, rebase or force-push. If the branch is not here, the person must
   bring it; say so instead of describing a fetch that did not happen.
6. **Finish the original goal.** `plan`, `write` and `verify`. Simplify where
   you can, but keep every intended behaviour. Do not defend old code because
   it exists, and do not add unrelated features.
7. **Verify on your own.** Reproduce the original problem, then show it fixed
   with the real command. `review` the final diff.
8. **Report** what was inherited, what changed, how it was verified, how credit
   was kept, and what is still open.

## Stop when

- The goal is reached, verified and reported.
- The goal is unclear or needs a product decision. Ask.
- Verification fails three times on the same issue. Report the blocker; do not
  keep editing around it.
