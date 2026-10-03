/**
 * An icon button's icon is as big as it was drawn.
 *
 * `IconButton` is a fixed-size square around an icon, built on `Button`, which carries horizontal
 * padding for its size. The square's own `p-0` lost to that padding in the stylesheet, so a 28px
 * button had an 8px content box and the icon in it shrank to match: the close and refresh icons in
 * the drawers, and the panel toggle, were slivers. jsdom has no layout, so the proof here is one
 * step earlier and just as concrete: the classes the button renders are compiled through the real
 * Tailwind config, and the padding reset must come out as an `!important` rule, which no `px-*`
 * can outrank. (Tailwind 4 writes the important modifier at the end of the class, `p-0!`.)
 */
import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { fileURLToPath } from "node:url";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const React = (await import("react")).default;
const { IconButton } = await import("../src/components/ui/IconButton.tsx");

const classOf = (props: Record<string, unknown>): string => {
  const markup = renderToStaticMarkup(
    React.createElement(IconButton, { label: "Close", ...props } as never, React.createElement("svg")),
  );
  const raw = /class="([^"]*)"/.exec(markup)?.[1] ?? "";
  // Markup escapes `&` and `>`, and the arbitrary variant `[&>svg]:shrink-0` has both; a browser
  // hands the class list back unescaped, so that is what gets compiled and compared.
  return raw.replace(/&gt;/g, ">").replace(/&lt;/g, "<").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
};

// The PostCSS plugin keeps one compiler per input path and treats a repeat path as the same file, so
// each compile gets a path of its own rather than being handed the previous compile's stylesheet.
let compiles = 0;

async function compile(classes: string): Promise<string> {
  const postcss = (await import("postcss")).default;
  const tailwind = (await import("@tailwindcss/postcss")).default;
  // The same entry `src/index.css` has, less the reset: the theme, the utilities, and the real config.
  // The classes under test arrive as an inline source. The config's own `content` globs are read as
  // well, so the output carries every class the app uses, and the assertions below match one rule at a
  // time rather than the whole sheet. `from` is where `@config`'s relative path starts.
  const entry = fileURLToPath(new URL(`../src/entry-under-test-${compiles++}.css`, import.meta.url));
  const input = [
    '@import "tailwindcss/theme.css" layer(theme);',
    '@import "tailwindcss/utilities.css" source(none);',
    '@config "../tailwind.config.js";',
    `@source inline(${JSON.stringify(classes)});`,
  ].join("\n");
  const result = await postcss([tailwind()]).process(input, { from: entry });
  return result.css;
}

test("both sizes render the padding reset and the icon guard", () => {
  for (const size of ["sm", "md"] as const) {
    const classes = classOf({ size });
    assert.ok(classes.split(/\s+/).includes("p-0!"), `${size}: padding reset is not important: ${classes}`);
    assert.ok(classes.split(/\s+/).includes("[&>svg]:shrink-0"), `${size}: the icon can still shrink: ${classes}`);
  }
});

test("the padding reset compiles to an important rule, which Button's px-* cannot outrank", async () => {
  const css = await compile(classOf({ size: "sm" }));
  assert.match(css, /\.p-0\\!\s*\{[^}]*padding:\s*0px\s*!important/, "p-0! did not compile to an important padding rule");
  // And the icon guard is a real rule on the svg child, not a class Tailwind dropped.
  assert.match(css, /\\\[\\&\\>svg\\\]\\:shrink-0\s*>\s*svg\s*\{[^}]*flex-shrink:\s*0/);
});

test("the base button's own padding is what the reset has to beat, and it is not important", async () => {
  // The reason `!` is needed, pinned: if `Button` ever stops carrying `px-2.5` the reset is optional,
  // and if it starts using important padding of its own this test says the two now fight.
  const css = await compile("px-2.5 p-0");
  assert.doesNotMatch(css, /padding-inline:\s*[^;]*!important/);
  const order = (needle: string): number => css.indexOf(needle);
  assert.ok(order(".p-0") < order(".px-2\\.5"), "Tailwind no longer emits px-* after p-*, so a plain p-0 would work again");
});

test("a caller's own classes are kept after the button's", () => {
  const classes = classOf({ size: "md", className: "w-8 h-8 shadow" });
  assert.ok(classes.includes("w-8 h-8 shadow"), classes);
  assert.ok(classes.indexOf("p-0!") < classes.indexOf("w-8 h-8 shadow"));
});
