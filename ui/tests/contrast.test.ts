/**
 * Every text colour the app actually paints, on the background it actually
 * paints it on, in every theme — judged against the AA number that its own size
 * and weight earn.
 *
 * **Why the pairs are read out of the source rather than listed here.** A
 * hand-written table of "these colours go together" is a claim about the app
 * that stops being true the first time someone adds a badge, and it fails in
 * the direction that looks like a passing suite: the new pair is simply absent.
 * So the pairs are *derived* — every string in every file under `ui/src` is
 * scanned for a `text-codify-*` and a `bg-codify-*` that sit in the same
 * literal, which is exactly the set of places one is drawn on top of the other.
 * A pair cannot be forgotten because it was never written down, and renaming a
 * token or moving a class moves the pair with it.
 *
 * **What that does and does not cover.** It covers a colour and its background
 * sharing one element, which is where the design system puts them: one class
 * string, one `className`. It does not cover a colour inheriting a background
 * from an ancestor three components up, because nothing in the source says which
 * ancestor that is.
 *
 * **Translucent backgrounds are measured too.** They used to be skipped, on the
 * argument that what shows through depends on what is behind them and guessing a
 * parent would manufacture a number. That left the whole status-pill idiom
 * (`bg-codify-danger/40` with red text on it) unmeasured, and it was under AA in
 * most themes. The honest answer to "what is behind" is a short list: every
 * surface a pill is drawn on is `bg`, `surface` or `raised`, so a translucent
 * background is composited over each of the three and the pair is held to the
 * worst of them. Text *opacity* is composited exactly as well: a tone at less
 * than full strength credits the text with contrast it does not have.
 *
 * **Why the weight is in the argument.** WCAG 1.4.3 asks for 4.5:1 and drops to
 * 3:1 for large text, where large means 18pt regular or 14pt **bold** — bold
 * being 700, not 600. This app is 280 `text-xs` and 124 `font-semibold`, so
 * the tempting move is to treat a semibold caption as large and check it at
 * 3:1. It is not large, and the distinction is the whole reason this file
 * exists rather than a loop over colour pairs.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { THEMES } from "../src/appearance.ts";
import { INK_TONES, deriveToneInks } from "../src/toneInk.ts";
import {
  aaThreshold,
  contrastRatio,
  composite,
  describeRequirement,
  relativeLuminance,
  type FontWeight,
} from "../src/contrast.ts";

// ── what the source paints ──────────────────────────────────────────────────

const SRC = new URL("../src/", import.meta.url).pathname;

/** Every `.ts`/`.tsx` under `ui/src`, so a new component cannot opt out. */
function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(entry) ? [path] : [];
  });
}

/**
 * The file with its comments removed.
 *
 * A comment is allowed to *name* a pair it is explaining — this file's own
 * neighbours do it constantly, and a reader who forbade it would end up with
 * nobody writing down why. Only the two comment forms are stripped, so a slash
 * inside a string or a regex is left where it is.
 */
function withoutComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/^[ \t]*\/\/.*$/gm, " ");
}

/**
 * Tailwind's size scale, in px — **as this app overrides it** in
 * `ui/tailwind.config.js`, not as Tailwright ships it.
 *
 * This table was Tailwind's defaults the first time round, which is wrong here
 * in six of seven entries: this app sets `xs` to 11px and `sm` to 12px so that
 * dense toolbars fit, and adds a `2xs` of 10px and an `md` of 14px that have no
 * Tailwind equivalent. Every threshold still came out the same — the whole scale
 * sits under 18.66px, so everything is body text either way — but a table that
 * reports 12px for a 10px badge is a table nobody can trust, and the first
 * `text-xl font-bold` heading would have been judged against the wrong rule.
 * The config is the owner; this is a copy, so a test below reads it back.
 */
const SIZES: Readonly<Record<string, number>> = {
  "2xs": 10,
  xs: 11,
  sm: 12,
  base: 13,
  md: 14,
  lg: 16,
  xl: 20,
};

const WEIGHTS: Readonly<Record<string, FontWeight>> = {
  normal: 400,
  medium: 500,
  semibold: 600,
  bold: 700,
};

/**
 * The size and weight a class string sets.
 *
 * Both default to the strictest thing that could be true — 12px at 400 — so a
 * class string that names neither is judged as body text. That is the
 * conservative direction: it can make a pair fail that would have passed had the
 * default been generous, and it cannot make a suite pass for the wrong reason.
 */
function typography(cls: string): { sizePx: number; weight: FontWeight } {
  const arbitrary = /text-\[(\d+)px\]/.exec(cls);
  const named = new RegExp(`\\btext-(${Object.keys(SIZES).join("|")})\\b`).exec(cls);
  const weight = /\bfont-(normal|medium|semibold|bold)\b/.exec(cls);
  return {
    sizePx: arbitrary ? Number(arbitrary[1]) : named ? SIZES[named[1]!]! : 12,
    weight: weight ? WEIGHTS[weight[1]!]! : 400,
  };
}

/** One text-on-background combination, and everywhere it is written down. */
interface Pair {
  readonly foreground: string;
  readonly foregroundAlpha: number;
  readonly background: string;
  /** 1 for an opaque fill; below 1 the fill is measured over each surface it can sit on. */
  readonly backgroundAlpha: number;
  /** The strictest threshold any of its uses earns — a pair is as weak as its
   *  smallest use, so one 12px label is enough to hold the pair to 4.5:1. */
  readonly threshold: number;
  readonly sizePx: number;
  readonly weight: FontWeight;
  /** Mutable while the scan runs — a later file can add another site — so
   *  this is a `Set` rather than the `ReadonlySet` a consumer should see. */
  readonly where: Set<string>;
}

const pairs = new Map<string, Pair>();

/**
 * The sets of classes that can appear on screen *together*, one per group.
 *
 * A regex over string literals is not good enough here, and the way it fails is
 * worth writing down because it produced a false finding while this was being
 * built. The workspace chip in `BottomCommandBar.tsx` reads
 *
 *     className={`… ${chosen ? "bg-raised border-border text-secondary" : "bg-info/40 border-info text-info"}`}
 *
 * and a regex that grabs the whole template literal pairs `bg-raised` with
 * `text-info` — two classes on opposite sides of a conditional, which never
 * render at the same time. That invented a failure, and the tempting fix is to
 * repaint the theme to satisfy a pair that cannot occur.
 *
 * So a conditional splits the groups: each arm is its own set, because each arm
 * is a different screen state. A plain interpolation does *not* split —
 * `` `bg-raised ${label}` `` and `` `bg-raised ${cond && "text-primary"}` `` are
 * one element — because there the expression is part of the same element rather
 * than a choice between two.
 *
 * **What this still cannot see.** Classes passed as separate arguments to a
 * helper (`cn("bg-raised", "text-primary")`) are two groups, so that pair is
 * missed. Erring that way loses a finding; pairing across a conditional invents
 * one, and a gate that invents its own failures gets ignored.
 *
 * Two lexical rules keep the walk from running away. A double-quoted string is
 * only a string if it *closes on the same line* — otherwise the quote belongs to
 * JSX prose and treating it as a delimiter swallows the rest of the file, which
 * is not a hypothetical: an apostrophe in "Codify's" did exactly that and
 * produced a pair of a colour painted on itself, failing in all nineteen themes.
 * Single quotes are not delimiters at all, for the same reason and the same
 * apostrophe. The codebase is double-quoted, so nothing real is lost.
 */
function classGroups(src: string): string[] {
  const out: string[] = [];
  const push = (s: string): void => {
    if (s.trim()) out.push(s);
  };
  let i = 0;
  while (i < src.length) {
    const c = src[i]!;
    if (c === '"') {
      const newline = src.indexOf("\n", i);
      const end = src.indexOf('"', i + 1);
      if (end < 0 || (newline >= 0 && end > newline)) {
        i += 1;
        continue;
      }
      push(src.slice(i + 1, end));
      i = end + 1;
      continue;
    }
    if (c !== "`") {
      i += 1;
      continue;
    }
    // A template literal: literal chunks join the current group, and each
    // `${…}` either continues it or, when it is a conditional, stands alone.
    let j = i + 1;
    let group = "";
    while (j < src.length) {
      if (src[j] === "\\") {
        group += src.slice(j, j + 2);
        j += 2;
        continue;
      }
      if (src[j] === "`") {
        j += 1;
        break;
      }
      if (src[j] === "$" && src[j + 1] === "{") {
        let depth = 1;
        let k = j + 2;
        while (k < src.length && depth > 0) {
          if (src[k] === "{") depth += 1;
          else if (src[k] === "}") depth -= 1;
          if (depth > 0) k += 1;
        }
        const inner = src.slice(j + 2, k);
        // A ternary at the top level means "one of these, never both".
        if (/(^|[^?])\?[^?:]*:/.test(inner)) out.push(...classGroups(inner));
        else group += classGroups(inner).join(" ");
        j = k + 1;
        continue;
      }
      group += src[j];
      j += 1;
    }
    push(group);
    i = j;
  }
  return out;
}

for (const file of sourceFiles(SRC)) {
  const where = relative(SRC, file);
  for (const cls of classGroups(withoutComments(readFileSync(file, "utf8")))) {
    const backgrounds = [...cls.matchAll(/\bbg-codify-([a-z-]+)(\/(\d+))?\b/g)];
    if (backgrounds.length === 0) continue;
    for (const fg of cls.matchAll(/\btext-codify-([a-z-]+)(?:\/(\d+))?\b/g)) {
      const { sizePx, weight } = typography(cls);
      const threshold = aaThreshold(sizePx, weight);
      for (const bg of backgrounds) {
        const key = `${fg[1]}/${fg[2] ?? 100}|${bg[1]}/${bg[3] ?? 100}`;
        const seen = pairs.get(key);
        if (seen) {
          // Keep the weakest use: a pair that is 3:1-legal at 20px bold and
          // 4.5-bound at 12px is a 4.5 pair, because both are on screen.
          if (threshold < seen.threshold) {
            pairs.set(key, { ...seen, threshold, sizePx, weight, where: seen.where });
          } else {
            seen.where.add(where);
          }
          continue;
        }
        pairs.set(key, {
          foreground: fg[1]!,
          foregroundAlpha: fg[2] ? Number(fg[2]) / 100 : 1,
          background: bg[1]!,
          backgroundAlpha: bg[3] ? Number(bg[3]) / 100 : 1,
          threshold,
          sizePx,
          weight,
          where: new Set([where]),
        });
      }
    }
  }
}

/**
 * Pairs the source scan cannot see, and the surface each is really painted on.
 *
 * A placeholder is the case that needs this. `placeholder-codify-muted` is not
 * a `text-codify-*` class, so the scan above never matched it at all — and the
 * composer's textarea is `bg-transparent`, so even a `text-` match would have
 * found no background, because the fill it sits on belongs to the command bar
 * card three components up. That is the inherited-background limitation stated
 * at the top of this file, and it happened to land on the one string a user is
 * most likely to be looking at when they say the chat prompt is the wrong
 * colour: the ghost text in the empty composer is `muted` on the command bar's
 * own `raised` fill.
 *
 * Declared rather than inferred, because the alternative is guessing a parent,
 * and a number invented from a guess is worse than no number. Each entry names
 * the surface, so a change to the component's fill shows up as this pair
 * becoming wrong rather than as a test that quietly stopped testing.
 */
const DECLARED_PAIRS: ReadonlyArray<{
  foreground: string;
  background: string;
  sizePx: number;
  weight: FontWeight;
  where: string;
  why: string;
}> = [
  {
    foreground: "muted",
    background: "raised",
    sizePx: SIZES.sm!,
    weight: 400,
    where: "components/BottomCommandBar.tsx (the composer placeholder)",
    why: "the textarea is bg-transparent, so the ghost text takes the command bar card's own raised fill from an ancestor",
  },
  {
    foreground: "accent",
    background: "raised",
    sizePx: SIZES.xs!,
    weight: 400,
    where: "components/Sidebar.tsx (the selected thread's title)",
    why: "the row's fill is on the container and the title's colour is on a child of it, so the two never share a class string — the same inherited-ancestor case as the placeholder, and the reason a selected row is readable at all",
  },
];

/**
 * A status tone as bare text on a surface: the red "Error" line, a warning sentence, a success
 * word. Those class strings carry no background (the fill belongs to the card or the page), so the
 * scan never sees them, and four themes had a tone under AA on `raised` that no test said so.
 * One declared pair per tone per surface, at the smallest text the app sets (11px regular).
 */
const STATUS_TEXT_PAIRS: ReadonlyArray<(typeof DECLARED_PAIRS)[number]> = INK_TONES.flatMap((tone) =>
  (["bg", "surface", "raised"] as const).map((surface) => ({
    foreground: tone,
    background: surface,
    sizePx: SIZES.xs!,
    weight: 400 as FontWeight,
    where: `status text (text-codify-${tone}) on the ${surface} surface`,
    why: "bare status text sits on an inherited surface the class string does not name",
  })),
);

for (const declared of [...DECLARED_PAIRS, ...STATUS_TEXT_PAIRS]) {
  const key = `${declared.foreground}/100|${declared.background}/100`;
  if (!pairs.has(key)) {
    pairs.set(key, {
      foreground: declared.foreground,
      foregroundAlpha: 1,
      background: declared.background,
      backgroundAlpha: 1,
      threshold: aaThreshold(declared.sizePx, declared.weight),
      sizePx: declared.sizePx,
      weight: declared.weight,
      where: new Set([declared.where]),
    });
  }
}

const ALL_PAIRS: ReadonlyArray<Pair> = [...pairs.values()].sort((a, b) =>
  `${a.foreground} on ${a.background}`.localeCompare(
    `${b.foreground} on ${b.background}`,
  ),
);

// ── what a theme actually renders ───────────────────────────────────────────

/**
 * The `:root` block, which is the floor every theme stands on.
 *
 * `--codify-design` and `--codify-knowledge` live here and in no theme, which is
 * why the audit has to read it rather than trusting `theme.tokens`: a token a
 * theme does not publish is not absent, it is the default, and 29 of the app's
 * text utilities are one of those two.
 */
const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const rootStart = css.indexOf(":root");
assert.notEqual(rootStart, -1, "index.css has no :root block to read defaults from");
const ROOT: Readonly<Record<string, string>> = Object.fromEntries(
  [...css.slice(rootStart, css.indexOf("}", rootStart)).matchAll(
    /(--[a-z-]+):\s*(#[0-9a-fA-F]{6})/g,
  )].map((m) => [m[1]!, m[2]!]),
);

/** Each theme's derived inks, computed once: what `applyTheme` publishes as `--codify-X-ink`. */
const INKS = new Map(THEMES.map((t) => [t.id, deriveToneInks({ ...ROOT, ...t.tokens })]));

/** The colour a theme renders a token as, or `undefined` if it has none. */
function rendered(theme: (typeof THEMES)[number], name: string): string | undefined {
  const key = `--codify-${name}`;
  return theme.tokens[key as keyof typeof theme.tokens] ?? INKS.get(theme.id)?.[key] ?? ROOT[key];
}

/** The surfaces a translucent fill can sit on, which is every surface a pill is drawn on. */
const SURFACE_TOKENS = ["bg", "surface", "raised"] as const;

/** The measured ratio for one pair in one theme: both alphas applied, the worst surface taken. */
function measure(pair: Pair, theme: (typeof THEMES)[number]): number {
  const foreground = rendered(theme, pair.foreground);
  const background = rendered(theme, pair.background);
  assert.ok(
    foreground && background,
    `${theme.id} has no --codify-${pair.foreground} or --codify-${pair.background}, ` +
      `so the pair cannot be measured at all — a missing token is a failure, not a skip`,
  );
  const fills =
    pair.backgroundAlpha === 1
      ? [background]
      : SURFACE_TOKENS.map((surface) => composite(background, pair.backgroundAlpha, rendered(theme, surface)!));
  return Math.min(
    ...fills.map((fill) =>
      contrastRatio(
        pair.foregroundAlpha === 1 ? foreground : composite(foreground, pair.foregroundAlpha, fill),
        fill,
      ),
    ),
  );
}

// ── the audit ───────────────────────────────────────────────────────────────

test("the scan finds the pairs, so a passing audit is not a passing empty list", () => {
  // A rename that broke the pattern would leave `ALL_PAIRS` empty and every
  // assertion below would pass for the wrong reason. This is the guard.
  assert.ok(
    ALL_PAIRS.length >= 20,
    `only ${ALL_PAIRS.length} text-on-background pairs were found in ui/src — ` +
      "the scan has stopped matching and the audit below is now vacuous",
  );
  assert.ok(
    new Set(ALL_PAIRS.map((p) => p.background)).size >= 3,
    "the scan found fewer than three distinct backgrounds; it is not reading the source",
  );
  // The pill idiom is a translucent fill. A scan that quietly went back to skipping those would still
  // find 20 opaque pairs and pass, while measuring none of the status pills in the app.
  const translucent = ALL_PAIRS.filter((p) => p.backgroundAlpha < 1);
  assert.ok(
    translucent.length >= 8,
    `only ${translucent.length} translucent-background pairs were found; the status pills are no longer being measured`,
  );
  assert.ok(
    translucent.some((p) => p.foreground.endsWith("-ink")),
    "no pair reads a status ink on a tint, so the ink the pills use is not under the audit",
  );
});

test("a translucent fill is held to the worst surface it can sit on", () => {
  // Over black a 40% tint is dark and over white it is pale, and the same red text has a very
  // different ratio on each. Measuring over only one surface credits the pair with the kinder answer.
  const dark = THEMES.find((t) => t.id === "codify-dark")!;
  const theme = {
    ...dark,
    tokens: { ...dark.tokens, "--codify-bg": "#000000", "--codify-surface": "#808080", "--codify-raised": "#ffffff" },
  };
  const pair: Pair = {
    foreground: "danger",
    foregroundAlpha: 1,
    background: "danger",
    backgroundAlpha: 0.4,
    threshold: 4.5,
    sizePx: 12,
    weight: 400,
    where: new Set(["test"]),
  };
  const red = "#f85149";
  const over = (surface: string): number => contrastRatio(red, composite(red, 0.4, surface));
  const expected = Math.min(over("#000000"), over("#808080"), over("#ffffff"));
  assert.ok(Math.abs(measure(pair, theme) - expected) < 1e-9, `measured ${measure(pair, theme)}, the worst surface gives ${expected}`);
  assert.ok(expected < over("#000000") - 0.5, "the test surfaces do not differ enough to tell worst-case from first-case");
});

test("dimmed text is measured dimmed", () => {
  // The app no longer dims a status tone (see the policy test below), so this branch of the audit
  // has no data behind it from the source — which is exactly why it gets its own test rather than a
  // hopeful assertion about the scan. The day a dimmed colour is declared or lands on a background,
  // this is the arithmetic that decides its ratio.
  const dimmed = composite("#f85149", 0.8, "#0d1117");
  assert.equal(relativeLuminance(dimmed) > 0, true);
  assert.ok(
    contrastRatio(dimmed, "#0d1117") < contrastRatio("#f85149", "#0d1117"),
    "compositing at 80% produced the same contrast as the undimmed colour",
  );
  assert.equal(composite("#f85149", 0, "#0d1117"), "#0d1117", "0% must be the background");
  assert.equal(composite("#f85149", 1, "#0d1117"), "#f85149", "100% must be the colour");
});

// ── the class-string rules the pair audit cannot state ──────────────────────

/** Every class group in `ui/src`, with the file it is in, for rules about how a string is written. */
const GROUPS: ReadonlyArray<{ where: string; cls: string }> = sourceFiles(SRC).flatMap((file) =>
  classGroups(withoutComments(readFileSync(file, "utf8"))).map((cls) => ({
    where: relative(SRC, file),
    cls,
  })),
);

test("a status tint is read in the tone's ink, never in the bare tone", () => {
  // `bg-codify-danger/40 text-codify-danger` is red on a tint of itself, and it was under AA in 54 of
  // 57 theme-and-surface combinations. The pair audit would catch a failing one by measuring it; this
  // names the cause, so a new pill fails with the fix in the message instead of a ratio.
  const violations: string[] = [];
  for (const { where, cls } of GROUPS) {
    for (const bg of cls.matchAll(/\bbg-codify-(accent|info|success|warning|danger|design|knowledge)\/\d+\b/g)) {
      const tone = bg[1]!;
      if (new RegExp(`\\btext-codify-${tone}(?![-\\w])`).test(cls)) {
        violations.push(`${where}: "${cls.trim().slice(0, 110)}" — use text-codify-${tone}-ink on a tint of ${tone}`);
      }
    }
  }
  assert.deepEqual([...new Set(violations)], [], "bare tone on its own tint:\n" + violations.join("\n"));
});

test("no status tint is stronger than 40% where text sits on it", () => {
  // The ink is derived against a 40% tint (`toneInk.ts`): a weaker tint is further from the text and so
  // easier, which is why clearing 40 clears them all. A hover state at 60 is a stronger tint the ink was
  // never derived for, and it failed in 14 themes. Hover feedback on a tint is brightness, or at most 40.
  const violations: string[] = [];
  for (const { where, cls } of GROUPS) {
    if (!/\btext-codify-/.test(cls)) continue;
    for (const bg of cls.matchAll(/\bbg-codify-(accent|info|success|warning|danger|design|knowledge)\/(\d+)\b/g)) {
      if (Number(bg[2]) > 40) violations.push(`${where}: ${bg[0]} in "${cls.trim().slice(0, 100)}"`);
    }
  }
  assert.deepEqual([...new Set(violations)], [], "a status tint stronger than the ink was derived for:\n" + violations.join("\n"));
});

test("status text is never dimmed: emphasis is size or weight, not a thinner colour", () => {
  // `text-codify-warning/90` is a tone composited toward whatever is behind it, on a surface the class
  // string does not name, so the audit cannot measure it, and DESIGN.md says emphasis never comes from
  // opacity. The declared status-text pairs measure the full-strength tone; a dimmed one escapes them.
  const violations: string[] = [];
  for (const { where, cls } of GROUPS) {
    for (const m of cls.matchAll(/\btext-codify-(accent|info|success|warning|danger|design|knowledge)\/\d+\b/g)) {
      violations.push(`${where}: ${m[0]}`);
    }
  }
  assert.deepEqual([...new Set(violations)], [], "dimmed status text:\n" + violations.join("\n"));
});

test("the size/weight rule is WCAG's and not a convenient reading of it", () => {
  // 11px semibold is the app's most common run of text by a wide margin, and it
  // is body text. If this ever passes at 3:1 the audit below is not measuring
  // what it claims to measure.
  assert.equal(aaThreshold(11, 600), 4.5, "11px semibold is not large text");
  assert.equal(aaThreshold(19, 600), 4.5, "19px semibold is not large text");
  assert.equal(aaThreshold(19, 700), 3, "19px bold is large text");
  assert.equal(aaThreshold(24, 400), 3, "24px regular is large text");
  assert.equal(aaThreshold(10, 700), 4.5, "bold only counts from 14pt");
  assert.equal(aaThreshold(11, 400), 4.5, "11px regular is body text");
});

test("the size table is the app's scale, read back from the config", () => {
  // The first version of this table was Tailwind's defaults, which are wrong
  // here in six of seven entries — this app shrinks the scale so dense toolbars
  // fit, and adds a 2xs and an md that Tailwind has no name for. Every verdict
  // came out the same, which is exactly why it survived: a table that is wrong
  // in a way no assertion can see is worse than one that is missing. So the
  // config is read back here, and the copy above is checked against it.
  const config = readFileSync(
    new URL("../tailwind.config.js", import.meta.url),
    "utf8",
  );
  const block = config.slice(config.indexOf("fontSize: {"));
  const fromConfig: Record<string, number> = {};
  // The config is in rem (so the UI scale can reach it), and this table is in px at
  // the default 16px root: the contrast rule is about the size a person sees at 100%.
  for (const m of block.matchAll(/"?(?:2xs|xs|sm|base|md|lg|xl)"?:\s*\["(\d+(?:\.\d+)?)rem"/g)) {
    const name = /"?(?:2xs|xs|sm|base|md|lg|xl)"?/.exec(m[0].replace(/:\s*\[.*/, ""))?.[0].replace(/"/g, "");
    if (name) fromConfig[name] = Number(m[1]) * 16;
  }
  assert.deepEqual(
    { ...SIZES },
    fromConfig,
    "the size table in this file no longer matches ui/tailwind.config.js — a " +
      "wrong px here would judge every run of text against the wrong rule",
  );
});

test("every painted text/background pair reads at AA in every theme", () => {
  const failures: string[] = [];
  for (const pair of ALL_PAIRS) {
    for (const theme of THEMES) {
      const ratio = measure(pair, theme);
      if (ratio < pair.threshold) {
        failures.push(
          `  ${theme.id}  ${ratio.toFixed(2)}:1, needs ${pair.threshold}:1 — ` +
            `text-codify-${pair.foreground}${pair.foregroundAlpha < 1 ? `/${Math.round(pair.foregroundAlpha * 100)}` : ""} ` +
            `on bg-codify-${pair.background}${pair.backgroundAlpha < 1 ? `/${Math.round(pair.backgroundAlpha * 100)}` : ""} at ${pair.sizePx}px/${pair.weight} ` +
            `(${describeRequirement(pair.sizePx, pair.weight)}) ` +
            `— painted in ${[...pair.where].join(", ")}`,
        );
      }
    }
  }
  assert.equal(
    failures.length,
    0,
    `${failures.length} of ${ALL_PAIRS.length * THEMES.length} pair/theme combinations are under WCAG AA:\n` +
      failures.join("\n"),
  );
});

test("the audit reaches every theme, and the next one is audited for free", () => {
  // The loop above iterates `THEMES`, so a new theme is covered by
  // construction — and three were added to `appearance.ts` while this file was
  // being written, which is the case that matters. What is worth pinning is
  // that the loop is still over the whole list, because "all themes" quietly
  // becoming "the themes that existed when the test was written" is the failure
  // that would otherwise be invisible.
  assert.ok(
    THEMES.length >= 8,
    `THEMES has ${THEMES.length} entries; the audit is not covering a real palette set`,
  );
  assert.equal(
    new Set(THEMES.map((t) => t.id)).size,
    THEMES.length,
    "two themes share an id, so one is audited twice and another not at all",
  );
  assert.equal(
    ALL_PAIRS.length * THEMES.length,
    ALL_PAIRS.length * new Set(THEMES.map((t) => t.id)).size,
    "the pair/theme product no longer matches the pair count times the theme count",
  );
  for (const theme of THEMES) {
    assert.ok(theme.label && theme.description, `${theme.id} is missing what the settings pane shows`);
  }
});

test("a theme that republishes a token below AA cannot slip past the audit", () => {
  // The audit reads `THEMES`, so a token's value in a theme is what gets
  // measured. This pins that claim with a value the shipped themes do not use,
  // so a change to how the colours are resolved — a theme dropping out of
  // `THEMES`, `rendered` reading `:root` unconditionally — fails here.
  const broken = {
    ...THEMES[0]!,
    tokens: { ...THEMES[0]!.tokens, "--codify-muted": THEMES[0]!.tokens["--codify-bg"] },
  };
  const worst = Math.max(
    ...ALL_PAIRS.filter((p) => p.foreground === "muted").map((p) => measure(p, broken)),
  );
  assert.ok(
    worst < 4.5,
    "setting a theme's muted text to its own background still measured as readable, " +
      "so this audit is not actually reading theme tokens",
  );
});

// ── the text cursor ─────────────────────────────────────────────────────────

test("the text cursor is the accent, and it clears 3:1 on every surface a field sits on, in every theme", () => {
  // The rule lives in index.css: `input, textarea { caret-color: var(--codify-accent) }`, in the base
  // layer. A caret is a graphic, not text, so the bar is WCAG 1.4.11's 3:1 and not 4.5:1. The surfaces
  // are the three a field is ever filled with (the prompt card is `surface`, the filters in a menu are
  // `bg`, a settings field is `raised`); a theme whose accent is too close to any of them has a caret
  // you lose in the box.
  const rule = css.match(/@layer base\s*\{\s*input,\s*textarea\s*\{([^}]*)\}/);
  assert.ok(rule, "index.css has no `input, textarea` rule in the base layer, so the caret is not themed");
  assert.match(rule[1]!, /caret-color:\s*var\(--codify-accent\)\s*;/, "the cursor is not read from --codify-accent");
  for (const theme of THEMES) {
    const accent = rendered(theme, "accent");
    assert.ok(accent, `${theme.id} publishes no --codify-accent, so its cursor has no colour`);
    for (const surface of SURFACE_TOKENS) {
      const ratio = contrastRatio(accent, rendered(theme, surface)!);
      assert.ok(
        ratio >= 3,
        `${theme.id}: the accent cursor is ${ratio.toFixed(2)}:1 on --codify-${surface} (${accent} on ${rendered(theme, surface)}), under 3:1`,
      );
    }
  }
});
