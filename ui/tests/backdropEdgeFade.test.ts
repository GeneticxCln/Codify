/**
 * The atmosphere canvas fades out at its edges instead of stopping at them.
 *
 * The claim this pins is a README one: every effect "fades out before it
 * reaches the content surfaces". Each painter draws its own internal fades —
 * that is what makes an effect look like an effect — but none of them can fade
 * the *element*. A canvas that runs under the opaque header and stops dead
 * against it has a hard cut there no matter how soft its last frame was.
 *
 * **Why this is a canvas operation and not a CSS mask.** A `mask-image` on a
 * full-window canvas made WebKitGTK's software compositor rebuild the window on
 * every frame, even when the canvas had not changed: measured on the machine
 * that froze, a still 1024-pixel rain canvas under the old mask held the web
 * process at ~57% of a core, and the same canvas animating held ~100%. Without
 * the mask the still canvas is free. The fade is now painted into the canvas by
 * `canvasEdgeFade.ts` after each painter's `draw`, so both the shape and the
 * performance decision are testable without a browser.
 *
 * The shape is the old rule's shape: `radial-gradient(ellipse farthest-corner
 * at 50% 50%, #000 55%, transparent 100%)`. An ellipse with radii `w/√2` and
 * `h/√2` passes through the farthest corner; a circle of radius `w/√2` under
 * `scale(1, h/w)` is that ellipse. `destination-out` erases where the gradient
 * is opaque, which is why the stops run transparent → opaque rather than the
 * other way round.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { applyEdgeFade } from "../src/canvasEdgeFade.ts";

const UI_DIR = fileURLToPath(new URL("../src/components/ui/", import.meta.url));
const CSS_SRC = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const HOOK_SRC = readFileSync(
  new URL("../src/hooks/useAtmosphereCanvas.ts", import.meta.url),
  "utf8",
);
const RAIN_SRC = readFileSync(
  new URL("../src/components/ui/MatrixRain.tsx", import.meta.url),
  "utf8",
);

/** A recording 2D context: enough of the surface for `applyEdgeFade`. */
function recordingContext(): {
  calls: Array<{ op: string; args: unknown[] }>;
  stops: Array<[number, string]>;
  gradientArgs: unknown[];
  ctx: CanvasRenderingContext2D;
} {
  const calls: Array<{ op: string; args: unknown[] }> = [];
  const stops: Array<[number, string]> = [];
  const gradientArgs: unknown[] = [];
  const gradient = {
    addColorStop: (offset: number, color: string) => void stops.push([offset, color]),
  };
  const ctx = {
    save: () => void calls.push({ op: "save", args: [] }),
    restore: () => void calls.push({ op: "restore", args: [] }),
    translate: (...args: unknown[]) => void calls.push({ op: "translate", args }),
    scale: (...args: unknown[]) => void calls.push({ op: "scale", args }),
    createRadialGradient: (...args: unknown[]) => {
      gradientArgs.push(...args);
      return gradient;
    },
    beginPath: () => void calls.push({ op: "beginPath", args: [] }),
    rect: (...args: unknown[]) => void calls.push({ op: "rect", args }),
    fill: () => void calls.push({ op: "fill", args: [] }),
    set globalCompositeOperation(value: string) {
      calls.push({ op: "composite", args: [value] });
    },
    set fillStyle(value: unknown) {
      calls.push({ op: "fillStyle", args: [value] });
    },
  } as unknown as CanvasRenderingContext2D;
  return { calls, stops, gradientArgs, ctx };
}

test("the fade erases the canvas's edges with the mask's old ellipse", () => {
  const { calls, stops, gradientArgs, ctx } = recordingContext();
  applyEdgeFade(ctx, 1000, 500);

  assert.ok(
    calls.some(({ op, args }) => op === "composite" && args[0] === "destination-out"),
    "the fade must erase the canvas, not paint a colour over it",
  );
  assert.deepEqual(
    calls.find(({ op }) => op === "scale")?.args,
    [1, 0.5],
    "the circle must be scaled by height/width to be the ellipse the mask described",
  );
  // radius = w/√2, inner = 55% of it; center is the canvas center.
  assert.equal(gradientArgs.length, 6);
  assert.equal(gradientArgs[0], 0);
  assert.equal(gradientArgs[1], 0);
  assert.ok(Math.abs(Number(gradientArgs[2]) - (1000 / Math.SQRT2) * 0.55) < 1e-9);
  assert.equal(gradientArgs[3], 0);
  assert.equal(gradientArgs[4], 0);
  assert.ok(Math.abs(Number(gradientArgs[5]) - 1000 / Math.SQRT2) < 1e-9);
  assert.deepEqual(stops, [
    [0, "rgba(0, 0, 0, 0)"],
    [1, "rgba(0, 0, 0, 1)"],
  ]);
  const rect = calls.find(({ op }) => op === "rect");
  assert.ok(rect, "the fade never defined its cover rectangle");
  const [x, y, w, h] = rect!.args as number[];
  assert.equal(w, 1000);
  assert.equal(h, 1000, "the scaled fill covers the canvas's full height");
  assert.equal(x, -500);
  assert.equal(y, -500);
  assert.ok(
    calls.some(({ op }) => op === "fill"),
    "the fade was never filled",
  );
  assert.ok(
    calls.some(({ op }) => op === "save") && calls.some(({ op }) => op === "restore"),
    "the fade must not leave its transform or composite op on the painter's context",
  );
});

test("both loops fade the frame they just drew", () => {
  assert.match(
    HOOK_SRC,
    /const paint = \(\): void => \{\s*painter\.draw\(ctx, size, tick\);\s*applyEdgeFade\(ctx, size\.width, size\.height\);/,
    "the shared hook has a paint path that does not fade",
  );
  assert.match(
    HOOK_SRC,
    /guard\.run\(\(\) => \{\s*painter\.step\?\.\([^)]*\);\s*paint\(\);/,
    "the shared loop draws without going through the fading paint path",
  );
  assert.match(
    RAIN_SRC,
    /applyEdgeFade\(ctx, canvas\.width, canvas\.height\);/,
    "the rain loop draws without the edge fade",
  );
});

test("the fade is not a CSS mask on the canvas any more", () => {
  // The performance bug this test guards is a regression to the mask: it looks
  // identical and costs a core. The class is gone, so its return is a diff.
  assert.doesNotMatch(CSS_SRC, /\.codify-atmosphere-canvas/);
  assert.doesNotMatch(CSS_SRC, /mask-image:\s*radial-gradient\(\s*ellipse farthest-corner/);
});

test("every painter threads className, so the mounts' positioning reaches all of them", () => {
  // The general claim behind the two mounts above. A painter that renders its
  // own <canvas> without the prop compiles, renders, animates and looks fine on
  // its own — and is the one canvas in the app with the wrong box. Checking the
  // files is the only way to notice, because nothing about a missing class
  // throws.
  const painters = readdirSync(UI_DIR).filter((f) => f.endsWith(".tsx"));
  const withCanvas: string[] = [];
  const missing: string[] = [];
  for (const file of painters) {
    const src = readFileSync(UI_DIR + file, "utf8");
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
    "these render a canvas but do not thread className, so the mounts cannot position them",
  );
});
