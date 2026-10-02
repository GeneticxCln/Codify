/**
 * The composer's dropdowns stay inside the window, wherever the composer is.
 *
 * `threadMenu.test.ts` is the arithmetic (`clampPickerLeft`). What it cannot show is that the composer uses it: the
 * menus are `position: fixed` at their button's left edge, which was always inside the window while the composer
 * spanned it, and in the right-hand pane of a split the button starts half way across.
 *
 * jsdom has no layout, so the button's wrapper and the menu report the rectangles a real window would, and the window is
 * given a width.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withApp } = await import("./appHarness.ts");

test("a model menu whose button is near the right edge is pulled back inside the window", async () => {
  await withApp({ viewport: { width: 1440, height: 900 } }, async (ctx) => {
    const win = ctx.dom.window as unknown as Window & typeof globalThis;
    const proto = win.HTMLElement.prototype;
    const saved = Object.getOwnPropertyDescriptor(proto, "getBoundingClientRect")!;
    const original = saved.value as (this: Element) => DOMRect;
    const rect = (left: number, width: number): DOMRect =>
      ({ x: left, y: 100, top: 100, left, right: left + width, bottom: 130, width, height: 30, toJSON() {} }) as DOMRect;
    Object.defineProperty(win, "innerWidth", { configurable: true, value: 1440 });
    Object.defineProperty(proto, "getBoundingClientRect", {
      configurable: true,
      writable: true,
      value(this: Element) {
        // The picker's wrapper sits at the far right of the window; its menu is `w-80`.
        if (this.classList.contains("relative") && this.querySelector(":scope > button")?.textContent?.includes("test-model")) return rect(1300, 120);
        if (this.classList.contains("w-80")) return rect(0, 320);
        return original.call(this);
      },
    });
    try {
      const button = [...ctx.dom.container.querySelectorAll("button")].find((b) => (b.textContent ?? "").includes("test-model"));
      assert.ok(button, "no model picker button");
      await ctx.dom.click(button);
      await ctx.settle();
      const menu = ctx.dom.container.querySelector<HTMLElement>("div.w-80.fixed, div.w-80[style*='position: fixed']");
      assert.ok(menu, "the menu did not open");
      assert.equal(menu.style.left, `${1440 - 8 - 320}px`, "the menu runs off the right of the window");
    } finally {
      Object.defineProperty(proto, "getBoundingClientRect", saved);
    }
  });
});
