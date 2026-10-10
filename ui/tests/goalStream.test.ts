import test, { mock } from "node:test";
import assert from "node:assert/strict";

// api.ts reads localStorage at module load — stub it before the dynamic import.
(globalThis as any).localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};

interface SentSocket {
  url: string;
  sent: string[];
  onopen: (() => void) | null;
  onmessage: ((e: { data: string }) => void) | null;
  onclose: ((e: { code?: number }) => void) | null;
  onerror: (() => void) | null;
  closed: boolean;
}

const sockets: SentSocket[] = [];

class MockWebSocket {
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code?: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  sent: string[] = [];
  url: string;

  constructor(url: string) {
    this.url = url;
    sockets.push(this as unknown as SentSocket);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.closed = true;
  }
}

(globalThis as any).WebSocket = MockWebSocket;

const { openGoalStream, STABLE_CONNECTION_MS } = await import("../src/goalStream.ts");
import type { Event } from "../src/types.ts";

function goalEvent(sequence: number, status: string): Event {
  return {
    id: `ev-${sequence}`,
    goal_id: "goal-1",
    type: "goal_status",
    payload: { status },
    timestamp: sequence,
    sequence,
  };
}

function lastSocket(): SentSocket {
  const s = sockets[sockets.length - 1];
  assert.ok(s, "expected a WebSocket to have been opened");
  return s;
}

test("auth handshake is sent on open", () => {
  sockets.length = 0;
  const handle = openGoalStream({ goalId: "goal-1", onEvent: () => {} });
  const ws = lastSocket();
  ws.onopen?.();
  assert.equal(ws.sent.length, 1);
  const auth = JSON.parse(ws.sent[0]);
  assert.equal(auth.type, "auth");
  handle.close();
});

test("live events are delivered; a terminal event fires onTerminal and closes", () => {
  sockets.length = 0;
  const seen: Event[] = [];
  let terminal: Event | null = null;
  const handle = openGoalStream({
    goalId: "goal-1",
    onEvent: (ev) => seen.push(ev),
    onTerminal: (ev) => {
      terminal = ev;
    },
  });
  const ws = lastSocket();
  ws.onopen?.();
  (ws.onmessage as any)?.({ data: JSON.stringify(goalEvent(1, "RUNNING")) });
  (ws.onmessage as any)?.({ data: JSON.stringify(goalEvent(2, "COMPLETED")) });
  assert.deepEqual(seen.map((e) => e.sequence), [1, 2]);
  assert.equal((terminal as unknown as Event | null)?.sequence, 2);
  assert.equal(ws.closed, true);
  handle.close();
});

test("replayed history at or below sinceSequence never triggers onTerminal", () => {
  sockets.length = 0;
  let terminalCalls = 0;
  const handle = openGoalStream({
    goalId: "goal-1",
    onEvent: () => {},
    onTerminal: () => {
      terminalCalls += 1;
    },
    sinceSequence: 5,
  });
  const ws = lastSocket();
  ws.onopen?.();
  (ws.onmessage as any)?.({ data: JSON.stringify(goalEvent(5, "COMPLETED")) });
  assert.equal(terminalCalls, 0);
  assert.equal(ws.closed, false);
  handle.close();
});

test("unparseable frames do not reach onEvent", () => {
  sockets.length = 0;
  let calls = 0;
  const errors: unknown[] = [];
  const orig = console.error;
  console.error = (...args: unknown[]) => {
    errors.push(args);
  };
  try {
    const handle = openGoalStream({ goalId: "goal-1", onEvent: () => (calls += 1) });
    const ws = lastSocket();
    ws.onopen?.();
    (ws.onmessage as any)?.({ data: "not-json{" });
    assert.equal(calls, 0);
    assert.equal(errors.length, 1);
    handle.close();
  } finally {
    console.error = orig;
  }
});

// ── M9: a close the engine meant is not a dropped connection ─────────────────────────────────
//
// The engine closes with 4404 when the goal is gone and 4401 when the token was refused. Neither
// changes on its own, and reconnecting forever at a 16 s cadence just re-asks a question that has
// been answered. An ordinary drop (1006, a restarted engine) must still reconnect.

test("a close for 'no such goal' (4404) is final: no reconnect, and the caller is told", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    sockets.length = 0;
    const gone: number[] = [];
    const reconnecting: number[] = [];
    const handle = openGoalStream({
      goalId: "goal-1",
      onEvent: () => {},
      onGone: (code) => gone.push(code),
      onReconnecting: (n) => reconnecting.push(n),
    });
    lastSocket().onclose?.({ code: 4404 });

    mock.timers.tick(120_000);

    assert.equal(sockets.length, 1, "a socket was reopened for a goal that does not exist");
    assert.deepEqual(gone, [4404]);
    assert.deepEqual(reconnecting, []);
    handle.close();
  } finally {
    mock.timers.reset();
  }
});

test("a close for a refused token (4401) is final too", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    sockets.length = 0;
    const gone: number[] = [];
    const handle = openGoalStream({ goalId: "goal-1", onEvent: () => {}, onGone: (code) => gone.push(code) });
    lastSocket().onclose?.({ code: 4401 });

    mock.timers.tick(120_000);

    assert.equal(sockets.length, 1);
    assert.deepEqual(gone, [4401]);
    handle.close();
  } finally {
    mock.timers.reset();
  }
});

test("an ordinary drop still reconnects, with backoff", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    sockets.length = 0;
    const reconnecting: number[] = [];
    const gone: number[] = [];
    const handle = openGoalStream({
      goalId: "goal-1",
      onEvent: () => {},
      onReconnecting: (n) => reconnecting.push(n),
      onGone: (code) => gone.push(code),
    });
    lastSocket().onclose?.({ code: 1006 });
    assert.deepEqual(reconnecting, [1]);
    assert.equal(sockets.length, 1, "reconnects wait for their delay");

    mock.timers.tick(1000);
    assert.equal(sockets.length, 2, "an ordinary drop did not reconnect");

    lastSocket().onclose?.({ code: 1006 });
    mock.timers.tick(2000);
    assert.equal(sockets.length, 3, "the second reconnect is on the backed-off delay");
    assert.deepEqual(gone, []);
    handle.close();
  } finally {
    mock.timers.reset();
  }
});

test("closing the handle after a final close is harmless", () => {
  sockets.length = 0;
  const handle = openGoalStream({ goalId: "goal-1", onEvent: () => {} });
  lastSocket().onclose?.({ code: 4404 });
  assert.doesNotThrow(() => handle.close());
});

// ── The backoff counter resets on a connection that lasted, not on open ───────────────────────
//
// The engine accepts the upgrade before it authenticates and replays the whole log from 0, so a
// socket that opens and then dies (1011) used to put the delay back at 1 s every time.

/**
 * Wrap the (mocked) global timers and track which are still pending. Call after
 * `mock.timers.enable`; the returned `restore` puts the mock's own functions back.
 */
function trackTimers(): { pending: () => number; restore: () => void } {
  const g = globalThis as any;
  const realSet = g.setTimeout;
  const realClear = g.clearTimeout;
  const live = new Set<unknown>();
  g.setTimeout = (fn: (...a: unknown[]) => void, ms?: number, ...rest: unknown[]) => {
    const handle: unknown = realSet(
      (...a: unknown[]) => {
        live.delete(handle);
        fn(...a);
      },
      ms,
      ...rest,
    );
    live.add(handle);
    return handle;
  };
  g.clearTimeout = (handle: unknown) => {
    live.delete(handle);
    return realClear(handle);
  };
  return {
    pending: () => live.size,
    restore: () => {
      g.setTimeout = realSet;
      g.clearTimeout = realClear;
    },
  };
}

/** The delays the documented backoff walks: 1s, 2s, 4s, 8s, 16s, then 16s for good. */
const BACKOFF_MS = [1000, 2000, 4000, 8000, 16000, 16000];

/** Accept the upgrade, replay a few events from 0, then die after accept (an engine fault). */
function openReplayThenFail(code = 1011): void {
  const ws = lastSocket();
  ws.onopen?.();
  for (let seq = 1; seq <= 5; seq++) {
    (ws.onmessage as any)?.({ data: JSON.stringify(goalEvent(seq, "RUNNING")) });
  }
  ws.onclose?.({ code });
}

test("the stability window is 10 s, pinned as a literal and not read back from the module", () => {
  assert.equal(STABLE_CONNECTION_MS, 10_000);
});

test("open, replay, 1011, repeatedly: backs off 1,2,4,8,16,16 s and not at 1 Hz", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    sockets.length = 0;
    const told: number[] = [];
    const handle = openGoalStream({
      goalId: "goal-1",
      onEvent: () => {},
      onReconnecting: (n) => told.push(n),
    });
    assert.equal(sockets.length, 1);

    for (let i = 0; i < BACKOFF_MS.length; i++) {
      const delay = BACKOFF_MS[i];
      const before: number = sockets.length;
      openReplayThenFail();
      assert.equal(sockets.length, before, `a socket was opened with no delay on round ${i + 1}`);

      mock.timers.tick(delay - 1);
      assert.equal(sockets.length, before, `reconnected before ${delay} ms on round ${i + 1}`);

      mock.timers.tick(1);
      assert.equal(sockets.length, before + 1, `did not reconnect at ${delay} ms on round ${i + 1}`);
    }
    assert.deepEqual(told, [1, 2, 3, 4, 5, 6]);
    handle.close();
  } finally {
    mock.timers.reset();
  }
});

test("a connection that stayed open for the stability window resets the backoff to 1 s", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    sockets.length = 0;
    const told: number[] = [];
    const handle = openGoalStream({
      goalId: "goal-1",
      onEvent: () => {},
      onReconnecting: (n) => told.push(n),
    });

    // Climb first: three short-lived connections, so the next delay would be 8 s.
    for (const delay of BACKOFF_MS.slice(0, 3)) {
      openReplayThenFail();
      mock.timers.tick(delay);
    }
    assert.deepEqual(told, [1, 2, 3]);

    const stable = lastSocket();
    stable.onopen?.();
    mock.timers.tick(STABLE_CONNECTION_MS);
    stable.onclose?.({ code: 1006 });
    assert.deepEqual(told, [1, 2, 3, 1], "onReconnecting says attempt 1 again");

    const before: number = sockets.length;
    mock.timers.tick(999);
    assert.equal(sockets.length, before, "reconnected before 1 s");
    mock.timers.tick(1);
    assert.equal(sockets.length, before + 1, "did not reconnect at exactly 1 s");
    handle.close();
  } finally {
    mock.timers.reset();
  }
});

test("a connection dropped one tick before the window does not reset the backoff", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    sockets.length = 0;
    const told: number[] = [];
    const handle = openGoalStream({
      goalId: "goal-1",
      onEvent: () => {},
      onReconnecting: (n) => told.push(n),
    });

    openReplayThenFail();
    mock.timers.tick(1000);
    openReplayThenFail();
    mock.timers.tick(2000);
    assert.deepEqual(told, [1, 2]);

    const nearlyStable = lastSocket();
    nearlyStable.onopen?.();
    mock.timers.tick(STABLE_CONNECTION_MS - 1);
    nearlyStable.onclose?.({ code: 1006 });
    assert.deepEqual(told, [1, 2, 3]);

    const before: number = sockets.length;
    mock.timers.tick(3999);
    assert.equal(sockets.length, before, "reconnected before 4 s");
    mock.timers.tick(1);
    assert.equal(sockets.length, before + 1, "did not reconnect at 4 s");

    // The window's timer was cleared on close, so it does not fire late and reset anything.
    mock.timers.tick(STABLE_CONNECTION_MS);
    assert.equal(sockets.length, before + 1);
    handle.close();
  } finally {
    mock.timers.reset();
  }
});

test("close() while the stability timer is pending leaves nothing that fires later", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const timers = trackTimers();
  try {
    sockets.length = 0;
    const told: number[] = [];
    const handle = openGoalStream({
      goalId: "goal-1",
      onEvent: () => {},
      onReconnecting: (n) => told.push(n),
    });
    const ws = lastSocket();
    ws.onopen?.();
    mock.timers.tick(STABLE_CONNECTION_MS - 1);
    assert.equal(timers.pending(), 1, "the stability timer should be armed");

    handle.close();
    assert.equal(ws.closed, true);
    assert.equal(timers.pending(), 0, "close() left a timer pending");

    assert.doesNotThrow(() => mock.timers.tick(10 * STABLE_CONNECTION_MS));
    assert.equal(sockets.length, 1, "a socket was opened after close()");

    // A late onclose from the closed socket is not a reason to reconnect either.
    ws.onclose?.({ code: 1005 });
    mock.timers.tick(10 * STABLE_CONNECTION_MS);
    assert.equal(sockets.length, 1);
    assert.equal(timers.pending(), 0);
    assert.deepEqual(told, []);
  } finally {
    timers.restore();
    mock.timers.reset();
  }
});

test("a terminal event inside the window closes the stream and leaves no timer", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const timers = trackTimers();
  try {
    sockets.length = 0;
    const told: number[] = [];
    let terminalCalls = 0;
    openGoalStream({
      goalId: "goal-1",
      onEvent: () => {},
      onTerminal: () => (terminalCalls += 1),
      onReconnecting: (n) => told.push(n),
    });
    const ws = lastSocket();
    ws.onopen?.();
    mock.timers.tick(STABLE_CONNECTION_MS - 1);
    assert.equal(timers.pending(), 1, "the stability timer should be armed");

    (ws.onmessage as any)?.({ data: JSON.stringify(goalEvent(1, "COMPLETED")) });
    assert.equal(terminalCalls, 1);
    assert.equal(ws.closed, true);
    assert.equal(timers.pending(), 0, "the terminal path left the stability timer pending");

    // The close that follows is the one we asked for: no reconnect.
    ws.onclose?.({ code: 1000 });
    mock.timers.tick(10 * STABLE_CONNECTION_MS);
    assert.equal(sockets.length, 1);
    assert.equal(timers.pending(), 0);
    assert.deepEqual(told, []);
  } finally {
    timers.restore();
    mock.timers.reset();
  }
});

test("4401 and 4404 after an open stay final and clear the stability timer", () => {
  for (const code of [4401, 4404]) {
    mock.timers.enable({ apis: ["setTimeout"] });
    const timers = trackTimers();
    try {
      sockets.length = 0;
      const gone: number[] = [];
      const told: number[] = [];
      openGoalStream({
        goalId: "goal-1",
        onEvent: () => {},
        onGone: (c) => gone.push(c),
        onReconnecting: (n) => told.push(n),
      });
      const ws = lastSocket();
      ws.onopen?.();
      assert.equal(timers.pending(), 1, `${code}: the stability timer should be armed`);

      ws.onclose?.({ code });
      assert.equal(timers.pending(), 0, `${code} left the stability timer pending`);
      assert.deepEqual(gone, [code]);

      mock.timers.tick(10 * STABLE_CONNECTION_MS);
      assert.equal(sockets.length, 1, `${code}: a socket was reopened`);
      assert.deepEqual(told, []);
    } finally {
      timers.restore();
      mock.timers.reset();
    }
  }
});
