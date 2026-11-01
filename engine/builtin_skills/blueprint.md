---
name: blueprint
description: Read the surrounding modules, write two structurally different type-and-signature sketches, choose the smaller surface, and implement only after approval.
moves: recon, plan, design, read_file, search_code, write, verify
---

# Blueprint

Fix the shape of the code before writing the bodies, so the implementation
fills in a plan instead of inventing one as it goes.

## The loop

1. **Ground yourself.** `read_file` every module the new code touches and
   `search_code` for the callers of the names it will change. If you cannot
   explain how they fit together, use `explain-code` first.
2. **Write two structurally different sketches in the plan.** Each sketch has
   real types, signatures, module boundaries and stub bodies that raise "not
   implemented", plus a short note on what it rejects. For a rendered surface,
   `design` may lock the direction; the sketch still owns the code shape.
3. **Compare.** Prefer the smaller public surface that hides more inside, and
   the one where a change that looks right from a single file is also right for
   the repo. Say what you rejected and why.
4. **Make the sketch compile.** Once a plan is approved, `write` the stubs and
   `verify` with the project's type checker or compiler. Stubs that do not
   compile are not a blueprint.
5. **Wait for the person.** `plan` hands over the chosen sketch and its cost.
   Do not fill in bodies before approval.
6. **Implement one stub at a time.** `write` one body, `verify` the narrow
   check, then the next. If the sketch turns out wrong, stop and revise the
   sketch, not only the body.
7. **Delete leftover stubs, dead sketches and unused types.** Search for the
   stub marker and prove none remain.

## Stop when

- Every stub is implemented, the type checker passes and the tests pass, with
  the output shown.
- The sketch needed three revisions during implementation. Stop and redesign
  with the person.

## Rules

- Signatures change only through the revise path, never silently.
- Keep each sketch small enough to read in one pass.
- Evidence is compiler and test output, not "it should work".
- This recipe cannot write before the plan is approved, and it does not merge
  the sketches into one another.
