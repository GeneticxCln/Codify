/**
 * Which renderer a terminal draws with, and what happens when the better one cannot be had.
 *
 * The properties, in the order a regression would hurt:
 *
 * 1. **A terminal always ends up drawn.** Whatever fails (the addon will not load, will not start, the
 *    pane closed in between) leaves the DOM renderer the terminal already had, and nothing throws into
 *    the pane's effect.
 * 2. **A closed pane is left alone.** Loading finishes after unmount; a terminal that has been
 *    disposed must not be handed an addon.
 * 3. **The pane still imports under `node --test`.** The addon is loaded on first use, not at module
 *    scope, because the canvas addon needs a DOM and the pane's markup tests need the pane importable.
 * 4. **The panes adopt it without yielding** between `open` and the replay.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import type { ITerminalAddon } from "@xterm/xterm";
import {
  attachRenderer,
  canvas2dAvailable,
  canvasAddonFrom,
  DEFAULT_RENDERER,
  readRendererChoice,
  TERMINAL_RENDERER_KEY,
} from "../src/terminalRenderer.ts";

const read = (path: string): string => readFileSync(new URL(path, import.meta.url), "utf8");

/** A terminal that records what it was asked to load, and can be told to refuse. */
function host(refuse?: Error) {
  const loaded: ITerminalAddon[] = [];
  return {
    loaded,
    loadAddon(addon: ITerminalAddon): void {
      if (refuse) throw refuse;
      loaded.push(addon);
    },
  };
}

/** An addon that counts being disposed. */
function addon(disposeThrows = false) {
  const state = { disposed: 0 };
  const instance: ITerminalAddon = {
    activate(): void {},
    dispose(): void {
      state.disposed += 1;
      if (disposeThrows) throw new Error("already gone");
    },
  };
  return { state, instance };
}

const store = (value: string | null): Pick<Storage, "getItem"> => ({ getItem: () => value });

// ── what is chosen ────────────────────────────────────────────────────────────────────────────────

test("canvas is the default, and only the two exact spellings change anything", () => {
  assert.equal(DEFAULT_RENDERER, "canvas");
  assert.equal(readRendererChoice(store(null)), "canvas");
  assert.equal(readRendererChoice(store("canvas")), "canvas");
  assert.equal(readRendererChoice(store("dom")), "dom");
  // A value somebody else wrote is not one this module was written against, whatever it looks like.
  for (const odd of ["", "DOM", "Canvas", " dom", "dom ", "webgl", "true", "0"]) {
    assert.equal(readRendererChoice(store(odd)), "canvas", JSON.stringify(odd));
  }
});

test("a store that cannot be read gives the default, not an error in the pane's effect", () => {
  const blocked: Pick<Storage, "getItem"> = {
    getItem: () => {
      throw new DOMException("blocked", "SecurityError");
    },
  };
  assert.equal(readRendererChoice(blocked), "canvas");
  assert.equal(TERMINAL_RENDERER_KEY, "codify.terminalRenderer");
});

// ── what happens ──────────────────────────────────────────────────────────────────────────────────

test("by default the canvas addon is built once and loaded onto the terminal", async () => {
  const term = host();
  const made = addon();
  let builds = 0;
  const outcome = await attachRenderer(term, {
    choice: "canvas",
    probe: () => true,
    load: async () => {
      builds += 1;
      return made.instance;
    },
  });
  assert.equal(outcome.kind, "canvas");
  assert.equal(builds, 1);
  assert.deepEqual(term.loaded, [made.instance]);
  assert.equal(made.state.disposed, 0, "an addon in use was disposed");
});

test("a pinned DOM renderer neither builds nor loads anything", async () => {
  const term = host();
  let builds = 0;
  const outcome = await attachRenderer(term, {
    choice: "dom",
    load: async () => {
      builds += 1;
      return addon().instance;
    },
  });
  assert.deepEqual(outcome, { kind: "dom", reason: "chosen" });
  assert.equal(builds, 0, "the addon was loaded for a terminal that was told not to use it");
  assert.equal(term.loaded.length, 0);
});

test("an addon that will not load leaves the DOM renderer and says why, without throwing", async () => {
  const term = host();
  const warned: string[] = [];
  const outcome = await attachRenderer(term, {
    choice: "canvas",
    probe: () => true,
    load: async () => {
      throw new Error("chunk failed to load");
    },
    warn: (m) => warned.push(m),
  });
  assert.equal(outcome.kind, "dom");
  assert.match(outcome.reason, /could not be loaded: chunk failed to load/);
  assert.equal(term.loaded.length, 0);
  assert.equal(warned.length, 1);
  assert.match(warned[0], /using the DOM renderer/);
});

test("an addon that will not start is disposed, the DOM renderer stays, and it is reported", async () => {
  // xterm registers an addon before it activates it, so one that throws in `activate` is left behind
  // unless it is disposed here. This is also what jsdom (no 2D canvas) does to the real one.
  const term = host(new Error("Could not get rendering context"));
  const made = addon();
  const warned: string[] = [];
  const outcome = await attachRenderer(term, { choice: "canvas",
    probe: () => true, load: async () => made.instance, warn: (m) => warned.push(m) });
  assert.equal(outcome.kind, "dom");
  assert.match(outcome.reason, /could not start: Could not get rendering context/);
  assert.equal(made.state.disposed, 1, "the half-started addon was left registered");
  assert.equal(warned.length, 1);
});

test("a pane that closed while the addon loaded is not handed one, and the addon is disposed", async () => {
  const term = host();
  const made = addon();
  let closed = false;
  const outcome = await attachRenderer(term, {
    choice: "canvas",
    probe: () => true,
    load: async () => {
      closed = true; // the pane unmounts while the dynamic import is in flight
      return made.instance;
    },
    isDisposed: () => closed,
  });
  assert.equal(outcome.kind, "dom");
  assert.equal(term.loaded.length, 0, "a disposed terminal was given an addon");
  assert.equal(made.state.disposed, 1);
});

test("a dispose that throws, on either cleanup path, still does not throw out of attachRenderer", async () => {
  const stuck = addon(true);
  let closed = false;
  const first = await attachRenderer(host(), {
    choice: "canvas",
    probe: () => true,
    load: async () => {
      closed = true;
      return stuck.instance;
    },
    isDisposed: () => closed,
  });
  assert.equal(first.kind, "dom");
  const second = await attachRenderer(host(new Error("no")), { choice: "canvas",
    probe: () => true, load: async () => addon(true).instance, warn: () => {} });
  assert.equal(second.kind, "dom");
});

// ── whether a canvas can be had at all ────────────────────────────────────────────────────────────

const documentWith = (context: unknown) =>
  ({ createElement: () => ({ getContext: () => context }) }) as unknown as Pick<Document, "createElement">;

test("a webview that gives no 2D context keeps the DOM renderer and never loads the addon", async () => {
  const term = host();
  let builds = 0;
  const warned: string[] = [];
  const outcome = await attachRenderer(term, {
    choice: "canvas",
    probe: () => false,
    load: async () => {
      builds += 1;
      return addon().instance;
    },
    warn: (m) => warned.push(m),
  });
  assert.equal(outcome.kind, "dom");
  assert.match(outcome.reason, /no 2D canvas/);
  assert.equal(builds, 0, "the addon was loaded where there is nothing for it to draw on");
  assert.equal(term.loaded.length, 0);
  assert.equal(warned.length, 1);
});

test("the probe says yes only for a real context, and never throws", () => {
  assert.equal(canvas2dAvailable(documentWith({ fillRect() {} })), true);
  for (const nothing of [null, undefined, false, 0, ""]) assert.equal(canvas2dAvailable(documentWith(nothing)), false);
  assert.equal(canvas2dAvailable({ createElement: () => { throw new Error("no document"); } }), false);
  assert.equal(canvas2dAvailable({ createElement: () => ({}) as unknown as HTMLElement }), false, "an element with no getContext");
  // No document at all (this file runs in plain node): the answer is no, not an exception.
  assert.equal(canvas2dAvailable(), false);
});

// ── the module shapes a bundler can hand back ────────────────────────────────────────────────────

test("the constructor is found whether the bundler gives the named export or only `default`", () => {
  class Named {}
  class Wrapped {}
  // Named export: what the module namespace looks like where the UMD's names are detected.
  assert.equal(canvasAddonFrom({ CanvasAddon: Named }), Named);
  // Only `default`, holding module.exports: what esbuild's bundle gave, and what broke the first version.
  assert.equal(canvasAddonFrom({ default: { CanvasAddon: Wrapped } }), Wrapped);
  // Both present: the named one wins, it is the one a conforming namespace would carry.
  assert.equal(canvasAddonFrom({ CanvasAddon: Named, default: { CanvasAddon: Wrapped } }), Named);
});

test("a module with no constructor is an error `attachRenderer` reports, not an uncaught `new undefined`", async () => {
  for (const bad of [undefined, null, {}, { default: {} }, { CanvasAddon: "nope" }, { default: { CanvasAddon: 3 } }]) {
    assert.throws(() => canvasAddonFrom(bad), /no CanvasAddon to construct/, JSON.stringify(bad));
  }
  // Through the real entry point, with a loader that fails the way a wrong-shaped module does.
  const warned: string[] = [];
  const outcome = await attachRenderer(host(), {
    choice: "canvas",
    probe: () => true,
    load: async () => new (canvasAddonFrom({ default: {} }))(),
    warn: (m) => warned.push(m),
  });
  assert.equal(outcome.kind, "dom");
  assert.match(outcome.reason, /no CanvasAddon to construct/);
  assert.equal(warned.length, 1);
});

// ── the contracts the pane's tests need ───────────────────────────────────────────────────────────

test("the addon is imported on first use and never at module scope, so the panes stay importable by node", () => {
  const source = read("../src/terminalRenderer.ts");
  const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  assert.doesNotMatch(code, /^import\s+(?!type\b)[^;]*@xterm/m, "a value import of xterm at module scope");
  assert.match(code, /await import\("@xterm\/addon-canvas"\)/);
});

for (const [name, path] of [
  ["TerminalPane", "../src/components/TerminalPane.tsx"],
  ["MachinePane", "../src/components/MachinePane.tsx"],
] as const) {
  test(`${name} attaches the renderer right after open, without awaiting it`, () => {
    const source = read(path);
    assert.match(source, /import \{ attachRenderer \} from "\.\.\/terminalRenderer"/);
    const open = source.indexOf("term.open(hostRef.current)");
    const attach = source.indexOf("void attachRenderer(term, { isDisposed: () => disposed })");
    assert.ok(open > 0, "the pane no longer opens its terminal where this test looks");
    assert.ok(attach > open, "the renderer is attached before the terminal is opened (the addon needs the element open makes)");
    assert.doesNotMatch(source, /await attachRenderer/, "awaiting it would put a yield between the open and the replay");
    // And nothing between `open` and the attach yields either.
    assert.doesNotMatch(source.slice(open, attach), /\bawait\b/);
  });
}

test("the dependency is declared, and matches the xterm major it draws for", () => {
  const manifest = JSON.parse(read("../package.json")) as { dependencies: Record<string, string> };
  assert.match(manifest.dependencies["@xterm/addon-canvas"] ?? "", /^\^0\.7\./);
  assert.match(manifest.dependencies["@xterm/xterm"] ?? "", /^\^5\./);
});
