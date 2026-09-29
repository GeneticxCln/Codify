/**
 * The terminal is the one surface in this app that CSS cannot reach.
 *
 * xterm.js paints its own screen — a canvas, plus inline styles — and takes its
 * colours as an `ITheme` object of concrete colour strings. So the runtime
 * theme layer in `ui/src/index.css`, which re-points every `codify-*` utility
 * at a live variable, never touched it. The shell was `#0d1117` on
 * `#c9d1d9` — the *default* theme's hexes — in all nine themes, which is why
 * the terminal was the one grey-blue box in an OLED app that was otherwise
 * black and phosphor.
 *
 * So the theme is read out of the document and handed over. That read is the
 * only part that needs a DOM, and it is three lines in the component; the
 * decision of which variable paints which terminal colour is `xtermTheme`,
 * which is a function of a lookup and therefore a thing a test can pin.
 *
 * ## Why concrete hexes and not `var(--x)`
 *
 * xterm parses the strings it is given — it computes contrast ratios against
 * the background to pick a cursor colour — and it writes them into inline
 * styles either way. Passing a `var()` reference would either be rejected or
 * silently not resolve where the value is read in JS. So the values are read
 * from the computed style and passed as they were declared.
 */

/** The subset of xterm's `ITheme` this app fills. Kept structural on purpose:
 * xterm is a dynamic import (`TerminalPane.tsx` loads it inside an effect,
 * because it needs a measured font), so this file must not import it. */
export interface XtermTheme {
  background: string;
  foreground: string;
  cursor: string;
  selectionBackground: string;
  black: string;
  red: string;
  green: string;
  yellow: string;
  blue: string;
  magenta: string;
  cyan: string;
  white: string;
  brightBlack: string;
  brightRed: string;
  brightGreen: string;
  brightYellow: string;
  brightBlue: string;
  brightMagenta: string;
  brightCyan: string;
  brightWhite: string;
}

/**
 * Which CSS variable paints which terminal colour.
 *
 * The ANSI sixteen are the *program's* palette — `ls`, `grep --color`, a
 * failing test — and they were stock, which meant a themed app still printed
 * stock escape sequences. Each one is now a token the theme publishes, so the
 * program and the window are the same picture.
 *
 * Two mappings are worth reading rather than skimming:
 *
 * - **`blue` and `cyan` both take `--codify-info`, and `magenta` takes
 *   `--codify-accent`.** A theme publishes six tones, not sixteen, so some
 *   names have to share. Sharing beats substituting: a hue that belongs to the
 *   theme and is nearly right reads as intentional, where a unique-but-foreign
 *   16-colour ramp reads as a different app inside the same window.
 * - **`black` is the background, not `#000000`.** A pure black in a theme
 *   whose background is `#020806` prints a visible grey-black block in
 *   `ls --color`, which is the one place a terminal shows its own background
 *   as a swatch.
 */
const MAPPING: Readonly<Record<keyof XtermTheme, string>> = {
  background: "--codify-bg",
  foreground: "--codify-secondary",
  cursor: "--codify-primary",
  // The strongest border the theme publishes, because a selection has to read
  // as selection over text that is already a light colour on a dark surface.
  selectionBackground: "--codify-border-strong",
  black: "--codify-bg",
  red: "--codify-danger",
  green: "--codify-success",
  yellow: "--codify-warning",
  blue: "--codify-info",
  magenta: "--codify-accent",
  cyan: "--codify-info",
  white: "--codify-secondary",
  // The bright variants take the same six tones. A theme publishes one hue per
  // meaning, not a light and a dark of each, so "bright red" is the theme's
  // red — which on every theme is already chosen to be legible on its own
  // background, and is usually the only saturated thing on screen by design
  // (DESIGN.md §2).
  brightBlack: "--codify-muted",
  brightRed: "--codify-danger",
  brightGreen: "--codify-success",
  brightYellow: "--codify-warning",
  brightBlue: "--codify-info",
  brightMagenta: "--codify-accent",
  brightCyan: "--codify-info",
  brightWhite: "--codify-primary",
};

/** What a terminal is painted with when the document says nothing.
 *
 * The default theme's own values, quoted from DESIGN.md §2 — the same answer
 * the hardcoded theme gave, so a terminal that opens before a theme is applied
 * is indistinguishable from one that opens after.
 */
const FALLBACK: XtermTheme = {
  background: "#0d1117",
  foreground: "#c9d1d9",
  cursor: "#e6edf3",
  selectionBackground: "#66707c",
  black: "#0d1117",
  red: "#f85149",
  green: "#3fb950",
  yellow: "#d29922",
  blue: "#418cf8",
  magenta: "#418cf8",
  cyan: "#418cf8",
  white: "#c9d1d9",
  brightBlack: "#979fa8",
  brightRed: "#f85149",
  brightGreen: "#3fb950",
  brightYellow: "#d29922",
  brightBlue: "#418cf8",
  brightMagenta: "#418cf8",
  brightCyan: "#418cf8",
  brightWhite: "#e6edf3",
};

/**
 * The theme, from a lookup that returns a CSS declaration value.
 *
 * `lookup` is `getPropertyValue` on a computed style, injected so this is a
 * function of a dictionary and not a function of a document. A name that comes
 * back empty or whitespace falls back per colour rather than per call: one
 * unset variable should not blank the whole terminal, and the fallbacks are
 * the colours that were there before any of this existed.
 */
export function xtermTheme(
  lookup: (variable: string) => string,
  fallback: XtermTheme = FALLBACK,
): XtermTheme {
  const out = {} as XtermTheme;
  for (const key of Object.keys(MAPPING) as Array<keyof XtermTheme>) {
    const declared = lookup(MAPPING[key]).trim();
    out[key] = declared || fallback[key];
  }
  return out;
}

/** The theme from the document's live custom properties.
 *
 * Reads `document.documentElement` rather than the pane, because the pane is
 * a child of the app root and a theme writes to the root — one place, so a
 * terminal in any pane reads the same answer.
 */
export function xtermThemeFromDocument(doc: Document = document): XtermTheme {
  const style = doc.defaultView?.getComputedStyle(doc.documentElement);
  if (!style) return xtermTheme(() => "");
  return xtermTheme((name) => style.getPropertyValue(name));
}

/** The variable each terminal colour comes from — exported for the test that
 * asserts the mapping still covers every key, which is the kind of thing a
 * renamed field would otherwise drop silently. */
export function terminalColorSource(key: keyof XtermTheme): string {
  return MAPPING[key];
}
