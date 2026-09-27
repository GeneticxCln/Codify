import React from "react";
import { X, Terminal, Globe, MessageSquareText } from "lucide-react";
import type { Tab, TabKind } from "../tabs";
import type { Workspace } from "../types";
import { threadLabel } from "../threadTitle";

/**
 * The tab strip: what is open, which one is showing, and how to move between
 * them.
 *
 * One strip for all three kinds, because the user thinks of them the same way —
 * "what have I got open" — and a chat tab next to a terminal is a window they
 * want to reach in one click. The kind is told apart by its icon and nothing
 * else: the strip is a list of things, and colouring the whole tab would make
 * the kind louder than the title.
 *
 * Keyboard reach matters more here than in most components. Tabs are a keyboard
 * object — `Cmd+1..9` to switch, `Cmd+W` to close, `Cmd+T` to open — so every
 * tab is focusable and carries an `aria-label` naming both its kind and its
 * title. A tab that can only be reached with a mouse is a tab that a keyboard
 * user cannot close.
 */

/** One icon per kind, so a tab's shape is its kind. */
const KIND_ICON: Record<TabKind, React.FC<{ className?: string }>> = {
  chat: MessageSquareText,
  terminal: Terminal,
  browser: Globe,
};

/** What the accessible name says, since the icon alone is not a label. */
const KIND_NAME: Record<TabKind, string> = {
  chat: "Conversation",
  terminal: "Terminal",
  browser: "Browser",
};

export interface TabBarProps {
  tabs: Tab[];
  activeId: string | null;
  onFocus: (tabId: string) => void;
  onClose: (tabId: string) => void;
  /** A live-run tab says so; the strip does not invent the fact itself. */
  busyTabIds?: string[];
  /**
   * Every workspace, so a tab can be named for the folder it belongs to.
   *
   * A lookup rather than a single "current" workspace, because the strip holds
   * tabs from several at once. A tab records *its own* folder (`Tab.workspaceId`)
   * and this resolves that id to a name and a path — which is why a tab cannot
   * be contradicted by the composer changing workspace underneath it.
   */
  workspaces?: Workspace[];
}

export const TabBar: React.FC<TabBarProps> = ({
  tabs,
  activeId,
  onFocus,
  onClose,
  busyTabIds = [],
  workspaces = [],
}) => {
  return (
    <div
      role="tablist"
      aria-label="Open tabs"
      className="flex items-stretch gap-1 px-1 pt-1 border-b border-codify-border bg-codify-surface overflow-x-auto"
    >
      {tabs.map((tab, i) => {
        const Icon = KIND_ICON[tab.kind];
        const active = tab.id === activeId;
        const busy = busyTabIds.includes(tab.id);
        // The folder this tab is in, resolved from the id the tab carries. Not
        // `selectedWs`: with two tabs in two folders, one global name would be
        // wrong for one of them.
        const ws = tab.workspaceId
          ? workspaces.find((w) => w.id === tab.workspaceId)
          : undefined;
        // What the tab is called: the thread's name, or the folder it is in when
        // the thread has not named itself yet. One label — a tab carrying
        // "New chat" *and* the folder is two names for one thing, and the
        // placeholder is the one that means nothing.
        const label = threadLabel(tab.title, ws?.name);
        return (
          <div
            key={tab.id}
            role="tab"
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            aria-label={`${KIND_NAME[tab.kind]}: ${label}${
              ws ? ` in ${ws.root_path}` : ""
            }`}
            title={ws ? `${ws.root_path} — ${label}` : label}
            onClick={() => onFocus(tab.id)}
            className={
              "group relative flex items-center gap-1 px-2.5 py-1 rounded-t-md " +
              "cursor-pointer max-w-48 border border-b-0 " +
              (active
                ? "bg-codify-bg border-codify-border text-gray-100"
                : "bg-codify-surface border-transparent text-gray-400 hover:text-gray-200")
            }
          >
            <Icon
              className={
                "w-3 h-3 flex-shrink-0 " + (active ? "text-blue-400" : "")
              }
            />
            {/* Normal weight, like the menu's rows: a strip of tabs is a list
            of equals, and when every title is bold nothing is. One label, and
            `threadLabel` decides what it is — the folder when the thread has no
            name, because that is what is true and it is short. */}
            <span className="text-xs font-normal truncate">{label}</span>
            {/* A run in flight is a fact about the tab, not a decoration: the
            strip is the only place that knows a tab is busy when it is not
            the one on screen. */}
            {busy && (
              <span
                aria-hidden
                className="w-1.5 h-1.5 rounded-full bg-blue-400 flex-shrink-0"
              />
            )}
            <button
              type="button"
              aria-label={`Close ${KIND_NAME[tab.kind].toLowerCase()}: ${tab.title}`}
              title={`Close (⌘W)`}
              onClick={(e) => {
                e.stopPropagation();
                onClose(tab.id);
              }}
              className={
                "ml-1 rounded p-0.5 flex-shrink-0 " +
                "opacity-0 group-hover:opacity-100 focus-visible:opacity-100 " +
                "hover:bg-codify-raised"
              }
            >
              <X className="w-3 h-3" />
            </button>
            {/* The active tab's seam is drawn under it, so the strip reads as
            tabs of one window rather than a row of buttons. */}
            {active && (
              <span
                aria-hidden
                className="absolute left-0 right-0 -bottom-px h-px bg-codify-bg"
              />
            )}
            <span className="sr-only">{i + 1} of {tabs.length}</span>
          </div>
        );
      })}
    </div>
  );
};
