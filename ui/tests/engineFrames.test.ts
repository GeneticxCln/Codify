/**
 * What one engine-level frame does.
 *
 * `readEngineFrame` is the whole decision, split out of the socket so it can be
 * tested without one — and it is worth its own tests because the connection it
 * runs on is shared by every screen in the app. A frame from a future engine
 * build, or a truncated one, has to be survivable: the alternative is a single
 * bad frame taking down the settings panel, the command bar and the chat at once,
 * all of which are listening on the same socket.
 *
 * Imported from `engineFrames` rather than `engineStream`: the socket module
 * pulls in the API client, which reads `localStorage` while it loads, and a test
 * for a pure decision should not need a browser to reach it.
 *
 * The engine's own half — that it only sends a frame when something really moved,
 * and that the payload is a diff rather than a catalogue — is pinned in
 * `tests/test_catalog_watch.py`.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { readEngineFrame } from "../src/engineFrames.ts";
import type { EngineEvent } from "../src/types.ts";

const FRAME = {
  type: "model_catalog_changed",
  payload: {
    added: { openrouter: ["anthropic/claude-x"] },
    removed: {},
    fetched_at: 1_700_000_000,
  },
};

const collect = () => {
  const seen: EngineEvent[] = [];
  return { seen, onFrame: (ev: EngineEvent) => seen.push(ev) };
};

test("a catalogue frame reaches the reader with its diff intact", () => {
  // The claim the whole feature rests on: the engine says a provider's list
  // moved, and the diff arrives naming which provider and which ids.
  const { seen, onFrame } = collect();
  const outcome = readEngineFrame(JSON.stringify(FRAME), onFrame);

  assert.equal(outcome, "catalog");
  assert.equal(seen.length, 1);
  assert.equal(seen[0].type, "model_catalog_changed");
  assert.deepEqual((seen[0] as { payload: { added: Record<string, string[]> } }).payload.added, {
    openrouter: ["anthropic/claude-x"],
  });
  assert.equal(seen[0].payload.fetched_at, 1_700_000_000);
});

test("a sweep that changed nothing still reports when it asked", () => {
  // Not a change, and it must not be reported as one — but a screen that no
  // longer polls needs the time, or "checked 40s ago" is a number nobody is
  // keeping true. Both frames mean the same thing to the app; they are told apart
  // so a test can say which arrived.
  const { seen, onFrame } = collect();
  const outcome = readEngineFrame(
    JSON.stringify({ type: "model_catalog_checked", payload: { fetched_at: 1_700_000_042 } }),
    onFrame,
  );
  assert.equal(outcome, "checked");
  assert.equal(seen[0].type, "model_catalog_checked");
  assert.equal(seen[0].payload.fetched_at, 1_700_000_042);
  assert.equal(
    (seen[0] as { payload: Record<string, unknown> }).payload.added,
    undefined,
    "a heartbeat grew a diff",
  );
});

test("a removal is as reportable as an addition", () => {
  // A provider that stopped answering arrives as everything it had, removed.
  // A reader that only handled `added` would show an empty provider as a provider
  // with nothing new.
  const { seen, onFrame } = collect();
  readEngineFrame(
    JSON.stringify({
      type: "model_catalog_changed",
      payload: { added: {}, removed: { groq: ["llama-3.1-70b"] }, fetched_at: 1 },
    }),
    onFrame,
  );
  assert.deepEqual((seen[0] as { payload: { removed: Record<string, string[]> } }).payload.removed, {
    groq: ["llama-3.1-70b"],
  });
});

test("a frame this build does not know is ignored, not acted on", () => {
  // The engine will grow this union. An older UI must keep working against a
  // newer engine rather than treating an unknown frame as a catalogue change and
  // re-reading a list that did not move.
  const { seen, onFrame } = collect();
  assert.equal(readEngineFrame(JSON.stringify({ type: "provider_quota_hit" }), onFrame), "ignored");
  assert.equal(seen.length, 0);
});

test("a frame that is not JSON cannot take the connection down", () => {
  // Every screen shares this socket. Throwing here would close the settings
  // panel, the command bar and the chat because one frame was truncated.
  const { seen, onFrame } = collect();
  for (const raw of ["", "not json", "{", "<html>502</html>", "null", "42"]) {
    assert.equal(readEngineFrame(raw, onFrame), "unreadable", `accepted ${raw}`);
  }
  assert.equal(seen.length, 0);
});

test("a frame whose type is missing is not a catalogue change", () => {
  // The one check that matters most: a payload with no type is a payload the
  // engine did not label, and treating it as a change would re-read on every
  // frame of any kind.
  const { seen, onFrame } = collect();
  assert.equal(
    readEngineFrame(JSON.stringify({ payload: { added: { openai: ["x"] } } }), onFrame),
    "ignored",
  );
  assert.equal(seen.length, 0);
});

test("a parsed object is accepted as well as a string", () => {
  // A test double, and a WebSocket implementation that hands over an object
  // rather than text, must not be told the frame is unreadable when it is not.
  const { seen, onFrame } = collect();
  assert.equal(readEngineFrame(FRAME, onFrame), "catalog");
  assert.equal(seen.length, 1);
});
