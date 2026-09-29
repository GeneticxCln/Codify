/**
 * Closing a tab in the real App, against the fake shell in `appHarness.ts`.
 *
 * Only the *active* browser tab is given a page after a restore; the others are
 * addresses waiting for their turn. Closing one of those used to ask the shell
 * to close a page it never opened, and the shell's honest answer ("no browser
 * tab "browser-4" is open") landed on screen as an error the user had caused by
 * closing a tab.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withApp } = await import("./appHarness.ts");

/** A remembered strip of two browser tabs, the first showing. */
const STORED_STRIP = {
  version: 1,
  layout: {
    tabs: [
      { key: "k_test_1", kind: "browser", url: "https://example.com/first" },
      { key: "k_test_2", kind: "browser", url: "https://example.com/second" },
    ],
    activeIndex: 0,
  },
  pendingRemovals: [],
  pendingWrites: [],
};

test("closing a restored tab that never got a page asks the shell for nothing", async () => {
  await withApp({ storedTabs: STORED_STRIP }, async ({ dom, shell, settle, tabs }) => {
    assert.equal(tabs().length, 2, "the remembered strip did not come back");
    const closers = [...dom.container.querySelectorAll('button[aria-label^="Close"]')] as HTMLElement[];
    assert.equal(closers.length, 2, `expected a close control per tab, found ${closers.length}`);

    await dom.click(closers[1]);
    await settle();

    assert.equal(
      shell.calls.filter((c) => c === "codify_browser_close").length,
      0,
      "the shell was asked to close a page that was never opened",
    );
    assert.equal(
      dom.container.querySelector('[role="alert"]')?.textContent ?? "",
      "",
      "closing a tab put an error on screen",
    );
    assert.equal(tabs().length, 1, "the tab did not close");
  });
});
