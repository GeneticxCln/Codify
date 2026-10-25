/**
 * A transcript follows its end only while the person is at its end.
 *
 * The container's scroll metrics are set by hand (a document with no layout has none), and `dom.scrolls` records which
 * element the code asked the browser to scroll to.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;

interface Metrics {
  scrollHeight: number;
  clientHeight: number;
  scrollTop: number;
}

async function mount(dom: Dom, metrics: Metrics) {
  const { useStickToBottom } = await import("../src/stickToBottom.ts");
  const Probe: React.FC<{ items: string[] }> = ({ items }) => {
    const end = React.useRef<HTMLDivElement>(null);
    useStickToBottom(end, items.length, items);
    return React.createElement(
      "div",
      { "data-scroller": "true", style: { overflowY: "auto" } },
      items.map((t) => React.createElement("p", { key: t }, t)),
      React.createElement("div", { ref: end, "data-end": "true" }),
    );
  };
  await dom.render(React.createElement(Probe, { items: ["a"] }));
  const scroller = dom.container.querySelector("[data-scroller]") as HTMLElement;
  for (const key of ["scrollHeight", "clientHeight", "scrollTop"] as const) {
    Object.defineProperty(scroller, key, { get: () => metrics[key], configurable: true });
  }
  const rerender = (items: string[]) => dom.render(React.createElement(Probe, { items }));
  const scrollTo = async (top: number) => {
    metrics.scrollTop = top;
    await dom.act(async () => {
      scroller.dispatchEvent(new dom.window.Event("scroll"));
    });
  };
  return { rerender, scrollTo };
}

test("a new message scrolls to the end", async () => {
  await withDom(async (dom) => {
    const m: Metrics = { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 };
    const { rerender } = await mount(dom, m);
    const before = dom.scrolls.length;
    await rerender(["a", "b"]);
    assert.equal(dom.scrolls.length, before + 1);
  });
});

test("an update that adds no message does not drag a reader who has scrolled up back down", async () => {
  await withDom(async (dom) => {
    const m: Metrics = { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 };
    const { rerender, scrollTo } = await mount(dom, m);
    // The reader leaves the end, long after the code's own scroll.
    const realNow = Date.now;
    Date.now = () => realNow() + 5_000;
    try {
      await scrollTo(200);
      const before = dom.scrolls.length;
      // A poll tick: a new array with the same messages.
      await rerender(["a"]);
      await rerender(["a"]);
      assert.equal(dom.scrolls.length, before, "the view was pulled back to the end");
    } finally {
      Date.now = realNow;
    }
  });
});

test("an update with no new message still follows when the reader is at the end", async () => {
  await withDom(async (dom) => {
    const m: Metrics = { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 };
    const { rerender } = await mount(dom, m);
    const before = dom.scrolls.length;
    await rerender(["a"]);
    assert.equal(dom.scrolls.length, before + 1, "a streaming answer stopped following");
  });
});

test("a new message brings a reader who had scrolled up back to the end", async () => {
  await withDom(async (dom) => {
    const m: Metrics = { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 };
    const { rerender, scrollTo } = await mount(dom, m);
    const realNow = Date.now;
    Date.now = () => realNow() + 5_000;
    try {
      await scrollTo(200);
      const before = dom.scrolls.length;
      await rerender(["a", "b"]);
      assert.equal(dom.scrolls.length, before + 1);
    } finally {
      Date.now = realNow;
    }
  });
});
