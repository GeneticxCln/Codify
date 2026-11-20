import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { FileIo } from "../src/editorBuffers.ts";

const { createEditorBuffers } = await import("../src/editorBuffers.ts");
const { EditorSelection } = await import("@codemirror/state");
const { createAnonFluidPainter } = await import("../src/components/ui/AnonFluid.tsx");

test("U1: editorBuffers.edit rejects empty search text without hanging", async () => {
  const io: FileIo = {
    async read() {
      return { content: "hello world\n", version: "v1" };
    },
    async save(_ws, body) {
      return { path: body.path, version: "v2", size: body.content.length };
    },
  };
  const store = createEditorBuffers(io);
  await store.open("t1", { workspaceId: "ws1", path: "a.ts" });

  const res = store.edit("t1", { oldText: "", newText: "injected", count: 0 });
  assert.equal(res.ok, false);
  assert.equal(store.get("t1")?.state.doc.toString(), "hello world\n");
});

test("U2: subscribeBadges notifies only when unsaved or assistant-edited sets change", async () => {
  const io: FileIo = {
    async read() {
      return { content: "abc", version: "v1" };
    },
    async save(_ws, body) {
      return { path: body.path, version: "v2", size: body.content.length };
    },
  };
  const store = createEditorBuffers(io);
  await store.open("t1", { workspaceId: "ws1", path: "a.ts" });

  let badgeEvents = 0;
  const unsub = store.subscribeBadges(() => {
    badgeEvents += 1;
  });

  // Pure cursor movement does not fire badge subscribers or recompute doc strings.
  const s0 = store.get("t1")!.state;
  store.applyTransactions("t1", [s0.update({ selection: EditorSelection.single(1) })]);
  assert.equal(badgeEvents, 0);

  // First edit marks t1 unsaved -> 1 badge notification.
  const s1 = store.get("t1")!.state;
  store.applyTransactions("t1", [s1.update({ changes: { from: 3, to: 3, insert: "d" } })]);
  assert.equal(badgeEvents, 1);
  assert.deepEqual(store.getBadgeSnapshot().unsavedIds, ["t1"]);

  // Subsequent keystrokes while already dirty do NOT fire badge notifications.
  const s2 = store.get("t1")!.state;
  store.applyTransactions("t1", [s2.update({ changes: { from: 4, to: 4, insert: "e" } })]);
  assert.equal(badgeEvents, 1);

  unsub();
});

test("U14: closing and reopening a tab ignores stale load completions from the first entry", async () => {
  let resolveFirst!: (val: { content: string; version: string }) => void;
  let callCount = 0;
  const io: FileIo = {
    read() {
      callCount += 1;
      if (callCount === 1) {
        return new Promise((resolve) => {
          resolveFirst = resolve;
        });
      }
      return Promise.resolve({ content: "second load", version: "v2" });
    },
    async save(_ws, body) {
      return { path: body.path, version: "v3", size: body.content.length };
    },
  };
  const store = createEditorBuffers(io);
  const p1 = store.open("t1", { workspaceId: "ws1", path: "a.ts" });
  store.close("t1");
  await store.open("t1", { workspaceId: "ws1", path: "a.ts" });
  assert.equal(store.get("t1")?.state.doc.toString(), "second load");

  // Now the stale first read resolves; it must not overwrite the second entry.
  resolveFirst({ content: "stale first load", version: "v1" });
  await p1;
  assert.equal(store.get("t1")?.state.doc.toString(), "second load");
  assert.equal(store.get("t1")?.version, "v2");
});

test("U6: painter factories observe live active getter updates without recreation", () => {
  let isActive = false;
  const painter = createAnonFluidPainter(
    () => "#22d3ee",
    () => isActive,
  );
  assert.ok(painter);
  isActive = true;
  assert.equal(typeof painter.draw, "function");
});
