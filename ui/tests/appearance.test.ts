/**
 * The themes module, pinned where a render test cannot reach it.
 *
 * `AppearancePane.tsx` proves the tiles draw; what makes a theme *safe* lives
 * here — that switching clears what the last theme set (the difference between
 * switching and layering), that an unknown stored id falls back to the default
 * rather than rendering an unnamed state, and that the OLED theme exports the
 * three variables the requirement names, at the values the requirement names.
 * Every test drives a fake storage/style target, so nothing here touches a real
 * `document` or `localStorage` — the module's own injectability is what makes
 * the suite hermetic.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { deriveToneInks, inkVar } from "../src/toneInk.ts";
import { relativeLuminance } from "../src/contrast.ts";
import {
  CMATRIX_OLED,
  CODIFY_DARK,
  DEFAULT_THEME_ID,
  MANAGED_VARS,
  STATUS_TONE_VARS,
  THEMES,
  THEME_TONES,
  applyTheme,
  hexChannels,
  readStoredThemeId,
  storeThemeId,
  themeById,
  type CssStyleTarget,
  type ThemeStorage,
} from "../src/appearance.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** A storage that starts empty and records what was written. */
function fakeStorage(): ThemeStorage & { map: Map<string, string> } {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
  };
}

/** A style target that records remove/set calls in order. */
function fakeStyle(): CssStyleTarget & { calls: string[]; props: Map<string, string> } {
  const calls: string[] = [];
  const props = new Map<string, string>();
  return {
    calls,
    props,
    removeProperty: (name: string) => {
      calls.push(`-${name}`);
      props.delete(name);
    },
    setProperty: (name: string, value: string) => {
      calls.push(`+${name}`);
      props.set(name, value);
    },
  };
}

test("the OLED CMatrix theme exports exactly the requirement's three variables", () => {
  assert.equal(CMATRIX_OLED.tokens["--cmatrix-bg"], "#000000");
  assert.equal(CMATRIX_OLED.tokens["--cmatrix-rain"], "#003300");
  assert.equal(CMATRIX_OLED.tokens["--cmatrix-text"], "#a3ffb8");
});

test("OLED CMatrix body text is the high-contrast light green it publishes", () => {
  // `--cmatrix-text` and `--codify-secondary` are one decision, not two that
  // can drift: a theme whose text dimmed while its rain stayed bright would
  // be two themes wearing one label.
  assert.equal(CMATRIX_OLED.tokens["--codify-secondary"], CMATRIX_OLED.tokens["--cmatrix-text"]);
});

test("the default theme restates index.css's :root values, byte for byte", () => {
  const css = readFileSync(path.join(HERE, "..", "src", "index.css"), "utf8");
  for (const [name, value] of Object.entries(CODIFY_DARK.tokens)) {
    const match = css.match(new RegExp(`${name}\\s*:\\s*(#[0-9a-fA-F]{6})`));
    assert.ok(match, `index.css does not declare ${name}`);
    assert.equal(
      value,
      match[1],
      `${name}: the default theme and index.css disagree. "Dark" and "no theme " +\n        "chosen" must be the same picture`,
    );
  }
});

/** Hue in degrees, 0–360, for a `#rrggbb`. Returns NaN for anything else, which
 * makes a malformed value fail every assertion below rather than pass one. */
function hueOf(hex: string): number {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex.trim());
  if (!m) return NaN;
  const [r, g, b] = [1, 2, 3].map((i) => parseInt(m[i], 16) / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  if (d === 0) return 0;
  let h: number;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  h *= 60;
  return h < 0 ? h + 360 : h;
}

/** A grey has no hue at all: `hueOf` reports 0° for it, which is red, so a grey would pass `isWarm` by accident. */
function isGrey(hex: string): boolean {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex.trim());
  return m !== null && m[1]!.toLowerCase() === m[2]!.toLowerCase() && m[2]!.toLowerCase() === m[3]!.toLowerCase();
}

/** A theme is monochrome when every one of its six tones is a grey: severity then has to be brightness. */
const isMonochrome = (theme: (typeof THEMES)[number]): boolean =>
  STATUS_TONE_VARS.every((v) => isGrey(theme.tokens[v] as string));

/** True for red → orange → gold, the arc `danger` and `warning` must stay on. */
function isWarm(hex: string): boolean {
  // A grey is not warm: it is not anything. Where a grey is allowed is stated below, not inferred from a hue of 0.
  if (isGrey(hex)) return false;
  const h = hueOf(hex);
  // `h >= 345 || h <= 70` is the warm arc across the 0° seam: a red at 350 and
  // a gold at 65 are both "warm", and a green at 140 is neither.
  return Number.isFinite(h) && (h >= 345 || h <= 70);
}

test("every theme states all six tones or none", () => {
  // All six, or the theme has said nothing. A half-stated theme is the worst of
  // the three options: `applyTheme` clears every managed variable before it
  // applies the next theme, so the three it did not set would show the
  // *previous* theme's values — which is the exact failure clearing exists to
  // prevent, arriving through the door clearing was built to keep shut.
  for (const theme of THEMES) {
    const stated = STATUS_TONE_VARS.filter((v) => theme.tokens[v] !== undefined);
    assert.equal(
      stated.length,
      STATUS_TONE_VARS.length,
      `${theme.id} states ${stated.length} of ${STATUS_TONE_VARS.length} tones: ` +
        `${stated.join(", ") || "(none)"}`,
    );
  }
});

test("a theme may restate failure, and may not make failure cyan", () => {
  // The rule that replaced "a theme may not touch the status hues". That rule
  // protected a meaning; this protects the same meaning and lets the hue move.
  // `danger` and `warning` have to stay on the warm arc, because a user who has
  // learned that orange means "needs attention" was owed that much — and a
  // cyberpunk theme with a pink `danger` is the failure this stops.
  for (const theme of THEMES) {
    for (const name of ["--codify-danger", "--codify-warning"] as const) {
      const value = theme.tokens[name];
      assert.ok(value, `${theme.id} does not state ${name}`);
      // The one carve-out: a theme whose whole palette is grey has no hue to put on the arc, and says
      // severity in brightness instead (the test below owns that). A grey `danger` in a theme that
      // otherwise has colour is the failure this rule exists for, and is not excused.
      if (isMonochrome(theme)) continue;
      assert.ok(
        isWarm(value as string),
        `${theme.id} puts ${name} at ${value}, which is ` +
          (isGrey(value as string)
            ? "a grey in a theme that has colour — only an all-grey theme may say severity in brightness"
            : `hue ${Math.round(hueOf(value as string))}° — off the warm arc`),
      );
    }
  }
});

test("a monochrome theme says severity in brightness, in the order failure, warning, success, idle", () => {
  // ASCII Rain publishes no hue, so brightness is all it has to say "failed" from "passed". The
  // order is the severity order: the brightest thing on a black screen is the failure, then the
  // warning, then the success, and idle is the dimmest. It was `warning` #757575 against `success`
  // #a3a3a3, which read as the quieter of the two, and no test noticed because the warm-arc rule
  // gave every grey a hue of 0°. The rule is stated for the theme by name, not inferred.
  const mono = THEMES.filter(isMonochrome);
  assert.deepEqual(
    mono.map((t) => t.id),
    ["ascii-rain"],
    "the set of all-grey themes changed: a theme that went grey needs this rule, and one that gained colour no longer does",
  );
  for (const theme of mono) {
    const lum = (v: string): number => relativeLuminance(theme.tokens[v as keyof typeof theme.tokens] as string);
    const chain = ["--codify-danger", "--codify-warning", "--codify-success", "--codify-neutral"];
    for (let i = 0; i + 1 < chain.length; i += 1) {
      // A step of 0.04 in relative luminance is what makes two greys tell apart at a glance on a
      // black screen; anything closer is the same sentence.
      assert.ok(
        lum(chain[i]!) - lum(chain[i + 1]!) >= 0.04,
        `${theme.id}: ${chain[i]} is not brighter than ${chain[i + 1]} by a visible step, so severity is not in order`,
      );
    }
    // The ink is what a pill is read in; it may be lifted to clear the floor but may never reorder the tones.
    const inks = deriveToneInks(theme.tokens as Record<string, string>);
    const inkLum = (tone: "danger" | "warning" | "success"): number =>
      relativeLuminance(inks[inkVar(tone)]!);
    assert.ok(inkLum("danger") >= inkLum("warning") && inkLum("warning") >= inkLum("success"),
      `${theme.id}: the pill inks are out of severity order`);
  }
});

test("only a monochrome theme may state a grey danger or warning", () => {
  for (const theme of THEMES) {
    if (isMonochrome(theme)) continue;
    for (const name of ["--codify-danger", "--codify-warning"] as const) {
      assert.ok(!isGrey(theme.tokens[name] as string), `${theme.id}: ${name} is grey in a theme that has colour`);
    }
  }
});

test("no theme states a tone inline, where it would silently beat the table", () => {
  // `withTones` folds `THEME_TONES` in *underneath* a theme's own tokens, so an
  // inline tone would win. That is a real escape hatch rather than a
  // hypothetical one, and it is also a place where the two halves of this file
  // could disagree without anything noticing.
  for (const theme of THEMES) {
    const tones = THEME_TONES[theme.id];
    for (const name of STATUS_TONE_VARS) {
      assert.equal(
        theme.tokens[name],
        tones?.[name],
        `${theme.id} publishes ${name} outside THEME_TONES, so the table and ` +
          "the theme disagree",
      );
    }
  }
});

test("the badge layer names theme variables, not Tailwind hues", () => {
  // The other half of the change. Four of `Badge.tsx`'s five tones were
  // `bg-green-950/40 text-green-400 border-green-800` — literals, so a success
  // pill in the OLED app was Tailwind green. Read the file and assert the four
  // saturated tones name a variable, because a re-introduced literal is exactly
  // the regression and it is invisible at runtime until someone picks a theme.
  const source = readFileSync(
    path.join(HERE, "..", "src", "components", "ui", "Badge.tsx"),
    "utf8",
  );
  // Comments are stripped first, and not as a nicety: this file's own docstring
  // quotes the literals it replaced, so a match on raw text would report a
  // regression that is only a paragraph explaining one.
  const code = source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*$/gm, "");
  const hues = code.match(/\b(?:bg|text|border)-(?:blue|green|amber|red|emerald|teal|rose|orange)-\d+/g);
  assert.deepEqual(
    hues,
    null,
    `Badge.tsx names Tailwind hues: ${(hues ?? []).join(", ")} — those compile ` +
      "to one colour forever, which is why a theme could not reach a badge",
  );
});

test("applyTheme clears what the last theme set before applying the new one", () => {
  const style = fakeStyle();
  applyTheme(CMATRIX_OLED.id, style);
  assert.equal(style.props.get("--codify-bg"), "#000000");
  assert.equal(style.props.get("--cmatrix-rain"), "#003300");

  applyTheme(CODIFY_DARK.id, style);
  // OLED's greens are gone, not layered under dark's values.
  assert.equal(style.props.get("--codify-bg"), "#0d1117");
  assert.ok(!style.props.has("--cmatrix-rain"), "the rain variable survived the switch");
  // And the clearing happened for every managed property, not just the ones
  // the outgoing theme happened to set.
  for (const name of MANAGED_VARS) {
    assert.ok(style.calls.includes(`-${name}`), `switching never cleared ${name}`);
  }
});

test("applyTheme with no target is a no-op, not a throw", () => {
  // `node --test` has no `document`; the module must stay importable there.
  assert.deepEqual(applyTheme(CMATRIX_OLED.id, undefined), []);
});

test("an unknown stored id falls back to the default, not to a broken state", () => {
  const storage = fakeStorage();
  assert.equal(readStoredThemeId(storage), DEFAULT_THEME_ID, "empty storage reads the default");
  storeThemeId("something-else", storage);
  assert.equal(readStoredThemeId(storage), DEFAULT_THEME_ID, "a stale id reads the default");
  storeThemeId(CMATRIX_OLED.id, storage);
  assert.equal(readStoredThemeId(storage), CMATRIX_OLED.id);
});

test("the boot path reads, applies, and lands on the id it was given", () => {
  // Boot is `readStoredThemeId` + the apply, composed by the caller: `main.tsx`
  // hands the id to `tint.ts`'s `applyTintedTheme`, which merges the stored
  // tints and then calls `applyTheme` under this hood. There is no single
  // `initAppearance` in `appearance.ts` to call, and that is the point — a boot
  // helper living here could not see the tints without a circular import, and
  // would have been a second path that applied the palette without them. Both
  // halves are asserted through the injectable seam the real calls use.
  const storage = fakeStorage();
  storeThemeId(CMATRIX_OLED.id, storage);
  const style = fakeStyle();
  const applied = readStoredThemeId(storage);
  applyTheme(applied, style);
  assert.equal(applied, CMATRIX_OLED.id);
  assert.equal(style.props.get("--codify-bg"), "#000000");
  assert.equal(style.props.get("--cmatrix-text"), "#a3ffb8");
});

test("hexChannels derives the triplets the utility overrides compose", () => {
  assert.equal(hexChannels("#0d1117"), "13 17 23");
  assert.equal(hexChannels("#003300"), "0 51 0");
  assert.equal(hexChannels("#a3ffb8"), "163 255 184");
  assert.throws(() => hexChannels("black"), /not a #rrggbb/);
});

test("applying a theme also derives every hex's -rgb triplet", () => {
  const style = fakeStyle();
  applyTheme(CMATRIX_OLED.id, style);
  assert.equal(style.props.get("--codify-bg-rgb"), "0 0 0");
  assert.equal(style.props.get("--codify-secondary-rgb"), hexChannels("#a3ffb8"));
  // The index.css override layer must never name a triplet the module does
  // not write, and vice versa: both are one contract about which utilities
  // follow the theme.
  const css = readFileSync(path.join(HERE, "..", "src", "index.css"), "utf8");
  const overridden = new Set(
    [...css.matchAll(/--codify-([\w-]+)-rgb/g)].map((m) => m[1]),
  );
  const derived = MANAGED_VARS.filter(
    (v) => v.startsWith("--codify-") && v.endsWith("-rgb"),
  ).map((v) => v.slice("--codify-".length, -"-rgb".length));
  for (const name of derived) {
    assert.ok(
      overridden.has(name),
      `applyTheme derives --codify-${name}-rgb but no utility override in index.css reads it`,
    );
  }
});

test("themeById resolves every listed theme and nothing else", () => {
  for (const theme of THEMES) assert.equal(themeById(theme.id), theme);
  assert.equal(themeById("nope"), CODIFY_DARK);
});

// ── the pane and the rain, rendered ────────────────────────────────────────

const { AppearancePane } = await import("../src/components/AppearancePane.tsx");
const { MatrixRain } = await import("../src/components/ui/MatrixRain.tsx");
const { RainBackdrop } = await import("../src/components/ui/RainBackdrop.tsx");

const markup = (el: React.ReactElement): string => renderToStaticMarkup(el);

test("AppearancePane renders one radio per theme, none pre-checked by a test process", () => {
  // The pane reads `localStorage` for its initial selection; the harness's
  // stub starts empty, so the default id is what renders — but *which* tile
  // starts active is storage's answer, not this assertion's business.
  const out = markup(React.createElement(AppearancePane));
  assert.match(out, /role="radiogroup"/);
  assert.match(out, /aria-checked="(?:true|false)"/);
  assert.match(out, /Codify Dark/);
  assert.match(out, /OLED CMatrix/);
});

test("the OLED tile names the three hexes the requirement asks for", () => {
  const out = markup(React.createElement(AppearancePane));
  assert.match(out, /#000000/);
  assert.match(out, /#003300/);
  assert.match(out, /#a3ffb8/);
});

test("MatrixRain renders a canvas, aria-hidden, and no canvas painting at import", () => {
  const out = markup(
    React.createElement(MatrixRain, { animated: false, className: "w-20 h-14" }),
  );
  assert.match(out, /<canvas/);
  assert.match(out, /aria-hidden="true"/);
  assert.match(out, /pointer-events-none/);
  // The effect never runs under renderToStaticMarkup, so a render must not
  // be where the frames start — that is the component staying importable.
  assert.doesNotMatch(out, /width="[1-9]/);
});

test("RainBackdrop mounts only for a theme that publishes a rain variable", () => {
  // Driven through the real mechanism — the persisted id the hook snapshots —
  // on the harness's map-backed localStorage stub, not through props a real
  // caller would never pass.
  localStorage.setItem("codify.theme", CMATRIX_OLED.id);
  const rain = markup(React.createElement(RainBackdrop));
  assert.match(rain, /<canvas/);
  // The vignette that keeps §1's promise: rain behind the words, not over them.
  assert.match(rain, /radial-gradient/);

  localStorage.setItem("codify.theme", CODIFY_DARK.id);
  const none = markup(React.createElement(RainBackdrop));
  assert.equal(none, "", "the default theme gets no backdrop — not even an empty div");
});

test("the two snow themes differ by what they publish, not by a second canvas", () => {
  // Two themes sharing one painter is the design — `SnowFall.tsx` is one file for
  // both — and it is also the thing most likely to rot into "the same theme listed
  // twice": someone adds a second snow entry, copies the tokens, and the picker now
  // offers Winter Snow and Festive Night as two names for one picture. What stops
  // that is the mechanism, so the test is on the mechanism — one shared trigger,
  // and the glow published by exactly one of the two.
  const winter = themeById("winter-snow");
  const festive = themeById("festive-night");

  // Both mount the same canvas, so both must publish the trigger.
  assert.ok(winter.tokens["--snow-flake"], "winter publishes no snow trigger");
  assert.ok(festive.tokens["--snow-flake"], "festive publishes no snow trigger");

  // Exactly one has lights. This single asymmetry is the whole difference between
  // the two themes, and it is why the painter never asks which theme it is
  // running: absence is the signal, so it has to actually be absent.
  assert.equal(
    winter.tokens["--snow-glow"],
    undefined,
    "winter publishes a glow, so it has lights — and then it is festive",
  );
  assert.ok(
    festive.tokens["--snow-glow"],
    "festive has no lights, so the two themes are the same picture",
  );

  // And the palettes are their own, not a copy with the label changed.
  assert.notEqual(
    winter.tokens["--codify-bg"],
    festive.tokens["--codify-bg"],
    "both snow themes share a background",
  );
  assert.notEqual(
    winter.tokens["--snow-flake"],
    festive.tokens["--snow-flake"],
    "both snow themes share a flake colour",
  );
});
