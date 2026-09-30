/**
 * The atmosphere canvas fades out at its edges instead of stopping at them.
 *
 * The claim this pins is a README one: that every effect "fades out before it
 * reaches the content surfaces". Each painter draws its own internal fades —
 * that is what makes an effect look like an effect — but none of them can fade
 * the *element*. A canvas that runs under the opaque header and stops dead
 * against it has a hard cut there no matter how soft its last frame was, and
 * that is the line the README described and the code did not have.
 *
 * The shape of the test matters as much as the assertion. The fade is one class
 * applied through the `className` every painter already threads, so the failure
 * mode is a *new* painter that renders its own canvas without threading it — a
 * file that looks exactly like the other twenty. So this checks the whole set,
 * not the two mount sites, and a painter that stops threading the prop fails
 * here rather than shipping a hard cut that nothing else would notice.
 *
 * One radial image, not two linear ones with `mask-composite: intersect`: the
 * default composite is `add`, so an engine that missed the intersect would
 * union the two masks and hide the canvas instead of fading it. The degradation
 * when `mask-image` is unsupported has to be "the old look" — never an
 * invisible backdrop — which is why the assertion is about the *fade* being
 * present and reaching transparent, not about a specific engine's syntax.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

const UI_DIR = fileURLToPath(new URL("../src/components/ui/", import.meta.url));
const CSS_SRC = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const WEATHER_SRC = readFileSync(
  new URL("../src/components/ui/WeatherBackdrop.tsx", import.meta.url),
  "utf8",
);
const RAIN_SRC = readFileSync(
  new URL("../src/components/ui/RainBackdrop.tsx", import.meta.url),
  "utf8",
);

test("the fade exists once, as a class, with both the prefixed and standard property", () => {
  // The prefixed one is not redundancy: Tauri renders through the platform
  // webview, which on Linux is WebKitGTK, and a WebKit older than the one in
  // Safari 15.4 only took the prefixed property — and the desktop app supports
  // distributions older than that.
  const block = CSS_SRC.slice(
    CSS_SRC.indexOf(".codify-atmosphere-canvas"),
    CSS_SRC.indexOf("}", CSS_SRC.indexOf(".codify-atmosphere-canvas")),
  );
  assert.match(block, /-webkit-mask-image/, "an older WebKitGTK would get no fade at all");
  assert.match(block, /(?<!-)mask-image/, "everything that is not WebKit gets no fade");
});

test("the mask fades to transparent, rather than to another colour", () => {
  // A mask that ends on an opaque colour masks nothing. The whole effect of
  // this class is the alpha reaching zero, so "transparent" has to be in it.
  const block = CSS_SRC.slice(
    CSS_SRC.indexOf(".codify-atmosphere-canvas"),
    CSS_SRC.indexOf("}", CSS_SRC.indexOf(".codify-atmosphere-canvas")),
  );
  assert.match(block, /transparent/, "the far edge must reach zero alpha");
  assert.match(
    block,
    /radial-gradient/,
    "one image rather than two composed linear gradients — see the file header",
  );
  assert.doesNotMatch(
    block,
    /mask-composite/,
    "an intersect the default does not honour would union the masks and hide the canvas",
  );
});

test("both backdrop mounts put the class on their canvas", () => {
  // Two mounts, not one: `WeatherBackdrop`'s table covers most themes, but the
  // rain predates that table and is mounted separately, so it is exactly the
  // canvas a single-site fix would have missed.
  for (const [name, src] of [
    ["WeatherBackdrop", WEATHER_SRC],
    ["RainBackdrop", RAIN_SRC],
  ] as const) {
    assert.match(
      src,
      /className="codify-atmosphere-canvas absolute inset-0 h-full w-full"/,
      `${name} mounts its canvas without the fade`,
    );
  }
});

test("every painter threads className, so the class reaches all of them", () => {
  // The general claim behind the two above. A painter that renders its own
  // <canvas> without the prop compiles, renders, animates and looks fine on
  // its own — and is the one canvas in the app with a hard edge. Checking the
  // files is the only way to notice, because nothing about a missing fade
  // throws.
  const painters = readdirSync(UI_DIR).filter((f) => f.endsWith(".tsx"));
  const withCanvas: string[] = [];
  const missing: string[] = [];
  for (const file of painters) {
    const src = readFileSync(UI_DIR + file, "utf8");
    // Only the painters matter: the rest of the directory is buttons and
    // panels, and a `<canvas>` is the only thing that can take a mask.
    if (!src.includes("<canvas")) continue;
    withCanvas.push(file);
    if (!/className=\{`pointer-events-none \$\{className\}`\}/.test(src)) {
      missing.push(file);
    }
  }
  assert.ok(withCanvas.length > 10, `expected the painter set, found ${withCanvas.length}`);
  assert.deepEqual(
    missing,
    [],
    "these render a canvas but do not thread className, so they cannot receive the fade",
  );
});
