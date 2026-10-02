/**
 * The editor, through the whole App: a person opens a file, edits it, saves it, shows another tab and comes back, and
 * closes it.
 *
 * The rules are tested where they live (`editorBuffers.test.ts` the text, `editorPane.test.ts` the pane, `editorTab.test.ts` the
 * strip). What only the App can show is that they are joined: that Ctrl+K finds a file and opens it into a tab, that the
 * text outlives a visit to another tab, that closing a tab with unsaved text asks, that an editor is never written to the
 * strip's storage, that it sits beside a chat, a terminal or another editor, and that a file the fixer changes reaches an
 * editor that has it open.
 *
 * Real CodeMirror, over the harness's fake engine and its fake disk. jsdom has no layout, so how any of it *looks* is a
 * person's, in a real window.
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
  editorBuffers,
  editorTabs,
  id,
  openFile,
  openPalette,
  openShell,
  openThread,
  pane,
  paletteOption,
  panes,
  queryPalette,
  selectedTab,
  shells,
  tabNamed,
  typeAtTop,
  withEditorApp,
} from "./editorHarness.ts";

const STORAGE_KEY = "CODIFY_TABS";

// ── finding a file ───────────────────────────────────────────────────────────

test("the palette offers no files until there is a query, and then finds them by name", async () => {
  await withEditorApp({}, async (ctx) => {
    await openPalette(ctx);
    await beat(30);
    assert.equal(paletteOption(ctx, "src/main.py"), undefined, "files were listed before anything was typed");

    await queryPalette(ctx, "util");

    assert.ok(paletteOption(ctx, "src/util.py"));
    assert.equal(paletteOption(ctx, "src/main.py"), undefined);
    assert.match(ctx.dom.container.textContent ?? "", /Files/);
  });
});

test("opening a file from the palette puts its text in an editor tab in front", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");

    assert.equal(editorTabs(ctx).length, 1);
    assert.match(selectedTab(ctx), /^Editor: main\.py/);
    assert.match(cmText(ctx), /def main\(\):/);
    assert.match(cmText(ctx), /return 1/);
    assert.equal(ctx.dom.container.querySelectorAll('[data-testid="editor-status"]').length, 1);
  });
});

test("opening the same file again is the same tab", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");
    await openFile(ctx, "main", "src/main.py");

    assert.equal(editorTabs(ctx).length, 1);
  });
});

test("opening a file that is open behind another tab brings its tab to the front", async () => {
  await withEditorApp({ ...SEEDED, shellAnswers: shells(id("t1")) }, async (ctx) => {
    await openShell(ctx);
    await openFile(ctx, "main", "src/main.py");
    await click(ctx, tabNamed(ctx, "Terminal:"));
    assert.match(selectedTab(ctx), /^Terminal:/);

    await openFile(ctx, "main", "src/main.py");

    assert.match(selectedTab(ctx), /^Editor: main\.py/, "the file's own tab was left behind");
    assert.equal(editorTabs(ctx).length, 1);
  });
});

test("a file that does not exist is refused, and no dead tab is left for it", async () => {
  await withEditorApp({ files: { "ws-a:src/main.py": MAIN } }, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");
    ctx.disk.delete("ws-a", "src/main.py");
    // A second tab for a path that is not there: the palette cannot offer it, so it comes from the assistant's side.
    const reply = await ctx.surface.ask({ op: "open", args: { path: "src/gone.py" } });

    assert.equal(reply.ok, false);
    assert.match(reply.error ?? "", /no file/);
    assert.equal(editorTabs(ctx).length, 1, "a dead tab was left for a file that does not exist");
  });
});

// ── editing and saving ───────────────────────────────────────────────────────

test("typing marks the tab unsaved, Save writes the file, and the mark goes", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");
    assert.equal(dotIn(editorTabs(ctx)[0]!, "unsaved"), false);

    await typeAtTop(ctx, "# header\n");
    assert.equal(dotIn(editorTabs(ctx)[0]!, "unsaved"), true);
    assert.equal(ctx.disk.read("ws-a", "src/main.py"), MAIN, "typing reached the disk before Save");

    await ctx.dom.click(button(ctx, "Save") as HTMLElement);
    await beat(40);
    await ctx.settle();

    assert.equal(ctx.disk.read("ws-a", "src/main.py"), "# header\n" + MAIN);
    assert.deepEqual(ctx.disk.saves.map((s) => [s.path, s.base_version]), [["src/main.py", "v1"]]);
    assert.equal(dotIn(editorTabs(ctx)[0]!, "unsaved"), false);
  });
});

test("showing another tab and coming back keeps what was typed", async () => {
  await withEditorApp({ ...SEEDED, shellAnswers: shells(id("t1")) }, async (ctx) => {
    await openShell(ctx);
    await openFile(ctx, "main", "src/main.py");
    await typeAtTop(ctx, "# kept\n");

    await click(ctx, tabNamed(ctx, "Terminal:"));
    assert.equal(ctx.dom.container.querySelectorAll(".cm-content").length, 0, "the editor should be gone while its tab is not in front");
    await click(ctx, tabNamed(ctx, "Editor:"));
    await beat(60);
    await ctx.settle();

    assert.match(cmText(ctx), /# kept/);
    assert.equal(dotIn(editorTabs(ctx)[0]!, "unsaved"), true);
  });
});

// ── closing ──────────────────────────────────────────────────────────────────

test("closing a tab with unsaved text asks first, and declining keeps the tab and the text", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");
    await typeAtTop(ctx, "# precious\n");
    const asked: string[] = [];
    ctx.dom.window.confirm = (message?: string) => {
      asked.push(String(message));
      return false;
    };

    await ctx.dom.click(ctx.dom.container.querySelector('button[aria-label="Close editor: main.py"]') as Element);
    await ctx.settle();

    assert.equal(asked.length, 1);
    assert.match(asked[0]!, /main\.py/);
    assert.match(asked[0]!, /not saved/);
    assert.equal(editorTabs(ctx).length, 1, "the tab closed anyway");
    assert.match(cmText(ctx), /# precious/);
  });
});

test("closing a tab with unsaved text, and agreeing, closes it and drops the buffer without writing anything", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");
    await typeAtTop(ctx, "# gone\n");
    ctx.dom.window.confirm = () => true;

    await ctx.dom.click(ctx.dom.container.querySelector('button[aria-label="Close editor: main.py"]') as Element);
    await beat(60);
    await ctx.settle();

    assert.equal(editorTabs(ctx).length, 0);
    assert.equal(editorBuffers.getSnapshot().byTab.size, 0, "the buffer outlived its tab");
    assert.deepEqual(ctx.disk.saves, []);
    assert.equal(ctx.disk.read("ws-a", "src/main.py"), MAIN);
  });
});

test("closing a tab with nothing unsaved does not ask", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");
    let asked = 0;
    ctx.dom.window.confirm = () => {
      asked += 1;
      return true;
    };

    await ctx.dom.click(ctx.dom.container.querySelector('button[aria-label="Close editor: main.py"]') as Element);
    await ctx.settle();

    assert.equal(asked, 0);
    assert.equal(editorTabs(ctx).length, 0);
  });
});

test("Ctrl+W on an editor with unsaved text asks too, because it is the same close", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");
    await typeAtTop(ctx, "# precious\n");
    let asked = 0;
    ctx.dom.window.confirm = () => {
      asked += 1;
      return false;
    };

    await chord(ctx, "w", "KeyW");

    assert.equal(asked, 1);
    assert.equal(editorTabs(ctx).length, 1);
  });
});

// ── never written down ───────────────────────────────────────────────────────

test("an editor tab is not in the strip's storage, and the engine is never told about it", async () => {
  await withEditorApp({ ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    await openFile(ctx, "main", "src/main.py");
    await typeAtTop(ctx, "# secret draft\n");
    await beat(80);
    await ctx.settle();

    const stored = ctx.dom.window.localStorage.getItem(STORAGE_KEY) ?? "";
    assert.doesNotMatch(stored, /main\.py|editor|secret draft/, "an editor reached CODIFY_TABS");
    assert.deepEqual(
      ctx.engineTabs().map((row) => row.kind),
      ["chat"],
      "the engine was handed a row for an editor",
    );
    assert.equal(
      ctx.engine.some((c) => c.path.startsWith("/shell/tabs") && /main\.py|editor/.test(JSON.stringify(c.body))),
      false,
    );
  });
});

// ── beside the others ────────────────────────────────────────────────────────

test("Ctrl+. on an editor with a conversation open puts them side by side, both live", async () => {
  await withEditorApp({ ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    await openFile(ctx, "main", "src/main.py");

    await chord(ctx, ".", "Period");
    await beat(60);
    await ctx.settle();

    assert.equal(panes(ctx).length, 2);
    assert.ok(composer(ctx), "the conversation's message box is gone");
    assert.match(cmText(ctx), /def main\(\):/);
    assert.equal(pane(ctx, 0).querySelectorAll(".cm-content").length + pane(ctx, 1).querySelectorAll(".cm-content").length, 1);
  });
});

test("an editor and a terminal can share the column", async () => {
  await withEditorApp({ shellAnswers: shells(id("t1")) }, async (ctx) => {
    await openShell(ctx);
    await openFile(ctx, "main", "src/main.py");

    await chord(ctx, ".", "Period");
    await beat(120);
    await ctx.settle();

    assert.equal(panes(ctx).length, 2);
    assert.match(cmText(ctx), /def main\(\):/);
    assert.ok(ctx.dom.container.querySelector("[data-terminal-id]"), "the terminal is not on screen");
  });
});

test("two editors can sit side by side, each with its own file", async () => {
  await withEditorApp({}, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");
    await openFile(ctx, "util", "src/util.py");
    await openPalette(ctx);
    await queryPalette(ctx, "beside");
    const row = [...ctx.dom.container.querySelectorAll<HTMLElement>('[role="option"]')].find((o) => /Show beside: main\.py/.test(o.textContent ?? ""));
    assert.ok(row, "the palette offered no way to put main.py beside util.py");
    await ctx.dom.click(row);
    await beat(80);
    await ctx.settle();

    assert.equal(panes(ctx).length, 2);
    const text = cmText(ctx);
    assert.match(text, /def main\(\):/);
    assert.match(text, /def util\(\):/);
  });
});

test("an editor in front takes the message box's place: there is no composer behind it", async () => {
  await withEditorApp({ ...SEEDED }, async (ctx) => {
    await openFile(ctx, "main", "src/main.py");

    assert.equal(composer(ctx), null, "an editor in front still showed a message box");
  });
});

// ── a file that changes under an open editor ─────────────────────────────────

const RUNNING_GOAL = {
  id: "g1",
  conversation_id: "c1",
  title: "Rewrite main",
  status: "RUNNING",
  mode: "normal",
  version: 3,
  steps: [] as unknown[],
};
const summary = (sequence: number, paths: string[], dry_run = false) => ({
  id: `ev-g1-${sequence}`,
  goal_id: "g1",
  step_id: null,
  type: "file_change_summary",
  payload: { paths, dry_run, unchanged: [] },
  timestamp: sequence,
  sequence,
});
const goalSocket = (ctx: Parameters<Parameters<typeof withEditorApp>[1]>[0]) => {
  const s = ctx.sockets.find((x) => x.url.endsWith("/ws/goals/g1"));
  if (!s) throw new Error(`no stream for g1: ${ctx.sockets.map((x) => x.url).join(", ")}`);
  return s;
};

test("a file the fixer rewrote is reloaded into an editor that has it open and untouched", async () => {
  await withEditorApp({ ...SEEDED, goals: [RUNNING_GOAL] as never }, async (ctx) => {
    await openThread(ctx, "c1");
    await openFile(ctx, "main", "src/main.py");
    ctx.disk.write("ws-a", "src/main.py", "def main():\n    return 'from the fixer'\n");

    await goalSocket(ctx).deliver(summary(5, ["src/main.py"]));
    await beat(60);
    await ctx.settle();

    assert.match(cmText(ctx), /from the fixer/);
    assert.equal(dotIn(editorTabs(ctx)[0]!, "unsaved"), false);
  });
});

test("a file the fixer rewrote under unsaved text is flagged and the person's text is kept", async () => {
  await withEditorApp({ ...SEEDED, goals: [RUNNING_GOAL] as never }, async (ctx) => {
    await openThread(ctx, "c1");
    await openFile(ctx, "main", "src/main.py");
    await typeAtTop(ctx, "# mine\n");
    ctx.disk.write("ws-a", "src/main.py", "def main():\n    return 'from the fixer'\n");

    await goalSocket(ctx).deliver(summary(5, ["src/main.py"]));
    await beat(60);
    await ctx.settle();

    assert.match(cmText(ctx), /# mine/, "the person's text was thrown away");
    assert.doesNotMatch(cmText(ctx), /from the fixer/);
    assert.match(ctx.dom.container.querySelector('[role="alert"]')?.textContent ?? "", /changed on disk/);
  });
});

test("a dry run wrote nothing, so it changes nothing in an editor", async () => {
  await withEditorApp({ ...SEEDED, goals: [RUNNING_GOAL] as never }, async (ctx) => {
    await openThread(ctx, "c1");
    await openFile(ctx, "main", "src/main.py");
    ctx.disk.write("ws-a", "src/main.py", "def main():\n    return 'proposed only'\n");

    await goalSocket(ctx).deliver(summary(5, ["src/main.py"], true));
    await beat(60);
    await ctx.settle();

    assert.match(cmText(ctx), /return 1/);
    assert.doesNotMatch(cmText(ctx), /proposed only/);
  });
});

test("a change to a file no editor has open asks nothing of the engine", async () => {
  await withEditorApp({ ...SEEDED, goals: [RUNNING_GOAL] as never }, async (ctx) => {
    await openThread(ctx, "c1");
    const before = ctx.engine.filter((c) => c.path.endsWith("/file")).length;

    await goalSocket(ctx).deliver(summary(5, ["src/main.py"]));
    await beat(60);
    await ctx.settle();

    assert.equal(ctx.engine.filter((c) => c.path.endsWith("/file")).length, before);
  });
});

// ── the diff card ────────────────────────────────────────────────────────────

const DIFF_GOAL = { ...RUNNING_GOAL, status: "COMPLETED" };
const diffEvent = (sequence: number, path: string) => ({
  id: `ev-g1-${sequence}`,
  goal_id: "g1",
  step_id: null,
  type: "diff",
  payload: { path, unified_diff: `--- a/${path}\n+++ b/${path}\n@@ -1 +1 @@\n-old\n+new\n` },
  timestamp: sequence,
  sequence,
});

test("the Open link on a change opens that file in an editor, in front, from the conversation", async () => {
  await withEditorApp(
    { ...SEEDED, goals: [DIFF_GOAL] as never, goalEvents: { g1: [diffEvent(1, "src/main.py")] } as never },
    async (ctx) => {
      await openThread(ctx, "c1");
      const link = ctx.dom.container.querySelector('button[aria-label="Open src/main.py in the editor"]');
      assert.ok(link, "the diff card has no Open link");

      await ctx.dom.click(link);
      await beat(60);
      await ctx.settle();

      assert.equal(editorTabs(ctx).length, 1);
      assert.match(selectedTab(ctx), /^Editor: main\.py/);
      assert.match(cmText(ctx), /def main\(\):/);
    },
  );
});

test("pressing Open twice does not make a second tab for the same file", async () => {
  await withEditorApp(
    { ...SEEDED, goals: [DIFF_GOAL] as never, goalEvents: { g1: [diffEvent(1, "src/main.py")] } as never },
    async (ctx) => {
      await openThread(ctx, "c1");
      const open = (): Element => ctx.dom.container.querySelector('button[aria-label="Open src/main.py in the editor"]')!;
      await ctx.dom.click(open());
      await beat(60);
      await ctx.settle();
      await click(ctx, tabNamed(ctx, "Conversation:"));
      await ctx.dom.click(open());
      await ctx.settle();

      assert.equal(editorTabs(ctx).length, 1);
      assert.match(selectedTab(ctx), /^Editor: main\.py/, "the second Open did not bring the file's tab forward");
    },
  );
});
