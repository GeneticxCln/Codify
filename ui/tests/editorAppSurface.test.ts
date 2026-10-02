/**
 * The assistant's eyes and hands on the editor, through the whole App: the engine puts a question to the window over the
 * surface bridge, the window answers from what is on screen, and what the person sees changes (or, for an edit, does not move).
 *
 * `editorSurface.test.ts` holds the operations against stand-ins for the window. What only the App can show is the part that
 * is *about the window*: that the window polls at all, that an open lands **beside the conversation the person is looking
 * at and leaves the keyboard where it was**, that it never rearranges a layout the person made, that an edit opens a file
 * in the background and moves nothing, that what the assistant changed is marked on the strip and in the text, and that
 * nothing it does reaches the disk until the person presses Save.
 *
 * Real CodeMirror over the harness's fake engine, which has a disk and can put questions to the window
 * (`AppContext.surface.ask`).
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  MAIN,
  SEEDED,
  beat,
  button,
  chord,
  click,
  cmText,
  composer,
  dotIn,
  editorTabs,
  focusedSide,
  id,
  openFile,
  openShell,
  openThread,
  panes,
  selectedTab,
  shells,
  tabNamed,
  typeAtTop,
  viewOf,
  withEditorApp,
} from "./editorHarness.ts";

// ── the window is there to be asked ──────────────────────────────────────────

test("the window polls the engine for questions, and the polls are not counted among what the app asked for", async () => {
  await withEditorApp({}, async (ctx) => {
    assert.ok(ctx.surface.polls() >= 1, "the window never asked the engine whether it had a question");
    assert.equal(ctx.engine.some((c) => c.path.startsWith("/surfaces/")), false);
  });
});

test("asked what is open with nothing open, it says so", async () => {
  await withEditorApp({}, async (ctx) => {
    const reply = await ctx.surface.ask({ op: "read" });

    assert.equal(reply.ok, true);
    assert.deepEqual(reply.result, { open: [], file: null });
  });
});

test("an operation the editor does not have is refused", async () => {
  await withEditorApp({}, async (ctx) => {
    const reply = await ctx.surface.ask({ op: "save", args: { path: "src/main.py" } });

    assert.equal(reply.ok, false);
    assert.match(reply.error ?? "", /no operation/);
  });
});

test("a question for a surface the window does not have is refused by name", async () => {
  await withEditorApp({}, async (ctx) => {
    const reply = await ctx.surface.ask({ op: "read", surface: "terminal" });

    assert.equal(reply.ok, false);
    assert.match(reply.error ?? "", /terminal/);
  });
});

// ── eyes: what the person is looking at ──────────────────────────────────────

test("asked, it reports the editors that are open, which is on screen, and the text the person has typed and not saved", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");
    await typeAtTop(ctx, "# unsaved\n");

    const reply = await ctx.surface.ask({ op: "read", args: { path: "src/main.py" } });

    assert.equal(reply.ok, true);
    assert.deepEqual(
      reply.result.open.map((e: any) => [e.path, e.dirty, e.in_view, e.focused]),
      [["src/main.py", true, true, true]],
    );
    assert.equal(reply.result.file.lines[0], "# unsaved", "it read the disk, which is not what the person sees");
    assert.equal(reply.result.file.dirty, true);
  });
});

test("an editor that is open but not in front is reported as not in view", async () => {
  await withEditorApp({ ...SEEDED, shellAnswers: shells(id("t1")) }, async (ctx) => {
    await openShell(ctx);
    await openFile(ctx, "main", "src/main.py");
    await click(ctx, tabNamed(ctx, "Terminal:"));

    const reply = await ctx.surface.ask({ op: "read" });

    assert.deepEqual(reply.result.open.map((e: any) => [e.path, e.in_view, e.focused]), [["src/main.py", false, false]]);
  });
});

test("what the person has selected is reported with its lines", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");
    await ctx.act(async () => {
      viewOf(ctx).dispatch({ selection: { anchor: 4, head: 8 } });
    });

    const reply = await ctx.surface.ask({ op: "read" });

    assert.deepEqual(reply.result.open[0].selection, { from_line: 1, to_line: 1, text: "main" });
  });
});

// ── hands that point: where an opened file goes ──────────────────────────────

test("a file the assistant opens goes beside the conversation the person is looking at, and the conversation keeps the keyboard", async () => {
  await withEditorApp({ ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");

    const reply = await ctx.surface.ask({ op: "open", args: { path: "src/main.py", line: 2 } });
    await beat(80);
    await ctx.settle();

    assert.equal(reply.ok, true, reply.error ?? "the window refused");
    assert.deepEqual(reply.result, { path: "src/main.py", opened: true, shown: "beside", from_line: 2, to_line: 2 });
    assert.equal(panes(ctx).length, 2);
    assert.match(cmText(ctx), /def main\(\):/);
    assert.ok(composer(ctx), "the conversation's message box was replaced");
    assert.match(selectedTab(ctx), /^Conversation:/, "the assistant moved the person to the editor");
    assert.equal(focusedSide(ctx), "0", "the conversation lost the focus");
    assert.notEqual(
      ctx.dom.window.document.activeElement?.classList.contains("cm-content"),
      true,
      "the assistant took the keyboard from the person",
    );
  });
});

test("the lines it points at are selected in the editor", async () => {
  await withEditorApp({ ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");

    await ctx.surface.ask({ op: "open", args: { path: "src/main.py", line: 2, end_line: 2 } });
    await beat(80);
    await ctx.settle();

    const { from, to } = viewOf(ctx).state.selection.main;
    assert.equal(viewOf(ctx).state.sliceDoc(from, to), "    return 1");
  });
});

test("a split the person already has is not rearranged: the file opens in the background and its tab says so", async () => {
  await withEditorApp({ ...SEEDED, shellAnswers: shells(id("t1")) }, async (ctx) => {
    await openShell(ctx);
    await openThread(ctx, "c1");
    await chord(ctx, ".", "Period");
    await beat(120);
    await ctx.settle();
    assert.equal(panes(ctx).length, 2);

    const reply = await ctx.surface.ask({ op: "open", args: { path: "src/main.py" } });
    await beat(60);

    assert.equal(reply.result.shown, "background");
    assert.equal(panes(ctx).length, 2, "the person's split was replaced");
    assert.equal(ctx.dom.container.querySelectorAll(".cm-content").length, 0);
    assert.equal(editorTabs(ctx).length, 1, "the file was not opened at all");
  });
});

test("with a terminal in front, the file opens in the background and the person is not moved", async () => {
  await withEditorApp({ shellAnswers: shells(id("t1")) }, async (ctx) => {
    await openShell(ctx);
    const before = selectedTab(ctx);

    const reply = await ctx.surface.ask({ op: "open", args: { path: "src/main.py" } });
    await beat(60);
    await ctx.settle();

    assert.equal(reply.result.shown, "background");
    assert.equal(selectedTab(ctx), before);
    assert.equal(editorTabs(ctx).length, 1);
  });
});

test("with another editor in front, the file opens in the background and the person is not moved", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "util", "src/util.py");
    const before = selectedTab(ctx);

    const reply = await ctx.surface.ask({ op: "open", args: { path: "src/main.py" } });
    await ctx.settle();

    assert.equal(reply.result.shown, "background");
    assert.equal(selectedTab(ctx), before);
    assert.equal(editorTabs(ctx).length, 2);
  });
});

test("opening a file the person is already looking at says it is in front, and selects the lines in it", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");

    const reply = await ctx.surface.ask({ op: "open", args: { path: "src/main.py", line: 1 } });
    await ctx.settle();

    assert.deepEqual([reply.result.opened, reply.result.shown], [false, "front"]);
    assert.equal(editorTabs(ctx).length, 1);
  });
});

test("when the window is too narrow for two panes, the file opens in the background instead", async () => {
  await withEditorApp({ ...SEEDED, viewport: { width: 700, height: 900 } }, async (ctx) => {
    await openThread(ctx, "c1");

    const reply = await ctx.surface.ask({ op: "open", args: { path: "src/main.py" } });
    await ctx.settle();

    assert.equal(reply.result.shown, "background");
    assert.equal(panes(ctx).length, 0);
    assert.match(selectedTab(ctx), /^Conversation:/);
  });
});

test("a file that does not exist is refused in the engine's words and leaves no tab", async () => {
  await withEditorApp({ ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");

    const reply = await ctx.surface.ask({ op: "open", args: { path: "src/nope.py" } });
    await ctx.settle();

    assert.equal(reply.ok, false);
    assert.match(reply.error ?? "", /no file/);
    assert.equal(editorTabs(ctx).length, 0);
    assert.equal(panes(ctx).length, 0, "a split was made for a file that is not there");
  });
});

// ── hands that change: an edit moves nothing and saves nothing ───────────────

test("an edit to a file that is not open opens it in the background, changes its text, and marks the tab", async () => {
  await withEditorApp({ ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    const before = selectedTab(ctx);

    const reply = await ctx.surface.ask({
      op: "edit",
      args: { path: "src/main.py", old_text: "return 1", new_text: "return 2", count: 1 },
    });
    await beat(40);
    await ctx.settle();

    assert.equal(reply.ok, true, reply.error ?? "the window refused");
    assert.deepEqual(reply.result, { path: "src/main.py", replaced: 1, from_line: 2, to_line: 2, opened: true, dirty: true });
    assert.equal(selectedTab(ctx), before, "an edit moved the person");
    assert.equal(panes(ctx).length, 0, "an edit rearranged the layout");
    const tab = editorTabs(ctx)[0]!;
    assert.equal(dotIn(tab, "assistant"), true);
    assert.equal(dotIn(tab, "unsaved"), true);
    assert.match(tab.getAttribute("aria-label") ?? "", /changed by the assistant/);
  });
});

test("an edit reaches the disk only when the person saves", async () => {
  await withEditorApp({}, async (ctx) => {
    await ctx.surface.ask({ op: "edit", args: { path: "src/main.py", old_text: "return 1", new_text: "return 2", count: 1 } });
    await ctx.settle();
    assert.equal(ctx.disk.read("ws-a", "src/main.py"), MAIN);
    assert.deepEqual(ctx.disk.saves, []);

    await click(ctx, tabNamed(ctx, "Editor:"));
    await beat(60);
    await ctx.settle();
    assert.match(cmText(ctx), /return 2/);
    assert.equal(ctx.dom.container.querySelectorAll(".cm-ai-edit").length, 1, "the assistant's text was not marked");
    assert.equal(ctx.disk.read("ws-a", "src/main.py"), MAIN, "looking at the tab saved it");

    await ctx.dom.click(button(ctx, "Save") as HTMLElement);
    await beat(40);
    await ctx.settle();

    assert.equal(ctx.disk.read("ws-a", "src/main.py"), "def main():\n    return 2\n");
    assert.equal(ctx.disk.saves.length, 1);
    assert.equal(dotIn(editorTabs(ctx)[0]!, "assistant"), false, "saving left the tab marked as the assistant's");
  });
});

test("an edit to a file on screen appears there, marked", async () => {
  await withEditorApp({ ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    await ctx.surface.ask({ op: "open", args: { path: "src/main.py" } });
    await beat(80);
    await ctx.settle();

    await ctx.surface.ask({ op: "edit", args: { path: "src/main.py", old_text: "return 1", new_text: "return 99", count: 1 } });
    await beat(40);
    await ctx.settle();

    assert.match(cmText(ctx), /return 99/);
    assert.equal(ctx.dom.container.querySelector(".cm-ai-edit")?.textContent, "return 99");
  });
});

test("an edit that does not apply says why, changes nothing, and leaves no tab behind", async () => {
  await withEditorApp({}, async (ctx) => {
    const reply = await ctx.surface.ask({
      op: "edit",
      args: { path: "src/main.py", old_text: "this text is not there", new_text: "x", count: 1 },
    });
    await ctx.settle();

    assert.equal(reply.ok, false);
    assert.match(reply.error ?? "", /not in/);
    assert.equal(editorTabs(ctx).length, 0);
    assert.deepEqual(ctx.disk.saves, []);
  });
});

test("an ambiguous edit is refused unless it says how many, and then changes every one", async () => {
  await withEditorApp({ files: { "ws-a:a.txt": "x y x\n" } }, async (ctx) => {
    const refused = await ctx.surface.ask({ op: "edit", args: { path: "a.txt", old_text: "x", new_text: "q" } });
    assert.equal(refused.ok, false);
    assert.match(refused.error ?? "", /2 times/);

    const all = await ctx.surface.ask({ op: "edit", args: { path: "a.txt", old_text: "x", new_text: "q", count: 0 } });
    assert.equal(all.ok, true);
    assert.equal(all.result.replaced, 2);
  });
});

test("the assistant's edit lands on top of the person's unsaved typing, and neither is lost", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");
    await typeAtTop(ctx, "# mine\n");

    const reply = await ctx.surface.ask({
      op: "edit",
      args: { path: "src/main.py", old_text: "return 1", new_text: "return 2", count: 1 },
    });
    await ctx.settle();

    assert.equal(reply.ok, true, reply.error ?? "the window refused");
    assert.equal(reply.result.from_line, 3, "the line is where it is now, below what the person added");
    assert.equal(viewOf(ctx).state.doc.toString(), "# mine\ndef main():\n    return 2\n");
  });
});

// ── the loop outlives the questions ──────────────────────────────────────────

test("several questions in a row are all answered, in order", async () => {
  await withEditorApp({}, async (ctx) => {
    const first = await ctx.surface.ask({ op: "read" });
    const second = await ctx.surface.ask({ op: "open", args: { path: "src/util.py" } });
    const third = await ctx.surface.ask({ op: "read" });

    assert.deepEqual([first.ok, second.ok, third.ok], [true, true, true]);
    assert.equal(third.result.open.length, 1);
  });
});

test("a refused question does not stop the window answering the next", async () => {
  await withEditorApp({}, async (ctx) => {
    const refused = await ctx.surface.ask({ op: "open", args: { path: "nope.py" } });
    const next = await ctx.surface.ask({ op: "read" });

    assert.equal(refused.ok, false);
    assert.equal(next.ok, true);
  });
});
