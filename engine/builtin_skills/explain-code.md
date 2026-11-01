---
name: explain-code
description: Explain how a piece of code works and why it has its shape, in plain language, citing the files and lines and labelling inference.
moves: read_file, search_code, git_history, recall, summarize
---

# Explain code

Give one plain account of what a thing is, how it runs, and why it has its
current shape, so the reader can change it with confidence.

## The loop

1. **Work out what the reader needs.** Are they about to change it, review it,
   debug it, or are they new? Pick the three or four things they should walk
   away knowing. Skip what they already know.
2. **Size the job.** One function is one pass. A subsystem can be split into a
   few read-only slices with `search_code` and `read_file`; the engine runs them
   one after another, not in parallel.
3. **Trace the real path.** Read the code from entry point to effect. Note who
   owns each piece of state and where the boundaries are. Cite `path:line` for
   every claim about behaviour.
4. **Find why it is that way.** `git_history` for the commit, blame, PR text or
   issue near the code. `recall` what an earlier run established. Separate what
   the record says from what you infer, and label the inference. If no record
   exists, say the reason is unknown.
5. **Write the explanation.** Start with a plain definition, then tie it to this
   codebase. Walk through what happens when someone uses it. Then the reasons,
   then the edge cases. Use a small table or diagram when it helps.
6. **Offer to go deeper on one named part.** `summarize` only if the answer is
   long enough to need it.

## Rules

- Do not edit code. This recipe changes nothing.
- Explain ideas, not a list of functions.
- Never present a guess about intent as fact.
- If three rounds of searching find no reason, say it is unknown rather than
  inventing one.
