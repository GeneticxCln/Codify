---
name: review-loop
description: Review the scoped diff with the critic, judge each finding yourself, fix the smallest real ones, and re-review until both agree or four rounds are done.
moves: review, write, verify, git_history, read_file
---

# Review loop

## The loop

1. **Scope the change.** Use `git_history` to read the diff and name the goal
   and what is out of scope. Run the covering checks before asking for review;
   do not send code that fails them.
2. **Run `review` once.** The critic reads the diff and the code around it. A
   review that could not run is not approval.
3. **Judge each finding yourself.** Agree: make the smallest fix through
   `write`, then `verify`. Disagree: answer with evidence, a file and line or a
   command and its output. A nit or out-of-scope request gets one line and no
   change.
4. **Re-run the checks and `review`.** Send the new diff and the list of what
   was fixed and declined. Ask the critic to check only the open findings and
   the changed code.
5. **Report the rounds, the fixed findings, the declined findings with reasons,
   and the final verdict.** If a finding is still disputed, say both sides in
   two or three lines and let the person decide.

## What this is not

- It is not several models voting. The engine has one critic. For a genuinely
  separate reviewer, use `second-opinion` and be honest about which model
  answered.
- It is not a second writer. Only `write` changes a file, and only inside an
  approved plan.
- It does not push or merge. Those are the person's actions.

## Stop when

- The critic approves the current diff, no blocker or should is open, and you
  have no concern left.
- A round returns only nits.
- Four rounds are done, or one item stays disputed after two exchanges. Report
  the disagreement; it is not approval.
