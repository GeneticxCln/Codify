---
name: compare-approaches
description: When one attempt could land on the wrong shape, sketch two or three approaches in the plan, compare them on stated criteria, and implement only the chosen one.
moves: design, plan, read_file, search_code, ask_user
---

# Compare approaches

One attempt is a sample, not an answer. This recipe buys a better shape before
any code is written, without pretending the engine can run attempts in parallel.

## The loop

1. **Frame the task once.** Write the prompt every approach would answer and a
   short rubric of three to six checkable criteria. The rubric is for you; do
   not let an approach grade itself.
2. **Write two or three sketches in the plan.** Each sketch names the types,
   the module boundary and the public surface, and says what it rejects. Use
   `read_file` and `search_code` to ground them in the current code. For a
   rendered surface, `design` may lock one direction; do not ask it for several.
3. **Compare on structure, not polish.** Prefer the smaller public surface that
   hides more inside, and the one where a change that looks right from one file
   is also right for the repo. Note what each sketch makes hard.
4. **Pick one and say why.** If the choice is a product decision, use
   `ask_user`; do not choose for the person.
5. **Put the chosen shape in `plan`.** The person approves the plan before any
   `write`. Then implement one piece at a time and verify each piece.

## What this does not do

- It does not run attempts in parallel or keep several worktrees. The engine has
  one writer and one workspace.
- It does not graft parts of several attempts into one. Pick a base and, if a
  rejected sketch has a genuinely better detail, make it a normal plan step.
- It does not write before approval. Sketches are prose in the plan.

## Stop when

- One approach is chosen, the plan names why, and the person has approved it.
- No approach fits after one clarified pass. Report the criteria and what each
  sketch failed; do not start a fourth.
