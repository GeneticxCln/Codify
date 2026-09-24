import test from "node:test";
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
  onclose: (() => void) | null;
  onerror: (() => void) | null;
  closed: boolean;
}

const sockets: SentSocket[] = [];

class MockWebSocket {
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
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

const { openGoalStream } = await import("../src/goalStream.ts");
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
