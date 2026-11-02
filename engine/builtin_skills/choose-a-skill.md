---
name: choose-a-skill
description: Pick the one native recipe that fits the task, say why, and load it instead of stacking several overlapping recipes.
---

# Choose a skill

The menu is short enough to choose from directly. Use this only when the task
has more than one plausible recipe.

| If the task is... | Load |
|---|---|
| vague, high stakes or the ask itself is wrong | `sharpen-the-ask`, then `plan-loop` |
| more than one file or step | `plan-loop` |
| a bug with a cheap local check | `test-first`, then `prove` |
| a risky change outside its own diff | `blast-radius` |
| the same failure after two fixes | `unstick` |
| a long run that is losing facts | `resume-long-run`, with `decision-log` |
| a review before a person merges | `review-loop`, or `second-opinion` when a separate model is available |
| a wide but shallow audit | `wide-audit` |
| a new or templated screen | `design-review` |
| a change to prepare for a person to push | `prepare-a-pull-request`, then `watch-ci` |
| a change that should be merge-ready | `ship-with-a-person` or `merge-ready` |
| someone else's branch or thread | `adopt-a-change` |
| an old change that may no longer fit | `revive-a-change` |
| an explanation of how code works | `explain-code` |
| a return after a long run | `catch-up` |
| a repeated agent mistake | `encode-a-lesson` |
| prose that sounds generated | `plain-prose` |
| a thread that needs a fresh context | `context-transfer` |
| a security review | `security-review` |
| a compiled file or generated bundle | `inspect-an-artifact` |
| wasted model calls | `usage-audit` |
| the shape of a change before its code | `blueprint` |
| one attempt may land on the wrong shape | `compare-approaches` |
| a behaviour, screen or output that must match a reference | `match-a-reference` |
| motion in an interface | `ui-motion`, then `design-review` |
| a note or comment that points at one place | `scoped-fix` |
| a README, release note or post with numbers in it | `claims-ledger`, then `plain-prose` |
| a measured number to push up or down | `tune-to-a-number` |
| a short film, explainer or animated demo as code | `film-as-code` |

## Rules

- Recommend one recipe. Name a second only when the task clearly has two
  phases, such as plan then prove.
- If no recipe fits, use `working-style` and answer directly.
- Loading a recipe is a hint about order, not a grant. It cannot add a tool or
  approve a write.
