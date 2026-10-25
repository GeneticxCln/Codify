/**
 * A refusal that comes back from the desktop shell is a bare string, and the app must show it.
 *
 * Rust commands return `Result<_, String>`, and Tauri rejects with that string, so `err.message` is `undefined`.
 * Three places read `err.message || "Failed to …"`, which swapped the engine's own sentence for the generic one:
 * the role cards' load, a role card's save and the test-connection button.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;

const REFUSAL = "openai has no API key stored, so there is nothing to send";

function shellThatRefuses(): () => void {
  const internals = {
    transformCallback: () => 1,
    async invoke(): Promise<unknown> {
      throw REFUSAL; // a bare string, as a Rust `Err(String)` arrives
    },
  };
  (globalThis.window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = internals;
  (globalThis as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = internals;
  return () => {
    delete (globalThis.window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    delete (globalThis as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  };
}

test("the test-connection button shows a refusal that is a bare string", async () => {
  await withDom(async (dom) => {
    const { TestConnectionButton } = await import("../src/components/TestConnectionButton.tsx");
    await dom.render(
      React.createElement(TestConnectionButton, {
        onTest: async () => {
          throw REFUSAL;
        },
      } as never),
    );
    await dom.click(dom.container.querySelector("button") as Element);
    await dom.settle();
    assert.match(dom.text(), new RegExp(REFUSAL));
    assert.doesNotMatch(dom.text(), /Failed to test connection/);
  });
});

test("loading the role cards shows the shell's sentence, not a generic one", async () => {
  await withDom(async (dom) => {
    const restore = shellThatRefuses();
    try {
      const { useAgentConfigs } = await import("../src/hooks/useAgentConfigs.ts");
      const Probe: React.FC = () => {
        const store = useAgentConfigs();
        return React.createElement("p", null, `error: ${store.error ?? "none"}`);
      };
      await dom.render(React.createElement(Probe));
      await dom.settle();
      await dom.settle();
      assert.match(dom.text(), new RegExp(REFUSAL));
    } finally {
      restore();
    }
  });
});
