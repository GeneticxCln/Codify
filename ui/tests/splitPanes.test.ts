/**
 * The two panes and the divider between them, drawn on their own.
 *
 * A view, like the drawers: the two panes' contents, the ratio and which pane has the focus come in, and every press goes
 * out as a call. What each pane holds is the App's business (`splitApp.test.ts`); this is that the pair is drawn in
 * order, that the divider can be dragged and driven by the keyboard without leaving the limits (`panes.test.ts`),
 * and that using a pane says which one.
 *
 * jsdom has no layout, so the harness answers every element's rectangle at 1440px wide, and a drag is the pointer
 * events a browser would send to the divider (it holds the pointer, so the moves reach it wherever the pointer is).
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;
const { act } = React as unknown as { act: (body: () => Promise<void> | void) => Promise<void> };
const { SplitPanes } = await import("../src/components/SplitPanes.tsx");
const { DEFAULT_RATIO, MIN_PANE_REM } = await import("../src/panes.ts");

interface Calls {
  focus: number[];
  ratios: Array<[number, boolean]>;
  closed: number;
}

type Props = Partial<React.ComponentProps<typeof SplitPanes>>;

async function mount(dom: Dom, props: Props = {}): Promise<Calls> {
  const calls: Calls = { focus: [], ratios: [], closed: 0 };
  await dom.render(
    h(SplitPanes, {
      left: h("p", null, "left body"),
      right: h("p", null, "right body"),
      leftTitle: "Refactor the parser",
      rightTitle: "shell one",
      leftKind: "chat",
      rightKind: "terminal",
      focused: 0,
      ratio: 0.5,
      onFocusPane: (side: number) => void calls.focus.push(side),
      onRatioChange: (ratio: number, commit: boolean) => void calls.ratios.push([ratio, commit]),
      onCloseSplit: () => void (calls.closed += 1),
      ...props,
    }),
  );
  return calls;
}

const divider = (dom: Dom): HTMLElement => dom.container.querySelector('[role="separator"]') as HTMLElement;
const pane = (dom: Dom, side: 0 | 1): HTMLElement => dom.container.querySelector(`[data-pane="${side}"]`) as HTMLElement;

async function pointer(dom: Dom, target: Element, type: "pointerdown" | "pointermove" | "pointerup" | "pointercancel", clientX: number): Promise<void> {
  await act(async () => {
    target.dispatchEvent(new dom.window.MouseEvent(type, { bubbles: true, cancelable: true, clientX }));
  });
}

async function key(dom: Dom, target: Element, name: string): Promise<boolean> {
  let prevented = false;
  await act(async () => {
    const event = new dom.window.KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true });
    target.dispatchEvent(event);
    prevented = event.defaultPrevented;
  });
  return prevented;
}

test("two panes are drawn left to right, each with what it was given and its own title", async () => {
  await withDom(async (dom) => {
    await mount(dom);
    const [left, right] = [pane(dom, 0), pane(dom, 1)];
    assert.match(left.textContent ?? "", /left body/);
    assert.match(right.textContent ?? "", /right body/);
    assert.match(left.textContent ?? "", /Refactor the parser/);
    assert.match(right.textContent ?? "", /shell one/);
    const all = [...dom.container.querySelectorAll("[data-pane], [role='separator']")];
    assert.deepEqual(
      all.map((el) => el.getAttribute("data-pane") ?? "divider"),
      ["0", "divider", "1"],
      "the divider is not between the panes",
    );
  });
});

test("each pane is named for what it holds, by kind, for a screen reader", async () => {
  await withDom(async (dom) => {
    await mount(dom);
    assert.equal(pane(dom, 0).getAttribute("aria-label"), "Conversation: Refactor the parser");
    assert.equal(pane(dom, 1).getAttribute("aria-label"), "Terminal: shell one");
  });
});

test("the focused pane says so, and only that one", async () => {
  await withDom(async (dom) => {
    await mount(dom, { focused: 1 });
    assert.equal(pane(dom, 0).getAttribute("data-focused"), "false");
    assert.equal(pane(dom, 1).getAttribute("data-focused"), "true");
    await mount(dom, { focused: 0 });
    assert.equal(pane(dom, 0).getAttribute("data-focused"), "true");
    assert.equal(pane(dom, 1).getAttribute("data-focused"), "false");
  });
});

test("the ratio sets the widths, left then right, and neither pane may be narrower than its minimum", async () => {
  await withDom(async (dom) => {
    await mount(dom, { ratio: 0.3 });
    const columns = (dom.container.firstElementChild as HTMLElement).style.gridTemplateColumns;
    assert.equal(columns, `minmax(${MIN_PANE_REM}rem, 0.3fr) 0.375rem minmax(${MIN_PANE_REM}rem, 0.7fr)`);
  });
});

test("the divider is a separator a keyboard can reach, and says where it is", async () => {
  await withDom(async (dom) => {
    await mount(dom, { ratio: 0.4 });
    const d = divider(dom);
    assert.equal(d.getAttribute("aria-orientation"), "vertical");
    assert.equal(d.getAttribute("aria-label"), "Resize panes");
    assert.equal(d.getAttribute("aria-valuenow"), "40");
    assert.equal(d.getAttribute("tabindex"), "0");
    assert.ok(Number(d.getAttribute("aria-valuemin")) < 40 && Number(d.getAttribute("aria-valuemax")) > 40);
  });
});

test("pressing in a pane focuses that pane, and so does focus arriving in it", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom);
    await pointer(dom, pane(dom, 1).querySelector("p") as Element, "pointerdown", 900);
    assert.deepEqual(calls.focus, [1]);
    await act(async () => {
      (pane(dom, 0).querySelector("p") as Element).dispatchEvent(new dom.window.FocusEvent("focusin", { bubbles: true }));
    });
    assert.deepEqual(calls.focus, [1, 0]);
  });
});

test("Close split is offered in each pane and ends the split", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom);
    const buttons = dom.allByLabel("Close split");
    assert.equal(buttons.length, 2);
    await dom.click(buttons[1]);
    assert.equal(calls.closed, 1);
  });
});

// ── dragging ────────────────────────────────────────────────────────────────

test("dragging the divider moves it with the pointer and says when it was let go", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom);
    await pointer(dom, divider(dom), "pointerdown", 720);
    await pointer(dom, divider(dom), "pointermove", 900);
    assert.deepEqual(calls.ratios.at(-1), [900 / 1440, false], "the divider did not follow the pointer");
    await pointer(dom, divider(dom), "pointermove", 1000 - 100);
    await pointer(dom, divider(dom), "pointerup", 900);
    assert.deepEqual(calls.ratios.at(-1), [900 / 1440, true], "letting go did not say the position was final");
  });
});

test("a drag is measured from where the row starts, not from the window's edge", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom);
    // The left panel sits to the row's left: the row starts at 240 and is 1200 wide.
    const row = dom.container.firstElementChild as HTMLElement;
    Object.defineProperty(row, "getBoundingClientRect", {
      configurable: true,
      value: () => ({ x: 240, y: 0, top: 0, left: 240, right: 1440, bottom: 600, width: 1200, height: 600, toJSON() {} }) as DOMRect,
    });
    await pointer(dom, divider(dom), "pointerdown", 840);
    await pointer(dom, divider(dom), "pointermove", 240 + 600);
    assert.equal(calls.ratios.at(-1)?.[0], 0.5, "the pointer's position was not taken relative to the row");
    await pointer(dom, divider(dom), "pointermove", 240 + 480);
    assert.equal(calls.ratios.at(-1)?.[0], 0.4);
  });
});

test("a drag cannot take either pane under its minimum", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom);
    // 1440px at the default 125% is 72rem, so a pane needs 22/72 of the row at the least.
    const least = 22 / 72;
    await pointer(dom, divider(dom), "pointerdown", 720);
    await pointer(dom, divider(dom), "pointermove", 5);
    assert.ok(Math.abs((calls.ratios.at(-1)?.[0] ?? 0) - least) < 1e-9, `${calls.ratios.at(-1)?.[0]} is under the minimum`);
    await pointer(dom, divider(dom), "pointermove", 1435);
    assert.ok(Math.abs((calls.ratios.at(-1)?.[0] ?? 0) - (1 - least)) < 1e-9);
  });
});

test("the pointer moving without the divider held does nothing", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom);
    await pointer(dom, divider(dom), "pointermove", 900);
    await pointer(dom, divider(dom), "pointerup", 900);
    assert.deepEqual(calls.ratios, []);
  });
});

test("a drag that is cancelled ends, and later moves do nothing", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom);
    await pointer(dom, divider(dom), "pointerdown", 720);
    await pointer(dom, divider(dom), "pointermove", 800);
    await pointer(dom, divider(dom), "pointercancel", 800);
    const settled = calls.ratios.length;
    await pointer(dom, divider(dom), "pointermove", 1000);
    assert.equal(calls.ratios.length, settled, "a move after the cancel was still followed");
    assert.equal(calls.ratios.at(-1)?.[1], true, "the cancelled drag never said where it ended");
  });
});

test("pressing the divider does not also focus a pane", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom);
    await pointer(dom, divider(dom), "pointerdown", 720);
    assert.deepEqual(calls.focus, [], "grabbing the divider moved the focus");
  });
});

// ── the keyboard ────────────────────────────────────────────────────────────

test("the arrow keys move the divider a step at a time, within the limits", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom, { ratio: 0.5 });
    assert.equal(await key(dom, divider(dom), "ArrowRight"), true, "the arrow was left to scroll the page");
    assert.deepEqual(calls.ratios.at(-1), [0.55, true]);
    await key(dom, divider(dom), "ArrowLeft");
    assert.deepEqual(calls.ratios.at(-1), [0.45, true]);
  });
  await withDom(async (dom) => {
    const calls = await mount(dom, { ratio: 0.31 });
    await key(dom, divider(dom), "ArrowLeft");
    assert.ok(Math.abs((calls.ratios.at(-1)?.[0] ?? 0) - 22 / 72) < 1e-9, "a step went under the minimum");
  });
});

test("Home and End go to the limits", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom, { ratio: 0.5 });
    await key(dom, divider(dom), "Home");
    assert.ok(Math.abs((calls.ratios.at(-1)?.[0] ?? 1) - 22 / 72) < 1e-9);
    await key(dom, divider(dom), "End");
    assert.ok(Math.abs((calls.ratios.at(-1)?.[0] ?? 0) - (1 - 22 / 72)) < 1e-9);
  });
});

test("other keys are left alone", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom);
    for (const k of ["a", "Tab", "Enter", "ArrowUp", "ArrowDown", "Escape"]) {
      assert.equal(await key(dom, divider(dom), k), false, `${k} was swallowed`);
    }
    assert.deepEqual(calls.ratios, []);
  });
});

test("double-clicking the divider goes back to an even split", async () => {
  await withDom(async (dom) => {
    const calls = await mount(dom, { ratio: 0.3 });
    await act(async () => {
      divider(dom).dispatchEvent(new dom.window.MouseEvent("dblclick", { bubbles: true }));
    });
    assert.deepEqual(calls.ratios.at(-1), [DEFAULT_RATIO, true]);
  });
});
