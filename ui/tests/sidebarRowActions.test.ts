/**
 * The two buttons on a thread row: Rename and Archive.
 *
 * They sit inside the row, and the row opens the thread when it is clicked or when Enter is pressed on it. A button
 * inside a control inherits both: pressing Archive also opened the thread (a tab appeared and hydrated, then closed),
 * and Enter on a focused Archive button was swallowed by the row's own key handler, which selected the thread and
 * cancelled the press, so a keyboard user could neither rename nor archive.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;

const thread = {
  id: "c1", workspace_id: "w1", title: "Parser work", archived: false, parent_id: null, parent_title: null,
  created_at: 1, updated_at: 1,
};

async function mountSidebar(dom: Dom) {
  const { Sidebar } = await import("../src/components/Sidebar.tsx");
  const calls: string[] = [];
  await dom.render(
    React.createElement(Sidebar, {
      conversations: [thread],
      selectedWorkspaceId: "w1",
      onNewThread: () => calls.push("new"),
      onNewProject: () => calls.push("project"),
      onSelect: (id: string) => calls.push(`select:${id}`),
      onRename: (id: string) => calls.push(`rename:${id}`),
      onArchive: (id: string) => calls.push(`archive:${id}`),
    }),
  );
  return calls;
}

test("pressing Rename renames and does not also open the thread", async () => {
  await withDom(async (dom) => {
    const calls = await mountSidebar(dom);
    await dom.click(dom.byLabel("Rename conversation"));
    assert.deepEqual(calls, ["rename:c1"]);
  });
});

test("pressing Archive archives and does not also open the thread", async () => {
  await withDom(async (dom) => {
    const calls = await mountSidebar(dom);
    await dom.click(dom.byLabel("Archive conversation"));
    assert.deepEqual(calls, ["archive:c1"]);
  });
});

test("Enter on a focused action button is the button's press, not the row's", async () => {
  await withDom(async (dom) => {
    const calls = await mountSidebar(dom);
    const archive = dom.byLabel("Archive conversation");
    // The keydown reaches the row first; it must leave the key alone so the browser can press the button.
    const event = new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    await dom.act(async () => {
      archive.dispatchEvent(event);
    });
    assert.equal(event.defaultPrevented, false, "the row cancelled a key that belonged to the button");
    assert.deepEqual(calls, [], "the row selected the thread on a key meant for its button");
  });
});

test("Enter on the row itself still opens the thread", async () => {
  await withDom(async (dom) => {
    const calls = await mountSidebar(dom);
    const row = dom.window.document.querySelector('[data-thread-row="true"]') as Element;
    await dom.press(row, "Enter");
    assert.deepEqual(calls, ["select:c1"]);
  });
});

test("the buttons are shown when focus is inside the row, not only when the pointer is over it", async () => {
  await withDom(async (dom) => {
    await mountSidebar(dom);
    const holder = dom.byLabel("Rename conversation").parentElement as HTMLElement;
    const classes = (holder.getAttribute("class") ?? "").split(/\s+/);
    assert.ok(classes.includes("group-hover:opacity-100"));
    assert.ok(classes.includes("group-focus-within:opacity-100"), "invisible while focused: keyboard users cannot see what they are pressing");
  });
});
