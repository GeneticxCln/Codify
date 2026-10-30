/**
 * The checkbox Settings draws, instead of the one the desktop draws.
 *
 * A native checkbox's unchecked box is painted by the widget toolkit, not by the
 * page: `accent-color` tints the checked fill, and the rest is the GTK theme's
 * light square — the one control left in Settings that went white on every dark
 * theme. `components/ui/Checkbox.tsx` is a `role="checkbox"` button over theme
 * tokens instead, and this holds the states a native input gave the form:
 * its accessible on/off, a toggle on click, and a disabled state that refuses.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;

const LABEL = "The conductor drives an approved plan";

async function mount(dom: Dom, disabled = false): Promise<string[]> {
  const { Checkbox } = await import("../src/components/ui/Checkbox.tsx");
  const changes: string[] = [];
  const Host: React.FC = () => {
    const [on, setOn] = React.useState(false);
    return h(Checkbox, {
      checked: on,
      disabled,
      onChange: (next: boolean) => {
        changes.push(next ? "on" : "off");
        setOn(next);
      },
      children: LABEL,
    });
  };
  await dom.render(h(Host));
  return changes;
}

test("a themed checkbox is not a native input, and says whether it is on", async () => {
  await withDom(async (dom) => {
    await mount(dom);
    assert.equal(dom.container.querySelector("input") === null, true, "a native input is back");
    const control = dom.byButton(LABEL);
    assert.equal(control.getAttribute("role"), "checkbox");
    assert.equal(control.getAttribute("aria-checked"), "false", "an unchecked box said it was on");
  });
});

test("clicking the sentence toggles it, and the state it reports follows", async () => {
  await withDom(async (dom) => {
    const changes = await mount(dom);
    const control = dom.byButton(LABEL);
    await dom.click(control);
    assert.deepEqual(changes, ["on"]);
    assert.equal(control.getAttribute("aria-checked"), "true", "the box did not report its new state");
    await dom.click(control);
    assert.deepEqual(changes, ["on", "off"]);
    assert.equal(control.getAttribute("aria-checked"), "false");
  });
});

test("a disabled checkbox refuses the click, as a native one does", async () => {
  await withDom(async (dom) => {
    const changes = await mount(dom, true);
    const control = dom.byButton(LABEL) as HTMLButtonElement;
    assert.equal(control.disabled, true);
    await dom.click(control);
    assert.deepEqual(changes, [], "a disabled box changed");
    assert.equal(control.getAttribute("aria-checked"), "false");
  });
});
