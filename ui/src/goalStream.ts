import type { Event } from "./types.ts";
import { getEngineInfo } from "./api.ts";

/**
 * Live goal event stream over WebSocket with reconnect + exponential backoff.
 *
 * Design:
 * - Auth handshake on open: `{ type: "auth", token }` (engine closes with 4401 otherwise).
 * - Dedup by `sequence`, since the server replays all events from 0 on every (re)connect.
 * - Reconnect with 1s → 2s → 4s … capped at 16s backoff; resets on successful open.
 * - `onTerminal` fires when a goal_status event reaches COMPLETED/FAILED/CANCELLED so
 *   callers can stop streaming (the engine keeps the goal endpoint open otherwise).
 * - `sinceSequence` floors replayed history: events at or below it are still delivered
 *   (callers dedup) but can never trigger `onTerminal`. This lets a caller re-subscribe
 *   to a finished goal (e.g. after Apply) without the replayed terminal event instantly
 *   closing the stream before the new run's events arrive.
 * - Optional `onReconnecting` for UI status display.
 */
export interface GoalStreamHandle {
  close: () => void;
}

export function openGoalStream(opts: {
  goalId: string;
  onEvent: (ev: Event) => void;
  onTerminal?: (ev: Event) => void;
  onReconnecting?: (attempt: number) => void;
  onConnected?: () => void;
  /** Events with sequence <= this are replayed history: never terminal. */
  sinceSequence?: number;
}): GoalStreamHandle {
  const { goalId, onEvent, onTerminal, onReconnecting, onConnected } = opts;
  const sinceSequence = opts.sinceSequence ?? 0;

  let ws: WebSocket | null = null;
  let destroyed = false;
  let attempt = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  // Terminal statuses end the stream — no point reconnecting to a finished goal.
  const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED"]);

  const connect = () => {
    if (destroyed) return;
    const engine = getEngineInfo();
    // Engine info may not be populated yet (startup race); retry shortly.
    if (!engine.port) {
      retryTimer = setTimeout(connect, 500);
      return;
    }

    ws = new WebSocket(`ws://127.0.0.1:${engine.port}/ws/goals/${goalId}`);

    ws.onopen = () => {
      attempt = 0;
      ws!.send(JSON.stringify({ type: "auth", token: engine.token }));
      onConnected?.();
    };

    ws.onmessage = (e) => {
      try {
        const ev: Event = JSON.parse(e.data);
        onEvent(ev);
        if (ev.type === "goal_status" && TERMINAL.has(ev.payload?.status) && ev.sequence > sinceSequence) {
          // Let the caller flush state, then close cleanly — the goal is done.
          onTerminal?.(ev);
          if (!destroyed) {
            destroyed = true;
            ws?.close();
          }
        }
      } catch (err) {
        console.error("WS event parse error", err);
      }
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
      // Guard: close() may run before the socket finished constructing.
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    },
  };
}
