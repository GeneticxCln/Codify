import test from "node:test";
import assert from "node:assert/strict";
import React, { act } from "react";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
const { confirmDialog, promptDialog, resetDialogs } = await import("../src/appDialog.ts");
const { AppDialogHost } = await import("../src/components/AppDialog.tsx");

test("confirmDialog renders a themed alertdialog and resolves true/false on confirm/cancel/Escape/backdrop", async () => {
  await withDom(async (dom) => {
    resetDialogs();
    await dom.render(React.createElement(AppDialogHost));

    let p1!: Promise<boolean>;
    await act(async () => {
      p1 = confirmDialog({
        title: "Delete this message?",
        message: "Files in your workspace are NOT touched.",
        confirmLabel: "Delete",
        tone: "danger",
      });
    });

    const dlg = dom.container.querySelector('[role="alertdialog"]');
    assert.ok(dlg, "confirmDialog did not mount a themed alertdialog");
    assert.match(dlg.className, /\bbg-codify-surface\b/, "dialog is not painted from --codify-* tokens");
    assert.match(dlg.textContent ?? "", /Delete this message\?/);

    await dom.click(dom.byButton("Delete"));
    assert.equal(await p1, true);
    assert.ok(dom.container.querySelector('[role="alertdialog"]') === null);

    // Cancel via Escape
    let p2!: Promise<boolean>;
    await act(async () => {
      p2 = confirmDialog({ title: "Remove?", message: "Careful" });
    });
    const dlg2 = dom.container.querySelector('[role="alertdialog"]');
    assert.ok(dlg2);
    await dom.press(dlg2, "Escape");
    assert.equal(await p2, false);

    // Cancel via backdrop click
    let p3!: Promise<boolean>;
    await act(async () => {
      p3 = confirmDialog({ title: "Reset?", message: "Careful" });
    });
    const backdrop = dom.container.querySelector('[data-app-dialog-backdrop="true"]');
    assert.ok(backdrop);
    await act(async () => {
      backdrop.dispatchEvent(new dom.window.MouseEvent("mousedown", { bubbles: true }));
    });
    assert.equal(await p3, false);
  });
});

test("promptDialog pre-fills value, submits edited text, and returns null on cancel", async () => {
  await withDom(async (dom) => {
    resetDialogs();
    await dom.render(React.createElement(AppDialogHost));

    let p1!: Promise<string | null>;
    await act(async () => {
      p1 = promptDialog({
        title: "Name this conversation",
        initialValue: "Old title",
        confirmLabel: "Save",
      });
    });

    const input = dom.byLabel("Name this conversation") as HTMLInputElement;
    assert.equal(input.value, "Old title");
    await dom.fill(input, "New title");
    await dom.click(dom.byButton("Save"));
    assert.equal(await p1, "New title");

    let p2!: Promise<string | null>;
    await act(async () => {
      p2 = promptDialog({ title: "Name this conversation", initialValue: "Keep" });
    });
    await dom.click(dom.byButton("Cancel"));
    assert.equal(await p2, null);
  });
});

test("first dialog queued before lazy mount survives React.StrictMode double-effect", async () => {
  await withDom(async (dom) => {
    resetDialogs();
    // Queue the dialog BEFORE AppDialogHost mounts (exact lazy-mount flow), then mount under StrictMode.
    const pending = confirmDialog({ title: "Delete workspace?", message: "Files stay on disk.", confirmLabel: "Delete" });
    await dom.render(React.createElement(React.StrictMode, null, React.createElement(AppDialogHost)));

    const dlg = dom.container.querySelector('[role="alertdialog"]');
    assert.ok(dlg, "StrictMode effect cleanup cancelled the first queued dialog");
    await dom.click(dom.byButton("Delete"));
    assert.equal(await pending, true);
  });
});
