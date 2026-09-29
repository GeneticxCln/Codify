/**
 * WCAG 2.1 contrast arithmetic, in one place.
 *
 * **Why it is a module and not a helper inside a test.** Two tests that each
 * carry their own copy of `relativeLuminance` is two answers to "what is the
 * ratio", and the one that gets a gamma threshold wrong fails quietly in the
 * direction that looks like a passing suite. `transcriptPalette.test.ts`
 * already had a correct copy; it was lifted here verbatim rather than written
 * a second time, and that test now imports it. Nothing in the app renders text
 * — this is a measurement tool, which is why it is a module of pure functions
 * with no React and no DOM.
 *
 * The sRGB values are taken as they are *stored*, not as they are displayed.
 * Every theme publishes `#rrggbb`, so there is no colour space to convert out
 * of and the transfer function below is the one WCAG 2.1 specifies.
 */

/** `#0d1117` → `[13, 17, 23]`. Throws rather than guessing at a shorthand. */
export function hexToRgb(hex: string): [number, number, number] {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) throw new Error(`not a 6-digit hex: ${hex}`);
  const n = parseInt(m[1]!, 16);
  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff];
}

/** WCAG 2.1 relative luminance. */
export function relativeLuminance(hex: string): number {
  const linear = hexToRgb(hex).map((channel) => {
    const s = channel / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

/** The contrast ratio, always ≥ 1 and order-independent. */
export function contrastRatio(a: string, b: string): number {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort(
    (x, y) => y - x,
  );
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * `color: rgb(var(--codify-warning-rgb) / 0.8)` over an opaque background,
 * as the hex a screenshot would show.
 *
 * **Why this exists.** Contrast is defined on the colour the eye receives, and
 * 43 of the app's text utilities are a tone at less than full opacity —
 * `text-codify-warning/80` appears 17 times. Measuring the tone at full
 * strength would credit the text with contrast it does not have and let a real
 * failure through. The blend is the standard source-over in sRGB, which is what
 * the compositor does.
 */
export function composite(over: string, alpha: number, under: string): string {
  const fg = hexToRgb(over);
  const bg = hexToRgb(under);
  const mixed = fg.map((c, i) => Math.round(alpha * c + (1 - alpha) * bg[i]!));
  const hex = mixed
    .map((c) => c.toString(16).padStart(2, "0"))
    .join("");
  return `#${hex}`;
}

/**
 * The font weights a theme's text can be set at, as CSS numeric weights.
 *
 * **This is the part worth reading.** WCAG 1.4.3 relaxes the requirement for
 * "large" text — 18pt (24px) regular, or 14pt (18.66px) **bold**. Bold means
 * 700, not 600. A design system that treats `font-semibold` as the bold that
 * buys 3:1 is buying the exemption with a weight the specification does not
 * recognise, and it is the single most common way a palette passes an audit on
 * paper and fails one in a browser.
 */
export type FontWeight = 400 | 500 | 600 | 700 | 800;

/** px per point, as CSS defines it: 1pt = 96/72 px. */
const PX_PER_PT = 96 / 72;

/** AA for body text. The number most palettes are actually fighting. */
export const AA_NORMAL = 4.5;

/** AA for large text: 18pt regular, or 14pt bold. */
export const AA_LARGE = 3;

/**
 * WCAG 2.1 SC 1.4.11 Non-text Contrast: 3:1.
 *
 * **This is a different criterion and it is worth being precise about why, because
 * the same number appears in `AA_LARGE` for a reason that has nothing to do with
 * it.** 1.4.11 governs "visual information required to identify user interface
 * components and states, and graphical objects" — the boundary of a control, a
 * selected-state indicator, a scrollbar thumb, the stroke of a chart. It has
 * three carves-out, and they are the whole difficulty: *inactive* components,
 * *essential* presentations, and *logotypes*.
 *
 * The one that does the work here is "required to identify". A control that is
 * identified by its label and its fill does not also need its 1px border to
 * clear 3:1, and holding it to the number anyway is how a design system ends up
 * with dividers you can see from across the room. So the number is not the hard
 * part; deciding what it applies to is, and
 * `ui/tests/nonTextContrast.test.ts` makes those decisions in a table, one named
 * object at a time, rather than applying the number to every colour in the
 * palette. See that file's docstring for the objects and the exemptions.
 */
export const AA_NON_TEXT = 3;

/**
 * The AA ratio a run of text has to clear, from the size and weight it is set
 * at. Size in px, weight as a CSS number.
 */
export function aaThreshold(sizePx: number, weight: FontWeight = 400): number {
  const isLarge =
    sizePx >= 18 * PX_PER_PT ||
    (sizePx >= 14 * PX_PER_PT && weight >= 700);
  return isLarge ? AA_LARGE : AA_NORMAL;
}

/**
 * A one-line description of what a run of text has to clear, for a failure
 * message. The size and weight go in it because "4.5:1" on its own does not say
 * which of the two rules was applied, and the whole point of carrying the
 * weight around is that the answer differs.
 */
export function describeRequirement(sizePx: number, weight: FontWeight = 400): string {
  const threshold = aaThreshold(sizePx, weight);
  return (
    threshold === AA_LARGE
      ? `${sizePx}px at weight ${weight} counts as large text, so ${AA_LARGE}:1`
      : `${sizePx}px at weight ${weight} is body text, so ${AA_NORMAL}:1`
  );
}

/**
 * A one-line description of what a non-text object has to clear.
 *
 * The adjacency goes in the message, because "3:1" alone does not say *against
 * what*, and getting that wrong is the mistake this criterion invites: a
 * scrollbar thumb is adjacent to its track, and a control's hover border is
 * adjacent to that control's own fill, and those are different colours in every
 * theme.
 */
export function describeGraphicRequirement(object: string, adjacent: string): string {
  return `${object} against ${adjacent} has to clear ${AA_NON_TEXT}:1 (SC 1.4.11)`;
}
