/**
 * Hiding and showing the left panel, through the whole App.
 *
 * The panel (threads, New Project, Browser, Terminal, Settings) is a flex sibling of the centre
 * column, so hiding it is what gives a transcript, a terminal or a browser pane the room: the
 * browser's native webview is told its new rectangle by the pane's own `ResizeObserver`, and that
 * only fires if the column actually changes size. These tests check the structure that makes that
 * true, the button and key that drive it, and that the choice survives a restart.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext } from "./appHarness.ts";

const { withApp } = await import("./appHarness.ts");
const React = (await import("react")).default;

const SIDEBAR_KEY = "codify.sidebar";

const panel = (ctx: AppContext): Element | null => ctx.dom.container.querySelector("main > nav");
const hasPanel = (ctx: AppContext): boolean => panel(ctx) !== null;
const toggle = (ctx: AppContext): HTMLElement =>
  (ctx.dom.allByLabel("Hide left panel")[0] ?? ctx.dom.byLabel("Show left panel"));

/** Ctrl+<key> on the document, returning whether the app claimed it (cancelled the default). */
async function ctrl(ctx: AppContext, key: string, code: string, extra: Record<string, unknown> = {}): Promise<boolean> {
  const event = new ctx.dom.window.KeyboardEvent("keydown", {
    key, code, ctrlKey: true, bubbles: true, cancelable: true, ...extra,
  });
  await React.act(async () => {
    ctx.dom.window.document.body.dispatchEvent(event);
  });
  return event.defaultPrevented;
}

test("the panel is open by default, and the button in the header hides and shows it", async () => {
  await withApp({}, async (ctx) => {
    assert.ok(hasPanel(ctx), "the left panel is not there by default");
    const button = toggle(ctx);
    assert.equal(button.getAttribute("aria-label"), "Hide left panel");
    assert.equal(button.getAttribute("aria-pressed"), "false");
    assert.ok(button.closest("header") !== null, "the button is not in the header, where it can never hide itself");

    await ctx.dom.click(button);
    assert.ok(!hasPanel(ctx), "hiding left the panel on screen");
    assert.equal(toggle(ctx).getAttribute("aria-label"), "Show left panel");
    assert.equal(toggle(ctx).getAttribute("aria-pressed"), "true");

    await ctx.dom.click(toggle(ctx));
    assert.ok(hasPanel(ctx), "showing did not bring it back");
  });
});

test("the button reads as a button, and the panel is capped so a big UI scale cannot swallow a narrow window", async () => {
  await withApp({}, async (ctx) => {
    // Rendered at 125% it was a bare glyph that looked like nothing was there. The subtle tone gives it
    // the same border and fill as Stats and History beside it.
    const classes = toggle(ctx).className.split(/\s+/);
    assert.ok(classes.includes("border-codify-border") && classes.includes("bg-codify-raised"), `the toggle has no visible button chrome: ${classes.join(" ")}`);
    // 15rem grows with the UI scale: 420px at 175%, nearly half of a 900px window. The cap is what
    // makes the panel give way first (measured in a real render; jsdom has no layout to measure).
    assert.ok(
      (panel(ctx) as Element).classList.contains("max-w-[35%]"),
      "the left panel has no width cap, so at 175% it takes nearly half of a narrow window",
    );
  });
});

test("the button is beside New Tab, outside the tab strip, and the header gains no Browser or Terminal control", async () => {
  await withApp({}, async (ctx) => {
    const header = ctx.dom.container.querySelector("header") as HTMLElement;
    const strip = header.querySelector('[role="tablist"]') as HTMLElement;
    assert.ok(!strip.contains(toggle(ctx)), "the button is inside the strip it is not a tab of");
    // Document order: the button precedes the strip, so it does not move as tabs open.
    assert.ok(
      (toggle(ctx).compareDocumentPosition(strip) & ctx.dom.window.Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
      "the button follows the strip, so its place would change with the number of open tabs",
    );
    // `appWiring.test.ts` keeps Browser and Terminal in the panel; the toggle must not be mistaken for them.
    const titles = [...header.querySelectorAll("button")].map((b) => b.getAttribute("title") ?? "");
    assert.deepEqual(titles.filter((t) => /^(Browser|Terminal)/.test(t)), []);
  });
});

test("hiding the panel gives the centre column the room: it is a sibling, not an overlay", async () => {
  await withApp({}, async (ctx) => {
    const main = ctx.dom.container.querySelector("main") as HTMLElement;
    const before = main.children.length;
    await ctx.dom.click(toggle(ctx));
    assert.equal(main.children.length, before - 1, "hiding did not remove the panel from the layout");
    assert.ok(
      (main.firstElementChild as HTMLElement).classList.contains("flex-1"),
      "the centre column is not first in the row, so it cannot have taken the panel's width",
    );
  });
});

test("Ctrl+B hides and shows the panel, and claims the key", async () => {
  await withApp({}, async (ctx) => {
    assert.equal(await ctrl(ctx, "b", "KeyB"), true, "the key was not claimed");
    assert.ok(!hasPanel(ctx));
    assert.equal(await ctrl(ctx, "b", "KeyB"), true);
    assert.ok(hasPanel(ctx));
    // A held key is one press.
    await ctrl(ctx, "b", "KeyB", { repeat: true });
    assert.ok(hasPanel(ctx), "a held key hid the panel");
  });
});

test("the choice is remembered, and a remembered 'closed' starts closed", async () => {
  await withApp({}, async (ctx) => {
    assert.equal(ctx.dom.window.localStorage.getItem(SIDEBAR_KEY), "open");
    await ctx.dom.click(toggle(ctx));
    assert.equal(ctx.dom.window.localStorage.getItem(SIDEBAR_KEY), "closed");
  });
  await withApp({ localStorage: { [SIDEBAR_KEY]: "closed" } }, async (ctx) => {
    assert.ok(!hasPanel(ctx), "a panel hidden last time came back on restart");
    assert.equal(toggle(ctx).getAttribute("aria-label"), "Show left panel");
  });
});

test("anything but 'closed' in storage is an open panel", async () => {
  for (const junk of ["", "false", "0", "hidden", "CLOSED", "null"]) {
    await withApp({ localStorage: { [SIDEBAR_KEY]: junk } }, async (ctx) => {
      assert.ok(hasPanel(ctx), `${JSON.stringify(junk)} in storage hid the panel`);
    });
  }
});

test("with a terminal in front, Ctrl+B is the terminal's own (tmux, readline) and passes through", async () => {
  await withApp({}, async (ctx) => {
    // xterm's bundle reaches for `self`, which Node's module scope lacks and jsdom has inside the window.
    if (!(globalThis as Record<string, unknown>).self) {
      (globalThis as Record<string, unknown>).self = ctx.dom.window;
    }
    const terminal = ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement;
    await ctx.dom.click(terminal);
    await ctx.settle();
    assert.ok(ctx.dom.container.querySelector('[role="tab"][aria-label*="erminal"], [role="tab"]') !== null);

    assert.equal(await ctrl(ctx, "b", "KeyB"), false, "Ctrl+B was taken from the shell's own use of it");
    assert.ok(hasPanel(ctx), "the panel hid under a focused terminal");
    // The button still works with the pointer, so the panel is never out of reach.
    await ctx.dom.click(toggle(ctx));
    assert.ok(!hasPanel(ctx));
  });
});
