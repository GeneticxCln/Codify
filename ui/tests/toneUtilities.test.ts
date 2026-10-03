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

import { THEMES } from "../src/appearance.ts";
import { INK_TONES, MODE_ACCENTS, deriveToneInks, inkVar } from "../src/toneInk.ts";

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
    ["hover:bg-codify-warning/40", ".hover\\:bg-codify-warning\\/40:hover"],
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

test("Tailwind's utilities are emitted unlayered and ahead of every rule that overrides them", () => {
  // `@import "tailwindcss"` puts the utilities in a cascade layer, and a rule outside every layer beats
  // a layered one whatever its specificity. This file was written for a flat cascade, and under a layer
  // the resting `.bg-codify-surface` below would beat `hover:bg-codify-raised` outright: no hover colour
  // would show. It is also what the file's own header says, and the header is not a test.
  assert.doesNotMatch(
    css,
    /@import\s+["']tailwindcss["']\s*;/,
    'index.css imports "tailwindcss" whole, which layers the utilities. Import theme.css, preflight.css and utilities.css separately (see the top of the file)',
  );
  const utilities = /@import\s+"tailwindcss\/utilities\.css"([^;]*);/.exec(css);
  assert.ok(utilities, 'index.css does not import "tailwindcss/utilities.css" itself');
  assert.doesNotMatch(utilities[1], /layer\(/, "the utilities are in a cascade layer; every unlayered rule below now beats them");
  const firstOverride = /^\.bg-codify-bg\s*\{/m.exec(css);
  assert.ok(firstOverride && firstOverride.index > utilities.index, "the runtime layer is not after the utilities, so it would lose every tie to them");
});

test("no selector in index.css repeats a class to outrank Tailwind's", () => {
  // Under Tailwind 3 the generated variant rules landed *after* this file, and each rule here repeated
  // its class once to win on specificity instead of position. Tailwind 4 emits them before. The repeat
  // then ties a themed hover rule (three classes deep) with a compound variant such as
  // `disabled:hover:bg-inherit` (one class, two pseudo-classes) and, being later, beats it: a disabled
  // button highlighted on hover. Found by comparing every element of the app under both versions.
  const repeated = [...css.matchAll(/^(\.[^{\n]+)\{/gm)]
    .map((m) => m[1].trim())
    .filter((selector) => /(\.(?:\\.|[\w-])+)\1(?![\w-])/.test(selector));
  assert.deepEqual(
    repeated,
    [],
    "these selectors repeat a class. The utilities are emitted first, so a plain selector already wins " +
      "ties by position, and the repeat only makes it beat variants that are meant to beat it:\n  " +
      repeated.join("\n  "),
  );
});

test("a default-palette class the source uses is pinned to the colour Tailwind 3 drew", () => {
  // Tailwind 4 redrew its default palette in oklch, a little more saturated. The UI uses the default
  // palette in a handful of places on purpose (hardcodedPalette.test.ts names them), and an upgrade must
  // not recolour them. Every such class needs its `--color-<hue>-<shade>` pinned in index.css.
  const palette =
    /\b(?:bg|text|border|ring|fill|stroke|from|via|to|divide|outline|decoration|accent|caret|shadow|placeholder)-(slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-(\d{2,3})\b/g;
  const wanted = new Map<string, string>();
  for (const file of sourceFiles(SRC)) {
    const code = readFileSync(file, "utf8").replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    for (const m of code.matchAll(palette)) wanted.set(`--color-${m[1]}-${m[2]}`, path.relative(SRC, file));
  }
  assert.ok(wanted.size > 0, "no default-palette class found — the match is wrong, not the UI");
  const unpinned = [...wanted].filter(([variable]) => !new RegExp(`${variable}\\s*:\\s*#[0-9a-fA-F]{6}\\s*;`).test(css));
  assert.deepEqual(
    unpinned,
    [],
    "these default-palette colours are not pinned in the @theme block of index.css, so Tailwind 4's " +
      "redrawn palette decides what they look like:\n" +
      unpinned.map(([v, f]) => `  ${v}  (${f})`).join("\n"),
  );
});

test("the Tailwind 3 defaults this UI relied on are restored", () => {
  // Each of these was found by rendering the app under both versions and comparing every element.
  // The reset Tailwind 4 ships drops them, and nothing fails: a button just stops showing a pointer, a
  // placeholder changes colour, the whole window changes typeface.
  const restored: ReadonlyArray<[string, RegExp]> = [
    ["the system UI font as the sans stack", /--font-sans\s*:\s*ui-sans-serif,\s*system-ui/],
    ["a pointer on enabled buttons", /button:not\(:disabled\)[^{]*\{[^}]*cursor:\s*pointer/],
    ["the placeholder colour of an input that names none", /input::placeholder[^{]*\{[^}]*color:\s*var\(--color-gray-400\)/],
    ["the browser's padding on table cells", /\btd,\s*th\s*\{[^}]*padding:\s*1px/],
    ["the browser's padding on options", /\boption\s*\{[^}]*padding:\s*0 2px 1px/],
    ["a search field that is a plain text field", /\[type="search"\]\s*\{[^}]*appearance:\s*textfield/],
  ];
  for (const [what, pattern] of restored) {
    assert.match(css, pattern, `index.css no longer restores ${what}, which Tailwind 3's reset gave and 4's does not`);
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

test("the mode accents toneInk.ts derives against are the ones the stylesheet declares", () => {
  // The mode accents are not theme-managed, so a theme's tokens do not carry them and the ink for
  // `design` and `knowledge` is derived from this copy. If `:root` moved one, the ink would be
  // derived against a colour that is no longer painted.
  assert.match(css, new RegExp(`--codify-design:\\s*${MODE_ACCENTS.design};`, "i"));
  assert.match(css, new RegExp(`--codify-knowledge:\\s*${MODE_ACCENTS.knowledge};`, "i"));
});

test("index.css reads every ink, and carries the default theme's inks as its :root fallback", () => {
  const dark = THEMES.find((t) => t.id === "codify-dark")!;
  const inks = deriveToneInks({ ...dark.tokens });
  for (const tone of INK_TONES) {
    assert.ok(css.includes(`.text-codify-${tone}-ink {`), `no .text-codify-${tone}-ink rule`);
    assert.ok(css.includes(`var(--codify-${tone}-ink-rgb)`), `${tone}-ink-rgb is never read`);
    assert.ok(
      new RegExp(`${inkVar(tone)}:\\s*${inks[inkVar(tone)]};`, "i").test(css),
      `:root has no fallback for ${inkVar(tone)} = ${inks[inkVar(tone)]}`,
    );
  }
});
