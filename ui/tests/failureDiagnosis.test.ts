import test from "node:test";
import assert from "node:assert/strict";

import { diagnoseFailure } from "../src/failureDiagnosis.ts";
import type { AgentConfig } from "../src/types.ts";

function fixer(extra: Partial<AgentConfig> = {}): AgentConfig {
  return {
    role: "fixer",
    display_name: "Fixer",
    provider: "openai",
    protocol: "openai_compat",
    model_name: "gpt-4o",
    temperature: 0.2,
    max_tokens: 4096,
    updated_at: 0,
    ...extra,
  };
}

test("a failure with no role attribution says so and still explains the code", () => {
  const verdicts = diagnoseFailure({
    code: "path_escape",
    message: "outside workspace",
    role: null,
    catalog: [],
  });
  // Non-config codes lead with the code meaning; the no-role note follows.
  assert.match(verdicts[0].title, /What "path_escape" means/);
  assert.ok(verdicts.some((v) => v.title.includes("did not attribute")));
});

test("a missing stored config is a blocker that keeps the code explainer", () => {
  const verdicts = diagnoseFailure({
    code: "provider_http",
    message: "boom",
    role: "fixer",
    catalog: [],
  });
  assert.equal(verdicts[0].level, "blocker");
  assert.match(verdicts[0].title, /No configuration is stored/);
  assert.ok(verdicts.some((v) => v.title.includes("provider_http")));
});

test("an empty model name blocks with a fix pointing at Agent Roles", () => {
  const verdicts = diagnoseFailure({
    code: "agent_not_configured",
    message: "no model",
    role: "fixer",
    config: fixer({ model_name: "" }),
    catalog: [],
  });
  const blocker = verdicts.find((v) => v.level === "blocker");
  assert.ok(blocker);
  assert.equal(blocker?.fix?.tab, "agents");
});

test("a confirmed-missing key blocks; its discovery symptom is suppressed", () => {
  const verdicts = diagnoseFailure({
    code: "provider_http",
    message: "401",
    role: "fixer",
    config: fixer(),
    keys: {
      provider: "openai",
      has_key: false,
      protocol: "openai_compat",
      base_url: "",
      needs_key: true,
      storage: "keyring",
      storage_detail: "keychain",
    },
    discovery: {
      provider: "openai",
      protocol: "openai_compat",
      ok: false,
      count: 0,
      error: "no api key configured",
    },
    catalog: [],
  });
  assert.ok(verdicts.some((v) => v.title.includes("No credential")));
  // The discovery failure is the missing key's symptom — reported once, not twice.
  assert.equal(
    verdicts.filter((v) => v.title.includes("model list")).length,
    0,
  );
});

test("an unknown key state keeps a real discovery problem visible", () => {
  const verdicts = diagnoseFailure({
    code: "provider_http",
    message: "500",
    role: "fixer",
    config: fixer(),
    discovery: {
      provider: "openai",
      protocol: "openai_compat",
      ok: false,
      count: 0,
      error: "no api key configured",
    },
    catalog: [],
  });
  assert.ok(verdicts.some((v) => v.title.includes("model list")));
});

test("a retired model blocks; a served model yields an info verdict", () => {
  const retired = diagnoseFailure({
    code: "provider_http",
    message: "404",
    role: "fixer",
    config: fixer(),
    discovery: { provider: "openai", protocol: "openai_compat", ok: true, count: 1 },
    catalog: [{ id: "other", name: "other", provider: "openai", description: "" }],
  });
  assert.ok(retired.some((v) => v.title.includes("no longer reports")));

  const served = diagnoseFailure({
    code: "agent_output_invalid",
    message: "bad json",
    role: "fixer",
    config: fixer(),
    discovery: { provider: "openai", protocol: "openai_compat", ok: true, count: 1 },
    catalog: [{ id: "gpt-4o", name: "gpt-4o", provider: "openai", description: "" }],
  });
  assert.ok(served.some((v) => v.title.includes("currently served")));
});

test("non-config codes lead with the code meaning and demote config findings", () => {
  const verdicts = diagnoseFailure({
    code: "path_escape",
    message: "outside workspace",
    role: "fixer",
    config: fixer({ model_name: "" }),
    catalog: [],
  });
  assert.match(verdicts[0].title, /What "path_escape" means/);
  const demoted = verdicts.find((v) => v.detail.startsWith("Separate from this failure"));
  assert.ok(demoted);
  assert.equal(demoted?.level, "warning");
});
