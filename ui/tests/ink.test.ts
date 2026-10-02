/**
 * The status ink: a tone's text colour on its own tint, readable in every theme.
 *
 * `contrast.test.ts` measures the pairs a class string declares; this file owns the *derivation* that
 * makes the pill pair pass, and checks it where it can fail: over all 19 themes, over every surface a
 * pill sits on, against the tint the stylesheet actually paints (`bg-codify-X/40`). It reads no
 * source text: what `index.css` must carry for an ink (the rule, the `:root` fallback, the mode
 * accents) is checked beside the rest of the runtime utility table, in `toneUtilities.test.ts`.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { THEMES, MANAGED_VARS, applyTheme } = await import("../src/appearance.ts");
const { contrastRatio, composite } = await import("../src/contrast.ts");
const { INK_TONES, INK_FLOOR, TINT_ALPHA, MODE_ACCENTS, deriveToneInks, inkVar } = await import("../src/toneInk.ts");

const SURFACES = ["--codify-bg", "--codify-surface", "--codify-raised"] as const;
/** The default theme *with its tones*: the raw exported theme object has surfaces only, before they are merged in. */
const DARK = THEMES.find((t) => t.id === "codify-dark")!;

/** A theme's resolved tokens: what `applyTheme` would publish. */
const tokensOf = (theme: { tokens: Record<string, string> }): Record<string, string> => ({ ...theme.tokens });

const toneHex = (tokens: Record<string, string>, tone: string): string =>
  tokens[`--codify-${tone}`] ?? MODE_ACCENTS[tone as "design" | "knowledge"];

/** Hue in degrees, and chroma (max minus min channel, 0 to 1): how far from grey a colour really is. */
function hsl(hex: string): { h: number; c: number } {
  const n = parseInt(hex.slice(1), 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => v / 255);
  const max = Math.max(r!, g!, b!), min = Math.min(r!, g!, b!);
  const d = max - min;
  if (d === 0) return { h: 0, c: 0 };
  let h = max === r ? ((g! - b!) / d) % 6 : max === g ? (b! - r!) / d + 2 : (r! - g!) / d + 4;
  h *= 60;
  return { h: h < 0 ? h + 360 : h, c: d };
}

test("there is an ink for every tone, in every theme", () => {
  assert.equal(THEMES.length, 19);
  for (const theme of THEMES) {
    const inks = deriveToneInks(tokensOf(theme));
    for (const tone of INK_TONES) {
      assert.match(inks[inkVar(tone)] ?? "", /^#[0-9a-f]{6}$/i, `${theme.id}: no ink for ${tone}`);
    }
  }
});

test("every ink clears AA on its own 40% tint, over every surface a pill sits on, in every theme", () => {
  const failures: string[] = [];
  for (const theme of THEMES) {
    const tokens = tokensOf(theme);
    const inks = deriveToneInks(tokens);
    for (const tone of INK_TONES) {
      const ink = inks[inkVar(tone)]!;
      for (const surface of SURFACES) {
        const tint = composite(toneHex(tokens, tone), TINT_ALPHA, tokens[surface]!);
        const ratio = contrastRatio(ink, tint);
        if (ratio < 4.5) failures.push(`${theme.id} ${tone} on ${surface}: ${ratio.toFixed(2)}`);
      }
    }
  }
  assert.deepEqual(failures, [], "a pill would be unreadable here:\n" + failures.join("\n"));
  assert.ok(INK_FLOOR >= 4.5 && TINT_ALPHA === 0.4);
});

test("an ink keeps its tone's hue, so danger is still red and warning still warm", () => {
  for (const theme of THEMES) {
    const tokens = tokensOf(theme);
    const inks = deriveToneInks(tokens);
    for (const tone of INK_TONES) {
      const base = hsl(toneHex(tokens, tone));
      const ink = hsl(inks[inkVar(tone)]!);
      // A tone with almost no chroma (platinum, grey) has no stable hue at 8 bits: a one-step change in
      // one channel swings it by degrees. For those, what matters is that it stays near-grey. (Chroma,
      // not HSL saturation, which is high for any very light colour however grey it looks.)
      if (base.c < 0.12) {
        assert.ok(ink.c < 0.2, `${theme.id} ${tone}: a near-grey tone gained colour (${ink.c.toFixed(2)})`);
        continue;
      }
      const drift = Math.min(Math.abs(base.h - ink.h), 360 - Math.abs(base.h - ink.h));
      assert.ok(drift <= 4, `${theme.id} ${tone}: hue moved ${drift.toFixed(1)} degrees`);
    }
  }
});

test("a tone that already clears the floor is not changed", () => {
  const ink = deriveToneInks({
    "--codify-bg": "#000000",
    "--codify-surface": "#000000",
    "--codify-raised": "#000000",
    "--codify-danger": "#ffffff",
  });
  assert.equal(ink["--codify-danger-ink"], "#ffffff");
});

/** A style target that remembers what was set, and lets a test read it back. */
function fakeRoot() {
  const props = new Map<string, string>();
  return {
    props,
    removeProperty: (name: string) => void props.delete(name),
    setProperty: (name: string, value: string) => void props.set(name, value),
  };
}

test("applyTheme writes each ink and its channels, and the next theme replaces them", () => {
  const root = fakeRoot();
  applyTheme("codify-dark", root);
  const dark = deriveToneInks(tokensOf(THEMES.find((t) => t.id === "codify-dark")!));
  for (const tone of INK_TONES) {
    assert.equal(root.props.get(inkVar(tone)), dark[inkVar(tone)]);
    assert.match(root.props.get(`${inkVar(tone)}-rgb`) ?? "", /^\d+ \d+ \d+$/);
  }
  const before = root.props.get("--codify-danger-ink");

  applyTheme("cmatrix-oled", root);
  assert.notEqual(root.props.get("--codify-danger-ink"), before, "the ink survived a theme switch");
  const oled = deriveToneInks(tokensOf(THEMES.find((t) => t.id === "cmatrix-oled")!));
  assert.equal(root.props.get("--codify-danger-ink"), oled["--codify-danger-ink"]);
});

test("a tint of the accent gets its own ink, not the theme's", () => {
  const root = fakeRoot();
  applyTheme("codify-dark", root);
  const stock = root.props.get("--codify-accent-ink");
  applyTheme("codify-dark", root, { "--codify-accent": "#ff00aa" });
  assert.notEqual(root.props.get("--codify-accent-ink"), stock);
  const expected = deriveToneInks({ ...tokensOf(DARK), "--codify-accent": "#ff00aa" });
  assert.equal(root.props.get("--codify-accent-ink"), expected["--codify-accent-ink"]);
});

test("every ink variable is managed, so a theme switch cannot leave one behind", () => {
  for (const tone of INK_TONES) {
    assert.ok((MANAGED_VARS as readonly string[]).includes(inkVar(tone)), `${inkVar(tone)} is not managed`);
    assert.ok((MANAGED_VARS as readonly string[]).includes(`${inkVar(tone)}-rgb`), `${inkVar(tone)}-rgb is not managed`);
  }
});
