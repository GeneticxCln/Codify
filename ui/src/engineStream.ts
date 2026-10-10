import { getEngineInfo } from "./api.ts";
import { readEngineFrame, type FrameOutcome } from "./engineFrames.ts";
import type { EngineEvent } from "./types.ts";

// Re-exported so the socket and the decision it makes are still one import for
// the app, while the decision itself stays importable by a test that has no
// browser (see `engineFrames.ts`).
export { readEngineFrame, type FrameOutcome };

/**
 * Engine-level events over WebSocket, with reconnect and exponential backoff.
 *
 * A sibling of `goalStream.ts` and deliberately not part of it. A goal stream
 * carries one goal's durable, sequenced log; this carries what belongs to no
 * goal. Merging them would put a frame with no `goal_id` and no `sequence` into
 * a log that replays from 0 and dedupes on `sequence` — and a client that
 * dedupes on a sequence number it did not get would drop it anyway.
 *
 * Today there are two frames. `model_catalog_changed` says a provider's list
 * moved and carries the diff; `model_catalog_checked` says only that the
 * providers were asked, and when. The reader treats both the same way — re-read
 * `GET /models`, which is a cache hit because the engine's watcher is what warmed
 * it. So the payload stays small, and the client keeps one code path for "the
 * catalogue is current" rather than one for "merged from a frame" and another for
 * "fetched".
 *
 * Auth is `{ type: "auth", token }` as the first frame, not a header: a browser
 * cannot set headers on a WebSocket, and the engine accepts either.
 *
 * Reconnect backoff is 1 s, 2 s, 4 s, 8 s, then 16 s at most. The counter is
 * **not** reset when a socket opens: the engine accepts the upgrade first and
 * only then reads the auth frame, closing with 4401 on a bad token, so "open"
 * proves nothing and resetting on it turned a rejected token into a 1 Hz
 * reconnect loop (open, close, open, close). It resets only once a connection
 * has stayed open for `STABLE_CONNECTION_MS`; one that drops sooner keeps
 * climbing, whatever the reason. 4401 is deliberately not final here, unlike on
 * the goal stream: every attempt re-reads the engine's connection info, and a
 * restarted engine can come back on another port or with another token, so
 * retrying is how this stream heals.
 */
export const STABLE_CONNECTION_MS = 10_000;

export interface EngineStreamHandle {
  close: () => void;
}

export function openEngineStream(opts: {
  onFrame: (ev: EngineEvent) => void;
  onConnected?: () => void;
  onReconnecting?: (attempt: number) => void;
}): EngineStreamHandle {
  const { onFrame, onConnected, onReconnecting } = opts;

  let ws: WebSocket | null = null;
  let destroyed = false;
  let attempt = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  // Armed on open, cleared on close: fires only for a connection that lasted.
  let stableTimer: ReturnType<typeof setTimeout> | null = null;

  const clearStableTimer = () => {
    if (stableTimer) clearTimeout(stableTimer);
    stableTimer = null;
  };

  const connect = () => {
    if (destroyed) return;
    const engine = getEngineInfo();
    if (!engine.port) {
      retryTimer = setTimeout(connect, 500);
      return;
    }

    ws = new WebSocket(`ws://127.0.0.1:${engine.port}/ws/engine`);

    ws.onopen = () => {
      clearStableTimer();
      stableTimer = setTimeout(() => {
        stableTimer = null;
        attempt = 0;
      }, STABLE_CONNECTION_MS);
      ws!.send(JSON.stringify({ type: "auth", token: engine.token }));
      onConnected?.();
    };

    ws.onmessage = (e) => {
      readEngineFrame(e.data, onFrame);
    };

    ws.onclose = () => {
      clearStableTimer();
      if (destroyed) return;
      attempt += 1;
      const delay = Math.min(1000 * 2 ** (attempt - 1), 16000);
      onReconnecting?.(attempt);
      retryTimer = setTimeout(connect, delay);
    };

    ws.onerror = () => {
      // onclose fires after onerror; reconnect is handled there.
    };
  };

  connect();

  return {
    close: () => {
      destroyed = true;
      clearStableTimer();
      if (retryTimer) clearTimeout(retryTimer);
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    },
  };
}
