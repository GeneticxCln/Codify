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

That bans *decoration*, not feedback. Three animations survive this rule, and each has
to survive it deliberately:

- **A streaming text cursor** (`▍` in the transcript) — a caret blinking because text is
  arriving. That is a caret.
- **A loading skeleton or busy spinner** — §6 Loading asks for "a determinate
  affordance for a single unknown", and that is one.
- **Nothing else.** In particular the engine-health dot is steady: its colour already
  reports the connection, so a loop over it carried no information a reduced-motion user
  could receive. If you add a fourth, the burden is on you to say which of the two
  categories it falls into.
