---
name: decision-log
description: Keep one evidence-backed row per decision in the conductor's notes, and re-read the goal row before each step so a long run does not drift.
moves: todo, recall, git_history, summarize
---

# Decision log

A long run forgets why it chose a path. A short log keeps the goal in view and
lets a reviewer trust the result.

## The row

Use `todo` for the log; do not create a file. Each row is one line with:

- **decision** — what was chosen or done;
- **why** — the reason in plain words;
- **evidence** — a pointer that can be opened or rerun: `path:line`, a commit,
  a command and its result;
- **result** — passing, reverted, open, or the next check.

The first row is the goal and its done condition.

## The loop

1. **Before each step**, re-read the goal row and the last few rows. Does the
   next action still serve the goal? If not, log the drift and correct course.
2. **After each real decision**, append one row. Use `git_history` for a commit
   or diff pointer, `read_file` for a line, and `recall` when an earlier run
   already established the fact.
3. **Log reverts and dropped work too**, with the reason. The failed rows are
   often the most useful part of the record.
4. **At the end**, add a summary row and point the person at it. `summarize`
   turns the log into the final account; the log itself stays short.

## Rules

- Every row needs evidence. "Looked fine" is not evidence.
- Append corrections; do not rewrite old rows.
- Write for a teammate: concrete and plain.
- If three rows in a row show no progress toward the goal, stop and report.
- A row is a note, not an approval. It cannot make `write` run or widen a menu.
