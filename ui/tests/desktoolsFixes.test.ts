import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { SurfaceRequest } from "../src/types.ts";
import type { FileIo } from "../src/editorBuffers.ts";

const { createMachineScreens, loadHeadless } = await import("../src/machineScreens.ts");
const { createSurfaceRegistry, runSurfaceLoop } = await import("../src/surfaceLoop.ts");
const { pasteInto } = await import("../src/terminalPaste.ts");
const { classifyBrowserAddress } = await import("../src/browserDispatch.ts");
const { createEditorBuffers } = await import("../src/editorBuffers.ts");
const { tabForFile, emptyTabs, openEditorTab } = await import("../src/tabs.ts");

test("DS-1: MachineScreens.read/flushed resolves under continuous 5ms output without livelock", async () => {
  const s = createMachineScreens(loadHeadless);
  s.open("m1");
  s.write("m1", "boot\r\n");
  await s.flushed("m1");

  let count = 0;
  const spam = setInterval(() => {
    count += 1;
    s.write("m1", `line ${count}\r\n`);
  }, 5);
  try {
    const snap = await Promise.race([
      s.read("m1", 10),
      new Promise<null>((_, reject) => setTimeout(() => reject(new Error("livelock timeout")), 1500)),
    ]);
    assert.ok(snap, "expected snapshot while continuous output is streaming");
  } finally {
    clearInterval(spam);
    s.close("m1");
  }
});

test("DS-1: runSurfaceLoop dispatches independent surfaces concurrently without head-of-line blocking", async () => {
  const registry = createSurfaceRegistry();
  let releaseMachine!: () => void;
  const order: string[] = [];

  registry.register("machine", async (req) => {
    order.push(`start:${req.id}`);
    await new Promise<void>((resolve) => {
      releaseMachine = resolve;
    });
    order.push(`end:${req.id}`);
    return { ok: true, result: "m" };
  });
  registry.register("editor", async (req) => {
    order.push(`run:${req.id}`);
    return { ok: true, result: "e" };
  });

  const controller = new AbortController();
  const posted: string[] = [];
  const queue: Array<SurfaceRequest | null> = [
    { id: "m1", surface: "machine", op: "run", workspace_id: "w1", args: {} },
    { id: "e1", surface: "editor", op: "read", workspace_id: "w1", args: {} },
  ];
  let idx = 0;
  const io = {
    async next(_wait: number, signal: AbortSignal): Promise<SurfaceRequest | null> {
      const next = queue[idx++];
      if (next !== undefined) return next;
      await new Promise<void>((_, reject) =>
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))),
      );
      return null;
    },
    async answer(body: { id: string }): Promise<void> {
      posted.push(body.id);
    },
    async sleep(): Promise<void> {},
    now: () => 1_000_000,
  };

  const loop = runSurfaceLoop(io, registry, controller.signal);
  for (let i = 0; i < 20; i += 1) await new Promise<void>((r) => setImmediate(r));
  // Editor answered BEFORE machine was released!
  assert.deepEqual(posted, ["e1"]);
  releaseMachine();
  for (let i = 0; i < 20; i += 1) await new Promise<void>((r) => setImmediate(r));
  controller.abort();
  await loop;
  assert.deepEqual(posted, ["e1", "m1"]);
  assert.deepEqual(order, ["start:m1", "run:e1", "end:m1"]);
});

test("DS-2: pasteInto strips bracketed-paste breakouts and raw control bytes", () => {
  const pasted: string[] = [];
  const term = {
    paste: (t: string) => void pasted.push(t),
    focus: () => {},
    modes: { bracketedPasteMode: true },
  };
  assert.equal(pasteInto(term, "echo hi\x1b[201~rm -rf /\n\x9b201~id\x03", false), "pasted");
  assert.equal(pasted[0], "echo hirm -rf /\nid");
});

test("DS-8: editorBuffers.find and tabForFile normalize ./ and // relative paths", async () => {
  const io: FileIo = {
    async read() {
      return { content: "x = 1\n", version: "v1" };
    },
    async save(_ws, body) {
      return { path: body.path, version: "v2", size: body.content.length };
    },
  };
  const buffers = createEditorBuffers(io);
  await buffers.open("t1", { workspaceId: "w1", path: "src/app.py" });
  assert.equal(buffers.find("w1", "./src//app.py")?.tabId, "t1");

  const tabs = openEditorTab(emptyTabs, { workspaceId: "w1", path: "src/app.py" }, { show: true, id: "t1" });
  assert.equal(tabForFile(tabs, "w1", "./src//app.py")?.id, "t1");
});

test("DS-10: noteDiskChange applies fetched content in one read without discarding concurrent edits", async () => {
  let reads = 0;
  let releaseRead!: (v: { content: string; version: string }) => void;
  const io: FileIo = {
    read() {
      reads += 1;
      if (reads === 1) return Promise.resolve({ content: "v1\n", version: "v1" });
      return new Promise((resolve) => {
        releaseRead = resolve;
      });
    },
    async save(_ws, body) {
      return { path: body.path, version: "v3", size: body.content.length };
    },
  };
  const buffers = createEditorBuffers(io);
  await buffers.open("t1", { workspaceId: "w1", path: "a.py" });

  const pending = buffers.noteDiskChange("w1", ["a.py"]);
  // User edits while noteDiskChange's read is in flight.
  const s = buffers.get("t1")!.state;
  buffers.applyTransactions("t1", [s.update({ changes: { from: 0, to: 0, insert: "# user\n" } })]);
  releaseRead({ content: "v2\n", version: "v2" });
  await pending;

  assert.equal(reads, 2, "must not issue a second reload read");
  assert.equal(buffers.get("t1")?.state.doc.toString(), "# user\nv1\n");
  assert.equal(buffers.get("t1")?.conflict?.kind, "changed");
});

test("DS-12: browserDispatch refuses non-// schemes and 2-part loopback IPv4 shorthand", () => {
  assert.equal(classifyBrowserAddress("javascript:alert(1)@example.com", "t1").kind, "refuse");
  assert.equal(classifyBrowserAddress("data:80/foo", "t1").kind, "refuse");
  assert.equal(classifyBrowserAddress("http://127.1/", "t1").kind, "refuse");
  assert.equal(classifyBrowserAddress("127.1", "t1").kind, "refuse");
  assert.equal(classifyBrowserAddress("example.com:8080/path", "t1").kind, "shell");
});
