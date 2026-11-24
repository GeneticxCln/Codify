/**
 * Provider health status dot, engine stream frame, and down/recovery notifications (Item 7).
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { providerHealthView } from "../src/providerHealth.ts";
import { readEngineFrame } from "../src/engineFrames.ts";
import { providerHealthNotification } from "../src/notifications.ts";
import type { EngineEvent } from "../src/types.ts";

const { withDom } = await import("./dom.ts");
const { ProviderRow } = await import("../src/components/ProviderRow.tsx");

test("providerHealthView distinguishes ok, auth failure, unreachable, and offline mode", () => {
  const ok = providerHealthView(
    { provider: "openai", protocol: "openai_compat", ok: true, count: 5, reachable: true, auth_ok: true, latency_ms: 42 },
    { hasKey: true, needsKey: true },
  );
  assert.equal(ok.state, "ok");
  assert.equal(ok.dotClass, "bg-codify-success");
  assert.equal(ok.latency, "42 ms");
  assert.match(ok.detail, /Reachable · auth ok · 42 ms/);

  const auth = providerHealthView(
    { provider: "openai", protocol: "openai_compat", ok: false, count: 0, reachable: true, auth_ok: false, latency_ms: 88, error: "HTTP 401 (check the API key)" },
    { hasKey: true, needsKey: true },
  );
  assert.equal(auth.state, "auth_error");
  assert.equal(auth.dotClass, "bg-codify-danger");

  const down = providerHealthView(
    { provider: "ollama", protocol: "ollama", ok: false, count: 0, reachable: false, auth_ok: null, latency_ms: 12, error: "ConnectError" },
    { hasKey: false, needsKey: false },
  );
  assert.equal(down.state, "unreachable");
  assert.equal(down.dotClass, "bg-codify-danger");

  const offline = providerHealthView(
    { provider: "openai", protocol: "openai_compat", ok: false, count: 0, reachable: false, auth_ok: null, offline_skipped: true },
    { hasKey: true, needsKey: true },
  );
  assert.equal(offline.state, "offline");
  assert.equal(offline.dotClass, "bg-codify-border");
});

test("readEngineFrame forwards provider_health_changed and providerHealthNotification formats down & recovery", () => {
  const seen: EngineEvent[] = [];
  const frame = {
    type: "provider_health_changed",
    payload: {
      down: [{ provider: "openai", reachable: false, auth_ok: null, error: "timed out after 8s" }],
      recovered: [],
      fetched_at: 1_700_000_123,
    },
  };
  assert.equal(readEngineFrame(JSON.stringify(frame), (ev) => seen.push(ev)), "health");
  assert.equal(seen.length, 1);

  const downNotice = providerHealthNotification(frame.payload, 1_700_000_123_000);
  assert.ok(downNotice);
  assert.equal(downNotice.tone, "danger");
  assert.match(downNotice.title, /OpenAI is unreachable/);
  assert.deepEqual(downNotice.target, { kind: "settings", tab: "keys" });

  const upNotice = providerHealthNotification(
    { down: [], recovered: [{ provider: "openai", latency_ms: 50 }], fetched_at: 1_700_000_200 },
    1_700_000_200_000,
  );
  assert.ok(upNotice);
  assert.equal(upNotice.tone, "success");
  assert.match(upNotice.title, /OpenAI is back/);
});

test("ProviderRow renders the status dot and latency pill in Settings", async () => {
  await withDom(async (dom) => {
    await dom.render(
      React.createElement(ProviderRow, {
        keyStatus: {
          provider: "openai",
          protocol: "openai_compat",
          base_url: "https://api.openai.com/v1",
          needs_key: true,
          has_key: true,
          storage: "keyring",
          storage_detail: "OS keyring",
        },
        status: {
          provider: "openai",
          protocol: "openai_compat",
          ok: true,
          count: 12,
          reachable: true,
          auth_ok: true,
          latency_ms: 63,
        },
        models: [],
        roleProviders: [],
        onSaveKey: async () => {},
        onApplyModel: async () => {},
      }),
    );
    await dom.settle();
    const dot = dom.container.querySelector('[data-provider-health="ok"]');
    assert.ok(dot, "expected healthy status dot");
    assert.match(dot.getAttribute("aria-label") ?? "", /Reachable · auth ok · 63 ms/);
    const latency = dom.container.querySelector('[data-provider-latency="true"]');
    assert.ok(latency, "expected latency label");
    assert.equal(latency.textContent, "63 ms");
  });
});
