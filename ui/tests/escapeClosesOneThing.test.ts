/**
 * One Escape closes one thing.
 *
 * A model dropdown closes on Escape with a listener on the document, and Settings closes on Escape with a listener on
 * the window. Nothing stopped the first from reaching the second, so Escape on an open dropdown also closed the whole
 * of Settings, discarding the unsaved role-card edits and any key typed into the page behind it. (The command palette
 * already honoured the rule that one keystroke never closes two surfaces.)
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { ModelOption } from "../src/types.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;

const options: ModelOption[] = [
  { id: "gpt-a", name: "gpt-a", provider: "openai", description: "", created: 2 },
  { id: "gpt-b", name: "gpt-b", provider: "openai", description: "", created: 1 },
] as ModelOption[];

/** What the Settings modal does: a window-level Escape listener. */
function modalListener(dom: Dom): { closed: () => number; remove: () => void } {
  let closed = 0;
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") closed += 1;
  };
  dom.window.addEventListener("keydown", onKey);
  return { closed: () => closed, remove: () => dom.window.removeEventListener("keydown", onKey) };
}

async function escape(dom: Dom): Promise<void> {
  await dom.act(async () => {
    dom.window.document.body.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
    );
  });
}

test("Escape on an open ModelSelect closes the list and not the modal behind it", async () => {
  await withDom(async (dom) => {
    const { ModelSelect } = await import("../src/components/ModelSelect.tsx");
    await dom.render(React.createElement(ModelSelect, { provider: "openai", value: "", onChange: () => {}, options }));
    const modal = modalListener(dom);
    await dom.click(dom.byLabel("Browse discovered models"));
    assert.ok(dom.window.document.body.textContent?.includes("gpt-a"), "the list did not open");
    await escape(dom);
    assert.equal(modal.closed(), 0, "the same Escape closed the modal");
    assert.ok(!dom.window.document.body.textContent?.includes("gpt-b"), "the list is still open");
    modal.remove();
  });
});

test("Escape with no list open still reaches the modal", async () => {
  await withDom(async (dom) => {
    const { ModelSelect } = await import("../src/components/ModelSelect.tsx");
    await dom.render(React.createElement(ModelSelect, { provider: "openai", value: "", onChange: () => {}, options }));
    const modal = modalListener(dom);
    await escape(dom);
    assert.equal(modal.closed(), 1);
    modal.remove();
  });
});

test("Escape on an open ModelPicker closes the list and not the modal behind it", async () => {
  await withDom(async (dom) => {
    const { ModelPicker } = await import("../src/components/ModelPicker.tsx");
    await dom.render(React.createElement(ModelPicker, { provider: "openai", value: "", onChange: () => {}, options }));
    const modal = modalListener(dom);
    await dom.click(dom.byLabel("Browse OpenAI models"));
    await escape(dom);
    assert.equal(modal.closed(), 0, "the same Escape closed the modal");
    modal.remove();
  });
});
