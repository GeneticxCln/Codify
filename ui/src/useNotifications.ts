import { useCallback, useEffect, useRef, useState } from "react";
import {
  appendNotification,
  engineNotification,
  loadNotifications,
  markAllRead as markAllReadIn,
  saveNotifications,
  unreadCount,
  type AppNotification,
  type NotificationStorage,
} from "./notifications";
import type { EngineState } from "./statusTone";

/** The window's storage, or null where reading it throws (a private window, blocked site data). */
function windowStorage(): NotificationStorage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export interface NotificationsApi {
  /** Newest first. */
  items: AppNotification[];
  unread: number;
  /** Add one. `null` is accepted and ignored, so a producer that decided "no news" can be passed straight in. */
  notify: (notification: AppNotification | null) => void;
  markAllRead: () => void;
  clear: () => void;
}

/**
 * The list, held and remembered.
 *
 * Restored from `CODIFY_NOTIFICATIONS` on start and written back on every change. The setters are stable,
 * so a callback that closes over `notify` (a goal's stream, an engine frame handler) is never a stale one
 * and never needs to be re-subscribed because the list changed.
 */
export function useNotifications(): NotificationsApi {
  const [items, setItems] = useState<AppNotification[]>(() => loadNotifications(windowStorage()));

  // Skipped on the first run: writing back exactly what was just read is a write that can only lose data
  // (a storage that failed to read would be overwritten with the empty list it produced).
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    saveNotifications(windowStorage(), items);
  }, [items]);

  const notify = useCallback((notification: AppNotification | null) => {
    if (notification) setItems((prev) => appendNotification(prev, notification));
  }, []);
  const markAllRead = useCallback(() => setItems((prev) => markAllReadIn(prev)), []);
  const clear = useCallback(() => setItems([]), []);

  return { items, unread: unreadCount(items), notify, markAllRead, clear };
}

/**
 * How long a connection state has to hold before it is news. A flapping connection (down for a second,
 * back for two) is one that never settled and says nothing; an outage that lasts says so once. An object
 * so a test can shorten it, and nothing else should.
 */
export const engineNoticeTiming = { settleMs: 4000 };

/**
 * Notices for the engine connection, debounced.
 *
 * `checking` never settles, and the first settled state is only a baseline: an engine that is up when the
 * window opens is not news, and one that is down is (`engineNotification` decides). The pending notice is
 * cancelled the moment the state changes again, which is the whole of the debounce.
 */
export function useEngineNotices(
  state: EngineState,
  notify: (notification: AppNotification | null) => void,
  detail: () => string | undefined,
): void {
  const settled = useRef<Exclude<EngineState, "checking"> | null>(null);
  const latestDetail = useRef(detail);
  latestDetail.current = detail;

  useEffect(() => {
    if (state === "checking" || state === settled.current) return;
    const timer = setTimeout(() => {
      notify(engineNotification(settled.current, state, Date.now(), latestDetail.current()));
      settled.current = state;
    }, engineNoticeTiming.settleMs);
    return () => clearTimeout(timer);
  }, [state, notify]);
}
