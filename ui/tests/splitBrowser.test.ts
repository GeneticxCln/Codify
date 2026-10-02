/**
 * A browser page in a split, through the whole App: beside a chat, a terminal or an editor, and never beside another page.
 *
 * `panes.test.ts` is the rules (who may pair, who is the partner, what a page arriving from outside does to a split). What it
 * cannot show is the part that is about the window: that the page, which is a native view the shell seats over one
 * rectangle, is **on screen while a chat beside it has the focus** (the shell shows the page it is told, and the active tab is
 * no longer the one that says so); that it is **taken out of the way** while something is drawn over it (the divider being
 * dragged, the palette, a tab's menu), because a native view paints above every DOM overlay and takes the pointer; that a
 * link clicked in an answer is read next to the answer when there is room; and that none of it happens when there is not.
 *
 * jsdom has no native views and no layout, so what the shell is *told* is the evidence: `codify_browser_focus` names the page
 * that is shown (an empty name hides them all). Where the page actually lands is the shell's, measured by its own smoke
 * (`docs/09` §7.3); how a drag feels over a real page is a person's.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const {
  WIDE, SEEDED, THREADS, reply, id, click, commands, chord, split, rightClick, menuItems, pressIn, panes, pane, focusedSide, terminalIn,
  composer, selectedTab, tabNamed, openShell, openThread, shells, withApp, withChatAndShell, beat,
} = await import("./splitHarness.ts");
import type { AppContext } from "./splitHarness.ts";

const address = (ctx: AppContext): HTMLInputElement => ctx.dom.container.querySelector('input[aria-label="Address"]') as HTMLInputElement;
const browserButton = (ctx: AppContext): HTMLElement => ctx.dom.container.querySelector('button[title^="Browser"]') as HTMLElement;
const viewport = (root: Element): Element | null => root.querySelector('[data-testid="browser-viewport"]');

/** Open a browser tab and give it its first address, the way a person does. */
async function seatAPage(ctx: AppContext, url: string): Promise<string> {
  await click(ctx, browserButton(ctx));
  await ctx.settle();
  await ctx.dom.fill(address(ctx), url);
  await ctx.dom.press(address(ctx), "Enter");
  await ctx.settle();
  const opened = commands(ctx, "codify_browser_open");
  return String(opened[opened.length - 1]!.tabId);
}

/** The page the shell was last told to show (an empty name is "none"), or null before it was told anything. */
const shown = (ctx: AppContext): string | null => {
  const all = commands(ctx, "codify_browser_focus");
  return all.length === 0 ? null : String(all[all.length - 1]!.tabId);
};

const LINKED = [
  "See [the guide](https://example.com/guide) for the details.",
].join("\n");
const WITH_LINK = {
  ...SEEDED,
  goalEvents: { g1: [reply("g1", LINKED)], g2: [reply("g2", "Reply from the second thread")] },
};
const linkButton = (ctx: AppContext): HTMLElement =>
  [...ctx.dom.container.querySelectorAll("button")].find((b) => b.getAttribute("title")?.startsWith("Open https://example.com/guide")) as HTMLElement;

// ── making one ───────────────────────────────────────────────────────────────

test("Ctrl+. shows an open page beside the chat, and the page stays on screen while the chat has the focus", async () => {
  await withApp({ viewport: WIDE, ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    const pageId = await seatAPage(ctx, "https://example.com/a");
    await click(ctx, tabNamed(ctx, "Conversation:"));
    assert.equal(shown(ctx), "", "the page stayed on screen over a chat that fills the column");

    assert.equal(await split(ctx), true, "the chord was not claimed");

    assert.equal(panes(ctx).length, 2);
    assert.ok(composer(ctx) && pane(ctx, 0).contains(composer(ctx)), "the message box is not with the chat");
    assert.ok(viewport(pane(ctx, 1)), "the page is not in the right pane");
    assert.equal(shown(ctx), pageId, "the shell was not told to show the page");

    await pressIn(ctx, pane(ctx, 0));
    await ctx.settle();
    assert.equal(focusedSide(ctx), "0");
    assert.match(selectedTab(ctx), /^Conversation:/);
    assert.equal(shown(ctx), pageId, "giving the chat the focus took the page off the screen");
  });
});

test("a tab's menu shows a page beside the current tab", async () => {
  await withApp({ viewport: WIDE, ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    const pageId = await seatAPage(ctx, "https://example.com/a");
    await click(ctx, tabNamed(ctx, "Conversation:"));

    await rightClick(ctx, tabNamed(ctx, "Browser:"));
    const [item] = menuItems(ctx);
    assert.equal(item.textContent?.trim(), "Show beside the current tab");
    assert.notEqual(item.getAttribute("aria-disabled"), "true", "a page was refused a pane");
    await click(ctx, item);
    await ctx.settle();

    assert.equal(panes(ctx).length, 2);
    assert.ok(viewport(pane(ctx, 1)));
    assert.equal(shown(ctx), pageId);
  });
});

test("Ctrl+. on a page with nothing to pair with opens a terminal beside it", async () => {
  await withApp({ viewport: WIDE, shellAnswers: shells(id("b")) }, async (ctx) => {
    await seatAPage(ctx, "https://example.com/a");

    await split(ctx);

    assert.equal(panes(ctx).length, 2, "the chord was refused");
    assert.ok(viewport(pane(ctx, 0)), "the page is not in the left pane");
    assert.equal(terminalIn(pane(ctx, 1)), id("b"));
  });
});

test("another page cannot be shown beside a page, and the menu says why", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    await seatAPage(ctx, "https://example.com/a");
    await seatAPage(ctx, "https://example.com/b");
    const first = ctx.tabs().filter((t) => (t.getAttribute("aria-label") ?? "").startsWith("Browser:"))[0]!;

    await rightClick(ctx, first);
    const [item] = menuItems(ctx);

    assert.equal(item.getAttribute("aria-disabled"), "true");
    assert.match(ctx.dom.container.querySelector('[role="menu"]')?.textContent ?? "", /Two browser pages can't be side by side/);
    await click(ctx, item);
    assert.equal(panes(ctx).length, 0, "two pages were drawn in a split");
  });
});

// ── the page lands where its pane is ────────────────────────────────────────

const resizeWidths = (ctx: AppContext): number[] =>
  commands(ctx, "codify_browser_resize").map((c) => (c.bounds as { width: number }).width);

test("the shell is told where the page's pane is as soon as it is measured, not a tenth of a second later", async () => {
  await withApp({ viewport: WIDE, ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    await seatAPage(ctx, "https://example.com/a");
    await click(ctx, tabNamed(ctx, "Conversation:"));
    await beat(300); // let the page's own measurements settle, so what follows is the split's
    const before = commands(ctx, "codify_browser_resize").length;

    // `chord`, not `split`: `split` waits a beat for xterm's sake, and the point is that the call is made *before* any wait.
    await chord(ctx, ".", "Period");
    const after = commands(ctx, "codify_browser_resize").length;

    assert.ok(after > before, "the page stayed at the size it had when it filled the column, over the chat, until a timer ran");
  });
});

test("a burst of measurements sends the first at once and the last when it settles: a drag is followed, and ends where it ended", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    // The harness's ResizeObserver never fires (jsdom has no layout), so this one keeps its callbacks to be called by hand.
    const callbacks: Array<() => void> = [];
    const g = globalThis as unknown as { ResizeObserver: unknown };
    const real = g.ResizeObserver;
    g.ResizeObserver = class {
      constructor(callback: () => void) {
        callbacks.push(callback);
      }
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
      takeRecords(): unknown[] {
        return [];
      }
    };
    try {
      await click(ctx, browserButton(ctx));
      await ctx.settle();
      await beat(300);
      const el = viewport(ctx.dom.container) as HTMLElement;
      let width = 800;
      el.getBoundingClientRect = () =>
        ({ left: 10, top: 20, right: 10 + width, bottom: 520, width, height: 500, x: 10, y: 20, toJSON: () => ({}) }) as DOMRect;
      const before = resizeWidths(ctx).length;

      for (const next of [780, 760, 700, 640]) {
        width = next;
        await ctx.act(async () => void callbacks.forEach((callback) => callback()));
      }
      assert.deepEqual(resizeWidths(ctx).slice(before), [780], "the first measurement of a burst waited for a timer");

      await beat(300);
      assert.deepEqual(resizeWidths(ctx).slice(before), [780, 640], "the burst was not reduced to its first and its last");
    } finally {
      g.ResizeObserver = real;
    }
  });
});

// ── the page is in the way ───────────────────────────────────────────────────

test("the page is taken off the screen while the divider is dragged, and put back when it is let go", async () => {
  await withApp({ viewport: WIDE, ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    const pageId = await seatAPage(ctx, "https://example.com/a");
    await click(ctx, tabNamed(ctx, "Conversation:"));
    await split(ctx);
    assert.equal(shown(ctx), pageId);
    const divider = ctx.dom.container.querySelector('[aria-label="Resize panes"]') as HTMLElement;
    const fire = (type: string, clientX: number): Promise<void> =>
      ctx.act(async () => void divider.dispatchEvent(new ctx.dom.window.MouseEvent(type, { bubbles: true, cancelable: true, clientX })));

    await fire("pointerdown", 1200);
    await ctx.settle();
    assert.equal(shown(ctx), "", "the page stayed under the pointer, where it would swallow the drag");

    await fire("pointermove", 900);
    await fire("pointerup", 900);
    await ctx.settle();
    assert.equal(shown(ctx), pageId, "the page did not come back when the divider was let go");
  });
});

test("the page is taken off the screen while the command palette is open, and put back when it closes", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    const pageId = await seatAPage(ctx, "https://example.com/a");
    assert.equal(shown(ctx), pageId);

    await chord(ctx, "k", "KeyK");
    assert.equal(shown(ctx), "", "the palette opened behind the page");

    await ctx.act(async () => {
      ctx.dom.window.document.body.dispatchEvent(new ctx.dom.window.KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
    });
    await ctx.dom.press(ctx.dom.byLabel("Command palette"), "Escape");
    await ctx.settle();
    assert.equal(shown(ctx), pageId, "the page did not come back when the palette closed");
  });
});

test("the page is taken off the screen while Settings is open, and put back when it closes", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    const pageId = await seatAPage(ctx, "https://example.com/a");
    assert.equal(shown(ctx), pageId);

    await ctx.dom.click(ctx.dom.byButton("Settings"));
    await ctx.settle();
    assert.equal(shown(ctx), "", "Settings opened behind the page");

    // Escape closes it, like every other overlay (a window-level key listener).
    await ctx.act(async () => {
      ctx.dom.window.dispatchEvent(new ctx.dom.window.KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
    });
    await ctx.settle();
    assert.equal(shown(ctx), pageId, "the page did not come back when Settings closed");
  });
});

test("the page is taken off the screen while a tab's menu is open, and put back when it closes", async () => {
  await withApp({ viewport: WIDE, ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    const pageId = await seatAPage(ctx, "https://example.com/a");
    await click(ctx, tabNamed(ctx, "Conversation:"));
    await split(ctx);
    assert.equal(shown(ctx), pageId);

    await rightClick(ctx, tabNamed(ctx, "Browser:"));
    await ctx.settle();
    assert.equal(shown(ctx), "", "the menu opened behind the page");

    await click(ctx, menuItems(ctx)[0]!);
    await ctx.settle();
    assert.equal(panes(ctx).length, 0, "Close split did not close the split");
  });
});

// ── a link in an answer ──────────────────────────────────────────────────────

test("a link in an answer is read beside the answer when there is room, and the page is the focused pane", async () => {
  await withApp({ viewport: WIDE, ...WITH_LINK }, async (ctx) => {
    await openThread(ctx, "c1");

    await click(ctx, linkButton(ctx));
    await ctx.settle();

    assert.equal(panes(ctx).length, 2, "the link did not open beside the chat");
    assert.match(pane(ctx, 0).textContent ?? "", /See the guide/, "the answer is not in the left pane");
    assert.ok(viewport(pane(ctx, 1)), "the page is not in the right pane");
    assert.equal(focusedSide(ctx), "1");
    const opened = commands(ctx, "codify_browser_open");
    assert.equal(opened.length, 1);
    assert.equal(opened[0]!.url, "https://example.com/guide");
    assert.equal(shown(ctx), String(opened[0]!.tabId));
  });
});

test("a link in an answer opens full-width when a split is already showing, and the split waits", async () => {
  await withChatAndShell(
    async (ctx) => {
      await split(ctx);
      assert.equal(panes(ctx).length, 2);

      await click(ctx, linkButton(ctx));
      await ctx.settle();

      assert.equal(panes(ctx).length, 0, "the page took a pane of a split the person had arranged");
      assert.ok(viewport(ctx.dom.container), "the page is not on screen");

      await click(ctx, tabNamed(ctx, "Conversation:"));
      assert.equal(panes(ctx).length, 2, "the split did not come back");
    },
    WITH_LINK,
  );
});

test("a link in an answer opens full-width when there is no room for two panes", async () => {
  await withApp({ viewport: { width: 1100, height: 700 }, ...WITH_LINK }, async (ctx) => {
    await openThread(ctx, "c1");

    await click(ctx, linkButton(ctx));
    await ctx.settle();

    assert.equal(panes(ctx).length, 0);
    assert.ok(viewport(ctx.dom.container), "the page is not on screen");
    assert.match(selectedTab(ctx), /^Browser:/);
  });
});

test("a popup a page asked for opens its own full-width tab, never a split of two pages", async () => {
  await withApp({ viewport: WIDE }, async (ctx) => {
    const source = await seatAPage(ctx, "https://example.com/a");

    await ctx.emit("browser-popup-requested", { tab_id: source, url: "https://accounts.example.com/signin" });
    await ctx.settle();

    assert.equal(commands(ctx, "codify_browser_open").length, 2, "the popup did not open a tab");
    assert.equal(panes(ctx).length, 0, "two pages were drawn side by side");
    assert.ok(viewport(ctx.dom.container));
  });
});

test("a page picked out of the strip while a split shows does not take a pane: the split waits", async () => {
  await withChatAndShell(async (ctx) => {
    await split(ctx);
    await click(ctx, browserButton(ctx));
    await ctx.settle();
    assert.equal(panes(ctx).length, 0);
    assert.ok(viewport(ctx.dom.container));
  });
});

// ── closing ──────────────────────────────────────────────────────────────────

test("closing the page's tab ends the split, and goes to the chat, and no page is shown", async () => {
  await withApp({ viewport: WIDE, ...SEEDED }, async (ctx) => {
    await openThread(ctx, "c1");
    await seatAPage(ctx, "https://example.com/a");
    await click(ctx, tabNamed(ctx, "Conversation:"));
    await split(ctx);
    assert.equal(panes(ctx).length, 2);

    const close = ctx.dom.container.querySelector('button[aria-label^="Close browser"]') as HTMLElement;
    await click(ctx, close);
    await ctx.settle();

    assert.equal(panes(ctx).length, 0);
    assert.match(selectedTab(ctx), /^Conversation:/);
    assert.equal(shown(ctx), "");
    assert.equal(commands(ctx, "codify_browser_close").length, 1, "the page's native view was left behind");
  });
});

void THREADS;
void openShell;
void beat;
