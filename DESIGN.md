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
| `border-strong` | `#66707c` | scrollbar thumbs, borders on hover/focus |

Three surfaces, not more. A fourth would mean a component could not say what it sits
on. `raised` exists because hover and "a control" are the same visual weight here.

### Text

| Token | Value | Used for |
|---|---|---|
| `primary` | `#e6edf3` | headings, the code you asked to read |
| `secondary` | `#c9d1d9` | body copy, chat prose |
| `muted` | `#979fa8` | metadata, timestamps, placeholders, disabled |

Never more than three weights of text colour in one component. If a fourth thing needs
emphasis, it is bigger or bolder, not a new grey.

**`muted` is the dimmest the app is allowed to go, and the floor is a measurement
rather than a taste.** It was `#8b949e` and every theme had its own equivalent dimmer,
and `ui/tests/contrast.test.ts` found the pair under 4.5:1 in sixteen of the nineteen
themes — most sharply on `border`, because a resting toolbar toggle hovers into a
`border` fill with this text on it, and a line colour is lighter than any surface a
line is drawn on. So `muted` is now the smallest value that clears **WCAG AA 4.5:1 on
`bg`, `surface`, `raised` and `border`**, in every theme, and a theme that wants a
dimmer grey wants a darker surface rather than a quieter label. The token's job is to
step back from `secondary`, not to step out of reading; where the two now crowd each
other, the honest fix is size or weight, which is the sentence above arriving on time.

### Status — five tones, five meanings

| Token | Value | Means exactly |
|---|---|---|
| `accent` | `#418cf8` | the one interactive hue: primary action, and the current selection in any list |
| `info` | `#418cf8` | in flight: planning or running |
| `success` | `#3fb950` | completed, passed, healthy |
| `warning` | `#d29922` | needs attention but is not wrong: paused, recording |
| `danger` | `#f85149` | failed, refused, destructive, or Stop |
| `neutral` | `#8b949e` | idle: pending, cancelled, or nothing to report |

`accent` and `info` are deliberately the same hue, and they are two tokens rather
than one on purpose: "press this" and "this is happening" are different sentences, and
a component should say which one it means. Sharing a value means the two can never
drift apart, while sharing a *name* would have made a control that reports state
indistinguishable from one you are meant to press.

**The blue was lifted from `#2f81f7`, once, deliberately.** Giving `accent` a
*resting* role — the send button, and the title of a selected row in a list — put
this hue on a `raised` fill, and the old value reads 4.06:1 there, under the 4.5:1
an 11px thread title is owed. `#418cf8` is the smallest step that clears it (4.59:1
on `raised`, 5.71:1 on `bg`), it is the same hue, and `info` moved with it rather
than staying behind: a "running" marker darker than the button you press is a
hierarchy nobody chose. `ui/tests/contrast.test.ts` is what makes this true — it
measures the pair the source cannot see, because the row's fill is on the container
and the title's colour is on a child of it.

**A status tone is never used decoratively.** If a control is not reporting a state, it
is grey, and that is not a lack of colour — it is the default.

**A theme may restate these six; a theme may not change what they mean.** The values
above are the *default theme's*, and the hexes are the ones
`ui/tailwind.config.js` compiles. They were also, until now, the only ones any theme
could ever have: a success pill in the OLED CMatrix app was `text-green-400`, a
saturated hue belonging to no palette the user had picked, and the badge layer did not
follow the theme at all. So a theme now publishes all six (`THEME_TONES` in
`ui/src/appearance.ts`) and the runtime layer re-points the classes.

Two rules hold that line, both pinned in `ui/tests/appearance.test.ts` rather than
left to good intentions:

- **All six or none.** `applyTheme` clears every managed variable before applying the
  next theme, so a theme that stated three would show the *previous* theme's other
  three — the failure clearing exists to prevent, arriving through its own door.
- **`danger` and `warning` stay on the warm arc.** A theme may restate failure in its
  own palette; it may not make failure cyan. The old rule said no theme could touch
  these at all, which protected a *meaning* by freezing a *habit* — and the habit was
  the part that was wrong. Monochrome themes say severity as brightness instead of hue,
  and that is a stated carve-out, not an accident of a grey having a hue of 0°: a theme
  whose six tones are all grey (ASCII Rain) is exempt from the warm arc and instead has to
  order them `danger` > `warning` > `success` > idle by a visible step of luminance, and a
  grey `danger` or `warning` in any theme that has colour fails. ASCII Rain had `warning`
  dimmer than `success`, which read as the quieter of the two. Because brightness is all it
  has, `Badge` also draws a glyph beside the word for the three severities (a tick, a
  triangle, a cross): failed, warning and done are never told apart by colour alone.

**Status ink: a tone as the text of a pill.** A pill is a tone as text on a tint of itself
(`bg-codify-danger/40 text-codify-danger`), and that pair is far closer in lightness than
either is to the surface behind it. Measured, red on its own 40% tint was under AA in 54 of
57 theme-and-surface combinations (worst 2.55:1), and the audit never saw it because it had
skipped every translucent background. So there is a derived token per tone,
`--codify-{accent,info,success,warning,danger,design,knowledge}-ink`, with a matching
`text-codify-X-ink` class: the tone moved in *lightness only* (hue and saturation kept, so
`danger` is still red) until it reaches 4.7:1 against its own 40% tint over `bg`, `surface`
and `raised`. `applyTheme` computes it for every theme, so a theme states nothing extra and a
tint of the accent gets its own ink. The rules, all in `ui/tests/contrast.test.ts` and
`ui/tests/ink.test.ts`:

- **A tint is read in its ink.** A class string with `bg-codify-X/N` and the same tone as text
  uses `text-codify-X-ink`. The bare tone stays for icons, borders and text on a plain surface.
- **No tint over 40% carries text.** The ink is derived against 40, which is the strongest
  resting tint; a weaker one is further from the text and so easier. Hover feedback on a tint is
  `hover:brightness-110`, or a step up to 40 at most.
- **Status text is never dimmed.** `text-codify-warning/90` composites the tone toward a surface
  the class string does not name, so no test can measure it, and emphasis comes from size or
  weight, not from a thinner colour.
- **Bare tones are measured on every surface.** Each of the seven is a declared pair on `bg`,
  `surface` and `raised` at 11px, so a theme that picks a tone too dark for `raised` fails.

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
tones *are* themed, and the rule about what they mean moved down into the status
table above, where a test holds it: a theme may restate failure, and it may not
make failure cyan.

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

**Five more themes, all of them margin effects.** Each publishes its own
namespace and its own canvas; the surfaces are here for the reason the snow
pair's are — `ui/tests/atmosphere.test.ts` reads every theme's swatch and fails
if this file has not published the value.

| Token | Solarized Flare | Cyber Organism | Event Horizon | Vector Wireframe | Liquid Mercury |
|---|---|---|---|---|---|
| `bg` | `#000000` | `#000000` | `#000000` | `#000000` | `#000000` |
| `surface` | `#0a0700` | `#0a192f` | `#09090b` | `#031008` | `#0b0d10` |
| `raised` | `#161000` | `#0f2540` | `#131318` | `#062012` | `#131720` |
| `border` | `#4a3800` | `#16385c` | `#1f1f26` | `#0d3b1f` | `#1e293b` |
| `border-strong` | `#785c00` | `#2c71b2` | `#606075` | `#1d763e` | `#4f6584` |
| `primary` | `#ffd700` | `#e0f7ff` | `#e4e4e7` | `#d6ffe6` | `#f1f5f9` |
| `secondary` | `#ffb000` | `#9fe8ff` | `#c084fc` | `#00ff66` | `#e2e8f0` |
| `muted` | `#c99f2d` | `#7aa5c3` | `#79899f` | `#5bab7d` | `#8291a5` |

All five are on pure black, and that is a shared decision rather than a
coincidence: four of the five confine themselves to the window's margins, and a
near-black surface is what lets a line of light at the edge read as *the edge*
rather than as a panel that happens to be dark.

Their weather variables, one namespace each, named for what the canvas draws
rather than for the theme — a theme's name changes and a canvas's job does not:

| Theme | Variable | Value | Used for |
|---|---|---|---|
| Solarized Flare | `--sun-core` | `#ffb000` | the flare's wide faint stroke, and the trigger |
| | `--sun-edge` | `#ffd700` | the thin bright core, and the heat pulse |
| Cyber Organism | `--organ-node` | `#00e5ff` | node cores, and the signals that travel the tendrils |
| | `--organ-tendril` | `#0077ff` | the branching tendrils and each node's halo |
| Event Horizon | `--void-arc` | `#c084fc` | the near side of the disc |
| | `--void-dust` | `#64748b` | the wide, faint outer orbits |
| | `--void-beam` | `#a855f7` | the radial jets while a run is in flight |
| Vector Wireframe | `--vector-line` | `#00ff66` | every edge of every solid, and the trigger |
| | `--vector-lock` | `#ff007f` | the locked vertices and the target reticle |
| Liquid Mercury | `--fluid-crest` | `#e2e8f0` | the thin highlight on each wave, and the trigger |
| | `--fluid-trough` | `#334155` | the thick body of the fluid under the highlight |

**One brief met deliberately against another.** Vector Wireframe asks for "green
*or* hot pink" vector lines, and it has both — the pink is `--vector-lock`, drawn
on the reticle and the vertices a run locks onto, which is what a vector arcade's
second colour is for. It is deliberately *not* that theme's `danger` tone: the
status rule above says failure stays on the warm arc so a user who learned one
theme's red recognises it in the next, and `#ff007f` is 325°, magenta. The danger
is `#ff3b5c`, a hot red that reads at a glance beside a mint "passed" on the same
screen. A brief and a contract disagreed here, and the contract won on the token
that means a state while the brief won on the token that means a colour.

**Three more margins, and the last table is the reason to read it twice.**
`Electric Arc`, `Toxic Lab` and `Anon Fluid` publish their surfaces here for
the reason every other theme's are: `ui/tests/atmosphere.test.ts` reads a
theme's swatch and fails if this file has not carried the value.

| Token | Electric Arc | Toxic Lab | Anon Fluid | Used for |
|---|---|---|---|---|
| `bg` | `#030310` | `#050a06` | `#000000` | app background, behind everything |
| `surface` | `#0a0a20` | `#0b1a0f` | `#03110a` | panels, the command bar card, drawers |
| `raised` | `#111132` | `#12271a` | `#061a10` | controls at rest, hover targets, inset rows |
| `border` | `#1e1e4a` | `#1d3d26` | `#0c3320` | dividers and resting control borders |
| `border-strong` | `#5757b9` | `#3b7947` | `#1e714c` | scrollbar thumbs, borders on hover/focus |
| `primary` | `#e8e8ff` | `#e2f5d9` | `#c8ffe0` | headings, the code you asked to read |
| `secondary` | `#8a7dff` | `#9dff3c` | `#00ff9c` | body copy, chat prose |
| `muted` | `#8787b2` | `#87aa72` | `#54a37d` | metadata, timestamps, placeholders |

Their weather variables, named for what the canvas draws rather than for the
theme — the fifth table above sets the rule and this one follows it:

| Theme | Variable | Value | Used for |
|---|---|---|---|
| Electric Arc | `--arc-core` | `#dfe4ff` | the discharge channel, and the trigger |
| | `--arc-fork` | `#8a7dff` | the spurs, and the wide faint halo under the channel |
| Toxic Lab | `--reagent` | `#9dff3c` | the body of each bubble, and the trigger |
| | `--reagent-skin` | `#d4ff7a` | the bright upper third of a bubble, and the burst ring |
| Anon Fluid | `--fluid-bit` | `#00ff9c` | the dashes, and the trigger |
| | `--fluid-cursor` | `#a8ffd8` | the one dash in eleven that marks a boundary |

**Each one is a different axis, because a second column of falling glyphs is
not a new theme.** The OLED rain falls, so `Anon Fluid` runs *sideways* along
the gutters and leaves the vertical axis to the transcript — which is also the
truer picture of a terminal reading a stream rather than printing a file.
`Toxic Lab` rises, and grows as it rises, because a bubble that keeps its size
to the ceiling reads as a particle emitter rather than as gas. `Electric Arc`
does not travel at all: it is an event with an afterimage, a hard decay rather
than a pulse, because a gentle fade reads as a glowing worm in the window.

**Two of the three are about what a colour is *for*, and that is why their
tones are the interesting part of the table.** The lab's green is a *substance*
— the reagent in the glass — so it cannot also be the interface's `success`,
and it certainly cannot be `danger`. `success` is therefore a mint (`#6ef0b0`),
`danger` is the orange-red of a hazard label (`#ff5630`) and `warning` is the
tape's amber (`#ffd23f`): three colours the eye has to separate at a glance in
a window that is otherwise one colour, and all three on the warm arc the status
rule requires. Its `accent` is `#b6ff2e`, the same reagent pushed one step
further, which is what the wordmark's gradient is for — a theme whose
signature colour is a *substance* wants the app's own interactive hue to be
more of it. `Anon Fluid` has the same problem in a different shape: a
phosphor green terminal that says "passed" in the same green as its data has
stopped using colour to say anything, so its `success` is a spring green
(`#39ff88`) held off the stream's own `#00ff9c`. `Electric Arc`'s accent is
`#8f7bff`, the violet of the arc's spurs, and its `danger` is a red rather than
that violet — a user who has learned that this window means "broken" by its
red should not have to relearn it in the one theme here that is mostly blue.

**The two snow themes' surfaces**, published here for the same reason the
other themes' are: `ui/tests/atmosphere.test.ts` reads a theme's swatch and
fails if this file does not carry the value, so a surface a reader cannot look
up is a surface this document has not finished describing.

| Token | Winter Snow | Festive Night | Used for |
|---|---|---|---|
| `bg` | `#050a14` | `#080d09` | app background, behind everything |
| `surface` | `#0a1220` | `#0e1a11` | panels, the command bar card, drawers |
| `raised` | `#0f1a2e` | `#15271a` | controls at rest, hover targets, inset rows |
| `border` | `#16263f` | `#1d3a25` | dividers and resting control borders |
| `border-strong` | `#3a689a` | `#3e794c` | scrollbar thumbs, borders on hover/focus |
| `primary` | `#eaf4ff` | `#f6f2e6` | headings, the code you asked to read |
| `secondary` | `#c3d9f0` | `#d9d3bf` | body copy, chat prose |
| `muted` | `#758fae` | `#989f8d` | metadata, timestamps, placeholders |

Both are dark for the reason §2 opens with: the app is a full-screen window
beside an editor. Winter Snow is blue-black and Festive Night green-black, and
their text is cold white against warm cream — which is the difference you
notice with snow in the window, not the snow.

**Two themes, one snowfall.** `winter-snow` and `festive-night` publish
`--snow-flake` and are drawn by a single `components/ui/SnowFall.tsx`, the way
`cmatrix-oled` and `ascii-rain` share `RainBackdrop` and `MatrixRain`. The
difference between the two is entirely in what they publish:

| Theme | Variable | Value | Used for |
|---|---|---|---|
| Winter Snow | `--snow-flake` | `#e8f4ff` | the near flakes, and the trigger that mounts the canvas |
| | `--snow-flake-2` | `#a9c8ea` | the paler, bluer flakes the field mixes in for depth |
| Festive Night | `--snow-flake` | `#f2f6f0` | the near flakes, and the trigger |
| | `--snow-flake-2` | `#cddcce` | the faintly green flakes under the lights |
| | `--snow-glow` | `#ffc85c` | the warm lights rising through the snow, and the gold accent |
| | `--snow-glow-2` | `#e2564d` | the second light colour, and the berry danger |

So the two are one component and four variables rather than two components. A
theme that publishes **no** `--snow-glow` gets no lights, and that absence is
what makes Winter Snow quiet and Festive Night lit — the painter never asks
which theme it is running, and a tenth snowfall would be a data entry. The
alternative, a second canvas that shares most of its drawing, is the sixth place
in this file where the same weather would be written twice.

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
surface, `#26261f` raised, `#3a3a33` border, `#707064` strong border,
`#f7f6f2` primary, `#d6d5cc` secondary, `#a4a49c` muted — the highest
`primary`-on-`raised` contrast of any theme here, and no saturated hue anywhere
in it. With nothing to look at, contrast is the whole design, and a colour
carrying no meaning is a colour competing for the attention the theme exists to
give back. The strong border is the dimmest warm grey that clears SC 1.4.11's
3:1 against all three surfaces — `ui/tests/nonTextContrast.test.ts` gates it,
and `stillTheme.test.ts` holds every token to a channel spread of 12, which is
why the lift stops at `#707064` rather than going brighter.

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

### Custom colours — the accent and the weather, never the ground

Nineteen themes, and a user who wants Liquid Mercury's fluid dynamics in deep
crimson should have them. The picker (`ui/src/tint.ts`, surfaced in the
Appearance pane) is the smallest thing that does that: it offers each theme's
`accent` plus **the two or three weather variables that theme's own painter
reads**, labelled with what they draw ("Bubble body", "Discharge channel",
"Wave highlight"), and a proposal is a hex in a custom property.

Three rules make it safe, and each of them exists because the alternative is a
bug report.

**Nothing here changes the physics.** No painter's arithmetic is touched. Every
painter resolves its variables through the hook's live `getComputedStyle` read on
the frame it draws, so a tint is on screen within one frame and a running canvas
costs nothing extra to recolour — which is the whole performance claim, and it
is only true because the painters moved their reads out of their constructors.
It was not true before, and `ui/tests/tint.test.ts` now paints a real painter
across two frames with the variable changing in between, because a docstring
saying so is not a test.

**The surfaces are not offered, and the backgrounds were deliberately not
flattened.** An earlier request for this feature asked for every background
pinned to `#000000`. Seven of the nineteen themes are not black, and their
backgrounds are load-bearing — Liquid Mercury's chrome *is* the chrome, Abyss
is a deep field the spores are meant to float in — so pinning would have deleted
seven palettes rather than customised nineteen. What is left is the guard: a
proposal keeps its **hue and saturation** and has its **lightness moved** until
it clears 4.5:1 on all three surfaces (`--codify-accent` ends up on a label, so
it is held to the text floor) or 3:1 on the two for a weather variable, which
paints a line or a particle and is held to the 1.4.11 graphic floor instead. The
swatch shows the *resolved* colour and says what it was and what it scored,
because a well showing a colour the screen does not have is worse than a
clamp the user did not ask for.

**A proposal that cannot be made readable is refused, not approximated.** There
is no lightness of any hue that is 4.5:1 from a near-black and a near-white panel
at once, and the answer is the theme's own value with an explanation. The clamp
compares against the *brightest* surface, not the mean: against a mean on a dark
theme every colour sits above it, so a deep crimson was told to get darker,
walked the whole sweep without passing, and handed back the last candidate it
had tried — a near-white, returned from the function whose entire job is to not
do that. `ui/tests/tint.test.ts` pins that case by name.

What stays untouchable is the list that would make the guard a suggestion:
`primary`, `secondary` and `muted` are the surfaces everything else is measured
against; the five status tones carry meanings learned in one theme and
recognised in the next; and `bg`, `surface` and `raised` are the ground. A theme
with no atmosphere offers its accent alone, which is the honest answer rather
than a picker offering colours that theme does not publish.

#### Undo, and why a drag is one step

A colour well is a native picker, and dragging its saturation slider fires a
change event per step — a single gesture is dozens of commits. `ui/src/
tintHistory.ts` collapses consecutive changes to the *same* variable inside
700 ms into one entry, and the entry it keeps is the state from **before the
gesture**, not before its last frame. The tempting implementation overwrites the
whole entry on each frame, which satisfies every other test and makes Undo move
the colour by one frame: the user presses it sixty times and gives up first.

Everything that changes a tint goes through the pane's one `commit`, so a fourth
path added later is undoable without anyone remembering — a per-row reset, a
whole-theme reset, and an imported scheme all are. The stack holds whole-store
snapshots rather than diffs, twenty-five deep, dropping the *oldest*: dropping
the newest would make editing silently stop being undoable with nothing on
screen to say so. An entry that has no burst key — a reset, an import — never
coalesces, because neither is a gesture that produces a stream of itself.

It is deliberately **not** in `localStorage`. Undo answers "I mis-dragged", which
is a thing that happened in the last few seconds; the store persists, the way
back to it does not, and a restart is a fine time to forget. For the same reason
an external write — another window, devtools — **clears** the stack rather than
keeping it: every entry holds a store from before something, and once the
something has been replaced from outside, undoing would silently discard a
change the user did not make.

The button names the change it would take back rather than reading "Undo", which
is also the only way to discover that the thing you are about to reverse was on
*another* theme — the stack is global, so pressing it while looking at an
untinted theme would otherwise appear to do nothing at all.

#### The scheme file, and what a format is for

Custom colours can leave the app as one file (`ui/src/scheme.ts`), and the
interesting part is not the JSON. The store is keyed by **theme id** —
`toxic-lab` — because that is the only key the app can resolve without asking a
human, and that is exactly what makes it unreadable to a person deciding whether
to trust a file someone sent them. So a scheme carries a **label** beside every
id, a **version**, and a name derived from its own contents. The id is the exact
match; the label is what a human reads and what they would retype.

Three rules make it survive two different installs, each because the obvious
alternative is a file that only works between identical ones:

- **Match on id, then label.** A scheme from a build that renamed a theme, or
  whose id later changed, is still about Toxic Lab and still applies.
- **Report the entries that could not.** A scheme that half-applied and said
  nothing looks exactly like one that fully applied, and the skipped half is
  named — "This build has no Holo Deck 2099" — because the person who needs to
  know is the one holding both installs.
- **Refuse, rather than half-read.** Not-a-scheme, from-a-newer-version, and
  valid-but-empty are three different sentences, and the last is refused because
  the most confusing outcome of an import is the app looking exactly as it did
  before.

The import **replaces within a theme and merges across themes**, and stores
values **raw**: the local contrast guard runs on arrival, not the sender's, so a
colour that is fine on their near-black theme is judged against *this* machine's
surfaces rather than arriving pre-clamped against a ground it will never be
painted on. Re-importing an export changes nothing, which is what makes sharing
safe to do twice.


### Armed, selected, and at rest

This is the distinction that was missing, and it is the one that most often went
wrong. Three separate ideas were all being drawn as "a coloured pill":

| State | How it is drawn |
|---|---|
| **Armed** (a toggle you have switched on) | the tone's `/20` background, `/50` border, and the tone's **ink** as text |
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
| `2xs` | `0.625rem` | 10px: badges, timestamps, counts, meta |
| `xs` | `0.6875rem` | 11px: dense rows, picker items, toolbars |
| `sm` | `0.75rem` | 12px: chat prose, control labels, the default body |
| `base` | `0.8125rem` | 13px: reading copy, paragraph text |
| `md` | `0.875rem` | 14px: section headings, card titles |
| `lg` | `1rem` | 16px: panel headings |
| `xl` | `1.25rem` | 20px: the app's one true hero moment, if any |

Consequences to know before you use these: `text-sm` is **12px**, not Tailwind's
14px, and `text-xs` is **11px**, not 12px. Anything written expecting the default is
two steps too large.

**The sizes are rem, and the px in the table is the size at a 16px root.** That is what
makes the UI scale work: Settings → Appearance (and Ctrl +, Ctrl -, Ctrl 0) sets one
percentage on `<html>`, and everything written in rem, which is the type ramp, the
radii, and Tailwind's spacing, grows with it. So **a size is never a `text-[Npx]`**.
That would be the one run of text that stays small while the window grows around it.
Line heights are rem for the same reason: text that grew inside a fixed-px line box
overlaps itself. The default scale is 125%, so the table's `sm` is 15px on screen
out of the box.

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
| `sm` | `0.25rem` | 4px: icon-to-label inside a control |
| `md` | `0.375rem` | 6px: inset rows, nested chips |
| `lg` | `0.5rem` | 8px: **the default control radius** |
| `xl` | `0.75rem` | 12px: cards, the command bar, drawers |

Radii are rem, like the type ramp, so a scaled window keeps its proportions.

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
| `IconButton` | square icon-only control, **must** carry `aria-label`; its padding reset is `!p-0`, because `Button`'s own `px-*` outranks a plain `p-0` in the stylesheet and squeezes the icon to a sliver |
| `Toggle` | the armed/at-rest distinction, and the feature's own armed hue |
| `Badge` | one of the five status tones, at `2xs`, in the tone's ink; success, warning and danger carry a glyph (`icon={false}` for a count or a tag) |
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
- **The eight margin effects** — `SolarWind.tsx` (amber flares down the gutters),
  `CyberOrganism.tsx` (nodes and branching tendrils in the margins),
  `EventHorizon.tsx` (tapered streaks tracing orbits of a disc centred on the
  window), `VectorWire.tsx`
  (rotating 1px wireframe solids), `Nanofluid.tsx` (viscous metallic waves
  climbing the margins), `ElectricArc.tsx` (forked discharges striking down
  both gutters), `ToxicLab.tsx` (reagent bubbles rising, growing, and popping
  at the ceiling) and `AnonFluid.tsx` (data running *sideways* along the
  gutters) — one bullet because they share a shape, and the shape is
  the thing worth a contract. Each is an animated theme and each survives §7 the
  way the rain does: the user asked for it by choosing it, and each is bounded
  identically by `hooks/useAtmosphereCanvas.ts` — 1024 device pixels, 30 frames
  a second, and **one** static frame under `prefers-reduced-motion`.

  Two shared rules, both of them about the middle of the window. **None of the
  eight paints anything across the middle**, because that is where the conversation
  is and a bright moving line across somebody's code is the worst legibility
  failure in the app. What differs is how the disc gets there: the other seven
  never compute anything past their gutter, while `EventHorizon` orbits a centre
  in the middle of the window, so its *geometry* spans the full window and reaches
  past every edge even though its output does not — which is what gives it a limb
  to flare jets from, and why it carries the faintest vignette of the set. And
  **every one of them fades out as it approaches the middle** rather than being
  clipped at an edge, because a hard cutoff reads as a bar across the screen
  where a fade reads as distance.

  What each does while a turn is in flight is deliberately *different*, because
  eight identical pulses would be one effect with eight palettes: a heat band
  crosses the gutters, signals run along the tendrils, the disc doubles its
  orbital rate and fires jets, extra chords appear and a reticle locks onto one
  solid, the fluid gains a second higher-frequency term so it breaks up
  rather than merely speeding up, the arcs strike more often and hold their
  charge twice as long, the bubbles pop more often, and the data stream
  *shears* — a band of rows displaced sideways and drawn in blocks, which is
  the one state report here that changes the picture's shape rather than its
  rate. All eight are information — a run is in flight —
  which is the side of §7's line that motion has to be on to be allowed.
- **The snowfall** (`components/ui/SnowFall.tsx`, behind the whole window, and in
  the settings preview tile) — Winter Snow and Festive Night, and the reason
  they are one bullet rather than two. It is an animated *theme* and it survives
  §7 the way the rain does: the user asked for it by choosing it, and it is
  bounded like every other one here — the hook in `hooks/useAtmosphereCanvas.ts`
  caps the canvas at 1024 device pixels, releases at most 30 frames a second, and
  renders **exactly one** static frame under `prefers-reduced-motion`, because a
  1 FPS snowfall is still a snowfall.

  Three things it is careful about, each of which is a way this could have been
  decoration without meaning to be. The drift is a *shared* wind, so the field
  leans together rather than being 96 flakes each going their own way — a
  hailstorm is not weather, it is a particle emitter. The flakes are three
  depth tiers, so the field has depth rather than being one disc repeated at
  three sizes; a random radius per flake is what produces a few so large they
  read as bugs. And the lights in the Festive Night variant *twinkle* — they are
  the only thing here with a rhythm a person could learn, which is the standard
  §7 sets for motion that is not reporting state, and they earn it by being the
  thing that makes that theme different from its own sibling.
- **The wordmark** (`components/ui/Wordmark.tsx`, filling the empty transcript) — the
  app's name, in its own letters, with a band of light travelling across it on a 9s
  loop. It is neither state nor feedback: it is the brand, and it is the same sentence
  `logo.gif` spells at 24px, which is why the two exemptions are the same one. It
  earns this by being a *logotype* — an animated gradient clipped to glyphs is how
  type is set, and the alternative at the centre of an empty window was a paragraph
  explaining the pipeline to someone who had not asked for a pipeline. It is set
  **thin** — `font-weight: 200` — because it sits over whatever weather the active
  theme is drawing, and an extrabold logotype at that size reads as a slab laid over
  the rain rather than as type set in it. The component's own docstring had been
  calling itself "thin by design" while the stylesheet said 800, so the two disagreed
  for as long as both existed and the stylesheet was the one rendering.
  The `prefers-reduced-motion` answer is the logo's: the band is parked off the glyphs
  rather than slowed, so the gradient wordmark is what remains. It matches every
  theme by *reading* one — every colour is `rgb(var(--codify-*-rgb) / …)` and there is
  no `switch` on the theme id — so the same rules give phosphor green, magenta, or flat
  white depending on what is active, and no new rule is needed for the seventeenth theme.
  `ui/tests/wordmark.test.ts` holds both halves, because neither was true by
  accident: the weight because a docstring is not a stylesheet, and the theme-reading
  because a hex literal in one gradient stop renders the wrong colour in all sixteen
  themes and nothing else in the suite would have said so.
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
- **The other four full-window atmospheres** (`components/ui/WeatherBackdrop.tsx` over
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
- **Affordability is measured, never assumed.** A 30 FPS full-window effect is a budget
  the machine has to be able to meet, and on some machines it cannot: WebKitGTK composites
  in software when it has no dma-buf path (an Nvidia/Wayland session, a VM), and there a
  full-surface repaint per frame costs about a whole core *in the UI process*, which stops
  the main loop dispatching input — a window that has not crashed and will not answer, which
  from a chair is the same thing. Nothing inside the page sees that cost: the painter is
  cheap, and `rAF` keeps delivering frames because the web process has already committed
  them. What a loop *can* see is the interval the compositor actually delivers, so both
  loops hand every frame to `makeFrameBudget` and stop animating when the mean over a window
  is `SLOW_FRAME_FACTOR` (4×) the requested interval — once, with the measurement in the
  log, leaving the last frame drawn. The numbers are contract, not tuning: 24 frames is
  0.8 s of evidence at 30 FPS, a 2 s warm-up keeps the app's own startup from being read as
  a verdict, and an interval over `SUSPENDED_FRAME_MS` is treated as a suspended window
  rather than a slow one, because every user who ever switched away would otherwise keep a
  still backdrop for the rest of the session.

  **The half this does not cover, and how it showed up.** On the checkout where this was
  measured, the frame budget never fires: `rAF` is delivered quickly while the UI process
  rasterises, so the page cannot tell that its own decoration is what is starving the
  window. Measured there, the app burned ~97% of a core at idle, and **~33% with the
  effects stopped** (`gtk-enable-animations=false`, which the page reads as
  `prefers-reduced-motion`) — so the relief valve works and the automatic verdict does not
  reach that machine. A compositor-aware verdict has to come from the side that pays the
  cost, which is the shell; until then, the budget above covers the machines whose frames
  are throttled and the motion preference covers the rest.
- **Nothing else.** In particular the engine-health dot is steady: its colour already
  reports the connection, so a loop over it carried no information a reduced-motion user
  could receive. If you add a sixth, the burden is on you to say which of the two
  categories it falls into.
