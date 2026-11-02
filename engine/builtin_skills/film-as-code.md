---
name: film-as-code
description: Plan and write a short film, explainer or animated demo as code - one idea, a timeline in numbers, stills first, a deterministic build - then hand the render and the watching to a person.
moves: ask_user, recon, read_file, search_code, fetch_page, read_page, plan, design, write, verify, run_command, todo
---

# Film as code

A short film made as code is a page or composition whose every frame is a
function of time. This recipe plans it, writes it, and checks what a machine can
check. It never claims to have watched it.

## What this can and cannot do

- **Can:** read the product (`recon`, `read_file`, `fetch_page`), write the plan
  files and the composition (`write`), and run the project's own tests and
  validators (`verify`, `run_command` where the allowlist admits them).
- **Cannot:** render, take a screenshot, watch or listen. No video or capture
  tool is admitted, and loading a recipe adds none. The render command and the
  watching are a **handoff to a person**, and the report says which checks ran.
- Anything that costs money or credits (a paid image, voice or music service) is
  the person's choice, made with the real price in front of them. Approval covers
  that amount and nothing more.

## Ask once, with the answers filled in

Put what you understood and your chosen defaults in a single message, so the
person can answer "yes" or correct one line. Not a form.

| Field | Default if not given |
|---|---|
| Audience and venue | a social feed, played silent |
| The single message | propose it, then restate it in one line |
| Length, shape, frame rate | 30 s; the shape of the place it plays |
| Locked words, and where facts come from | no figure shown that lacks a source |
| Real material | the product's own screens, logo, colours, type |
| Voice and music | none unless asked |

Do not invent a product's interface. Use the real one, or say there is none. If
the person is away, decide, and log each decision in `todo` as
`DECISION: ... because ...` so the person can reverse any of them with one reply.

## Treatment before code

Write it down, in the plan or `todo`:

- the idea in one sentence, and the film retold in three plain sentences with
  no technique in them. If you cannot retell it, there is a treatment, not a
  film yet;
- the same film with its words removed. A shot that is just text over an empty
  background reads as a caption rather than an image; let what the product does carry it;
- the hook, on screen and readable with the sound off by 3 seconds;
- one or two reference works: what to learn from them, what not to take;
- **three** structures; keep one and say why the other two lost;
- a beat table, one row per beat: when, the action, what the viewer has to take
  in, what triggers it, the exit, the sound.

Every number on screen goes through `claims-ledger`.

## The timeline is the plan

A single file lists each beat with its time in seconds, and the picture,
captions, sound and checks are all derived from it. Write a small validator with the film and run it through `verify`. It
fails a hook not legible by 3 s, text under a size floor, a term that is never
shown, a reveal with no cue, a hold longer than about a second that is not
marked as a hold, and a seam with no stated reason. Feed it a deliberately
broken timeline once, so you know it can fail.

## Build

- **Nothing but time varies.** No wall clock, timers, CSS transitions or
  unseeded randomness. A seeded generator, and only libraries that can seek.
  The same frame drawn twice is the same frame. Test it without a renderer:
  sample the scene's state at a set of times forward, then backward, and require
  the two to be equal.
- Use the engine the project already uses.
- **Riskiest shot first,** at full size. Decide whether the approach works
  before building the rest.
- **Stills before any render:** one per beat, at full size and at 360 px wide.
- No catalogue of shots to copy. Take structure from the product, not from the
  last film you saw.

## Hand off

Give the person: the render command the project defines; a list of moments to
look at (first frame, the hook at 3 s, the first half-second after each cut,
the payoff, the last frame, the loop seam) and what to look for in each; the
sound to listen for (quiet before the payoff, loudest on it); the decisions
made; the weakest shot; what was **not checked**.

Ask any reviewer, a person or a separate model, to retell the film in one
sentence first and to say where attention drifted. Give them the brief, the
file and a table of where each on-screen word came from, not the maker's
explanation. Each note they pin to a moment goes through `scoped-fix`.

## Stop when

- The validator passes, the stills list and the render command are handed over,
  and the report separates what ran from what a person must do.
- Three review rounds are done and the score is still under the bar. Hand it
  over with the open problems; do not run a fourth round and call it passing.
- The person stops you, or the next step spends money they have not approved.

## What this is not

It does not post, upload or publish. It does not install render tools or models;
a person decides that.
