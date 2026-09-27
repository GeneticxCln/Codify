/**
 * Every atmosphere, and the three ways a themed backdrop goes wrong.
 *
 * The rain's suite reads `RainBackdrop.tsx` *and* `App.tsx` as source, because
 * the failure it was written for — a backdrop mounted inside the transcript —
 * is invisible to any test that renders the component alone: on its own, the
 * box is correct. With six animated themes the same three failures are each
 * available more than once over, so they are pinned once and made general:
 *
 * 1. **A theme that declares weather but gets none.** The gate is a CSS custom
 *    property, not a truthiness test on an id, and a variable the module does
 *    not manage survives a switch back to dark. This is now a *pairing* claim
 *    between `appearance.ts` and the backdrop's table, so it is checked for
 *    every theme rather than for one.
 * 2. **A loop with no opt-out.** The canvas honours
 *    `prefers-reduced-motion` and the veil's CSS animation honours it too; a
 *    class that animates with no escape hatch is what DESIGN.md §7 bans, and
 *    it is invisible in markup.
 * 3. **A component and its theme drifting apart.** Every painter reads its
 *    colours from the theme's variables, the hexes are quoted in DESIGN.md, and
 *    the clock's bounds live in one hook rather than in six files.
 *
 * Plus the one that makes the last theme free: `ascii-rain` has no canvas of
 * its own, and the test that says so is what stops a later theme from quietly
 * forking the rain.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import React from "react";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import {
  ASCII_RAIN,
  ATMOSPHERE_TRIGGERS,
  CMATRIX_OLED,
  CODIFY_DARK,
  CYBERPUNK_NEON,
  MANAGED_VARS,
  MOTIONLESS_THEME_IDS,
  THEMES,
  hexChannels,
  themeById,
} from "../src/appearance.ts";

const { WeatherBackdrop, themesWithWeather, triggerFor, effectFor } = await import(
  "../src/components/ui/WeatherBackdrop.tsx"
);
const { RainBackdrop } = await import("../src/components/ui/RainBackdrop.tsx");
const { AppearancePane } = await import("../src/components/AppearancePane.tsx");
const { renderToStaticMarkup } = await import("react-dom/server");

const UI_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "src", "components", "ui");
const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const clock = readFileSync(new URL("../src/hooks/useAtmosphereCanvas.ts", import.meta.url), "utf8");
const rain = readFileSync(new URL("../src/components/ui/MatrixRain.tsx", import.meta.url), "utf8");
const timeline = readFileSync(new URL("../src/components/ChatTimeline.tsx", import.meta.url), "utf8");
const design = readFileSync(new URL("../../DESIGN.md", import.meta.url), "utf8");

/** Every effect variable a theme publishes, from the triggers list. */
const EFFECT_VARS = ATMOSPHERE_TRIGGERS.map((t) => t.trigger);

const weather = (): string => renderToStaticMarkup(React.createElement(WeatherBackdrop));
/** The rain's own backdrop, under whatever theme is currently persisted. */
const rainFor = (): string => renderToStaticMarkup(React.createElement(RainBackdrop));

test("every animated theme publishes a managed variable, triplets included", () => {
  for (const theme of THEMES) {
    const published = Object.keys(theme.tokens);
    const trigger = triggerFor(theme) ?? (theme.tokens["--cmatrix-rain"] ? "--cmatrix-rain" : undefined);
    if (trigger === undefined) {
      // Which themes are weatherless is a decision, so it is *declared* — in
      // `MOTIONLESS_THEME_IDS`, beside the themes it names — rather than left
      // as an inline "it must be the default". That inline check read as a fact
      // about `CODIFY_DARK` instead of a statement about motion, and the second
      // deliberately motionless theme (`still`) is what exposed it.
      //
      // The list is the point: a theme that has an effect and no trigger, and
      // has not also declared that it wants none, is a theme whose canvas
      // silently never mounts.
      assert.ok(
        MOTIONLESS_THEME_IDS.includes(theme.id),
        `${theme.id} has no atmosphere trigger and is not on MOTIONLESS_THEME_IDS, so its canvas would never mount`,
      );
      continue;
    }
    assert.ok(published.includes(trigger), `${theme.id} does not publish ${trigger}`);
  }
  for (const theme of THEMES) {
    for (const name of Object.keys(theme.tokens)) {
      assert.ok(MANAGED_VARS.includes(name as never), `${theme.id} sets un-managed ${name}`);
      // A hex gets a derived triplet, and the triplet is managed too — that is
      // what `applyTheme` clears on the way to the next theme.
      if (/^#[0-9a-fA-F]{6}$/.test(theme.tokens[name as never] as string)) {
        assert.ok(
          MANAGED_VARS.includes(`${name}-rgb` as never),
          `${theme.id}: ${name} is a hex but ${name}-rgb is not managed`,
        );
      }
    }
  }
  // The one non-colour name, and the reason the rule above is a conditional:
  // `ascii-rain` publishes a glyph set, which is set verbatim and never parsed
  // as a colour, so it has no triplet to manage.
  assert.ok(MANAGED_VARS.includes("--cmatrix-glyphs" as never));
  assert.equal(CODIFY_DARK.tokens["--cmatrix-glyphs"], undefined);
});

test("the trigger list and the backdrop's own table agree", () => {
  // Two files, one pairing, and nothing enforcing them until a theme is added
  // to one and not the other. `appearance.ts` exists so the question is asked in
  // the same breath as the tokens; this is the part that notices the answer was
  // wrong.
  const table = themesWithWeather();
  for (const { trigger, themes } of ATMOSPHERE_TRIGGERS) {
    for (const id of themes) {
      const theme = themeById(id);
      assert.ok(theme.tokens[trigger as never], `${id} is listed under ${trigger} but does not publish it`);
    }
  }
  // The two rain themes are the deliberate exception: they are weather, drawn by
  // the rain's own backdrop, which predates this table. So the pairing is not
  // "every weather theme is in the table" but "every weather theme is in exactly
  // one of the two", and this is the test that says which mechanism owns what.
  const RAIN_THEMES = [CMATRIX_OLED.id, ASCII_RAIN.id];
  for (const id of RAIN_THEMES) {
    assert.ok(!table.includes(id), `${id} is in the table AND in the rain's backdrop`);
  }
  for (const id of table) {
    assert.ok(!RAIN_THEMES.includes(id), `${id} is claimed by both backdrops`);
  }
  // And nothing may publish a trigger that no mechanism watches: that is the
  // half of the pairing that a new theme gets wrong.
  const watched = new Set(ATMOSPHERE_TRIGGERS.map((t) => t.trigger));
  for (const theme of THEMES) {
    const published = Object.keys(theme.tokens).filter((n) => EFFECT_VARS.includes(n));
    // A declared-motionless theme publishes no effect variable, so there is
    // nothing for this loop to check — the same exemption the old single-id
    // check gave `CODIFY_DARK`, now stated as the decision it actually is.
    if (MOTIONLESS_THEME_IDS.includes(theme.id)) continue;
    assert.ok(
      watched.has(published[0]),
      `${theme.id} publishes ${published.join(", ")}, which no backdrop watches`,
    );
  }
  for (const id of table) {
    assert.ok(
      EFFECT_VARS.some((trigger) => themeById(id).tokens[trigger as never]),
      `${id} is in the backdrop table but publishes no trigger`,
    );
    assert.ok(effectFor(id), `${id} is in the table with no effect to draw`);
  }
});

test("the ASCII rain needs no component of its own", () => {
  // The claim the module makes about itself, cashed: the monochrome theme is
  // the OLED theme with different variables, and the same backdrop draws it
  // because the gate is a variable rather than an id. A theme that had needed a
  // forked `MatrixRain` would show up here as a file that does not exist.
  assert.equal(effectFor(ASCII_RAIN.id), undefined, "the ASCII rain grew its own effect");
  assert.equal(ASCII_RAIN.tokens["--cmatrix-rain"], "#262626");
  // The head glyph is `#404040`, not the theme's own text colour: OLED CMatrix
  // aliases those two because its head glyph *is* the app's body text, and
  // copying the alias here put a white head on every column behind the app's
  // white text. The test says so, because the alias is the tempting default.
  assert.equal(ASCII_RAIN.tokens["--cmatrix-text"], "#404040");
  assert.notEqual(ASCII_RAIN.tokens["--cmatrix-text"], ASCII_RAIN.tokens["--codify-secondary"]);
  assert.equal(ASCII_RAIN.tokens["--codify-secondary"], "#f5f5f5");
  // The glyphs are a token too, which is what makes it ASCII rain rather than
  // CMatrix with a grey filter: hex bytes, and the OLED theme still has kana.
  assert.equal(ASCII_RAIN.tokens["--cmatrix-glyphs"], "0123456789ABCDEF");
  assert.equal(CMATRIX_OLED.tokens["--cmatrix-glyphs"], undefined);
  assert.match(rain, /readVar\("--cmatrix-glyphs", GLYPHS\)/);
  assert.match(rain, /randomGlyph\(glyphs\)/);

  localStorage.setItem("codify.theme", ASCII_RAIN.id);
  const out = rainFor();
  assert.match(out, /<canvas/, "the rain's own backdrop does not mount for it");
  assert.equal(
    weather(),
    "",
    "the weather shell also drew it, so the monochrome theme is running two raindrops",
  );
});

test("each weather theme mounts its own effect, and the others mount nothing", () => {
  for (const id of themesWithWeather()) {
    localStorage.setItem("codify.theme", id);
    const out = weather();
    assert.match(out, /<canvas/, `${id} has weather and drew no canvas`);
    assert.match(out, /absolute inset-0/, `${id}'s backdrop is not full-bleed`);
    assert.doesNotMatch(out, /max-w-/, `${id}'s backdrop is a centred column again`);
    assert.match(out, /aria-hidden="true"/);
    assert.match(out, /pointer-events-none/);
  }
  for (const id of [CODIFY_DARK.id, CMATRIX_OLED.id, ASCII_RAIN.id]) {
    localStorage.setItem("codify.theme", id);
    assert.equal(weather(), "", `${id} gets weather it never asked for`);
  }
});

test("only the cyberpunk theme has a CSS veil, and it has an opt-out", () => {
  localStorage.setItem("codify.theme", CYBERPUNK_NEON.id);
  assert.match(weather(), /class="cyber-veil/, "the veil class is gone from its theme");
  // The guarantee, not the animation: one block that turns it off for a user
  // who asked for no motion. Asserted against the reduced-motion query rather
  // than by counting animations, so adding a second animated class later does
  // not quietly pass.
  const reduced = css.slice(css.indexOf("prefers-reduced-motion: reduce"));
  assert.match(reduced, /\.cyber-veil\s*\{\s*animation: none;\s*\}/);
  // The roll is a transform, not a `background-position`. The first version
  // animated the offset and looked fine; the cost was invisible until a
  // screenshot caught it mid-repaint, because animating a paint property on a
  // full-window element repaints the whole window every frame at the display's
  // rate rather than the theme's 30 FPS budget. A transform is composited, so
  // the same motion costs one layer move.
  assert.match(css, /@keyframes cyber-roll\s*\{\s*0%\s*\{\s*transform: translateY/);
  assert.doesNotMatch(css, /@keyframes cyber-roll\s*\{[^}]*background-position/);
  // And the loop has no seam: the element is one gradient period taller than
  // the window, because it translates by exactly one period.
  assert.match(css, /\.cyber-veil\s*\{[^}]*bottom: -3px/);
  // The veil's colour is the theme's, not a hex typed into CSS: the rule reads
  // the triplet `applyTheme` derives, so a restyle moves it with everything else.
  assert.match(css, /rgb\(var\(--cyber-scan-rgb\)/);
  assert.doesNotMatch(css, /#1b0a35/);
});

test("the bounds live in one hook, and every effect goes through it", () => {
  // One bounded budget rather than six: the cap, the accumulator, the single
  // static frame. Asserted on the hook, then asserted that each painter is a
  // client of it — because the strongest version of "one clock" is not the hook
  // being correct, it being the only way to get a clock at all.
  assert.match(clock, /Math\.min\(1, maxDimension \/ Math\.max\(cssW, cssH\)\)/);
  assert.match(clock, /1000 \/ Math\.max\(1, fps\)/);
  assert.match(clock, /prefers-reduced-motion: reduce/);
  assert.match(clock, /\/\/ The static frame[\s\S]*?painter\.draw\(ctx, size, tick\);/);
  assert.match(clock, /if \(!reduced\) raf = requestAnimationFrame\(loop\);/);
  assert.match(clock, /cancelAnimationFrame/);
  assert.match(clock, /ResizeObserver/);
  // The tick carries the canvas size, because a painter that owns drifting
  // things needs somewhere to bounce them and `window.innerWidth` is the wrong
  // unit the moment a cap is in play.
  assert.match(clock, /size: AtmosphereSize;/);

  for (const file of readdirSync(UI_DIR).filter((f) => f.endsWith(".tsx"))) {
    const source = readFileSync(path.join(UI_DIR, file), "utf8");
    if (!source.includes("useAtmosphereCanvas(")) continue;
    // A painter must not reach for `window.innerWidth` for a coordinate: the
    // canvas is capped, so CSS pixels are the wrong unit and the error is silent
    // on any display where devicePixelRatio is 1.
    assert.doesNotMatch(
      source,
      /innerWidth|innerHeight/,
      `${file} measures the window where it means the canvas`,
    );
    assert.doesNotMatch(
      source,
      /requestAnimationFrame/,
      `${file} runs its own loop instead of the shared clock`,
    );
    // An assignment, not the word: two of the painters *discuss* shadowBlur in
    // their comments while explaining why they do not use it, and a test that
    // cannot tell a mention from a call is a test that gets commented out.
    assert.doesNotMatch(source, /\.shadowBlur\s*=/, `${file} pays for a blur pass`);
  }
});

test("no painter declares a colour the theme already published", () => {
  for (const file of readdirSync(UI_DIR).filter((f) => f.endsWith(".tsx"))) {
    const source = readFileSync(path.join(UI_DIR, file), "utf8");
    if (!source.includes("useAtmosphereCanvas(")) continue;
    // Fallbacks are allowed — they are what a painter uses when the theme has
    // not published yet — but they must be *reads*, one per variable the theme
    // owns, so the theme stays the single place a colour is written down.
    const reads = [...source.matchAll(/read\("(--[\w-]+)"/g)].map((m) => m[1]);
    assert.ok(reads.length > 0, `${file} reads no theme variable at all`);
    for (const name of reads) {
      assert.ok(
        MANAGED_VARS.includes(name as never),
        `${file} reads ${name}, which no theme publishes`,
      );
    }
  }
});

test("the shell mounts once, and the transcript never mounts it", () => {
  // The mount point is the whole design: a backdrop inside the transcript is a
  // centred animation that vanishes on the first message, and the rain's suite
  // exists because that bug shipped once already.
  assert.equal(app.match(/<WeatherBackdrop/g)?.length, 1, "mounted more than once");
  // `\b[^>]*` rather than a literal ` />`: the rain now takes `active`, so it is
  // `<RainBackdrop active={canStop} />`. What this test is about is *where* it
  // is mounted and how many times, and neither changed — pinning the exact
  // closing syntax would have made a prop a false alarm about the mount point.
  assert.equal(app.match(/<RainBackdrop\b[^>]*\/>/g)?.length, 1, "the rain's mount moved");
  assert.ok(
    app.indexOf("<WeatherBackdrop") < app.indexOf("<header"),
    "the backdrop must be under the chrome, not inside it",
  );
  assert.doesNotMatch(timeline, /WeatherBackdrop|RainBackdrop|AbyssSpores|HudSweep/);
  // The one piece of state an atmosphere may report, taken from the goal's own
  // status rather than a second flag — so it cannot disagree with Stop.
  assert.match(app, /<WeatherBackdrop active=\{canStop\} \/>/);
});

test("the settings pane draws all eight themes from data", () => {
  const out = renderToStaticMarkup(React.createElement(AppearancePane));
  for (const theme of THEMES) {
    assert.ok(out.includes(theme.label), `the pane does not list ${theme.label}`);
  }
  // The footer is the theme's own `swatch`, not a chain of conditions: every
  // tile prints a variable that theme publishes, and a theme that published
  // none would print an empty line rather than another theme's hexes.
  for (const theme of THEMES) {
    for (const entry of theme.swatch ?? []) {
      assert.ok(
        out.includes(theme.tokens[entry.v] as string),
        `${theme.label} names ${entry.v}, which it does not publish`,
      );
    }
  }
  // The rain's hexes, and the ASCII rain's, which is how the last theme's
  // glyphs reach the pane.
  assert.match(out, /#003300/);
  assert.match(out, /0123456789ABCDEF/);
  // **Changed from `assert.equal(canvases, 7)`.** The pane used to be a grid of
  // cards that each drew their own theme's live effect, so this count was
  // asserting seven animation loops running at once inside a modal someone
  // opened to read a setting. The pane is a list and a detail now, and exactly
  // one canvas runs: the inspected theme's.
  //
  // The claim is *stronger* than the one it replaces, not weaker. Counting seven
  // canvases only proved that seven effects existed somewhere on the page; it
  // never checked that any particular one belonged to the theme being shown.
  // Driving the stored id and asking each animated theme for its own canvas
  // checks both halves: the effect is reachable, and only one is ever live.
  for (const theme of THEMES) {
    localStorage.setItem("codify.theme", theme.id);
    const canvases = (renderToStaticMarkup(React.createElement(AppearancePane)).match(
      /<canvas/g,
    ) ?? []).length;
    const animated = Boolean(
      ATMOSPHERE_TRIGGERS.some((t) => t.themes.includes(theme.id)) || theme.tokens["--cmatrix-rain"],
    );
    assert.equal(
      canvases,
      animated ? 1 : 0,
      `${theme.id} drew ${canvases} canvas${canvases === 1 ? "" : "es"}; the detail panel shows one effect or none, never a row per theme`,
    );
  }
  localStorage.setItem("codify.theme", CODIFY_DARK.id);
});

test("the themes' hexes are the ones DESIGN.md publishes", () => {
  // The promise in `appearance.ts`: these hexes are each theme's public shape
  // and DESIGN.md's runtime-themes table is where they are read from. A doc that
  // drifts is the same failure as a component that drifts.
  for (const theme of THEMES) {
    for (const entry of theme.swatch ?? []) {
      if (entry.v === "--cmatrix-glyphs") continue; // a glyph set, not a colour
      const value = theme.tokens[entry.v] as string;
      assert.ok(
        design.includes(`| \`${entry.v}\` | \`${value}\` |`) ||
          // A surface is quoted in DESIGN.md's own tables, in prose rather than
          // as a variable list, so accept the hex being present at all — the
          // point is that the doc and the theme are not two different stories.
          design.includes(value),
        `DESIGN.md does not publish ${theme.id}'s ${entry.v} at ${value}`,
      );
    }
  }
  assert.equal(hexChannels("#00e5ff"), "0 229 255");
});
