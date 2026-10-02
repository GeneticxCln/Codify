/**
 * The strip when a split is showing, and the menu's way in.
 *
 * Both panes' tabs are marked so the strip says what is on screen, and only the focused one is selected (everything
 * keyed to the active tab keeps meaning "the one I am working in"). A right-click, or the Menu key, on a tab asks the
 * App for that tab's menu, and a strip with no one to ask leaves the browser's own menu alone.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { Tab } from "../src/tabs.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;
const { act } = React as unknown as { act: (body: () => Promise<void> | void) => Promise<void> };
const { TabBar } = await import("../src/components/TabBar.tsx");

const tabs: Tab[] = [
  { id: "c", kind: "chat", title: "Refactor the parser", conversationId: "c" },
  { id: "t", kind: "terminal", title: "shell one" },
  { id: "other", kind: "chat", title: "Add an index", conversationId: "other" },
];

async function mount(dom: Dom, props: Partial<React.ComponentProps<typeof TabBar>> = {}): Promise<void> {
  await dom.render(h(TabBar, { tabs, activeId: "c", onFocus: () => {}, onClose: () => {}, ...props }));
}

const tab = (dom: Dom, id: string): HTMLElement =>
  [...dom.container.querySelectorAll<HTMLElement>('[role="tab"]')].find((t) => t.getAttribute("aria-label")?.includes(tabs.find((x) => x.id === id)!.title))!;

test("the tabs in a split are marked, the others are not", async () => {
  await withDom(async (dom) => {
    await mount(dom, { splitIds: ["c", "t"] });
    assert.equal(tab(dom, "c").getAttribute("data-in-split"), "true");
    assert.equal(tab(dom, "t").getAttribute("data-in-split"), "true");
    assert.equal(tab(dom, "other").getAttribute("data-in-split"), null);
    assert.match(tab(dom, "t").getAttribute("aria-label") ?? "", /in split view/);
    assert.doesNotMatch(tab(dom, "other").getAttribute("aria-label") ?? "", /split/);
  });
});

test("only the focused tab is selected, whichever two are in the split", async () => {
  await withDom(async (dom) => {
    await mount(dom, { splitIds: ["c", "t"], activeId: "t" });
    const selected = [...dom.container.querySelectorAll('[role="tab"]')].filter((t) => t.getAttribute("aria-selected") === "true");
    assert.equal(selected.length, 1);
    assert.match(selected[0].getAttribute("aria-label") ?? "", /shell one/);
  });
});

test("with no split nothing is marked", async () => {
  await withDom(async (dom) => {
    await mount(dom);
    assert.equal(dom.container.querySelectorAll("[data-in-split]").length, 0);
    await mount(dom, { splitIds: [] });
    assert.equal(dom.container.querySelectorAll("[data-in-split]").length, 0);
  });
});

test("a right-click on a tab asks for that tab's menu, where the pointer is, and keeps the browser's own away", async () => {
  await withDom(async (dom) => {
    const asked: Array<[string, number, number]> = [];
    await mount(dom, { onMenu: (id: string, x: number, y: number) => void asked.push([id, x, y]) });
    let prevented = false;
    await act(async () => {
      const event = new dom.window.MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 123, clientY: 45 });
      tab(dom, "t").dispatchEvent(event);
      prevented = event.defaultPrevented;
    });
    assert.deepEqual(asked, [["t", 123, 45]]);
    assert.equal(prevented, true);
  });
});

test("a strip nobody listens to leaves the right-click alone", async () => {
  await withDom(async (dom) => {
    await mount(dom);
    let prevented = true;
    await act(async () => {
      const event = new dom.window.MouseEvent("contextmenu", { bubbles: true, cancelable: true });
      tab(dom, "t").dispatchEvent(event);
      prevented = event.defaultPrevented;
    });
    assert.equal(prevented, false);
  });
});

test("clicking and closing work as they did", async () => {
  await withDom(async (dom) => {
    const focused: string[] = [];
    const closed: string[] = [];
    await mount(dom, { splitIds: ["c", "t"], onFocus: (id: string) => void focused.push(id), onClose: (id: string) => void closed.push(id) });
    await dom.click(tab(dom, "t"));
    await dom.click(dom.byLabel("Close terminal: shell one"));
    assert.deepEqual(focused, ["t"]);
    assert.deepEqual(closed, ["t"], "closing a tab also focused it");
  });
});
