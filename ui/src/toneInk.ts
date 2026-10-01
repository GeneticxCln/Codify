// `.ts` explicitly: `appearance.ts` imports this and is loaded by tests that have no extension resolver.
import { clampForContrast, composite } from "./contrast.ts";

/**
 * The text colour for a status pill: the tone, moved just far enough to be readable on its own tint.
 *
 * ## The problem this exists for
 *
 * A pill is a status colour as text on a tint of the *same* colour: `bg-codify-danger/40` with
 * `text-codify-danger`. The tint pulls the background toward the text, so the pair is far closer in
 * lightness than either is to the surface it sits on. Measured over the 19 themes, red on its own
 * 40% tint was under AA in 54 of 57 theme-and-surface combinations (worst 2.55:1), and the
 * contrast audit never saw it, because it drops translucent backgrounds.
 *
 * ## Why a derived token and not a fixed colour
 *
 * No single colour works: the surfaces and tones are different in every theme, and a fixed
 * near-white text would stop the pill saying which tone it is. So each theme's tone is moved in
 * *lightness only* (hue and saturation kept, which is what keeps `danger` red and the warm-arc rule
 * true) until it clears the floor against the tint, over every surface a pill sits on. It is the same
 * clamp a user's own tint goes through, so there is one rule for "make this readable".
 *
 * `applyTheme` writes the result as `--codify-X-ink` for every theme, so a theme needs no extra
 * tokens and a tint of `accent` gets its own ink. A pill uses `text-codify-X-ink`; everything else
 * (icons, borders, bare text on a surface) keeps the tone itself.
 *
 * ## Why 40% and 4.7
 *
 * 40% is the strongest resting tint any pill uses (`DESIGN.md` §2 caps it there, and
 * `contrast.test.ts` fails a class string above it): a weaker tint is further from the text and so
 * easier, so clearing it here clears them all. 4.7 and not 4.5 absorbs the rounding when a colour is
 * written as an 8-bit hex.
 */

/** The tones that get an ink: the status tones, the interactive hue, and the two mode accents. */
export const INK_TONES = ["accent", "info", "success", "warning", "danger", "design", "knowledge"] as const;
export type InkTone = (typeof INK_TONES)[number];

/** The strongest resting tint of a tone over a surface. */
export const TINT_ALPHA = 0.4;
/** What an ink has to reach against that tint. */
export const INK_FLOOR = 4.7;

/** The three surfaces text sits on, worst case last. */
const SURFACES = ["--codify-bg", "--codify-surface", "--codify-raised"] as const;

/**
 * The mode accents are not theme-managed (`index.css` explains why), so a theme's tokens do not carry
 * them; these are the `:root` values, which a test pins against the stylesheet.
 */
export const MODE_ACCENTS: Readonly<Record<"design" | "knowledge", string>> = {
  design: "#db61a2",
  knowledge: "#39c5cf",
};

/** The default theme's surfaces, for a token map that does not carry one. */
const DEFAULT_SURFACES: Readonly<Record<(typeof SURFACES)[number], string>> = {
  "--codify-bg": "#0d1117",
  "--codify-surface": "#161b22",
  "--codify-raised": "#21262d",
};

/** The custom-property name of a tone's ink. */
export const inkVar = (tone: InkTone): string => `--codify-${tone}-ink`;

/** `{ "--codify-danger-ink": "#fba6a1", … }` for one theme's resolved tokens. Pure. */
export function deriveToneInks(tokens: Readonly<Record<string, string>>): Record<string, string> {
  const surfaces = SURFACES.map((name) => tokens[name] ?? DEFAULT_SURFACES[name]);
  const inks: Record<string, string> = {};
  for (const tone of INK_TONES) {
    const base = tokens[`--codify-${tone}`] ?? MODE_ACCENTS[tone as "design" | "knowledge"];
    if (!base || !/^#[0-9a-fA-F]{6}$/.test(base)) continue;
    const tints = surfaces.map((surface) => composite(base, TINT_ALPHA, surface));
    // `null` means no lightness of this hue can clear the floor; the tone itself is the honest answer
    // then, and `ink.test.ts` fails if any shipped theme lands there.
    inks[inkVar(tone)] = clampForContrast(base, tints, INK_FLOOR) ?? base;
  }
  return inks;
}
