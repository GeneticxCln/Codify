import test from "node:test";
import assert from "node:assert/strict";
import React from "react";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
const { ColorPicker, hexToHsv, hsvToHex, normalizeHex } = await import("../src/components/ColorPicker.tsx");

test("hex <-> hsv conversion round-trips standard colours cleanly", () => {
  for (const hex of ["#000000", "#ffffff", "#ff0000", "#00ff88", "#3b82f6", "#a855f7"]) {
    const [h, s, v] = hexToHsv(hex);
    assert.equal(hsvToHex(h, s, v), hex, `round-trip drifted for ${hex}`);
  }
  assert.equal(normalizeHex("00FF88"), "#00ff88");
  assert.equal(normalizeHex("#AbCdEf"), "#abcdef");
  assert.equal(normalizeHex("not-a-hex"), null);
});

test("ColorPicker opens a themed dialog, accepts presets and hex edits, and closes on Escape without bubbling", async () => {
  await withDom(async (dom) => {
    let parentSawEscape = false;
    const picked: string[] = [];

    const Host: React.FC = () => {
      const [hex, setHex] = React.useState("#00ff88");
      return React.createElement(
        "div",
        {
          onKeyDown: (e: React.KeyboardEvent) => {
            if (e.key === "Escape") parentSawEscape = true;
          },
        },
        React.createElement(ColorPicker, {
          label: "Interface accent",
          value: hex,
          onChange: (next) => {
            setHex(next);
            picked.push(next);
          },
        }),
      );
    };

    await dom.render(React.createElement(Host));
    assert.ok(dom.container.querySelector('[role="dialog"]') === null, "dialog opened before click");

    await dom.click(dom.byLabel("Interface accent colour"));
    const dialog = dom.container.querySelector('[role="dialog"]');
    assert.ok(dialog, "swatch click did not open themed picker dialog");
    assert.match(dialog.className, /\bbg-codify-surface\b/, "picker dialog is not themed");

    await dom.click(dom.byLabel("Preset #3b82f6"));
    assert.equal(picked.at(-1), "#3b82f6");

    await dom.fill(dom.byLabel("Interface accent hex value") as HTMLInputElement, "#ff2e9a");
    assert.equal(picked.at(-1), "#ff2e9a");

    await dom.press(dialog, "Escape");
    assert.ok(dom.container.querySelector('[role="dialog"]') === null, "Escape did not close picker");
    assert.equal(parentSawEscape, false, "Escape leaked out to the parent modal");
  });
});
