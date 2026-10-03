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

import { canPair } from "./panes.ts";
import type { Tab } from "./tabs";
import type { Conversation, SettingsTab } from "./types";

/**
 * Where an item sends you. Five sources — the strip, the thread list, settings, what can be done to the split, and the
 * workspace's files.
 */
export type PaletteItemKind = "tab" | "conversation" | "settings" | "action" | "file";

/** What a split action asks the shell to do. Data, like everything here: the jump is one `switch` in `App.tsx`. */
export type PaletteAction =
  | { type: "close-split" }
  | { type: "new-machine"; network: boolean }
  | { type: "split-new-terminal" }
  | { type: "show-beside"; tabId: string };

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
      settingsTab: SettingsTab;
    })
  | (BaseItem & {
      kind: "action";
      action: PaletteAction;
    })
  | (BaseItem & {
      kind: "file";
      /** Relative to the workspace root, as the engine listed it. */
      path: string;
    });

/** What the palette is choosing from. */
export interface PaletteSources {
  tabs: Tab[];
  activeId: string | null;
  conversations: Conversation[];
  /**
   * Whether a split is showing. Absent means the caller has nothing to say about splits, and the palette lists none:
   * what it held before is what it still holds.
   */
  split?: { showing: boolean };
  /**
   * The paths of the selected workspace's files, as `GET /workspaces/{id}/files` listed them. Absent or empty means there
   * are none to offer. They are searched, not browsed: see [`filterPalette`].
   */
  files?: readonly string[];
}

/** How many files one query may show. A common word matches thousands of paths, and a list that long is not a result. */
export const MAX_FILE_RESULTS = 30;

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
  settingsTab: SettingsTab;
}> = [
  { id: "settings:keys", title: "Provider keys & endpoints", settingsTab: "keys" },
  { id: "settings:agents", title: "Agent roles & prompts", settingsTab: "agents" },
  { id: "settings:audio", title: "Audio: microphone, dictation & read-aloud", settingsTab: "audio" },
  { id: "settings:appearance", title: "Appearance, themes & UI scale", settingsTab: "appearance" },
  { id: "settings:about", title: "About Codify", settingsTab: "about" },
];

const KIND_LABEL: Record<PaletteItemKind, string> = {
  tab: "Open tab",
  conversation: "Conversation",
  settings: "Settings",
  action: "Split",
  file: "Open file",
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
  const files: PaletteItem[] = (sources.files ?? []).map((path) => ({
    id: `file:${path}`,
    kind: "file" as const,
    label: KIND_LABEL.file,
    title: path,
    path,
  }));
  return [...tabs, ...conversations, ...settings, ...machineActions(), ...splitActions(sources), ...files];
}

/**
 * Opening a machine: always on offer, and in two rows rather than one with a switch, because the network is the one fact about
 * a jail that must be chosen on purpose and cannot be changed afterwards. The row that gives it one says so in its title, and
 * is a different row, so there is no way to get a network by pressing Enter on the first.
 */
function machineActions(): PaletteItem[] {
  const make = (id: string, title: string, network: boolean): PaletteItem => ({
    id: `action:${id}`,
    kind: "action",
    // Its own group: these are not about the split, and a heading that said "Split" over them would be a lie.
    label: "Machine",
    title,
    action: { type: "new-machine", network },
  });
  return [
    make("new-machine", "New machine: a jailed shell, no network", false),
    make("new-machine-network", "New machine with network: it can reach this computer's network", true),
  ];
}

/**
 * What can be done about the split from here: close it while one is showing; otherwise a new terminal beside this tab, and
 * each other tab that can sit beside it. Last in the list (they are found by typing "split"), and never offered where the
 * split could not be made, because a row that did nothing would be the one thing a palette must not hold.
 */
function splitActions(sources: PaletteSources): PaletteItem[] {
  if (!sources.split) return [];
  const make = (id: string, title: string, action: PaletteAction): PaletteItem => ({
    id: `action:${id}`,
    kind: "action",
    label: KIND_LABEL.action,
    title,
    action,
  });
  if (sources.split.showing) return [make("close-split", "Close split", { type: "close-split" })];

  const active = sources.tabs.find((t) => t.id === sources.activeId);
  if (!active) return [];
  return [
    make("split-new-terminal", "Split: new terminal beside this one", { type: "split-new-terminal" }),
    // `canPair` refuses the tab itself ("same"), so the active tab is not offered beside itself.
    ...sources.tabs
      .filter((t) => canPair(active, t))
      .map((t) => make(`show-beside:${t.id}`, `Show beside: ${t.title}`, { type: "show-beside", tabId: t.id })),
  ];
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
  // A file is looked for by its name, and its path is only the folder it is in: `main` is `src/app/main.py`, not
  // `docs/domain.md`, and a name that *starts* with the word beats a path that merely contains it.
  if (item.kind === "file" && (title.split("/").pop() ?? "").startsWith(token)) return 3;
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
 * until you type is a palette that cannot be browsed. **Except for files**:
 * a workspace has thousands, and listing them is not browsing, so they appear
 * only once there is a query, and then only the best `MAX_FILE_RESULTS`.
 */
export function filterPalette(items: PaletteItem[], query: string): PaletteItem[] {
  const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return items.filter((item) => item.kind !== "file");

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
  let files = 0;
  return scored
    .sort((a, b) => b.score - a.score)
    .map((entry) => entry.item)
    .filter((item) => item.kind !== "file" || ++files <= MAX_FILE_RESULTS);
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
