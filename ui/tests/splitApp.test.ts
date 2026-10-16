/**
 * Split panes, through the whole App: making one, refusing one, using it, ending it.
 *
 * `panes.test.ts` is the rules, `splitPanes.test.ts` the divider, `tabMenu.test.ts` the menu. What none of them can show is
 * that `App.tsx` joins them: that a chat and a real xterm are both live in one window, that the transcript stays
 * while the terminal beside it has the focus, that the three ways in all end in the same place, and that closing a
 * pane's tab or opening a browser page does what `docs/09` §12 says. The room, what is remembered and the clipboard drawer
 * are in `splitAppLayout.test.ts`; the helpers are `splitHarness.ts`.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const {
  React, WIDE, SEEDED, id, click, commands, chord, split, rightClick, menuItems, pressIn, panes, pane, focusedSide, terminalIn, screenOf, composer, selectedTab, tabNamed, terminalTabs, openShell, openThread, shells, withApp, withChatAndShell,
} = await import("./splitHarness.ts");
import type { AppContext, AppOptions } from "./splitHarness.ts";


// ── making a split ──────────────────────────────────────────────────────────

test("Ctrl+. shows the nearest terminal beside the chat, and both are live", async () => {
  await withChatAndShell(async (ctx) => {
    assert.equal(panes(ctx).length, 0, "a split was showing before anyone asked");
    assert.equal(await split(ctx), true, "the chord was not claimed");
    assert.equal(panes(ctx).length, 2);
    assert.match(pane(ctx, 0).textContent ?? "", /Reply from the first thread/, "the transcript is not in the left pane");
    assert.ok(composer(ctx) && pane(ctx, 0).contains(composer(ctx)), "the message box is not with the chat");
    assert.equal(terminalIn(pane(ctx, 1)), id("t1"), "the terminal is not in the right pane");

    // Both are real: the shell's output reaches its xterm, and the chat still has its words.
    await ctx.emit("terminal-output", { id: id("t1"), data: "hello-beside-the-chat\r\n" });
    await ctx.settle();
    assert.match(screenOf(ctx, id("t1")), /hello-beside-the-chat/);
    assert.match(ctx.dom.text(), /Reply from the first thread/);
  });
});

test("the pane just asked for is the focused one, the tab strip says so, and it has the keyboard", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    assert.equal(focusedSide(ctx), "1");
    assert.match(selectedTab(ctx), /^Terminal:/);
    const input = pane(ctx, 1).querySelector("textarea");
    assert.ok(input && ctx.dom.window.document.activeElement === input, "the terminal did not take the keyboard");
  });
});

test("both tabs are marked as in the split, and no other", async () => {
  await withChatAndShell(async (ctx) => {
    await click(ctx, ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
    await ctx.settle();
    await split(ctx);
    const marked = ctx.tabs().filter((t) => t.getAttribute("data-in-split") === "true");
    assert.equal(marked.length, 2);
    assert.ok(ctx.tabs().length > 2, "the test had no third tab to be left out");
    assert.equal(ctx.tabs().filter((t) => t.getAttribute("aria-selected") === "true").length, 1, "more than one tab is selected");
  });
});

test("a tab's menu shows it beside the current one", async () => {
  await withChatAndShell(async (ctx) => {
    await rightClick(ctx, terminalTabs(ctx)[0]);
    const [item] = menuItems(ctx);
    assert.equal(item.textContent?.trim(), "Show beside the current tab");
    assert.notEqual(item.getAttribute("aria-disabled"), "true");
    await click(ctx, item);
    await ctx.settle();
    assert.equal(panes(ctx).length, 2);
    assert.equal(terminalIn(pane(ctx, 1)), id("t1"));
    assert.equal(menuItems(ctx).length, 0, "the menu stayed open after it was used");
  });
});

test("the palette shows it too", async () => {
  await withChatAndShell(async (ctx) => {
    await chord(ctx, "k", "KeyK");
    const option = [...ctx.dom.container.querySelectorAll('[role="option"]')].find((o) => /Show beside/.test(o.textContent ?? ""));
    assert.ok(option, "the palette offered no split");
    await click(ctx, option);
    await ctx.settle();
    assert.equal(panes(ctx).length, 2);
    assert.equal(terminalIn(pane(ctx, 1)), id("t1"));
  });
});

test("with no terminal to share with, the split opens a new one in the same folder", async () => {
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: shells(id("new")) }, async (ctx) => {
    await openThread(ctx, "c1");
    await split(ctx);
    assert.equal(panes(ctx).length, 2);
    assert.equal(terminalIn(pane(ctx, 1)), id("new"));
    const opened = commands(ctx, "codify_terminal_open");
    assert.equal(opened.length, 1, "no terminal was opened");
  });
});

test("the tab's own menu offers a split with a new terminal", async () => {
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: shells(id("menu")) }, async (ctx) => {
    await openThread(ctx, "c1");
    await rightClick(ctx, tabNamed(ctx, "Conversation:"));
    const [item] = menuItems(ctx);
    assert.equal(item.textContent?.trim(), "Split with a new terminal");
    await click(ctx, item);
    await ctx.settle();
    assert.equal(terminalIn(pane(ctx, 1)), id("menu"));
  });
});

// ── a browser page or a machine beside this tab, from its menu ──────────────

/** A shell that makes machines: ids in order, and what each open was asked for is in `commands(ctx, "codify_machine_open")`. */
const machines = (...ids: string[]): NonNullable<AppOptions["shellAnswers"]> => {
  let next = 0;
  return {
    codify_machine_open: () => ({ id: ids[Math.min(next++, ids.length - 1)], copy_on_write: true, note: null }),
    codify_machine_resize: () => null,
    codify_machine_write: () => null,
    codify_machine_close: () => null,
  };
};
const itemTitled = (ctx: AppContext, text: RegExp): HTMLElement => {
  const found = menuItems(ctx).find((i) => text.test(i.textContent ?? ""));
  if (!found) throw new Error(`no menu item ${text}: ${menuItems(ctx).map((i) => i.textContent).join(" | ")}`);
  return found;
};

test("the tab's menu offers a browser page and a machine beside it, not only a terminal", async () => {
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: shells(id("menu")) }, async (ctx) => {
    await openThread(ctx, "c1");
    await rightClick(ctx, tabNamed(ctx, "Conversation:"));
    assert.deepEqual(
      menuItems(ctx).map((i) => i.textContent?.trim()),
      [
        "Split with a new terminal",
        "Split with a new browser page",
        "Split with a new machine (no network)",
        "Split with a new machine, with network",
      ],
    );
  });
});

test("Split with a new browser page puts an empty page beside the chat, and the page has the focus", async () => {
  await withApp({ viewport: WIDE, ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    await rightClick(ctx, tabNamed(ctx, "Conversation:"));
    await click(ctx, itemTitled(ctx, /new browser page/));
    await ctx.settle();
    assert.equal(panes(ctx).length, 2, "no split was made");
    assert.match(pane(ctx, 0).textContent ?? "", /Reply from the first thread/, "the chat is not the left pane");
    assert.ok(pane(ctx, 1).querySelector('[data-testid="browser-viewport"]'), "the right pane is not a browser page");
    assert.match(selectedTab(ctx), /^Browser/, "the page was not the focused pane");
  });
});

test("Split with a new machine opens one with no network beside the chat", async () => {
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: machines("mach-1") }, async (ctx) => {
    await openThread(ctx, "c1");
    await rightClick(ctx, tabNamed(ctx, "Conversation:"));
    await click(ctx, itemTitled(ctx, /new machine \(no network\)/));
    await ctx.settle();
    assert.deepEqual(commands(ctx, "codify_machine_open"), [{ workspaceId: "ws-a", cols: 80, rows: 24, network: false }]);
    assert.equal(panes(ctx).length, 2, "no split was made");
    assert.ok(pane(ctx, 1).querySelector('[data-machine-id="mach-1"]'), "the machine is not the right pane");
  });
});

test("Split with a new machine, with network, asks the shell for a network and for nothing else", async () => {
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: machines("mach-1") }, async (ctx) => {
    await openThread(ctx, "c1");
    await rightClick(ctx, tabNamed(ctx, "Conversation:"));
    await click(ctx, itemTitled(ctx, /new machine, with network/));
    await ctx.settle();
    assert.deepEqual(commands(ctx, "codify_machine_open"), [{ workspaceId: "ws-a", cols: 80, rows: 24, network: true }]);
    assert.equal(panes(ctx).length, 2);
  });
});

test("a shell that cannot make a jail leaves no split and says why", async () => {
  await withApp(
    { viewport: WIDE, ...SEEDED, shellFails: { codify_machine_open: "bubblewrap (bwrap) is not installed" } },
    async (ctx) => {
      await openThread(ctx, "c1");
      await rightClick(ctx, tabNamed(ctx, "Conversation:"));
      await click(ctx, itemTitled(ctx, /new machine \(no network\)/));
      await ctx.settle();
      assert.equal(panes(ctx).length, 0, "a split was made around a machine that does not exist");
      const alert = [...ctx.dom.container.querySelectorAll('[role="alert"]')].map((a) => a.textContent).join(" ");
      assert.match(alert, /bubblewrap \(bwrap\) is not installed/);
    },
  );
});

test("on a browser page, a new page beside it is refused with the reason, and a terminal or machine is not", async () => {
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: shells(id("page")) }, async (ctx) => {
    await openThread(ctx, "c1");
    await click(ctx, ctx.dom.container.querySelector('button[title^="Browser"]') as HTMLElement);
    await ctx.settle();
    await rightClick(ctx, tabNamed(ctx, "Browser"));
    const page = itemTitled(ctx, /new browser page/);
    assert.equal(page.getAttribute("aria-disabled"), "true");
    assert.match(ctx.dom.container.querySelector('[role="menu"]')?.textContent ?? "", /Two browser pages can't be side by side yet/);
    for (const other of [/new terminal/, /new machine \(no network\)/, /new machine, with network/]) {
      assert.notEqual(itemTitled(ctx, other).getAttribute("aria-disabled"), "true", `${other} was refused beside a page`);
    }
    await click(ctx, page);
    await ctx.settle();
    assert.equal(panes(ctx).length, 0, "a page was split beside a page");
  });
});

// ── what cannot be split, and why ───────────────────────────────────────────

test("two conversations are refused in the menu, with the reason, and nothing happens", async () => {
  await withApp({ viewport: WIDE, ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    await chord(ctx, "t", "KeyT");
    assert.equal(ctx.tabs().length, 2, "no second chat tab");
    await rightClick(ctx, ctx.tabs()[0]);
    const [item] = menuItems(ctx);
    assert.equal(item.getAttribute("aria-disabled"), "true");
    assert.match(ctx.dom.container.querySelector('[role="menu"]')?.textContent ?? "", /Two conversations can't be side by side yet/);
    await click(ctx, item);
    assert.equal(panes(ctx).length, 0, "a split of two conversations was made");
  });
});

test("the palette does not offer a conversation beside a conversation", async () => {
  await withApp({ viewport: WIDE, ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    await chord(ctx, "t", "KeyT");
    await chord(ctx, "k", "KeyK");
    const offered = [...ctx.dom.container.querySelectorAll('[role="option"]')].map((o) => o.textContent ?? "").filter((t) => /Show beside/.test(t));
    assert.deepEqual(offered, [], "a chat was offered beside a chat");
  });
});

test("with no tab open the chord says so, and the refusal goes when the tab changes", async () => {
  await withApp({ viewport: WIDE, ...SEEDED }, async (ctx) => {
    await split(ctx);
    assert.equal(panes(ctx).length, 0);
    assert.match(ctx.dom.container.querySelector('[role="status"]')?.textContent ?? "", /Open a tab first/);
    await openThread(ctx, "c1");
    assert.ok(!/Open a tab first/i.test(ctx.dom.text()), "the refusal outlived the tab it was about");
  });
});

// ── using it ────────────────────────────────────────────────────────────────

test("the transcript stays on screen, and stays the chat's, while the terminal beside it has the focus", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    assert.equal(focusedSide(ctx), "1");
    assert.match(pane(ctx, 0).textContent ?? "", /Reply from the first thread/, "the focus left the chat's own conversation");
    // The thread's message box still sends to *its* conversation.
    const box = composer(ctx)!;
    await ctx.dom.fill(box, "run the tests");
    await React.act(async () => {
      box.dispatchEvent(new ctx.dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    });
    await ctx.settle();
    const sent = ctx.engine.filter((c) => c.method === "POST" && /\/turns$/.test(c.path));
    assert.deepEqual(sent.map((c) => c.path), ["/conversations/c1/turns"], "the message did not go to the chat's conversation");
  });
});

test("using a pane focuses it, and the tab strip follows", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    assert.equal(focusedSide(ctx), "1");
    await pressIn(ctx, pane(ctx, 0).querySelector("p, div") as Element);
    assert.equal(focusedSide(ctx), "0");
    assert.match(selectedTab(ctx), /^Conversation:/);
    await pressIn(ctx, pane(ctx, 1).querySelector("section") as Element);
    assert.equal(focusedSide(ctx), "1");
    assert.match(selectedTab(ctx), /^Terminal:/);
  });
});

test("focus arriving in the message box focuses the chat pane, as the clipboard drawer's Insert does", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    assert.equal(focusedSide(ctx), "1");
    await React.act(async () => {
      composer(ctx)!.focus();
    });
    assert.equal(focusedSide(ctx), "0");
  });
});

test("opening a thread while the terminal has the focus replaces the chat, never makes two", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    assert.equal(focusedSide(ctx), "1");
    await openThread(ctx, "c2");
    assert.equal(panes(ctx).length, 2);
    assert.match(pane(ctx, 0).textContent ?? "", /Reply from the second thread/, "the new thread is not in the chat pane");
    assert.doesNotMatch(ctx.dom.text(), /Reply from the first thread/);
    assert.equal(terminalIn(pane(ctx, 1)), id("t1"), "the terminal was dropped");
  });
});

test("a terminal opened while two are showing takes the focused pane's place", async () => {
  await withApp({ viewport: WIDE, shellAnswers: shells(id("a"), id("b"), id("c")) }, async (ctx) => {
    await openShell(ctx);
    await openShell(ctx);
    await split(ctx);
    assert.equal(panes(ctx).length, 2);
    const [left, right] = [terminalIn(pane(ctx, 0)), terminalIn(pane(ctx, 1))];
    assert.deepEqual([left, right], [id("b"), id("a")], "the pair is not the two terminals, the active one on the left");
    await openShell(ctx);
    assert.deepEqual([terminalIn(pane(ctx, 0)), terminalIn(pane(ctx, 1))], [id("b"), id("c")], "the new terminal did not replace the focused pane");
    assert.equal(terminalTabs(ctx).length, 3, "a terminal was closed to make room");
  });
});

// ── two terminals ───────────────────────────────────────────────────────────

test("two terminals each show their own output, and each is sized by its own pane", async () => {
  await withApp({ viewport: WIDE, shellAnswers: shells(id("a"), id("b")) }, async (ctx) => {
    await openShell(ctx);
    await openShell(ctx);
    await split(ctx);
    await ctx.emit("terminal-output", { id: id("a"), data: "only-in-a\r\n" });
    await ctx.emit("terminal-output", { id: id("b"), data: "only-in-b\r\n" });
    await ctx.settle();
    assert.match(screenOf(ctx, id("a")), /only-in-a/);
    assert.doesNotMatch(screenOf(ctx, id("a")), /only-in-b/, "one terminal's output drew in the other");
    assert.match(screenOf(ctx, id("b")), /only-in-b/);
    assert.doesNotMatch(screenOf(ctx, id("b")), /only-in-a/);
    const sized = new Set(commands(ctx, "codify_terminal_resize").map((a) => a.terminalId));
    assert.ok(sized.has(id("a")) && sized.has(id("b")), "a pane never sized its own shell");
    // And no tab got a badge for output a pane was showing.
    assert.equal(ctx.dom.container.querySelectorAll('[data-testid="terminal-unread-dot"]').length, 0);
  });
});

// ── ending a split ──────────────────────────────────────────────────────────

test("Ctrl+. again closes it, and the focused pane's tab is still the active one", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    assert.equal(panes(ctx).length, 2);
    await split(ctx);
    assert.equal(panes(ctx).length, 0);
    assert.match(selectedTab(ctx), /^Terminal:/, "closing the split moved the active tab");
    assert.equal(ctx.tabs().length, 2, "closing the split closed a tab");
    assert.equal(ctx.tabs().filter((t) => t.getAttribute("data-in-split")).length, 0, "the strip still says there is a split");
  });
});

test("Close split in a pane, in a tab's menu and in the palette all end it", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    await click(ctx, ctx.dom.allByLabel("Close split")[0]);
    assert.equal(panes(ctx).length, 0, "the pane's button did not end it");

    await split(ctx);
    await rightClick(ctx, tabNamed(ctx, "Conversation:"));
    assert.equal(menuItems(ctx)[0].textContent?.trim(), "Close split");
    await click(ctx, menuItems(ctx)[0]);
    await ctx.settle();
    assert.equal(panes(ctx).length, 0, "the menu did not end it");

    await split(ctx);
    await chord(ctx, "k", "KeyK");
    const option = [...ctx.dom.container.querySelectorAll('[role="option"]')].find((o) => /Close split/.test(o.textContent ?? ""));
    assert.ok(option, "the palette did not offer to close it");
    await click(ctx, option);
    await ctx.settle();
    assert.equal(panes(ctx).length, 0, "the palette did not end it");
  });
});

test("closing a showing pane's tab ends the split and lands on the other pane's tab", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    const closer = ctx.dom.container.querySelector('button[aria-label^="Close terminal"]') as HTMLElement;
    await click(ctx, closer);
    await ctx.settle();
    assert.equal(panes(ctx).length, 0);
    assert.match(selectedTab(ctx), /^Conversation:/, "it did not land on the pane that was left");
    assert.equal(terminalTabs(ctx).length, 0);
    assert.match(ctx.dom.text(), /Reply from the first thread/);
  });
});

test("closing a tab that is in neither pane leaves the split as it was", async () => {
  await withChatAndShell(async (ctx) => {
    await chord(ctx, "t", "KeyT"); // a blank chat tab, now the active one
    await click(ctx, tabNamed(ctx, "Conversation: Refactor the parser"));
    await split(ctx);
    assert.equal(panes(ctx).length, 2);
    const blank = ctx.tabs().find((t) => !t.getAttribute("data-in-split"))!;
    const closer = blank.querySelector("button") as HTMLElement;
    await click(ctx, closer);
    await ctx.settle();
    assert.equal(panes(ctx).length, 2, "closing an unrelated tab ended the split");
  });
});

// ── a page, and coming back ─────────────────────────────────────────────────

test("opening a browser page puts the split to one side, and returning to a pane's tab brings it back", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    const terminalId = terminalIn(pane(ctx, 1));
    await click(ctx, ctx.dom.container.querySelector('button[title^="Browser"]') as HTMLElement);
    await ctx.settle();
    assert.equal(panes(ctx).length, 0, "the page was drawn in a split");
    assert.ok(ctx.dom.container.querySelector('[data-testid="browser-viewport"]'), "the page is not on screen");

    await click(ctx, tabNamed(ctx, "Conversation:"));
    await ctx.settle();
    assert.equal(panes(ctx).length, 2, "the split did not come back");
    assert.equal(terminalIn(pane(ctx, 1)), terminalId, "it came back with a different terminal");
    assert.match(pane(ctx, 0).textContent ?? "", /Reply from the first thread/);
  });
});
