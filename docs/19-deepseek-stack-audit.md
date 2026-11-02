# Codify — The deepseek-stack audit: 29 skills, and the 24 native recipes built from them

[`aicodeking/deepseek-stack`](https://github.com/aicodeking/deepseek-stack) is a collection of **29 `SKILL.md`**
files for models that are cheap and literal: it assumes the model may skip the plan, claim a fix it never ran,
retry a broken idea, or drift on a long run, and gives each failure a short loop with a stop condition and a
demand for evidence. It is not a harness and it ships no engine. It is prose, plus one `gh` watcher script.

That is worth auditing here because the failure list is this engine's own. Codify already has a conductor that
chooses moves, skills that are data, an approval gate before a write, a verifier that runs the project's command,
and a critic that can refuse. This audit first took three recipes; after reading all 29, it adds **24 native
recipes**, written here in this project's own words and using only the tools and moves the engine already has.

Nothing was copied. deepseek-stack is MIT, so copying would be permitted, and it is not done: `docs/08` and
`CONTRIBUTING.md` say no third-party source is committed to this repository, and `docs/11`–`17` set the precedent
of taking the idea and leaving the text.

## 1. What was read, and what was not

- **Read:** the repository's `README.md` in full, the tree at the pin, and all 29 `SKILL.md` files in full: the
  three Core, nine Loops, six Build, seven Ship and four Understand skills.
- **The pin:** `d4f1ebe72e8314c3a0c6135bb82f623c650f7218`, the initial release (2026-10-28). Re-checked against
  `main` on 2026-11-01: GitHub's compare API reports `main` identical to the pin, with no commits, changed files or
  new skills after it.
- **Not read:** the body of `ds-watch-pr/scripts/watch_pr.py` — only its documented command line and exit meanings.
  Nothing was installed, run or benchmarked, and no upstream number is repeated here.
- **Not a source of code:** no file from the repository is in this tree, and the native recipes contain no
  sentence from it.

## 2. What it is, in this project's terms

deepseek-stack is a **prompt library, not a capability**. Its skills cannot add a tool, widen an allowlist, run a
command or save a file; they can only tell a model which existing action to take next. That is exactly what a Codify
skill is (`engine/skills.py`), which makes the comparison direct: the question is never "can this be installed",
only "does this ordering say something the conductor's menu and prompts do not".

Its strongest idea is not a skill at all. The README says thinking at maximum effort for planning and review
matters more than any skill. That remains a **setting** (`AgentConfig.thinking_effort`), not a recipe.

## 3. Already native before the 24

Five source skills already had a native counterpart:

| Source | Native |
|---|---|
| `ds-blast-radius` | `blast-radius` |
| `ds-verify-loop` | `prove` |
| `ds-design` and `ds-design-audit` | `design-review` (one native review for both) |
| `ds-unstick` | `unstick` (written independently before the audit) |

Those, plus the 24 below, account for all 29.

## 4. The 24 native recipes

| Source | Native built-in | What it does |
|---|---|---|
| `ds-mode` | `working-style` | Restate, read, plan small steps, prove each claim. |
| `ds-principles` | `engineering-principles` | Twenty-one rules of thumb, each with its trigger. |
| `ds-help` | `choose-a-skill` | Routes a task to one existing recipe. |
| `ds-plan-loop` | `plan-loop` | One checked step at a time, one critique, then execute. |
| `ds-context-reset` | `resume-long-run` | Keeps proved state in `todo` and packages a fresh handoff. |
| `ds-ledger` | `decision-log` | One evidence-backed decision row in `todo`, re-read before each step. |
| `ds-review-loop` | `review-loop` | One critic, judged findings, fixes, at most four rounds. |
| `ds-jury` | `second-opinion` | Asks the person for a genuinely separate reviewer; invents no jury. |
| `ds-best-of-n` | `compare-approaches` | Two or three sketches in the plan, compared before one is built. |
| `ds-swarm` | `wide-audit` | Non-overlapping slices, checked one at a time, with gaps recorded. |
| `ds-sharpen` | `sharpen-the-ask` | Deepens and rewrites the request before planning. |
| `ds-blueprint` | `blueprint` | Types and signatures first, two sketches, then one implementation. |
| `ds-tdd` | `test-first` | A failing test first, then the smallest fix, both shown. |
| `ds-pr` | `prepare-a-pull-request` | Drafts the title and body; a person pushes and opens the PR. |
| `ds-watch-pr` | `watch-ci` | Reads the pushed commit's checks with `read_ci`; no `gh`. |
| `ds-ship` | `ship-with-a-person` | Proves and reviews the branch, then hands push and merge over. |
| `ds-ready` | `merge-ready` | Prepares an open PR and gives a ready, waiting or blocked verdict. |
| `ds-adopt` | `adopt-a-change` | Finishes an existing branch in this checkout and keeps credit. |
| `ds-revive` | `revive-a-change` | Judges revive, rework or close; rebase and force-push stay with the person. |
| `ds-pr-comments` | `review-replies` | Drafts an attributed reply; a person posts and resolves it. |
| `ds-explain` | `explain-code` | Traces behaviour, cites lines, separates record from inference. |
| `ds-catchup` | `catch-up` | A read-only summary and the decisions waiting on the person. |
| `ds-lessons` | `encode-a-lesson` | Turns a mistake that happened twice into a type, lint or test. |
| `ds-unslop` | `plain-prose` | Removes AI tells from prose and code comments. |

All 24 are data. Their `moves:` headers name only tools that already exist; loading one adds no move, widens no
allowlist and reaches no write gate.

## 5. What the native versions still refuse

- **Unattended merge and force-push.** `ship-with-a-person`, `merge-ready`, `adopt-a-change` and
  `revive-a-change` prepare and report; they never merge, push or force-push. A person presses Push & PR and a
  person merges. Invariant 9's one-writer rule and the routes' boot token are the wall.
- **A `gh` script or a second network client.** `watch-ci` uses the engine's tokenless `read_ci` for the commit the
  person pushed. There is no `gh`, no token and no new spawn site. `unknown` is never drawn as a pass.
- **Several reviewers, models or parallel workers.** `review-loop` is one critic. `second-opinion` asks the person
  for a separate model rather than inventing a jury. `compare-approaches` writes sketches in one plan;
  `wide-audit` runs slices one after another. The engine has one loop, one writer and one approval story.
- **A file a skill writes.** `resume-long-run` and `decision-log` use `todo`, `recall`, `recall_threads` and
  `context-transfer`; they do not create `PROGRESS.md` or a ledger file. Only `write`, behind an approved plan,
  changes the workspace.
- **A GitHub comment.** `review-replies` drafts one; a person posts it and resolves the thread. The engine stores
  no token and has no comment route.
- **A visual verdict without a rendered view.** `design-review` can read source and page text, but it has no
  screenshot tool. An area it could not see is **not checked visually**, not guessed.

## 6. What was built

The 24 files join the nine built-ins already in `engine/builtin_skills/`, giving the directory **33
built-ins**. `ship-a-change` remains the old compiled order; the rest cover review, proof, shipping, long-run state
and understanding. The docs list every name, and `tests/test_skills.py` derives that list from the directory rather
than maintaining a second one.

`tests/test_skill_moves.py` checks that every declared move exists; `tests/test_skills.py` pins the proof grades,
handoff sections, design limits and the menu/docs agreement; `tests/test_conductor_prompt.py` checks that no
built-in names a removed tool. The native recipes are prose, so they are verified as data, not as a capability.

## 7. Keeping the built-ins current

A skill is data, and data decays: a command it names is renamed, a doc it cites is moved, a description stops
matching what the skill does. The upkeep is mechanical where it can be and a monthly pass where it cannot.

**Held by `tests/test_skill_contracts.py` on every run.** Every built-in loads with no problems and declares only
moves that exist. Its description is a sentence that fits the 200 characters the menu shows without being cut (a
clipped description ends in an ellipsis, and that is the tell), and no two descriptions are the same or one inside the
other. A skill that declares `write` or `run_command` has a `## Stop when`. Every `docs/`, `engine/`, `tests/` or
`scripts/` path a skill cites exists. A workspace skill is loaded, never judged: a person's own recipe is not refused
for style.

**The monthly pass, by a person.** For each built-in, re-read its description against its body (is it still the one
sentence that tells the conductor when to reach for it?), re-run the move it leans on hardest, and delete or merge a
skill nobody reached for. `audit_usage` and the goals' `use_skill` calls say which were loaded. A skill that earns no
use for a quarter goes on a decay list in the pass's notes and is removed the next quarter unless something loaded it.
Whether a skill makes the conductor *better* is not measured: that needs the benchmark harness to run a task with the
skill on and off (`docs/21` P6), and until then a skill is kept on its reasoning and its use, not on a number.

**Security profiles carry provenance.** Each `engine/builtin_profiles/*.json` has `reviewed` (an ISO date) and
`sources` (the CWE ids its rules cite, kept equal to the rules by the same test). `reviewed` is the date a person last
compared the rules with their sources. Its first value, 2026-11-02, only attests the field's introduction: the suite
already checks that every rule matches its own positive examples and none of its negatives, and nobody re-derived the
rules from the CWE entries that day. The next real review replaces it.

## 8. What this does not claim

- That those 24 recipes make the engine cheaper or better. They change the order the conductor is told to work in,
  and nothing more.
- That any upstream number is true. None was run or reproduced; the repository publishes no benchmark result that
  this audit checked.
- That the `ds-watch-pr` script's body was read. Its documented command line and exit meanings are the evidence; it
  was not installed or run.
- That a person's merge, push, comment or screenshot is now an engine action. Each stays a handoff, stated in the
  recipe that reaches it.
- That upstream is frozen. `main` matched the pin on 2026-11-01; a later commit needs another comparison.
