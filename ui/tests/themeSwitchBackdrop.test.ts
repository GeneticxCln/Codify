/**
 * One click on a theme switches its backdrop, not only its colours.
 *
 * Found by a person: after picking a theme, the window showed the new colours over the old theme's animation, and
 * only a second click on the same row put the right one up. `select` applied the theme (which announces the change)
 * before storing its id, and `useTheme`'s snapshot is the stored id, so the backdrop heard the announcement, read
 * the old id, and kept the old canvas. A second click announced again, after the store, and the switch went through.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withApp } = await import("./appHarness.ts");
const { CMATRIX_OLED, CYBERPUNK_NEON } = await import("../src/appearance.ts");

const backdrops = (root: ParentNode): string[] =>
  [...root.querySelectorAll<HTMLElement>("[data-backdrop]")].map((el) => el.dataset.backdrop ?? "");

test("a single click on a theme puts up that theme's backdrop, and back again", async () => {
  await withApp({ localStorage: { "codify.theme": CMATRIX_OLED.id } }, async (ctx) => {
    const { dom, settle } = ctx;
    await settle();
    assert.deepEqual(backdrops(dom.container), ["rain"], "the Matrix theme did not start with its rain");

    await dom.click(dom.byButton("Settings"));
    await settle();
    await dom.click(dom.byButton("Appearance"));
    await settle();
    const row = (name: string): HTMLElement => {
      const found = [...dom.container.querySelectorAll<HTMLElement>('[role="radiogroup"][aria-label="Theme"] [role="radio"]')]
        .find((el) => (el.textContent ?? "").includes(name));
      assert.ok(found, `no theme row for ${name}`);
      return found;
    };

    await dom.click(row(CYBERPUNK_NEON.label));
    await settle();
    assert.deepEqual(backdrops(dom.container), ["weather"], "one click left the previous theme's backdrop running");
    assert.equal(dom.window.localStorage.getItem("codify.theme"), CYBERPUNK_NEON.id);

    await dom.click(row(CMATRIX_OLED.label));
    await settle();
    assert.deepEqual(backdrops(dom.container), ["rain"], "switching back took more than one click");
  });
});
