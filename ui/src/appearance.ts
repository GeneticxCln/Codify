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
 * a third view of the same eight hexes, not a new decision. Only surfaces and
 * text are themed: the five status hues are load-bearing meanings and a theme
 * that repainted them would repaint what "failed" means.
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
    "--codify-border-strong": "#484f58",
    "--codify-primary": "#e6edf3",
    "--codify-secondary": "#c9d1d9",
    "--codify-muted": "#8b949e",
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
    "--codify-border-strong": "#1c4028",
    "--codify-primary": "#dcffe4",
    "--codify-secondary": "#a3ffb8",
    "--codify-muted": "#5f8f70",
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
    "--codify-border-strong": "#6d2fb0",
    "--codify-primary": "#f6efff",
    "--codify-secondary": "#d9c8ff",
    "--codify-muted": "#8f74bd",
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
    "--codify-border-strong": "#1e293b",
    "--codify-primary": "#e0f2fe",
    "--codify-secondary": "#bae6fd",
    "--codify-muted": "#64748b",
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
    "--codify-border-strong": "#4a3a18",
    "--codify-primary": "#fef3c7",
    "--codify-secondary": "#fde68a",
    "--codify-muted": "#a1804a",
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
    "--codify-border-strong": "#134b3a",
    "--codify-primary": "#d1fae5",
    "--codify-secondary": "#a7f3d0",
    "--codify-muted": "#4b7f6c",
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
    "--codify-border-strong": "#2e2352",
    "--codify-primary": "#f3e8ff",
    "--codify-secondary": "#ddd6fe",
    "--codify-muted": "#6b5f8f",
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
    "--codify-border-strong": "#333333",
    "--codify-primary": "#f5f5f5",
    "--codify-secondary": "#f5f5f5",
    "--codify-muted": "#737373",
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
    "--codify-border-strong": "#55554a",
    "--codify-primary": "#f7f6f2",
    "--codify-secondary": "#d6d5cc",
    "--codify-muted": "#8e8d84",
  },
  swatch: [
    { v: "--codify-bg" },
    { v: "--codify-raised" },
    { v: "--codify-border-strong" },
  ],
};

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
];

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
 * Returns the properties it set, so a caller (and a test) can see what changed
 * without re-reading the DOM.
 */
export function applyTheme(
  id: string,
  root: CssStyleTarget | undefined = typeof document !== "undefined"
    ? document.documentElement.style
    : undefined,
): Array<[string, string]> {
  if (!root) return [];
  const theme = themeById(id);
  for (const name of MANAGED_VARS) root.removeProperty(name);
  const applied: Array<[string, string]> = [];
  for (const [name, value] of Object.entries(theme.tokens)) {
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

/**
 * Read-apply-persist in one call, for boot: `main.tsx` runs this before the
 * first paint so a chosen theme is on screen from the first frame, not after.
 */
export function initAppearance(storage: ThemeStorage | undefined = store()): string {
  const id = readStoredThemeId(storage);
  applyTheme(id);
  return id;
}
