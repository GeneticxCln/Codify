/**
 * The terminal is the one surface the runtime theme layer cannot reach.
 *
 * xterm.js paints its own screen and takes concrete colour strings, so
 * `ui/src/index.css` never applied to it — the shell was `#0d1117` on
 * `#c9d1d9`, the *default theme's* hexes, in all nine themes. That is why the
 * terminal was the one grey-blue box in an OLED app with phosphor behind it.
 *
 * `ui/src/terminalTheme.ts` is the fix, and the mapping is a function of a
 * lookup, so it is pinned here without a DOM: which variable paints which
 * terminal colour, that a theme's tones actually arrive, and that a variable
 * which is not set costs one colour rather than the whole screen.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { THEMES, themeById, type ManagedVar } from "../src/appearance.ts";
import {
  terminalColorSource,
  xtermTheme,
  type XtermTheme,
} from "../src/terminalTheme.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const KEYS: Array<keyof XtermTheme> = [
  "background",
  "foreground",
  "cursor",
  "selectionBackground",
  "black",
  "red",
  "green",
  "yellow",
  "blue",
  "magenta",
  "cyan",
  "white",
  "brightBlack",
  "brightRed",
  "brightGreen",
  "brightYellow",
  "brightBlue",
  "brightMagenta",
  "brightCyan",
  "brightWhite",
];

test("every terminal colour is painted by a variable the app publishes", () => {
  // A colour mapped to nothing would fall back to the default theme and be
  // invisible: correct in Codify Dark, wrong in the other eight, and wrong
  // quietly. The check is that each source is a `--codify-` name, because that
  // is the namespace `applyTheme` clears and refills.
  for (const key of KEYS) {
    const source = terminalColorSource(key);
    assert.match(
      source,
      /^--codify-[a-z-]+$/,
      `${key} is painted by ${source}, which is not an app variable`,
    );
  }
});

test("a theme's tones reach the terminal it belongs to", () => {
  // The end-to-end pin, and the reason this is worth a test: the terminal's
  // `red` is the OLED theme's `--codify-danger`, not Tailwind red and not the
  // default theme's `#f85149`.
  for (const theme of THEMES) {
    const painted = xtermTheme((n) => theme.tokens[n as ManagedVar] ?? "");
    assert.equal(
      painted.red,
      theme.tokens["--codify-danger"],
      `${theme.id}: the terminal's red is not the theme's danger`,
    );
    assert.equal(
      painted.green,
      theme.tokens["--codify-success"],
      `${theme.id}: the terminal's green is not the theme's success`,
    );
    assert.equal(
      painted.background,
      theme.tokens["--codify-bg"],
      `${theme.id}: the terminal's background is not the theme's background`,
    );
    assert.equal(
      painted.cursor,
      theme.tokens["--codify-primary"],
      `${theme.id}: the terminal's cursor is not the theme's primary text`,
    );
  }
});

test("the two themes the user would notice most are actually different", () => {
  // A mapping that resolved everything to one colour would satisfy the two
  // tests above for any single theme. The pair is what catches it: OLED CMatrix
  // and Cyberpunk Neon share no hue at all, so a terminal painted from either
  // cannot come out the same.
  const oled = themeById("cmatrix-oled");
  const neonTheme = themeById("cyberpunk-neon");
  const phosphor = xtermTheme((n) => oled.tokens[n as ManagedVar] ?? "");
  const neon = xtermTheme((n) => neonTheme.tokens[n as ManagedVar] ?? "");
  assert.notEqual(phosphor.background, neon.background);
  assert.notEqual(phosphor.red, neon.red);
  assert.notEqual(phosphor.magenta, neon.magenta);
});

test("a variable that is not set costs one colour, not the whole screen", () => {
  // `getComputedStyle` returns an empty string for a property nothing declared,
  // and a terminal with a blank background is a terminal nobody can read. The
  // fallbacks are per colour, so the other nineteen still come from the
  // document.
  const document_: Record<string, string> = {
    "--codify-bg": "#020806",
    "--codify-danger": "#ff5f56",
  };
  const painted = xtermTheme((name) => document_[name] ?? "");
  assert.equal(painted.background, "#020806", "the one set variable is used");
  assert.equal(painted.red, "#ff5f56", "the one set variable is used");
  assert.equal(painted.green, "#3fb950", "the unset ones fall back");
  assert.equal(painted.foreground, "#c9d1d9");
});

test("no colour comes back empty, whatever the document says", () => {
  // A blank string reaching xterm is the failure this whole file exists to
  // prevent, and the guard is that the fallback is applied per colour rather
  // than trusting the caller to have a complete document.
  const painted = xtermTheme(() => "   ");
  for (const key of KEYS) {
    assert.ok(
      painted[key].trim().length > 0,
      `${key} came back blank with an empty document`,
    );
  }
});

test("the pane no longer carries its own colours", () => {
  // The regression is a literal, not a missing feature: someone re-types
  // `background: "#0d1117"` in the component and every test above still passes,
  // because they all test the mapping rather than the call site.
  const source = readFileSync(
    path.join(HERE, "..", "src", "components", "TerminalPane.tsx"),
    "utf8",
  );
  const code = source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*$/gm, "");
  const hexes = code.match(/#[0-9a-f]{6}\b/gi);
  assert.deepEqual(
    hexes,
    null,
    `TerminalPane.tsx hard-codes ${(hexes ?? []).join(", ")} — the theme is ` +
      "read from the document, not written here",
  );
  assert.match(
    code,
    /theme:\s*xtermThemeFromDocument\(\)/,
    "the terminal must be constructed from the live theme",
  );
});
