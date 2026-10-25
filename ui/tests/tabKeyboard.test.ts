/**
 * Moving between tabs with the keyboard.
 *
 * Only the active tab is in the Tab order (the roving tabindex a tablist uses), so the others are reachable only if
 * the arrow keys move between them. They did nothing: Ctrl+1..9 was the one keyboard route, and a strip's header
 * comment promised that every tab is focusable.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { Tab } from "../src/tabs.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;

const tab = (id: string, title: string): Tab => ({ id, kind: "chat", title, conversationId: id });

async function mount(dom: Dom, activeId: string): Promise<string[]> {
  const { TabBar } = await import("../src/components/TabBar.tsx");
  const focused: string[] = [];
  await dom.render(
    React.createElement(TabBar, {
      tabs: [tab("a", "one"), tab("b", "two"), tab("c", "three")],
      activeId,
      onFocus: (id: string) => focused.push(id),
      onClose: () => {},
    }),
  );
  return focused;
}

const tabs = (dom: Dom): HTMLElement[] => [...dom.container.querySelectorAll<HTMLElement>('[role="tab"]')];

test("the right arrow moves to the next tab, focusing it and selecting it", async () => {
  await withDom(async (dom) => {
    const focused = await mount(dom, "a");
    await dom.press(tabs(dom)[0], "ArrowRight");
    assert.deepEqual(focused, ["b"]);
    assert.ok(dom.window.document.activeElement === tabs(dom)[1], "the keyboard focus did not follow");
  });
});

test("the left arrow wraps from the first tab to the last", async () => {
  await withDom(async (dom) => {
    const focused = await mount(dom, "a");
    await dom.press(tabs(dom)[0], "ArrowLeft");
    assert.deepEqual(focused, ["c"]);
  });
});

test("Home and End go to the first and last", async () => {
  await withDom(async (dom) => {
    const focused = await mount(dom, "b");
    await dom.press(tabs(dom)[1], "End");
    await dom.press(tabs(dom)[1], "Home");
    assert.deepEqual(focused, ["c", "a"]);
  });
});

test("Enter and Space on a tab select it", async () => {
  await withDom(async (dom) => {
    const focused = await mount(dom, "a");
    await dom.press(tabs(dom)[2], "Enter");
    await dom.press(tabs(dom)[2], " ");
    assert.deepEqual(focused, ["c", "c"]);
  });
});

test("a key pressed on the close button is the button's, not the tab's", async () => {
  await withDom(async (dom) => {
    const focused = await mount(dom, "a");
    const close = dom.container.querySelector<HTMLElement>('[role="tab"] button');
    assert.ok(close, "no close button on a tab");
    await dom.press(close!, "ArrowRight");
    await dom.press(close!, "Enter");
    assert.deepEqual(focused, [], "the tab took a key that belonged to its close button");
  });
});
