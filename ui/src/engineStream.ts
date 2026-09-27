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
 */
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

  const connect = () => {
    if (destroyed) return;
    const engine = getEngineInfo();
    if (!engine.port) {
      retryTimer = setTimeout(connect, 500);
      return;
    }

    ws = new WebSocket(`ws://127.0.0.1:${engine.port}/ws/engine`);

    ws.onopen = () => {
      attempt = 0;
      ws!.send(JSON.stringify({ type: "auth", token: engine.token }));
      onConnected?.();
    };

    ws.onmessage = (e) => {
      readEngineFrame(e.data, onFrame);
    };

    ws.onclose = () => {
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
      if (retryTimer) clearTimeout(retryTimer);
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    },
  };
}
