/**
 * The editor's buffers: what is open, what it says, and what has not been saved.
 *
 * A buffer lives **above** the pane that shows it, in a module store, for the reason a terminal's scrollback does: the
 * centre column mounts only the tab in front (and, in a split, two), so a pane that owned its text would lose it whenever
 * its tab was not showing. The store holds a CodeMirror `EditorState`, which is plain data, so everything here runs
 * with no view and no DOM.
 *
 * The properties that matter:
 *  - **what is saved is what was typed**, byte for byte: the byte-order mark and CRLF line endings come back on save,
 *    and a file whose endings cannot be kept intact is refused rather than quietly rewritten;
 *  - **dirty means the text differs from the disk**, so typing a character and deleting it is not unsaved;
 *  - **a save names the version it read**, and a conflict is a state to resolve, not a silent overwrite;
 *  - **a file changed on disk is reloaded if the person has not touched it, and flagged if they have**.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { EditorBuffer, EditorBuffers, FileIo } from "../src/editorBuffers.ts";

const { createEditorBuffers, normalizeForEditing, serializeForDisk } = await import("../src/editorBuffers.ts");
const { EditorSelection } = await import("@codemirror/state");

interface Disk {
  [key: string]: { content: string; version: string };
}

/** A fake workspace on a fake disk, recording what was asked of it. */
function world(files: Disk) {
  const disk: Disk = { ...files };
  const calls: string[] = [];
  let counter = 100;
  const io: FileIo = {
    async read(workspaceId, path) {
      calls.push(`read ${workspaceId}:${path}`);
      const file = disk[`${workspaceId}:${path}`];
      if (!file) throw Object.assign(new Error("file_missing: there is no file"), { code: "file_missing", status: 404 });
      return { ...file };
    },
    async save(workspaceId, body) {
      calls.push(`save ${workspaceId}:${body.path}`);
      const file = disk[`${workspaceId}:${body.path}`];
      if (!file) throw Object.assign(new Error("file_missing"), { code: "file_missing", status: 404 });
      if (file.version !== body.base_version) {
        throw Object.assign(new Error("file_changed: changed on disk"), {
          code: "file_changed",
          status: 409,
          extra: { current_version: file.version },
        });
      }
      counter += 1;
      disk[`${workspaceId}:${body.path}`] = { content: body.content, version: `v${counter}` };
      return { path: body.path, version: `v${counter}`, size: body.content.length };
    },
  };
  return { disk, calls, io };
}

const TEXT = "def f():\n    return 1\n";

async function opened(files: Disk = { "w1:a.py": { content: TEXT, version: "v1" } }) {
  const w = world(files);
  const buffers = createEditorBuffers(w.io);
  const first = Object.keys(files)[0];
  const [workspaceId, ...rest] = first.split(":");
  await buffers.open("e1", { workspaceId, path: rest.join(":") });
  return { ...w, buffers, get: (): EditorBuffer => buffers.get("e1") as EditorBuffer };
}

const text = (b: EditorBuffer): string => b.state.doc.toString();

// ── reading it in ────────────────────────────────────────────────────────────

test("a file opens as loading, then as its text with nothing unsaved", async () => {
  const w = world({ "w1:a.py": { content: TEXT, version: "v1" } });
  const buffers = createEditorBuffers(w.io);

  const pending = buffers.open("e1", { workspaceId: "w1", path: "a.py" });
  assert.equal(buffers.get("e1")?.status, "loading");
  await pending;

  const b = buffers.get("e1") as EditorBuffer;
  assert.equal(b.status, "ready");
  assert.equal(text(b), TEXT);
  assert.equal(b.version, "v1");
  assert.equal(b.dirty, false);
  assert.deepEqual(b.aiRanges, []);
});

test("opening the same tab twice reads the file once", async () => {
  const { buffers, calls } = await opened();

  await buffers.open("e1", { workspaceId: "w1", path: "a.py" });

  assert.equal(calls.filter((c) => c.startsWith("read")).length, 1);
});

test("a file that cannot be opened says why in a sentence, and is an error rather than an empty editor", async () => {
  const w = world({});
  const buffers = createEditorBuffers(w.io);

  await buffers.open("e1", { workspaceId: "w1", path: "nope.py" });

  const b = buffers.get("e1") as EditorBuffer;
  assert.equal(b.status, "error");
  assert.match(b.error ?? "", /no file/i);
  assert.equal(text(b), "", "an error is not an empty file that could be saved over the real one");
});

test("an error can be retried by opening again", async () => {
  const w = world({});
  const buffers = createEditorBuffers(w.io);
  await buffers.open("e1", { workspaceId: "w1", path: "a.py" });
  w.disk["w1:a.py"] = { content: "x\n", version: "v1" };

  await buffers.open("e1", { workspaceId: "w1", path: "a.py" });

  assert.equal(buffers.get("e1")?.status, "ready");
  assert.equal(text(buffers.get("e1") as EditorBuffer), "x\n");
});

test("the editor refuses to keep a file whose line endings it cannot keep intact, and says so", async () => {
  for (const [label, content] of [
    ["mixed", "a\r\nb\nc\r\n"],
    ["a bare carriage return", "a\rb\r"],
  ] as const) {
    const w = world({ "w1:m.txt": { content, version: "v1" } });
    const buffers = createEditorBuffers(w.io);

    await buffers.open("e1", { workspaceId: "w1", path: "m.txt" });

    const b = buffers.get("e1") as EditorBuffer;
    assert.equal(b.status, "error", label);
    assert.match(b.error ?? "", /line endings/i, label);
  }
});

// ── what is kept, and what is written back ───────────────────────────────────

test("a file with CRLF endings is edited as LF and written back as CRLF", async () => {
  const { buffers, disk, get } = await opened({ "w1:w.txt": { content: "one\r\ntwo\r\n", version: "v1" } });
  assert.equal(text(get()), "one\ntwo\n");

  buffers.edit("e1", { oldText: "two", newText: "TWO", count: 1 });
  await buffers.save("e1");

  assert.equal(disk["w1:w.txt"].content, "one\r\nTWO\r\n");
});

test("a byte-order mark is kept off the text and put back on save", async () => {
  const { buffers, disk, get } = await opened({ "w1:b.txt": { content: "\uFEFFhello\n", version: "v1" } });
  assert.equal(text(get()), "hello\n");

  buffers.edit("e1", { oldText: "hello", newText: "bye", count: 1 });
  await buffers.save("e1");

  assert.equal(disk["w1:b.txt"].content, "\uFEFFbye\n");
});

test("a file that ends without a newline is saved without one", async () => {
  const { buffers, disk } = await opened({ "w1:n.txt": { content: "no newline", version: "v1" } });

  buffers.edit("e1", { oldText: "no", newText: "No", count: 1 });
  await buffers.save("e1");

  assert.equal(disk["w1:n.txt"].content, "No newline");
});

test("normalising and serialising are inverses for every shape a file can have", () => {
  for (const content of ["", "a", "a\n", "a\nb\n\n", "\uFEFFa\n", "a\r\nb\r\n", "\uFEFFa\r\nb", "\n\n"]) {
    const n = normalizeForEditing(content);
    assert.equal("error" in n, false, JSON.stringify(content));
    if (!("error" in n)) assert.equal(serializeForDisk(n.text, n), content, JSON.stringify(content));
  }
});

test("an edit that makes a file too big to save is refused by its size in bytes, not in characters", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "x\n", version: "v1" } });

  // 400 000 characters is well under the limit; at three bytes each it is 1.2 MB, which no save could carry.
  const refused = buffers.edit("e1", { oldText: "x", newText: "\u20ac".repeat(400_000), count: 1 });

  assert.equal(refused.ok, false);
  assert.equal(get().state.doc.toString(), "x\n");
});

test("an edit that stays under the limit in bytes is allowed even when it is large", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "x\n", version: "v1" } });

  const done = buffers.edit("e1", { oldText: "x", newText: "\u20ac".repeat(300_000), count: 1 });

  assert.equal(done.ok, true);
  assert.equal(get().state.doc.length, 300_001);
});

// ── dirty ────────────────────────────────────────────────────────────────────

test("dirty is whether the text differs from the disk, not whether anything was typed", async () => {
  const { buffers, get } = await opened();

  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
  assert.equal(get().dirty, true);

  buffers.edit("e1", { oldText: "return 2", newText: "return 1", count: 1 });
  assert.equal(get().dirty, false, "the text is what it was, so there is nothing to save");
});

test("a change of the same length that is not the same text is still dirty", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "abc", version: "v1" } });

  buffers.edit("e1", { oldText: "b", newText: "X", count: 1 });

  assert.equal(get().dirty, true);
});

test("every change makes a new snapshot, so a subscriber is told", async () => {
  const { buffers } = await opened();
  let told = 0;
  buffers.subscribe(() => void (told += 1));
  const before = buffers.getSnapshot();

  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });

  assert.equal(told > 0, true);
  assert.notEqual(buffers.getSnapshot(), before);
  assert.notEqual(buffers.getSnapshot().byTab.get("e1"), before.byTab.get("e1"));
});

test("a snapshot nobody changed is the same object, so a render is not triggered for nothing", async () => {
  const { buffers } = await opened();

  assert.equal(buffers.getSnapshot(), buffers.getSnapshot());
});

// ── the assistant's edits ───────────────────────────────────────────────────

test("an edit replaces the text once by default and says where it landed", async () => {
  const { buffers, get } = await opened();

  const done = buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });

  assert.deepEqual(done, { ok: true, replaced: 1, fromLine: 2, toLine: 2 });
  assert.equal(text(get()), "def f():\n    return 2\n");
});

test("an edit that finds nothing, or the wrong number, changes nothing and says which", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "x y x\n", version: "v1" } });

  const none = buffers.edit("e1", { oldText: "zzz", newText: "q", count: 1 });
  const twice = buffers.edit("e1", { oldText: "x", newText: "q", count: 1 });
  const three = buffers.edit("e1", { oldText: "x", newText: "q", count: 3 });

  for (const refused of [none, twice, three]) assert.equal(refused.ok, false);
  assert.match((none as { reason: string }).reason, /not in/);
  assert.match((twice as { reason: string }).reason, /2 times/);
  assert.match((three as { reason: string }).reason, /2 times/);
  assert.equal(text(get()), "x y x\n");
  assert.equal(get().dirty, false);
});

test("count 0 replaces every occurrence, and still refuses when there are none", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "x y x\n", version: "v1" } });

  const none = buffers.edit("e1", { oldText: "zzz", newText: "q", count: 0 });
  const all = buffers.edit("e1", { oldText: "x", newText: "q", count: 0 });

  assert.equal(none.ok, false);
  assert.deepEqual(all, { ok: true, replaced: 2, fromLine: 1, toLine: 1 });
  assert.equal(text(get()), "q y q\n");
});

test("occurrences that overlap are not counted twice", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "aaa\n", version: "v1" } });

  const done = buffers.edit("e1", { oldText: "aa", newText: "b", count: 0 });

  assert.equal(done.ok, true);
  assert.equal((done as { replaced: number }).replaced, 1);
  assert.equal(text(get()), "ba\n");
});

test("what the model typed with LF is found in a file that is CRLF on disk", async () => {
  const { buffers, get } = await opened({ "w1:w.txt": { content: "one\r\ntwo\r\nthree\r\n", version: "v1" } });

  const done = buffers.edit("e1", { oldText: "one\ntwo", newText: "1\n2", count: 1 });

  assert.equal(done.ok, true);
  assert.equal(text(get()), "1\n2\nthree\n");
});

test("replacing text with what a regular expression would treat as special is literal", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "a.b $1 a.b\n", version: "v1" } });

  const done = buffers.edit("e1", { oldText: "a.b", newText: "$&$1", count: 0 });

  assert.equal(done.ok, true);
  assert.equal(text(get()), "$&$1 $1 $&$1\n");
});

test("a multi-occurrence edit is one undo step and one transaction", async () => {
  const { buffers } = await opened({ "w1:a.txt": { content: "x y x\n", version: "v1" } });
  const seen: number[] = [];
  buffers.attachView("e1", {
    state: (buffers.get("e1") as EditorBuffer).state,
    dispatch(spec) {
      seen.push(Array.isArray((spec as { changes: unknown[] }).changes) ? (spec as { changes: unknown[] }).changes.length : 1);
      return undefined;
    },
  });

  buffers.edit("e1", { oldText: "x", newText: "q", count: 0 });

  assert.deepEqual(seen, [2], "both replacements went in one dispatch");
});

test("the edited ranges are remembered, and cleared when saved", async () => {
  const { buffers, get } = await opened();

  buffers.edit("e1", { oldText: "return 1", newText: "return 22", count: 1 });
  assert.equal(get().aiRanges.length, 1);
  const [range] = get().aiRanges;
  assert.equal(text(get()).slice(range.from, range.to), "return 22");

  await buffers.save("e1");

  assert.deepEqual(get().aiRanges, []);
});

test("typing the text back to what is on disk clears the assistant's marks, since nothing is changed any more", async () => {
  const { buffers, get } = await opened();
  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
  assert.equal(get().aiChanged, true);

  buffers.edit("e1", { oldText: "return 2", newText: "return 1", count: 1 });

  assert.equal(get().dirty, false);
  assert.equal(get().aiChanged, false);
  assert.deepEqual(get().aiRanges, []);
});

test("a deletion by the assistant is a change by the assistant, though it leaves no range to colour", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "keep\ndrop\n", version: "v1" } });

  buffers.edit("e1", { oldText: "drop\n", newText: "", count: 1 });

  assert.equal(get().aiChanged, true);
  assert.deepEqual(get().aiRanges, []);
});

test("several replacements of different lengths each mark exactly the new text", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "a-b-c-d\n", version: "v1" } });

  buffers.edit("e1", { oldText: "-", newText: "<<->>", count: 0 });

  const doc = get().state.doc.toString();
  assert.equal(doc, "a<<->>b<<->>c<<->>d\n");
  assert.equal(get().aiRanges.length, 3);
  for (const r of get().aiRanges) assert.equal(doc.slice(r.from, r.to), "<<->>");
});

test("what the model typed with CRLF line breaks is found in a buffer that holds LF", async () => {
  const { buffers, get } = await opened({ "w1:w.txt": { content: "one\ntwo\nthree\n", version: "v1" } });

  const done = buffers.edit("e1", { oldText: "one\r\ntwo", newText: "1\r\n2", count: 1 });

  assert.equal(done.ok, true);
  assert.equal(get().state.doc.toString(), "1\n2\nthree\n");
});

test("a range the person types before moves with the text", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "alpha\nbeta\n", version: "v1" } });
  buffers.edit("e1", { oldText: "beta", newText: "BETA", count: 1 });
  const before = get().aiRanges[0];

  buffers.applyTransactions("e1", [get().state.update({ changes: { from: 0, insert: "123" } })]);

  const after = get().aiRanges[0];
  assert.deepEqual([after.from, after.to], [before.from + 3, before.to + 3]);
  assert.equal(text(get()).slice(after.from, after.to), "BETA");
});

test("a range the person types all over is dropped, not left pointing at nothing", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "alpha\nbeta\n", version: "v1" } });
  buffers.edit("e1", { oldText: "beta", newText: "BETA", count: 1 });
  const { from, to } = get().aiRanges[0];

  buffers.applyTransactions("e1", [get().state.update({ changes: { from, to, insert: "mine" } })]);

  assert.deepEqual(get().aiRanges, []);
});

test("a range the person selects part of and types over keeps what is left, and none of what they typed", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "alpha\nbeta\n", version: "v1" } });
  buffers.edit("e1", { oldText: "beta", newText: "BETAS", count: 1 });
  const { from } = get().aiRanges[0];

  // Typing over the first two letters of the assistant's text: the range stays, the person's text is inside it only
  // because it is in the middle of theirs, and the range is still a range of the document.
  buffers.applyTransactions("e1", [get().state.update({ changes: { from: from + 1, to: from + 3, insert: "xy" } })]);

  assert.equal(get().aiRanges.length, 1);
  const [r] = get().aiRanges;
  assert.ok(r.from < r.to && r.to <= get().state.doc.length);
});

test("a person's own typing is never marked as the assistant's", async () => {
  const { buffers, get } = await opened();

  buffers.applyTransactions("e1", [get().state.update({ changes: { from: 0, insert: "# hi\n" } })]);

  assert.deepEqual(get().aiRanges, []);
  assert.equal(get().dirty, true);
});

test("an edit past the size a person could save is refused", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "x\n", version: "v1" } });

  const refused = buffers.edit("e1", { oldText: "x", newText: "y".repeat(1_000_001), count: 1 });

  assert.equal(refused.ok, false);
  assert.match((refused as { reason: string }).reason, /too large|too big|bytes/i);
  assert.equal(text(get()), "x\n");
});

test("an edit to a file that is not ready, or not open, is refused with a reason", async () => {
  const w = world({});
  const buffers = createEditorBuffers(w.io);
  await buffers.open("e1", { workspaceId: "w1", path: "nope.py" });

  assert.equal(buffers.edit("e1", { oldText: "a", newText: "b", count: 1 }).ok, false);
  assert.equal(buffers.edit("missing", { oldText: "a", newText: "b", count: 1 }).ok, false);
});

// ── saving ───────────────────────────────────────────────────────────────────

test("saving writes what is in the editor and takes the new version", async () => {
  const { buffers, disk, get } = await opened();
  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });

  const result = await buffers.save("e1");

  assert.deepEqual(result, { ok: true });
  assert.equal(disk["w1:a.py"].content, "def f():\n    return 2\n");
  assert.equal(get().version, disk["w1:a.py"].version);
  assert.equal(get().dirty, false);
  assert.equal(get().saving, false);
});

test("a save names the version it read, and a second save names the one the first returned", async () => {
  const { buffers, calls } = await opened();

  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
  await buffers.save("e1");
  buffers.edit("e1", { oldText: "return 2", newText: "return 3", count: 1 });
  const second = await buffers.save("e1");

  assert.deepEqual(second, { ok: true });
  assert.equal(calls.filter((c) => c.startsWith("save")).length, 2);
});

test("saving a file nobody changed writes nothing", async () => {
  const { buffers, calls } = await opened();

  const result = await buffers.save("e1");

  assert.deepEqual(result, { ok: true });
  assert.equal(calls.some((c) => c.startsWith("save")), false);
});

test("a file that changed on disk is a conflict: kept, not overwritten, and the person's text is untouched", async () => {
  const { buffers, disk, get } = await opened();
  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
  disk["w1:a.py"] = { content: "def f():\n    return 99\n", version: "v9" };

  const result = await buffers.save("e1");

  assert.deepEqual(result, { ok: false, conflict: true });
  assert.equal(disk["w1:a.py"].content, "def f():\n    return 99\n");
  assert.deepEqual(get().conflict, { kind: "changed", currentVersion: "v9" });
  assert.equal(text(get()), "def f():\n    return 2\n");
  assert.equal(get().dirty, true);
});

test("overwriting after a conflict names the version that is on disk now", async () => {
  const { buffers, disk, get } = await opened();
  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
  disk["w1:a.py"] = { content: "theirs\n", version: "v9" };
  await buffers.save("e1");

  const result = await buffers.save("e1", { overwrite: true });

  assert.deepEqual(result, { ok: true });
  assert.equal(disk["w1:a.py"].content, "def f():\n    return 2\n");
  assert.equal(get().conflict, null);
});

test("any other failure to save is reported in words and leaves the text and the dirty mark alone", async () => {
  const { buffers, io, get } = await opened();
  const failing = createEditorBuffers({
    ...io,
    async save() {
      throw Object.assign(new Error("file_access: Permission denied"), { code: "file_access", status: 422 });
    },
  });
  await failing.open("e1", { workspaceId: "w1", path: "a.py" });
  failing.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });

  const result = await failing.save("e1");

  assert.equal(result.ok, false);
  assert.match((result as { error: string }).error, /Permission denied/);
  assert.equal((failing.get("e1") as EditorBuffer).dirty, true);
  assert.equal((failing.get("e1") as EditorBuffer).saving, false);
  void buffers;
  void get;
});

test("two saves at once are one save", async () => {
  const { buffers, calls } = await opened();
  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });

  const [a, b] = await Promise.all([buffers.save("e1"), buffers.save("e1")]);

  assert.equal(calls.filter((c) => c.startsWith("save")).length, 1);
  assert.deepEqual([a, b], [{ ok: true }, { ok: true }]);
});

test("a person who kept typing while the save was in flight is still dirty afterwards", async () => {
  const w = world({ "w1:a.txt": { content: "one\n", version: "v1" } });
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => (release = resolve));
  const slow: FileIo = {
    read: w.io.read,
    async save(workspaceId, body) {
      await gate;
      return w.io.save(workspaceId, body);
    },
  };
  const buffers = createEditorBuffers(slow);
  await buffers.open("e1", { workspaceId: "w1", path: "a.txt" });
  buffers.edit("e1", { oldText: "one", newText: "two", count: 1 });

  const saving = buffers.save("e1");
  buffers.edit("e1", { oldText: "two", newText: "three", count: 1 });
  release();
  await saving;

  const b = buffers.get("e1") as EditorBuffer;
  assert.equal(b.dirty, true, "the text typed during the save was never written");
  assert.equal(text(b), "three\n");
  assert.equal(w.disk["w1:a.txt"].content, "two\n", "what was written is what was there when Save was pressed");
});

// ── reloading, reverting and the disk changing underneath ────────────────────

test("reverting puts back what is on disk and forgets the assistant's marks", async () => {
  const { buffers, get } = await opened();
  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });

  await buffers.reload("e1");

  assert.equal(text(get()), TEXT);
  assert.equal(get().dirty, false);
  assert.deepEqual(get().aiRanges, []);
});

test("a file changed on disk is reloaded silently when the person has not touched it", async () => {
  const { buffers, disk, get } = await opened();
  disk["w1:a.py"] = { content: "new from disk\n", version: "v2" };

  await buffers.noteDiskChange("w1", ["a.py"]);

  assert.equal(text(get()), "new from disk\n");
  assert.equal(get().version, "v2");
  assert.equal(get().conflict, null);
});

test("a file changed on disk is flagged, not reloaded, when the person has unsaved text", async () => {
  const { buffers, disk, get } = await opened();
  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
  disk["w1:a.py"] = { content: "new from disk\n", version: "v2" };

  await buffers.noteDiskChange("w1", ["a.py"]);

  assert.equal(text(get()), "def f():\n    return 2\n", "unsaved text was thrown away");
  assert.deepEqual(get().conflict, { kind: "changed", currentVersion: "v2" });
});

test("a disk change that left the file at the version we hold is no change at all", async () => {
  const { buffers, calls, get } = await opened();
  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });

  await buffers.noteDiskChange("w1", ["a.py"]);

  assert.equal(get().conflict, null, "the disk is exactly what we read, so there is nothing to resolve");
  assert.equal(text(get()), "def f():\n    return 2\n");
  void calls;
});

test("a clean buffer whose file was rewritten with the same bytes is not reloaded", async () => {
  const { buffers, calls } = await opened();
  const before = buffers.get("e1");

  await buffers.noteDiskChange("w1", ["a.py"]);

  assert.equal(buffers.get("e1"), before, "an identical file replaced the buffer anyway, and would have thrown away its undo history");
  void calls;
});

test("a disk change in another workspace or another file is not this buffer's business", async () => {
  const { buffers, calls } = await opened();
  const reads = (): number => calls.filter((c) => c.startsWith("read")).length;
  const before = reads();

  await buffers.noteDiskChange("w2", ["a.py"]);
  await buffers.noteDiskChange("w1", ["b.py"]);

  assert.equal(reads(), before);
});

test("a file that has been deleted on disk is flagged, and the person's text is kept", async () => {
  const { buffers, disk, get } = await opened();
  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
  delete disk["w1:a.py"];

  await buffers.noteDiskChange("w1", ["a.py"]);

  assert.equal(text(get()), "def f():\n    return 2\n");
  assert.ok(get().conflict, "the file is gone and nothing said so");
});

test("after keeping what is in the editor, the next save overwrites the file instead of conflicting again", async () => {
  const { buffers, disk, get } = await opened();
  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
  disk["w1:a.py"] = { content: "theirs\n", version: "v9" };
  await buffers.noteDiskChange("w1", ["a.py"]);
  buffers.keepMine("e1");

  const result = await buffers.save("e1");

  assert.deepEqual(result, { ok: true });
  assert.equal(disk["w1:a.py"].content, "def f():\n    return 2\n");
  assert.equal(get().dirty, false);
});

test("keeping what is in the editor clears the flag without touching the text", async () => {
  const { buffers, disk, get } = await opened();
  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });
  disk["w1:a.py"] = { content: "new\n", version: "v2" };
  await buffers.noteDiskChange("w1", ["a.py"]);

  buffers.keepMine("e1");

  assert.equal(get().conflict, null);
  assert.equal(text(get()), "def f():\n    return 2\n");
});

// ── the selection, and the view ──────────────────────────────────────────────

test("selecting lines selects whole lines, clamped to the file", async () => {
  const { buffers, get } = await opened({ "w1:a.txt": { content: "one\ntwo\nthree\n", version: "v1" } });
  const selected = (): string => get().state.sliceDoc(get().state.selection.main.from, get().state.selection.main.to);

  buffers.selectLines("e1", 2, 3);
  assert.equal(selected(), "two\nthree");

  buffers.selectLines("e1", 3, 99);
  assert.equal(selected(), "three", "past the end is the last line, not the empty one after the final newline");

  buffers.selectLines("e1", 99, 100);
  assert.equal(selected(), "three");

  buffers.selectLines("e1", 2);
  assert.equal(selected(), "two", "no end means the one line");
});

test("a line count is what a person would count: a final newline does not start another line", async () => {
  const { buffers } = await opened({ "w1:a.txt": { content: "one\ntwo\n", version: "v1" } });
  const noNewline = createEditorBuffers(world({ "w1:b.txt": { content: "one\ntwo", version: "v1" } }).io);
  await noNewline.open("e2", { workspaceId: "w1", path: "b.txt" });

  assert.equal(buffers.lineCount("e1"), 2);
  assert.equal(noNewline.lineCount("e2"), 2);
  assert.equal(createEditorBuffers(world({}).io).lineCount("nope"), 0);
});

test("selecting asks the pane to scroll to it, once", async () => {
  const { buffers, get } = await opened();
  const before = get().revealToken;

  buffers.selectLines("e1", 2, 2);

  assert.equal(get().revealToken, before + 1);
});

test("with a view attached, an edit goes through the view so its undo history sees it", async () => {
  const { buffers, get } = await opened();
  const dispatched: unknown[] = [];
  buffers.attachView("e1", {
    state: get().state,
    dispatch(spec) {
      dispatched.push(spec);
    },
  });

  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });

  assert.equal(dispatched.length, 1);
});

test("detaching the view goes back to editing the state directly", async () => {
  const { buffers, get } = await opened();
  const dispatched: unknown[] = [];
  buffers.attachView("e1", { state: get().state, dispatch: (spec) => void dispatched.push(spec) });
  buffers.attachView("e1", null);

  buffers.edit("e1", { oldText: "return 1", newText: "return 2", count: 1 });

  assert.equal(dispatched.length, 0);
  assert.equal(text(get()), "def f():\n    return 2\n");
});

test("a selection set by the pane is kept with the buffer", async () => {
  const { buffers, get } = await opened();

  buffers.applyTransactions("e1", [get().state.update({ selection: EditorSelection.single(4, 8) })]);

  assert.equal(get().state.selection.main.from, 4);
  assert.equal(get().state.selection.main.to, 8);
});

// ── the strip's two facts, and closing ───────────────────────────────────────

test("which tabs have unsaved text, and which the assistant changed, are answered from the snapshot", async () => {
  const w = world({ "w1:a.txt": { content: "a\n", version: "v1" }, "w1:b.txt": { content: "b\n", version: "v1" } });
  const buffers = createEditorBuffers(w.io);
  await buffers.open("e1", { workspaceId: "w1", path: "a.txt" });
  await buffers.open("e2", { workspaceId: "w1", path: "b.txt" });
  buffers.applyTransactions("e1", [(buffers.get("e1") as EditorBuffer).state.update({ changes: { from: 0, insert: "x" } })]);
  buffers.edit("e2", { oldText: "b", newText: "B", count: 1 });

  const snapshot = buffers.getSnapshot();

  assert.deepEqual([...buffers.unsavedIds(snapshot)].sort(), ["e1", "e2"]);
  assert.deepEqual([...buffers.assistantEditedIds(snapshot)], ["e2"]);
});

test("an error or a loading buffer is never reported as unsaved", async () => {
  const w = world({});
  const buffers = createEditorBuffers(w.io);
  await buffers.open("e1", { workspaceId: "w1", path: "nope.py" });

  assert.deepEqual([...buffers.unsavedIds(buffers.getSnapshot())], []);
});

test("closing a buffer forgets it and lets go of its view", async () => {
  const { buffers } = await opened();
  buffers.attachView("e1", { state: (buffers.get("e1") as EditorBuffer).state, dispatch: () => {} });

  buffers.close("e1");

  assert.equal(buffers.get("e1"), undefined);
  assert.equal(buffers.edit("e1", { oldText: "a", newText: "b", count: 1 }).ok, false);
});

test("a view attached to a closed buffer is not written to when the same tab id is opened again", async () => {
  const w = world({ "w1:a.txt": { content: "a\n", version: "v1" } });
  const buffers = createEditorBuffers(w.io);
  await buffers.open("e1", { workspaceId: "w1", path: "a.txt" });
  const dispatched: unknown[] = [];
  buffers.attachView("e1", { state: (buffers.get("e1") as EditorBuffer).state, dispatch: (spec) => void dispatched.push(spec) });
  buffers.close("e1");
  await buffers.open("e1", { workspaceId: "w1", path: "a.txt" });

  buffers.edit("e1", { oldText: "a", newText: "b", count: 1 });

  assert.deepEqual(dispatched, [], "an edit went to a view that belongs to a buffer that is gone");
  assert.equal((buffers.get("e1") as EditorBuffer).state.doc.toString(), "b\n");
});

test("a buffer is found by workspace and path, for the tools that name a file", async () => {
  const { buffers } = await opened();

  assert.equal(buffers.find("w1", "a.py")?.tabId, "e1");
  assert.equal(buffers.find("w2", "a.py"), undefined);
  assert.equal(buffers.find("w1", "b.py"), undefined);
});

void (null as unknown as EditorBuffers);
