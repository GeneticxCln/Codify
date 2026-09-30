/**
 * Every test that reads the app's source as text, and the reason it has to.
 *
 * A test that greps `App.tsx` for `onClick={handleX}` proves the words are there,
 * not that pressing the control does anything; every one of the bugs found in
 * the 2026-09-29 audit (a motion flag read the wrong way round, a layer that ate
 * every click, queues nothing ever filled, a stale dispatch-table literal) had a
 * source-text test that was green throughout. `appHarness.ts` mounts the whole
 * App against a recording fake engine and shell, so "does it work" no longer
 * needs a regex.
 *
 * This does not ban them: some rules really are about text (a palette with no
 * hard-coded hex, the one spelling of a placeholder, a Rust constant matching a
 * TypeScript one). It used to be a count that could only be raised or lowered,
 * and a count says how many and never which, so it drifted: it read 27 for a
 * tree that held 23. It is now a list. A test file that starts reading source is
 * a failure until it is named here with its reason, and a file that stops is a
 * failure until its line is deleted, so the list cannot go stale in either
 * direction and every entry is a sentence someone had to write.
 *
 * Two kinds of entry, because they are not the same debt:
 *
 *  - `text`: the claim is about what is written, and no mounted test could
 *    observe it (a token in a stylesheet, the bytes of a GIF, a Rust constant).
 *  - `unconverted`: the claim is about behaviour or about where something is
 *    mounted, held by a regex for want of a mounted test. Converting one means
 *    deleting its line, which is the whole reward: `harness` tests exist for
 *    each of these to be rewritten against (see `appWiring.test.ts`).
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

interface Reason {
  kind: "text" | "unconverted";
  why: string;
}

const TEXT_READERS: Record<string, Reason> = {
  // ── styling that is only observable as written ──────────────────────────────
  "appearance.test.ts": {
    kind: "text",
    why: "The themes module's contract with index.css: the custom properties a theme must define are declared in the stylesheet, so that is where they are checked.",
  },
  "appearancePane.test.ts": {
    kind: "text",
    why: "The pane's arrangement (grid tracks, breakpoints) is a claim about class names; jsdom has no layout engine, so the classes are the only thing observable.",
  },
  "backdropEdgeFade.test.ts": {
    kind: "text",
    why: "The edge fade is a CSS mask applied to the canvas element; with no layout in jsdom it can only be checked where it is written.",
  },
  "contrast.test.ts": {
    kind: "text",
    why: "By design it reads every class string and index.css to compute contrast for each pair the app paints, so a new badge cannot be left out of a hand-written table.",
  },
  "nonTextContrast.test.ts": {
    kind: "text",
    why: "The non-text half of contrast.test.ts: it measures the tokens as index.css writes them.",
  },
  "hardcodedPalette.test.ts": {
    kind: "text",
    why: "A rule about what is written: no Tailwind default-palette class in the settings, stats and audit panels.",
  },
  "toneUtilities.test.ts": {
    kind: "text",
    why: "Proves the hand-written runtime utility table in index.css covers every themed utility the source uses, which is a comparison between two texts.",
  },
  "transcriptPalette.test.ts": {
    kind: "text",
    why: "A rule about what is written in the transcript's class strings: no saturated hue that belongs to no theme.",
  },
  "terminalTheme.test.ts": {
    kind: "text",
    why: "The terminal pane must not re-type a hex literal; the mapping is tested by calling it, the absence of a literal at the call site can only be read.",
  },
  "wordmark.test.ts": {
    kind: "text",
    why: "The wordmark's weight and its reading of the theme live in index.css and Wordmark.tsx as written; jsdom paints nothing to measure.",
  },
  "logo.test.ts": {
    kind: "text",
    why: "Walks the bytes of the checked-in GIF and icon and the CSS that sizes them: an asset is checked as bytes.",
  },

  // ── cross-language and cross-file contracts no type system spans ───────────
  "invokeArgs.test.ts": {
    kind: "text",
    why: "The argument names api.ts sends must match the Rust command parameters; the two languages share nothing but text.",
  },
  "motionPreference.test.ts": {
    kind: "text",
    why: "One test reads src-tauri/src/lib.rs: the Rust event constant must equal the TypeScript one. The App half of the contract is mounted.",
  },
  "staleAuthBanner.test.ts": {
    kind: "text",
    why: "One test reads the Makefile: the command the banner tells a person to run must be a target that exists.",
  },
  "sourceRules.test.ts": {
    kind: "text",
    why: "Rules about the source's own text by definition: comments that must not describe a shape the code no longer has, the one-spelling rules, and the assertion hazards.",
  },

  // ── behaviour held by a regex, for want of a mounted test ─────────────────
  "atmosphere.test.ts": {
    kind: "unconverted",
    why: "Where the backdrop is mounted in App and the shared clock's structure. The loops' behaviour is mounted in atmosphereMotion.test.ts; the mount position is not yet.",
  },
  "rainBackdrop.test.ts": {
    kind: "unconverted",
    why: "The rain's position in the tree (outside the transcript) is read from App.tsx and ChatTimeline.tsx. It could be asserted on a mounted App and has not been.",
  },
  "canvasRecovery.test.ts": {
    kind: "unconverted",
    why: "Both loops must listen for contextlost and cap what they log. jsdom has no GPU context to lose, so this needs a fake context that can be lost; it has not been written.",
  },
};

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SELF = path.basename(fileURLToPath(import.meta.url));

/** Test files that read something under `src/` as text (or import from it and read a file). */
function textReaders(): string[] {
  return readdirSync(HERE)
    .filter((f) => f.endsWith(".test.ts") && f !== SELF)
    .filter((f) => {
      const text = readFileSync(path.join(HERE, f), "utf8");
      return /readFileSync\(/.test(text) && /\.\.\/src\/|"\.\.", "src"|'\.\.', 'src'/.test(text);
    })
    .sort();
}

test("every test that reads the app's source as text is named here, with its reason", () => {
  const unlisted = textReaders().filter((f) => !(f in TEXT_READERS));
  assert.deepEqual(
    unlisted,
    [],
    `${unlisted.join(", ")} read(s) src/ as text and is not in TEXT_READERS. Test the behaviour ` +
      "instead: mount the App with tests/appHarness.ts (see appWiring.test.ts) and check what the " +
      "app asked the engine or shell to do. If the rule really is about text, add the file to " +
      "TEXT_READERS with kind 'text' and the reason no mounted test could hold it.",
  );
});

test("the list names no file that has stopped reading source as text", () => {
  const readers = new Set(textReaders());
  const stale = Object.keys(TEXT_READERS).filter((f) => !readers.has(f));
  assert.deepEqual(
    stale,
    [],
    `${stale.join(", ")} no longer read(s) src/ as text (converted, renamed or deleted). ` +
      "Delete the line from TEXT_READERS, so the list stays true.",
  );
});

test("every entry says why, in a sentence", () => {
  for (const [file, { kind, why }] of Object.entries(TEXT_READERS)) {
    assert.ok(kind === "text" || kind === "unconverted", `${file} has an unknown kind: ${kind}`);
    assert.ok(why.trim().length >= 40, `${file} has no real reason: ${JSON.stringify(why)}`);
    assert.ok(/[.]$/.test(why.trim()), `${file}'s reason is not a sentence: ${JSON.stringify(why)}`);
  }
});
