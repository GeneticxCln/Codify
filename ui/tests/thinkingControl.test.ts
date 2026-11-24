/**
 * The composer's Think control: how hard the model thinks on chat turns, an engine setting.
 *
 * Found by a person asking why cloud models never think in chat. The engine side is in
 * `tests/test_thinking_in_chat.py`; this holds the control: its order, what it says, and that it is hidden (not
 * dead) when the engine does not know the setting.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { nextThinking, thinkingLabel, THINKING_LEVELS } = await import("../src/conductorSettings.ts");
const { BottomCommandBar } = await import("../src/components/BottomCommandBar.tsx");

test("one click steps off, low, medium, high and back to off", () => {
  assert.deepEqual([...THINKING_LEVELS], ["", "low", "medium", "high"]);
  assert.equal(nextThinking(""), "low");
  assert.equal(nextThinking("low"), "medium");
  assert.equal(nextThinking("medium"), "high");
  assert.equal(nextThinking("high"), "");
  assert.equal(nextThinking("max"), "", "a value the engine never sends counts as off, so the next click is off again");
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
