import test, { mock } from "node:test";
import assert from "node:assert/strict";

// api.ts reads localStorage at module load — stub it before the dynamic import. The stored
// connection value is a recognisable placeholder so the auth frame can be checked against it.
const STORED_CREDENTIAL = "placeholder-for-the-auth-frame";
(globalThis as any).localStorage = {
  getItem: (key: string) => (key === "CODIFY_TOKEN" ? STORED_CREDENTIAL : null),
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

const { openEngineStream, STABLE_CONNECTION_MS } = await import("../src/engineStream.ts");

function lastSocket(): SentSocket {
  const s = sockets[sockets.length - 1];
  assert.ok(s, "expected a WebSocket to have been opened");
  return s;
}

/** Accept the upgrade, then have the engine refuse the credential: the open -> close(4401) shape. */
function openThenReject(): void {
  const ws = lastSocket();
  ws.onopen?.();
  ws.onclose?.({ code: 4401 });
}

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

test("the stability window is 10 s, pinned as a literal and not read back from the module", () => {
  assert.equal(STABLE_CONNECTION_MS, 10_000);
});

test("auth frame is sent with the stored value on open, and onConnected fires", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    sockets.length = 0;
    let connected = 0;
    const handle = openEngineStream({ onFrame: () => {}, onConnected: () => (connected += 1) });
    const ws = lastSocket();
    assert.match(ws.url, /^ws:\/\/127\.0\.0\.1:\d+\/ws\/engine$/);
    assert.equal(ws.sent.length, 0, "nothing is sent before the upgrade is accepted");
    ws.onopen?.();
    assert.equal(ws.sent.length, 1);
    assert.deepEqual(JSON.parse(ws.sent[0]), { type: "auth", token: STORED_CREDENTIAL });
    assert.equal(connected, 1);
    handle.close();
  } finally {
    mock.timers.reset();
  }
});

test("open then an immediate 4401, repeatedly, backs off 1,2,4,8,16,16 s and not earlier", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    sockets.length = 0;
    const handle = openEngineStream({ onFrame: () => {} });
    assert.equal(sockets.length, 1);

    for (let i = 0; i < BACKOFF_MS.length; i++) {
      const delay = BACKOFF_MS[i];
      const before: number = sockets.length;
      openThenReject();
      assert.equal(sockets.length, before, `a socket was opened with no delay on round ${i + 1}`);

      mock.timers.tick(delay - 1);
      assert.equal(sockets.length, before, `reconnected before ${delay} ms on round ${i + 1}`);

      mock.timers.tick(1);
      assert.equal(sockets.length, before + 1, `did not reconnect at ${delay} ms on round ${i + 1}`);
    }
    handle.close();
  } finally {
    mock.timers.reset();
  }
});

test("onReconnecting is told 1, 2, 3, ... while connections keep being dropped after open", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    sockets.length = 0;
    const told: number[] = [];
    const handle = openEngineStream({ onFrame: () => {}, onReconnecting: (n) => told.push(n) });
    for (const delay of BACKOFF_MS) {
      openThenReject();
      mock.timers.tick(delay);
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
    const handle = openEngineStream({ onFrame: () => {}, onReconnecting: (n) => told.push(n) });

    // Climb the backoff first: three short-lived connections, so the next delay would be 8 s.
    for (const delay of BACKOFF_MS.slice(0, 3)) {
      openThenReject();
      mock.timers.tick(delay);
    }
    assert.deepEqual(told, [1, 2, 3]);

    // This one survives the window, then drops.
    const stable = lastSocket();
    stable.onopen?.();
    mock.timers.tick(STABLE_CONNECTION_MS);
    stable.onclose?.({ code: 1006 });
    assert.deepEqual(told, [1, 2, 3, 1], "onReconnecting says attempt 1 again");

    const before: number = sockets.length;
    mock.timers.tick(999);
    assert.equal(sockets.length, before, "reconnected before 1 s");
    mock.timers.tick(1);
    assert.equal(sockets.length, before + 1, "did not reconnect after 1 s");
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
    const handle = openEngineStream({ onFrame: () => {}, onReconnecting: (n) => told.push(n) });

    openThenReject();
    mock.timers.tick(1000);
    openThenReject();
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
    const handle = openEngineStream({ onFrame: () => {}, onReconnecting: (n) => told.push(n) });
    const ws = lastSocket();
    ws.onopen?.();
    mock.timers.tick(STABLE_CONNECTION_MS - 1);
    assert.equal(timers.pending(), 1, "the stability timer should be armed");

    handle.close();
    assert.equal(ws.closed, true);
    assert.equal(timers.pending(), 0, "close() left a timer pending");

    assert.doesNotThrow(() => mock.timers.tick(10 * STABLE_CONNECTION_MS));
    assert.equal(sockets.length, 1, "a socket was opened after close()");
    assert.deepEqual(told, []);

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
