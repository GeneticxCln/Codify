---
name: sharpen-the-ask
description: Restate a vague or high-stakes request, deepen it without widening it, and rewrite the prompt before any plan or code.
moves: ask_user, recon, read_file
---

# Sharpen the ask

A sharper request saves more work than a faster loop. Fix the ask before
spending a plan on the wrong one.

## The loop

1. **Restate what the person wants and the problem behind it**, in three
   sentences. If you cannot, `ask_user` one question rather than guessing.
2. **Deepen the task, do not widen it.** Answer briefly:
   - What would a top professional do here that the person did not ask for?
   - What raw material would they start from instead of a summary?
   - Which method or tool would they use?
   - What outside check, not you and not the person, would show the result is
     right? Name a command, test, metric or source.
3. **Rewrite the prompt to ask for exactly that.** Keep the scope no bigger.
   Leave product choices open and list them for the person.
4. **Check it against the code.** `read_file` the files the rewritten ask names.
   `recon` the workspace when the ask depends on what is already there. Do not
   plan from the rewrite alone.
5. **Present the restatement, the rewritten prompt and the open choices.** Ask
   the person to accept or edit it before `plan`.

## Stress test

When the plan is risky, a second reader can attack it. The engine cannot fan
out to several contexts; use `second-opinion` and a model the person provides,
or ask the person to challenge it. A finding without a run or a file and line
behind it is an opinion; leave it out.

## Rules

- Deeper, not bigger. Do not add features.
- Do not apply fixes from a stress test. Report them.
- Do not write code in this recipe. It ends at an accepted ask and a plan.
