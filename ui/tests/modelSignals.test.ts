import test from "node:test";
import assert from "node:assert/strict";

import {
  EMPTY_SIGNALS,
  buildModelSignals,
  modelBadges,
  modelKey,
  orderModelMenu,
  orderProviderModels,
} from "../src/modelSignals.ts";
import type { AgentConfig, ModelOption, RecentRunModel } from "../src/types.ts";

function config(
  role: AgentConfig["role"],
  provider: string,
  model: string,
  extra: Partial<AgentConfig> = {},
): AgentConfig {
  return {
    role,
    display_name: role,
    provider,
    protocol: "openai_compat",
    model_name: model,
    temperature: 0.2,
    max_tokens: 4096,
    updated_at: 0,
    ...extra,
  };
}

function model(provider: string, id: string, extra: Partial<ModelOption> = {}): ModelOption {
  return { id, name: id, provider, description: "", ...extra };
}

test("modelKey is a provider:id composite so shared ids do not collide", () => {
  assert.equal(modelKey("ollama", "qwen"), "ollama:qwen");
  assert.notEqual(modelKey("ollama", "qwen"), modelKey("openai", "qwen"));
});

test("buildModelSignals records roles, fallbacks, and the newest run", () => {
  const signals = buildModelSignals(
    [
      config("fixer", "ollama", "qwen"),
      config("critic", "openai", "gpt", {
        fallback_provider: "ollama",
        fallback_model_name: "qwen",
      }),
    ],
    [
      { provider: "ollama", model: "qwen", ran_at: 200, role: "fixer" },
      { provider: "ollama", model: "qwen", ran_at: 100, role: "critic" },
    ] as RecentRunModel[],
  );

  const usage = signals.usage[modelKey("ollama", "qwen")];
  assert.deepEqual([...usage.roles].sort(), ["critic", "fixer"]);
  // Newest-first input wins: the first run in the array is kept.
  assert.equal(usage.ranAt, 200);
  assert.equal(usage.ranRole, "fixer");
});

test("buildModelSignals ignores runs with a blank provider or model", () => {
  const signals = buildModelSignals([], [
    { provider: "", model: "qwen", ran_at: 1 },
    { provider: "ollama", model: "  ", ran_at: 1 },
  ] as RecentRunModel[]);
  assert.deepEqual(signals, { usage: {} });
});

test("orderModelMenu pins in-use, then recent, then providers — nothing dropped", () => {
  const models = [model("ollama", "qwen"), model("openai", "gpt"), model("openai", "emb")];
  const signals = buildModelSignals([config("fixer", "ollama", "qwen")], [
    { provider: "openai", model: "gpt", ran_at: 5 },
  ] as RecentRunModel[]);

  const sections = orderModelMenu(models, { signals });
  assert.deepEqual(sections.map((s) => s.id), ["roles", "recent", "provider:openai"]);
  assert.deepEqual(sections[0].models.map((m) => m.id), ["qwen"]);
  assert.deepEqual(sections[1].models.map((m) => m.id), ["gpt"]);
  assert.deepEqual(sections[2].models.map((m) => m.id), ["emb"]);
  // Every input model appears exactly once across sections.
  const all = sections.flatMap((s) => s.models.map((m) => modelKey(m.provider, m.id))).sort();
  assert.deepEqual(all, ["ollama:qwen", "openai:emb", "openai:gpt"].sort());
});

test("orderModelMenu filters by name, id, and provider", () => {
  const models = [model("ollama", "qwen2.5-coder:7b"), model("openai", "gpt-4o")];
  const sections = orderModelMenu(models, { filter: "QWEN" });
  assert.equal(sections.flatMap((s) => s.models).length, 1);
});

test("orderWithinGroup puts non-chat models last but keeps them listed", () => {
  const models = [
    model("openai", "emb", { supports_chat: false, created: 999 }),
    model("openai", "gpt", { created: 1 }),
  ];
  const ordered = orderProviderModels(models);
  assert.deepEqual(ordered.map((m) => m.id), ["gpt", "emb"]);
});

test("modelBadges names roles, recency, and non-chat models", () => {
  const signals = buildModelSignals([config("fixer", "ollama", "qwen")], [
    { provider: "ollama", model: "qwen", ran_at: 9, role: "fixer" },
  ] as RecentRunModel[]);
  const badges = modelBadges(model("ollama", "qwen"), signals);
  assert.equal(badges.roles, "fixer");
  assert.equal(badges.lastRun, true);
  assert.match(badges.lastRunTitle, /fixer/);
  assert.equal(badges.notChat, false);

  const emb = modelBadges(model("openai", "emb", { supports_chat: false }), EMPTY_SIGNALS);
  assert.equal(emb.notChat, true);
  assert.equal(emb.lastRun, false);
});
