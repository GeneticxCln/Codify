import { useCallback, useEffect, useRef, useState } from "react";
import {
  captureClip,
  clearUnpinned as clearUnpinnedIn,
  isPrivateField,
  loadClips,
  removeClip,
  saveClips,
  selectedTextOf,
  togglePin as togglePinIn,
  type Clip,
  type ClipboardStorage,
  type ClipSource,
  type FieldLike,
  type PrivacyTarget,
  type SkipReason,
} from "./clipboardHistory";

/** The window's storage, or null where reading it throws (a private window, blocked site data). */
function windowStorage(): ClipboardStorage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

/**
 * The refusals worth a sentence. A password field is private by being one, an empty selection is a Ctrl+C
 * with nothing selected, and saying so every time would be noise; a key and an over-long clip are things the
 * person expected to find and will look for.
 */
const SAID: readonly SkipReason[] = ["secret", "too-long"];

export interface ClipboardApi {
  /** As kept: newest first. The drawer orders them for display. */
  clips: Clip[];
  /** Why the last thing was not kept, while that is still the latest news. */
  lastSkip: SkipReason | null;
  /** For a button that copies through the API, which fires no event. */
  record: (text: string, source: ClipSource) => void;
  togglePin: (id: string) => void;
  remove: (id: string) => void;
  clearUnpinned: () => void;
  dismissSkip: () => void;
}

/** The element an event came at: a text node stands for the element around it. */
function elementOf(target: EventTarget | null): (FieldLike & PrivacyTarget) | null {
  const node = target as (Node & FieldLike & PrivacyTarget) | null;
  if (!node) return null;
  if (typeof node.closest === "function") return node;
  return (node.parentElement as (FieldLike & PrivacyTarget) | null) ?? node;
}

function textOnTheClipboard(event: Event): string {
  const data = (event as Event & { clipboardData?: DataTransfer | null }).clipboardData;
  const text = data?.getData("text/plain");
  return typeof text === "string" ? text : "";
}

/**
 * What the window has copied, kept inside the window.
 *
 * It listens to the document's own `copy`, `cut` and `paste`, which the browser gives to any page for its own
 * content without asking for anything: no permission, no watcher on the system clipboard, nothing in another
 * program. What is copied in a browser tab's own webview, or in another application, is not seen.
 *
 * - **Copy and cut** are heard after the handlers beneath have run, so text a terminal wrote into the event is
 *   what is kept (it has no selection the window can see); failing that, the selection, read from the field
 *   itself when it is a text field (`window.getSelection()` reports nothing for one).
 * - **Paste** is heard before them, so a handler that stops the event cannot hide it.
 * - **Never read:** a password field, and anything inside `data-clipboard="off"`. The model refuses what looks
 *   like a credential (`clipboardHistory.ts`); this is the part that never looks.
 *
 * The list is the ref, and the state is the copy React draws: two records in one tick, or one inside an event
 * handler and one from a button, each start from what the other left.
 */
export function useClipboardHistory(): ClipboardApi {
  const [clips, setClips] = useState<Clip[]>(() => loadClips(windowStorage()));
  const [lastSkip, setLastSkip] = useState<SkipReason | null>(null);
  const list = useRef(clips);
  const serial = useRef(0);

  // Skipped on the first run, for the reason `useNotifications` gives: writing back exactly what was just
  // read can only lose data.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    saveClips(windowStorage(), clips);
  }, [clips]);

  const keep = useCallback((next: Clip[]) => {
    list.current = next;
    setClips(next);
  }, []);

  const capture = useCallback(
    (text: string, source: ClipSource, privateField: boolean) => {
      serial.current += 1;
      const { list: next, outcome } = captureClip(list.current, {
        id: `clip-${Date.now().toString(36)}-${serial.current.toString(36)}`,
        text,
        at: Date.now(),
        source,
        privateField,
      });
      if (outcome.kind === "kept") {
        keep(next);
        setLastSkip(null);
      } else if (SAID.includes(outcome.why)) {
        setLastSkip(outcome.why);
      }
    },
    [keep],
  );

  useEffect(() => {
    const onCopy = (event: Event): void => {
      const field = elementOf(event.target);
      const carried = textOnTheClipboard(event);
      const text = carried || selectedTextOf(field, document.getSelection()?.toString() ?? "");
      capture(text, "selection", isPrivateField(field));
    };
    const onPaste = (event: Event): void => {
      capture(textOnTheClipboard(event), "paste", isPrivateField(elementOf(event.target)));
    };
    document.addEventListener("copy", onCopy);
    document.addEventListener("cut", onCopy);
    document.addEventListener("paste", onPaste, true);
    return () => {
      document.removeEventListener("copy", onCopy);
      document.removeEventListener("cut", onCopy);
      document.removeEventListener("paste", onPaste, true);
    };
  }, [capture]);

  const record = useCallback((text: string, source: ClipSource) => capture(text, source, false), [capture]);
  const togglePin = useCallback((id: string) => keep(togglePinIn(list.current, id)), [keep]);
  const remove = useCallback((id: string) => keep(removeClip(list.current, id)), [keep]);
  const clearUnpinned = useCallback(() => keep(clearUnpinnedIn(list.current)), [keep]);
  const dismissSkip = useCallback(() => setLastSkip(null), []);

  return { clips, lastSkip, record, togglePin, remove, clearUnpinned, dismissSkip };
}
