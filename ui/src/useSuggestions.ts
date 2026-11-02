import { useCallback, useEffect, useRef, useState } from "react";
import { dismissSuggestion, listSuggestions, scanSuggestions, snoozeSuggestion } from "./api";
import { POLL_MS, SNOOZE_HOURS } from "./suggestions.ts";
import type { Suggestion } from "./types.ts";

export interface SuggestionsApi {
  items: Suggestion[];
  /** Open offers that are asleep: not shown, but the inbox can say they exist. */
  snoozed: number;
  /** An id being acted on, so its buttons wait instead of double-firing. */
  busyId: string | null;
  error: string | null;
  refresh: () => Promise<void>;
  /** "Look now". Answers how many offers the engine made, or null when it could not look. */
  scan: () => Promise<number | null>;
  snooze: (id: string) => Promise<void>;
  dismiss: (id: string) => Promise<void>;
}

/**
 * The inbox as the window holds it: read from the engine on a timer while it is up, and nothing else.
 *
 * It never sends a turn. A failed read keeps the last list: an engine mid-restart is not an empty inbox, and a
 * screen that blanked on every hiccup would teach its reader to ignore it.
 */
export function useSuggestions(engineUp: boolean): SuggestionsApi {
  const [items, setItems] = useState<Suggestion[]>([]);
  const [snoozed, setSnoozed] = useState(0);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const list = await listSuggestions();
      if (!alive.current) return;
      // Shaped here, not trusted: an answer without a list (a proxy's page, an engine that is not ours) is an empty
      // inbox, not a crash in the component that draws the bell.
      setItems(Array.isArray(list?.suggestions) ? list.suggestions : []);
      setSnoozed(typeof list?.snoozed === "number" ? list.snoozed : 0);
      setError(null);
    } catch (err: unknown) {
      if (alive.current) setError(err instanceof Error ? err.message : "Could not load suggestions");
    }
  }, []);

  useEffect(() => {
    if (!engineUp) return undefined;
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [engineUp, refresh]);

  const act = useCallback(
    async (id: string, run: () => Promise<unknown>) => {
      setBusyId(id);
      try {
        await run();
        setError(null);
      } catch (err: unknown) {
        if (alive.current) setError(err instanceof Error ? err.message : "That did not work");
      } finally {
        if (alive.current) setBusyId(null);
      }
      await refresh();
    },
    [refresh],
  );

  const scan = useCallback(async () => {
    try {
      const { created } = await scanSuggestions();
      await refresh();
      return created;
    } catch (err: unknown) {
      if (alive.current) setError(err instanceof Error ? err.message : "Could not check for suggestions");
      return null;
    }
  }, [refresh]);

  return {
    items,
    snoozed,
    busyId,
    error,
    refresh,
    scan,
    snooze: (id) => act(id, () => snoozeSuggestion(id, SNOOZE_HOURS)),
    dismiss: (id) => act(id, () => dismissSuggestion(id)),
  };
}
