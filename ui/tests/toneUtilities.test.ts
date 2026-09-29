/**
 * The runtime theme layer, proved to cover what uses it.
 *
 * `ui/src/index.css` re-points the `codify-*` utility names at live CSS custom
 * properties, because Tailwind bakes literal `rgb(…)` values at build time and a
 * theme applied at runtime would repaint the body and nothing else. That layer is
 * a *hand-written table*, and it had two failure modes that no type checker saw.
 *
 * **A missing rule.** A component uses `bg-codify-info/60`, the table has `/40`
 * and `/20` and not `/60`, so the class compiles — to the *default theme's*
 * colour, from Tailwind. It looks fine in Codify Dark and wrong in the other
 * eight.
 *
 * **A variant the layer cannot reach.** This one is worse, because it looks like
 * the table is working. Tailwind compiles `focus-within:border-codify-accent`
 * into `.focus-within\:border-codify-accent:focus-within` — the baked value, plus
 * a pseudo-class, so two classes of specificity against the layer's single
 * `.border-codify-accent`. Source order is irrelevant; the variant wins, and the
 * composer shows a blue focus ring in the OLED CMatrix app. It is the difference
 * between a class that is absent and a class that is *actively wrong*, and it is
 * why the table has to repeat itself per variant rather than once per colour.
 *
 * So the table is checked against the source instead of trusted — the same
 * argument `designTokens.test.ts` makes about DESIGN.md, applied to the layer a
 * theme actually paints through.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, "..", "src");

/** Every `.ts`/`.tsx` under `src/`, comments stripped so a class quoted in a
 * docstring is not mistaken for one in use — the same reason
 * `appearance.test.ts` strips before matching. */
function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...sourceFiles(full));
    } else if (/\.tsx?$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

const css = readFileSync(path.join(SRC, "index.css"), "utf8");

/** The escaped class token as CSS spells it, e.g. `hover\:bg-codify-info\/60`. */
function escaped(name: string): string {
  // Every colon, not just the first: a compound variant like
  // `disabled:hover:bg-codify-info/20` spells `.disabled\:hover\:bg-…` in CSS, and
  // replacing one colon produced a selector that matched nothing.
  return name.replace(/:/g, "\\:").replace("/", "\\/");
}

function utilitiesInUse(): Map<string, string> {
  const used = new Map<string, string>();
  // The variant prefix is optional and may be *compound* — `disabled:hover:` is
  // two of them, and it compiles to two pseudo-classes, so it needs a rule with
  // both. The first version of this pattern allowed one prefix and matched only
  // the inner `hover:` of such a class, which asked the layer for a
  // `.hover\:bg-…` rule that would match no element on the page: the test would
  // have been satisfied by dead CSS.
  const variant = "(?:hover|focus|focus-within|focus-visible|active|disabled|group-hover|peer-focus):";
  const pattern = new RegExp(
    `(?:${variant})?(?:${variant})?(?:bg|text|border|placeholder)-codify-[a-z-]+(?:/\\d+)?`,
    "g",
  );
  for (const file of sourceFiles(SRC)) {
    const code = readFileSync(file, "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/\/\/.*$/gm, "");
    for (const m of code.matchAll(pattern)) {
      if (!used.has(m[0])) used.set(m[0], path.relative(SRC, file));
    }
  }
  return used;
}

test("every themed utility in the source is defined in the runtime layer", () => {
  const used = utilitiesInUse();
  assert.ok(used.size > 0, "no themed utilities found — the match is wrong, not the UI");

  const missing = [...used.entries()].filter(
    ([name]) => !css.includes("." + escaped(name)),
  );
  assert.deepEqual(
    missing,
    [],
    "these classes have no rule in ui/src/index.css, so they fall back to " +
      "Tailwind's baked default and ignore the theme:\n" +
      missing.map(([n, f]) => `  ${n}  (${f})`).join("\n"),
  );
});

test("a variant utility is defined at the variant, not only at the base", () => {
  // The specific regression, pinned on its own because it is invisible from the
  // base-class check above: `hover:border-codify-accent` needs a rule whose
  // selector carries `:hover`, or the base rule does nothing for it and Tailwind
  // paints the composer's ring in the default theme's blue. One assertion each for
  // a hover, a focus and a `focus-within`, because those are the three that were
  // broken and they fail in the same way for the same reason.
  const variants: ReadonlyArray<[string, string]> = [
    ["hover:bg-codify-info/60", ".hover\\:bg-codify-info\\/60:hover"],
    ["focus:border-codify-accent", ".focus\\:border-codify-accent:focus"],
    [
      "focus-within:border-codify-accent",
      ".focus-within\\:border-codify-accent:focus-within",
    ],
    ["group-hover:text-codify-accent", ".group:hover .group-hover\\:text-codify-accent"],
  ];
  for (const [name, selector] of variants) {
    assert.ok(
      css.includes(selector),
      `index.css has no ${selector} for ${name}. A bare ` +
        ".border-codify-accent cannot reach a variant: Tailwind's rule carries " +
        "a pseudo-class, so it outranks the layer on specificity and the element " +
        "keeps the baked default-theme colour",
    );
  }
});

test("the default theme publishes every tone the source can ask for", () => {
  // The other half. A class can be defined and still paint nothing if the
  // variable behind it is unset, and `:root` is what a theme that declines to
  // restate a tone inherits from.
  //
  // The *hexes* only, not the `-rgb` triplets — `applyTheme` derives a triplet
  // from every managed variable it writes, and that is where the other eight
  // tokens get theirs too. `main.tsx` applies the theme before the first
  // render, so nothing React paints is ever read from an un-derived triplet;
  // asserting the triplets here would be asking this layer to be stricter than
  // the one it sits in.
  const root = /:root\s*\{([\s\S]*?)\}/.exec(css);
  assert.ok(root, "index.css has no :root block");
  for (const tone of ["accent", "info", "success", "warning", "danger", "neutral"]) {
    assert.match(
      root[1],
      new RegExp(`--codify-${tone}:`),
      `:root does not declare --codify-${tone}, so a theme that does not ` +
        "restate it leaves that tone undefined",
    );
  }
});
