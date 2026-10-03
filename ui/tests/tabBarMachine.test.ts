/**
 * A machine tab in the strip: what kind it says it is, that it is not mistaken for a terminal, and the one fact a strip is the
 * only place to say about a jail: whether it can reach the network.
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
const { TabBar, KIND_ICON, KIND_NAME } = await import("../src/components/TabBar.tsx");

const tabs: Tab[] = [
  { id: "c", kind: "chat", title: "Refactor the parser", conversationId: "c" },
  { id: "t", kind: "terminal", title: "Terminal" },
  { id: "m1", kind: "machine", title: "Machine", workspaceId: "w1", network: false },
  { id: "m2", kind: "machine", title: "Machine · network", workspaceId: "w1", network: true },
];

async function mount(dom: Dom): Promise<void> {
  await dom.render(h(TabBar, { tabs, activeId: "c", onFocus: () => {}, onClose: () => {} }));
}

const tabEl = (dom: Dom, title: string): HTMLElement =>
  [...dom.container.querySelectorAll<HTMLElement>('[role="tab"]')].find((t) => t.getAttribute("aria-label")?.includes(title))!;

test("a machine has its own icon and name, and is not a terminal's", () => {
  assert.equal(KIND_NAME.machine, "Machine");
  assert.equal(typeof KIND_ICON.machine, "object");
  assert.notEqual(KIND_ICON.machine, KIND_ICON.terminal, "a jailed shell wears the icon of the person's own");
});

test("a machine tab is named for its kind and closes by that name", async () => {
  await withDom(async (dom) => {
    await mount(dom);

    assert.equal(tabEl(dom, "Machine · network").getAttribute("aria-label")?.startsWith("Machine: Machine · network"), true);
    assert.equal(dom.container.querySelectorAll('button[aria-label="Close machine: Machine"]').length, 1);
  });
});

test("only a machine with a network says so in its accessible name", async () => {
  await withDom(async (dom) => {
    await mount(dom);

    const names = [...dom.container.querySelectorAll<HTMLElement>('[role="tab"]')].map((t) => t.getAttribute("aria-label") ?? "");
    const offline = names.find((n) => n.startsWith("Machine: Machine") && !n.includes("·"))!;
    const online = names.find((n) => n.includes("Machine · network"))!;
    assert.doesNotMatch(offline, /network/i);
    assert.match(online, /has network access/);
    assert.doesNotMatch(names.find((n) => n.startsWith("Terminal"))!, /network/i);
  });
});
