/**
 * The wordmark: thin, and a reader of the theme rather than a branch on it.
 *
 * `Wordmark.tsx` fills the centre of an empty window, and its docstring says two
 * things about itself that nothing was checking. It says it is **thin by
 * design**, and the stylesheet said `font-weight: 800` — so the component and its
 * own documentation disagreed for as long as both existed, and the docstring was
 * the one nobody opens. It also says it *matches a theme by reading it, not by
 * branching on it*, and that claim was true and untested: the gradient is written
 * in `index.css` as `rgb(var(--codify-*-rgb) / …)`, so it does follow every
 * theme, but a hex literal dropped into one gradient stop would have rendered a
 * word in the wrong colour in one theme and in all the rest, silently, because
 * nothing compares the two.
 *
 * The weight is the sharper of the two failures. It is one number, it is in the
 * wrong direction, and "thinner" is not a preference a future edit is entitled
 * to reverse without something to argue with — hence the bound below rather than
 * an equality, so the weight can be tuned but not silently thickened back into
 * the slab the docstring denies.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { THEMES } from "../src/appearance.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CSS = readFileSync(path.join(HERE, "..", "src", "index.css"), "utf8");

/** Every declaration of every rule whose selector names `cls`, concatenated.
 *
 * Collecting *all* of them rather than the first is the point: the wordmark's
 * two layers share one grouped rule for `background-clip`/`color` and each has
 * its own for the gradient, so a first-match lookup returns the group — which
 * contains no colour at all, and makes a hard-coded hex in the real gradient
 * invisible to the check below. The negative lookahead is what stops
 * `.codify-wordmark` from matching `.codify-wordmark-fill`.
 */
function rule(cls: string): string {
  const name = new RegExp(`\\.${cls}(?![\\w-])`);
  const found: string[] = [];
  for (const chunk of CSS.split("}")) {
    const brace = chunk.lastIndexOf("{");
    if (brace < 0) continue;
    if (name.test(chunk.slice(0, brace))) found.push(chunk.slice(brace + 1));
  }
  assert.ok(found.length > 0, `index.css has no rule whose selector names \`.${cls}\``);
  return found.join("\n");
}

/**
 * A rule with one selector satisfying `selector`, and a body matching `body`.
 *
 * The selector list is split on commas first. A grouped rule can name
 * `[data-starved="true"] .cyber-veil` and `.codify-wordmark-sweep` in the same
 * list, and matching the whole list would report that the starved rule applies
 * to the wordmark when only its sibling selector does.
 */
function hasRule(selector: (s: string) => boolean, body: RegExp): boolean {
  for (const chunk of CSS.split("}")) {
    const brace = chunk.lastIndexOf("{");
    if (brace < 0) continue;
    const selectors = chunk.slice(0, brace).split(",").map((s) => s.trim());
    if (selectors.some(selector) && body.test(chunk.slice(brace + 1))) return true;
  }
  return false;
}

const sweepStopped = (attribute: string): boolean =>
  hasRule(
    (s) => s.includes(attribute) && s.includes(".codify-wordmark-sweep"),
    /animation:\s*none/,
  );

test("a starved window stops the sweep even when its person kept the backdrop", () => {
  // Measured on a software-composited window: the band is a transform under a
  // glyph mask now (no glyph repaint), but this compositor still charges a
  // full-window blend for any animated layer — the band alone pinned the shell
  // at 100%, against the old paint version's ~45%. The verdict is the only
  // party that can tell; it stops the band and the theme canvas keeps the
  // lower rate the person chose.
  assert.ok(
    sweepStopped('[data-starved="true"]'),
    "a measured-starved boot still sweeps the wordmark",
  );
  assert.ok(
    hasRule(
      (s) => s.includes('[data-starved="true"]') && s.includes(".codify-wordmark-sweep"),
      /transform:\s*translate3d\(100%, 0, 0\)/,
    ),
    "the sweep was stopped without the off-glyph frame the reduced-motion block uses",
  );
});

test("the first sweep waits for the shell's verdict", () => {
  // The watchdog arms after 5s and needs ten one-second samples. A sweep that
  // starts before that is a sweep the verdict can only discover after paying
  // for it; ten seconds of stillness is the empty-state hero loading, and a
  // window that is healthy gets its shimmer after it.
  const sweep = rule("codify-wordmark-sweep");
  assert.match(
    sweep,
    /animation:\s*codify-wordmark-sweep\s+9s\s+linear\s+1\b/,
    "the sweep does not rest after one pass",
  );
  assert.match(
    sweep,
    /animation-delay:\s*10s/,
    "the sweep can start before the shell has had its ten samples",
  );
});

test("the band moves by transform, not by a background-position repaint", () => {
  // The failure this holds: `background-position` is a paint property, so
  // animating it on text-clipped glyphs made the browser rasterize the word at
  // the display's rate — ~45% of a core in the shell on a software-composited
  // window, for a highlight. A transform is composited, and the glyphs are
  // painted once into the mask wrapper by `Wordmark.tsx`.
  // Comments are stripped first: the rule's own comment says why
  // `background-position` is wrong, and a declaration check must not read the
  // explanation as the declaration.
  const withoutComments = (css: string): string => css.replace(/\/\*[\s\S]*?\*\//g, "");
  const sweep = withoutComments(rule("codify-wordmark-sweep"));
  assert.doesNotMatch(sweep, /background-position/, "the sweep repaints its glyphs again");
  assert.match(sweep, /will-change:\s*transform/, "the band is not marked as a composited layer");

  const at = CSS.indexOf("@keyframes codify-wordmark-sweep");
  assert.ok(at >= 0, "the sweep's keyframes moved or were renamed");
  const open = CSS.indexOf("{", at);
  let depth = 0;
  let end = open;
  for (; end < CSS.length; end += 1) {
    if (CSS[end] === "{") depth += 1;
    else if (CSS[end] === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  const frames = withoutComments(CSS.slice(open + 1, end));
  assert.match(frames, /transform:\s*translate3d/, "the keyframes do not move a transform");
  assert.doesNotMatch(frames, /background-position/, "the keyframes repaint instead of compositing");
});

test("the app's own reduced setting reaches the sweep", () => {
  assert.ok(
    sweepStopped('[data-motion="reduced"]'),
    "choosing reduced motion in the app left the wordmark sweeping",
  );
});

test("the wordmark is thin, as its own docstring claims", () => {
  const match = /font-weight:\s*(\d+)/.exec(rule("codify-wordmark"));
  assert.ok(match, ".codify-wordmark declares no font-weight at all");
  const weight = Number(match[1]);
  assert.ok(
    weight <= 300,
    `.codify-wordmark is font-weight ${weight}. Wordmark.tsx documents itself as ` +
      "\"thin by design\", and it fills the middle of an empty window over whatever " +
      "weather the active theme is drawing — a heavy logotype there reads as a slab " +
      "laid over the rain. Raise the bound here first if the weight has to go up.",
  );
});

test("every colour the wordmark paints is a variable the active theme publishes", () => {
  const layers = ["codify-wordmark-fill", "codify-wordmark-sweep"];
  for (const layer of layers) {
    const body = rule(layer);
    // Non-vacuity: a rule that stopped painting anything would satisfy "no
    // hard-coded colour" perfectly and draw nothing.
    assert.match(
      body,
      /var\(--codify-[a-z-]+-rgb\)/,
      `${layer} paints no theme variable, so it is either empty or hard-coded`,
    );
    const hex = body.match(/#[0-9a-f]{3,8}\b/gi);
    assert.equal(
      hex,
      null,
      `${layer} hard-codes ${hex?.join(", ")}. The wordmark follows the theme by ` +
        "reading it, so a literal here is one theme's colour in all sixteen.",
    );
    // Any `rgb(`/`rgba(` that is not the `rgb(var(--codify-…-rgb) / a)` form is a
    // colour the theme cannot reach. `transparent` is a keyword rather than a
    // function, and is the sweep's resting state rather than a colour, so the
    // sweep's `transparent` stops are left alone.
    const literals = body.replace(/rgb\(var\(--codify-[a-z-]+-rgb\)/g, "");
    const stray = literals.match(/\brgba?\(/g);
    assert.equal(
      stray,
      null,
      `${layer} has a colour outside the rgb(var(--codify-*-rgb) / a) form. The ` +
        "triplet is how a theme publishes a colour it can be given an alpha for.",
    );
  }
});

test("the wordmark never branches on which theme is active", () => {
  for (const source of [
    readFileSync(path.join(HERE, "..", "src", "components", "ui", "Wordmark.tsx"), "utf8"),
    rule("codify-wordmark-fill"),
    rule("codify-wordmark-sweep"),
  ]) {
    const themeIds = THEMES.map((theme) => theme.id);
    for (const id of themeIds) {
      assert.ok(
        !source.includes(`"${id}"`) && !source.includes(`'${id}'`),
        `the wordmark mentions the theme id \`${id}\`. A branch here is the thing ` +
          "reading the theme is supposed to replace: the seventeenth theme would " +
          "need a seventeenth rule here.",
      );
    }
  }
});

test("every theme can actually resolve the wordmark's two variables", () => {
  // The CSS reads `--codify-primary-rgb` and `--codify-accent-rgb`. `applyTheme`
  // derives the `-rgb` triplet from a hex token, so a theme missing either source
  // token would leave the gradient with an unresolvable var and the whole
  // `background-image` invalid — which, with `color: transparent` on the same
  // element, is a wordmark that renders as nothing at all.
  // `as const` so the loop variable is the union of the two names rather than
  // `string`. Without it this is TS7053, and `node --test` does not catch it:
  // `--experimental-strip-types` erases types rather than checking them, so the
  // suite goes green on a file `make typecheck-ui-tests` rejects.
  const REQUIRED = ["--codify-primary", "--codify-accent"] as const;
  for (const theme of THEMES) {
    for (const name of REQUIRED) {
      const value = theme.tokens[name];
      assert.match(
        value ?? "",
        /^#[0-9a-f]{6}$/i,
        `theme \`${theme.id}\` publishes ${name} as ${JSON.stringify(value)}, so the ` +
          "wordmark's gradient cannot resolve and the mark renders invisible in it",
      );
    }
  }
});
