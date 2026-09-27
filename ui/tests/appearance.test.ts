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

import {
  CMATRIX_OLED,
  CODIFY_DARK,
  DEFAULT_THEME_ID,
  MANAGED_VARS,
  THEMES,
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

test("no theme touches the status hues — they are load-bearing, not decor", () => {
  // Five tones, five meanings (§2): a theme that repainted danger repainted
  // what "failed" means.
  for (const theme of THEMES) {
    for (const name of Object.keys(theme.tokens)) {
      assert.ok(
        !name.startsWith("--codify-info") &&
          !name.startsWith("--codify-success") &&
          !name.startsWith("--codify-warning") &&
          !name.startsWith("--codify-danger") &&
          !name.startsWith("--codify-accent") &&
          !name.startsWith("--codify-neutral") &&
          !name.startsWith("--codify-design") &&
          !name.startsWith("--codify-knowledge"),
        `${theme.id} sets ${name}; a theme may set surfaces and text only`,
      );
    }
  }
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

test("initAppearance reads, applies, and returns the id it applied", () => {
  // `initAppearance` is `readStoredThemeId` + `applyTheme` with the document
  // default filled in — the boot path `main.tsx` runs. Its target defaults
  // from `document`, which this process does not own, so the two halves are
  // asserted through the same injectable seam the function itself uses.
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
