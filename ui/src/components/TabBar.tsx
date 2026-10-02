import React from "react";
import { X, Terminal, Globe, MessageSquareText, FileCode } from "lucide-react";
import type { Tab, TabKind } from "../tabs";
import type { Workspace } from "../types";
import { threadLabel } from "../threadTitle";

/**
 * The tab strip: what is open, which one is showing, and how to move between
 * them.
 *
 * One strip for all four kinds, because the user thinks of them the same way —
 * "what have I got open" — and a chat tab next to a terminal is a window they
 * want to reach in one click. The kind is told apart by its icon and nothing
 * else: the strip is a list of things, and colouring the whole tab would make
 * the kind louder than the title.
 *
 * Keyboard reach matters more here than in most components. Tabs are a keyboard
 * object — `Ctrl+1..9` to switch, `Ctrl+W` to close, `Ctrl+T` to open — so every
 * tab is focusable and carries an `aria-label` naming both its kind and its
 * title. A tab that can only be reached with a mouse is a tab that a keyboard
 * user cannot close.
 *
 * **There is no New Tab control here, and that is deliberate.** It used to sit
 * at the right end of this strip, which put “start something” halfway along
 * a list of things already open and made a strip of ten tabs a ten-tab walk to
 * the control that shortens it. New Tab belongs beside the CODIFY badge, at the
 * left edge of the header, where it is the same distance away however many tabs
 * are open — see `NewTabButton.tsx`. A strip that grows pushes its own controls
 * out of reach, and the one control it had was the one that needed to be
 * furthest from the edges.
 */

/** One icon per kind, so a tab's shape is its kind. */
export const KIND_ICON: Record<TabKind, React.FC<{ className?: string }>> = {
  chat: MessageSquareText,
  terminal: Terminal,
  browser: Globe,
  editor: FileCode,
};

/** What the accessible name says, since the icon alone is not a label. */
export const KIND_NAME: Record<TabKind, string> = {
  chat: "Conversation",
  terminal: "Terminal",
  browser: "Browser",
  editor: "Editor",
};

export interface TabBarProps {
  tabs: Tab[];
  activeId: string | null;
  onFocus: (tabId: string) => void;
  onClose: (tabId: string) => void;
  /** A live-run tab says so; the strip does not invent the fact itself. */
  busyTabIds?: string[];
  /**
   * Browser tabs whose page is loading, as the page reported it. A different
   * fact from `busyTabIds` — a run in flight versus a page on the wire — and
   * shaped differently on purpose: a pulse reads as motion without claiming
   * progress the runtime cannot measure.
   */
  loadingTabIds?: string[];
  /**
   * Terminal tabs whose shell said something while no pane displayed it.
   *
   * The strip does not derive this either — the recorder (`App.tsx`, via
   * `terminalBuffer.ts`) is the only party that knows a chunk was kept, and
   * App is the only party that knows which tab the user is on. A badge the
   * strip invented from weaker facts would be wrong exactly when it matters.
   */
  unreadTerminalIds?: string[];
  /**
   * Every workspace, so a tab can be named for the folder it belongs to.
   *
   * A lookup rather than a single "current" workspace, because the strip holds
   * tabs from several at once. A tab records *its own* folder (`Tab.workspaceId`)
   * and this resolves that id to a name and a path — which is why a tab cannot
   * be contradicted by the composer changing workspace underneath it.
   */
  workspaces?: Workspace[];
  /**
   * The tabs a split is showing, so the strip says what is on screen. Marked, not selected: only the active tab, the
   * focused pane's, is `aria-selected`, because everything keyed to "the active tab" means the one being worked in.
   */
  splitIds?: readonly string[];
  /**
   * Editor tabs whose text has changes nobody has saved. The strip cannot know: the buffer lives above the pane
   * (`editorBuffers.ts`), and the tab you are not looking at is the one you will close and lose.
   */
  unsavedIds?: readonly string[];
  /**
   * Editor tabs the assistant has changed since the person last looked: "something in a tab I was not looking at
   * changed", the same fact family as the terminal's unread dot.
   */
  assistantEditedIds?: readonly string[];
  /**
   * A right-click on a tab, or the Menu key on a focused one (the browser delivers both as `contextmenu`), asks for that
   * tab's menu. Absent, the strip leaves the browser's own menu alone.
   */
  onMenu?: (tabId: string, x: number, y: number) => void;
}

export const TabBar: React.FC<TabBarProps> = ({
  tabs,
  activeId,
  onFocus,
  onClose,
  busyTabIds = [],
  loadingTabIds = [],
  unreadTerminalIds = [],
  workspaces = [],
  splitIds = [],
  unsavedIds = [],
  assistantEditedIds = [],
  onMenu,
}) => {
  return (
    <div className="flex flex-1 min-w-0 items-center gap-1">
      <div
        role="tablist"
        aria-label="Open tabs"
        className="flex flex-1 min-w-0 items-stretch gap-1 overflow-x-auto"
      >
        {tabs.map((tab, i) => {
          const Icon = KIND_ICON[tab.kind];
          const active = tab.id === activeId;
          const busy = busyTabIds.includes(tab.id);
          const unread = unreadTerminalIds.includes(tab.id);
          const inSplit = splitIds.includes(tab.id);
          // Only an editor has text to lose or an assistant to change it, whatever ids a caller passes.
          const unsaved = tab.kind === "editor" && unsavedIds.includes(tab.id);
          const assistantEdited = tab.kind === "editor" && assistantEditedIds.includes(tab.id);
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
              }${unread ? " — new output" : ""}${unsaved ? " — unsaved changes" : ""}${
                assistantEdited ? " — changed by the assistant" : ""
              }${inSplit ? " — in split view" : ""}`}
              title={
                // An editor tab is a file, so its tooltip says which one and where: a name alone cannot tell two `index.ts` apart.
                tab.kind === "editor" && tab.path
                  ? ws
                    ? `${ws.root_path}/${tab.path}`
                    : tab.path
                  : ws
                    ? `${ws.root_path} — ${label}`
                    : label
              }
              data-in-split={inSplit ? "true" : undefined}
              onClick={() => onFocus(tab.id)}
              onContextMenu={
                onMenu
                  ? (event) => {
                      event.preventDefault();
                      onMenu(tab.id, event.clientX, event.clientY);
                    }
                  : undefined
              }
              className={
                "group relative flex items-center gap-1 px-2.5 py-1 rounded-md " +
                "cursor-pointer max-w-48 border " +
                (active
                  ? "bg-codify-bg border-codify-border text-codify-primary"
                  : (inSplit ? "border-codify-accent/30 " : "border-transparent ") +
                    "bg-codify-surface text-codify-muted hover:text-codify-secondary")
              }
            >
              <Icon
                className={
                  "w-3 h-3 flex-shrink-0 " +
                  (active ? "text-codify-info" : "")
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
                  className="w-1.5 h-1.5 rounded-full bg-codify-accent flex-shrink-0"
                />
              )}
              {/* A page on the wire: motion, not a percentage — the runtime
              has no load-fraction event, and a fake one would be a lie with
              a keyframe. Bounded by the expiry in App, so a page that dies
              into an interstitial stops pulsing rather than forever. */}
              {!busy && loadingTabIds.includes(tab.id) && (
                <span
                  aria-hidden
                  data-loading="true"
                  className="w-1.5 h-1.5 rounded-full bg-codify-info flex-shrink-0 animate-pulse"
                />
              )}
              {/* Unread output: same fact family as the busy dot — something
              happened in a tab the user is not looking at — so the same size
              and shape, in the info colour to say *look here* rather than
              *still running*. Sighted users get the dot; screen-reader users
              get the name, which is why the aria-label above grew the suffix. */}
              {unread && (
                <span
                  aria-hidden
                  data-testid="terminal-unread-dot"
                  className="w-1.5 h-1.5 rounded-full bg-codify-info flex-shrink-0"
                />
              )}
              {/* Text nobody has saved: a warning-coloured dot, because this is the one mark on the strip that says
              "closing this loses something". Not the busy accent and not the info blue, which both mean "happening". */}
              {unsaved && (
                <span
                  aria-hidden
                  data-testid="editor-unsaved-dot"
                  className="w-1.5 h-1.5 rounded-full bg-codify-warning flex-shrink-0"
                />
              )}
              {/* The assistant changed it: the info blue, as the terminal's unread dot is — look here — and a ring
              rather than a disc so it cannot be mistaken for the unsaved dot beside it. */}
              {assistantEdited && (
                <span
                  aria-hidden
                  data-testid="editor-assistant-dot"
                  className="w-1.5 h-1.5 rounded-full border border-codify-info flex-shrink-0"
                />
              )}
              <button
                type="button"
                aria-label={`Close ${KIND_NAME[tab.kind].toLowerCase()}: ${tab.title}`}
                title={`Close (Ctrl+W)`}
                onClick={(e) => {
                  e.stopPropagation();
                  onClose(tab.id);
                }}
                className={
                  // Visible, always — it used to be `opacity-0
                  // group-hover:opacity-100`, which reads as "a close control
                  // a user cannot see", and that is a tab a user cannot close
                  // however well the click behind it works. The strip is
                  // already a row of truncated labels a hundred wide; hover is
                  // not a thing a person can find their way to on purpose.
                  "ml-1 rounded p-0.5 flex-shrink-0 hover:bg-codify-raised"
                }
              >
                <X className="w-3 h-3" />
              </button>            <span className="sr-only">{i + 1} of {tabs.length}</span>
          </div>
        );
      })}
      </div>
    </div>
  );
};
