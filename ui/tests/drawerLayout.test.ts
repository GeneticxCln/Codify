/**
 * The drawers and the left panel, through the whole App, in a window whose width the test controls.
 *
 * jsdom has no layout, so the row's width is whatever `getBoundingClientRect` is told and a resize is
 * a recorded `ResizeObserver` callback the test fires. What is asserted is behaviour: which of the
 * panel and the drawer is on screen, what the toggle says, and, above all, what `codify.sidebar`
 * holds, because the panel giving way to a drawer must never be remembered as the person's choice.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext } from "./appHarness.ts";

const { withApp } = await import("./appHarness.ts");
const React = (await import("react")).default;

const SIDEBAR_KEY = "codify.sidebar";

interface Row {
  /** The width the App's `<main>` reports, in px. */
  width: number;
  /** Re-measure, as the browser does when the window changes size. */
  resize(width: number): Promise<void>;
}

/** Mount the App at a UI scale, with `<main>` reporting `width`, and run the body. */
async function inWindow(
  width: number,
  scale: 100 | 125 | 175,
  body: (ctx: AppContext, row: Row) => Promise<void>,
  extraStorage: Record<string, string> = {},
): Promise<void> {
  await withApp({ localStorage: { "codify.uiScale": String(scale), ...extraStorage } }, async (ctx) => {
    const win = ctx.dom.window as unknown as Window & typeof globalThis;
    // The test DOM (`dom.ts`) answers every element's rect at a fixed 1440px on `HTMLElement.prototype`,
    // and that shadows anything defined further up the chain, so this replaces it *there* for one test
    // and puts the descriptor back after. (1440px is 72rem at the default 125%: just under the Stats
    // drawer's threshold, which is why a test that forgot to say its width would always see the panel yield.)
    const proto = win.HTMLElement.prototype;
    const saved = Object.getOwnPropertyDescriptor(proto, "getBoundingClientRect")!;
    const original = saved.value as (this: Element) => DOMRect;
    const row: Row = { width, resize: async () => {} };
    Object.defineProperty(proto, "getBoundingClientRect", {
      configurable: true,
      writable: true,
      value(this: Element) {
        if (this.tagName === "MAIN") {
          return { x: 0, y: 0, top: 0, left: 0, right: row.width, bottom: 600, width: row.width, height: 600, toJSON() {} } as DOMRect;
        }
        return original.call(this);
      },
    });
    // Only an observer that is watching `<main>` is the layout's: the page's canvases and panes have
    // observers of their own, and firing those too is not "the window was resized", it is a different
    // experiment (one of them redraws in a loop with no layout to settle it).
    const watching: Array<{ owner: object; cb: () => void; el: Element }> = [];
    const RecordingObserver = class {
      // No TypeScript parameter property: `node --experimental-strip-types` erases types and refuses syntax
      // that needs emitting, and `tsc` does not warn about it.
      cb: () => void;
      constructor(cb: () => void) {
        this.cb = cb;
      }
      observe(el: Element) {
        watching.push({ owner: this, cb: this.cb, el });
      }
      unobserve() {}
      disconnect() {
        for (let i = watching.length - 1; i >= 0; i -= 1) if (watching[i]!.owner === this) watching.splice(i, 1);
      }
    };
    // The app reads the *global*, which the test DOM copied from the window when it was installed, so both are
    // replaced (and both put back after).
    const savedObservers = { win: (win as unknown as Record<string, unknown>).ResizeObserver, global: (globalThis as Record<string, unknown>).ResizeObserver };
    (win as unknown as Record<string, unknown>).ResizeObserver = RecordingObserver;
    (globalThis as Record<string, unknown>).ResizeObserver = RecordingObserver;
    row.resize = async (w: number) => {
      row.width = w;
      await React.act(async () => {
        for (const w of watching.filter((o) => o.el.tagName === "MAIN")) w.cb();
      });
    };
    try {
      await body(ctx, row);
    } finally {
      Object.defineProperty(proto, "getBoundingClientRect", saved);
      (win as unknown as Record<string, unknown>).ResizeObserver = savedObservers.win;
      (globalThis as Record<string, unknown>).ResizeObserver = savedObservers.global;
    }
  });
}

const panel = (ctx: AppContext): Element | null => ctx.dom.container.querySelector("main > nav");
const stats = (ctx: AppContext): Element | null => ctx.dom.container.querySelector('aside:has([aria-label="Close statistics"])');
const history = (ctx: AppContext): Element | null => ctx.dom.container.querySelector('aside:has([aria-label="Close goal history"])');
const toggle = (ctx: AppContext): HTMLElement => ctx.dom.allByLabel("Hide left panel")[0] ?? ctx.dom.byLabel("Show left panel");
const openStats = (ctx: AppContext): Promise<void> => ctx.dom.click(ctx.dom.byButton("Stats"));
const openHistory = (ctx: AppContext): Promise<void> => ctx.dom.click(ctx.dom.byButton("History"));
const stored = (ctx: AppContext): string | null => ctx.dom.window.localStorage.getItem(SIDEBAR_KEY);

test("a wide window holds the panel and a drawer together", async () => {
  await inWindow(2200, 125, async (ctx) => {
    await openStats(ctx);
    assert.ok(stats(ctx), "the drawer did not open");
    assert.ok(panel(ctx), "the panel gave way in a window that holds both");
    assert.equal(toggle(ctx).getAttribute("aria-label"), "Hide left panel");
  });
});

test("a narrow window gives the room to the drawer, and the panel comes back when it closes", async () => {
  await inWindow(1280, 125, async (ctx) => {
    assert.ok(panel(ctx));
    await openStats(ctx);
    assert.ok(stats(ctx));
    assert.ok(panel(ctx) === null, "the panel stayed and squeezed the transcript");
    // The toggle reports what is on screen, not what was chosen.
    assert.equal(toggle(ctx).getAttribute("aria-label"), "Show left panel");
    assert.equal(toggle(ctx).getAttribute("aria-pressed"), "true");

    await ctx.dom.click(ctx.dom.byLabel("Close statistics"));
    assert.ok(stats(ctx) === null);
    assert.ok(panel(ctx), "closing the drawer did not bring the panel back");
    assert.equal(toggle(ctx).getAttribute("aria-label"), "Hide left panel");
  });
});

test("the panel giving way is never remembered as the person's choice", async () => {
  await inWindow(1280, 125, async (ctx) => {
    assert.equal(stored(ctx), "open");
    await openStats(ctx);
    assert.ok(panel(ctx) === null);
    assert.equal(stored(ctx), "open", "a drawer made the panel's hiding a stored preference");
    await openHistory(ctx);
    assert.equal(stored(ctx), "open");
    await ctx.dom.click(ctx.dom.byLabel("Close goal history"));
    assert.equal(stored(ctx), "open");
    assert.ok(panel(ctx));
  });
});

test("resizing the window moves the panel in and out while a drawer stays open", async () => {
  await inWindow(2200, 125, async (ctx, row) => {
    await openStats(ctx);
    assert.ok(panel(ctx));
    await row.resize(1280);
    assert.ok(panel(ctx) === null, "narrowing the window did not move the panel aside");
    assert.ok(stats(ctx), "the drawer closed on resize");
    await row.resize(2200);
    assert.ok(panel(ctx), "widening the window did not bring the panel back");
    assert.equal(stored(ctx), "open");
  });
});

test("a person's own hide stays a hide, through drawers opening and closing", async () => {
  await inWindow(2200, 125, async (ctx) => {
    await ctx.dom.click(toggle(ctx));
    assert.ok(panel(ctx) === null);
    assert.equal(stored(ctx), "closed");
    await openStats(ctx);
    await ctx.dom.click(ctx.dom.byLabel("Close statistics"));
    assert.ok(panel(ctx) === null, "closing a drawer showed a panel the person had hidden");
    assert.equal(stored(ctx), "closed");
  });
});

test("pressing 'Show left panel' while a drawer displaced it closes the drawer, since both do not fit", async () => {
  await inWindow(1280, 125, async (ctx) => {
    await openStats(ctx);
    assert.ok(panel(ctx) === null);
    await ctx.dom.click(toggle(ctx));
    assert.ok(panel(ctx), "the toggle did not show the panel");
    assert.ok(stats(ctx) === null, "the drawer stayed, so the panel and the drawer were fighting for the room");
    assert.equal(stored(ctx), "open");
    // And the press after that is an ordinary hide.
    await ctx.dom.click(toggle(ctx));
    assert.ok(panel(ctx) === null);
    assert.equal(stored(ctx), "closed");
  });
});

test("the threshold follows the UI scale, because it is in rem", async () => {
  // The same 1280px window: roomy at 100%, too tight at 125%.
  await inWindow(1280, 100, async (ctx) => {
    await openStats(ctx);
    assert.ok(panel(ctx), "at 100% there is room for both");
  });
  await inWindow(1280, 125, async (ctx) => {
    await openStats(ctx);
    assert.ok(panel(ctx) === null, "at 125% there is not");
  });
});

test("each drawer asks for its own width", async () => {
  // 1400px at 125% is 70rem: the History drawer (20rem) fits beside the panel, Stats (28rem) does not.
  await inWindow(1400, 125, async (ctx) => {
    await openHistory(ctx);
    assert.ok(history(ctx));
    assert.ok(panel(ctx), "History fits and should not have displaced the panel");
    await openStats(ctx);
    assert.ok(stats(ctx) && !history(ctx), "switching drawers left two open");
    assert.ok(panel(ctx) === null, "Stats does not fit beside the panel");
  });
});

test("only one drawer is ever open", async () => {
  await inWindow(2200, 125, async (ctx) => {
    await openStats(ctx);
    await openHistory(ctx);
    assert.ok(history(ctx) && !stats(ctx));
    await openStats(ctx);
    assert.ok(stats(ctx) && !history(ctx));
    await openStats(ctx);
    assert.ok(!stats(ctx) && !history(ctx), "pressing the open drawer's button did not close it");
  });
});

test("the widths the rule assumes are the widths the layout has, and the drawers are capped", async () => {
  await inWindow(2200, 125, async (ctx) => {
    const { SIDEBAR_REM, DRAWER_REM } = await import("../src/drawers.ts");
    assert.ok((panel(ctx) as Element).classList.contains("w-60"), "the panel is no longer w-60 (15rem)");
    assert.equal(SIDEBAR_REM, 15);
    await openStats(ctx);
    const s = stats(ctx) as Element;
    assert.ok(s.classList.contains("w-[28rem]") && DRAWER_REM.stats === 28, "Stats is no longer 28rem");
    assert.ok(s.classList.contains("max-w-[45%]"), "Stats has no cap that keeps the centre column usable");
    await openHistory(ctx);
    const h = history(ctx) as Element;
    assert.ok(h.classList.contains("w-80") && DRAWER_REM.history === 20, "History is no longer 20rem");
    assert.ok(h.classList.contains("max-w-[40%]"), "History has no cap that keeps the centre column usable");
    // Both are siblings of the centre column, never overlays: a native browser webview paints above overlays.
    const main = ctx.dom.container.querySelector("main") as Element;
    assert.ok(h.parentElement === main, "the History drawer is not a sibling of the centre column");
  });
});

test("a window that cannot be measured never hides the panel", async () => {
  // Width 0 is what a document with no layout reports. Hiding something because it could not be
  // measured would be the wrong default.
  await inWindow(0, 125, async (ctx) => {
    await openStats(ctx);
    assert.ok(stats(ctx));
    assert.ok(panel(ctx), "a window that could not be measured lost its panel");
  });
});
