/**
 * The Appearance pane's layout, as tests.
 *
 * This pane is the only place a user goes to compare themes, so its shape *is*
 * the feature — and shape is exactly what a component test cannot see. Rendered
 * in isolation, `ThemeRow` and `ThemeDetail` are correct whether they are stacked
 * in a grid of eight or listed beside a preview. What was wrong was the
 * arrangement: eight cards meant the eighth theme was two scrolls down a modal,
 * and it meant eight live animation loops inside the window someone opened to
 * read a setting.
 *
 * So these read the committed source and the rendered markup together. The
 * canvas count is the assertion that earns its keep — it is the difference
 * between "navigable" as a claim and "navigable" as a fact, because a grid
 * would render one canvas per theme and a list renders one per screen.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { CMATRIX_OLED, CYBERPUNK_NEON, MOTIONLESS_THEME_IDS, THEMES } from "../src/appearance.ts";

const { AppearancePane } = await import("../src/components/AppearancePane.tsx");

const source = readFileSync(
  new URL("../src/components/AppearancePane.tsx", import.meta.url),
  "utf8",
);

const markup = (): string => renderToStaticMarkup(React.createElement(AppearancePane));
const canvases = (html: string): number => (html.match(/<canvas/g) ?? []).length;

test("every theme is a row, so the last one is not two scrolls down a modal", () => {
  const out = markup();
  const radios = out.match(/role="radio"/g) ?? [];
  assert.equal(
    radios.length,
    THEMES.length,
    "the list is not showing one row per theme, so some themes are only reachable by scrolling",
  );
  for (const theme of THEMES) {
    assert.ok(
      out.includes(theme.label),
      `${theme.id} has no row — the list dropped a theme instead of adding one`,
    );
  }
});

test("one canvas on screen, whichever theme you are looking at", () => {
  // The regression this whole layout exists to prevent. A rainy theme and a
  // grid theme both used to draw a canvas *per theme*; the list draws one for
  // the theme being inspected and none for the rest.
  for (const theme of [CMATRIX_OLED, CYBERPUNK_NEON]) {
    localStorage.setItem("codify.theme", theme.id);
    const count = canvases(markup());
    assert.equal(
      count,
      1,
      `${theme.id} rendered ${count} canvases — one per theme is what the detail panel replaced`,
    );
  }
  localStorage.setItem("codify.theme", THEMES[0].id);
});

test("a theme with no weather draws no canvas at all", () => {
  // Both declared-motionless themes, not just the default: a preview that
  // cannot show a theme is worse than no preview, and `still` is the theme
  // where a missing canvas is the entire feature.
  for (const theme of THEMES.filter((t) => MOTIONLESS_THEME_IDS.includes(t.id))) {
    localStorage.setItem("codify.theme", theme.id);
    assert.equal(
      canvases(markup()),
      0,
      `${theme.id} declares itself motionless and still drew a canvas`,
    );
  }
  localStorage.setItem("codify.theme", THEMES[0].id);
});

test("the pane picks no colour of its own", () => {
  // The same rule the transcript obeys (`transcriptPalette.test.ts`), and this
  // is the place it bites hardest: a hardcoded chip inside a *theme chooser* is
  // the one blue the user cannot explain. The old "active" badge was
  // `bg-blue-950/40 border-blue-800/60 text-blue-300`, which is the bug.
  //
  // `green` is deliberately *not* in the banned set, and the exemption is
  // narrow rather than general: the status green is load-bearing, not decor
  // (DESIGN.md §2), and the dark theme's preview shows it precisely because it
  // is the only saturated thing in that palette. The next test pins it to the
  // one place it belongs, so the exemption cannot quietly become a licence.
  const code = source
    .replace(/(^|[({])[ \t]*\/\*[\s\S]*?\*\//gm, "$1")
    .replace(/^[ \t]*\/\/.*$/gm, "");
  const hue = code.match(/\b(?:bg|text|border)-(?:blue|purple|violet|red|yellow|cyan)-\d/);
  assert.equal(
    hue?.[0],
    undefined,
    `the Appearance pane picks a hue again: ${hue?.[0]} — it is the one surface that must not`,
  );
});

test("the status green stays the status green, in the one place it means something", () => {
  const code = source
    .replace(/(^|[({])[ \t]*\/\*[\s\S]*?\*\//gm, "$1")
    .replace(/^[ \t]*\/\/.*$/gm, "");
  assert.equal(
    (code.match(/bg-green-\d+/g) ?? []).length,
    1,
    "the status green is spreading past the dark theme's ramp, where it is a demonstration and not decoration",
  );
  assert.match(
    code,
    /CODIFY_DARK\.id &&[\s\S]{0,200}bg-green-\d+/,
    "the status green is no longer shown as part of the default theme's preview",
  );
  // And it belongs to the default alone. The preview's ramp is shared with every
  // deliberately motionless theme, so the guard is what keeps the green from
  // leaking into `still` — which is a near-monochrome palette on purpose, and
  // one saturated chip in its preview would undo the whole point of it.
  assert.match(
    code,
    /\{still &&[\s\S]{0,600}CODIFY_DARK\.id &&/,
    "the status green is no longer gated on the default theme",
  );
});

test("the radiogroup keeps the promise its role makes", () => {
  // `role="radiogroup"` promises arrow-key navigation and the DOM does not
  // deliver it on its own, so the handler has to exist and has to be asserted
  // rather than assumed.
  assert.match(source, /role="radiogroup"/);
  assert.match(
    source,
    /onKeyDown/,
    "the group declares radio semantics but has no keyboard handler, which is the broken half of the pattern",
  );
  assert.match(source, /ArrowDown/, "arrow keys do nothing, so the group is a set of buttons wearing a radio's name");
});
