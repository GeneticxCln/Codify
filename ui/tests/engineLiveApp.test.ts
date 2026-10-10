/**
 * The engine's catalogue stream, through the whole App: what "connected" does to the model list.
 *
 * `engineStream.test.ts` holds the rule (a bare open is not a connection; one the engine did not reject within
 * `CONNECTION_PROVEN_MS`, or that sent a frame, is). What it cannot show is the consequence in `App.tsx`: a stream
 * the engine keeps refusing must not make the app re-read `GET /models` on every retry, and a connection that
 * was proven, lost and proven again must re-read exactly once. These tests are the engine on the other end of a
 * recorded socket, waiting out the real backoff.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext, HarnessSocket } from "./appHarness.ts";

const { withApp } = await import("./appHarness.ts");
const { CONNECTION_PROVEN_MS } = await import("../src/engineStream.ts");

const engineSockets = (ctx: AppContext): HarnessSocket[] => ctx.sockets.filter((s) => s.url.endsWith("/ws/engine"));

/** Plain `GET /models` reads: the catalogue re-read a frame or a reconnect asks for. A refresh is the app's own open-time read. */
const rereads = (ctx: AppContext): number =>
  ctx.engine.filter((c) => c.method === "GET" && c.path === "/models" && !/refresh=true/.test(c.url)).length;

const wait = (ctx: AppContext, ms: number): Promise<void> => ctx.act(() => new Promise<void>((r) => setTimeout(r, ms)));

/** Wait until the app has opened `n` engine sockets (the backoff is real time), then return the newest. */
async function nthEngineSocket(ctx: AppContext, n: number): Promise<HarnessSocket> {
  const deadline = Date.now() + 6000;
  while (engineSockets(ctx).length < n) {
    if (Date.now() > deadline) throw new Error(`the app opened ${engineSockets(ctx).length} engine sockets, wanted ${n}`);
    await wait(ctx, 50);
  }
  return engineSockets(ctx)[n - 1]!;
}

test("a stream the engine keeps refusing does not re-read the catalogue on every retry", async () => {
  await withApp({}, async (ctx) => {
    await ctx.settle();
    const before = rereads(ctx);
    for (let round = 1; round <= 3; round++) {
      const socket = await nthEngineSocket(ctx, round);
      await socket.open();
      await socket.drop(4401);
      await ctx.settle();
    }
    assert.equal(engineSockets(ctx).length >= 3, true);
    assert.equal(rereads(ctx), before, "a refused socket was treated as a connection and re-read /models");
  });
});

test("a proven connection that is lost and proven again re-reads the catalogue once", async () => {
  await withApp({}, async (ctx) => {
    await ctx.settle();
    const first = await nthEngineSocket(ctx, 1);
    await first.open();
    await wait(ctx, CONNECTION_PROVEN_MS + 500);
    await ctx.settle();
    const before = rereads(ctx);

    await first.drop(1006);
    const second = await nthEngineSocket(ctx, 2);
    await second.open();
    assert.equal(rereads(ctx), before, "the reconnect re-read before it was proven");
    // Real timers on a loaded machine: poll for the re-read rather than trusting one fixed sleep, then
    // wait a little longer to show it was one and not several.
    const deadline = Date.now() + 5000;
    while (rereads(ctx) === before && Date.now() < deadline) await wait(ctx, 100);
    await wait(ctx, 300);
    await ctx.settle();
    assert.equal(rereads(ctx), before + 1, "a proven reconnect should re-read exactly once");
  });
});
