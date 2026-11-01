---
name: encode-a-lesson
description: Turn a mistake that happened twice into a type, lint, test or structural check, prove the check fails on the old mistake, and leave a rule only when nothing can enforce it.
moves: recon, plan, write, verify, run_command, read_file, search_code, recall
---

# Encode a lesson

An instruction a model can skip is a wish. Make the repeated mistake fail
loudly instead.

## When this is for

The same mistake has happened twice: the same wrong edit, the same broken
assumption, the same review finding. A one-off mistake is not this. A
correction the person gave once is not this either.

## The loop

1. **Gather the evidence.** `recall` the earlier runs; `read_file` the code,
   tests and review comments; `git_history` the commits or reverts that show
   the mistake. Group the cases. A class counts once it has happened twice.
2. **Choose the highest level that works:**
   1. **Structure** — one owner for the state, one supported way to do the
      task, old patterns deleted.
   2. **Types** — make the bad state impossible to write.
   3. **Lint or CI** — the error names the right file, type or function. If the
      bad pattern is already common, fail only on new additions.
   4. **Test** — checks behaviour, not internals.
   5. **Docs** — only for a judgment call nothing can check. Add a short
      example of the failure.
3. **Plan the fix.** `plan` the smallest change that adds the enforcement, then
   `write` it. Do not add prose when a structural fix is possible.
4. **Prove the check.** Re-introduce the old mistake, run the check, and show
   it fails. Remove the mistake, run it again, and show it passes. A check that
   has never failed is not known to work.
5. **Record the rule and its enforcement** in the project's own instructions,
   one line each. If a rule already existed but nothing enforced it, treat it
   as a repeat and enforce it now.
6. **Report** each class with its evidence, the level chosen, and why a higher
   level was not possible.

## Rules

- Do not weaken an existing test or lint rule to make room for the new one.
- `write` still needs an approved plan; this recipe cannot edit around it.
- A rule added only as prose is the last resort, and the report says so.
- Stop after the top three classes this session. Note the rest for later.
