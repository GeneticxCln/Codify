/**
 * Whether a terminal pane takes the keyboard when it appears.
 *
 * A pane used to focus its terminal the moment xterm was ready, because the tab strip had just taken the focus and a
 * terminal the user cannot type into until they click it is the classic complaint. With two panes showing, a pane that
 * mounts *unfocused* (the second half of a split, or one that comes back when the window widens) must not take the
 * keyboard from the pane the person is using, so `autoFocus` says which it is.
 *
 * The pane is mounted alone, over the harness's fake shell, so a real xterm loads and the answer is read off the
 * document's active element.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext } from "./appHarness.ts";

const { withApp } = await import("./appHarness.ts");
const React = (await import("react")).default;
const h = React.createElement;
const { TerminalPane } = await import("../src/components/TerminalPane.tsx");

const VIEWPORT = { width: 900, height: 600 };

const typingTarget = (ctx: AppContext, id: string): Element | null =>
  ctx.dom.container.querySelector(`[data-terminal-id="${id}"] textarea`);

async function mountPane(ctx: AppContext, id: string, props: { autoFocus?: boolean }): Promise<void> {
  await ctx.dom.render(h(TerminalPane, { terminalId: id, workspaceId: "ws-a", ...props }));
  await ctx.settle();
}

test("a pane takes the keyboard when it appears, unless told not to", async () => {
  await withApp({ viewport: VIEWPORT }, async (ctx) => {
    await mountPane(ctx, "focus-default", {});
    const input = typingTarget(ctx, "focus-default");
    assert.ok(input, "xterm never opened");
    assert.ok(ctx.dom.window.document.activeElement === input, "the new terminal did not take the keyboard");
  });
});

test("a pane that mounts unfocused leaves the keyboard where it was", async () => {
  await withApp({ viewport: VIEWPORT }, async (ctx) => {
    const elsewhere = ctx.dom.window.document.createElement("input");
    ctx.dom.window.document.body.appendChild(elsewhere);
    elsewhere.focus();
    assert.ok(ctx.dom.window.document.activeElement === elsewhere);

    await mountPane(ctx, "focus-off", { autoFocus: false });
    const input = typingTarget(ctx, "focus-off");
    assert.ok(input, "xterm never opened, so this proved nothing");
    assert.ok(ctx.dom.window.document.activeElement === elsewhere, "a pane that was not asked to focus took the keyboard");
  });
});

test("clicking the pane still focuses it, whatever it started with", async () => {
  await withApp({ viewport: VIEWPORT }, async (ctx) => {
    await mountPane(ctx, "focus-click", { autoFocus: false });
    const input = typingTarget(ctx, "focus-click");
    assert.ok(input);
    const host = ctx.dom.container.querySelector('[data-terminal-id="focus-click"] div.flex-1') as HTMLElement;
    assert.ok(host, "no host to click");
    await ctx.dom.click(host);
    assert.ok(ctx.dom.window.document.activeElement === input, "clicking the terminal did not focus it");
  });
});
