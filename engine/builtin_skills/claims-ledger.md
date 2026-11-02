---
name: claims-ledger
description: Trace every number, comparison and first-best-only in a README, release note, changelog or post to a source before it ships; soften, label or strike what has none.
moves: read_file, search_code, git_history, recall, todo, plan, write
---

# Claims ledger

Copy that sounds sure and cannot be traced is the cheapest thing to ship and
the most expensive to retract. A slogan asserts nothing checkable. A figure, a
comparison, a "works with", a date, a cause and "first, best, only" do.

## Find the claims

`read_file` the text and `search_code` it for digits, "faster", "only", "first",
"all", "never", "supports", "works with" and "free". List each one verbatim.
Also search for template residue: "lorem", "Acme", "Your headline", "TODO",
and a name or number left over from another project.

## One row each, in `todo`

- **claim** — the exact words;
- **source** — `path:line`, a commit, a command with its output and date, or a
  dated page. "Seems right" is not a source;
- **strength** — what the source supports: exactly this, something weaker, or
  nothing;
- **action** — keep, soften, label, or strike.

## Rules

- **Words no stronger than the source.** A rate measured on one machine is not
  "faster for everyone". A range stays a range.
- **Timing is not cause.** "After the release, sign-ups rose" is a sequence.
  "The release brought sign-ups" is a cause, and needs more.
- **An unfinished period says so.** "October to date", not "October".
- **What is true today comes from the repository,** not from the request. When
  the text names a feature, find it in code, tests or docs. Write three lines:
  what the product does today, what the text says, the gap. If it is not there,
  ask one question: is it shipped, on the roadmap, or only an idea? Mark a
  roadmap item as one.
- **A number you produced has a command and a date.** A number someone else
  published, and you did not run, is attributed to them or left out. Benchmarks
  follow `docs/08-benchmarks.md`: say what the run measured and nothing more.
- **Examples are labelled.** Sample output, a mock screen or illustrative data
  says so. Never paste output that was not produced.
- **"One command", "zero config", "one prompt"** only if it is literally so.
  Count the steps.
- **Comparisons with other products** need a dated source and fair settings, or
  they come out.
- **Borrowed words** are credited, or not borrowed. Third-party text is not
  copied into this repository.

## The loop

1. Collect the claims and write the rows.
2. Source each one. Run the command where a command is the source.
3. Edit through the plan and `write`: keep what is sourced, soften or label the
   rest, strike what cannot be defended.
4. Re-read the final text. Every claim in it traces to a row, and no sentence
   crept in that is not in the ledger.

## Report

A short table: the claims kept, softened, labelled and struck, and the one-line
reason for each change. Name anything left unchecked.

## Stop when

- Every claim in the changed text has a row whose source is at least as strong
  as its wording, or has been softened, labelled or struck.
- A source cannot be found and the person has been asked; do not guess.
- An empty ledger is not a pass. If you wrote no rows, say nothing was looked
  for, or why the text makes no claims.

## What this is not

It checks the text against this repository and what can be run here. It is not
a check of the world, and it does not publish anything.
