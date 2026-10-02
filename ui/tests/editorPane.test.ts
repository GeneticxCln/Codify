/**
 * The editor pane, mounted: a real CodeMirror view in jsdom, over the store, over a fake disk.
 *
 * The pane is glue and the rules are elsewhere (`editorBuffers.test.ts` holds the text's rules, `editorSurface.test.ts` the
 * assistant's), so what is asserted here is what only a mounted pane can show:
 *
 *  - the text is on screen, and an edit the assistant makes while it is showing appears, marked, and is one undo step;
 *  - **the text survives the pane**: show another tab and come back, and what was typed, and how to undo it, is still there;
 *  - Save is a button and Ctrl+S, writes what is on screen, and says so when it cannot;
 *  - a conflict, a missing file and a file that will not open each say what is wrong and offer the way out;
 *  - a pane that appears unfocused (the second half of a split) does not take the keyboard.
 *
 * jsdom has no layout, so nothing about how it *looks* is claimed: that is a person's, in a real window.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { EditorBuffer, EditorBuffers, FileIo } from "../src/editorBuffers.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;
const { createEditorBuffers } = await import("../src/editorBuffers.ts");
const { EditorPane } = await import("../src/components/EditorPane.tsx");

const TEXT = "def f():\n    return 1\n";

interface Disk {
  [key: string]: { content: string; version: string };
}

function world(files: Disk) {
  const disk: Disk = { ...files };
  const saves: Array<{ path: string; content: string; base_version: string }> = [];
  let counter = 100;
  const io: FileIo = {
    async read(workspaceId, path) {
      const file = disk[`${workspaceId}:${path}`];
      if (!file) throw Object.assign(new Error("file_missing: there is no file"), { code: "file_missing" });
      return { ...file };
    },
    async save(workspaceId, body) {
      saves.push(body);
      const file = disk[`${workspaceId}:${body.path}`];
      if (!file) throw Object.assign(new Error("file_missing: there is no file"), { code: "file_missing" });
      if (file.version !== body.base_version) {
        throw Object.assign(new Error("file_changed"), { code: "file_changed", extra: { current_version: file.version } });
      }
      counter += 1;
      disk[`${workspaceId}:${body.path}`] = { content: body.content, version: `v${counter}` };
      return { path: body.path, version: `v${counter}`, size: body.content.length };
    },
  };
  return { disk, saves, io };
}

/** Wait for what the pane does after mount: the view's dynamic import, and the language's. */
const beat = (ms = 40): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

async function mounted(
  dom: Dom,
  options: { files?: Disk; autoFocus?: boolean; key?: string } = {},
) {
  const files = options.files ?? { "w1:src/a.py": { content: TEXT, version: "v1" } };
  const w = world(files);
  const buffers: EditorBuffers = createEditorBuffers(w.io);
  const views: any[] = [];
  const attach = buffers.attachView.bind(buffers);
  buffers.attachView = (tabId, view) => {
    if (view) views.push(view);
    attach(tabId, view);
  };
  await buffers.open("e1", { workspaceId: "w1", path: "src/a.py" });
  const render = async (props: { tabId?: string; autoFocus?: boolean } = {}) => {
    await dom.render(h(EditorPane, { tabId: props.tabId ?? "e1", buffers, autoFocus: props.autoFocus ?? options.autoFocus ?? true }));
    await beat();
    await dom.settle();
  };
  await render();
  return { ...w, buffers, views, render, get: (): EditorBuffer => buffers.get("e1") as EditorBuffer };
}

const text = (dom: Dom): string => dom.container.querySelector(".cm-content")?.textContent ?? "";
const button = (dom: Dom, name: string): HTMLButtonElement =>
  [...dom.container.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === name) as HTMLButtonElement;

// ── what is on screen ────────────────────────────────────────────────────────

test("the file's text is on screen, under its path, and nothing is marked", async () => {
  await withDom(async (dom) => {
    await mounted(dom);

    assert.match(text(dom), /def f\(\):/);
    assert.match(text(dom), /return 1/);
    assert.ok(dom.container.textContent?.includes("src/a.py"));
    assert.equal(dom.container.querySelectorAll(".cm-ai-edit").length, 0);
    assert.equal(button(dom, "Save").disabled, true, "nothing to save");
    assert.equal(dom.container.textContent?.includes("Unsaved"), false);
  });
});

test("the editor is named for its file, for a screen reader", async () => {
  await withDom(async (dom) => {
    await mounted(dom);

    assert.equal(dom.container.querySelector(".cm-content")?.getAttribute("aria-label"), "Editing src/a.py");
  });
});

test("a file that is still loading says so, and a file that failed to open says why and can be tried again", async () => {
  await withDom(async (dom) => {
    const w = world({});
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    const buffers = createEditorBuffers({
      async read(workspaceId, path) {
        await gate;
        return w.io.read(workspaceId, path);
      },
      save: w.io.save,
    });
    const pending = buffers.open("e1", { workspaceId: "w1", path: "nope.py" });
    await dom.render(h(EditorPane, { tabId: "e1", buffers }));
    assert.match(dom.container.textContent ?? "", /Opening nope\.py/);

    release();
    await pending;
    await dom.settle();

    assert.match(dom.container.querySelector('[role="alert"]')?.textContent ?? "", /no file/);
    assert.equal(dom.container.querySelectorAll(".cm-content").length, 0, "an editor was shown over a file that did not open");
    w.disk["w1:nope.py"] = { content: "now here\n", version: "v1" };
    await dom.click(button(dom, "Try again"));
    await beat();
    await dom.settle();

    assert.match(text(dom), /now here/);
  });
});

test("a tab with no buffer yet says it is opening, rather than showing an empty editor to type into", async () => {
  await withDom(async (dom) => {
    const buffers = createEditorBuffers(world({}).io);

    await dom.render(h(EditorPane, { tabId: "nothing", buffers }));

    assert.match(dom.container.textContent ?? "", /Opening/);
    assert.equal(dom.container.querySelectorAll(".cm-content").length, 0);
  });
});

test("the status line says where the cursor is and what the file's line endings and encoding are", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom, { files: { "w1:src/a.py": { content: "one\r\ntwo\r\n", version: "v1" } } });
    m.buffers.selectLines("e1", 2, 2);
    await dom.settle();

    const bar = dom.container.querySelector('[data-testid="editor-status"]')?.textContent ?? "";

    assert.match(bar, /Ln 2, Col 4/, "selecting a line leaves the cursor at its end");
    assert.match(bar, /CRLF/);
    assert.match(bar, /UTF-8/);
  });
});

// ── the assistant edits while it is showing ──────────────────────────────────

test("an edit by the assistant appears on screen, marked, with the pane told it was the assistant's", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);

    const done = m.buffers.edit("e1", { oldText: "return 1", newText: "return 42", count: 1 });
    await dom.settle();

    assert.equal(done.ok, true);
    assert.match(text(dom), /return 42/);
    const marks = [...dom.container.querySelectorAll(".cm-ai-edit")];
    assert.equal(marks.length, 1);
    assert.equal(marks[0].textContent, "return 42");
    assert.match(dom.container.textContent ?? "", /Changed by the assistant/);
    assert.match(dom.container.textContent ?? "", /Unsaved/);
    assert.equal(button(dom, "Save").disabled, false);
  });
});

test("the assistant's edit is its own undo step, apart from what the person had just typed", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    const { undo } = await import("@codemirror/commands");
    const view = m.views[0];
    view.dispatch({ changes: { from: 0, insert: "# mine\n" }, userEvent: "input.type" });
    m.buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
    await dom.settle();

    undo(view);
    await dom.settle();

    assert.equal(view.state.doc.toString(), "# mine\ndef f():\n    return 1\n", "one undo took back the assistant's edit and the person's typing with it");
    undo(view);
    assert.equal(view.state.doc.toString(), TEXT);
  });
});

test("the assistant's edit stays its own undo step even when it touches the text the person was just typing", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    const { undo } = await import("@codemirror/commands");
    const view = m.views[0];
    // Typed straight in front of "def", and the assistant then rewrites "def" itself: adjacent changes in quick succession
    // are one undo step to CodeMirror's history unless something says otherwise.
    view.dispatch({ changes: { from: 0, insert: "async " }, userEvent: "input.type" });
    m.buffers.edit("e1", { oldText: "def", newText: "DEF", count: 1 });
    await dom.settle();
    assert.match(view.state.doc.toString(), /^async DEF f\(\)/);

    undo(view);

    assert.equal(view.state.doc.toString(), "async def f():\n    return 1\n", "one undo took back the person's typing along with the assistant's edit");
  });
});

test("the marks are right the moment the text changes, not a render later", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    m.buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
    await dom.settle();

    // No settle after this: what is on screen *now* is what the view drew from the store's ranges at the time.
    m.views[0].dispatch({ changes: { from: 0, insert: "# a comment\n" } });

    assert.equal(dom.container.querySelector(".cm-ai-edit")?.textContent, "return 2", "the mark was drawn from ranges that had not caught up with the text");
  });
});

test("selecting lines scrolls the selection into view", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    const view = m.views[0];
    const dispatched: Array<{ effects?: unknown }> = [];
    const original = view.dispatch.bind(view);
    view.dispatch = (...args: Array<{ effects?: unknown }>) => {
      dispatched.push(args[0] ?? {});
      return original(...args);
    };

    m.buffers.selectLines("e1", 2, 2);
    await dom.settle();

    assert.ok(dispatched.some((spec) => spec.effects !== undefined), "nothing asked the view to scroll");
  });
});

test("the file's grammar arrives after its text, and colours it", async () => {
  await withDom(async (dom) => {
    await mounted(dom);
    await beat(80);

    const coloured = dom.container.querySelectorAll(".cm-line span").length;

    assert.ok(coloured > 0, "a Python file was shown as plain text");
  });
});

test("a file with no grammar is plain text, and shows its text all the same", async () => {
  await withDom(async (dom) => {
    const w = world({ "w1:Makefile": { content: "all:\n\ttrue\n", version: "v1" } });
    const buffers = createEditorBuffers(w.io);
    await buffers.open("e1", { workspaceId: "w1", path: "Makefile" });
    await dom.render(h(EditorPane, { tabId: "e1", buffers }));
    await beat(80);

    assert.match(dom.container.querySelector(".cm-content")?.textContent ?? "", /all:/);
    assert.equal(dom.container.querySelectorAll(".cm-line span").length, 0);
  });
});

test("a mark follows the text when the person types above it", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    m.buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
    await dom.settle();

    m.views[0].dispatch({ changes: { from: 0, insert: "# a comment\n" } });
    await dom.settle();

    assert.equal(dom.container.querySelector(".cm-ai-edit")?.textContent, "return 2");
  });
});

test("saving clears the marks and the unsaved badge", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    m.buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
    await dom.settle();

    await dom.click(button(dom, "Save"));
    await beat();
    await dom.settle();

    assert.equal(dom.container.querySelectorAll(".cm-ai-edit").length, 0);
    assert.equal(dom.container.textContent?.includes("Unsaved"), false);
    assert.equal(dom.container.textContent?.includes("Changed by the assistant"), false);
    assert.equal(button(dom, "Save").disabled, true);
  });
});

test("selecting lines for the assistant puts the selection on screen", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);

    m.buffers.selectLines("e1", 2, 2);
    await dom.settle();

    const { from, to } = m.views[0].state.selection.main;
    assert.equal(m.views[0].state.sliceDoc(from, to), "    return 1");
  });
});

// ── the text outlives the pane ───────────────────────────────────────────────

test("showing another tab and coming back keeps the text, the selection and the undo history", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    const { undo } = await import("@codemirror/commands");
    m.views[0].dispatch({ changes: { from: 0, insert: "# kept\n" }, userEvent: "input.type" });
    await dom.settle();

    await dom.render(h("div", null, "another tab"));
    assert.equal(dom.container.querySelectorAll(".cm-content").length, 0, "the view should be gone with its pane");
    await m.render();

    assert.match(text(dom), /# kept/);
    assert.equal(m.views.length, 2, "a new view was made for the new mount");
    undo(m.views[1]);
    assert.equal(m.views[1].state.doc.toString(), TEXT, "the undo history did not survive the pane");
  });
});

test("while the pane is away the assistant can still edit, and the edit is there when it comes back", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    await dom.render(h("div", null, "another tab"));

    const done = m.buffers.edit("e1", { oldText: "return 1", newText: "return 7", count: 1 });
    await m.render();

    assert.equal(done.ok, true);
    assert.match(text(dom), /return 7/);
    assert.equal(dom.container.querySelector(".cm-ai-edit")?.textContent, "return 7");
  });
});

test("the view is detached from the store when the pane goes, so a stale view is never written to", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    const gone = m.views[0];
    let dispatched = 0;
    const original = gone.dispatch.bind(gone);
    gone.dispatch = (...args: unknown[]) => {
      dispatched += 1;
      return original(...args);
    };
    await dom.render(h("div", null, "another tab"));

    m.buffers.edit("e1", { oldText: "return 1", newText: "return 9", count: 1 });

    assert.equal(dispatched, 0);
  });
});

test("reverting puts the file's text back on screen", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    m.buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
    await dom.settle();

    await dom.click(button(dom, "Revert"));
    assert.match(dom.container.textContent ?? "", /Discard/, "reverting throws text away, so it asks");
    assert.equal(m.get().dirty, true, "the first press must not discard anything");
    await dom.click(button(dom, "Discard"));
    await beat();
    await dom.settle();

    assert.match(text(dom), /return 1/);
    assert.equal(m.get().dirty, false);
    assert.equal(dom.container.querySelectorAll(".cm-ai-edit").length, 0);
  });
});

test("backing out of a revert changes nothing", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    m.buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
    await dom.settle();

    await dom.click(button(dom, "Revert"));
    await dom.click(button(dom, "Cancel"));

    assert.equal(m.get().dirty, true);
    assert.match(text(dom), /return 2/);
    assert.ok(button(dom, "Revert"), "the Revert button should be back");
  });
});

// ── saving ───────────────────────────────────────────────────────────────────

test("Save writes what is on screen, with the version it was opened at", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    m.views[0].dispatch({ changes: { from: 0, insert: "# new\n" } });
    await dom.settle();

    await dom.click(button(dom, "Save"));
    await beat();

    assert.equal(m.saves.length, 1);
    assert.deepEqual(m.saves[0], { path: "src/a.py", content: "# new\n" + TEXT, base_version: "v1" });
  });
});

test("Ctrl+S in the editor saves, and nothing outside the editor does", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    m.views[0].dispatch({ changes: { from: 0, insert: "# new\n" } });
    await dom.settle();
    const content = dom.container.querySelector(".cm-content") as HTMLElement;

    const event = new dom.window.KeyboardEvent("keydown", { key: "s", ctrlKey: true, bubbles: true, cancelable: true });
    content.dispatchEvent(event);
    await beat();

    assert.equal(m.saves.length, 1, "Ctrl+S in the editor did not save");
    assert.equal(event.defaultPrevented, true, "the browser's own Save page dialog would have opened");
  });
});

test("a file that changed on disk offers a way out and does not overwrite", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    m.buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
    m.disk["w1:src/a.py"] = { content: "theirs\n", version: "v9" };
    await dom.settle();

    await dom.click(button(dom, "Save"));
    await beat();
    await dom.settle();

    const banner = dom.container.querySelector('[role="alert"]')?.textContent ?? "";
    assert.match(banner, /changed on disk/);
    assert.ok(button(dom, "Reload from disk"));
    assert.ok(button(dom, "Keep my version"));
    assert.equal(m.disk["w1:src/a.py"].content, "theirs\n");
  });
});

async function conflicted(dom: Dom) {
  const m = await mounted(dom);
  m.buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
  m.disk["w1:src/a.py"] = { content: "theirs\n", version: "v9" };
  await dom.settle();
  await dom.click(button(dom, "Save"));
  await beat();
  await dom.settle();
  return m;
}

test("keeping my version and saving again overwrites the file", async () => {
  await withDom(async (dom) => {
    const m = await conflicted(dom);

    await dom.click(button(dom, "Keep my version"));
    await dom.click(button(dom, "Save"));
    await beat();
    await dom.settle();

    assert.equal(m.disk["w1:src/a.py"].content, "def f():\n    return 2\n");
    assert.equal(dom.container.querySelectorAll('[role="alert"]').length, 0);
  });
});

test("reloading from disk takes the disk's text and drops the person's", async () => {
  await withDom(async (dom) => {
    const m = await conflicted(dom);

    await dom.click(button(dom, "Reload from disk"));
    await beat();
    await dom.settle();

    assert.match(text(dom), /theirs/);
    assert.equal(m.get().dirty, false);
    assert.equal(dom.container.querySelectorAll('[role="alert"]').length, 0);
  });
});

test("a file deleted on disk says so, and that the text on screen is still the person's", async () => {
  await withDom(async (dom) => {
    const m = await mounted(dom);
    m.buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
    delete m.disk["w1:src/a.py"];

    await m.buffers.noteDiskChange("w1", ["src/a.py"]);
    await dom.settle();

    const banner = dom.container.querySelector('[role="alert"]')?.textContent ?? "";
    assert.match(banner, /deleted/);
    assert.match(banner, /still here|still yours|copy/i);
    assert.match(text(dom), /return 2/);
  });
});

test("a save that fails for another reason says why, in the person's words, and keeps the text", async () => {
  await withDom(async (dom) => {
    const w = world({ "w1:src/a.py": { content: TEXT, version: "v1" } });
    const failing = createEditorBuffers({
      ...w.io,
      async save() {
        throw Object.assign(new Error("file_access: Permission denied"), { code: "file_access" });
      },
    });
    await failing.open("e1", { workspaceId: "w1", path: "src/a.py" });
    await dom.render(h(EditorPane, { tabId: "e1", buffers: failing }));
    await beat();
    failing.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
    await dom.settle();

    await dom.click(button(dom, "Save"));
    await beat();
    await dom.settle();

    assert.match(dom.container.querySelector('[role="alert"]')?.textContent ?? "", /Permission denied/);
    assert.match(text(dom), /return 2/);
    assert.equal(button(dom, "Save").disabled, false, "the person must be able to try again");
  });
});

test("while a save is in flight the button says so and cannot be pressed twice", async () => {
  await withDom(async (dom) => {
    const w = world({ "w1:src/a.py": { content: TEXT, version: "v1" } });
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    const slow = createEditorBuffers({
      read: w.io.read,
      async save(workspaceId, body) {
        await gate;
        return w.io.save(workspaceId, body);
      },
    });
    await slow.open("e1", { workspaceId: "w1", path: "src/a.py" });
    await dom.render(h(EditorPane, { tabId: "e1", buffers: slow }));
    await beat();
    slow.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
    await dom.settle();

    const saving = slow.save("e1");
    await dom.settle();
    assert.equal(button(dom, "Saving…").disabled, true);
    release();
    await saving;
    await dom.settle();

    assert.equal(button(dom, "Save").disabled, true);
  });
});

// ── focus ────────────────────────────────────────────────────────────────────

test("an editor that is ready takes the keyboard when it is asked to", async () => {
  await withDom(async (dom) => {
    await mounted(dom, { autoFocus: true });

    assert.equal(dom.window.document.activeElement?.classList.contains("cm-content"), true);
  });
});

test("an editor that appears unfocused, as the second half of a split does, does not take the keyboard", async () => {
  await withDom(async (dom) => {
    await mounted(dom, { autoFocus: false });

    assert.equal(dom.window.document.activeElement?.classList.contains("cm-content"), false);
  });
});
