/**
 * The menu on a tab: what it offers, and where it lands.
 *
 * Pure rules first (`tabMenu.ts`), then the component (`TabMenu.tsx`), which is a view like `ThreadMenu`: a point and
 * some callbacks in, a menu out. What each item does is `splitApp.test.ts`'s business; here it is that an item that
 * cannot work says why instead of doing nothing, that the menu stays inside the window, and that the keyboard can leave it.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { Tab, TabState } from "../src/tabs.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;
const { act } = React as unknown as { act: (body: () => Promise<void> | void) => Promise<void> };
const { TAB_MENU_FALLBACK_SIZE, TAB_MENU_LABEL, TAB_MENU_ROW_HEIGHT, tabMenuItems } = await import("../src/tabMenu.ts");
const { PAIR_REFUSALS } = await import("../src/panes.ts");
const { TabMenu } = await import("../src/components/TabMenu.tsx");

const chat = (id: string): Tab => ({ id, kind: "chat", title: id });
const term = (id: string): Tab => ({ id, kind: "terminal", title: id });
const page = (id: string): Tab => ({ id, kind: "browser", title: id });
const strip = (tabs: Tab[], activeId: string | null): TabState => ({ tabs, activeId });

const ids = (items: ReturnType<typeof tabMenuItems>): string[] => items.map((i) => i.id);

// ── the items ───────────────────────────────────────────────────────────────

test("on the tab you are in, the menu offers a split with a new terminal", () => {
  for (const active of [chat("c"), term("t")]) {
    const state = strip([active, chat("other")], active.id);
    const items = tabMenuItems(active, state, false);
    assert.deepEqual(ids(items), ["split-new-terminal"]);
    assert.equal(items[0].label, "Split with a new terminal");
    assert.equal(items[0].reason, undefined, "an item that can work says it cannot");
  }
});

test("on a browser page you are in, that same item says why it cannot", () => {
  const state = strip([page("p"), term("t")], "p");
  const [item] = tabMenuItems(page("p"), state, false);
  assert.equal(item.id, "split-new-terminal");
  assert.equal(item.reason, PAIR_REFUSALS.browser);
});

test("on another tab, the menu offers to show it beside the current one", () => {
  const state = strip([chat("c"), term("t")], "c");
  const [item] = tabMenuItems(term("t"), state, false);
  assert.equal(item.id, "show-beside");
  assert.equal(item.label, "Show beside the current tab");
  assert.equal(item.reason, undefined);
  assert.deepEqual(ids(tabMenuItems(chat("c"), strip([term("t"), chat("c")], "t"), false)), ["show-beside"]);
});

test("another tab that cannot sit beside the current one says why, in the reason's own words", () => {
  const twoChats = strip([chat("a"), chat("b")], "a");
  assert.equal(tabMenuItems(chat("b"), twoChats, false)[0].reason, PAIR_REFUSALS["two-chats"]);
  const withPage = strip([chat("a"), page("p")], "a");
  assert.equal(tabMenuItems(page("p"), withPage, false)[0].reason, PAIR_REFUSALS.browser);
  const fromPage = strip([page("p"), chat("a")], "p");
  assert.equal(tabMenuItems(chat("a"), fromPage, false)[0].reason, PAIR_REFUSALS.browser);
});

test("while a split is showing, every tab's menu offers to close it, and only that", () => {
  const state = strip([chat("c"), term("t"), chat("other")], "c");
  for (const opened of state.tabs) {
    const items = tabMenuItems(opened, state, true);
    assert.deepEqual(ids(items), ["close-split"], `on ${opened.id}`);
    assert.equal(items[0].label, "Close split");
    assert.equal(items[0].reason, undefined);
  }
});

test("a tab with nothing active to be beside is told so", () => {
  const state = strip([chat("a"), term("t")], null);
  assert.equal(tabMenuItems(term("t"), state, false)[0].reason, PAIR_REFUSALS.missing);
});

test("the menu's label and its fallback size fit what it holds", () => {
  assert.equal(TAB_MENU_LABEL, "Tab actions");
  assert.ok(TAB_MENU_FALLBACK_SIZE.height >= TAB_MENU_ROW_HEIGHT, "the guess is shorter than one row");
  assert.ok(TAB_MENU_FALLBACK_SIZE.width > 0);
});

// ── the component ───────────────────────────────────────────────────────────

interface Calls {
  chosen: string[];
  closed: number;
}

async function mount(dom: Dom, props: Partial<React.ComponentProps<typeof TabMenu>> = {}): Promise<Calls> {
  const calls: Calls = { chosen: [], closed: 0 };
  await dom.render(
    h(TabMenu, {
      items: [{ id: "show-beside", label: "Show beside the current tab" }],
      x: 40,
      y: 30,
      bounds: { x: 0, y: 0, width: 1000, height: 700 },
      onChoose: (id: string) => void calls.chosen.push(id),
      onClose: () => void (calls.closed += 1),
      ...props,
    }),
  );
  return calls;
}

const menu = (dom: Dom): HTMLElement => dom.container.querySelector('[role="menu"]') as HTMLElement;
const items = (dom: Dom): HTMLElement[] => [...dom.container.querySelectorAll<HTMLElement>('[role="menuitem"]')];

test("the menu is named, holds its items, and puts the focus on the first", async () => {
  await withDom(async (dom) => {
    await mount(dom);
    assert.equal(menu(dom).getAttribute("aria-label"), TAB_MENU_LABEL);
    assert.deepEqual(items(dom).map((i) => i.textContent?.trim()), ["Show beside the current tab"]);
    assert.ok(dom.window.document.activeElement === items(dom)[0], "the first item does not have the focus");
  });
});

test("choosing an item closes the menu and says which", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom, { items: [{ id: "close-split", label: "Close split" }] });
    await dom.click(items(dom)[0]);
    assert.deepEqual(calls.chosen, ["close-split"]);
    assert.equal(calls.closed, 1);
  });
});

test("an item that cannot work shows why, stays reachable, and does nothing when pressed", async () => {
  await withDom(async (dom) => {
    const reason = "Two conversations can't be side by side yet: they would share one message box.";
    const calls = await mount(dom, { items: [{ id: "show-beside", label: "Show beside the current tab", reason }] });
    const [item] = items(dom);
    assert.equal(item.getAttribute("aria-disabled"), "true");
    assert.match(menu(dom).textContent ?? "", /Two conversations can't be side by side yet/, "the reason is not on screen");
    assert.ok(dom.window.document.activeElement === item, "a disabled item cannot be reached, so a keyboard user never hears why");
    await dom.click(item);
    assert.deepEqual(calls.chosen, []);
    assert.equal(calls.closed, 0, "pressing a disabled item closed the menu as if it had worked");
  });
});

test("clicking outside closes it, and so does a right-click outside", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom);
    const sheet = dom.container.querySelector(".fixed.inset-0") as HTMLElement;
    await dom.click(sheet);
    assert.equal(calls.closed, 1);
    let prevented = false;
    await act(async () => {
      const event = new dom.window.MouseEvent("contextmenu", { bubbles: true, cancelable: true });
      sheet.dispatchEvent(event);
      prevented = event.defaultPrevented;
    });
    assert.equal(calls.closed, 2);
    assert.equal(prevented, true, "the webview's own menu would have opened over this one");
  });
});

test("Escape closes it, and goes no further", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom);
    let reached = false;
    dom.window.document.addEventListener("keydown", () => (reached = true));
    await act(async () => {
      items(dom)[0].dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    });
    assert.equal(calls.closed, 1);
    assert.equal(reached, false, "Escape went on to whatever is behind the menu");
  });
});

test("the arrow keys move between items and wrap", async () => {
  await withDom(async (dom) => {
    await mount(dom, {
      items: [
        { id: "show-beside", label: "One" },
        { id: "close-split", label: "Two" },
      ],
    });
    const [one, two] = items(dom);
    const press = (name: string) =>
      act(async () => {
        dom.window.document.activeElement?.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true }));
      });
    assert.ok(dom.window.document.activeElement === one);
    await press("ArrowDown");
    assert.ok(dom.window.document.activeElement === two);
    await press("ArrowDown");
    assert.ok(dom.window.document.activeElement === one, "did not wrap past the last");
    await press("ArrowUp");
    assert.ok(dom.window.document.activeElement === two, "did not wrap above the first");
  });
});

test("the menu goes where it was asked, and stays inside the window", async () => {
  await withDom(async (dom) => {
    // The harness answers every rectangle at the same size, so the window is made larger than that to leave room.
    const wide = { x: 0, y: 0, width: 4000, height: 3000 };
    await mount(dom, { x: 40, y: 30, bounds: wide });
    assert.equal(menu(dom).style.left, "40px");
    assert.equal(menu(dom).style.top, "30px");
    await mount(dom, { x: 9000, y: 9000, bounds: wide });
    assert.ok(Number.parseFloat(menu(dom).style.left) < 4000, "the menu was placed off the right of the window");
    assert.ok(Number.parseFloat(menu(dom).style.top) < 3000, "the menu was placed off the bottom of the window");
    assert.ok(Number.parseFloat(menu(dom).style.left) > 0 && Number.parseFloat(menu(dom).style.top) > 0, "it was pinned to the origin");
  });
});
