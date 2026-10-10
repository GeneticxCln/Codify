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
 * "Open" proves nothing: the engine accepts the upgrade first and only then
 * reads the auth frame, closing with 4401 on a bad token. So a socket opening
 * is not reported as connected. `onConnected` fires once per socket, when the
 * connection is *proven*: either a frame arrived (the engine only speaks to an
 * authenticated subscriber), or `CONNECTION_PROVEN_MS` passed without a close.
 * A rejection arrives within milliseconds of the auth frame, so it never gets
 * that far, and a healthy idle socket (no frame until the engine's first sweep)
 * is proven by the timer.
 *
 * Reconnect backoff is 1 s, 2 s, 4 s, 8 s, then 16 s at most. The counter is
 * **not** reset when a socket opens, for the same reason: resetting on it
 * turned a rejected token into a 1 Hz reconnect loop (open, close, open,
 * close). It resets only once a connection has stayed open for
 * `STABLE_CONNECTION_MS`, a longer window than the proof above because it
 * answers a different question (is this link steady enough to forgive earlier
 * failures, not was I accepted); one that drops sooner keeps
 * climbing, whatever the reason. 4401 is deliberately not final here, unlike on
 * the goal stream: every attempt re-reads the engine's connection info, and a
 * restarted engine can come back on another port or with another token, so
 * retrying is how this stream heals.
 */
export const STABLE_CONNECTION_MS = 10_000;

/**
 * How long an open socket must go without a close before it is reported as
 * connected (when no frame has arrived to say so sooner). The client sends the
 * auth frame in the same tick as `onopen` and the engine closes a bad token in
 * milliseconds (measured 0.5 to 3.5 ms idle, 18 ms worst under load), so this
 * is about a hundred times the slowest rejection seen. It cannot be a frame
 * alone: a healthy idle socket gets none until the engine's first sweep, which
 * can be a minute away. If the engine stalls past it, the result is the old
 * behaviour (connected, then dropped), never worse.
 */
export const CONNECTION_PROVEN_MS = 2_000;

export interface EngineStreamHandle {
  close: () => void;
}

export function openEngineStream(opts: {
  onFrame: (ev: EngineEvent) => void;
  /**
   * The connection is proven, not merely open: the engine did not reject it
   * within `CONNECTION_PROVEN_MS`, or a frame arrived. At most once per socket,
   * and never for one the engine closed with 4401 straight after the upgrade.
   */
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

  // Armed on open, cleared on close: fires only for a connection that was not rejected.
  let provenTimer: ReturnType<typeof setTimeout> | null = null;

  const clearStableTimer = () => {
    if (stableTimer) clearTimeout(stableTimer);
    stableTimer = null;
  };

  const clearProvenTimer = () => {
    if (provenTimer) clearTimeout(provenTimer);
    provenTimer = null;
  };

  const connect = () => {
    if (destroyed) return;
    const engine = getEngineInfo();
    if (!engine.port) {
      retryTimer = setTimeout(connect, 500);
      return;
    }

    const sock = new WebSocket(`ws://127.0.0.1:${engine.port}/ws/engine`);
    ws = sock;

    // Per socket. The timers above are shared, so every handler first checks it is still the
    // current socket: an event delivered late by an earlier one (or after close()) must neither
    // report for it nor cancel the timers of the socket that replaced it.
    const current = () => !destroyed && ws === sock;
    let proven = false;
    const prove = () => {
      if (proven || !current()) return;
      proven = true;
      clearProvenTimer();
      onConnected?.();
    };

    sock.onopen = () => {
      if (!current()) return;
      clearStableTimer();
      stableTimer = setTimeout(() => {
        stableTimer = null;
        attempt = 0;
      }, STABLE_CONNECTION_MS);
      sock.send(JSON.stringify({ type: "auth", token: engine.token }));
      clearProvenTimer();
      provenTimer = setTimeout(prove, CONNECTION_PROVEN_MS);
    };

    sock.onmessage = (e) => {
      if (!current()) return;
      // Any frame, before it is classified: the engine sends nothing to a socket it has not authenticated.
      prove();
      readEngineFrame(e.data, onFrame);
    };

    sock.onclose = () => {
      if (ws !== sock) return;
      clearStableTimer();
      clearProvenTimer();
      if (destroyed) return;
      attempt += 1;
      const delay = Math.min(1000 * 2 ** (attempt - 1), 16000);
      onReconnecting?.(attempt);
      retryTimer = setTimeout(connect, delay);
    };

    sock.onerror = () => {
      // onclose fires after onerror; reconnect is handled there.
    };
  };

  connect();

  return {
    close: () => {
      destroyed = true;
      clearStableTimer();
      clearProvenTimer();
      if (retryTimer) clearTimeout(retryTimer);
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    },
  };
}
