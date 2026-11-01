---
name: second-opinion
description: Get one genuinely independent review when the native critic is not enough, using a second model the person provides rather than inventing a jury.
moves: review, ask_user, read_file, git_history
---

# Second opinion

The native engine has one critic. That is enough for most changes. This recipe
is for the small set where a second, independent reading is worth the cost.

## The loop

1. **Run the native review first.** Use `review-loop`. Do not ask for another
   model before the critic has seen the diff and the checks pass.
2. **Say why one critic is not enough.** Examples: the change touches security,
   a migration, or a public contract; the critic and the author share an
   assumption; the person asked for a second opinion by name.
3. **Ask the person for the second reviewer.** Use `ask_user` to ask which
   model or service they want, or whether they will provide the review. The
   engine stores no second provider and cannot fan out; it must not pretend it
   did.
4. **Give the reviewer one brief.** `git_history` the diff; `read_file` the code
   around it. Include the goal, the out-of-scope list, the checks and their
   output. Tell the reviewer to review only and change nothing.
5. **Judge the findings yourself.** Fix what you agree with through the normal
   plan and `write`; answer the rest with evidence. A timeout or an error is not
   approval.
6. **Report which model answered**, what it found, what was fixed, and what was
   declined. If there was no second reviewer, say the review was one critic.

## Rules

- Never invent a juror, a score or a verdict.
- Do not run several reviewers in parallel; the engine has one loop and one
  accounting.
- A second opinion is advice. The person still decides and still merges.
- The reviewer is read-only: it does not edit, push, comment or start agents.
