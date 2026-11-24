/**
 * The wordmark's shimmer waits for the shell's render watchdog to finish (`renderCheck.ts`, `lib.rs`).
 *
 * A fixed ten-second delay put the nine-second sweep inside the watchdog's thirty, and on a CPU-composited window
 * the watchdog read the sweep as starvation and stopped the theme's backdrop for the session. The CSS half (held
 * while `data-render-checked` is not "true") is `wordmark.test.ts`; this is the App half: what sets the attribute.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withApp } = await import("./appHarness.ts");
const { RENDER_CHECK_SETTLED_COMMAND, RENDER_CHECK_SETTLED_EVENT } = await import("../src/renderCheck.ts");

const checked = (ctx: { dom: { window: Window } }): string | undefined =>
  ctx.dom.window.document.documentElement.dataset.renderChecked;

test("while the watchdog is still judging, the shimmer is held, and the shell's event releases it", async () => {
  await withApp({ shellAnswers: { [RENDER_CHECK_SETTLED_COMMAND]: false } }, async (ctx) => {
    await ctx.settle();
    assert.equal(checked(ctx), "false", "the shimmer can start while the watchdog is measuring");
    assert.ok(ctx.shell.calls.includes(RENDER_CHECK_SETTLED_COMMAND), "the page never asked whether the check was done");
    await ctx.emit(RENDER_CHECK_SETTLED_EVENT, null);
    await ctx.settle();
    assert.equal(checked(ctx), "true", "the watchdog finished and the shimmer stayed held");
  });
});

test("a page that loads after the watchdog finished asks, and starts at once", async () => {
  await withApp({ shellAnswers: { [RENDER_CHECK_SETTLED_COMMAND]: true } }, async (ctx) => {
    await ctx.settle();
    assert.equal(checked(ctx), "true", "a reload after the check would never shimmer");
  });
});

test("a shell that predates the command is treated as settled, not as waiting forever", async () => {
  await withApp({ shellFails: { [RENDER_CHECK_SETTLED_COMMAND]: "unknown command" } }, async (ctx) => {
    await ctx.settle();
    assert.equal(checked(ctx), "true");
  });
});
