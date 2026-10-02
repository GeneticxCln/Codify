/**
 * The window's side of the surface bridge: a registry of surfaces, and the loop that polls the engine, hands each
 * question to the right one, and posts the answer.
 *
 * The loop has to be boring in three ways that are easy to get wrong. It must **never die** (an engine that is down, a
 * handler that throws, an answer that cannot be posted: each is a thing to survive and carry on, because a dead loop is
 * an assistant that is silently blind). It must **never spin** (an engine that answers instantly with nothing, or an
 * error, is waited out, not hammered). And it must **never answer for the wrong thing** (a question for a surface that
 * is not there is refused, not guessed at, and an answer is only ever for the question that was asked).
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { SurfaceRequest } from "../src/types.ts";

const { abortableSleep, createSurfaceRegistry, runSurfaceLoop } = await import("../src/surfaceLoop.ts");

const request = (over: Partial<SurfaceRequest> = {}): SurfaceRequest => ({
  id: "r1",
  surface: "editor",
  op: "read",
  workspace_id: "w1",
  args: {},
  ...over,
});

// ── the registry ─────────────────────────────────────────────────────────────

test("a question goes to the surface it names, with its op, args and workspace", async () => {
  const registry = createSurfaceRegistry();
  const seen: SurfaceRequest[] = [];
  registry.register("editor", async (r) => {
    seen.push(r);
    return { ok: true, result: { hello: "world" } };
  });

  const reply = await registry.dispatch(request({ op: "edit", args: { path: "a.py" } }));

  assert.deepEqual(reply, { ok: true, result: { hello: "world" } });
  assert.equal(seen[0].op, "edit");
  assert.deepEqual(seen[0].args, { path: "a.py" });
  assert.equal(seen[0].workspace_id, "w1");
});

test("a question for a surface that is not registered is refused by name", async () => {
  const registry = createSurfaceRegistry();

  const reply = await registry.dispatch(request({ surface: "terminal" }));

  assert.equal(reply.ok, false);
  assert.match((reply as { error: string }).error, /terminal/);
});

test("a handler that throws, or rejects, is a refusal and not an exception", async () => {
  const registry = createSurfaceRegistry();
  registry.register("editor", async () => {
    throw new Error("boom");
  });

  const reply = await registry.dispatch(request());

  assert.deepEqual(reply, { ok: false, error: "boom" });
});

test("a handler that returns something that is not a reply is a refusal, not trusted", async () => {
  const registry = createSurfaceRegistry();
  registry.register("editor", (async () => "not a reply") as never);

  const reply = await registry.dispatch(request());

  assert.equal(reply.ok, false);
});

test("a surface can be unregistered, and then it is not there", async () => {
  const registry = createSurfaceRegistry();
  const off = registry.register("editor", async () => ({ ok: true, result: 1 }));
  off();

  assert.equal((await registry.dispatch(request())).ok, false);
});

test("an older registration does not unregister a newer one", async () => {
  const registry = createSurfaceRegistry();
  const first = registry.register("editor", async () => ({ ok: true, result: "first" }));
  registry.register("editor", async () => ({ ok: true, result: "second" }));

  first();

  assert.deepEqual(await registry.dispatch(request()), { ok: true, result: "second" });
});

// ── the loop ─────────────────────────────────────────────────────────────────

interface Script {
  /** What each poll does, in order: a question, null, or an error. After the list, it waits for the abort. */
  polls: Array<SurfaceRequest | null | Error>;
  answers?: Array<Error | undefined>;
}

async function drive(script: Script, registry = createSurfaceRegistry()) {
  const controller = new AbortController();
  const posted: Array<{ id: string; ok: boolean; result?: unknown; error?: string }> = [];
  const slept: number[] = [];
  const waits: number[] = [];
  let poll = 0;
  let answer = 0;
  const io = {
    async next(wait: number, signal: AbortSignal): Promise<SurfaceRequest | null> {
      waits.push(wait);
      const step = script.polls[poll++];
      if (step === undefined) {
        await new Promise<void>((_, reject) => signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
        return null;
      }
      if (step instanceof Error) throw step;
      return step;
    },
    async answer(body: { id: string; ok: boolean; result?: unknown; error?: string }): Promise<void> {
      posted.push(body);
      const failure = script.answers?.[answer++];
      if (failure) throw failure;
    },
    async sleep(ms: number): Promise<void> {
      slept.push(ms);
    },
    now: () => 1_000_000,
  };
  const running = runSurfaceLoop(io, registry, controller.signal);
  for (let i = 0; i < 50; i += 1) await new Promise<void>((resolve) => setImmediate(resolve));
  controller.abort();
  await running;
  return { posted, slept, waits };
}

test("each question is answered, in order, with its own id", async () => {
  const registry = createSurfaceRegistry();
  registry.register("editor", async (r) => ({ ok: true, result: { echoed: r.op } }));

  const { posted } = await drive(
    { polls: [request({ id: "a", op: "read" }), request({ id: "b", op: "open" }), request({ id: "c", op: "edit" })] },
    registry,
  );

  assert.deepEqual(posted, [
    { id: "a", ok: true, result: { echoed: "read" } },
    { id: "b", ok: true, result: { echoed: "open" } },
    { id: "c", ok: true, result: { echoed: "edit" } },
  ]);
});

test("a refusal is posted as not ok with its reason", async () => {
  const registry = createSurfaceRegistry();
  registry.register("editor", async () => ({ ok: false, error: "that file is not open" }));

  const { posted } = await drive({ polls: [request({ id: "a" })] }, registry);

  assert.deepEqual(posted, [{ id: "a", ok: false, error: "that file is not open" }]);
});

test("a question for a surface that is not there is answered as a refusal, so the engine is not left waiting", async () => {
  const { posted } = await drive({ polls: [request({ id: "a", surface: "terminal" })] });

  assert.equal(posted.length, 1);
  assert.equal(posted[0].ok, false);
});

test("an empty poll asks again, and every poll waits as long as the engine can hold it", async () => {
  const { waits } = await drive({ polls: [null, null, null] });

  assert.ok(waits.length >= 4);
  assert.ok(waits.every((w) => w === 20), String(waits));
});

test("an engine that is down is waited out with a growing pause, and the loop carries on when it is back", async () => {
  const registry = createSurfaceRegistry();
  registry.register("editor", async () => ({ ok: true, result: 1 }));

  const { slept, posted } = await drive(
    { polls: [new Error("connection refused"), new Error("connection refused"), new Error("connection refused"), request({ id: "a" })] },
    registry,
  );

  assert.deepEqual(slept.slice(0, 3), [1000, 2000, 4000]);
  assert.deepEqual(posted.map((p) => p.id), ["a"]);
});

test("the pause stops growing, and starts over once a poll succeeds", async () => {
  const errors = Array.from({ length: 8 }, () => new Error("down"));

  const first = await drive({ polls: errors });
  assert.equal(Math.max(...first.slept), 10_000);

  const registry = createSurfaceRegistry();
  registry.register("editor", async () => ({ ok: true, result: 1 }));
  const again = await drive({ polls: [new Error("x"), new Error("x"), null, new Error("x")] }, registry);
  // 1 s, 2 s, then the empty poll that came back instantly (0.5 s), then the next error starting over from 1 s, not 4 s.
  assert.deepEqual(again.slept.slice(0, 4), [1000, 2000, 500, 1000], "a good poll in between put the pause back to the start");
});

test("an answer that cannot be posted does not stop the loop", async () => {
  const registry = createSurfaceRegistry();
  registry.register("editor", async () => ({ ok: true, result: 1 }));

  const { posted } = await drive(
    { polls: [request({ id: "a" }), request({ id: "b" })], answers: [new Error("network"), undefined] },
    registry,
  );

  assert.deepEqual(posted.map((p) => p.id), ["a", "b"]);
});

test("a handler that is slow does not get a second question handed to it at the same time", async () => {
  const registry = createSurfaceRegistry();
  let running = 0;
  let overlapped = false;
  registry.register("editor", async () => {
    running += 1;
    if (running > 1) overlapped = true;
    await new Promise<void>((resolve) => setImmediate(resolve));
    running -= 1;
    return { ok: true, result: 1 };
  });

  await drive({ polls: [request({ id: "a" }), request({ id: "b" }), request({ id: "c" })] }, registry);

  assert.equal(overlapped, false);
});

test("aborting ends the loop, quietly, even in the middle of a poll", async () => {
  const { posted, slept } = await drive({ polls: [] });

  assert.deepEqual(posted, []);
  assert.deepEqual(slept, []);
});

test("an engine that answers at once with nothing is not hammered", async () => {
  const { slept } = await drive({ polls: [null, null, null, null] });

  assert.ok(slept.length >= 3 && slept.every((ms) => ms >= 250), `an instant empty poll must be waited out: ${slept}`);
});


// ── the pause ────────────────────────────────────────────────────────────────

test("a pause lasts as long as it was asked to, and ends early the moment the loop is told to stop", async () => {
  const quick = new AbortController();
  const started = Date.now();
  await abortableSleep(30, quick.signal);
  assert.ok(Date.now() - started >= 25, "the pause ended before its time");

  const stopped = new AbortController();
  const long = abortableSleep(60_000, stopped.signal);
  stopped.abort();
  await assert.rejects(long, (error: Error) => error.name === "AbortError");
});

test("a pause asked for after the loop was told to stop does not even start", async () => {
  const controller = new AbortController();
  controller.abort();

  await assert.rejects(abortableSleep(60_000, controller.signal), (error: Error) => error.name === "AbortError");
});

test("a pause that is stopped cancels its timer, so a long pause does not outlive the loop", async () => {
  const realSet = globalThis.setTimeout;
  const realClear = globalThis.clearTimeout;
  const started: unknown[] = [];
  const cleared: unknown[] = [];
  // Only the pause's own timer (a minute long) is recorded: the test runner has timers of its own.
  globalThis.setTimeout = ((fn: () => void, ms?: number, ...rest: unknown[]) => {
    const timer = realSet(fn, ms, ...rest);
    if (ms === 60_000) started.push(timer);
    return timer;
  }) as typeof setTimeout;
  globalThis.clearTimeout = ((timer: Parameters<typeof clearTimeout>[0]) => {
    cleared.push(timer);
    realClear(timer);
  }) as typeof clearTimeout;
  try {
    const controller = new AbortController();
    const pause = abortableSleep(60_000, controller.signal);
    controller.abort();
    await assert.rejects(pause);

    assert.equal(started.length, 1);
    assert.ok(cleared.includes(started[0]), "the stopped pause left its timer running");
  } finally {
    globalThis.setTimeout = realSet;
    globalThis.clearTimeout = realClear;
  }
});

test("a pause that ran its course leaves nothing listening to the signal, however many pauses a long run makes", async () => {
  const controller = new AbortController();
  const signal = controller.signal;
  const added: unknown[] = [];
  const removed: unknown[] = [];
  const add = signal.addEventListener.bind(signal);
  const remove = signal.removeEventListener.bind(signal);
  signal.addEventListener = ((type: string, listener: EventListenerOrEventListenerObject, options?: boolean | AddEventListenerOptions) => {
    added.push(listener);
    add(type, listener, options);
  }) as typeof signal.addEventListener;
  signal.removeEventListener = ((type: string, listener: EventListenerOrEventListenerObject, options?: boolean | EventListenerOptions) => {
    removed.push(listener);
    remove(type, listener, options);
  }) as typeof signal.removeEventListener;

  await abortableSleep(5, signal);

  assert.equal(added.length, 1);
  assert.deepEqual(removed, added, "the finished pause left its listener on the signal");
});
