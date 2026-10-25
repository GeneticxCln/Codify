/**
 * What the app says when the engine answers badly.
 *
 * Two small things in `api.ts` that a person sees when something is wrong: a settings save that is refused
 * (the card should show the engine's sentence, not the JSON it arrived in), and the liveness probe meeting a
 * server that answers 200 with something that is not our engine's health document.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const api = await import("../src/api.ts");

async function withReply<T>(reply: () => Response, body: () => Promise<T>): Promise<T> {
  const original = globalThis.fetch;
  api.setEngineInfo({ port: 43118, token: "boot-token-under-test" });
  globalThis.fetch = (async () => reply()) as unknown as typeof fetch;
  try {
    return await body();
  } finally {
    globalThis.fetch = original;
  }
}

test("a refused settings save throws the engine's sentence, not its JSON", async () => {
  await withReply(
    () =>
      new Response(JSON.stringify({ code: "invalid_value", message: "parallel_width must be between 1 and 8" }), {
        status: 422,
        headers: { "content-type": "application/json" },
      }),
    async () => {
      await assert.rejects(
        () => api.saveEngineSettings({ parallel_width: 99 }),
        (err: Error) => {
          assert.equal(err.message, "parallel_width must be between 1 and 8");
          return true;
        },
      );
    },
  );
});

test("a refused settings save with a body that is not the engine's shape still says something", async () => {
  await withReply(
    () => new Response("upstream exploded", { status: 502 }),
    async () => {
      await assert.rejects(
        () => api.saveEngineSettings({ parallel_width: 2 }),
        (err: Error) => {
          assert.equal(err.message, "upstream exploded");
          return true;
        },
      );
    },
  );
  await withReply(
    () => new Response("", { status: 500 }),
    async () => {
      await assert.rejects(
        () => api.saveEngineSettings({ parallel_width: 2 }),
        (err: Error) => {
          assert.match(err.message, /500/);
          return true;
        },
      );
    },
  );
});

test("a 200 whose body is not JSON is an engine that is not there, not a probe that throws", async () => {
  await withReply(
    () => new Response("<html>some other local server</html>", { status: 200, headers: { "content-type": "text/html" } }),
    async () => {
      const health = await api.checkEngineHealth();
      assert.deepEqual(health, { ok: false, authenticated: false });
    },
  );
});

test("a healthy engine's answer is passed through", async () => {
  await withReply(
    () => new Response(JSON.stringify({ ok: true, authenticated: true }), { status: 200 }),
    async () => {
      const health = await api.checkEngineHealth();
      assert.equal(health.ok, true);
      assert.equal(health.authenticated, true);
    },
  );
});
