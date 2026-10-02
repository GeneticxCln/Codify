/**
 * The two surfaces that hold a size in pixels and so cannot follow a root font size by themselves:
 * the xterm terminal (it draws its own canvas in a font size it is handed) and the prompt box's
 * height cap. Both are mounted, because "reads the scale" is a claim about what appears on screen.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const scale = await import("../src/uiScale.ts");

test("the terminal's text is 12px at 100% and keeps its proportion at every step", async () => {
  const { terminalFontSize } = await import("../src/components/TerminalPane.tsx");
  const sizes = scale.UI_SCALE_STEPS.map((step) => {
    scale.applyUiScale(step);
    return terminalFontSize();
  });
  assert.deepEqual(sizes, [12, 14, 15, 18, 21]);
  scale.applyUiScale(scale.DEFAULT_UI_SCALE);
});

/** The font size xterm's own renderer wrote into the page's style sheets. */
function xtermFontSizes(doc: Document): string[] {
  return [...doc.querySelectorAll("style")]
    .map((el) => el.textContent ?? "")
    .filter((css) => /xterm/.test(css) && /font-size/.test(css))
    .flatMap((css) => css.match(/font-size:\s*[\d.]+px/g) ?? [])
    .map((rule) => rule.replace(/font-size:\s*/, ""));
}

test("a terminal opens at the UI scale, and changes size with it while it is open", async () => {
  await withDom(async (dom) => {
    // xterm's bundle reaches for `self`, which Node's module scope lacks and jsdom has inside the window.
    if (!(globalThis as Record<string, unknown>).self) {
      (globalThis as Record<string, unknown>).self = dom.window;
    }
    scale.applyUiScale(150);
    const { TerminalPane } = await import("../src/components/TerminalPane.tsx");
    await dom.render(React.createElement(TerminalPane, { terminalId: "t-scale", workspaceId: "w1" }));
    for (let i = 0; i < 8; i++) await dom.settle();
    assert.deepEqual(xtermFontSizes(dom.window.document), ["18px"], "opened at the wrong size");

    await React.act(async () => {
      scale.writeUiScale(100);
    });
    for (let i = 0; i < 4; i++) await dom.settle();
    assert.deepEqual(xtermFontSizes(dom.window.document), ["12px"], "an open terminal ignored the change");

    await React.act(async () => {
      scale.writeUiScale(175);
    });
    for (let i = 0; i < 4; i++) await dom.settle();
    assert.deepEqual(xtermFontSizes(dom.window.document), ["21px"]);
    scale.applyUiScale(scale.DEFAULT_UI_SCALE);
  });
});

test("the prompt box grows to a cap that scales: 180px at 100%, and the same proportion above it", async () => {
  const { withApp } = await import("./appHarness.ts");
  await withApp({}, async (ctx) => {
    const { dom } = ctx;
    // jsdom has no layout, so a box never has a content height. Give every textarea a tall one, so
    // the cap is the only thing that can decide how high it ends up.
    Object.defineProperty(dom.window.HTMLTextAreaElement.prototype, "scrollHeight", {
      configurable: true,
      get: () => 2000,
    });
    const box = dom.byField("Chat prompt") as HTMLTextAreaElement;

    await React.act(async () => {
      scale.writeUiScale(100);
    });
    await dom.fill(box, "line one\nline two");
    assert.equal(box.style.height, "180px");

    // A change of scale re-measures at once, without waiting for the next keystroke.
    await React.act(async () => {
      scale.writeUiScale(175);
    });
    assert.equal(box.style.height, "315px", "the cap did not follow the scale");
    scale.applyUiScale(scale.DEFAULT_UI_SCALE);
  });
});
