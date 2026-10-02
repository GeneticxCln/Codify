/**
 * An editor tab in the strip: what kind it says it is, and the two facts a strip is the only place to say.
 *
 * A file with unsaved changes is marked on its tab, because the tab you are not looking at is the one you will close and
 * lose; and a file the assistant changed is marked too, because "something in a tab I was not looking at changed" is exactly
 * what the unread-output dot already says for a terminal. Both are also in the accessible name, since a dot alone is not a
 * label.
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
  { id: "e1", kind: "editor", title: "parser.py", path: "src/parser.py", workspaceId: "w1" },
  { id: "e2", kind: "editor", title: "lexer.py", path: "src/lexer.py", workspaceId: "w1" },
];

async function mount(dom: Dom, props: Partial<React.ComponentProps<typeof TabBar>> = {}): Promise<void> {
  await dom.render(h(TabBar, { tabs, activeId: "c", onFocus: () => {}, onClose: () => {}, ...props }));
}

const tabEl = (dom: Dom, title: string): HTMLElement =>
  [...dom.container.querySelectorAll<HTMLElement>('[role="tab"]')].find((t) => t.getAttribute("aria-label")?.includes(title))!;

test("every kind has an icon and a name, and an editor is called an editor", () => {
  for (const kind of ["chat", "terminal", "browser", "editor"] as const) {
    assert.equal(typeof KIND_ICON[kind], "object", `${kind} has no icon`);
    assert.ok(KIND_NAME[kind].length > 0);
  }
  assert.equal(KIND_NAME.editor, "Editor");
});

test("an editor tab is named for its kind and its file, and closes by that name", async () => {
  await withDom(async (dom) => {
    await mount(dom);

    assert.equal(tabEl(dom, "parser.py").getAttribute("aria-label"), "Editor: parser.py");
    assert.equal(dom.container.querySelectorAll('button[aria-label="Close editor: parser.py"]').length, 1);
  });
});

test("a tab with unsaved changes is marked, and says so in its name", async () => {
  await withDom(async (dom) => {
    await mount(dom, { unsavedIds: ["e1"] });

    assert.match(tabEl(dom, "parser.py").getAttribute("aria-label") ?? "", /unsaved changes/);
    assert.equal(tabEl(dom, "parser.py").querySelectorAll('[data-testid="editor-unsaved-dot"]').length, 1);
    assert.doesNotMatch(tabEl(dom, "lexer.py").getAttribute("aria-label") ?? "", /unsaved/);
    assert.equal(tabEl(dom, "lexer.py").querySelectorAll('[data-testid="editor-unsaved-dot"]').length, 0);
  });
});

test("a tab the assistant changed is marked, and says so in its name", async () => {
  await withDom(async (dom) => {
    await mount(dom, { assistantEditedIds: ["e2"] });

    assert.match(tabEl(dom, "lexer.py").getAttribute("aria-label") ?? "", /changed by the assistant/);
    assert.equal(tabEl(dom, "lexer.py").querySelectorAll('[data-testid="editor-assistant-dot"]').length, 1);
    assert.equal(tabEl(dom, "parser.py").querySelectorAll('[data-testid="editor-assistant-dot"]').length, 0);
  });
});

test("a tab can be both, and each says it", async () => {
  await withDom(async (dom) => {
    await mount(dom, { unsavedIds: ["e1"], assistantEditedIds: ["e1"] });

    const name = tabEl(dom, "parser.py").getAttribute("aria-label") ?? "";
    assert.match(name, /unsaved changes/);
    assert.match(name, /changed by the assistant/);
  });
});

test("the marks are only ever on editor tabs, whatever ids are passed", async () => {
  await withDom(async (dom) => {
    await mount(dom, { unsavedIds: ["c"], assistantEditedIds: ["c"] });

    assert.equal(tabEl(dom, "Refactor the parser").querySelectorAll("[data-testid^='editor-']").length, 0);
    assert.doesNotMatch(tabEl(dom, "Refactor the parser").getAttribute("aria-label") ?? "", /unsaved|assistant/);
  });
});

test("with nothing passed nothing is marked", async () => {
  await withDom(async (dom) => {
    await mount(dom);

    assert.equal(dom.container.querySelectorAll("[data-testid^='editor-']").length, 0);
  });
});
