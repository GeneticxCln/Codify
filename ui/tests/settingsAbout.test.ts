/**
 * The About tab inside Settings, reached the two ways a person reaches it: by clicking the tab, and
 * by searching the command palette. `aboutPane.test.ts` covers what the pane says; this covers that
 * the modal actually offers it and that the palette opens the modal *on* it.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withApp } = await import("./appHarness.ts");
const React = (await import("react")).default;

test("Settings has an About tab after Appearance, and it shows the shortcuts", async () => {
  await withApp({}, async (ctx) => {
    await ctx.dom.click(ctx.dom.byButton("Settings"));
    await ctx.settle();
    const tabs = [...ctx.dom.container.querySelectorAll("button")]
      .map((b) => (b.textContent ?? "").trim())
      .filter((t) => ["Provider Keys", "Agent Roles", "Audio", "Appearance", "About"].includes(t));
    assert.deepEqual(tabs, ["Provider Keys", "Agent Roles", "Audio", "Appearance", "About"]);
    assert.doesNotMatch(ctx.dom.text(), /Keyboard shortcuts/, "About content showed on another tab");

    await ctx.dom.click(ctx.dom.byButton("About"));
    await ctx.settle();
    assert.match(ctx.dom.text(), /Keyboard shortcuts/);
    assert.match(ctx.dom.text(), /What this app is, what it is running on/, "the header subtitle did not follow the tab");
  });
});

test("searching the palette for About opens Settings on the About tab", async () => {
  await withApp({}, async (ctx) => {
    const press = (key: string, code: string, extra: Record<string, unknown> = {}) =>
      ctx.dom.press(ctx.dom.window.document.body, key, { code, ...extra });
    await press("k", "KeyK", { ctrlKey: true });
    await ctx.settle();
    await ctx.dom.fill(ctx.dom.byLabel("Command palette") as HTMLInputElement, "about");
    await ctx.dom.press(ctx.dom.byLabel("Command palette"), "Enter");
    await ctx.settle();
    await React.act(async () => {});

    assert.match(ctx.dom.text(), /Keyboard shortcuts/, "the palette did not land on About");
  });
});
