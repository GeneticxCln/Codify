# DESIGN.md — the brand contract for Codify's own UI

Binding for any goal run against this repository. The engine discovers a `DESIGN.md`
at the workspace root on its own (`docs/04` §4.0a) and treats it as binding, so this
is not documentation — a fixer's step that changes a rendered surface is judged
against this file, and a design-role goal edits *this* contract rather than authoring
a rival one.

The machine-readable half is `ui/tailwind.config.js`. The two cannot drift:
`ui/tests/designTokens.test.ts` fails if a value quoted here stops matching the config,
in either direction — an undocumented token is as much a failure as a wrong one.

---

## 1. What this app is

A developer tool, not a consumer app. The user is reading **diffs, JSON, stack traces
and shell output** for minutes at a stretch, often while a goal is running. Every
visual decision follows from that one fact:

- **Legibility outranks decoration.** A diff you cannot read is a failure. Contrast is
  never traded for prettiness, and nothing animates that the user did not ask to move.
- **Density is a feature, not a defect.** An engineer scanning 40 pipeline stages
  wants more on screen than a marketing page would. But density only reads as dense
  when the *spacing* is systematic — see §4.
- **Status is information, and it is load-bearing.** Six hues doing one job is how a
  reader learns to stop trusting colour. Five tones, each with exactly one meaning.
- **Nothing is hidden behind a hover.** If a control is reachable by pointer, it is
  reachable by keyboard, and it says what it does.

## 2. Colour

Dark only. The app is a full-screen desktop window beside an editor; a light theme
would be a second brand to maintain for no one. The base is a neutral blue-grey ramp
so the **status hues are the only saturated things on screen** — when something is
coloured, it means something.

### Surfaces

| Token | Value | Used for |
|---|---|---|
| `bg` | `#0d1117` | app background, behind everything |
| `surface` | `#161b22` | panels, the command bar card, drawers |
| `raised` | `#21262d` | controls at rest, hover targets, inset rows |
| `border` | `#30363d` | all dividers and resting control borders |
| `border-strong` | `#484f58` | scrollbar thumbs, borders on hover/focus |

Three surfaces, not more. A fourth would mean a component could not say what it sits
on. `raised` exists because hover and "a control" are the same visual weight here.

### Text

| Token | Value | Used for |
|---|---|---|
| `primary` | `#e6edf3` | headings, the code you asked to read |
| `secondary` | `#c9d1d9` | body copy, chat prose |
| `muted` | `#8b949e` | metadata, timestamps, placeholders, disabled |

Never more than three weights of text colour in one component. If a fourth thing needs
emphasis, it is bigger or bolder, not a new grey.

### Status — five tones, five meanings

| Token | Value | Means exactly |
|---|---|---|
| `accent` | `#2f81f7` | the one interactive hue: primary action, and the current selection in any list |
| `info` | `#2f81f7` | in flight: planning or running |
| `success` | `#3fb950` | completed, passed, healthy |
| `warning` | `#d29922` | needs attention but is not wrong: paused, recording |
| `danger` | `#f85149` | failed, refused, destructive, or Stop |
| `neutral` | `#8b949e` | idle: pending, cancelled, or nothing to report |

`accent` and `info` are deliberately the same hue, and they are two tokens rather
than one on purpose: "press this" and "this is happening" are different sentences, and
a component should say which one it means. Sharing a value means the two can never
drift apart, while sharing a *name* would have made a control that reports state
indistinguishable from one you are meant to press.

**A status tone is never used decoratively.** If a control is not reporting a state, it
is grey, and that is not a lack of colour — it is the default.

### Mode accents — reserved, and each one means one thing

| Token | Value | Reserved for |
|---|---|---|
| `design` | `#db61a2` | the DESIGN.md deliverable mode, and nothing else |
| `knowledge` | `#39c5cf` | the CODIFY.md deliverable mode, and nothing else |

These two were previously indistinguishable from each other and from **Record**,
which is the bug the audit found: Record and Knowledge Deliverable carried
byte-identical classes, so two unrelated features announced themselves the same way.
Record keeps `warning` — "this records every model call" genuinely is a caution — and
knowledge moved to its own hue so it no longer reads as Record.

The rule that prevents a recurrence: **a new feature does not pick a colour, it picks
a tone from the tables above.** A hue is added here only when a genuinely new
*category* exists, never to differentiate two controls that are both merely "armed".

### Runtime themes — a data layer, not tokens

The Appearance setting (`ui/src/appearance.ts`) swaps the surface and text properties
at runtime by writing CSS custom properties on the document root. Those hexes are
**deliberately absent from the tables above and from `tailwind.config.js`**: this
file's tables are compile-time and frozen to this config by
`ui/tests/designTokens.test.ts`, while a theme must vary per user choice. The status
hues are not themed — five tones each with exactly one meaning is a contract about
what "failed" looks like, and a theme does not get to renegotiate it.

**The transcript's own surfaces take no hue of their own.** Anything in the
conversation that carries words composes from the active theme's `raised` and
`primary`, and is opaque — no `/NN` alpha, because a theme that can put a user's
own prompt over moving weather has made a legibility problem out of a preference.
The user's bubble and both speaker avatars are the case this rule exists for: the
bubble was `bg-blue-600 text-white` beside a blue-tinted avatar, a saturated slab
in an app whose premise is that a theme supplies the surfaces. The blue belonged
to no theme and to no meaning — it is neither the one interactive hue nor one of
the five status tones, so it was decoration that happened to be loud. With both
avatars neutral, *who is speaking is said by side and by the bubble's tail
corner*, not by colour, and that is asserted rather than assumed
(`ui/tests/transcriptPalette.test.ts`). That test also checks the `primary`/`raised`
pair of **every** theme against 4.5:1, because "a colour that will not conflict
with any of the themes" is a claim about the pair and not about a class name: a
fourth theme could ship a `primary` too close to its own `raised` and every
class-name assertion would still pass. The bubble's border is load-bearing, not
decoration — on OLED `raised` and `bg` are two steps apart, so the border is what
draws the panel.

The OLED CMatrix theme publishes three variables of its own, which its canvas rain
(`components/ui/MatrixRain.tsx`) reads live:

| Variable | Value | Used for |
|---|---|---|
| `--cmatrix-bg` | `#000000` | the rain's background — pure black, the point of OLED |
| `--cmatrix-rain` | `#003300` | the falling trail glyphs, deep dark green |
| `--cmatrix-text` | `#a3ffb8` | the rain's head glyph; high-contrast light green |

`--cmatrix-text` is also that theme's `--codify-secondary`, so the app's body text
and the rain's bright pixel are one decision, not two that can drift.

The Cyberpunk theme publishes four of its own, which its grid and its CRT veil
read (`components/ui/CyberGrid.tsx`, `.cyber-veil` in `ui/src/index.css`):

| Variable | Value | Used for |
|---|---|---|
| `--cyber-cyan` | `#00e5ff` | the horizon grid, the horizon's bright edge, the stutter wash |
| `--cyber-magenta` | `#ff2e9a` | the sun's lower band and the rungs nearest the viewer |
| `--cyber-amber` | `#ffb02e` | the sun's upper band |
| `--cyber-scan` | `#1b0a35` | the CRT veil's scanline period |

One deliberate difference from OLED CMatrix: there, the bright variable *is* the
body text, because the rain's head glyph is text. Here the bright pixels are
lines and a sun, which nothing in the transcript has to be legible against, so
`--codify-secondary` stays a lavender (`#d9c8ff`) that reads over a moving
grid. Coupling them would buy no anti-drift guarantee and cost legibility — the
rule being "one colour, one decision" applies to a colour doing two jobs, not
to two colours doing one.

A theme that wants weather is a token edit plus a backdrop, not a new mechanism:
the managed-property list in `ui/src/appearance.ts` is data, and a backdrop
decides whether it exists by asking the theme whether it published its variable
(`RainBackdrop` on `--cmatrix-rain`, `WeatherBackdrop`'s table on the rest).
Adding a variable to a theme that is not in `MANAGED_VARS` is the one mistake
that cannot be caught at the call site, because it shows up as a previous
theme's colour surviving a switch — `ui/tests/atmosphere.test.ts` asserts every
published variable is managed, triplets included. That test earned its keep the
first time it ran: `--cmatrix-*-rgb` had never been managed, so the OLED theme's
derived triplets survived a switch to any other theme. Nothing read them yet,
which is the only reason it had been invisible.

The remaining four animated themes publish variables of their own, each in its
own namespace so a theme can never set another's by accident:

| Theme | Variable | Value | Used for |
|---|---|---|---|
| Neural Constellation | `--neural-web` | `#1e293b` | the links between nodes, at their faintest |
| | `--neural-node` | `#38bdf8` | the nodes' cores and the link colour near the middle |
| | `--neural-pulse` | `#0ea5e9` | the ring that travels outward while a run is in flight |
| Cyberpunk HUD | `--hud-amber` | `#f59e0b` | brackets, sweeps, the tick ladder, and the element opacity |
| | `--hud-tick` | `#fef3c7` | the long ticks and a sweep's leading edge |
| Bioluminescent Abyss | `--abyss-deep` | `#059669` | each spore's wide halo |
| | `--abyss-spore` | `#10b981` | each spore's core |
| Solar Flare | `--flare-indigo` | `#6366f1` | the inner end of every stream and the dimmer stars |
| | `--flare-ultraviolet` | `#a855f7` | the outer end of every stream and the brighter stars |

The HUD's "15% opacity" is `ELEMENT_ALPHA` in `HudSweep.tsx` rather than a
baked-in alpha, because the brief's elements are not all the same brightness —
a sweep's leading edge and a long tick both need more than the brackets do, and
an alpha inside a hex cannot be varied per element.

**One theme with no effect of its own.** `ascii-rain` publishes
`--cmatrix-rain` at `#262626`, `--cmatrix-text` at `#404040`, and a
`--cmatrix-glyphs` of `0123456789ABCDEF`, so `RainBackdrop` mounts for it on the
variable it already watched and `MatrixRain` draws hex bytes instead of kana.
Its `--codify-secondary` is `#f5f5f5` and is deliberately *not* the same value
as its head glyph: OLED CMatrix aliases the two because its head glyph is the
app's body text, and doing the same here put a white head on every column behind
the app's own white text. Monochrome is one colour, not one brightness.
The glyph set is the one non-colour value a theme may publish: `applyTheme`
derives a `-rgb` triplet for `#rrggbb` values and sets everything else verbatim.
It is also the claim the rain's own docstring makes — "adding a third rainy
theme is a token edit" — being cashed two themes later, and it is the reason the
gates are keyed on variables. Keyed on ids, this theme would have needed a
canvas that differed from `MatrixRain` in four lines, and the fifth theme after
it would have needed one of its own.

**One theme with no motion at all: `still`.** Every theme above is a weather
system, and `still` is the one that is not a palette — it is a statement about
motion, shipping **no canvas whatsoever**. It is in the list because §7's "motion
must be justified" is a rule with nothing behind it until something bets a whole
theme on it.

It is not the same thing as `prefers-reduced-motion`. That already offers a
motionless app, and offers it by freezing a *rain* — a still frame of an
animation, sitting in the settings pane behind the words. Motion opted out of is
not motion absent. This theme has no frame to freeze, and it is the difference
between "I do not want weather" and "I want weather, quietly", which are two
different requests and until now only the second had an answer.

The palette is a warm near-monochrome, at `#131311` background, `#1c1c19`
surface, `#26261f` raised, `#3a3a33` border, `#55554a` strong border,
`#f7f6f2` primary, `#d6d5cc` secondary, `#8e8d84` muted — the highest
`primary`-on-`raised` contrast of any theme here, and no saturated hue anywhere
in it. With nothing to look at, contrast is the whole design, and a colour
carrying no meaning is a colour competing for the attention the theme exists to
give back.

**Which themes have no motion is declared, not implied.** `MOTIONLESS_THEME_IDS`
in `appearance.ts` names them: `codify-dark`, which never had an effect, and
`still`, for which having none is the point. Every theme is either in
`ATMOSPHERE_TRIGGERS` or on that list, and a theme in neither fails the suite —
that is the stress test doing its job, because a rule that cannot say no is not a
rule. Before this, the same fact lived as an inline `CODIFY_DARK.id` inside a
test, which read as a remark about the default rather than a decision about
motion; the second motionless theme is what made the difference visible.

**How the settings surface them.** The Appearance pane is a **list and a detail**,
not a grid of cards, and that is a decision about what a theme list is for. Two
things broke together at eight themes, and only one of them was navigation: a
card is ~110px tall, so the eighth theme was two scrolls down a modal and
comparing two meant holding one in your head; and every card drew its theme's
live effect, so opening the tab started **eight animation loops inside a window
someone opened to read a setting**. The old file conceded the second point in a
comment while shipping it. Now the rows are ~68px, which is all eight inside the
list's cap without it scrolling, and exactly **one** canvas runs — the one in the
detail panel, at a 320px cap rather than a tile's 120.

The rows keep every theme's swatch line, because it is the fastest way to tell
two dark themes apart and the easy fix would have been to drop it. The
description moved to the detail panel, because it is the wordiest part and the
part you read once you have already narrowed to one theme. Hover previews;
clicking commits — only the click writes storage, so a preview never claims to
have been chosen.

**Adding a theme has a cost, and it is not the one people expect.** The token
edit is cheap; the ninth row is what hurts. The pane's navigability is the thing
under pressure, so a new theme should arrive with a reason it is distinguishable
from an existing one, and if it cannot be stated, the answer is a narrower list
— grouping, or showing a few by default — rather than a longer one. This is also
the one surface in the app that must pick **no** colour of its own: it is where
a user looks to see what everything else looks like. The status green is the sole
exemption, because the dark theme's preview shows it on purpose as the only
saturated thing in that palette, and it is pinned to that one spot.

### Armed, selected, and at rest

This is the distinction that was missing, and it is the one that most often went
wrong. Three separate ideas were all being drawn as "a coloured pill":

| State | How it is drawn |
|---|---|
| **Armed** (a toggle you have switched on) | the tone's `/-20` background, `/-50` border, `-300` text |
| **Selected** (the current choice in a list) | `accent` tint — one colour, everywhere |
| **At rest** | `raised` fill, `border` border, `muted` or `secondary` text |

Selection is `accent` in *every* list — model menus, workspace pickers, mode pickers.
It used to be purple in one and blue in another, which made "which one am I on?"
a per-component question. Arming is a state, not an identity: a tool that is armed is
tinted with **its own** tone, and only because it is armed.

## 3. Type

A dense ramp, roughly a step tighter than Tailwind's defaults. The reason is
arithmetic, not taste: this UI already rendered 203 declarations at `10px` and `11px`,
so the off-scale values were the majority and the scale steps were the deviation.
Defining the ramp at the sizes the app actually used makes the odd ones out the ones
that change.

| Token | Size | Used for |
|---|---|---|
| `2xs` | `10px` | badges, timestamps, counts, meta |
| `xs` | `11px` | dense rows, picker items, toolbars |
| `sm` | `12px` | chat prose, control labels, the default body |
| `base` | `13px` | reading copy, paragraph text |
| `md` | `14px` | section headings, card titles |
| `lg` | `16px` | panel headings |
| `xl` | `20px` | the app's one true hero moment, if any |

Consequences to know before you use these: `text-sm` is **12px**, not Tailwind's
14px, and `text-xs` is **11px**, not 12px. Anything written expecting the default is
two steps too large.

**Code is always `font-mono`, and never scaled below `2xs`.** Diffs, JSON and payloads
are the payload of this app; they get the monospace stack and their own size, and they
keep their own colour rather than inheriting a component's.

## 4. Spacing and radius

Spacing is a 2px-multiple scale, and **components use the same gaps for the same
relationships**: 4px inside a control, 8px between controls in a group, 12px between
groups, 16px at a panel edge. If two components disagree about the gap between the
same two things, that is a bug.

| Token | Value | Used for |
|---|---|---|
| `sm` | `4px` | icon-to-label inside a control |
| `md` | `6px` | inset rows, nested chips |
| `lg` | `8px` | **the default control radius** |
| `xl` | `12px` | cards, the command bar, drawers |

`rounded-lg` is the default for anything a pointer lands on; `rounded-full` is for
circles only (status dots, avatars). Nesting a `lg` inside an `lg` looks wrong — step
down one level when a surface sits inside another surface of the same kind.

## 5. Components

Primitives live in `ui/src/components/ui/` and are the only sanctioned way to render
these things. A hand-written `className` that duplicates a primitive is a defect, not
a shortcut.

| Primitive | Owns |
|---|---|
| `Button` | tone, size, icon slot, disabled reasoning, focus ring |
| `IconButton` | square icon-only control, **must** carry `aria-label` |
| `Toggle` | the armed/at-rest distinction, and the feature's own armed hue |
| `Badge` | one of the five status tones, at `2xs` |
| `Panel` | surface, border, heading row, body padding |
| `Field` | label, control, hint and error, and the spacing between all three |

`Toggle` is the one that earns its place by fixing a bug rather than tidying: it is
the control the amber collision lived in, and a rule stated in a document cannot
stop a class string from being copied, but a rule stated in the only component a
toggle can be built from can.

Rules a component must satisfy without being asked:

- **Every interactive element is a real `<button>` or `<a>`.** A `div` with `onClick`
  is unreachable by keyboard and invisible to a screen reader. The first pass of this
  audit claimed 72 such sites; a precise scan found **zero** — the hits were all
  `Toggle` and `IconButton`, which are buttons. The rule stays because it is the
  defect that *would* be invisible, not because this codebase had it. A count this
  cheap to get wrong does not get written down.
- **Every icon-only control has an `aria-label`.** An icon that carries the only meaning
  is invisible to assistive tech and to a reader who cannot see it.
- **One icon set.** `lucide-react`, which is already a dependency. Never a text glyph
  (`✕`, `↻`, `…`) standing in for an icon.
- **Focus is always visible.** A control that cannot be tabbed to is a control that
  does not exist for half the users.
- **The disabled state is reasoned, not decorative.** A disabled control the user can
  see but cannot use must say why, on hover or beside it.

## 6. States, and what a component owes each

A component is not done when it renders. It owes a real state for each of:

- **Empty** — says what would be here and how to get it. Never a bare "No data".
- **Loading** — says what is being waited for. Skeletons for lists, a determinate
  affordance for a single unknown.
- **Error** — says what failed, in the engine's own words, and what the user can do.
  The rule this app already follows: never dress a failed discovery up as a diagnosis.
- **Long content** — wraps or truncates with a title. A path, a model id or a commit
  subject must never widen a toolbar.
- **Running** — if work is in flight, the control that stops it is on screen and
  enabled. A disabled spinner is not a stop button.

That last one is a contract, not a style note, and it is why `POST /goals/{id}/cancel`
is reachable from the command bar and not only from a goal card.

## 7. Motion

Under 200ms, and only for state that changed on screen. The existing
`prefers-reduced-motion` handling in `ui/src/index.css` is the model: users who opt out
get the instant state change with no animation at all, never a slower animation.

Nothing loops, nothing pulses to look alive, nothing moves that the user did not cause.

That bans *decoration*, not feedback. Five animations survive this rule, and each has
to survive it deliberately:

- **A streaming text cursor** (`▍` in the transcript) — a caret blinking because text is
  arriving. That is a caret.
- **A loading skeleton or busy spinner** — §6 Loading asks for "a determinate
  affordance for a single unknown", and that is one.
- **The logo** (`logo.gif`, rendered through `components/ui/Logo.tsx`) — the mark's
  caret blink. It is neither state nor feedback: it is the brand, the same sentence
  the wordmark beside it spells. The animation is a pre-rendered GIF, not a CSS loop
  the UI drives, and a GIF cannot read `prefers-reduced-motion` — so the component
  reads it on the GIF's behalf and swaps in `logo-static.gif`, the loop's resting
  frame. Opting out of motion still removes the animation entirely, which is the
  rule this entry exists to keep. The assets are generated by `scripts/make_logo.py`
  (run it by hand when the mark changes); the palette in that script quotes this
  file's hexes and must not diverge from them.
- **The Appearance rain** (`components/ui/RainBackdrop.tsx` over
  `components/ui/MatrixRain.tsx`, the OLED CMatrix theme's canvas effect — behind
  the whole window, and in the settings preview tile) — one of the animated
  *themes*, and it survives §7 for the reason the logo's cousin does: the user asked for it.
  Choosing a theme is the act; the rain is what was chosen, not a state pretending
  to be alive. It is bounded by the rest of this section: capped at 30 FPS, capped
  canvas size, and `prefers-reduced-motion` renders exactly one static frame — the
  trail, frozen — never a slower animation. Switching themes unmounts it outright
  rather than pausing it, because the rain belongs to a theme and a theme is
  replaced, not blended.

  **It is also feedback now, and that is a different exemption.** Every animated
  theme above runs **faster while a turn is in flight** — the rain falls at
  `ACTIVE_RATE`, 1.8×, eased in and out rather than switched. Until this, the
  weather was decoration with one exception: `NeuralWeb` brightened, and its prop
  docstring drew the line explicitly — "it changes nothing about how fast anything
  moves, which is the line between an effect that reports state and one that is
  decoration." That was the right worry attached to the wrong test, and this
  entry is the correction.

  §7's distinction is not between motion that changes *speed* and motion that
  does not. A streaming caret blinks at a constant rate and reports perfectly
  well. The distinction is between motion that **carries information the user did
  not already have** and motion that does not, and a rate that tracks the run's
  state is on the right side of that line: faster means working, it is learnable
  in one exposure, and it is confirmed by the Stop button and the live dot, so it
  is never the only channel. A rate that wobbled on a timer, or a "breath"
  nobody could learn, would look identical in code and would be decoration — which
  is exactly why the contract is written as *"it means something the user can
  name"*, not as a shape.

  Four properties hold it to that, and each is a test:

  - **The rate is a multiplier on simulated time, not on frames.** `fps` is the
    cost and does not change: a faster effect is not a busier one, and a capped
    canvas still moves at the same speed per second.
  - **It eases, never snaps**, on the same coefficient `NeuralWeb` already used
    for brightness, so the two reactions to one event settle at one pace. A jump
    in the corner of the eye, mid-sentence, is worse than no signal.
  - **One number, six effects.** `ACTIVE_RATE` and `nextRate` live in
    `useAtmosphereCanvas.ts` and the rain's separate loop imports them, so "what
    working looks like" cannot mean two things depending on the theme. The rain
    needs a sub-cell accumulator to run a fractional rate on an integer grid.
  - **It is the goal's own status.** `canStopGoal(activeGoal?.status)` — the same
    value that enables the Stop button — so the weather cannot disagree with the
    control that stops the run. A second flag would be a second truth.

  And `still` opts out of the whole thing by having no canvas, which is the clean
  answer for a user who wants no signal rather than a dimmer one.
  It is bounded in *place* by one rule, and the rule is about the **mount point**,
  not the canvas: the backdrop is mounted once by the shell (`App.tsx`), as the
  first child of the app's `relative` root, and nothing a conversation does can
  reach it. It was not always so. It used to live inside the transcript's empty
  state — a `max-w-2xl mx-auto` column rendered only while `messages.length === 0`
  — which produced two failures from one decision: the rain showed as a centred
  panel rather than a background, and it disappeared entirely the first time the
  user sent a message. A backdrop mounted inside the transcript cannot be behind
  the sidebar either, because the sidebar is the transcript's *sibling*: no
  ancestor of a centred column covers the window. So the placement is the
  contract. Two things make it hold — the chrome is glass
  (`.bg-codify-chrome`, the surface at 60% alpha) rather than paint, because an
  opaque top bar and sidebar hide the rain and re-create the centred look, and the
  content surfaces stay opaque, because reading over glass is not reading. The
  backdrop is decoration the user asked for; it is never decoration *underneath
  the words they are reading*, and it never moves anything, covers a control, or
  takes a click (`pointer-events-none`, `aria-hidden`).
- **The cyberpunk horizon** (`components/ui/CyberBackdrop.tsx` over
  `components/ui/CyberGrid.tsx`, plus the `.cyber-veil` class) — the second
  animated theme, and the same bargain as the rain rather than a new exemption.
  Choosing the theme is the act; the grid and the sun are what was chosen. It
  inherits every bound above without restating them: capped canvas (1024 behind
  the window, 120 in the preview tile), 30 FPS paced by an accumulator rather
  than by the display, and a full repaint per frame of about forty strokes
  instead of the rain's per-glyph trail. Two details are its own. The scroll is
  a modulo over fixed offsets rather than stored per-line state, so a resize
  cannot desynchronise the rungs;  and the CRT veil is **CSS**, not canvas, so
  that the opt-out sits in the same `prefers-reduced-motion` block as the
  audit flash — a canvas would have made the two opt-outs live in two files,
  and the second one is the one someone would forget. The veil's roll is a
  `transform` and not a `background-position`, which is the second half of that
  bargain: the offset version looked identical and cost a full-window repaint
  every frame at the display's rate, and the only way that showed up was a
  screenshot catching it mid-repaint. The stutter is on a fixed period, not a
  random one: a glitch a user cannot predict is a flicker they cannot read past.
  Its mount point is the rain's, and for the same reason: once, by the shell,
  as the first children of the app's `relative` root, as siblings rather than
  alternatives, each gated on its own variable. The two can never both be true.
- **The other four atmospheres** (`components/ui/WeatherBackdrop.tsx` over
  `NeuralWeb`, `HudSweep`, `AbyssSpores`, `NebulaFlow`) — four more themes'
  weather, and the reason the rules above are written as a *family* rather than
  repeated per effect. Three decisions carry all of them:
  1. **The clock is one hook.** `hooks/useAtmosphereCanvas.ts` owns the cap, the
     30 FPS accumulator, the single static frame, the resize handling and the
     live variable reads. A painter supplies drawing and nothing else, so the
     bounded budget is a thing that can be wrong in one place instead of six.
     The tests assert it there *and* assert that no painter runs its own loop,
     because "one clock" is only true if the hook is the only way to have one.
  2. **The gate is a variable, not an id.** `ATMOSPHERE_TRIGGERS` in
     `appearance.ts` and the table in `WeatherBackdrop.tsx` list the same
     pairing, and `ui/tests/atmosphere.test.ts` holds them against each other —
     so a theme that publishes a variable nobody watches fails the suite rather
     than shipping a theme whose canvas never mounts.
  3. **One of them reports state.** The constellation's web brightens and sends a
     ring outward while a run is in flight, driven by the goal's own status
     rather than a second flag. That is the line the whole family sits on: an
     effect may report what the app is doing, and it may not invent motion of
     its own to seem alive. It is a *ref*, not a prop on the hook's dependency
     list, because re-running the effect to pick up a boolean would restart a
     simulation that has drift.
- **Nothing else.** In particular the engine-health dot is steady: its colour already
  reports the connection, so a loop over it carried no information a reduced-motion user
  could receive. If you add a sixth, the burden is on you to say which of the two
  categories it falls into.
