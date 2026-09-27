/**
 * The rain's two failures, as tests.
 *
 * Both were one decision — where `RainBackdrop` is mounted — and neither was
 * visible to a test that only looked at the component in isolation: rendered on
 * its own, the backdrop is a canvas in a full-size box, exactly as it should be.
 * What was wrong was its *position in the tree*, so these tests read the committed
 * source for the position and render the component for the parts that are its own
 * (the theme gate, the full-bleed box, the vignette). The same source-reading
 * approach `invokeArgs.test.ts` uses for the IPC payloads, for the same reason:
 * the failure is invisible from the outside.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import React from "react";

// Must run before the component is reached: `--experimental-strip-types` erases
// types rather than compiling JSX, so a `.tsx` import without this hook is
// `ERR_UNKNOWN_FILE_EXTENSION`. It also installs the `localStorage` stub the
// theme gate below reads.
import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { CODIFY_DARK, CMATRIX_OLED } from "../src/appearance.ts";

// Dynamic, like `appearance.test.ts`: a static import is resolved — and fails —
// before `registerTsx()` above has run.
const { RainBackdrop } = await import("../src/components/ui/RainBackdrop.tsx");
const { renderToStaticMarkup } = await import("react-dom/server");

const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const timeline = readFileSync(
  new URL("../src/components/ChatTimeline.tsx", import.meta.url),
  "utf8",
);
const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const backdrop = readFileSync(
  new URL("../src/components/ui/RainBackdrop.tsx", import.meta.url),
  "utf8",
);

const markup = (): string => renderToStaticMarkup(React.createElement(RainBackdrop));

test("the backdrop spans the whole window, not a centred column", () => {
  localStorage.setItem("codify.theme", CMATRIX_OLED.id);
  const out = markup();
  // `absolute inset-0` inside the app's own `relative` root: the box is the
  // window. A `max-w-*` or `mx-auto` here is the centred-animation bug back.
  assert.match(out, /absolute inset-0/);
  assert.doesNotMatch(out, /max-w-/);
  assert.doesNotMatch(out, /mx-auto/);
});

test("the canvas fills that box", () => {
  localStorage.setItem("codify.theme", CMATRIX_OLED.id);
  assert.match(markup(), /h-full w-full/);
});

test("a theme with no rain variable still gets nothing at all", () => {
  localStorage.setItem("codify.theme", CODIFY_DARK.id);
  assert.equal(markup(), "", "not even an empty div — no canvas, no vignette");
});

test("the shell mounts it once, and the transcript does not mount it at all", () => {
  // The teardown was structural: the backdrop lived in the branch the transcript
  // renders only while `messages.length === 0`, so the first message removed it.
  // No amount of CSS would have fixed that; only the mount point would.
  assert.doesNotMatch(
    timeline,
    /RainBackdrop/,
    "the transcript mounts the rain again — it will be gone the moment a message exists",
  );
  assert.match(
    app,
    /import \{ RainBackdrop \}/,
    "App.tsx no longer imports the backdrop, so nothing mounts it",
  );
  assert.match(
    app,
    /<RainBackdrop\b[^>]*\/>/,
    "App.tsx no longer renders the backdrop anywhere",
  );
});

test("the rain sits under the content, not over it", () => {
  // An absolutely positioned box with no z-index paints *after* in-flow content,
  // so a `z-0` backdrop would cover the header's text. Both the header and the
  // main row have to be positioned and lifted, or the rain is an overlay.
  assert.match(
    backdrop,
    /z-0/,
    "the backdrop no longer declares a stacking position, so it paints over the header",
  );
  const header = app.slice(app.indexOf("<header"));
  assert.match(
    header.slice(0, 400),
    /z-10/,
    "the header is no longer lifted above the rain",
  );
  const main = app.slice(app.indexOf("<main"));
  assert.match(
    main.slice(0, 200),
    /z-10/,
    "the main row is no longer lifted above the rain, so glyphs land on the transcript",
  );
});

test("the chrome is glass, or the rain stops at the middle of the window", () => {
  // The sidebar and the top bar are the two full-bleed surfaces the rain is
  // behind. Opaque, they hide it completely and the user sees a centred
  // animation with clean edges — which is exactly the reported symptom.
  assert.match(
    css,
    /\.bg-codify-chrome \{[^}]*var\(--codify-surface-rgb\)\s*\/\s*0?\.\d/,
    "the chrome utility must compose from the triplet with an alpha below 1",
  );
  assert.match(
    app,
    /bg-codify-chrome/,
    "the top bar is still opaque, so the rain stops below it",
  );
  const sidebar = readFileSync(
    new URL("../src/components/Sidebar.tsx", import.meta.url),
    "utf8",
  );
  assert.match(
    sidebar,
    /bg-codify-chrome/,
    "the sidebar is still opaque, so the rain never reaches the left edge",
  );
  // And the content surfaces stay solid: reading happens over the transcript.
  assert.match(
    css,
    /\.bg-codify-surface \{[^}]*\/ 1\)/,
    "the content surfaces went translucent — reading over glass is not reading",
  );
});

test("the vignette no longer dims the two sides the rain is for", () => {
  localStorage.setItem("codify.theme", CMATRIX_OLED.id);
  const out = markup();
  const alpha = Number(/rgba\(0,\s*0,\s*0,\s*([\d.]+)\)/.exec(out)?.[1]);
  assert.ok(Number.isFinite(alpha), "the vignette no longer names its own alpha");
  assert.ok(
    alpha <= 0.4,
    `the vignette is ${alpha} — strong enough to hide the rain it dims (was 0.55)`,
  );
});
