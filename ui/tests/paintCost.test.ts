/**
 * What the window may make the machine paint while nobody is doing anything.
 *
 * Measured on 2026-10-30, in software-composited Chromium (the path a GPU-less
 * WebKitGTK takes, and the one the shell's render watchdog exists for), against
 * the production build and a real engine:
 *
 *  - **A blurred backdrop behind a dialog.** Settings, the command palette and
 *    three more dialogs sat on a full-window `backdrop-blur`. Every frame under
 *    it, whether the dialog was scrolled or something behind it animated,
 *    re-blurred the whole window: Settings scrolled at 10–15 FPS in every one of
 *    the nineteen themes, and at 60 with the blur gone. Under 75–80% black the
 *    blur was barely visible.
 *  - **A loop on a paint property.** The wordmark's sweep animated
 *    `background-position` on text clipped to its background, forever: the
 *    whole browser spent 43% of a core with the app idle on the default theme,
 *    and 4% without it.
 *  - **A continuous composite.** The Cyberpunk veil rolled 3px over nine seconds
 *    with `linear` timing, so it moved every frame and held that theme at 30 FPS;
 *    in three steps it reads the same and runs at 60.
 *
 * Each is a rule about what is written in the stylesheet and the class strings,
 * because jsdom has no compositor to measure.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, "..", "src");
const CSS = readFileSync(path.join(SRC, "index.css"), "utf8");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return sources(full);
    return /\.(tsx?|css)$/.test(name) ? [full] : [];
  });
}

test("nothing in the window blurs what is behind it", () => {
  const blurring = sources(SRC).filter((file) =>
    /backdrop-blur|backdrop-filter/.test(readFileSync(file, "utf8")),
  );
  assert.deepEqual(
    blurring.map((file) => path.relative(SRC, file)),
    [],
    "a backdrop filter re-blurs everything under it on every frame",
  );
});

test("an animation that never ends moves in steps, not on every frame", () => {
  const declarations = [...CSS.matchAll(/animation:\s*([^;]+);/g)].map((m) => m[1].trim());
  const endless = declarations.filter((d) => /\binfinite\b/.test(d));
  assert.ok(endless.length > 0, "no endless animation found: this test's parse is stale");
  for (const declaration of endless) {
    assert.match(
      declaration,
      /\bsteps\(/,
      `"${declaration}" runs forever and changes every frame`,
    );
  }
});

test("the wordmark sweeps a counted number of times and then rests", () => {
  const rule = CSS.match(/\.codify-wordmark-sweep\s*\{[^}]*animation:\s*([^;]+);/);
  assert.ok(rule, "the sweep's animation declaration moved");
  assert.match(rule[1], /^codify-wordmark-sweep\s+\S+\s+\S+\s+\d+$/, rule[1]);
  assert.doesNotMatch(rule[1], /infinite/);
});
