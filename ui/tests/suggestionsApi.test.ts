/**
 * The inbox's five calls: the paths, methods and bodies the engine's routes expect (docs/04 §3). A rename on either
 * side is a failing test here rather than a Send button that quietly does nothing.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");

interface Call {
  method: string;
  path: string;
  search: string;
  body: unknown;
}

async function recording(answer: unknown, body: (calls: Call[]) => Promise<void>): Promise<void> {
  await withDom(async () => {
    const calls: Call[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      calls.push({
        method: (init?.method ?? "GET").toUpperCase(),
        path: url.pathname,
        search: url.search,
        body: typeof init?.body === "string" ? JSON.parse(init.body) : null,
      });
      return new Response(JSON.stringify(answer), { status: 200, headers: { "content-type": "application/json" } });
    }) as typeof fetch;
    await body(calls);
  });
}

test("the inbox is read, scanned, snoozed, dismissed and recorded through the routes the engine has", async () => {
  const api = await import("../src/api.ts");
  await recording({ suggestions: [], snoozed: 0 }, async (calls) => {
    await api.listSuggestions();
    await api.listSuggestions("w 1");
    await api.scanSuggestions();
    await api.snoozeSuggestion("s/1", 24);
    await api.dismissSuggestion("s1");
    await api.recordSuggestionAccepted("s1", "g1");
    assert.deepEqual(
      calls.map((c) => [c.method, c.path + c.search, c.body]),
      [
        ["GET", "/suggestions", null],
        ["GET", "/suggestions?workspace_id=w%201", null],
        ["POST", "/suggestions/scan", null],
        ["POST", "/suggestions/s%2F1/snooze", { hours: 24 }],
        ["POST", "/suggestions/s1/dismiss", null],
        ["POST", "/suggestions/s1/accept", { goal_id: "g1" }],
      ],
    );
  });
});

test("a refusal comes back as an error with the engine's words, not as a resolved empty inbox", async () => {
  const api = await import("../src/api.ts");
  await withDom(async () => {
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ code: "illegal_status", message: "this suggestion is already dismissed" }), {
        status: 409,
        headers: { "content-type": "application/json" },
      })) as typeof fetch;
    await assert.rejects(api.dismissSuggestion("s1"), /already dismissed/);
  });
});
