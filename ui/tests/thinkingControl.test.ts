/**
 * The composer's Think control: how hard the model thinks on chat turns, an engine setting.
 *
 * Found by a person asking why cloud models never think in chat. The engine side is in
 * `tests/test_thinking_in_chat.py`; this holds the control: a menu of every level, what each says, that a choice is
 * what reaches the engine, and that it is hidden (not dead) when the engine does not know the setting. It was a
 * button that stepped one level per click, so high back to low took two clicks through Off, and nobody could see
 * the levels before choosing; asked for as "a picker menu for all levels of thinking".
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { thinkingLabel, THINKING_LEVELS, THINKING_CHOICES } = await import("../src/conductorSettings.ts");
const { BottomCommandBar } = await import("../src/components/BottomCommandBar.tsx");
const { withApp } = await import("./appHarness.ts");

test("the menu offers every level the engine accepts, off first, each saying what it costs", () => {
  assert.deepEqual([...THINKING_LEVELS], ["", "low", "medium", "high"]);
  assert.deepEqual(THINKING_CHOICES.map((c) => c.level), [...THINKING_LEVELS], "a level the engine accepts is missing");
  assert.deepEqual(THINKING_CHOICES.map((c) => c.label), ["Off", "Low", "Medium", "High"]);
  for (const choice of THINKING_CHOICES) assert.ok(choice.hint.length > 10, `${choice.label} has no hint`);
});

test("the label says the level only when there is one", () => {
  assert.equal(thinkingLabel(""), "Think");
  assert.equal(thinkingLabel("high"), "Think: high");
  assert.equal(thinkingLabel("max"), "Think");
});

const noop = (): void => {};
const bar = (thinking: string | undefined): string =>
  renderToStaticMarkup(
    React.createElement(BottomCommandBar, {
      workspaces: [],
      onSelectWorkspace: noop,
      onBrowseWorkspace: async () => {},
      onCreateWorkspace: async () => {},
      manualWorkspaceOpen: false,
      onManualWorkspaceOpenChange: noop,
      onDeleteWorkspace: noop,
      onSetDesignContract: async () => {},
      availableModels: [],
      onSelectModel: noop,
      onRefreshModels: noop,
      mode: "direct",
      onChangeMode: noop,
      onSubmit: noop,
      isLoading: false,
      onOpenSettings: noop,
      onOpenAudioSettings: noop,
      thinking,
      onChangeThinking: noop,
    } as never),
  );

test("the composer shows the control when the engine knows the setting, and hides it otherwise", () => {
  assert.match(bar(""), />Think</);
  assert.match(bar("medium"), />Think: medium</);
  assert.doesNotMatch(bar(undefined), /Think/, "an engine without the setting got a control that does nothing");
});

const engineSettings = (level: string) => ({
  body: { conductor_thinking_effort: { value: level, max: 6 } },
});

test("the menu lists every level, ticks the current one, and a choice is what the engine is sent", async () => {
  await withApp(
    {
      answers: {
        "GET /settings/engine": engineSettings("high"),
        "PUT /settings/engine": (body) => ({ body: { saved: body ?? {} } }),
      },
    },
    async (ctx) => {
      const { dom, settle } = ctx;
      await settle();
      const trigger = () => dom.byButton("Think: high");
      assert.equal(trigger().getAttribute("aria-expanded"), "false");
      await dom.click(trigger());
      await settle();
      const menu = dom.byLabel("Thinking level");
      const items = [...menu.querySelectorAll('[role="menuitemradio"]')];
      assert.deepEqual(
        items.map((i) => i.querySelector(".font-semibold")?.textContent),
        ["Off", "Low", "Medium", "High"],
        "the menu does not offer every level",
      );
      assert.deepEqual(items.map((i) => i.getAttribute("aria-checked")), ["false", "false", "false", "true"]);

      // High straight to Low: one choice, not two clicks through Off.
      await dom.click(items[1]);
      await settle();
      const saves = ctx.engine.filter((c) => c.method === "PUT" && c.path === "/settings/engine");
      assert.deepEqual(saves.map((c) => c.body), [{ conductor_thinking_effort: "low" }]);
      assert.ok(dom.container.querySelector('[aria-label="Thinking level"]') === null, "the menu stayed open");
      assert.ok(dom.byButton("Think: low"), "the control does not show what the engine stored");

      // Choosing the level already set closes the menu and sends nothing.
      await dom.click(dom.byButton("Think: low"));
      await settle();
      const low = [...dom.byLabel("Thinking level").querySelectorAll('[role="menuitemradio"]')][1];
      await dom.click(low);
      await settle();
      assert.equal(ctx.engine.filter((c) => c.method === "PUT" && c.path === "/settings/engine").length, 1);

      // Escape in the prompt closes an open menu, as it does the other pickers.
      await dom.click(dom.byButton("Think: low"));
      await settle();
      await dom.press(dom.container.querySelector("textarea")!, "Escape");
      await settle();
      assert.ok(dom.container.querySelector('[aria-label="Thinking level"]') === null, "Escape left the menu open");
    },
  );
});
