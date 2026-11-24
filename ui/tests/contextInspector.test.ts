/**
 * Context inspector & composer "Record next turn" toggle, tested end-to-end
 * against the mounted App and fake engine (`appHarness.ts`).
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withApp, conversation } = await import("./appHarness.ts");

test("Record next turn arms one submitted turn with trace=true and disarms for the next", async () => {
  await withApp(
    {
      conversations: [conversation({ id: "c1", title: "Thread" })],
    },
    async ({ dom, engine, settle }) => {
      const row = dom.container.querySelector('[data-thread-row="true"]');
      assert.ok(row, "thread row missing");
      await dom.click(row);
      await settle();

      const recordBtn = dom.container.querySelector(
        'button[aria-label="Record next turn"]',
      ) as HTMLButtonElement | null;
      assert.ok(recordBtn, "composer is missing the Record next turn toggle");
      assert.equal(recordBtn.getAttribute("aria-pressed"), "false");

      await dom.click(recordBtn);
      await settle();
      assert.equal(recordBtn.getAttribute("aria-pressed"), "true");

      const textarea = dom.container.querySelector(
        'textarea[aria-label="Chat prompt"]',
      ) as HTMLTextAreaElement | null;
      assert.ok(textarea, "chat prompt textarea missing");
      await dom.fill(textarea, "explain how trace works");
      await dom.press(textarea, "Enter");
      await settle();

      const turns = engine.filter((c) => c.method === "POST" && c.path === "/conversations/c1/turns");
      assert.equal(turns.length, 1);
      assert.equal((turns[0].body as Record<string, unknown>).trace, true);
      // One-shot: disarms right after dispatch so subsequent turns are not quietly recorded.
      assert.equal(recordBtn.getAttribute("aria-pressed"), "false");

      await dom.fill(textarea, "second unrecorded message");
      await dom.press(textarea, "Enter");
      await settle();

      const allTurns = engine.filter((c) => c.method === "POST" && c.path === "/conversations/c1/turns");
      assert.equal(allTurns.length, 2);
      assert.equal((allTurns[1].body as Record<string, unknown>).trace, undefined);
    },
  );
});

test("TracePanel inspects full system prompt, user context and model reply on row click", async () => {
  await withApp(
    {
      conversations: [conversation({ id: "c1", title: "Debug thread" })],
      goals: [
        {
          id: "g1",
          conversation_id: "c1",
          title: "Check parser",
          mode: "chat",
          status: "COMPLETED",
          trace: true,
        },
      ],
      goalEvents: {
        g1: [
          {
            id: "ev-1",
            goal_id: "g1",
            step_id: null,
            type: "log",
            payload: { level: "info", turn: true, message: "The parser validates AST nodes." },
            timestamp: 1,
            sequence: 1,
          },
        ],
      },
      traceCalls: {
        g1: [
          {
            seq: 1,
            role: "conductor",
            provider: "ollama",
            model: "qwen2.5-coder:7b",
            prompt_hash: "abcdef0123456789abcdef0123456789",
            system_prompt: "You are the Codify conductor.",
            user_prompt: JSON.stringify([{ role: "user", content: "Check parser" }]),
            response: JSON.stringify({
              text: "The parser validates AST nodes.",
              tool_calls: [],
            }),
            input_tokens: 120,
            output_tokens: 24,
            duration_ms: 85,
          },
        ],
      },
    },
    async ({ dom, engine, settle }) => {
      const row = dom.container.querySelector('[data-thread-row="true"]');
      assert.ok(row);
      await dom.click(row);
      await settle();

      const openRecording = dom.container.querySelector(
        'button[aria-label="Show recording"]',
      ) as HTMLElement | null;
      assert.ok(openRecording, "recorded conversational turn must show Recording button");
      await dom.click(openRecording);
      await settle();

      // Opening the panel fetches only the summary, not the full call body yet.
      assert.equal(
        engine.filter((c) => c.method === "GET" && c.path === "/goals/g1/trace/1").length,
        0,
      );
      assert.match(dom.text(), /1 call recorded/);
      assert.match(dom.text(), /abcdef01…/);

      const inspectBtn = dom.container.querySelector(
        'button[aria-label="Inspect call 1"]',
      ) as HTMLElement | null;
      assert.ok(inspectBtn, "call row must offer Inspect call 1");
      await dom.click(inspectBtn);
      await settle();

      assert.equal(
        engine.filter((c) => c.method === "GET" && c.path === "/goals/g1/trace/1").length,
        1,
      );
      const sysPre = dom.container.querySelector('[data-trace-section="system"]');
      const userPre = dom.container.querySelector('[data-trace-section="user"]');
      const respPre = dom.container.querySelector('[data-trace-section="response"]');
      assert.ok(sysPre && userPre && respPre, "inspector must render system, user and response sections");
      assert.match(sysPre.textContent ?? "", /You are the Codify conductor/);
      assert.match(userPre.textContent ?? "", /"content": "Check parser"/);
      assert.match(respPre.textContent ?? "", /The parser validates AST nodes/);
    },
  );
});
