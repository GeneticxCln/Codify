/**
 * The assistant's eyes and hands on the editor, answered from the buffers: `read`, `open` and `edit`.
 *
 * The engine puts a question (`engine/surfaces.py`), the window answers it from here, and what comes back is checked
 * against a fixed shape on the engine's side. Everything below runs headless: the answers are computed from the store's
 * `EditorState`s and a stand-in for the App, with no view and no DOM.
 *
 * The properties that carry the weight:
 *  - **it sees what the person sees**: the unsaved text, not the disk, and where the cursor and selection are;
 *  - **it is scoped to the workspace it was asked about**: another workspace's files are neither listed nor reachable;
 *  - **it never leaves anything behind**: an open that fails closes the tab it made, an edit that fails changes nothing and
 *    (if it had to open the file to try) closes it again;
 *  - **it never moves the person**: editing a file that is not open opens it in the background, and `open` goes beside the
 *    conversation only if the host says that is where it belongs;
 *  - **it cannot save**: no operation here calls the store's `save`, and the proof is the store's io never being asked to.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { EditorBuffers, FileIo } from "../src/editorBuffers.ts";
import type { EditorHost } from "../src/editorSurface.ts";

const { createEditorBuffers } = await import("../src/editorBuffers.ts");
const { createEditorSurface } = await import("../src/editorSurface.ts");
const { EditorSelection } = await import("@codemirror/state");

const A = "def f():\n    return 1\n\n\ndef g():\n    return 2\n";

function setup(files: Record<string, string> = { "w1:src/a.py": A, "w1:src/b.py": "b\n", "w2:other.py": "o\n" }) {
  const saved: string[] = [];
  const reads: string[] = [];
  const io: FileIo = {
    async read(workspaceId, path) {
      reads.push(`${workspaceId}:${path}`);
      const content = files[`${workspaceId}:${path}`];
      if (content === undefined) throw Object.assign(new Error("file_missing: there is no file"), { code: "file_missing" });
      return { content, version: "v1" };
    },
    async save(workspaceId, body) {
      saved.push(`${workspaceId}:${body.path}`);
      return { path: body.path, version: "v2", size: 0 };
    },
  };
  const buffers: EditorBuffers = createEditorBuffers(io);
  const tabs: Array<{ tabId: string; workspaceId: string; path: string }> = [];
  const closed: string[] = [];
  const hostCalls: string[] = [];
  const screen = { shown: [] as string[], focused: null as string | null };
  let placement: "beside" | "front" | "background" = "beside";
  let next = 1;
  const host: EditorHost = {
    view: () => screen,
    openFile(file, intent) {
      hostCalls.push(`${intent}:${file.workspaceId}:${file.path}`);
      const existing = tabs.find((t) => t.workspaceId === file.workspaceId && t.path === file.path);
      if (existing) return { tabId: existing.tabId, opened: false, shown: intent === "show" ? placement : "background" };
      const tabId = `e${next++}`;
      tabs.push({ tabId, ...file });
      return { tabId, opened: true, shown: intent === "show" ? placement : "background" };
    },
    closeFile(tabId) {
      closed.push(tabId);
      const at = tabs.findIndex((t) => t.tabId === tabId);
      if (at !== -1) tabs.splice(at, 1);
      buffers.close(tabId);
    },
  };
  const surface = createEditorSurface(buffers, host);
  const ask = (op: string, args: Record<string, unknown>, workspaceId = "w1") =>
    surface({ id: "r1", surface: "editor", op, workspace_id: workspaceId, args });
  /** Open a file as the person would have: a tab and a loaded buffer. */
  const have = async (workspaceId: string, path: string): Promise<string> => {
    const placed = host.openFile({ workspaceId, path }, "show");
    await buffers.open(placed.tabId, { workspaceId, path });
    return placed.tabId;
  };
  return {
    buffers, host, ask, have, saved, reads, closed, hostCalls, screen, tabs,
    place: (where: "beside" | "front" | "background") => void (placement = where),
  };
}

const ok = (reply: { ok: boolean; result?: unknown; error?: string }): Record<string, any> => {
  assert.equal(reply.ok, true, `refused: ${"error" in reply ? reply.error : ""}`);
  return (reply as { result: Record<string, any> }).result;
};
const refused = (reply: { ok: boolean; error?: string }): string => {
  assert.equal(reply.ok, false, "this should have been refused");
  return (reply as { error: string }).error;
};

// ── read: what is open ───────────────────────────────────────────────────────

test("with nothing open, there is nothing to list and no file", async () => {
  const { ask } = setup();

  assert.deepEqual(ok(await ask("read", {})), { open: [], file: null });
});

test("the open editors are listed with whether they are on screen, focused and unsaved", async () => {
  const { ask, have, buffers, screen } = setup();
  const a = await have("w1", "src/a.py");
  const b = await have("w1", "src/b.py");
  screen.shown = [a, b];
  screen.focused = b;
  buffers.applyTransactions(a, [(buffers.get(a) as any).state.update({ changes: { from: 0, insert: "# x\n" } })]);

  const { open } = ok(await ask("read", {}));

  assert.deepEqual(
    open.map((e: any) => [e.path, e.dirty, e.in_view, e.focused, e.ai_changed]),
    [
      ["src/a.py", true, true, false, false],
      ["src/b.py", false, true, true, false],
    ],
  );
});

test("a buffer that is not on screen is listed as not in view", async () => {
  const { ask, have } = setup();
  await have("w1", "src/a.py");

  const { open } = ok(await ask("read", {}));

  assert.equal(open[0].in_view, false);
  assert.equal(open[0].focused, false);
});

test("only this workspace's files are listed", async () => {
  const { ask, have } = setup();
  await have("w1", "src/a.py");
  await have("w2", "other.py");

  assert.deepEqual(ok(await ask("read", {}, "w1")).open.map((e: any) => e.path), ["src/a.py"]);
  assert.deepEqual(ok(await ask("read", {}, "w2")).open.map((e: any) => e.path), ["other.py"]);
});

test("a file still loading, or that failed to open, is not listed as an editor", async () => {
  const { ask, have, host, buffers } = setup();
  await have("w1", "src/a.py");
  const dead = host.openFile({ workspaceId: "w1", path: "gone.py" }, "show");
  await buffers.open(dead.tabId, { workspaceId: "w1", path: "gone.py" });

  assert.deepEqual(ok(await ask("read", {})).open.map((e: any) => e.path), ["src/a.py"]);
});

test("the cursor is a 1-based line and column, and a selection says which lines and what text", async () => {
  const { ask, have, buffers } = setup();
  const a = await have("w1", "src/a.py");
  const state = (buffers.get(a) as any).state;
  // "    return 1" is line 2; put the cursor after "    ret" (column 8), then select "return 1".
  const line2 = state.doc.line(2).from;
  buffers.applyTransactions(a, [state.update({ selection: EditorSelection.cursor(line2 + 7) })]);
  assert.deepEqual(ok(await ask("read", {})).open[0].cursor, { line: 2, column: 8 });
  assert.equal(ok(await ask("read", {})).open[0].selection, null);

  buffers.applyTransactions(a, [(buffers.get(a) as any).state.update({ selection: EditorSelection.single(line2 + 4, line2 + 12) })]);
  const { selection } = ok(await ask("read", {})).open[0];

  assert.deepEqual(selection, { from_line: 2, to_line: 2, text: "return 1" });
});

test("a selection across lines names both", async () => {
  const { ask, have, buffers } = setup();
  const a = await have("w1", "src/a.py");
  const state = (buffers.get(a) as any).state;
  buffers.applyTransactions(a, [state.update({ selection: EditorSelection.single(state.doc.line(1).from, state.doc.line(2).to) })]);

  const { selection } = ok(await ask("read", {})).open[0];

  assert.deepEqual([selection.from_line, selection.to_line], [1, 2]);
  assert.equal(selection.text, "def f():\n    return 1");
});

// ── read: one file ───────────────────────────────────────────────────────────

test("a file is read as the editor holds it, unsaved changes and all, numbered from where it starts", async () => {
  const { ask, have, buffers } = setup();
  const a = await have("w1", "src/a.py");
  buffers.edit(a, { oldText: "return 1", newText: "return 100", count: 1 });

  const { file } = ok(await ask("read", { path: "src/a.py" }));

  assert.equal(file.path, "src/a.py");
  assert.equal(file.dirty, true);
  assert.equal(file.ai_changed, true);
  assert.equal(file.from_line, 1);
  assert.equal(file.total_lines, 6);
  assert.equal(file.lines[1], "    return 100", "it read the disk, not the editor");
  assert.equal(file.truncated, false);
});

test("a range is honoured, clamped to the file, and says where it starts", async () => {
  const { ask, have } = setup();
  await have("w1", "src/a.py");

  const mid = ok(await ask("read", { path: "src/a.py", from_line: 5, to_line: 6 })).file;
  const past = ok(await ask("read", { path: "src/a.py", from_line: 5, to_line: 999 })).file;
  const beyond = ok(await ask("read", { path: "src/a.py", from_line: 50 })).file;

  assert.deepEqual([mid.from_line, mid.lines], [5, ["def g():", "    return 2"]]);
  assert.deepEqual(past.lines, ["def g():", "    return 2"]);
  assert.equal(past.truncated, false, "the range ran off the end of the file, which is not text being cut");
  assert.deepEqual([beyond.lines, beyond.total_lines], [[], 6]);
});

test("a long file is cut at 400 lines, and says so, and the rest is reachable by range", async () => {
  const long = Array.from({ length: 1000 }, (_, i) => `line ${i + 1}`).join("\n") + "\n";
  const { ask, have } = setup({ "w1:long.txt": long });
  await have("w1", "long.txt");

  const first = ok(await ask("read", { path: "long.txt" })).file;
  const second = ok(await ask("read", { path: "long.txt", from_line: 401, to_line: 500 })).file;
  const exactly = ok(await ask("read", { path: "long.txt", from_line: 1, to_line: 400 })).file;

  assert.equal(first.lines.length, 400);
  assert.equal(first.total_lines, 1000);
  assert.equal(first.truncated, true);
  assert.equal(second.lines[0], "line 401");
  assert.equal(second.truncated, false);
  assert.equal(exactly.truncated, false, "400 lines were asked for and 400 were given");
});

test("a range wider than the cap is cut and says so", async () => {
  const long = Array.from({ length: 1000 }, (_, i) => `line ${i + 1}`).join("\n") + "\n";
  const { ask, have } = setup({ "w1:long.txt": long });
  await have("w1", "long.txt");

  const file = ok(await ask("read", { path: "long.txt", from_line: 10, to_line: 900 })).file;

  assert.equal(file.lines.length, 400);
  assert.equal(file.truncated, true);
});

test("a file that is not open is not read from the disk behind the person's back", async () => {
  const { ask, reads } = setup();

  const why = refused(await ask("read", { path: "src/a.py" }));

  assert.match(why, /not open/);
  assert.match(why, /open_in_editor|read_file/);
  assert.deepEqual(reads, [], "the editor tool went to the disk");
});

test("another workspace's file is not reachable by naming it", async () => {
  const { ask, have } = setup();
  await have("w2", "other.py");

  assert.match(refused(await ask("read", { path: "other.py" }, "w1")), /not open/);
});

test("a file with no final newline, and an empty one, count their lines as a person would", async () => {
  const { ask, have } = setup({ "w1:a.txt": "one\ntwo", "w1:e.txt": "" });
  await have("w1", "a.txt");
  await have("w1", "e.txt");

  assert.equal(ok(await ask("read", { path: "a.txt" })).file.total_lines, 2);
  const empty = ok(await ask("read", { path: "e.txt" })).file;
  assert.deepEqual([empty.total_lines, empty.lines], [0, []]);
});

// ── open ─────────────────────────────────────────────────────────────────────

test("opening a file makes its tab, loads it, selects the lines and says where it went", async () => {
  const { ask, buffers, place, hostCalls } = setup();
  place("beside");

  const result = ok(await ask("open", { path: "src/a.py", line: 2, end_line: 2 }));

  assert.deepEqual(result, { path: "src/a.py", opened: true, shown: "beside", from_line: 2, to_line: 2 });
  assert.deepEqual(hostCalls, ["show:w1:src/a.py"]);
  const b = buffers.find("w1", "src/a.py");
  assert.equal(b?.status, "ready");
  assert.equal(b?.state.sliceDoc(b.state.selection.main.from, b.state.selection.main.to), "    return 1");
});

test("opening says it went to the front or the background when that is where the host put it", async () => {
  const { ask, place } = setup();

  place("front");
  assert.equal(ok(await ask("open", { path: "src/a.py" })).shown, "front");
  place("background");
  assert.equal(ok(await ask("open", { path: "src/b.py" })).shown, "background");
});

test("opening a file already open says it was already open and selects the lines in the one tab", async () => {
  const { ask, have, tabs } = setup();
  await have("w1", "src/a.py");

  const result = ok(await ask("open", { path: "src/a.py", line: 5, end_line: 6 }));

  assert.equal(result.opened, false);
  assert.deepEqual([result.from_line, result.to_line], [5, 6]);
  assert.equal(tabs.length, 1);
});

test("opening without a line shows the file and selects nothing", async () => {
  const { ask } = setup();

  const result = ok(await ask("open", { path: "src/a.py" }));

  assert.equal("from_line" in result && result.from_line !== null && result.from_line !== undefined, false);
});

test("lines past the end are clamped to the file, and the answer says what was selected", async () => {
  const { ask } = setup();

  const result = ok(await ask("open", { path: "src/a.py", line: 5, end_line: 999 }));

  assert.deepEqual([result.from_line, result.to_line], [5, 6]);
});

test("a file that cannot be opened is refused in the file's own words, and no dead tab is left behind", async () => {
  const { ask, tabs, closed } = setup();

  const why = refused(await ask("open", { path: "nope.py" }));

  assert.match(why, /no file/);
  assert.deepEqual(tabs, []);
  assert.equal(closed.length, 1);
});

test("opening a file the person already had open never closes it", async () => {
  const { ask, have, closed } = setup();
  await have("w1", "src/a.py");

  ok(await ask("open", { path: "src/a.py", line: 1 }));

  assert.deepEqual(closed, []);
});

test("a file the person has open whose buffer failed to load is not closed under them when a re-open fails too", async () => {
  const { ask, host, buffers, tabs, closed } = setup();
  const dead = host.openFile({ workspaceId: "w1", path: "gone.py" }, "show");
  await buffers.open(dead.tabId, { workspaceId: "w1", path: "gone.py" });
  assert.equal(buffers.get(dead.tabId)?.status, "error");

  const why = refused(await ask("open", { path: "gone.py" }));

  assert.match(why, /no file/);
  assert.deepEqual(closed, [], "a tab the person already had was closed by the assistant");
  assert.equal(tabs.length, 1);
});

// ── edit ─────────────────────────────────────────────────────────────────────

test("editing an open file changes the buffer and says where, and that it is unsaved", async () => {
  const { ask, have, buffers, saved } = setup();
  const a = await have("w1", "src/a.py");

  const result = ok(await ask("edit", { path: "src/a.py", old_text: "return 1", new_text: "return 10", count: 1 }));

  assert.deepEqual(result, { path: "src/a.py", replaced: 1, from_line: 2, to_line: 2, opened: false, dirty: true });
  assert.equal((buffers.get(a) as any).state.doc.line(2).text, "    return 10");
  assert.deepEqual(saved, [], "an edit reached the disk");
});

test("editing a file that is not open opens it in the background for the edit, and says it did", async () => {
  const { ask, hostCalls, buffers } = setup();

  const result = ok(await ask("edit", { path: "src/b.py", old_text: "b", new_text: "B", count: 1 }));

  assert.equal(result.opened, true);
  assert.deepEqual(hostCalls, ["background:w1:src/b.py"], "an edit must not move the person");
  assert.equal(buffers.find("w1", "src/b.py")?.state.doc.toString(), "B\n");
});

test("an edit that does not apply changes nothing, says why, and closes the tab it opened to try", async () => {
  const { ask, tabs, closed, buffers } = setup();

  const why = refused(await ask("edit", { path: "src/b.py", old_text: "zzz", new_text: "x", count: 1 }));

  assert.match(why, /not in/);
  assert.deepEqual(tabs, []);
  assert.equal(closed.length, 1);
  assert.equal(buffers.find("w1", "src/b.py"), undefined);
});

test("an edit that does not apply to a file the person already had open leaves it open and untouched", async () => {
  const { ask, have, closed, buffers } = setup();
  const a = await have("w1", "src/a.py");

  const why = refused(await ask("edit", { path: "src/a.py", old_text: "zzz", new_text: "x", count: 1 }));

  assert.match(why, /not in/);
  assert.deepEqual(closed, []);
  assert.equal((buffers.get(a) as any).dirty, false);
});

test("editing a file that does not exist is refused in the file's words and leaves no tab", async () => {
  const { ask, tabs } = setup();

  assert.match(refused(await ask("edit", { path: "nope.py", old_text: "a", new_text: "b", count: 1 })), /no file/);
  assert.deepEqual(tabs, []);
});

test("another workspace's file cannot be edited by naming it, and is not opened either", async () => {
  const { ask, have, buffers } = setup();
  const other = await have("w2", "other.py");

  // Asked about w1, naming a path that only exists in w2: w1 has no such file.
  const why = refused(await ask("edit", { path: "other.py", old_text: "o", new_text: "O", count: 1 }, "w1"));

  assert.match(why, /no file/);
  assert.equal((buffers.get(other) as any).state.doc.toString(), "o\n");
});

test("an edit that does not say how many occurrences means exactly one, so an ambiguous one is refused", async () => {
  const { ask, have, buffers } = setup({ "w1:a.txt": "x y x z x\n" });
  const a = await have("w1", "a.txt");

  const why = refused(await ask("edit", { path: "a.txt", old_text: "x", new_text: "q" }));

  assert.match(why, /3 times/);
  assert.equal((buffers.get(a) as any).state.doc.toString(), "x y x z x\n");
});

test("a count of every occurrence replaces all of them in one go", async () => {
  const { ask, have, buffers } = setup({ "w1:a.txt": "x y x z x\n" });
  const a = await have("w1", "a.txt");

  const result = ok(await ask("edit", { path: "a.txt", old_text: "x", new_text: "q", count: 0 }));

  assert.equal(result.replaced, 3);
  assert.equal((buffers.get(a) as any).state.doc.toString(), "q y q z q\n");
});

test("what it changed is marked as the assistant's, and is the buffer's, not the disk's", async () => {
  const { ask, have, buffers } = setup();
  const a = await have("w1", "src/a.py");

  ok(await ask("edit", { path: "src/a.py", old_text: "return 2", new_text: "return 20", count: 1 }));

  const b = buffers.get(a) as any;
  assert.equal(b.aiChanged, true);
  assert.equal(b.aiRanges.length, 1);
  assert.equal(b.diskText, A, "the disk text moved with the edit");
});

// ── the whole surface ────────────────────────────────────────────────────────

test("an operation that does not exist is refused, not guessed at", async () => {
  const { ask } = setup();

  assert.match(refused(await ask("save", { path: "src/a.py" })), /no operation|not an operation|unknown/i);
});

test("no operation reaches the disk: not read, not written, not saved", async () => {
  const { ask, have, saved, reads } = setup();
  await have("w1", "src/a.py");
  const before = reads.length;

  await ask("read", { path: "src/a.py" });
  await ask("read", {});
  await ask("open", { path: "src/a.py", line: 1 });
  await ask("edit", { path: "src/a.py", old_text: "return 1", new_text: "return 3", count: 1 });

  assert.deepEqual(saved, []);
  assert.equal(reads.length, before, "an operation on an open file went back to the disk to read it");
});

test("a handler that throws is a refusal with a reason, never a rejected promise", async () => {
  const buffers = createEditorBuffers({ read: async () => ({ content: "", version: "v" }), save: async () => ({ path: "", version: "", size: 0 }) });
  const surface = createEditorSurface(buffers, {
    view: () => {
      throw new Error("the window is on fire");
    },
    openFile: () => ({ tabId: "e1", opened: true, shown: "background" }),
    closeFile: () => {},
  });

  const reply = await surface({ id: "r", surface: "editor", op: "read", workspace_id: "w1", args: {} });

  assert.equal(reply.ok, false);
  assert.match((reply as { error: string }).error, /on fire/);
});
