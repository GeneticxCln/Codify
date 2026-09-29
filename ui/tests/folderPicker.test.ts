/**
 * The folder button tells the person what happened (audit of 2026-09-29, M8).
 *
 * The engine answered `{"cancelled": true}` for a dialog that had *crashed* — no PyGObject, no display —
 * and the UI's `browseWorkspace` returned null for any non-2xx as well, so a picker that could not open
 * looked exactly like one the person closed: nothing happened, and nothing said why. The engine now
 * separates the cases (`tests/test_workspace_browse.py`); this is the other half: a refusal reaches the
 * screen in the engine's words, and a real cancel stays silent.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withApp } = await import("./appHarness.ts");

const alerts = (root: HTMLElement): HTMLElement[] =>
  [...root.querySelectorAll('[role="alert"]')] as HTMLElement[];

const folderButton = (root: HTMLElement): HTMLElement => {
  const button = root.querySelector('button[title="Add a project folder"]');
  assert.ok(button, "the panel has no control for adding a project folder");
  return button as HTMLElement;
};

test("a picker that could not open says why, in the engine's words", async () => {
  const message = "No folder dialog could be opened (PyGObject is not importable). Type the folder's path instead.";
  await withApp(
    { browse: { status: 503, body: { code: "picker_unavailable", message } } },
    async ({ dom, settle }) => {
      await dom.click(folderButton(dom.container));
      await settle();

      const shown = alerts(dom.container);
      assert.equal(shown.length, 1, "a picker that failed to open left the screen silent");
      assert.match(shown[0].textContent ?? "", /PyGObject is not importable/);
      assert.match(shown[0].textContent ?? "", /Type the folder's path/);
    },
  );
});

test("a dialog the person closed is not an error", async () => {
  await withApp({ browse: { body: { cancelled: true } } }, async ({ dom, settle }) => {
    await dom.click(folderButton(dom.container));
    await settle();

    assert.equal(alerts(dom.container).length, 0, "closing the dialog was reported as a failure");
  });
});

test("a chosen folder becomes the selected project, with no error", async () => {
  const workspace = { id: "ws-b", name: "Bravo", root_path: "/tmp/e2e-bravo", design_contract_path: "", created_at: 1 };
  await withApp({ browse: { body: { cancelled: false, workspace } } }, async ({ dom, settle }) => {
    await dom.click(folderButton(dom.container));
    await settle();

    assert.equal(alerts(dom.container).length, 0);
    assert.match(dom.container.textContent ?? "", /Bravo/, "the chosen project was not selected");
  });
});
