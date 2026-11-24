/**
 * Runtime DOM tests for the Replay button in `TracePanel` (Item 8).
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
const { setEngineInfo } = await import("../src/api.ts");
const { TracePanel } = await import("../src/components/TracePanel.tsx");

const GOAL = {
  id: "g1",
  workspace_id: "ws-1",
  title: "Add feature",
  description: "Add feature",
  status: "COMPLETED" as const,
  dry_run: false,
  plan_only: true,
  parallel: false,
  mode: "normal" as const,
  trace: true,
  version: 2,
  created_at: 1,
  updated_at: 2,
};

const SUMMARY = {
  goal_id: "g1",
  calls: 3,
  by_role: { librarian: 1, design: 1, planner: 1 },
  prompts_kept: true,
  recording_error: null,
  recorded: [
    { seq: 1, role: "librarian", model: "m", prompt_hash: "aaaa1111bbbb2222", input_tokens: 10, output_tokens: 5, duration_ms: 20, at: 1 },
    { seq: 2, role: "design", model: "m", prompt_hash: "cccc3333dddd4444", input_tokens: 10, output_tokens: 5, duration_ms: 20, at: 2 },
    { seq: 3, role: "planner", model: "m", prompt_hash: "eeee5555ffff6666", input_tokens: 10, output_tokens: 5, duration_ms: 20, at: 3 },
  ],
};

test("TracePanel Replay button posts /goals/{id}/trace/replay and renders match vs divergence", async () => {
  await withDom(async (dom) => {
    setEngineInfo({ port: 7430, token: "tok" });
    const realFetch = globalThis.fetch;
    let attempt = 0;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/goals/g1/trace") && method === "GET") {
        return new Response(JSON.stringify(SUMMARY), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.endsWith("/goals/g1/trace/replay") && method === "POST") {
        attempt += 1;
        const body =
          attempt === 1
            ? {
                goal_id: "g1",
                replay_goal_id: "rg1",
                recorded_calls: 3,
                served_calls: 3,
                gate_calls_replayed: 0,
                conductor_calls_skipped: 0,
                stages: [["librarian", "ok"]],
                status: "PENDING",
                diverged: null,
                matched: true,
              }
            : {
                goal_id: "g1",
                replay_goal_id: "rg2",
                recorded_calls: 3,
                served_calls: 0,
                gate_calls_replayed: 0,
                conductor_calls_skipped: 0,
                stages: [],
                status: "FAILED",
                diverged: "replay: librarian was handed a different prompt",
                matched: false,
              };
        return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response("{}", { status: 404 });
    }) as typeof fetch;

    try {
      await dom.render(
        React.createElement(TracePanel, {
          goalId: "g1",
          goal: GOAL,
          onSetTrace: () => {},
          onClose: () => {},
        }),
      );
      await dom.settle();

      const btn = dom.container.querySelector('button[aria-label="Replay recording"]') as HTMLButtonElement | null;
      assert.ok(btn, "Replay recording button missing");
      await dom.click(btn);
      await dom.settle();
      assert.ok(dom.container.querySelector('[data-trace-replay="matched"]'), "expected matched replay verdict");
      assert.match(dom.text(), /Replay matched/);
      assert.match(dom.text(), /3\/3 calls served/);

      await dom.click(btn);
      await dom.settle();
      assert.ok(dom.container.querySelector('[data-trace-replay="diverged"]'), "expected diverged replay verdict");
      assert.match(dom.text(), /Replay diverged/);
      assert.match(dom.text(), /different prompt/);
    } finally {
      globalThis.fetch = realFetch;
    }
  });
});

test("TracePanel ignores a slower earlier call-detail response when a newer call is selected", async () => {
  await withDom(async (dom) => {
    setEngineInfo({ port: 7430, token: "tok" });
    const realFetch = globalThis.fetch;

    let resolveCall1!: (res: Response) => void;
    const call1Deferred = new Promise<Response>((resolve) => {
      resolveCall1 = resolve;
    });

    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/goals/g1/trace")) {
        return new Response(JSON.stringify(SUMMARY), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.endsWith("/goals/g1/trace/1")) {
        return call1Deferred;
      }
      if (url.endsWith("/goals/g1/trace/2")) {
        return new Response(
          JSON.stringify({
            goal_id: "g1",
            seq: 2,
            step_id: null,
            role: "design",
            provider: "ollama",
            model: "m",
            temperature: 0.2,
            max_tokens: 100,
            prompt_hash: "cccc3333dddd4444",
            system_hash: "sys2",
            system_prompt: "Design system prompt",
            user_prompt: "Design user prompt",
            response: "Design reply",
            input_tokens: 10,
            output_tokens: 5,
            duration_ms: 20,
            at: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("{}", { status: 404 });
    }) as typeof fetch;

    try {
      await dom.render(
        React.createElement(TracePanel, {
          goalId: "g1",
          goal: GOAL,
          onSetTrace: () => {},
          onClose: () => {},
        }),
      );
      await dom.settle();

      const btn1 = dom.container.querySelector('button[aria-label="Inspect call 1"]') as HTMLButtonElement | null;
      const btn2 = dom.container.querySelector('button[aria-label="Inspect call 2"]') as HTMLButtonElement | null;
      assert.ok(btn1 && btn2, "inspect buttons missing");

      // Click call 1 (stays pending), then immediately click call 2 (resolves first).
      await dom.click(btn1);
      await dom.click(btn2);
      for (let i = 0; i < 4; i++) await dom.settle();
      assert.match(dom.text(), /Design reply/);

      // Resolve slower call 1: it must NOT overwrite call 2's inspector view.
      resolveCall1(
        new Response(
          JSON.stringify({
            goal_id: "g1",
            seq: 1,
            step_id: null,
            role: "librarian",
            provider: "ollama",
            model: "m",
            temperature: 0.2,
            max_tokens: 100,
            prompt_hash: "aaaa1111bbbb2222",
            system_hash: "sys1",
            system_prompt: "Stale librarian system prompt",
            user_prompt: "Stale librarian user prompt",
            response: "Stale librarian reply",
            input_tokens: 10,
            output_tokens: 5,
            duration_ms: 20,
            at: 1,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
      for (let i = 0; i < 4; i++) await dom.settle();

      assert.match(dom.text(), /Design reply/);
      assert.doesNotMatch(dom.text(), /Stale librarian reply/);
    } finally {
      globalThis.fetch = realFetch;
    }
  });
});
