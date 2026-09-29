/**
 * The settings, stats and audit panels carry no hardcoded colour.
 *
 * The migration that produced this file replaced roughly 350 Tailwind default
 * palette classes across sixteen components with the `codify-*` theme tokens.
 * The reason they were worth replacing is not tidiness: `bg-blue-600` and
 * `text-gray-400` are not "a colour that happens to be written inline", they are
 * a colour that is **correct in Codify Dark and wrong in the other fifteen**.
 * Every one of them looked fine in the theme the author was working in, which is
 * exactly why there were three hundred and fifty of them.
 *
 * Two failure modes are pinned here, and the first one is the more interesting
 * because nothing else caught it.
 *
 * **A doubled opacity.** The migration was done with ordered `sed` rules, and a
 * rule written for the bare token happily matches inside a token that has an
 * opacity suffix: `border-blue-800` → `border-codify-info/60` turns
 * `border-blue-800/60` into `border-codify-info/60/60`. Tailwind compiles
 * neither that class nor its one correct parent, so the element silently loses
 * its border — and `tsc`, the tone-utility table and the whole rest of the suite
 * were all green, because a missing class is not a compile error. It happened
 * twice in one pass. This is the check for it.
 *
 * **A new hardcoded colour.** The migration is only worth anything if it stays
 * done, so the migrated files are held at zero. The two exceptions below are
 * enumerated rather than pattern-matched, which is the point: a ninth exception
 * fails this test instead of quietly joining a list nobody reads.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));

/**
 * Every component in `ui/src`, not a chosen subset.
 *
 * The first version of this file listed sixteen files by name — the settings,
 * stats and audit panels — because that was the migration's scope. That was the
 * wrong shape: a hand-kept list is a list that stops covering new files, and the
 * twenty-odd components that were left behind were the ones a reader would assume
 * were fine. The scope is now the whole tree, which means a new component is
 * covered the day it is written rather than the day somebody remembers it.
 */
function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "node_modules") continue;
      out.push(...sourceFiles(full));
    } else if (/\.tsx?$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

const SRC = path.join(HERE, "..", "src");
const MIGRATED = sourceFiles(SRC).map((f) => path.relative(SRC, f));

/** Tailwind's default palette, at any step, at any opacity, behind any utility. */
const RAW_PALETTE =
  /\b(?:bg|text|border|ring|from|to|via|fill|stroke|shadow|outline|decoration|accent|caret|divide|placeholder)-(?:white|black|slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)(?:-\d{2,3})?(?:\/\d{1,3})?\b/g;

/**
 * The colours that are allowed to stay, and why.
 *
 * `AgentConfigCard.ROLE_ICONS` — the eight agent roles each have their own icon
 * hue. There are eight roles and six status tones, so mapping them onto tones
 * gives two pairs the same colour, which is the collision
 * `tailwind.config.js` records having already cost one pair of features its
 * identity; and it would claim a role *is* a status. Mapping them here was tried
 * and reverted. Giving the contract eight role tokens is the real fix.
 *
 * `SettingsModal`'s scrim — a scrim's job is to darken whatever is behind it.
 * A theme surface colour would go *lighter* on a light theme and stop working as
 * a scrim at all, so this one is deliberately not themed.
 */
const ALLOWED = new Map<string, ReadonlyArray<string>>([
  [
    path.join("components", "AgentConfigCard.tsx"),
    [
      "text-emerald-400",
      "text-cyan-400",
      "text-pink-400",
      "text-purple-400",
      "text-blue-400",
      "text-green-400",
      "text-amber-400",
      "text-indigo-400",
    ],
  ],
  [path.join("components", "SettingsModal.tsx"), ["bg-black/75"]],
  [
    // The one green chip in the theme preview, which says "this is the default"
    // — a fact about the palette being demonstrated, not a status. It was
    // migrated to `--codify-success` by the sweep that produced this file and
    // reverted: the existing `appearancePane.test.ts` pins it at exactly one
    // occurrence and gated on the default theme, so that it cannot leak into
    // `still`, and that pin is load-bearing. This entry exists to say the pin
    // was read and agreed with, not worked around.
    path.join("components", "AppearancePane.tsx"),
    ["bg-green-500"],
  ],
  // The four modal scrims. A scrim's job is to darken whatever is behind it, so
  // a theme surface colour would go *lighter* on a light theme and stop working
  // as a scrim at all. Three of the five live in files the settings migration did
  // not touch, which is why the count grew.
  [path.join("components", "BottomCommandBar.tsx"), ["bg-black/70", "bg-black/70"]],
  [path.join("components", "FailureDiagnosisPanel.tsx"), ["bg-black/70"]],
  [path.join("components", "CommandPalette.tsx"), ["bg-black/75"]],
]);

/** Source with comments stripped, so a class quoted in prose is not one in use. */
function code(file: string): string {
  return readFileSync(path.join(SRC, file), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*$/gm, "");
}

test("the scan covers every component, so a new file is covered the day it is written", () => {
  // The claim this file rests on. If the walk ever stopped finding files, every
  // check below would pass while checking nothing — the failure mode of a test
  // that asserts on the output of its own discovery.
  assert.ok(
    MIGRATED.length > 25,
    `expected the whole of ui/src, walked only ${MIGRATED.length} files`,
  );
  assert.ok(
    MIGRATED.includes(path.join("components", "ChatTimeline.tsx")),
    "the walk is not finding components/",
  );
  assert.ok(
    MIGRATED.includes("appearance.ts"),
    "the walk is not finding top-level modules",
  );
});

test("the migrated panels declare no colour from Tailwind's default palette", () => {
  const found: string[] = [];
  for (const file of MIGRATED) {
    const allowed = new Set(ALLOWED.get(file) ?? []);
    for (const match of code(file).matchAll(RAW_PALETTE)) {
      if (allowed.has(match[0])) continue;
      found.push(`  ${match[0]}  (${file})`);
    }
  }
  assert.deepEqual(
    found,
    [],
    "these classes ignore the active theme — they render the Codify Dark " +
      "colour in all sixteen. Use a `codify-*` token, or add the class to " +
      "ALLOWED above with the reason it cannot follow a theme:\n" +
      found.join("\n"),
  );
});

test("every allowed exception is still used, so the list cannot rot", () => {
  // The other failure of an allowlist: it is write-only. A token that is deleted
  // from the source leaves the exception behind, and the next exception is
  // cheaper than the first because the list already looks maintained. Counted,
  // not just found, so dropping one of two scrims is also a failure.
  for (const [file, classes] of ALLOWED) {
    const src = code(file);
    for (const cls of new Set(classes)) {
      const used = [...src.matchAll(new RegExp(`\\b${cls.replace("/", "\\/")}\\b`, "g"))].length;
      const expected = classes.filter((c) => c === cls).length;
      assert.equal(
        used,
        expected,
        `ALLOWED lists ${cls} ${expected}× for ${file}, but the source has ${used}`,
      );
    }
  }
});

test("no colour utility carries two opacities", () => {
  // The `border-codify-info/60/60` bug, pinned at the class-shape level so it
  // cannot come back through a different token. It survived the whole suite the
  // first time because an uncompilable class name is not an error — it is an
  // element that quietly stopped being styled.
  const doubled: string[] = [];
  for (const file of MIGRATED) {
    for (const match of code(file).matchAll(/\b[a-z-]*[a-z]+-\d{2,3}\/\d{1,3}\/\d{1,3}\b/g)) {
      doubled.push(`  ${match[0]}  (${file})`);
    }
    for (const match of code(file).matchAll(/\bcodify-[a-z-]+\/\d{1,3}\/\d{1,3}\b/g)) {
      doubled.push(`  ${match[0]}  (${file})`);
    }
  }
  assert.deepEqual(
    doubled,
    [],
    "a class with two opacity suffixes compiles to nothing, so the element " +
      "silently loses the style. This is what an over-broad `sed` rule does " +
      "when it matches a bare token inside a suffixed one:\n" + doubled.join("\n"),
  );
});

test("the panels reach the theme's tones, not only its surfaces", () => {
  // The weak version of this migration is to replace the six greys with the
  // three text weights and stop — which themes the *chrome* of a panel and
  // leaves every status inside it the colour it had before. So the claim is
  // about the vocabulary being reachable, and it is deliberately not a per-file
  // claim: a protocol dropdown reports no state at all, and a rule that
  // demanded a status tone of it would be a rule that gets deleted.
  const all = MIGRATED.map(code).join("\n");
  for (const tone of ["info", "success", "warning", "danger"]) {
    assert.ok(
      all.includes(`-codify-${tone}`),
      `no migrated component uses --codify-${tone}, so nothing in the ` +
        "settings, stats or audit panels reports that state through the theme",
    );
  }
});

test("the two panels whose whole job is reporting state use the tones", () => {
  // A per-file claim, on the two files where it is actually true. StatsPanel
  // counts goals, steps and failures; AuditReport is a severity report. If
  // either of them ever themes its background and not its statuses, that is the
  // migration being half-applied and this is where it shows.
  for (const file of [
    path.join("components", "StatsPanel.tsx"),
    path.join("components", "AuditReport.tsx"),
  ]) {
    const src = code(file);
    for (const tone of ["success", "warning", "danger"]) {
      assert.ok(
        src.includes(`-codify-${tone}`),
        `${file} reports no ${tone}, and a severity report that cannot say ` +
          "`danger` is not reporting severity",
      );
    }
  }
});
