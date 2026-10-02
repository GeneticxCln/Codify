/**
 * Split panes, through the whole App: room, what is remembered, the clipboard drawer, and the details a split has to get right.
 *
 * The first half of the story is `splitApp.test.ts`; the helpers are `splitHarness.ts`. Real xterm panes over the
 * harness's fake shell; jsdom has no layout, so the window's width comes from the harness's `viewport`.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const {
  React, WIDE, SEEDED, STRIP, beat, id, click, commands, chord, split, pressIn, panes, pane, focusedSide, terminalIn, composer, selectedTab, tabNamed, terminalTabs, openShell, openThread, shells, withApp, withChatAndShell, pasteSomething, clipRow,
} = await import("./splitHarness.ts");


// ── room ────────────────────────────────────────────────────────────────────

test("a narrow window gives the room to the split: the left panel gives way first, and both panes are drawn", async () => {
  // 900px at 125% is 45rem: the panel (15) and two panes (44) do not fit, the panes alone do.
  await withChatAndShell(
    async (ctx) => {
      assert.ok(ctx.dom.container.querySelector("main > nav"), "the test had no panel to take away");
      await split(ctx);
      assert.equal(panes(ctx).length, 2, "the panel was kept and a pane dropped");
      assert.ok(ctx.dom.container.querySelector("main > nav") === null, "the panel stayed and squeezed the panes");
      assert.equal(ctx.dom.byLabel("Show left panel").getAttribute("aria-pressed"), "true");
    },
    { viewport: { width: 900, height: 700 }, localStorage: { "codify.uiScale": "125" } },
  );
});

test("pressing Show left panel while a split took its room closes the split, and the panel comes back", async () => {
  await withChatAndShell(
    async (ctx) => {
      await split(ctx);
      assert.ok(ctx.dom.container.querySelector("main > nav") === null);
      await click(ctx, ctx.dom.byLabel("Show left panel"));
      assert.equal(panes(ctx).length, 0, "the split stayed and the two did not fit");
      assert.ok(ctx.dom.container.querySelector("main > nav"), "the panel did not come back");
    },
    { viewport: { width: 900, height: 700 }, localStorage: { "codify.uiScale": "125" } },
  );
});

test("with a drawer open as well, Show left panel closes the drawer and keeps the split", async () => {
  // 1280px at 125% is 64rem: panel + split fit (59); panel + History (20) + split do not (79), the split and drawer alone do (64).
  await withChatAndShell(
    async (ctx) => {
      await split(ctx);
      assert.ok(ctx.dom.container.querySelector("main > nav"), "the panel went for the split alone");
      await click(ctx, ctx.dom.byButton("History"));
      assert.ok(ctx.dom.container.querySelector("main > nav") === null, "the panel stayed beside a drawer and a split");
      assert.equal(panes(ctx).length, 2, "the split lost its second pane to the drawer");
      await click(ctx, ctx.dom.byLabel("Show left panel"));
      assert.ok(ctx.dom.container.querySelector('[aria-label="Close goal history"]') === null, "the drawer is still open");
      assert.equal(panes(ctx).length, 2, "the split was closed before the drawer was");
      assert.ok(ctx.dom.container.querySelector("main > nav"), "the panel did not come back");
    },
    { viewport: { width: 1280, height: 800 }, localStorage: { "codify.uiScale": "125" } },
  );
});

test("a window too narrow even without the panel draws the focused pane, keeps the split, and brings the other back at a smaller scale", async () => {
  // 800px at 125% is 40rem, under the 44 two panes need; at 100% it is 50rem.
  await withChatAndShell(
    async (ctx) => {
      await split(ctx);
      assert.equal(panes(ctx).length, 0, "two panes were drawn without room for them");
      assert.equal(ctx.tabs().filter((t) => t.getAttribute("data-in-split") === "true").length, 2, "the split was forgotten, not just not drawn");
      assert.equal(terminalIn(ctx.dom.container), id("t1"), "the focused pane is not what is on screen");

      try {
        await chord(ctx, "-", "Minus");
        await beat();
        assert.equal(panes(ctx).length, 2, "making the UI smaller did not bring the second pane back");
      } finally {
        await chord(ctx, "0", "Digit0"); // the scale is module state that outlives an app: put it back
      }
    },
    { viewport: { width: 800, height: 700 }, localStorage: { "codify.uiScale": "125" } },
  );
});

test("a pane that comes back when there is room does not take the keyboard from the one in use", async () => {
  await withChatAndShell(
    async (ctx) => {
      await split(ctx);
      assert.equal(panes(ctx).length, 0);
      // Work in the chat: it is the pane on screen and the focused one.
      await click(ctx, tabNamed(ctx, "Conversation:"));
      const box = composer(ctx)!;
      await React.act(async () => {
        box.focus();
      });
      assert.ok(ctx.dom.window.document.activeElement === box);

      try {
        await chord(ctx, "-", "Minus"); // room for both
        await beat();
        assert.equal(panes(ctx).length, 2);
        const active = ctx.dom.window.document.activeElement;
        assert.ok(active && pane(ctx, 0).contains(active) && active.tagName === "TEXTAREA", "the keyboard left the chat's message box");
        assert.ok(!pane(ctx, 1).contains(active), "the terminal that came back took the keyboard");
      } finally {
        await chord(ctx, "0", "Digit0");
      }
    },
    { viewport: { width: 800, height: 700 }, localStorage: { "codify.uiScale": "125" } },
  );
});

// ── what is and is not remembered ───────────────────────────────────────────

test("a split is never written to the tab strip's storage", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    await ctx.settle();
    const everything = JSON.stringify(
      Object.fromEntries(Object.keys(ctx.dom.window.localStorage).map((k) => [k, ctx.dom.window.localStorage.getItem(k)])),
    );
    assert.doesNotMatch(everything, /panes|"split"|focused/i, "a split reached localStorage");
    const pushed = ctx.engineTabs().map((t) => t.payload).join(" ");
    assert.doesNotMatch(pushed, /panes|split/i, "a split reached the engine's tab strip");
  });
});

test("the divider's position is remembered, and read back at the next start", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    const divider = ctx.dom.container.querySelector('[aria-label="Resize panes"]') as HTMLElement;
    await ctx.act(async () => {
      divider.dispatchEvent(new ctx.dom.window.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }));
    });
    assert.equal(ctx.dom.window.localStorage.getItem("codify.splitRatio"), "0.55");
    assert.equal(divider.getAttribute("aria-valuenow"), "55");
  });
  await withChatAndShell(
    async (ctx) => {
      await split(ctx);
      const row = (ctx.dom.container.querySelector('[aria-label="Resize panes"]') as HTMLElement).parentElement as HTMLElement;
      assert.match(row.style.gridTemplateColumns, /0\.3fr/, "the remembered position was not used");
    },
    { localStorage: { "codify.splitRatio": "0.3" } },
  );
});

// ── the clipboard drawer in a split ─────────────────────────────────────────

test("with a chat and a terminal showing, both Insert and Paste work, whichever pane has the focus", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    assert.equal(focusedSide(ctx), "1");
    await pasteSomething(ctx, "npm test");
    await click(ctx, ctx.dom.byLabel("Clipboard history"));
    const row = clipRow(ctx, "npm test");
    const insert = row.querySelector('[aria-label="Insert into the message box"]') as HTMLButtonElement;
    const paste = row.querySelector('[aria-label="Paste into the terminal"]') as HTMLButtonElement;
    assert.equal(insert.disabled, false, "Insert was refused with a chat showing");
    assert.equal(paste.disabled, false, "Paste was refused with a terminal showing");

    // The terminal has the focus now, and Paste reaches it.
    await click(ctx, paste);
    await ctx.settle();
    assert.deepEqual(commands(ctx, "codify_terminal_write").map((w) => [w.terminalId, w.data]), [[id("t1"), "npm test"]]);

    // The chat does not have the focus, and Insert reaches its message box all the same.
    await click(ctx, insert);
    await ctx.settle();
    assert.equal(composer(ctx)!.value, "npm test");
  });
});

test("with two terminals showing, Paste goes to the focused one", async () => {
  await withApp({ viewport: WIDE, shellAnswers: shells(id("a"), id("b")) }, async (ctx) => {
    await openShell(ctx);
    await openShell(ctx);
    await split(ctx);
    await pasteSomething(ctx, "ls");
    await click(ctx, ctx.dom.byLabel("Clipboard history"));
    const paste = clipRow(ctx, "ls").querySelector('[aria-label="Paste into the terminal"]') as HTMLButtonElement;
    const focused = terminalIn(pane(ctx, Number(focusedSide(ctx)) as 0 | 1));
    await click(ctx, paste);
    await ctx.settle();
    assert.deepEqual(commands(ctx, "codify_terminal_write").map((w) => w.terminalId), [focused]);

    const other = panes(ctx).find((p) => p.getAttribute("data-focused") === "false")!;
    await pressIn(ctx, other.querySelector("section") as Element);
    await click(ctx, paste);
    await ctx.settle();
    const ids = commands(ctx, "codify_terminal_write").map((w) => w.terminalId);
    assert.equal(ids.length, 2);
    assert.notEqual(ids[1], focused, "Paste went to the terminal that was not focused");
  });
});

test("a chat alone still has no terminal to paste into, as before", async () => {
  await withApp({ viewport: WIDE, ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    await pasteSomething(ctx, "ls");
    await click(ctx, ctx.dom.byLabel("Clipboard history"));
    const row = clipRow(ctx, "ls");
    assert.equal((row.querySelector('[aria-label="Paste into the terminal"]') as HTMLButtonElement).disabled, true);
    assert.equal((row.querySelector('[aria-label="Insert into the message box"]') as HTMLButtonElement).disabled, false);
  });
});

// ── more of what the split has to get right ─────────────────────────────────

test("with the chat on the right, the transcript and the message box are in the right pane", async () => {
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: shells(id("r1")) }, async (ctx) => {
    await openThread(ctx, "c1");
    await openShell(ctx); // the terminal is now the active tab, and the chat the nearest partner
    await split(ctx);
    assert.equal(terminalIn(pane(ctx, 0)), id("r1"), "the active terminal is not on the left");
    assert.match(pane(ctx, 1).textContent ?? "", /Reply from the first thread/, "the transcript is not in the right pane");
    assert.ok(composer(ctx) && pane(ctx, 1).contains(composer(ctx)), "the message box is not with the chat");
    const box = composer(ctx)!;
    await ctx.dom.fill(box, "from the right");
    await React.act(async () => {
      box.dispatchEvent(new ctx.dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    });
    await ctx.settle();
    assert.deepEqual(ctx.engine.filter((c) => c.method === "POST" && /\/turns$/.test(c.path)).map((c) => c.path), ["/conversations/c1/turns"]);
  });
});

test("Paste reaches the terminal beside the chat while the chat has the focus", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    await pressIn(ctx, pane(ctx, 0).querySelector("div") as Element);
    assert.equal(focusedSide(ctx), "0", "the chat did not take the focus");
    await pasteSomething(ctx, "git status");
    await click(ctx, ctx.dom.byLabel("Clipboard history"));
    await click(ctx, clipRow(ctx, "git status").querySelector('[aria-label="Paste into the terminal"]') as Element);
    await ctx.settle();
    assert.deepEqual(commands(ctx, "codify_terminal_write").map((w) => [w.terminalId, w.data]), [[id("t1"), "git status"]]);
  });
});

test("with two terminals showing there is no message box, so Insert says so", async () => {
  await withApp({ viewport: WIDE, shellAnswers: shells(id("a"), id("b")) }, async (ctx) => {
    await openShell(ctx);
    await openShell(ctx);
    await split(ctx);
    await pasteSomething(ctx, "ls");
    await click(ctx, ctx.dom.byLabel("Clipboard history"));
    const insert = clipRow(ctx, "ls").querySelector('[aria-label="Insert into the message box"]') as HTMLButtonElement;
    assert.equal(insert.disabled, true, "Insert was offered with no message box on screen");
    assert.match(insert.title, /chat thread/);
  });
});

test("a terminal whose shell has exited cannot be pasted into, even beside the chat", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    await ctx.emit("terminal-exit", { id: id("t1"), code: 0 });
    await ctx.settle();
    await pasteSomething(ctx, "ls");
    await click(ctx, ctx.dom.byLabel("Clipboard history"));
    const paste = clipRow(ctx, "ls").querySelector('[aria-label="Paste into the terminal"]') as HTMLButtonElement;
    assert.equal(paste.disabled, true, "a finished shell was offered a paste");
    assert.match(paste.title, /exited/);
  });
});

test("closing a pane's tab lands on the other pane, not on whichever tab was beside it in the strip", async () => {
  // The strip is [a a terminal, another conversation]: closing the terminal would land on the first.
  await withApp(
    { viewport: WIDE, ...SEEDED, shellAnswers: shells(id("c")), storedTabs: STRIP([{ key: "k_sp_1", conversationId: "c2", workspaceId: "ws-a" }]) },
    async (ctx) => {
      await openShell(ctx);
      await openThread(ctx, "c1");
      assert.deepEqual(ctx.tabs().map((t) => (t.getAttribute("aria-label") ?? "").split(":")[0]), ["Conversation", "Terminal", "Conversation"]);
      await click(ctx, terminalTabs(ctx)[0]);
      await split(ctx);
      assert.equal(panes(ctx).length, 2);
      await click(ctx, terminalTabs(ctx)[0]); // focus the terminal pane
      await click(ctx, ctx.dom.container.querySelector('button[aria-label^="Close terminal"]') as Element);
      assert.equal(panes(ctx).length, 0);
      assert.match(selectedTab(ctx), /Refactor the parser/, "it landed on the neighbour in the strip, not on the pane that was left");
    },
  );
});

test("a split with a new terminal opens it in the folder of the tab it is beside, not the composer's", async () => {
  await withApp(
    {
      viewport: WIDE,
      workspaces: [
        { id: "ws-a", name: "Alpha", root_path: "/tmp/e2e-alpha" },
        { id: "ws-b", name: "Beta", root_path: "/tmp/e2e-beta" },
      ],
      shellAnswers: shells(id("f")),
      storedTabs: STRIP([{ key: "k_sp_b", conversationId: "cb", workspaceId: "ws-b" }]),
    },
    async (ctx) => {
      await split(ctx);
      const opened = commands(ctx, "codify_terminal_open");
      assert.equal(opened.length, 1);
      assert.equal(opened[0].workspaceId, "ws-b", "the shell was opened in the composer's folder, not the tab's");
    },
  );
});

test("a split with a new terminal says so when there is no folder to open it in", async () => {
  await withApp({ viewport: WIDE, workspaces: [], ...SEEDED }, async (ctx) => {
    await split(ctx);
    assert.equal(panes(ctx).length, 0);
  });
});

test("the divider is remembered when it is let go, not while it is being dragged", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    const divider = ctx.dom.container.querySelector('[aria-label="Resize panes"]') as HTMLElement;
    const fire = (type: string, clientX: number): Promise<void> =>
      ctx.act(async () => void divider.dispatchEvent(new ctx.dom.window.MouseEvent(type, { bubbles: true, cancelable: true, clientX })));
    await fire("pointerdown", 720);
    await fire("pointermove", 500);
    assert.equal(ctx.dom.window.localStorage.getItem("codify.splitRatio"), null, "a position mid-drag was written");
    await fire("pointerup", 500);
    assert.ok(ctx.dom.window.localStorage.getItem("codify.splitRatio"), "the final position was not remembered");
  });
});

test("a pane that was replaced stays replaced after the split waits for a page and comes back", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    await openThread(ctx, "c2"); // replaces the chat pane
    assert.match(pane(ctx, 0).textContent ?? "", /Reply from the second thread/);
    await click(ctx, ctx.dom.container.querySelector('button[title^="Browser"]') as Element);
    assert.equal(panes(ctx).length, 0);
    await click(ctx, terminalTabs(ctx)[0]);
    assert.equal(panes(ctx).length, 2);
    assert.match(pane(ctx, 0).textContent ?? "", /Reply from the second thread/, "the split came back with the chat it had replaced");
    assert.doesNotMatch(ctx.dom.text(), /Reply from the first thread/);
  });
});

// ── nothing changes without a split ─────────────────────────────────────────

test("without a split the centre column is what it was: one view, no divider, no pane headers", async () => {
  await withChatAndShell(async (ctx) => {
    assert.equal(panes(ctx).length, 0);
    assert.ok(ctx.dom.container.querySelector('[aria-label="Resize panes"]') === null, "a divider is on screen");
    assert.equal(ctx.dom.allByLabel("Close split").length, 0);
    assert.ok(composer(ctx), "the chat view lost its message box");
  });
});
