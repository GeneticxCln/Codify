import type { Event } from "./types.ts";
import { getEngineInfo } from "./api.ts";

/**
 * Live goal event stream over WebSocket with reconnect + exponential backoff.
 *
 * Design:
 * - Auth handshake on open: `{ type: "auth", token }` (engine closes with 4401 otherwise).
 * - Dedup by `sequence`, since the server replays all events from 0 on every (re)connect.
 * - Reconnect with 1s → 2s → 4s … capped at 16s backoff. The counter does **not** reset on
 *   open: the engine accepts the upgrade before it authenticates and then replays the log
 *   from 0, so "open" proves nothing and a post-accept failure (1011, 1006) would reconnect
 *   at 1 Hz forever. It resets only once a connection has stayed open for
 *   `STABLE_CONNECTION_MS`; one that drops sooner keeps climbing.
 * - `onTerminal` fires when a goal_status event reaches COMPLETED/FAILED/CANCELLED so
 *   callers can stop streaming (the engine keeps the goal endpoint open otherwise).
 * - `sinceSequence` floors replayed history: events at or below it are still delivered
 *   (callers dedup) but can never trigger `onTerminal`. This lets a caller re-subscribe
 *   to a finished goal (e.g. after Apply) without the replayed terminal event instantly
 *   closing the stream before the new run's events arrive.
 * - Optional `onReconnecting` for UI status display.
 * - There is deliberately no `onConnected`: an open proves nothing here (see the reconnect
 *   note above), a goal with no events sends no frame to prove it by, and nothing needed the
 *   signal. A caller that wants one must take `engineStream`'s proof rule, timer included.
 * - The engine closes with 4404 (there is no such goal) or 4401 (the token was refused).
 *   4404 is final: no reconnect, and `onGone` says so. Reconnecting at the 16 s cap forever
 *   was a request nothing could answer. 4401 is not final, unlike what this stream first
 *   assumed: a restarted engine can come back on the same port with a new token, and
 *   `connect()` re-reads `getEngineInfo()` every time, so the retry that used to be a
 *   request nothing could answer is now how the stream heals. `engineStream` already
 *   retries 4401 for exactly this reason.
 */
export const STABLE_CONNECTION_MS = 10_000;

export interface GoalStreamHandle {
  close: () => void;
}

export function openGoalStream(opts: {
  goalId: string;
  onEvent: (ev: Event) => void;
  onTerminal?: (ev: Event) => void;
  onReconnecting?: (attempt: number) => void;
  /** The engine ended the stream for good (4404: there is no such goal). */
  onGone?: (code: number) => void;
  /** Events with sequence <= this are replayed history: never terminal. */
  sinceSequence?: number;
}): GoalStreamHandle {
  const { goalId, onEvent, onTerminal, onReconnecting, onGone } = opts;
  const sinceSequence = opts.sinceSequence ?? 0;

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
  // Terminal statuses end the stream — no point reconnecting to a finished goal.
  const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
  // `engine/app.py` `ws_goal`: 4404 after auth for an unknown goal, 4401 for a bad token.
  const CLOSE_NO_SUCH_GOAL = 4404;

  const connect = () => {
    if (destroyed) return;
    const engine = getEngineInfo();
    // Engine info may not be populated yet (startup race); retry shortly.
    if (!engine.port) {
      retryTimer = setTimeout(connect, 500);
      return;
    }

    const sock = new WebSocket(`ws://127.0.0.1:${engine.port}/ws/goals/${goalId}`);
    ws = sock;
    // Per socket. A reconnect and `close()` both replace `ws`, so a handler a
    // superseded socket delivers late must not act for the socket that replaced
    // it: a stale close used to clear the new socket's stability timer and open
    // a second stream for the same goal. `engineStream.ts` guards its handlers
    // the same way, for the same reason.
    const current = () => !destroyed && ws === sock;

    sock.onopen = () => {
      if (!current()) return;
      clearStableTimer();
      stableTimer = setTimeout(() => {
        stableTimer = null;
        attempt = 0;
      }, STABLE_CONNECTION_MS);
      sock.send(JSON.stringify({ type: "auth", token: engine.token }));
    };

    sock.onmessage = (e) => {
      if (!current()) return;
      try {
        const ev: Event = JSON.parse(e.data);
        onEvent(ev);
        if (ev.type === "goal_status" && TERMINAL.has(ev.payload?.status) && ev.sequence > sinceSequence) {
          // Let the caller flush state, then close cleanly — the goal is done.
          onTerminal?.(ev);
          if (!destroyed) {
            destroyed = true;
            clearStableTimer();
            // Dropped before closing: the close this asks for is not news, and
            // a handler that arrives afterwards has no socket to act for.
            ws = null;
            sock.close();
          }
        }
      } catch (err) {
        console.error("WS event parse error", err);
      }
    };

    sock.onclose = (e) => {
      // A close from a socket that is no longer the current one — one that was
      // reconnected over, or one `close()` ended — is history, not an event.
      if (ws !== sock) return;
      ws = null;
      clearStableTimer();
      if (destroyed) return;
      if (e?.code === CLOSE_NO_SUCH_GOAL) {
        destroyed = true;
        onGone?.(e.code);
        return;
      }
      // 4401 falls through to the backoff: the token may have rotated with a
      // restarted engine, and the next `connect()` reads the fresh pair.
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
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = null;
      // Taken before the handle is dropped, so the socket still closes; the
      // handlers see `ws !== sock` and do not treat it as a dropped connection.
      const sock = ws;
      ws = null;
      try {
        sock?.close();
      } catch {
        /* noop */
      }
    },
  };
}
