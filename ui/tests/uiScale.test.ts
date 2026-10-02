/**
 * The UI scale: what is stored and refused, what lands on the root, and the three ways to change it
 * (the Settings choice, the keyboard, and another window).
 *
 * The root's `font-size` is the whole mechanism: the type ramp and spacing are rem, so one number on
 * `<html>` scales text, icons and padding together. These tests read that number back off a real
 * document, because a store that holds 150 while the window shows 100 would pass any test that only
 * looked at the store.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const scale = await import("../src/uiScale.ts");
const React = (await import("react")).default;
const h = React.createElement;

/** A `Storage` that remembers what it was given, and can refuse to. */
function fakeStore(initial: Record<string, string> = {}, refuseWrites = false) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem: (key: string): string | null => data.get(key) ?? null,
    setItem: (key: string, value: string): void => {
      if (refuseWrites) throw new Error("storage is full");
      data.set(key, value);
    },
  };
}

const rootSize = (dom: Dom): string => dom.window.document.documentElement.style.fontSize;

test("the default is 125%, and the steps are the ones layout is checked at", () => {
  assert.equal(scale.DEFAULT_UI_SCALE, 125);
  assert.deepEqual([...scale.UI_SCALE_STEPS], [100, 112.5, 125, 150, 175]);
  assert.equal(scale.readUiScale(fakeStore()), 125, "nothing stored is the default, not 100");
});

test("a stored value is read back only if it is one of the steps", () => {
  for (const step of scale.UI_SCALE_STEPS) {
    assert.equal(scale.readUiScale(fakeStore({ [scale.UI_SCALE_KEY]: String(step) })), step);
  }
  // A hand-edited or corrupt value is refused, not trusted: 3 would be a window nobody can read,
  // and 900 one nobody can reach the Settings pane in to fix.
  for (const bad of ["3", "900", "126", "-100", "NaN", "", "  ", "big", "112.50.1", "1e2", "100.0", " 100", "0x64"]) {
    assert.equal(
      scale.readUiScale(fakeStore({ [scale.UI_SCALE_KEY]: bad })),
      scale.DEFAULT_UI_SCALE,
      `${JSON.stringify(bad)} was accepted as a scale`,
    );
  }
});

test("a store that throws is the default, not a crash", () => {
  const broken = {
    getItem: (): string | null => {
      throw new Error("blocked");
    },
  };
  assert.equal(scale.readUiScale(broken), scale.DEFAULT_UI_SCALE);
});

test("stepping goes one step and stops at the ends", () => {
  assert.equal(scale.stepUiScale(125, 1), 150);
  assert.equal(scale.stepUiScale(125, -1), 112.5);
  assert.equal(scale.stepUiScale(112.5, -1), 100);
  assert.equal(scale.stepUiScale(100, -1), 100, "stepped below the smallest size");
  assert.equal(scale.stepUiScale(175, 1), 175, "stepped past the largest size");
  assert.equal(scale.stepUiScale(150, 1), 175);
});

test("writing stores it, puts it on the root, and tells listeners, in that order", async () => {
  await withDom(async (dom) => {
    const store = fakeStore();
    const seen: string[] = [];
    dom.window.addEventListener(scale.UI_SCALE_CHANGED, () =>
      // What a listener sees when it is told: the store and the root are already right.
      seen.push(`${store.data.get(scale.UI_SCALE_KEY)}|${rootSize(dom)}`),
    );

    scale.writeUiScale(150, store);

    assert.equal(store.data.get(scale.UI_SCALE_KEY), "150");
    assert.equal(rootSize(dom), "150%");
    assert.deepEqual(seen, ["150|150%"]);
    assert.equal(scale.uiScaleFactor(), 1.5);
  });
});

test("a store that refuses the write still gives this session its size", async () => {
  await withDom(async (dom) => {
    scale.writeUiScale(112.5, fakeStore({}, true));
    assert.equal(rootSize(dom), "112.5%");
    assert.equal(scale.currentUiScale(), 112.5, "stepping would start from the default again");
    assert.equal(scale.uiScaleFactor(), 1.125);
  });
});

test("booting shows the stored scale before anything renders, and follows another window", async () => {
  await withDom(async (dom) => {
    dom.window.localStorage.setItem(scale.UI_SCALE_KEY, "175");
    const stop = scale.startUiScale();
    try {
      assert.equal(rootSize(dom), "175%");

      // Another window chose 100: `storage` fires here, and only here.
      dom.window.localStorage.setItem(scale.UI_SCALE_KEY, "100");
      let told = 0;
      dom.window.addEventListener(scale.UI_SCALE_CHANGED, () => told++);
      dom.window.dispatchEvent(new dom.window.StorageEvent("storage", { key: scale.UI_SCALE_KEY }));
      assert.equal(rootSize(dom), "100%", "another window's choice never reached this root");
      assert.equal(told, 1, "this window's listeners were not told");

      // A different key is none of its business.
      dom.window.dispatchEvent(new dom.window.StorageEvent("storage", { key: "codify.theme" }));
      assert.equal(told, 1);
    } finally {
      stop();
    }
  });
});

test("nothing stored boots at the 125% default", async () => {
  await withDom(async (dom) => {
    const stop = scale.startUiScale();
    try {
      assert.equal(rootSize(dom), "125%");
    } finally {
      stop();
    }
  });
});

// ── the Settings control ──────────────────────────────────────────────────

async function withPanel(body: (dom: Dom) => Promise<void>): Promise<void> {
  await withDom(async (dom) => {
    const { UiScalePanel } = await import("../src/components/UiScalePanel.tsx");
    scale.applyUiScale(scale.DEFAULT_UI_SCALE);
    await dom.render(h(UiScalePanel));
    await body(dom);
  });
}

const options = (dom: Dom): HTMLElement[] => dom.allByLabel("UI scale").length === 0
  ? []
  : [...dom.byLabel("UI scale").querySelectorAll('[role="radio"]')] as HTMLElement[];

const chosen = (dom: Dom): string[] =>
  options(dom)
    .filter((o) => o.getAttribute("aria-checked") === "true")
    .map((o) => (o.textContent ?? "").replace("default", "").trim());

test("the panel offers every step, and marks the one in force", async () => {
  await withPanel(async (dom) => {
    assert.deepEqual(
      options(dom).map((o) => (o.textContent ?? "").replace("default", "").trim()),
      ["100%", "112.5%", "125%", "150%", "175%"],
    );
    assert.deepEqual(chosen(dom), ["125%"]);
    assert.match(dom.text(), /125%default/, "the default is not labelled");
  });
});

test("choosing a size applies it to the window at once and remembers it", async () => {
  await withPanel(async (dom) => {
    await dom.click(options(dom)[3]); // 150%

    assert.equal(rootSize(dom), "150%", "the choice did not reach the window");
    assert.equal(dom.window.localStorage.getItem(scale.UI_SCALE_KEY), "150");
    assert.deepEqual(chosen(dom), ["150%"]);
    assert.match(dom.text(), /at 150%/, "the preview does not say what size it is");
  });
});

test("the arrow keys move through the sizes and choose as they go, and stop at the ends", async () => {
  await withPanel(async (dom) => {
    const group = dom.byLabel("UI scale");
    await dom.press(group, "ArrowRight");
    assert.equal(rootSize(dom), "150%");
    await dom.press(group, "ArrowRight");
    await dom.press(group, "ArrowRight");
    assert.equal(rootSize(dom), "175%", "ran past the largest size");
    await dom.press(group, "ArrowLeft");
    assert.equal(rootSize(dom), "150%");
    for (let i = 0; i < 6; i++) await dom.press(group, "ArrowLeft");
    assert.equal(rootSize(dom), "100%", "ran past the smallest size");
    // One tab stop for the group, on the option in force.
    assert.deepEqual(
      options(dom).map((o) => o.getAttribute("tabindex")),
      ["0", "-1", "-1", "-1", "-1"],
    );
  });
});

test("the panel follows a change made elsewhere, such as the keyboard shortcut", async () => {
  await withPanel(async (dom) => {
    await React.act(async () => {
      scale.writeUiScale(112.5);
    });
    assert.deepEqual(chosen(dom), ["112.5%"], "the panel kept showing a size the window had left");
  });
});

// ── the keyboard, through the whole App ─────────────────────────────────

test("Ctrl + and Ctrl - step the window, and Ctrl 0 returns to the default", async () => {
  const { withApp } = await import("./appHarness.ts");
  await withApp({}, async (ctx) => {
    scale.applyUiScale(scale.DEFAULT_UI_SCALE);
    const press = (key: string, code: string, extra: Record<string, unknown> = {}) =>
      ctx.dom.press(ctx.dom.window.document.body, key, { code, ctrlKey: true, ...extra });
    const size = () => rootSize(ctx.dom);

    await press("=", "Equal");
    assert.equal(size(), "150%");
    await press("-", "Minus");
    await press("-", "Minus");
    await press("-", "Minus"); // 150 -> 125 -> 112.5 -> 100
    assert.equal(size(), "100%");
    await press("-", "Minus");
    assert.equal(size(), "100%", "stepped below the smallest size");
    await press("=", "Equal", { repeat: true });
    assert.equal(size(), "100%", "a held key ran the scale");
    await press("0", "Digit0");
    assert.equal(size(), "125%");
    assert.equal(ctx.dom.window.localStorage.getItem(scale.UI_SCALE_KEY), "125");
  });
});
