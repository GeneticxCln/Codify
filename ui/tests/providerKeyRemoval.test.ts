/**
 * Removing a saved provider key from the provider row, mounted.
 *
 * `DELETE /settings/keys/{provider}` existed and nothing called it, so a key saved in Settings could be overwritten
 * and never removed: the secret stayed in the store for good. The control must ask before it removes, must call the
 * route for the provider on the row and no other, and must not claim a key was removed when the engine had none saved
 * (a key that still shows as configured then comes from the environment, which Codify does not change).
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { ProviderKeyStatus } from "../src/types.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;
const { ProviderRow } = await import("../src/components/ProviderRow.tsx");

const status = (over: Partial<ProviderKeyStatus> = {}): ProviderKeyStatus => ({
  provider: "nvidia",
  protocol: "openai_compat",
  base_url: "https://integrate.api.nvidia.com/v1",
  has_key: true,
  needs_key: true,
  storage: "keyring",
  storage_detail: "OS keychain",
  ...over,
} as ProviderKeyStatus);

async function withRow(
  over: Record<string, unknown>,
  body: (dom: Dom, removed: string[]) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    const removed: string[] = [];
    await dom.render(h(ProviderRow, {
      keyStatus: status(),
      models: [],
      roleProviders: [],
      onSaveKey: async () => {},
      onApplyModel: async () => {},
      onRemoveKey: async (provider: string) => {
        removed.push(provider);
        return { removed: true };
      },
      ...over,
    } as never));
    await dom.settle();
    await body(dom, removed);
  });
}

test("a saved key offers Remove, and nothing is removed by offering it", async () => {
  await withRow({}, async (dom, removed) => {
    assert.ok(dom.byButton("Remove saved key"));
    assert.deepEqual(removed, []);
    assert.doesNotMatch(dom.text(), /Yes, remove it/);
  });
});

test("the first click asks, and keeping it removes nothing", async () => {
  await withRow({}, async (dom, removed) => {
    await dom.click(dom.byButton("Remove saved key"));
    assert.match(dom.text(), /Remove the saved nvidia key\?/);
    assert.deepEqual(removed, [], "the key was removed on the first click");

    await dom.click(dom.byButton("Keep it"));
    assert.doesNotMatch(dom.text(), /Remove the saved nvidia key\?/);
    assert.deepEqual(removed, []);
    assert.ok(dom.byButton("Remove saved key"));
  });
});

test("confirming removes the key of this provider and says so", async () => {
  await withRow({}, async (dom, removed) => {
    await dom.click(dom.byButton("Remove saved key"));
    await dom.click(dom.byButton("Yes, remove it"));
    await dom.settle();

    assert.deepEqual(removed, ["nvidia"]);
    assert.match(dom.text(), /The saved nvidia key was removed/);
  });
});

test("when nothing was saved, the row says the key comes from the environment and does not claim it was removed", async () => {
  await withRow({ onRemoveKey: async () => ({ removed: false }) }, async (dom) => {
    await dom.click(dom.byButton("Remove saved key"));
    await dom.click(dom.byButton("Yes, remove it"));
    await dom.settle();

    assert.match(dom.text(), /had no saved nvidia key to remove/);
    assert.match(dom.text(), /environment variable/);
    assert.doesNotMatch(dom.text(), /key was removed/);
  });
});

test("a refused removal says why", async () => {
  await withRow({ onRemoveKey: async () => { throw new Error("key store unavailable"); } }, async (dom) => {
    await dom.click(dom.byButton("Remove saved key"));
    await dom.click(dom.byButton("Yes, remove it"));
    await dom.settle();

    assert.match(dom.text(), /key store unavailable/);
    assert.doesNotMatch(dom.text(), /key was removed/);
  });
});

test("Remove is not offered where there is nothing saved to remove, or no way to remove it", async () => {
  await withRow({ keyStatus: status({ has_key: false }) }, async (dom) => {
    assert.doesNotMatch(dom.text(), /Remove saved key/, "offered with no key saved");
  });
  await withRow({ keyStatus: status({ needs_key: false, has_key: true }) }, async (dom) => {
    assert.doesNotMatch(dom.text(), /Remove saved key/, "offered for a provider that takes no key");
  });
  await withRow({ onRemoveKey: undefined }, async (dom) => {
    assert.doesNotMatch(dom.text(), /Remove saved key/, "offered with no handler");
  });
});
