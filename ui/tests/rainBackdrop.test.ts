/**
 * The rain's own parts, as tests.
 *
 * The rain's two original failures were one decision — where `RainBackdrop` is mounted — and neither
 * was visible to a test that only looked at the component in isolation: rendered on its own, the
 * backdrop is a canvas in a full-size box, exactly as it should be. What was wrong was its *position
 * in the tree*, and that is now held against the mounted App in `backdropShell.test.ts` (one layer,
 * under the chrome, outside the transcript, the same element after the first message, told when the
 * agent is working). What is left here are the parts that are the component's own — the theme gate,
 * the full-bleed box, the vignette — rendered, and the two rules that are written in the stylesheet
 * and nowhere else, which are read from it because jsdom applies no Tailwind and computes no cascade.
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

const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
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

test("the chrome utility is glass and the content surface is solid", () => {
  // The sidebar and the top bar are the two full-bleed surfaces the rain is behind; that they carry
  // this utility is checked on the mounted App (`backdropShell.test.ts`). What the utility *is* is
  // written here and only here: opaque, it hides the rain completely and the user sees a centred
  // animation with clean edges, which is exactly the reported symptom.
  assert.match(
    css,
    /\.bg-codify-chrome \{[^}]*var\(--codify-surface-rgb\)\s*\/\s*0?\.\d/,
    "the chrome utility must compose from the triplet with an alpha below 1",
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
