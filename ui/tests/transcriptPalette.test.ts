/**
 * The transcript's own surfaces, and the one rule they obey.
 *
 * The user's prompt was `bg-blue-600 text-white` beside a blue-tinted avatar: a
 * saturated slab in an app whose whole premise is that a theme supplies the
 * surfaces. It read as a bug rather than a decision, because the blue belonged
 * to no theme and to no meaning — the design system has exactly one interactive
 * hue and five status tones, and the prompt was none of them.
 *
 * So the rule is a negative one, and it is the kind that quietly comes back: the
 * transcript's chrome composes from the active theme's `raised`/`primary`
 * tokens. These tests hold it two ways. The first reads the source, because a
 * hardcoded hex or hue class is invisible from the outside — this is the same
 * trick `rainBackdrop.test.ts` uses for the mount point, and for the same
 * reason. The second does arithmetic on every theme, because "a colour that
 * will not conflict with any of the themes" is only true if the *pair* is
 * checked, not the class name: a fourth theme could ship a `primary` too close
 * to its own `raised` and every class-name assertion would still pass.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { THEMES } from "../src/appearance.ts";

const timeline = readFileSync(
  new URL("../src/components/ChatTimeline.tsx", import.meta.url),
  "utf8",
);

/** The slice of the component that draws one user's message and one reply's. */
const exchange = timeline.slice(
  timeline.indexOf('msg.role === "user" ?'),
  timeline.indexOf("ref={scopeRef}"),
);

// A slice that came out empty would make every `doesNotMatch` here pass for the
// wrong reason, and a rename of either marker is exactly how that happens. So
// the slice is asserted before it is trusted, rather than after.
assert.notEqual(
  exchange.length,
  0,
  "the exchange slice is empty — one of its two markers moved, and these tests would now pass vacuously",
);
assert.match(exchange, /Bot className/, "the slice no longer reaches the assistant's avatar");

/**
 * The slice with its comments removed, which is what the colour rules are about.
 *
 * A comment is allowed to name a hue it is describing as gone — the comment
 * above the user's message quotes the `bg-blue-600` it replaced, and a rule that
 * cannot tell a mention from a class would forbid remembering why. Stripped here
 * rather than avoided in prose, because the alternative is that nobody ever
 * writes down what a class used to be.
 *
 * Two comment forms, because TSX has two: a brace-wrapped JSX comment, and the
 * bare block comment that sits inside a `{cond ? ( … )}` arm. Only a comment that
 * opens a line or follows `(` / `{` is treated as a comment, so a stray slash-pair
 * inside a string or a regex is left alone rather than eating the rest of the
 * slice.
 */
const code = exchange
  .replace(/(^|[({])[ \t]*\/\*[\s\S]*?\*\//gm, "$1")
  .replace(/^[ \t]*\/\/.*$/gm, "");

function channels(hex: string): [number, number, number] {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  assert.ok(m, `not a 6-digit hex: ${hex}`);
  const n = parseInt(m![1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** WCAG 2.1 relative luminance, on sRGB values as they are actually stored. */
function luminance(hex: string): number {
  const [r, g, b] = channels(hex).map((c) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

test("the transcript's own surfaces carry no colour of their own", () => {
  // A `blue`/`purple`/`red` class in this slice is the bug back, whatever it is
  // attached to. Status hues and the interactive accent are *not* caught here:
  // they are absent from this slice by design, because who is speaking is said
  // by side and by the tail corner, not by hue.
  const hue = code.match(/\b(?:bg|text|border)-(?:blue|purple|violet|red|green|yellow)-/);
  assert.equal(
    hue?.[0],
    undefined,
    `the transcript picks a hue again: ${hue?.[0]} — a theme supplies surfaces, and a new token does not pick a colour`,
  );
});

test("the bubble and both avatars are opaque, so no theme paints words on weather", () => {
  // `raised` is a solid fill in every theme, but the *rule* has to be visible in
  // the source: a `/NN` alpha here would put the user's own prompt over the rain
  // and over whatever is scrolling beneath it.
  assert.match(
    code,
    /bg-codify-raised\b(?![\w/-]*\/)/,
    "the bubble fill lost its opacity",
  );
  assert.doesNotMatch(
    code,
    /bg-codify-(?:raised|surface)\/\d/,
    "a transcript surface went translucent — that is weather under the words",
  );
  assert.match(code, /text-codify-primary/, "the bubble text is not the theme's own");
});

test("every theme's bubble text reads on its bubble", () => {
  for (const theme of THEMES) {
    const raised = theme.tokens["--codify-raised"];
    const primary = theme.tokens["--codify-primary"];
    assert.ok(raised && primary, `${theme.id} is missing the pair the bubble needs`);
    const ratio = contrast(primary!, raised!);
    // AA for body text. Every shipped theme clears 4.5 with room to spare, so
    // this is not a bar any of them are near — it is the floor that stops the
    // *next* theme from shipping unreadable, which is the actual risk.
    assert.ok(
      ratio >= 4.5,
      `${theme.id}: ${primary} on ${raised} is ${ratio.toFixed(2)}:1, under the 4.5:1 floor for body text`,
    );
  }
});

test("the two speakers are told apart by side, not by hue", () => {
  // With both chips neutral, the only thing left that distinguishes them is
  // layout — and layout is what has to carry it, so it is asserted rather than
  // assumed. A future "let's make the assistant blue again" has to show up here.
  assert.match(code, /justify-end/, "the user's message is no longer on the right");
  assert.match(
    code,
    /rounded-tr-sm/,
    "the tail corner is gone, and it was the second half of saying whose line this is",
  );
  const avatars = code.match(/aria-hidden="true"/g) ?? [];
  assert.equal(
    avatars.length,
    2,
    "expected both avatar chips to be marked decorative; the count says they are still two",
  );
});
