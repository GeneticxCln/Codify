/**
 * The editor takes its colours from the same variables as the rest of the window, and only from the ones that mean something.
 *
 * Like the terminal, the editor paints itself (CodeMirror writes its own styles), so the runtime theme layer in `index.css`
 * does not reach it by class names. Unlike the terminal it does not need concrete colours: its theme is CSS, so it can say
 * `rgb(var(--codify-bg-rgb))` and follow a theme change with no code at all. That makes the rule worth pinning: every colour
 * it names is a variable the theme layer manages, no colour is typed in as a literal, and text colours come only from the
 * tokens that are measured for contrast (`*-ink`, `primary`, `secondary`, `muted`), so a syntax colour is as legible in
 * every theme as the status text the app already shows.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { UI, SYNTAX, rgb } = await import("../src/editorTheme.ts");
const { MANAGED_VARS } = await import("../src/appearance.ts");

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, "..", "src");
const source = (name: string): string => readFileSync(path.join(SRC, name), "utf8");
const css = readFileSync(path.join(SRC, "index.css"), "utf8");

const allVars = (): string[] => [...Object.values(UI), ...Object.values(SYNTAX)];

test("a colour is written as a variable with an optional alpha, in the form index.css writes it", () => {
  assert.equal(rgb("--codify-bg-rgb"), "rgb(var(--codify-bg-rgb))");
  assert.equal(rgb("--codify-info-rgb", 0.3), "rgb(var(--codify-info-rgb) / 0.3)");
});

test("every variable the editor names is one the theme layer manages or index.css declares", () => {
  for (const name of allVars()) {
    const known = (MANAGED_VARS as readonly string[]).includes(name) || css.includes(`${name}:`);
    assert.ok(known, `${name} is not a variable this app defines, so a theme could never change it`);
  }
});

test("every variable is an rgb triple, which is what the alpha form needs", () => {
  for (const name of allVars()) assert.match(name, /^--codify-[a-z-]+-rgb$/, name);
});

test("syntax colours come only from the tokens that are measured for contrast on the background", () => {
  const allowed = /^--codify-(accent|info|success|warning|danger|design|knowledge)-ink-rgb$|^--codify-(primary|secondary|muted)-rgb$/;
  for (const [role, name] of Object.entries(SYNTAX)) assert.match(name, allowed, `${role} uses ${name}`);
});

test("the editor's own source types no colour: no hex, no rgb() literal, no named colour", () => {
  for (const file of ["editorTheme.ts", "editorMount.ts"]) {
    const text = source(file).replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
    assert.doesNotMatch(text, /#[0-9a-fA-F]{3,8}\b/, `${file} has a hex literal`);
    assert.doesNotMatch(text, /\brgba?\(\s*\d/, `${file} has a literal rgb()`);
    assert.doesNotMatch(text, /["'`](?:white|black|red|green|blue|gray|grey|orange|yellow)["'`]/, `${file} names a colour`);
  }
});

test("the roles the theme needs are all there, so nothing falls back to CodeMirror's own light theme", () => {
  for (const role of ["background", "text", "gutter", "gutterText", "border", "caret", "selection", "activeLine", "assistant"]) {
    assert.ok(role in UI, role);
  }
  for (const role of ["keyword", "string", "number", "comment", "function", "type", "property", "invalid"]) {
    assert.ok(role in SYNTAX, role);
  }
});
