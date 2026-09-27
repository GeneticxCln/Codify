/**
 * `still`, and the rule it exists to stress.
 *
 * DESIGN.md §7 says motion must be justified. That is a rule with nothing behind
 * it until something is willing to bet a whole theme on the possibility that the
 * honest answer is *none* — and when that theme landed, the check that used to
 * encode the rule turned out to be an accident. It read
 * `assert.equal(theme.id, CODIFY_DARK.id)`, which says something true about the
 * default rather than anything about motion: it would have rejected a second
 * motionless theme for being motionless.
 *
 * So the rule became `MOTIONLESS_THEME_IDS`, a list a person maintains, and
 * these tests hold the two halves of it. The first is the half that is easy —
 * `still` draws no canvas. The second is the half that matters: **every theme
 * must be one or the other.** A theme that is neither animated-by-declaration
 * nor motionless-by-declaration is a theme whose canvas silently never mounts,
 * and that is the exact failure the atmosphere pairing tests were built to catch,
 * arriving through the front door.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import {
  ATMOSPHERE_TRIGGERS,
  CMATRIX_OLED,
  CODIFY_DARK,
  CYBERPUNK_NEON,
  MOTIONLESS_THEME_IDS,
  STILL,
  THEMES,
  isMotionless,
} from "../src/appearance.ts";

const { WeatherBackdrop } = await import("../src/components/ui/WeatherBackdrop.tsx");
const { RainBackdrop } = await import("../src/components/ui/RainBackdrop.tsx");

const weather = (): string => renderToStaticMarkup(React.createElement(WeatherBackdrop));

test("every theme is either animated by declaration or motionless by declaration", () => {
  // The stress test, and the reason `MOTIONLESS_THEME_IDS` is a list rather than
  // an inline id. There is no third option: a theme nobody declared is a theme
  // whose canvas never mounts, and the suite says so instead of the user finding
  // out in a settings window.
  for (const theme of THEMES) {
    const animated = ATMOSPHERE_TRIGGERS.some((t) => t.themes.includes(theme.id));
    const declaredStill = MOTIONLESS_THEME_IDS.includes(theme.id);
    assert.ok(
      animated !== declaredStill,
      `${theme.id} is ${animated ? "animated" : "motionless"} but is not declared as such — ` +
        (animated
          ? "drop it from MOTIONLESS_THEME_IDS"
          : "give it a trigger, or list it as deliberately motionless"),
    );
  }
});

test("still is on the motionless list, and the default is on it for a different reason", () => {
  assert.ok(MOTIONLESS_THEME_IDS.includes(STILL.id), "still is not declared motionless");
  assert.ok(MOTIONLESS_THEME_IDS.includes(CODIFY_DARK.id));
  // The two are on the same list for opposite reasons, and a test that only
  // counted the list would not know that: the default never had an effect,
  // `still` is *about* having none.
  assert.ok(isMotionless(STILL.id));
  assert.ok(!isMotionless(CMATRIX_OLED.id));
});

test("still publishes no weather variable, so nothing can watch one", () => {
  for (const [name, value] of Object.entries(STILL.tokens)) {
    assert.ok(
      !/^--(cmatrix|cyber|neural|hud|abyss|flare)/.test(name),
      `still publishes ${name} (${value}); a motionless theme that publishes a trigger variable gets a canvas`,
    );
  }
  for (const entry of ATMOSPHERE_TRIGGERS) {
    assert.ok(
      !entry.themes.includes(STILL.id),
      `still is named in the trigger table for ${entry.trigger}, which is the opposite of the theme's whole point`,
    );
  }
});

test("still draws no canvas in the app or in the settings pane", () => {
  for (const id of MOTIONLESS_THEME_IDS) {
    localStorage.setItem("codify.theme", id);
    assert.equal(weather(), "", `${id} is motionless and the app still mounted an atmosphere`);
    assert.equal(
      renderToStaticMarkup(React.createElement(RainBackdrop)),
      "",
      `${id} is motionless and the rain backdrop still mounted`,
    );
  }
  // And the other side of the same door: a theme that does publish a trigger
  // still gets its canvas, so "motionless" is per-theme and not a global kill
  // switch that happens to be off today.
  //
  // The two backdrops are checked against the theme each one owns. The rain is
  // `RainBackdrop`'s, not `WeatherBackdrop`'s — that split is the pre-existing
  // special case, and asking `WeatherBackdrop` for a rain canvas would pass for
  // the wrong reason if it ever gained one.
  localStorage.setItem("codify.theme", CMATRIX_OLED.id);
  assert.match(
    renderToStaticMarkup(React.createElement(RainBackdrop)),
    /<canvas/,
    "the rain theme lost its canvas — motionless must be per-theme, not global",
  );
  localStorage.setItem("codify.theme", CYBERPUNK_NEON.id);
  assert.match(weather(), /<canvas/, "the grid theme lost its canvas");
  localStorage.setItem("codify.theme", STILL.id);
});

test("still's palette is contrast-first, and near-monochrome on purpose", () => {
  // With nothing moving, what is left to look at is the text — so the ramp *is*
  // the theme, and it has to be the highest-contrast one in the list rather than
  // merely a dark one. This is the same WCAG arithmetic
  // `transcriptPalette.test.ts` runs, applied to the theme's own contract
  // instead of to the chat bubble.
  const lum = (hex: string): number => {
    const channels = hex
      .replace("#", "")
      .match(/../g)!
      .map((pair) => Number.parseInt(pair, 16) / 255)
      .map((s) => (s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4));
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  };
  const contrast = (a: string, b: string): number => {
    const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };

  const onRaised = contrast(
    STILL.tokens["--codify-primary"] as string,
    STILL.tokens["--codify-raised"] as string,
  );
  assert.ok(
    onRaised >= 12,
    `still's primary on raised is ${onRaised.toFixed(2)}:1; it is the contrast-first theme and 12:1 is what it is claiming`,
  );

  // Near-monochrome: a theme whose whole job is to stop competing for attention
  // must not introduce a hue that competes. Warm neutrals are not perfectly
  // equal-channel, so a small spread is allowed — what is forbidden is a
  // *saturated* colour, which is what would pull the eye in this palette.
  for (const [name, value] of Object.entries(STILL.tokens)) {
    const [r, g, b] = (value as string)
      .replace("#", "")
      .match(/../g)!
      .map((pair) => Number.parseInt(pair, 16));
    assert.ok(
      Math.max(r, g, b) - Math.min(r, g, b) <= 12,
      `still's ${name} is ${value} (r${r} g${g} b${b}) — a hue here is a bug in a near-monochrome palette`,
    );
  }
});
