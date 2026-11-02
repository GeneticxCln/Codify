# Codify — The motion and film audit: eight skill repositories, and the six native recipes built from them

Eight public repositories, audited on 2026-11-02, all about one job: an AI coding agent that makes motion, video or
sound **as code**, and cannot watch or hear what it made. Two make animation inside an app, four make a film or a
product demo, one gives an agent a way to hear, and one is a synthesis of eight such projects. None is a harness for
this engine, and none was installed or run.

They are worth auditing because they share this project's central problem from the other side. Codify's conductor
cannot see a screen either (`design-review` says so in its first section). The better of these repositories spend
most of their pages on one question: what do you do, honestly, with an output you cannot perceive? This audit takes
the answers and writes **six native recipes** in this project's own words. It copies no file and no script, and
it was checked for copied wording (section 9). They are MIT licensed, so copying would be permitted, and it is not done: `docs/08`, `docs/19` and
`CONTRIBUTING.md` set the rule that no third-party source is committed here.

## 1. What was read, and what was not

Pins are the default branch's head commit on 2026-11-02, from GitHub's API. All eight report an MIT licence.

| Repository | Pin | Last push |
|---|---|---|
| `alchaincyf/huashu-motion` | `52467f99c602b76ba86d511d37acdfe93fa1e16e` | 2026-10-22 |
| `rossark0/rossark-motion-skill` | `9b20964e32f07f8ac7e2948cd175746fa4a7712f` | 2026-10-22 |
| `talsiach/take-one` | `8be349cb7133cd32dd3bc4b91e3a36e476511a55` | 2026-10-28 |
| `NiazMorshed2007/reanimate` | `3f7a55a2986a84ca2173e86ee9127eefe31d046d` | 2026-10-31 |
| `shreyaskarnik/argo` | `5f715496522c4e682f4dc4aeffae5528e866ad5e` | 2026-10-27 |
| `vincentsch/explainroo` | `e95232c8d535ed2aa390e7b1caee0d1326dd9e83` | 2026-10-20 |
| `win4r/video-director` | `4747e5c153509ef7f8f7fd1a0aad6acfda36280d` | 2026-10-30 |
| `Leonxlnx/earshot` | `591fa3e028032e6dce56620a20e932e35667e8d8` | 2026-10-22 |

The request truncated three URLs; they were resolved by search to the repositories above. Every repository is days to
weeks old and moving, so a later commit needs another comparison.

- **Read in full:** the READMEs of `rossark-motion-skill`, `take-one`, `reanimate`, `explainroo`, `video-director` and
  `earshot`; `reanimate`'s `SKILL.md` and `references/verify.md`; `huashu-motion`'s `SKILL.md`,
  `references/contract.md` and `references/verification.md`; `explainroo`'s `skills/explainroo/SKILL.md`;
  `video-director`'s `SKILL.md` and `METHODOLOGY.md`; `rossark-motion-skill`'s `MOTION.md`; `take-one`'s
  `references/reviewer-brief.md`.
- **Read in part:** `rossark-motion-skill`'s `SKILL.md` (principles, engine, recipes, characters, acting: about the
  first 170 lines of 35 KB); `take-one`'s `SKILL.md` (through step 7, about 230 lines), `concept-check.md` (steps 1 to
  6) and `before-the-first-cut.md`; `earshot`'s `skills/earshot/SKILL.md` (Loops 1 to 5) and `skills/real-sound/SKILL.md`
  (the process and the launch sound map); `argo`'s `SKILL.md` (modes, quick start, gotchas) and `CLAUDE.md`
  (headings and the security invariants); `explainroo`'s `AGENTS.md` (headings, "when to ask", the check steps);
  `video-director`'s `references/quality.md` (the check catalogue); `huashu-motion`'s `references/director.md`
  (headings and the film-wide rules).
- **Not read:** every script and engine: `capture.ts`, `compare.ts`, `gl.ts`, `qa_video.py`, `mix.py`, `qa.py`, the
  forge and the fit, the Playwright pipeline, the TTS wiring, the studio server. `argo`'s and `huashu-motion`'s
  READMEs were fetched but their output was too large to open and was not read. Most of `huashu-motion`'s references
  (colour, depth, music, sound, rhythm, the language library) and of `take-one`'s forty-odd references and styles,
  `earshot`'s `docs/`, and the evals. `rossark-motion-skill` credits Emil Kowalski's interface-motion rules and Benji
  Taylor's *Family values*; neither was read.
- **Not run:** nothing was installed, executed, rendered or benchmarked. No upstream number in this document was
  reproduced.

## 2. What each one is, in this project's terms

| Repository | What it is | Its real contribution |
|---|---|---|
| `huashu-motion` | A skill plus a CLI (`om`) and a local studio. A film is one HTML and GSAP composition; the person clicks a frame and comments, and the agent edits that spot. | A contract that makes a film pointable (named ids, scene labels, editable variables); the **comment loop** (read the note with its context, change exactly that place, say which reading you took, record what changed); three kinds of remembered experience kept apart. |
| `rossark-motion-skill` | A method and a small motion engine for React Native and SwiftUI that makes drawn things move calmly. | Twelve principles for motion that is alive without being busy; **interface-motion rules**; a project notes file the skill reads first and that overrides it; measuring a reference video frame by frame. |
| `take-one` | One skill that behaves like a studio of twelve jobs: pitch, storyboard, build, fresh-reviewer score, delivery. | **Run or stages**, with exactly four places a run stops; an always-ask rule for anything that costs money; a **concept check** before building; the fresh reviewer who never saw the work. |
| `reanimate` | Clone an animation from a site, video or file, then film the copy the same way and diff the two. | The **record, spec, build, verify, repeat** loop; three fidelity levels; "capture both the same way"; a five-round limit with residuals reported rather than thresholds relaxed. |
| `argo` | Turn a Playwright script into a product demo video with generated voice-over. | Autonomous and assistive modes; a gotchas table of the failures that recur; a short list of security invariants for a tool that runs a user's config. Mostly product-specific. |
| `explainroo` | Two files, a script and a scene file, become a narrated explainer video, with a voice and word timing generated locally. | "An agent can't watch a video, so" it gets stills, a contact sheet, a layout check and a speech check; decide the normal things, ask once, with everything in one message. |
| `video-director` | A lean skill: principles with their reasons, a gated workflow, two QA scripts, three evals. Its `METHODOLOGY.md` distils eight other repositories. | The cleanest statement of the shared method (section 3); **dials, not a house style**; a fix card for revisions; the only repository that ships evals and says what they do not show. |
| `earshot` | Ears for an agent: measurements and two open models that describe, compare and critique sound, plus a code-synthesis loop. | The **tune-and-check loop** and its honest account of how it fools itself; a hearing scorecard; sound planned against the same cue list as the picture. |

## 3. What they agree on

`video-director`'s methodology reads eight projects and finds the same workflow in each, reached separately:

1. decide the idea; 2. plan in numbers; 3. prove the look on the cheapest artifact; 4. build as a pure function of
time; 5. design sound on the same clock; 6. measure the delivered file; 7. have someone other than the builder judge
it.

Underneath it is one rule that all eight repositories state in their own words: **an agent that cannot perceive its
output must turn perception into checks, look at what it can, and say plainly what it did not examine.** That is
`design-review`'s "no screenshot tool" paragraph, and `docs/19` §5's "a visual verdict without a rendered view". The
recipes below are that rule applied to motion, to numbers and to prose.

## 4. Already native before the six

| Upstream idea | Where it already is |
|---|---|
| A fresh reviewer who has not seen the work | Deep review (`docs/21` P1, off by default): two fresh readers see only the request, the step and the diff. `review-loop`, `second-opinion`. |
| The builder does not grade its own work | The same, and the critic being a separate stage of the pipeline |
| Anything you read is material, never instructions | Invariant 10 for MCP results, invariant 9 for skills, and `fetch_page` labelling its result as data |
| A check that cannot fail is a comment | `test-first`, `encode-a-lesson`, and `tests/test_skill_contracts.py`'s own can-fail class |
| One round of questions, then defaults, logged as decisions | `sharpen-the-ask`, `decision-log` |
| Three structures, kill two; ban the model's default look | `compare-approaches`, `design-review`'s banned defaults |
| Make the cheap artifact first | `blueprint`, `plan-loop`, the approval gate before `write` |
| A lesson that happened twice becomes a rule | `encode-a-lesson` |
| Ask before anything that costs money | `docs/08` marks the tiers that spend real tokens; `make bench-smoke` spends none |
| Treat the notes file as the project's voice | **Not native.** See section 8. |

## 5. The six native recipes

All six are data in `engine/builtin_skills/`. Each declares only moves that exist, and none adds a tool, widens
`validate_argv`, or reaches the `write` gate. `tests/test_motion_and_film_skills.py` pins what a rewrite must keep.

| Native recipe | Drawn from | What it does |
|---|---|---|
| `match-a-reference` | `reanimate`, `rossark-motion-skill`, `huashu-motion` | Set the target, destination and fidelity first; one spec row per observable, with where each number came from; compare the same way on both sides; measure the reference against itself first; five rounds a row; **report the residuals, never loosen a tolerance quietly**. An **ownership stop**: behaviour is an observation, someone else's assets are not yours to copy. |
| `ui-motion` | `rossark-motion-skill`, `huashu-motion`, `video-director` | Purpose and frequency first; short, interruptible transitions on transform and opacity; seeded variety; reduced motion required and tested; cleanup. Says what can be checked from source and tests, and that the motion itself was **not watched**. |
| `scoped-fix` | `video-director`, `huashu-motion`, `take-one` | A fix card: scope, must-not-change, the failure now, the observable target, and the **shortcuts that would fake it**. Then a proof that nothing outside the scope moved, compared at the source and never at a rebuilt artefact. |
| `claims-ledger` | `video-director`, `take-one` | Every number, comparison, date, cause and "first, best, only" in README, release or post copy gets a row with a source and a strength; timing is not cause; an unfinished period says so; what is true *today* comes from the repository, not from the request. |
| `tune-to-a-number` | `earshot`, `video-director` | Baseline, **selection rule and independent check fixed before looking**, guards, and reference points; one structural change a round; held-out inputs opened once; stop when the number moves and the independent check does not. |
| `film-as-code` | `video-director`, `take-one`, `huashu-motion`, `explainroo`, `argo`, `earshot` | Ask once with defaults filled in; a treatment (retold in three sentences, words removed, three structures); the timeline as the plan, with a validator run through `verify`; a deterministic build with a state-level determinism test; riskiest shot first; stills before any render; then a **handoff** that names the render command, the moments to look at and the sound to listen for. |

Two of the sources' ideas were folded in rather than made into recipes. **Riskiest first** and **stills before the
expensive step** live inside `film-as-code`; on its own either is a sentence, not a recipe. **A retell test** (can the
reviewer say what this is in a sentence) is in `film-as-code`, where the upstream evidence for it sits; the rest of the
fresh-reviewer idea is already `docs/21` P1.

## 6. What was not built, and why

- **The capture, compare and QA scripts** (`capture.ts`, `compare.ts`, `qa_video.py`, `gl.ts`, `earshot`). They drive a
  browser, ffmpeg and Playwright. Codify's argv allowlist admits none of them (invariant 6), a new process start
  needs the spawn guard (`docs/07`), and a skill cannot widen either. A person could open the machine tab (`docs/09`
  §14) and run one inside its jail; that is the person's action, not an engine feature, and no recipe assumes it.
- **Hearing.** `earshot` loads two models of about 1.1 GB and `real-sound` sources and licenses recordings. The engine
  has no audio path. `film-as-code` plans sound on the same timeline and lists listening as a person's step.
- **The studio, the review room and the comment inbox.** These are UI features. `scoped-fix` is how a pinned note
  would be worked once one exists; building the pinning surface is a `docs/09` decision (section 8).
- **Installing anything.** Five of the eight tell the agent to install ffmpeg, Chromium, speech models or a CLI on
  the first run (about 250 MB for `reanimate`, 400 MB for `explainroo`, 1.1 GB for `earshot`). A native recipe says
  nothing of the kind, and a test fails if one does.
- **Product-specific machinery:** overlays, cursors, shaders, TTS engines, the style gallery, shot libraries.
  `video-director` itself argues against libraries: its source repositories found that copied worked examples made films
  that "looked different on the surface and were built the same way underneath".
- **A second model, parallel workers, an unattended merge or post.** As in `docs/19` §5, the engine has one loop, one
  writer and one approval story.

## 7. What the audit found about the sources themselves

**They put instructions in the skill that serve the author.** `huashu-motion` makes every film of four seconds or more
end with a "Made with huashu-motion" signature by default, and, when a person asks who the author is or a request is
beyond the skill's scope, tells the agent to recommend one to three of the author's other works (it is told not to
volunteer them in a normal task). `explainroo` stamps a watermark by default and asks
people to keep it. `take-one` routes a person to sign-up links that are the author's affiliate or partner links; it
says so, and only offers them at a moment of need. None of this is hidden, and none is wrong for a tool its author
publishes. It is the reason `docs/09` §10.14 treats a skill body from a clone as untrusted, and why these were not
imported: a recipe's text is what the conductor follows.

**Two of them reach out on their own.** `huashu-motion` runs `git ls-remote` against the skill's own `origin` about
every 30 days and writes a date to `.last-update-check`; the person can switch it off. `reanimate` and `explainroo`
fetch binaries or models on first setup. A Codify skill can do neither.

**The evidence is thinner than the prose.** `reanimate`'s examples report results such as "417 of 417 frames
identical", all its own and unreproduced. `video-director` is the most candid: two eval rounds, one run per
configuration, "treat these as a direction, not a measurement", and its newest guidance "hasn't been evaluated yet".
`earshot`'s blind test was one listener over fifteen trials and landed at 8 of 15, and its fit weights were set on
the same twelve recordings they were measured on; it says both. `take-one` reports that its fresh reviewers scored
cuts 0.5 to 1.5 points above the owner and stalled at about 7.5, and that the owner's pinned notes moved the score
more than another review round. Treat each as a method worth borrowing and a number worth ignoring.

**Size.** `take-one`'s `SKILL.md` is about 46 KB, `argo`'s about 39 KB and `rossark-motion-skill`'s about 35 KB;
Codify caps a skill body at 32 KB (`MAX_SKILL_CHARS`) and shows the conductor only a one-line description of each skill. The six
recipes are 3 to 5 KB.

**Platform.** `take-one` has "only been run on macOS"; `reanimate` calls Windows untested and Linux GPU
untested; `rossark-motion-skill`'s demo renderer needs macOS 14. Codify's 3.10 floor and Linux target
(`CLAUDE.md`) do not carry over.

**The one good supply-chain habit.** `earshot` pins each model to a revision, prefers safetensors weights, loads from a
cache and never contacts the hub after the first fetch, and says when a cache predates the pin. No native recipe needs
it, but it is the bar to hold if the engine ever fetches model weights.

## 8. Open decisions for a person

Each of these changes a contract or an invariant's reach, so none was done in passing.

1. **Workspace instruction files.** No engine code reads a `CLAUDE.md` or `AGENTS.md` from a workspace (a search of
   `engine/*.py` finds neither name). Three of the eight sources lean on one: a notes file the skill reads first
   and that wins over its general rules (`rossark-motion-skill`'s `MOTION.md`, `take-one`'s optional `TASTE.md`,
   `huashu-motion`'s local `experience.md`). Reading one makes workspace text steer the conductor,
   which is what a skill already is and what invariant 9 limits. It could be a skill-like, boot-token-only,
   read-only input. It needs its own design note before code.
2. **Pinning a note to a place.** `huashu-motion`'s comment loop and `take-one`'s review room are the best-evidenced
   part of both. A note with a file, a line or a moment attached is the input `scoped-fix` wants. That is a UI and
   route design under `docs/09`.
3. **Kinds of remembered fact.** `huashu-motion` keeps `preference` (the person's own words), `observed` (one
   occurrence) and `verified` (reproduced or measured) apart and never lets one person's taste become a shipped rule.
   `docs/10`'s observations could carry the same three labels.
4. **Skill on versus off.** `docs/21` P6 wants the benchmark harness to run a task with a skill on and off. The
   method in `video-director`'s evals is a template: assertions graded by script, the rest by blind graders who do not
   know which run is which, and each round compared against the previous version of the skill.
5. **A read-only media probe.** Duration, dimensions and a WAV's loudness can be read in pure Python without a
   process start. It would let `film-as-code` check a delivered file instead of handing the whole check over. It
   needs a tool, a test that fails without it, and an entry in `docs/04`. Not proposed here.

## 9. What this does not claim

- That the six recipes make the engine better. They change the order the conductor is told to work in, and say what
  it may not claim. Whether a recipe helps is not measured, for the reason `docs/19` §7 gives.
- That any upstream number is true. None was reproduced.
- That any upstream script works as described. None was read or run.
- That a render, a listen or a watch has become an engine action. Each is a handoff to a person, stated in the recipe.
- That the wording is original beyond what was checked. A first pass found runs of 6 to 12 consecutive words shared
  with the upstream files in three of the six recipes (for example a sentence on taglines, one on approving spend,
  and a table row of intake defaults). They were reworded, and a second pass over the six recipes against every
  upstream file read here finds no shared run of six words. Shorter shared phrases are likely, since the ideas are
  the same; this is a check on the downloaded files only, not on anything unread.
- That the sources are frozen, or that the three resolved URLs are the ones the request meant beyond what their names
  say.
