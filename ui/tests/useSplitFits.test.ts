/**
 * Whether the row holds two panes, as the hook measures it: re-read when the row changes size, the drawer opens or the
 * UI scale moves, and never stuck on an old answer. The arithmetic is `drawers.test.ts`; this is the part that watches.
 *
 * jsdom has no layout, so the row's width is whatever `getBoundingClientRect` is told and a resize is a recorded
 * `ResizeObserver` callback the test fires.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { Drawer } from "../src/drawers.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;
const { useSplitFits } = await import("../src/useSplitFits.ts");
const { splitFits } = await import("../src/drawers.ts");
const { currentUiScale } = await import("../src/uiScale.ts");

interface Row {
  width: number;
  /** The row changes size and the browser says so. */
  resize(width: number): Promise<void>;
  /** Every answer the hook has given, in order. */
  answers: boolean[];
  /** How many observers are watching and how many were released. */
  watching: () => number;
}

async function inRow(
  dom: Dom,
  width: number,
  props: { sidebarShown: boolean; drawer: Drawer | null },
  body: (row: Row, rerender: (next: { sidebarShown: boolean; drawer: Drawer | null }) => Promise<void>) => Promise<void>,
): Promise<void> {
  const win = dom.window as unknown as Window & typeof globalThis;
  const proto = win.HTMLElement.prototype;
  const saved = Object.getOwnPropertyDescriptor(proto, "getBoundingClientRect")!;
  const row: Row = { width, resize: async () => {}, answers: [], watching: () => 0 };
  Object.defineProperty(proto, "getBoundingClientRect", {
    configurable: true,
    writable: true,
    value(this: Element) {
      return { x: 0, y: 0, top: 0, left: 0, right: row.width, bottom: 100, width: row.width, height: 100, toJSON() {} } as DOMRect;
    },
  });
  const live = new Set<object>();
  const callbacks: Array<() => void> = [];
  const Recording = class {
    cb: () => void;
    constructor(cb: () => void) {
      this.cb = cb;
    }
    observe() {
      live.add(this);
      callbacks.push(this.cb);
    }
    unobserve() {}
    disconnect() {
      live.delete(this);
    }
  };
  const savedObserver = { win: (win as unknown as Record<string, unknown>).ResizeObserver, global: (globalThis as Record<string, unknown>).ResizeObserver };
  (win as unknown as Record<string, unknown>).ResizeObserver = Recording;
  (globalThis as Record<string, unknown>).ResizeObserver = Recording;
  row.watching = () => live.size;
  row.resize = async (w: number) => {
    row.width = w;
    await React.act(async () => {
      for (const cb of callbacks) cb();
    });
  };

  const Probe = (p: { sidebarShown: boolean; drawer: Drawer | null }): React.ReactElement => {
    const ref = React.useRef<HTMLDivElement>(null);
    const fits = useSplitFits(ref, p.sidebarShown, p.drawer);
    row.answers.push(fits);
    return h("div", { ref });
  };
  try {
    await dom.render(h(Probe, props));
    await body(row, (next) => dom.render(h(Probe, next)));
  } finally {
    Object.defineProperty(proto, "getBoundingClientRect", saved);
    (win as unknown as Record<string, unknown>).ResizeObserver = savedObserver.win;
    (globalThis as Record<string, unknown>).ResizeObserver = savedObserver.global;
  }
}

const rootPx = (): number => (16 * currentUiScale()) / 100;
const last = (row: Row): boolean | undefined => row.answers[row.answers.length - 1];

test("it answers for the row it is given, at the scale the window is at", async () => {
  await withDom(async (dom) => {
    const root = rootPx();
    await inRow(dom, 100 * root, { sidebarShown: false, drawer: null }, async (row) => {
      assert.equal(last(row), splitFits(100 * root, root, false, null));
      assert.equal(last(row), true);
      assert.equal(row.answers[0], true, "the first render assumed the row was too narrow, and drew one pane for a frame");
    });
    await inRow(dom, 30 * root, { sidebarShown: false, drawer: null }, async (row) => {
      assert.equal(last(row), false, "a row too narrow for two panes was said to fit");
    });
  });
});

test("a row that changes size is read again", async () => {
  await withDom(async (dom) => {
    const root = rootPx();
    await inRow(dom, 100 * root, { sidebarShown: false, drawer: null }, async (row) => {
      assert.equal(last(row), true);
      await row.resize(30 * root);
      assert.equal(last(row), false, "narrowing the window did not take the second pane away");
      await row.resize(100 * root);
      assert.equal(last(row), true, "widening it did not bring it back");
    });
  });
});

test("the panel being shown, and a drawer opening, change the answer", async () => {
  await withDom(async (dom) => {
    const root = rootPx();
    // 48rem: two panes (44) fit alone; a 15rem panel takes it below; a 20rem drawer does too.
    await inRow(dom, 48 * root, { sidebarShown: false, drawer: null }, async (row, rerender) => {
      assert.equal(last(row), true);
      await rerender({ sidebarShown: true, drawer: null });
      assert.equal(last(row), false, "showing the panel left room for two panes");
      await rerender({ sidebarShown: false, drawer: "history" });
      assert.equal(last(row), false, "opening a drawer left room for two panes");
      await rerender({ sidebarShown: false, drawer: null });
      assert.equal(last(row), true, "closing it did not bring the pane back");
    });
  });
});

test("an unmeasured row never takes a pane away", async () => {
  await withDom(async (dom) => {
    await inRow(dom, 0, { sidebarShown: true, drawer: "stats" }, async (row) => {
      assert.equal(last(row), true);
    });
  });
});

test("it stops watching when it goes", async () => {
  await withDom(async (dom) => {
    await inRow(dom, 100 * rootPx(), { sidebarShown: false, drawer: null }, async (row) => {
      assert.equal(row.watching(), 1);
      await dom.render(h("div"));
      assert.equal(row.watching(), 0, "an observer was left watching a row that is gone");
    });
  });
});
