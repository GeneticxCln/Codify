/**
 * The command palette (Ctrl+K): what can be jumped to, how a query narrows the
 * list, and where the selection lands.
 *
 * Pure, like `tabs.ts` and `shortcuts.ts` — the palette's job is *finding*
 * things, and finding-things is arithmetic over titles: which of a dozen open
 * tabs and threads matches "ref", whether `Refactor the parser` should beat
 * `Fresh bread CI notes`, and what ArrowUp does at the top of the list. None
 * of that is visible in markup, so all of it lives here where `node --test`
 * reaches it. `CommandPalette.tsx` renders what this returns and forwards
 * keys; `App.tsx` performs the jump the selected item names.
 *
 * A selected item carries *data*, never a callback: a model that called
 * handlers could not be tested without reproducing the whole app, and the
 * jump itself is one `switch` in `App.tsx`.
 */

import type { Tab } from "./tabs";
import type { Conversation } from "./types";

/** Where an item sends you. Three sources — the strip, the thread list, settings. */
export type PaletteItemKind = "tab" | "conversation" | "settings";

interface BaseItem {
  /** Unique, stable, and used as the DOM id of its option row. */
  id: string;
  /** The group heading this item sits under, and its accessible label. */
  label: string;
  title: string;
}

export type PaletteItem =
  | (BaseItem & {
      kind: "tab";
      tabId: string;
      /** The tab on screen right now — marked, not special-cased. */
      active: boolean;
    })
  | (BaseItem & {
      kind: "conversation";
      conversationId: string;
    })
  | (BaseItem & {
      kind: "settings";
      settingsTab: "keys" | "agents" | "appearance";
    });

/** What the palette is choosing from. */
export interface PaletteSources {
  tabs: Tab[];
  activeId: string | null;
  conversations: Conversation[];
}

/**
 * The settings destinations, named as `SettingsModal` names its tabs.
 *
 * Two entries rather than one "Settings": a palette item that opens a dialog
 * on the right page is a jump, and one that opens it on *some* page is only
 * a button with extra steps.
 */
export const SETTINGS_ENTRIES: ReadonlyArray<{
  id: string;
  title: string;
  settingsTab: "keys" | "agents" | "appearance";
}> = [
  { id: "settings:keys", title: "Provider keys & endpoints", settingsTab: "keys" },
  { id: "settings:agents", title: "Agent roles & prompts", settingsTab: "agents" },
  { id: "settings:appearance", title: "Appearance & themes", settingsTab: "appearance" },
];

const KIND_LABEL: Record<PaletteItemKind, string> = {
  tab: "Open tab",
  conversation: "Conversation",
  settings: "Settings",
};

/**
 * Every item, in strip/list order: open tabs, then threads, then settings.
 *
 * A thread that is already open appears in both groups on purpose — both
 * resolve to the same jump (`openConversation` focuses the tab that thread
 * already has rather than opening a second), and hiding the conversation would
 * make the palette's most natural query ("the thread called X") miss.
 *
 * The empty-title fallback matches `Sidebar.threadTitle`: the engine stores
 * no title until a turn gives the thread one, and "New chat" is what the
 * panel calls it, so the palette must call it the same thing or the two
 * surfaces disagree about what a row is.
 */
export function buildPaletteItems(sources: PaletteSources): PaletteItem[] {
  const tabs: PaletteItem[] = sources.tabs.map((tab) => ({
    id: `tab:${tab.id}`,
    kind: "tab" as const,
    label: KIND_LABEL.tab,
    title: tab.title,
    tabId: tab.id,
    active: tab.id === sources.activeId,
  }));
  const conversations: PaletteItem[] = sources.conversations.map((convo) => ({
    id: `conversation:${convo.id}`,
    kind: "conversation" as const,
    label: KIND_LABEL.conversation,
    title: convo.title.trim() || "New chat",
    conversationId: convo.id,
  }));
  const settings: PaletteItem[] = SETTINGS_ENTRIES.map((entry) => ({
    id: entry.id,
    kind: "settings" as const,
    label: KIND_LABEL.settings,
    title: entry.title,
    settingsTab: entry.settingsTab,
  }));
  return [...tabs, ...conversations, ...settings];
}

/** What a token is matched against: the title first, the group label second. */
function haystack(item: PaletteItem): [string, string] {
  return [item.title.toLowerCase(), item.label.toLowerCase()];
}

/**
 * How well one token matches one item: a title that *starts with* the token
 * beats one that contains it, which beats a hit on the group label alone.
 */
function tokenScore(item: PaletteItem, token: string): number | null {
  const [title, label] = haystack(item);
  if (title.startsWith(token)) return 3;
  if (title.includes(token)) return 2;
  if (label.includes(token)) return 1;
  return null;
}

/**
 * Narrow the list to what the query names.
 *
 * Every whitespace-separated token must match (so "ref parser" finds
 * "Refactor the parser" but not every thread containing "ref"), scores add
 * up, and the sort is stable — equal scores keep source order, so tabs
 * never shuffle under a query that does not care which group a hit is in.
 * An empty query is the whole list, untouched: a palette that shows nothing
 * until you type is a palette that cannot be browsed.
 */
export function filterPalette(items: PaletteItem[], query: string): PaletteItem[] {
  const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return items;

  const scored: { item: PaletteItem; score: number }[] = [];
  for (const item of items) {
    let total = 0;
    let matched = true;
    for (const token of tokens) {
      const s = tokenScore(item, token);
      if (s === null) {
        matched = false;
        break;
      }
      total += s;
    }
    if (matched) scored.push({ item, score: total });
  }
  // Stable sort: Array#sort keeps insertion order for equal scores.
  return scored.sort((a, b) => b.score - a.score).map((entry) => entry.item);
}

/**
 * The next selection after an arrow key, wrapping at both ends.
 *
 * `-1` for "nothing to select" (an empty match list); an empty list never
 * produces a valid index, and pretending 0 would mean Enter activates a row
 * that is not there.
 */
export function moveSelection(
  current: number,
  delta: 1 | -1,
  count: number
): number {
  if (count <= 0) return -1;
  // Nothing selected yet: Down lands on the first row, Up on the last —
  // "from -1, step" is not arithmetic (-1 + 1 would skip the first row).
  if (current < 0 || current >= count) return delta === 1 ? 0 : count - 1;
  return (((current + delta) % count) + count) % count;
}
