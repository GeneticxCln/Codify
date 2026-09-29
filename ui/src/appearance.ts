/**
 * The themes module: what "Appearance" in settings switches.
 *
 * A theme here is *data* — a map of CSS custom property names to values —
 * applied to the document root at runtime. That is why these hexes are not
 * Tailwind tokens: `ui/tailwind.config.js` is compile-time and its colour
 * table is frozen to the brand contract by `ui/tests/designTokens.test.ts`,
 * while a theme has to swap the same property names per user choice without
 * a rebuild. `--codify-*` names are reused deliberately: every utility class
 * already reads them, so applying a theme repaints the whole app with zero
 * component changes — and the default theme sets the same values `:root`
 * does in `ui/src/index.css`, so "dark" and "no theme chosen" are the same
 * picture and cannot drift apart.
 *
 * The OLED CMatrix theme is the second one, and the reason the module
 * exists: pure black `#000000` background, rain glyphs in deep dark green
 * `#003300`, and the high-contrast light green `#a3ffb8` exported as a CSS
 * variable (`--cmatrix-text`) as well as being the theme's body text.
 *
 * `cyberpunk-neon` is the third: an animated one too, but its motion is a
 * horizon grid and a banded sun, so it publishes `--cyber-*` variables instead
 * and its canvas is `components/ui/CyberGrid.tsx`. A theme that wants weather
 * is a token edit plus a backdrop, not a new mechanism — which is the whole
 * reason the managed list below is data. Five more followed (neural, hud,
 * abyss, flare, ascii), and the last of them needed no canvas at all: see
 * `ASCII_RAIN`.
 *
 * Nothing here touches `window` at module scope — settings state must stay
 * importable from `node --test` (see `tsxLoader.ts`'s `localStorage` note) —
 * so storage reads happen inside functions, with the store injectable.
 */

/** The CSS custom properties a theme may set. `applyTheme` clears exactly
 * these before applying a theme, so switching from OLED back to dark cannot
 * leave a green `--codify-bg` behind. The `-rgb` companions are derived from
 * the hexes by `hexChannels` — they exist because Tailwind's generated
 * utilities bake literal `rgb(13 17 23 …)` at build time, and the override
 * rules in `ui/src/index.css` need channel triplets to rebuild those colours
 * with an alpha (`rgb(var(--codify-border-rgb) / 0.6)`). */
export const MANAGED_VARS = [
  "--codify-bg",
  "--codify-surface",
  "--codify-raised",
  "--codify-border",
  "--codify-border-strong",
  "--codify-primary",
  "--codify-secondary",
  "--codify-muted",
  "--codify-bg-rgb",
  "--codify-surface-rgb",
  "--codify-raised-rgb",
  "--codify-border-rgb",
  "--codify-border-strong-rgb",
  "--codify-primary-rgb",
  "--codify-secondary-rgb",
  "--codify-muted-rgb",
  // The one non-colour name in the list, and the reason it is legal: the ASCII
  // rain publishes the glyphs it draws, so a monochrome theme is a token edit
  // rather than a forked component. `applyTheme` only derives a `-rgb` triplet
  // for `#rrggbb` values, so this one is set verbatim and nothing tries to
  // parse it as a colour.
  "--cmatrix-glyphs",
  "--cmatrix-bg",
  "--cmatrix-rain",
  "--cmatrix-text",
  // Every hex a theme publishes has a derived triplet, and `applyTheme` writes
  // that triplet whatever the name is — so the triplets have to be managed or
  // they survive a switch. `--cmatrix-*-rgb` was missing for the OLED theme
  // from the day it shipped; nothing read them yet, which is the only reason it
  // was invisible. The names below are the same fact for the new namespaces,
  // written out rather than computed, because this list is read by a person.
  "--cmatrix-bg-rgb",
  "--cmatrix-rain-rgb",
  "--cmatrix-text-rgb",
  // The five status tones, and the one interactive hue they share. These used
  // to be unreachable from here — `applyTheme` could not clear a variable no
  // theme published, and Tailwind baked their hexes at build time, so a badge
  // in the OLED theme was the same orange it had always been. A theme
  // repainting them is now a legal edit, and the meaning is protected by a
  // test rather than by this list being short: see `THEME_TONES`.
  "--codify-accent",
  "--codify-info",
  "--codify-success",
  "--codify-warning",
  "--codify-danger",
  "--codify-neutral",
  "--codify-accent-rgb",
  "--codify-info-rgb",
  "--codify-success-rgb",
  "--codify-warning-rgb",
  "--codify-danger-rgb",
  "--codify-neutral-rgb",
  "--cyber-cyan",
  "--cyber-magenta",
  "--cyber-amber",
  "--cyber-scan",
  "--cyber-cyan-rgb",
  "--cyber-magenta-rgb",
  "--cyber-amber-rgb",
  "--cyber-scan-rgb",
  "--neural-web",
  "--neural-node",
  "--neural-pulse",
  "--neural-web-rgb",
  "--neural-node-rgb",
  "--neural-pulse-rgb",
  "--hud-amber",
  "--hud-tick",
  "--hud-amber-rgb",
  "--hud-tick-rgb",
  "--abyss-spore",
  "--abyss-deep",
  "--abyss-spore-rgb",
  "--abyss-deep-rgb",
  "--flare-indigo",
  "--flare-ultraviolet",
  "--flare-indigo-rgb",
  "--flare-ultraviolet-rgb",
  // The snowfall. `--snow-flake` is the trigger both snow themes publish;
  // `--snow-flake-2` is an optional second flake colour; `--snow-glow` and
  // `--snow-glow-2` are the *only* difference between the two of them, and
  // publishing them is what puts warm lights in the field. See `SnowFall.tsx`
  // for why a theme's weather is a variable and not a branch.
  "--snow-flake",
  "--snow-flake-2",
  "--snow-glow",
  "--snow-glow-2",
  "--snow-flake-rgb",
  "--snow-flake-2-rgb",
  "--snow-glow-rgb",
  "--snow-glow-2-rgb",
  // Five more namespaces, one per weather below. Each is named for what it
  // draws rather than for its theme, because a theme's name changes and a
  // canvas's job does not — and because `--flare-*` was already taken by Solar
  // Flare when the amber CRT theme needed a name.
  "--sun-core",
  "--sun-edge",
  "--organ-node",
  "--organ-tendril",
  "--void-arc",
  "--void-dust",
  "--void-beam",
  "--vector-line",
  "--vector-lock",
  "--fluid-crest",
  "--fluid-trough",
  // Three more namespaces, for the three themes added after `Liquid Mercury`.
  // Same rule as the five above them: the name is for what the canvas draws.
  // The arc is a channel and its spurs, the lab is a reagent and the skin on
  // its bubbles, and the anon theme is the stream and the one brighter cell
  // that marks a boundary in it.
  "--arc-core",
  "--arc-fork",
  "--reagent",
  "--reagent-skin",
  "--fluid-bit",
  "--fluid-cursor",
  "--sun-core-rgb",
  "--sun-edge-rgb",
  "--organ-node-rgb",
  "--organ-tendril-rgb",
  "--void-arc-rgb",
  "--void-dust-rgb",
  "--void-beam-rgb",
  "--vector-line-rgb",
  "--vector-lock-rgb",
  "--fluid-crest-rgb",
  "--fluid-trough-rgb",
  "--arc-core-rgb",
  "--arc-fork-rgb",
  "--reagent-rgb",
  "--reagent-skin-rgb",
  "--fluid-bit-rgb",
  "--fluid-cursor-rgb",
] as const;

export type ManagedVar = (typeof MANAGED_VARS)[number];

/** One theme: an id, what a person calls it, and the properties it sets. */
export interface AppearanceTheme {
  id: string;
  label: string;
  /** One sentence on what choosing it does — shown under the label. */
  description: string;
  /** The CSS custom properties this theme sets, `--name` → value. */
  tokens: Partial<Record<ManagedVar, string>>;
  /**
   * The variables the settings tile prints under the description, in order.
   * Data, not a `switch` in the pane: the pane rendered a different sentence
   * per theme, which is how a fourth theme's footer ends up showing the third
   * theme's hexes. A label is optional and is only there where the bare hex is
   * ambiguous — OLED CMatrix's line has always read `rain #003300`, and losing
   * the word would make the pane less useful, not more uniform.
   */
  swatch?: ReadonlyArray<{ v: ManagedVar; label?: string }>;
}


/**
 * The default: the brand contract, restated as runtime values.
 *
 * Every value is quoted from `ui/tailwind.config.js` / DESIGN.md §2 — this is
 * a third view of the same eight hexes, not a new decision.
 *
 * This theme is also the one that *declines* to repaint the five status tones,
 * which is the default answer for every theme that has nothing deliberate to
 * say about them: its rows in `THEME_TONES` are the §2 hexes, which is what
 * Tailwind compiled before a theme could reach them.
 */
export const CODIFY_DARK: AppearanceTheme = {
  id: "codify-dark",
  label: "Codify Dark",
  description: "The default contract: blue-grey surfaces, green reserved for status.",
  tokens: {
    "--codify-bg": "#0d1117",
    "--codify-surface": "#161b22",
    "--codify-raised": "#21262d",
    "--codify-border": "#30363d",
    "--codify-border-strong": "#66707c",
    "--codify-primary": "#e6edf3",
    "--codify-secondary": "#c9d1d9",
    "--codify-muted": "#979fa8",
  },
  swatch: [
    { v: "--codify-bg" }, { v: "--codify-surface" }, { v: "--codify-border" },
  ],
};

/**
 * OLED CMatrix: pure black, dark green rain, light green text.
 *
 * `--cmatrix-rain` and `--cmatrix-text` are the variables the requirement
 * names: the canvas component (`components/ui/MatrixRain.tsx`) draws with
 * them by default, and `#a3ffb8` is exported both as `--cmatrix-text` and as
 * the theme's `--codify-secondary`, so the whole app's body text becomes the
 * high-contrast light green rather than only the preview.
 */
export const CMATRIX_OLED: AppearanceTheme = {
  id: "cmatrix-oled",
  label: "OLED CMatrix",
  description: "Pure black on OLED-grade panels, with the rain drawn in #003300.",
  tokens: {
    "--codify-bg": "#000000",
    "--codify-surface": "#020806",
    "--codify-raised": "#061009",
    "--codify-border": "#0d2417",
    "--codify-border-strong": "#2f6b43",
    "--codify-primary": "#dcffe4",
    "--codify-secondary": "#a3ffb8",
    "--codify-muted": "#619272",
    "--cmatrix-bg": "#000000",
    "--cmatrix-rain": "#003300",
    "--cmatrix-text": "#a3ffb8",
  },
  swatch: [
    { v: "--cmatrix-bg" }, { v: "--cmatrix-rain", label: "rain" }, { v: "--cmatrix-text", label: "text" },
  ],
};

/**
 * Cyberpunk Neon: a violet-black night, a neon horizon grid, a banded sun.
 *
 * The third theme, and the first one whose animation is *lines* rather than
 * glyphs — which is why it publishes four variables of its own and gets a
 * different canvas (`components/ui/CyberGrid.tsx`). `--cyber-cyan`,
 * `--cyber-magenta` and `--cyber-amber` are the grid, the sun's lower band and
 * its upper band; `--cyber-scan` is the CRT veil, which lives in CSS rather
 * than on the canvas.
 *
 * One deliberate difference from OLED CMatrix: there, `--cmatrix-text` is also
 * the theme's `--codify-secondary`, because the rain's head glyph *is* body
 * text and two copies of that colour would be two decisions that could drift.
 * Here the bright pixels are lines and a sun, which nothing in the transcript
 * has to be legible against, so the app's text stays a lavender that reads
 * over the grid at 1440p — coupling them would make the theme harder to read
 * for no anti-drift gain.
 */
export const CYBERPUNK_NEON: AppearanceTheme = {
  id: "cyberpunk-neon",
  label: "Cyberpunk Neon",
  description: "Violet-black, with a neon horizon grid and a banded sun in cyan and magenta.",
  tokens: {
    "--codify-bg": "#0a0616",
    "--codify-surface": "#150b28",
    "--codify-raised": "#22123d",
    "--codify-border": "#3a1a63",
    "--codify-border-strong": "#8543cd",
    "--codify-primary": "#f6efff",
    "--codify-secondary": "#d9c8ff",
    "--codify-muted": "#a089c7",
    "--cyber-cyan": "#00e5ff",
    "--cyber-magenta": "#ff2e9a",
    "--cyber-amber": "#ffb02e",
    "--cyber-scan": "#1b0a35",
  },
  swatch: [
    { v: "--cyber-cyan", label: "grid" }, { v: "--cyber-magenta" }, { v: "--cyber-amber" },
  ],
};

/**
 * Neural Constellation: a slow web of points on pure black.
 *
 * The lines are the theme's own low end (`#1e293b`) and the nodes its high
 * (`#38bdf8`), so the effect and the chrome quote one ramp. It is the only
 * theme here whose animation reports something: `NeuralWeb` brightens and
 * sends a ring outward while the agent is working, which is why the app passes
 * it a live flag rather than leaving it purely decorative.
 */
export const NEURAL_CONSTELLATION: AppearanceTheme = {
  id: "neural-constellation",
  label: "Neural Constellation",
  description: "Pure black, with a slow web of nodes that brightens while the agent works.",
  tokens: {
    "--codify-bg": "#000000",
    "--codify-surface": "#060a14",
    "--codify-raised": "#0b1220",
    "--codify-border": "#16233a",
    "--codify-border-strong": "#48628d",
    "--codify-primary": "#e0f2fe",
    "--codify-secondary": "#bae6fd",
    "--codify-muted": "#7d8ca1",
    "--neural-web": "#1e293b",
    "--neural-node": "#38bdf8",
    "--neural-pulse": "#0ea5e9",
  },
  swatch: [
    { v: "--neural-web" }, { v: "--neural-node" }, { v: "--neural-pulse" },
  ],
};

/**
 * Cyberpunk HUD: amber instrumentation, mostly stationary.
 *
 * Two amber variables rather than one: the elements are one hue, but a readout
 * that is a single value at a single brightness is not an instrument. The brief
 * put its 15% on the elements, and that alpha lives in the painter, not baked
 * into a hex — see `HudSweep.tsx`.
 */
export const HUD_TACTICAL: AppearanceTheme = {
  id: "hud-tactical",
  label: "Cyberpunk HUD",
  description: "A flight deck's furniture: amber brackets, two slow sweeps, a tick ladder.",
  tokens: {
    "--codify-bg": "#000000",
    "--codify-surface": "#0b0a06",
    "--codify-raised": "#14110a",
    "--codify-border": "#2a2213",
    "--codify-border-strong": "#765d26",
    "--codify-primary": "#fef3c7",
    "--codify-secondary": "#fde68a",
    "--codify-muted": "#a7854d",
    "--hud-amber": "#f59e0b",
    "--hud-tick": "#fef3c7",
  },
  swatch: [
    { v: "--hud-amber" }, { v: "--hud-tick" },
  ],
};

/**
 * Bioluminescent Abyss: spores in the gutters and nothing in the middle.
 *
 * The window's centre is the transcript, so this theme's whole design is a
 * constraint about where things are *not*. The emerald ramp runs dark to light
 * the other way from the others: the spores are dim, the text is the bright end.
 */
export const BIOLUMINESCENT_ABYSS: AppearanceTheme = {
  id: "abyss-bioluminescent",
  label: "Bioluminescent Abyss",
  description: "Deep black with spores rising in the gutters, fading before the middle.",
  tokens: {
    "--codify-bg": "#000000",
    "--codify-surface": "#040d0b",
    "--codify-raised": "#071a15",
    "--codify-border": "#0d2b22",
    "--codify-border-strong": "#1d7157",
    "--codify-primary": "#d1fae5",
    "--codify-secondary": "#a7f3d0",
    "--codify-muted": "#5a9982",
    "--abyss-spore": "#10b981",
    "--abyss-deep": "#059669",
  },
  swatch: [
    { v: "--abyss-deep" }, { v: "--abyss-spore" },
  ],
};

/**
 * Solar Flare: an indigo-to-ultraviolet depth at the window's edges.
 *
 * No surface here is a tint of its own accent; the whole ramp is neutral-violet
 * and the two effect colours are the only saturated things on screen, which is
 * what keeps a starfield from competing with the text.
 */
export const SOLAR_FLARE: AppearanceTheme = {
  id: "solar-flare",
  label: "Solar Flare",
  description: "A starfield and slow streams along the edges, indigo into ultraviolet.",
  tokens: {
    "--codify-bg": "#000000",
    "--codify-surface": "#07060f",
    "--codify-raised": "#0e0b1c",
    "--codify-border": "#1b1633",
    "--codify-border-strong": "#654db3",
    "--codify-primary": "#f3e8ff",
    "--codify-secondary": "#ddd6fe",
    "--codify-muted": "#887da8",
    "--flare-indigo": "#6366f1",
    "--flare-ultraviolet": "#a855f7",
  },
  swatch: [
    { v: "--flare-indigo" }, { v: "--flare-ultraviolet" },
  ],
};

/**
 * Monochrome ASCII Rain: the CMatrix theme with the colour taken out.
 *
 * **This theme has no component.** It publishes `--cmatrix-rain`,
 * `--cmatrix-text` and `--cmatrix-bg` like the OLED theme does, so
 * `RainBackdrop` mounts for it on the token it already watches — and
 * `--cmatrix-glyphs` swaps the glyph set to hex bytes, which is what makes it
 * *ASCII rain* rather than CMatrix with a grey filter. That is the claim
 * `RainBackdrop.tsx` makes about itself ("adding a third rainy theme is a token
 * edit") being cashed two themes later, and it is why the shell is keyed on
 * variables rather than on theme ids: had it keyed on `cmatrix-oled`, this
 * theme would have needed a canvas of its own for no reason at all.
 *
 * One deliberate difference from OLED CMatrix, and it came from looking at it:
 * there, `--cmatrix-text` *is* `--codify-secondary`, because the rain's head
 * glyph is the app's body text. Here the brief separates the two — glyphs at
 * `#262626` to `#404040`, text at `#f5f5f5` — and the first version aliased
 * them anyway, which put a white head on every one of a hundred columns behind
 * the app's own white text. Monochrome means one colour, not one brightness.
 */
export const ASCII_RAIN: AppearanceTheme = {
  id: "ascii-rain",
  label: "Monochrome ASCII Rain",
  description: "The same rain, in hex bytes and grey, with nothing green about it.",
  tokens: {
    "--codify-bg": "#000000",
    "--codify-surface": "#050505",
    "--codify-raised": "#0a0a0a",
    "--codify-border": "#1f1f1f",
    "--codify-border-strong": "#5d5d5d",
    "--codify-primary": "#f5f5f5",
    "--codify-secondary": "#f5f5f5",
    "--codify-muted": "#878787",
    "--cmatrix-bg": "#000000",
    "--cmatrix-rain": "#262626",
    "--cmatrix-text": "#404040",
    "--cmatrix-glyphs": "0123456789ABCDEF",
  },  swatch: [
    { v: "--cmatrix-rain", label: "rain" },
    { v: "--cmatrix-text", label: "head" },
    { v: "--cmatrix-glyphs", label: "glyphs" },
  ],
};

/**
 * Still: the theme that ships **no canvas at all**, on purpose.
 *
 * Every other theme here is a weather system. This one is the ninth entry in
 * the list and the only one that is not a palette — it is a *statement about
 * motion*, and it exists because DESIGN.md §7 says motion must be justified,
 * which is a rule with no test behind it until something is willing to bet a
 * whole theme on it.
 *
 * What makes it more than "the default with a different grey": `prefers-reduced-motion`
 * already offers a motionless app, and it offers one by freezing a *rain* — a
 * still photograph of an animation, in the settings pane, behind the words. That
 * is motion opted out of, not motion absent. This theme has no frame to freeze.
 * For someone whose peripheral vision is the problem — long sessions, a bright
 * room, an attention difference, or simply a dislike of pixels that move when
 * nothing is happening — "I do not want weather" and "I want weather, quietly"
 * are different requests, and until now only the second one could be answered.
 *
 * So the absence is declared rather than implied. Before this, "the dark theme
 * is the only weatherless one" lived as an inline `CODIFY_DARK.id` inside a
 * test, which read as a fact about a default rather than a decision about
 * motion. It is now the list below, and a theme that is neither in
 * `ATMOSPHERE_TRIGGERS` nor on it fails the suite — which is the whole point of
 * a stress test: the rule has to be able to say no.
 *
 * The palette is a warm near-monochrome for the same reason the name is. There
 * is nothing to look at, so what is left is contrast: the highest
 * `primary`-on-`raised` ratio of any theme here, and no saturated hue anywhere
 * in it, because a colour with no meaning is a colour competing for the
 * attention this theme exists to give back.
 */
export const STILL: AppearanceTheme = {
  id: "still",
  label: "Still",
  description:
    "No canvas at all. For long sessions, and for anyone who would rather have no weather than quiet weather.",
  tokens: {
    "--codify-bg": "#131311",
    "--codify-surface": "#1c1c19",
    "--codify-raised": "#26261f",
    "--codify-border": "#3a3a33",
    "--codify-border-strong": "#707064",
    "--codify-primary": "#f7f6f2",
    "--codify-secondary": "#d6d5cc",
    "--codify-muted": "#a4a49c",
  },
  swatch: [
    { v: "--codify-bg" },
    { v: "--codify-raised" },
    { v: "--codify-border-strong" },
  ],
};

/**
 * Winter Snow: a clear cold night, and snow falling through all of it.
 *
 * No `--snow-glow`, deliberately. That absence *is* the theme — it is what tells
 * `SnowFall` not to light the field — and it is the reason this is a data entry
 * rather than a second component. The palette is blue-black through to ice, and
 * the two flake colours are a white and a pale blue so the field has depth
 * rather than being one flat disc repeated.
 */
export const WINTER_SNOW: AppearanceTheme = {
  id: "winter-snow",
  label: "Winter Snow",
  description:
    "A clear cold night, and snow falling through the whole window. No lights — just weather.",
  tokens: {
    "--codify-bg": "#050a14",
    "--codify-surface": "#0a1220",
    "--codify-raised": "#0f1a2e",
    "--codify-border": "#16263f",
    "--codify-border-strong": "#3a689a",
    "--codify-primary": "#eaf4ff",
    "--codify-secondary": "#c3d9f0",
    "--codify-muted": "#758fae",
    "--snow-flake": "#e8f4ff",
    "--snow-flake-2": "#a9c8ea",
  },
  swatch: [
    { v: "--codify-bg" },
    { v: "--snow-flake" },
    { v: "--codify-border-strong" },
  ],
};

/**
 * Festive Night: the same snowfall, and lights rising through it.
 *
 * The pair with Winter Snow, and the whole of the difference is four variables.
 * Two themes that shared a component *and* a palette would be one theme twice;
 * what makes this read as a different evening is the `--snow-glow` pair, which
 * Winter Snow does not publish. The gold is the accent — a warm, saturated hue
 * in a palette of greens and near-blacks — and the berry is the danger, which
 * keeps §2's rule that failure stays warm while being unmistakably a Christmas
 * red rather than the app's usual alert orange.
 */
export const FESTIVE_NIGHT: AppearanceTheme = {
  id: "festive-night",
  label: "Festive Night",
  description:
    "Evergreen dark, gold and berry, and warm lights drifting up through the snow.",
  tokens: {
    "--codify-bg": "#080d09",
    "--codify-surface": "#0e1a11",
    "--codify-raised": "#15271a",
    "--codify-border": "#1d3a25",
    "--codify-border-strong": "#3e794c",
    "--codify-primary": "#f6f2e6",
    "--codify-secondary": "#d9d3bf",
    "--codify-muted": "#989f8d",
    "--snow-flake": "#f2f6f0",
    "--snow-flake-2": "#cddcce",
    "--snow-glow": "#ffc85c",
    "--snow-glow-2": "#e2564d",
  },
  swatch: [
    { v: "--codify-bg" },
    { v: "--snow-glow" },
    { v: "--codify-border-strong" },
  ],
};

/**
 * Solarized Flare: a monochrome amber CRT, and solar wind down the gutters.
 *
 * Pure black, because the brief asks for OLED and this is the only theme that
 * takes it literally rather than as "nearly black". The gold is the only colour
 * in the theme, which is what a monochrome CRT is — and it is why this theme's
 * `success` is a *dimmer gold* than its accent rather than a green: there is no
 * green here, and inventing one would make the palette two themes. The one
 * exception is `danger`, which is a hot red-orange, because "failed" being
 * gold is the one meaning DESIGN.md §2 will not give up.
 */
export const SOLARIZED_FLARE: AppearanceTheme = {
  id: "solarized-flare",
  label: "Solarized Flare",
  description:
    "A monochrome amber CRT. Solar wind runs the gutters, and a heat pulse crosses them while a run is in flight.",
  tokens: {
    "--codify-bg": "#000000",
    "--codify-surface": "#0a0700",
    "--codify-raised": "#161000",
    "--codify-border": "#4a3800",
    "--codify-border-strong": "#785c00",
    "--codify-primary": "#ffd700",
    "--codify-secondary": "#ffb000",
    "--codify-muted": "#c99f2d",
    "--sun-core": "#ffb000",
    "--sun-edge": "#ffd700",
  },
  swatch: [
    { v: "--codify-bg" },
    { v: "--sun-edge" },
    { v: "--codify-border-strong" },
  ],
};

/**
 * Bio-Luminescent Cyber-Organism: a nerve network in the margins, firing.
 *
 * The third thing `neural-constellation` and `abyss-bioluminescent` were each
 * half of — those draw a whole-window web and a gutter of rising spores
 * respectively, and neither joins its points with anything. This one is nodes
 * *and* the tendrils between them, and the signal that travels along a tendril
 * is the part a recolour of either could not do.
 */
export const CYBER_ORGANISM: AppearanceTheme = {
  id: "cyber-organism",
  label: "Cyber Organism",
  description:
    "Electric blue and cyan. Glowing nodes and branching tendrils in the margins, firing while a run is in flight.",
  tokens: {
    "--codify-bg": "#000000",
    "--codify-surface": "#0a192f",
    "--codify-raised": "#0f2540",
    "--codify-border": "#16385c",
    "--codify-border-strong": "#2c71b2",
    "--codify-primary": "#e0f7ff",
    "--codify-secondary": "#9fe8ff",
    "--codify-muted": "#7aa5c3",
    "--organ-node": "#00e5ff",
    "--organ-tendril": "#0077ff",
  },
  swatch: [
    { v: "--codify-bg" },
    { v: "--organ-node" },
    { v: "--codify-border-strong" },
  ],
};

/**
 * Deep Void Event Horizon: an accretion disc seen edge-on, and mostly the dark.
 *
 * The most minimal theme in the app: near-black surfaces, one violet, and a
 * slate grey for everything that is not either. The painter orbits a centre
 * placed *below* the window, so the arcs read as the near edge of something
 * enormous rather than as rings drawn on the glass.
 */
export const EVENT_HORIZON: AppearanceTheme = {
  id: "event-horizon",
  label: "Event Horizon",
  description:
    "Obsidian and violet. Orbital streams curve away at the margins, and beams ignite while a run is in flight.",
  tokens: {
    "--codify-bg": "#000000",
    "--codify-surface": "#09090b",
    "--codify-raised": "#131318",
    "--codify-border": "#1f1f26",
    "--codify-border-strong": "#606075",
    "--codify-primary": "#e4e4e7",
    "--codify-secondary": "#c084fc",
    "--codify-muted": "#79899f",
    "--void-arc": "#c084fc",
    "--void-dust": "#64748b",
    "--void-beam": "#a855f7",
  },
  swatch: [
    { v: "--codify-bg" },
    { v: "--void-arc" },
    { v: "--codify-border-strong" },
  ],
};

/**
 * Vector Wireframe: 1px line art on black, like a vector arcade cabinet.
 *
 * Sharp, unrounded and monospace — the whole theme is the idea that a line has
 * no thickness and a corner has no radius. It carries **two** accent colours,
 * which no other theme does: the brief's "high-phosphor green or hot pink", and
 * the honest way to offer both is a token. `--vector-line` is the phosphor and
 * `--vector-lock` is the pink, used for the reticle that locks onto a solid while
 * a run is in flight.
 *
 * The pink is deliberately **not** this theme's `danger` — see `THEME_TONES`.
 */
export const VECTOR_WIREFRAME: AppearanceTheme = {
  id: "vector-wireframe",
  label: "Vector Wireframe",
  description:
    "Green or hot pink 1px line art. Rotating wireframe solids in the gutters, with a target reticle while a run is in flight.",
  tokens: {
    "--codify-bg": "#000000",
    "--codify-surface": "#031008",
    "--codify-raised": "#062012",
    "--codify-border": "#0d3b1f",
    "--codify-border-strong": "#1d763e",
    "--codify-primary": "#d6ffe6",
    "--codify-secondary": "#00ff66",
    "--codify-muted": "#5bab7d",
    "--vector-line": "#00ff66",
    "--vector-lock": "#ff007f",
  },
  swatch: [
    { v: "--codify-bg" },
    { v: "--vector-line" },
    { v: "--vector-lock" },
  ],
};

/**
 * Liquid Mercury: a heavy metal fluid, running up and down the margins.
 *
 * Chrome rather than colour: platinum for the crests, brushed steel for the
 * borders, titanium for the status tones. The surfaces are near-neutral with a
 * faint blue in them, which is what keeps it reading as *metal* rather than as
 * grey — a perfectly neutral grey is the one value that looks like nothing.
 */
export const LIQUID_MERCURY: AppearanceTheme = {
  id: "liquid-mercury",
  label: "Liquid Mercury",
  description:
    "Platinum, steel and titanium. Viscous metallic waves climb the margins and go turbulent under load.",
  tokens: {
    "--codify-bg": "#000000",
    "--codify-surface": "#0b0d10",
    "--codify-raised": "#131720",
    "--codify-border": "#1e293b",
    "--codify-border-strong": "#4f6584",
    "--codify-primary": "#f1f5f9",
    "--codify-secondary": "#e2e8f0",
    "--codify-muted": "#8291a5",
    "--fluid-crest": "#e2e8f0",
    "--fluid-trough": "#334155",
  },
  swatch: [
    { v: "--codify-bg" },
    { v: "--fluid-crest" },
    { v: "--codify-border-strong" },
  ],
};

/**
 * Electric Arc: a discharge down both gutters, on an ink-blue that is almost
 * black.
 *
 * **The arc is a shape, not a glow.** A bloom around a line reads as a lens
 * flare and a filter; what makes lightning recognisable is the *fork* — a main
 * channel with two or three branches leaving it at an angle and dying
 * quickly — so that is what `ElectricArc.tsx` draws. The core is the near-white
 * of the channel and the fork is the theme's violet, which is also why the two
 * are separate variables: one arc at the wrong depth would be invisible, and a
 * single colour cannot be both the thing that is hot and the thing that is
 * only near it.
 *
 * The surfaces are blue rather than neutral because a white discharge on a
 * neutral black reads as a photograph of lightning, and on blue it reads as a
 * wire. `--arc-core` is the trigger: publishing it is the theme asking for
 * weather, and the trigger is the pale core rather than the violet so a theme
 * that published only the cooler one could not mount a canvas with no hot end.
 */
export const ELECTRIC_ARC: AppearanceTheme = {
  id: "electric-arc",
  label: "Electric Arc",
  description:
    "Ink-blue and a lightning-white core. Arcs strike down both gutters, fork, and go out as fast as they came.",
  tokens: {
    "--codify-bg": "#030310",
    "--codify-surface": "#0a0a20",
    "--codify-raised": "#111132",
    "--codify-border": "#1e1e4a",
    "--codify-border-strong": "#5757b9",
    "--codify-primary": "#e8e8ff",
    "--codify-secondary": "#8a7dff",
    "--codify-muted": "#8888ac",
    "--arc-core": "#dfe4ff",
    "--arc-fork": "#8a7dff",
  },
  swatch: [
    { v: "--codify-bg" },
    { v: "--arc-core" },
    { v: "--codify-accent" },
  ],
};

/**
 * Toxic Lab: a fume hood at three in the morning.
 *
 * **The green is a substance, not a mood.** Every other green theme here puts
 * green on the *edges* and calls the middle clean; a lab puts the reagent in
 * the glass and the glass in the middle of the bench, so the bubbles rise
 * through the gutters and the window's centre stays empty. That is the whole
 * design constraint and it is the same one the margin effects obey, arrived at
 * from the other direction.
 *
 * **The interesting half is that failure is not green.** The reagent is acid
 * yellow-green and the theme's `accent` is more of it, so a reader who has
 * learned this window needs a failure colour that is *not* in it. `danger` is
 * therefore the orange-red of a hazard label and `warning` its amber — see
 * `THEME_TONES`, and the warm-arc rule in `ui/tests/appearance.test.ts` that
 * holds every theme to exactly this. `success` is a mint rather than the
 * reagent's own green, because "it worked" and "there is acid on screen" being
 * the same colour is the one confusion this palette could afford.
 */
export const TOXIC_LAB: AppearanceTheme = {
  id: "toxic-lab",
  label: "Toxic Lab",
  description:
    "A fume hood at 3am. Reagent-green bubbles climb the gutters, wobble, and burst; failure is the amber the hazard tape is.",
  tokens: {
    "--codify-bg": "#050a06",
    "--codify-surface": "#0b1a0f",
    "--codify-raised": "#12271a",
    "--codify-border": "#1d3d26",
    "--codify-border-strong": "#3b7947",
    "--codify-primary": "#e2f5d9",
    "--codify-secondary": "#9dff3c",
    "--codify-muted": "#93a786",
    "--reagent": "#9dff3c",
    "--reagent-skin": "#d4ff7a",
  },
  swatch: [
    { v: "--codify-bg" },
    { v: "--reagent" },
    { v: "--codify-accent" },
  ],
};

/**
 * Anon Fluid: green on black, and deliberately no name on it.
 *
 * **Sideways, not falling.** The OLED theme already falls glyphs down the
 * window, and a third column of falling characters would be that theme again
 * in a different colour. So the motion here is *horizontal* — data running
 * along the gutters as a continuous liquid column, each row offset by a wave so
 * the stream has a surface — which also happens to be what a terminal does when
 * it is reading a stream rather than printing a file.
 *
 * The anonymity is the palette's job, not the name's: a near-black green-black,
 * a phosphor primary, and an `accent` identical to the theme's own stream
 * colour so the wordmark reads as part of the same picture. There is no
 * `--anon-mask`; a mask would be a logo, and this theme is the one place in
 * the app that has none.
 */
export const ANON_FLUID: AppearanceTheme = {
  id: "anon-fluid",
  label: "Anon Fluid",
  description:
    "Green on black and no name on it. Data runs sideways through the gutters as a liquid column, and a run in flight shears it into blocks.",
  tokens: {
    "--codify-bg": "#000000",
    "--codify-surface": "#03110a",
    "--codify-raised": "#061a10",
    "--codify-border": "#0c3320",
    "--codify-border-strong": "#1e714c",
    "--codify-primary": "#c8ffe0",
    "--codify-secondary": "#00ff9c",
    "--codify-muted": "#759f8a",
    "--fluid-bit": "#00ff9c",
    "--fluid-cursor": "#a8ffd8",
  },
  swatch: [
    { v: "--codify-bg" },
    { v: "--fluid-bit" },
    { v: "--codify-accent" },
  ],
};

/**
 * The six tone variables a theme may repaint: the one interactive hue, and the
 * five status tones. Named rather than derived because the shape of the answer
 * is a decision, and `Object.keys(tones)[0]` would not be one.
 */
export const STATUS_TONE_VARS = [
  "--codify-accent",
  "--codify-info",
  "--codify-success",
  "--codify-warning",
  "--codify-danger",
  "--codify-neutral",
] as const;

export type StatusToneVar = (typeof STATUS_TONE_VARS)[number];

/**
 * What each theme makes of those six.
 *
 * **This reverses a decision, deliberately.** Status tones used to be
 * unreachable from a theme, and `ui/tests/appearance.test.ts` asserted that no
 * theme touched them: *"a theme that repainted them would repaint what 'failed'
 * means."* The badge layer ignored the theme, so a success pill in the OLED
 * CMatrix app was Tailwind green on a black-and-phosphor screen — the one
 * saturated thing on screen, per DESIGN.md §2, and it belonged to nobody.
 *
 * What is kept from the old rule is the part that protected a *meaning* rather
 * than a habit, and it is now a test instead of a comment:
 *
 * - **All six or none.** `applyTheme` clears every managed variable before
 *   applying the next theme, so a theme that set three would show the *previous*
 *   theme's other three — the exact failure clearing exists to prevent. A
 *   half-toned theme is worse than an untoned one.
 * - **`danger` and `warning` stay warm.** A theme may restate failure in its
 *   own palette; it may not make it cyan. A user who has learned that orange
 *   means "needs attention" is owed that much.
 *
 * `accent` and `info` are the same hue in every row, because DESIGN.md §2 calls
 * them the same sentence: the primary action and the thing in flight.
 */
export const THEME_TONES: Readonly<
  Record<string, Readonly<Record<StatusToneVar, string>>>
> = {
  // §2's hexes, one step off. This theme is the default, so its answer is
  // "whatever `:root` says" — but §2's blue had to move: once the accent took
  // a resting role (the send button, a selected row in a list) it was painted
  // on `raised`, where `#2f81f7` reads 4.06:1 against the 4.5:1 that an 11px
  // thread title needs. `#418cf8` is the smallest step that clears it, the hue
  // is unchanged, and `info` follows because §2 makes the two one sentence —
  // "sharing a value means the two can never drift apart".
  [CODIFY_DARK.id]: {
    "--codify-accent": "#418cf8",
    "--codify-info": "#418cf8",
    "--codify-success": "#3fb950",
    "--codify-warning": "#d29922",
    "--codify-danger": "#f85149",
    "--codify-neutral": "#8b949e",
  },
  // Phosphor. Success is a paler green than accent so the two never collide,
  // and danger is the one saturated red in an all-green palette.
  [CMATRIX_OLED.id]: {
    "--codify-accent": "#00e676",
    "--codify-info": "#00e676",
    "--codify-success": "#7cff9e",
    "--codify-warning": "#ffc857",
    "--codify-danger": "#ff5f56",
    "--codify-neutral": "#6f8f7f",
  },
  // The theme's own three accents, spread across the tones rather than all on
  // one: magenta is the interactive hue, cyan is in flight, amber warns.
  [CYBERPUNK_NEON.id]: {
    "--codify-accent": "#ff2e9a",
    "--codify-info": "#00e5ff",
    "--codify-success": "#39ff14",
    "--codify-warning": "#ffb02e",
    "--codify-danger": "#ff3b5c",
    "--codify-neutral": "#8f74bd",
  },
  [NEURAL_CONSTELLATION.id]: {
    "--codify-accent": "#38bdf8",
    "--codify-info": "#38bdf8",
    "--codify-success": "#2dd4bf",
    "--codify-warning": "#fbbf24",
    "--codify-danger": "#fb5c7d",
    "--codify-neutral": "#64748b",
  },
  // A Tokyo-terminal palette: amber is the hue it already speaks in, and
  // success is the green those terminals used, not a default.
  [HUD_TACTICAL.id]: {
    "--codify-accent": "#f59e0b",
    "--codify-info": "#f59e0b",
    "--codify-success": "#9ece6a",
    "--codify-warning": "#fbbf24",
    "--codify-danger": "#f4544f",
    "--codify-neutral": "#a1804a",
  },
  [BIOLUMINESCENT_ABYSS.id]: {
    "--codify-accent": "#10b981",
    "--codify-info": "#10b981",
    "--codify-success": "#5eead4",
    "--codify-warning": "#fbbf24",
    "--codify-danger": "#ff6b81",
    "--codify-neutral": "#4b7f6c",
  },
  // Ultraviolet leads: this is the one theme whose signature hue is not cool,
  // and accent is where that gets said.
  [SOLAR_FLARE.id]: {
    "--codify-accent": "#a855f7",
    "--codify-info": "#6366f1",
    "--codify-success": "#34d399",
    "--codify-warning": "#fb923c",
    "--codify-danger": "#f43f5e",
    "--codify-neutral": "#6b5f8f",
  },
  // Monochrome by intent, so severity is brightness rather than hue: failure is
  // the brightest thing on a black screen, and idle is the dimmest.
  [ASCII_RAIN.id]: {
    "--codify-accent": "#f5f5f5",
    "--codify-info": "#d4d4d4",
    "--codify-success": "#a3a3a3",
    "--codify-warning": "#757575",
    "--codify-danger": "#ffffff",
    "--codify-neutral": "#525252",
  },
  [STILL.id]: {
    "--codify-accent": "#7c9cc4",
    "--codify-info": "#7c9cc4",
    "--codify-success": "#9fb8a8",
    "--codify-warning": "#c2a878",
    "--codify-danger": "#d08770",
    "--codify-neutral": "#8e8d84",
  },
  // Ice blue leads, and `success` is the pale green of light on snow rather
  // than a default. `danger` is a rose red — warm on the arc, and legible
  // against a background that is almost entirely blue.
  [WINTER_SNOW.id]: {
    "--codify-accent": "#7cc4ff",
    "--codify-info": "#7cc4ff",
    "--codify-success": "#8fe3c4",
    "--codify-warning": "#ffd28a",
    "--codify-danger": "#ff7a8a",
    "--codify-neutral": "#7f9cbb",
  },
  // Gold, because this is the one theme in the app whose accent is warm. The
  // berry danger is the same warm arc the other nine are on, checked by the
  // hue test in `appearance.test.ts` like every other theme.
  [FESTIVE_NIGHT.id]: {
    "--codify-accent": "#ffc85c",
    "--codify-info": "#7fd1a8",
    "--codify-success": "#7fd1a8",
    "--codify-warning": "#ffc85c",
    "--codify-danger": "#e2564d",
    "--codify-neutral": "#8a927e",
  },

  // Monochrome amber, so `success` is a dimmer gold rather than a green: there
  // is no green in this theme and inventing one makes it two themes. `danger` is
  // the one hue that breaks the palette, and it has to — see DESIGN.md §2.
  [SOLARIZED_FLARE.id]: {
    "--codify-accent": "#ffb000",
    "--codify-info": "#e09a00",
    "--codify-success": "#9c7d1f",
    "--codify-warning": "#ffd700",
    "--codify-danger": "#ff5f3a",
    "--codify-neutral": "#6b5200",
  },
  [CYBER_ORGANISM.id]: {
    "--codify-accent": "#00e5ff",
    "--codify-info": "#0077ff",
    "--codify-success": "#7cffb2",
    "--codify-warning": "#ffd166",
    "--codify-danger": "#ff5c7a",
    "--codify-neutral": "#4a7fa5",
  },
  [EVENT_HORIZON.id]: {
    "--codify-accent": "#c084fc",
    "--codify-info": "#a78bfa",
    "--codify-success": "#86efac",
    "--codify-warning": "#fcd34d",
    "--codify-danger": "#fb7185",
    "--codify-neutral": "#64748b",
  },
  // The brief asks for hot pink, and it is here — as `--vector-lock`, the colour
  // the reticle and the locked vertices are drawn in, which is what "laser lines
  // in green or pink" is actually describing. It is deliberately NOT the danger
  // tone: `#ff007f` sits at hue 325°, off the warm arc, and the rule that
  // failure stays warm exists so that a user who has learned one theme's red
  // still recognises it in the next. So the danger is `#ff3b5c` — a hot red
  // that reads at a glance beside a mint "passed" on the same screen.
  [VECTOR_WIREFRAME.id]: {
    "--codify-accent": "#00ff66",
    "--codify-info": "#00ff66",
    "--codify-success": "#7cffb2",
    "--codify-warning": "#ffd700",
    "--codify-danger": "#ff3b5c",
    "--codify-neutral": "#3f7a58",
  },
  [LIQUID_MERCURY.id]: {
    "--codify-accent": "#e2e8f0",
    "--codify-info": "#94a3b8",
    "--codify-success": "#a7f3d0",
    "--codify-warning": "#fcd34d",
    "--codify-danger": "#fb7185",
    "--codify-neutral": "#64748b",
  },
  // The discharge is violet and the *wire* is the white of it, so accent leads
  // and primary is the pale end. Failure stays a red rather than becoming the
  // violet, for the reason every theme here keeps to the warm arc: a user who
  // has learned that this window means "broken" by its red should not have to
  // relearn it in the one theme that is mostly blue.
  [ELECTRIC_ARC.id]: {
    "--codify-accent": "#8f7bff",
    "--codify-info": "#7fc4ff",
    "--codify-success": "#3fe0a0",
    "--codify-warning": "#ffb340",
    "--codify-danger": "#ff4d5a",
    "--codify-neutral": "#6a6a9c",
  },
  // The hardest palette in the list to get right, and the reason is on the
  // theme: acid green is the *substance*, so it cannot also be the interface's
  // success, and it cannot be failure either. Success is a mint, failure is the
  // hazard-label orange-red, and warning is the tape's amber — the three the
  // eye has to separate at a glance in a window that is otherwise one colour.
  [TOXIC_LAB.id]: {
    "--codify-accent": "#b6ff2e",
    "--codify-info": "#7fd4ff",
    "--codify-success": "#6ef0b0",
    "--codify-warning": "#ffd23f",
    "--codify-danger": "#ff5630",
    "--codify-neutral": "#7a8a72",
  },
  // A monochrome phosphor theme, so severity is partly hue and mostly
  // brightness: `danger` is a full-bleed red, `warning` a gold, and `success`
  // a spring green that is deliberately not the stream's own `00ff9c` — a
  // terminal that says "passed" in the same green as its data has stopped
  // using colour to say anything.
  [ANON_FLUID.id]: {
    "--codify-accent": "#00ff9c",
    "--codify-info": "#7fd4ff",
    "--codify-success": "#39ff88",
    "--codify-warning": "#ffcc00",
    "--codify-danger": "#ff3b30",
    "--codify-neutral": "#5f8a72",
  },
};


/**
 * Fold a theme's tones into its tokens.
 *
 * The tones go in *underneath*, so a theme that ever sets one inline still
 * wins — the escape hatch is real rather than aspirational, and a test pins
 * that the two cannot disagree.
 */
function withTones(theme: AppearanceTheme): AppearanceTheme {
  const tones = THEME_TONES[theme.id];
  if (!tones) return theme;
  return { ...theme, tokens: { ...tones, ...theme.tokens } };
}

export const THEMES: ReadonlyArray<AppearanceTheme> = [
  CODIFY_DARK,
  CMATRIX_OLED,
  CYBERPUNK_NEON,
  NEURAL_CONSTELLATION,
  HUD_TACTICAL,
  BIOLUMINESCENT_ABYSS,
  SOLAR_FLARE,
  ASCII_RAIN,
  STILL,
  WINTER_SNOW,
  FESTIVE_NIGHT,
  SOLARIZED_FLARE,
  CYBER_ORGANISM,
  EVENT_HORIZON,
  VECTOR_WIREFRAME,
  LIQUID_MERCURY,
  ELECTRIC_ARC,
  TOXIC_LAB,
  ANON_FLUID,
].map(withTones);

export const DEFAULT_THEME_ID = CODIFY_DARK.id;

/**
 * The themes that deliberately publish **no** weather variable, and therefore
 * get no canvas.
 *
 * This is the declaration that makes "motion must be justified" enforceable
 * rather than aspirational. Every theme in `THEMES` is either named in
 * `ATMOSPHERE_TRIGGERS` (and so has a mechanism watching its variable) or named
 * here (and so has chosen to have none). A theme that is in neither is a theme
 * whose canvas silently never mounts — the failure the pairing tests were built
 * to catch, and the one this list turns from a single hard-coded id into
 * something a person maintains on purpose.
 *
 * `CODIFY_DARK` is on it because it is the plain brand palette and never had an
 * effect; `STILL` is on it because having no effect is the entire point. Both
 * are decisions, and they are written down in the same place so that adding a
 * theme is a choice between two lists rather than a question of whether a test
 * happens to be looking.
 */
export const MOTIONLESS_THEME_IDS: ReadonlyArray<string> = [CODIFY_DARK.id, STILL.id];

/** Whether this theme is one of the declared motionless ones. */
export function isMotionless(themeId: string): boolean {
  return MOTIONLESS_THEME_IDS.includes(themeId);
}

/**
 * Themes that ship an animation, and the variable each one's atmosphere is
 * keyed on. Declared here rather than in the backdrop so the two facts that
 * must agree — a theme publishes the variable, and something watches for it —
 * are in the same file, and so a theme with an effect cannot be added without
 * the question "what watches it?" being asked in the same breath.
 *
 * Read it as the sixth entry the tests are about to want: `ascii-rain` is not
 * here, because it is weather too and the *only* thing that distinguishes it is
 * that it reuses the OLED theme's trigger. So the trigger is listed once per
 * mechanism, and a theme inherits the entry whose variable it publishes.
 */
export const ATMOSPHERE_TRIGGERS: ReadonlyArray<{
  /** The variable whose presence means "this theme wants weather". */
  trigger: string;
  /** Themes that publish it, for the tests that assert the pairing. */
  themes: ReadonlyArray<string>;
}> = [
  { trigger: "--cmatrix-rain", themes: [CMATRIX_OLED.id, ASCII_RAIN.id] },
  { trigger: "--cyber-cyan", themes: [CYBERPUNK_NEON.id] },
  { trigger: "--neural-node", themes: [NEURAL_CONSTELLATION.id] },
  { trigger: "--hud-amber", themes: [HUD_TACTICAL.id] },
  { trigger: "--abyss-spore", themes: [BIOLUMINESCENT_ABYSS.id] },
  { trigger: "--flare-indigo", themes: [SOLAR_FLARE.id] },
  // Two themes, one trigger, one painter — the ASCII-rain relationship again.
  { trigger: "--snow-flake", themes: [WINTER_SNOW.id, FESTIVE_NIGHT.id] },
  { trigger: "--sun-core", themes: [SOLARIZED_FLARE.id] },
  { trigger: "--organ-node", themes: [CYBER_ORGANISM.id] },
  { trigger: "--void-arc", themes: [EVENT_HORIZON.id] },
  { trigger: "--vector-line", themes: [VECTOR_WIREFRAME.id] },
  { trigger: "--fluid-crest", themes: [LIQUID_MERCURY.id] },
  { trigger: "--arc-core", themes: [ELECTRIC_ARC.id] },
  { trigger: "--reagent", themes: [TOXIC_LAB.id] },
  { trigger: "--fluid-bit", themes: [ANON_FLUID.id] },
];

export function themeById(id: string): AppearanceTheme {
  return THEMES.find((t) => t.id === id) ?? CODIFY_DARK;
}

/** The storage key. One namespace, as `api.ts` names its own keys. */
const STORAGE_KEY = "codify.theme";

/**
 * Fired on `window` whenever a theme is applied. The DOM pieces that react to
 * the theme *as React state* (a component that mounts or unmounts on the
 * choice, rather than repainting via CSS) subscribe to this; everything else
 * follows the custom properties with no JS at all.
 */
export const THEME_CHANGE_EVENT = "codify:theme-changed";

/** The slice of `Storage` this module needs, so a test can pass a fake. */
export type ThemeStorage = Pick<Storage, "getItem" | "setItem">;

function store(): ThemeStorage | undefined {
  return typeof localStorage !== "undefined" ? localStorage : undefined;
}

/**
 * The stored theme id, or the default. Anything unrecognized — an old id, a
 * hand-edited value, a different app's key — falls back to the default rather
 * than throwing or rendering an unnamed state.
 */
export function readStoredThemeId(storage: ThemeStorage | undefined = store()): string {
  const raw = storage?.getItem(STORAGE_KEY) ?? "";
  return THEMES.some((t) => t.id === raw) ? raw : DEFAULT_THEME_ID;
}

export function storeThemeId(
  id: string,
  storage: ThemeStorage | undefined = store(),
): void {
  storage?.setItem(STORAGE_KEY, id);
}

/** What `applyTheme` writes to: `document.documentElement.style` in the app. */
export interface CssStyleTarget {
  removeProperty(name: string): void;
  setProperty(name: string, value: string): void;
}

/**
 * `#0d1117` → `"13 17 23"` — the channel triplet the utility overrides in
 * `ui/src/index.css` compose with an alpha. Pure, so a test can pin every
 * theme's derivation against its own hexes.
 */
export function hexChannels(hex: string): string {
  const m = hex.match(/^#([0-9a-fA-F]{6})$/);
  if (!m) throw new Error(`not a #rrggbb colour: ${hex}`);
  const n = parseInt(m[1], 16);
  return `${(n >> 16) & 0xff} ${(n >> 8) & 0xff} ${n & 0xff}`;
}

/**
 * Apply a theme to `root` (the document root's style, by default): clear the
 * managed properties first, then set this theme's. Clearing first is the
 * whole difference between switching and layering — without it, OLED's greens
 * survive a switch back to dark and `:root` can never win again.
 *
 * Every hex token also writes its `-rgb` channel triplet, which is what the
 * utility overrides read; the hexes stay the theme's public shape, quoted in
 * DESIGN.md's runtime-themes table.
 *
 * ## `overrides`: a user's own colours, merged here rather than beside
 *
 * `overrides` is the resolved half of a tint — already contrast-checked by
 * `tint.ts`, which is the only thing allowed to decide what a proposed colour
 * becomes. It is merged **before** the `-rgb` derivation rather than written
 * after it, so an overridden hex derives its own triplet; writing the hex alone
 * would leave `rgb(var(--arc-core-rgb) / 0.4)` resolving to the theme's
 * original hue while the canvas painted the user's.
 *
 * It is a parameter rather than a read of `localStorage` from inside here for
 * two reasons. This module is the one `tint.ts` imports, so reaching back into
 * it would close a cycle; and an `applyTheme` that secretly consulted storage
 * could not be tested against a theme without also inventing a browser. The
 * caller resolves and passes, which is the same shape every other override in
 * this app takes.
 *
 * Returns the properties it set, so a caller (and a test) can see what changed
 * without re-reading the DOM.
 */
export function applyTheme(
  id: string,
  root: CssStyleTarget | undefined = typeof document !== "undefined"
    ? document.documentElement.style
    : undefined,
  overrides: Readonly<Record<string, string>> = {},
): Array<[string, string]> {
  if (!root) return [];
  const theme = themeById(id);
  for (const name of MANAGED_VARS) root.removeProperty(name);
  const applied: Array<[string, string]> = [];
  const tokens: Record<string, string> = { ...theme.tokens, ...overrides } as Record<string, string>;
  for (const [name, value] of Object.entries(tokens)) {
    root.setProperty(name, value as string);
    applied.push([name, value as string]);
    if (/^#[0-9a-fA-F]{6}$/.test(value as string)) {
      const rgbName = `${name}-rgb`;
      const channels = hexChannels(value as string);
      root.setProperty(rgbName, channels);
      applied.push([rgbName, channels]);
    }
  }
  // Announce it for the subscribers that hold the id as state. Guarded: a
  // `node --test` process has no window, and applying a theme there must
  // stay a pure property write.
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(THEME_CHANGE_EVENT, { detail: id }));
  }
  return applied;
}

/** The applied theme id, for `useSyncExternalStore`'s snapshot. */
export function activeThemeId(): string {
  return readStoredThemeId();
}

/** Subscribe to theme applications; returns the unsubscribe function. */
export function subscribeTheme(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(THEME_CHANGE_EVENT, onChange);
  // Another window (or devtools) writing the key directly is rare but real;
  // the storage event covers it for free.
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(THEME_CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

// There is deliberately no `initAppearance` here any more. It was
// `readStoredThemeId` + `applyTheme` for boot, and once tints existed it would
// have been the *other* boot path — one that silently dropped a user's chosen
// colours and flashed the stock palette for a frame on every launch, while
// looking correct. `main.tsx` calls `tint.ts`'s `applyTintedTheme` instead,
// which is the same two steps plus the tint merge, and there is now exactly
// one way the app gets its colours onto the root.
