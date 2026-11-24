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
const { RENDER_CHECK_QUIET_COMMAND, RENDER_CHECK_SETTLED_COMMAND, RENDER_CHECK_SETTLED_EVENT } = await import(
  "../src/renderCheck.ts"
);
const { CMATRIX_OLED, CYBERPUNK_NEON } = await import("../src/appearance.ts");

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

// ── a person's own theme switch is not the window starving ─────────────────
//
// Switching themes inside the first thirty seconds mounted a new backdrop and repainted the window past a third of a
// core for a few seconds; the watchdog read the person's clicks as starvation and capped every backdrop at 2-10 FPS
// until restart. While the watchdog is judging, each theme application tells the shell to leave the next seconds out.

async function clickTheme(ctx: { dom: any; settle: () => Promise<void> }, label: string): Promise<void> {
  const row = [...ctx.dom.container.querySelectorAll('[role="radiogroup"][aria-label="Theme"] [role="radio"]')].find(
    (el: Element) => (el.textContent ?? "").includes(label),
  );
  assert.ok(row, `no theme row for ${label}`);
  await ctx.dom.click(row);
  await ctx.settle();
}

const quietCalls = (ctx: { shell: { calls: string[] } }): number =>
  ctx.shell.calls.filter((c) => c === RENDER_CHECK_QUIET_COMMAND).length;

test("while the watchdog judges, a theme switch tells the shell to leave those seconds out", async () => {
  await withApp(
    { localStorage: { "codify.theme": CMATRIX_OLED.id }, shellAnswers: { [RENDER_CHECK_SETTLED_COMMAND]: false } },
    async (ctx) => {
      await ctx.settle();
      await ctx.dom.click(ctx.dom.byButton("Settings"));
      await ctx.settle();
      await ctx.dom.click(ctx.dom.byButton("Appearance"));
      await ctx.settle();
      assert.equal(quietCalls(ctx), 0, "the shell was told to look away before anyone acted");

      await clickTheme(ctx, CYBERPUNK_NEON.label);
      assert.equal(quietCalls(ctx), 1, "a theme switch during the check was judged as the window starving");
      await clickTheme(ctx, CMATRIX_OLED.label);
      assert.equal(quietCalls(ctx), 2);

      // Once the watchdog has finished there is nothing to keep out of it, and nothing is sent.
      await ctx.emit(RENDER_CHECK_SETTLED_EVENT, null);
      await ctx.settle();
      await clickTheme(ctx, CYBERPUNK_NEON.label);
      assert.equal(quietCalls(ctx), 2, "the shell was still being told after its check had settled");
    },
  );
});
