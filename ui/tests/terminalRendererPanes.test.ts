/**
 * The terminal and machine panes, mounted, with the canvas renderer chosen: does the real addon take over, and
 * does a terminal that cannot have it stay readable?
 *
 * `terminalRenderer.test.ts` holds the helper with stand-ins. What only a mounted pane can show is that the
 * panes call it at all, and that the *real* `@xterm/addon-canvas` (loaded the way the pane loads it, through a
 * dynamic import of a UMD file) activates over a real xterm and replaces its DOM renderer. jsdom's 2D context is
 * the harness's stub, which draws nothing, so what is observable is the structure xterm builds, not pixels: the
 * canvases appear and the DOM rows go empty. Pixels were looked at in a real browser (`docs/09` §7.1).
 *
 * Every other DOM test starts on the DOM renderer (`dom.ts` seeds it), because they read its rows.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { SEEDED, WIDE, beat, click, withApp } from "./splitHarness.ts";
import type { AppContext, AppOptions } from "./splitHarness.ts";
import { TERMINAL_RENDERER_KEY } from "../src/terminalRenderer.ts";

const choose = (renderer: "canvas" | "dom"): Pick<AppOptions, "localStorage"> => ({
  localStorage: { [TERMINAL_RENDERER_KEY]: renderer },
});

const canvases = (root: Element | null): number => root?.querySelectorAll(".xterm-screen canvas").length ?? 0;
const rowsText = (root: Element | null): string => (root?.querySelector(".xterm-rows")?.textContent ?? "").trim();

/** jsdom has no `createImageBitmap`, which the addon's texture atlas calls from a timer: noise, not a failure. */
function quietAtlas(ctx: AppContext): void {
  (ctx.dom.window as unknown as Record<string, unknown>).createImageBitmap = async () => ({ close() {} });
}

/** What the page warned about while `body` ran. */
async function warnings(body: () => Promise<void>): Promise<string[]> {
  const seen: string[] = [];
  const original = console.warn;
  console.warn = (...args: unknown[]) => void seen.push(args.join(" "));
  try {
    await body();
  } finally {
    console.warn = original;
  }
  return seen;
}

async function openTerminal(ctx: AppContext): Promise<Element | null> {
  quietAtlas(ctx);
  await click(ctx, ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
  await ctx.settle();
  await beat(60);
  await ctx.emit("terminal-output", { id: "term-1", data: "build finished\r\n" });
  await ctx.settle();
  await beat(60);
  return ctx.dom.container.querySelector('section[aria-label="Terminal"]');
}

const terminalShell = { codify_terminal_open: () => "term-1" };

const machineShell = {
  codify_machine_open: () => ({ id: "mach-1", copy_on_write: true, note: null }),
  codify_machine_write: () => null,
  codify_machine_resize: () => null,
  codify_machine_reset: () => ({ id: "mach-1", copy_on_write: true, note: null }),
  codify_machine_close: () => null,
};

async function openMachine(ctx: AppContext): Promise<Element | null> {
  quietAtlas(ctx);
  await click(ctx, ctx.dom.container.querySelector('button[title^="Machine"]') as HTMLElement);
  await ctx.settle();
  await beat(60);
  await ctx.emit("machine-output", { id: "mach-1", data: "build finished\r\n" });
  await ctx.settle();
  await beat(60);
  return ctx.dom.container.querySelector('[data-machine-id="mach-1"]');
}

test("a terminal pane with the canvas renderer chosen is drawn on canvases, not DOM rows", async () => {
  await withApp({ viewport: WIDE, ...SEEDED, ...choose("canvas"), shellAnswers: terminalShell }, async (ctx) => {
    const pane = await openTerminal(ctx);
    assert.ok(canvases(pane) > 0, "the real canvas addon did not take over the pane's terminal");
    assert.equal(rowsText(pane), "", "the DOM renderer is still drawing as well");
    assert.ok(ctx.dom.container.querySelector('[role="alert"]') === null, "the pane reported a failure");
  });
});

test("a terminal pane with the DOM renderer pinned is the terminal it was before: rows, no canvases", async () => {
  await withApp({ viewport: WIDE, ...SEEDED, ...choose("dom"), shellAnswers: terminalShell }, async (ctx) => {
    const pane = await openTerminal(ctx);
    assert.equal(canvases(pane), 0);
    assert.match(rowsText(pane), /build finished/);
  });
});

test("a webview with no 2D canvas leaves the terminal readable, and says why once", async () => {
  await withApp({ viewport: WIDE, ...SEEDED, ...choose("canvas"), shellAnswers: terminalShell }, async (ctx) => {
    (ctx.dom.window.HTMLCanvasElement.prototype as unknown as { getContext: () => null }).getContext = () => null;
    let pane: Element | null = null;
    const said = await warnings(async () => {
      pane = await openTerminal(ctx);
    });
    assert.equal(canvases(pane), 0, "a canvas renderer was attached where no canvas can be had");
    assert.match(rowsText(pane), /build finished/, "the terminal went blank when the canvas could not be had");
    assert.deepEqual(said.filter((m) => /terminal:/.test(m)).map((m) => m.replace(/;.*/, "")), ["terminal: this webview gives no 2D canvas"]);
  });
});

test("a machine pane with the canvas renderer chosen is drawn on canvases too", async () => {
  await withApp({ viewport: WIDE, ...SEEDED, ...choose("canvas"), shellAnswers: machineShell }, async (ctx) => {
    const pane = await openMachine(ctx);
    assert.ok(pane, "the machine's pane is not in the centre column");
    assert.ok(canvases(pane) > 0, "the real canvas addon did not take over the machine's terminal");
    assert.equal(rowsText(pane), "");
  });
});

test("a machine pane with the DOM renderer pinned still shows what the machine said", async () => {
  await withApp({ viewport: WIDE, ...SEEDED, ...choose("dom"), shellAnswers: machineShell }, async (ctx) => {
    const pane = await openMachine(ctx);
    assert.equal(canvases(pane), 0);
    assert.match(rowsText(pane), /build finished/);
  });
});
