/**
 * The Badge primitive's tone shapes: failed, warning and done are not told apart by colour alone.
 *
 * Rendered, not read as source: the assertions are about the markup a pill produces.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const React = (await import("react")).default;
const { Badge } = await import("../src/components/ui/Badge.tsx");

type Tone = "neutral" | "info" | "success" | "warning" | "danger";
const render = (tone: Tone, extra: Record<string, unknown> = {}): string =>
  renderToStaticMarkup(React.createElement(Badge, { tone, children: "word", ...extra }));
const svgOf = (markup: string): string => /<svg[\s\S]*?<\/svg>/.exec(markup)?.[0] ?? "";
const textOf = (markup: string): string => markup.replace(/<[^>]*>/g, "");

test("success, warning and danger each carry a glyph, and the three glyphs differ", () => {
  const glyphs = (["success", "warning", "danger"] as const).map((t) => svgOf(render(t)));
  for (const g of glyphs) assert.ok(g.length > 0, "a severity pill has no glyph, so only colour tells it apart");
  assert.equal(new Set(glyphs).size, 3, "two severities share a glyph, so they differ by colour alone");
});

test("the glyph is decoration: hidden from assistive technology and adding no text", () => {
  for (const tone of ["success", "warning", "danger"] as const) {
    const markup = render(tone);
    assert.match(svgOf(markup), /aria-hidden="true"/, `${tone}: the glyph is announced`);
    assert.equal(textOf(markup), "word", `${tone}: the glyph added text to the pill`);
  }
});

test("neutral and info are not severities, and carry no glyph", () => {
  for (const tone of ["neutral", "info"] as const) assert.equal(svgOf(render(tone)), "", `${tone} has a glyph`);
});

test("a count or a tag can turn the glyph off", () => {
  for (const tone of ["success", "warning", "danger"] as const) {
    assert.equal(svgOf(render(tone, { icon: false })), "", `${tone}: icon={false} still drew one`);
  }
});

test("the glyph is sized in rem, so it follows the UI scale with the 2xs text beside it", () => {
  const svg = svgOf(render("danger"));
  assert.match(svg, /class="[^"]*\bh-2\.5\b[^"]*\bw-2\.5\b|class="[^"]*\bw-2\.5\b[^"]*\bh-2\.5\b/, "glyph size is not a Tailwind step (rem)");
  assert.doesNotMatch(svg, /\bwidth="\d+px"|\bheight="\d+px"|\b(?:w|h)-\[\d+px\]/, "glyph is pinned in px and would not scale");
});
