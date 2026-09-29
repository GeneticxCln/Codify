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

// ── a picker that cannot open leaves the person somewhere to go ───────────────────────────────────────
//
// The banner above says "type the folder's path instead", and the form for that lived behind the project
// chip's dropdown ("Enter path manually…"). On a desktop with no dialog helper the folder button *never*
// works, so the most prominent way to add a project ended in an instruction and a hunt for the control it
// named (audit of 2026-09-29, second pass). The app now opens that form itself.

const pathDialog = (root: HTMLElement): HTMLElement | null =>
  root.querySelector('[role="dialog"][aria-label="Enter workspace path"]');

const unavailable = {
  status: 503,
  body: { code: "picker_unavailable", message: "No folder dialog could be opened. Type the folder's path instead." },
};

test("a picker that could not open opens the path form for the person", async () => {
  await withApp({ browse: unavailable }, async ({ dom, settle }) => {
    assert.equal(pathDialog(dom.container), null, "the form is open before anything was asked");

    await dom.click(folderButton(dom.container));
    await settle();

    assert.ok(pathDialog(dom.container), "the picker failed and the person was left to find the path form");
    assert.equal(alerts(dom.container).length, 1, "the reason is still on screen behind the form");
  });
});

test("a path typed into that form becomes the selected project", async () => {
  await withApp({ browse: unavailable }, async ({ dom, engine, settle }) => {
    await dom.click(folderButton(dom.container));
    await settle();
    const dialog = pathDialog(dom.container);
    assert.ok(dialog);

    await dom.fill(dialog.querySelector('input[placeholder="e.g. My Next.js App"]') as HTMLInputElement, "Charlie");
    await dom.fill(dialog.querySelector('input[placeholder="/home/you/Projects/my-app"]') as HTMLInputElement, "/tmp/e2e-charlie");
    await dom.click(dom.byButton("Add Directory"));
    await settle();

    const created = engine.filter((c) => c.method === "POST" && c.path === "/workspaces");
    assert.equal(created.length, 1);
    assert.deepEqual(created[0].body, { name: "Charlie", root_path: "/tmp/e2e-charlie" });
    assert.equal(pathDialog(dom.container), null, "the form stayed open after the project was added");
    assert.match(dom.container.textContent ?? "", /Charlie/, "the added project was not selected");
  });
});

test("a dialog the person closed does not open the path form", async () => {
  await withApp({ browse: { body: { cancelled: true } } }, async ({ dom, settle }) => {
    await dom.click(folderButton(dom.container));
    await settle();

    assert.equal(pathDialog(dom.container), null, "closing the dialog opened a form nobody asked for");
  });
});

test("any other failure is shown, but does not open the path form", async () => {
  const broken = { status: 500, body: { code: "boom", message: "the engine fell over" } };
  await withApp({ browse: broken }, async ({ dom, settle }) => {
    await dom.click(folderButton(dom.container));
    await settle();

    assert.equal(alerts(dom.container).length, 1);
    assert.equal(pathDialog(dom.container), null, "a failure that is not 'no dialog helper' opened the form");
  });
});
