import test from "node:test";
import assert from "node:assert/strict";

import { findStaleFallback, findStaleModel, staleTarget } from "../src/staleModel.ts";
import type { ModelOption, ProviderModelStatus } from "../src/types.ts";

function modelsFor(provider: string, ids: string[]): ModelOption[] {
  return ids.map((id) => ({ id, name: id, provider, description: "" }));
}

function ok(provider: string, count: number): ProviderModelStatus {
  return { provider, protocol: "openai_compat", ok: true, count };
}

function failed(provider: string): ProviderModelStatus {
  return { provider, protocol: "openai_compat", ok: false, count: 0, error: "no api key" };
}

test("a model present in a successful catalog is not stale", () => {
  assert.equal(
    staleTarget("ollama", "qwen", modelsFor("ollama", ["qwen", "llama"]), [ok("ollama", 2)]),
    null,
  );
});

test("a model missing from a successful catalog is stale with the reported count", () => {
  const verdict = staleTarget("ollama", "retired", modelsFor("ollama", ["qwen"]), [ok("ollama", 1)]);
  assert.deepEqual(verdict, { model: "retired", provider: "ollama", reportedCount: 1 });
});

test("a failed or not-yet-asked discovery never condemns a model", () => {
  assert.equal(staleTarget("openai", "gpt", modelsFor("openai", []), [failed("openai")]), null);
  assert.equal(staleTarget("openai", "gpt", modelsFor("openai", []), []), null);
});

test("shape drift (count > 0 but no rows for this provider) is unknown, not stale", () => {
  assert.equal(staleTarget("openai", "gpt", [], [ok("openai", 3)]), null);
});

test("blank provider or model is not stale", () => {
  assert.equal(staleTarget("", "gpt", modelsFor("", ["gpt"]), [ok("", 1)]), null);
  assert.equal(staleTarget("openai", "  ", modelsFor("openai", ["gpt"]), [ok("openai", 1)]), null);
});

test("findStaleModel reads the primary target, findStaleFallback the fallback", () => {
  const catalog = modelsFor("ollama", ["qwen"]);
  const status = [ok("ollama", 1)];
  assert.equal(
    findStaleModel({ provider: "ollama", model_name: "qwen" }, catalog, status),
    null,
  );
  assert.deepEqual(
    findStaleModel({ provider: "ollama", model_name: "gone" }, catalog, status),
    { model: "gone", provider: "ollama", reportedCount: 1 },
  );
  assert.equal(
    findStaleFallback({ fallback_provider: "ollama", fallback_model_name: "qwen" }, catalog, status),
    null,
  );
  assert.deepEqual(
    findStaleFallback({ fallback_provider: "ollama", fallback_model_name: "gone" }, catalog, status),
    { model: "gone", provider: "ollama", reportedCount: 1 },
  );
  assert.equal(findStaleFallback(undefined, catalog, status), null);
});
