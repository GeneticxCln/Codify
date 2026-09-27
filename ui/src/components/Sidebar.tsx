import React from "react";
import {
  Archive,
  Globe,
  MessageSquarePlus,
  MessageSquareText,
  Pencil,
  Settings,
  TerminalSquare,
} from "lucide-react";
import { Button } from "./ui/Button";
import { IconButton } from "./ui/IconButton";
import { ThreadMenu } from "./ThreadMenu";
import { newThreadParentId } from "../threadMenu";
import { UNTITLED_THREAD_TITLE, threadLabel } from "../threadTitle";
import type { Conversation, Workspace } from "../types";

/**
 * The side panel: what threads exist, which one is showing, and the one action
 * that starts a new one.
 *
 * This is the component that gives the app more than one conversation. Before
 * it, the transcript was one array of React state and "history" was a drawer
 * that re-added goals one at a time. A sidebar is the shape that fixes that:
 * the threads are *visible*, so a second line of inquiry is something you can
 * see rather than something you have to remember.
 *
 * Its own file rather than a section of the shell, for the reason the deliverable
 * cards have one: a component that renders alone is a component that can be
 * tested alone.
 */
export interface SidebarProps {
  /** Threads for the selected workspace, most recently touched first. */
  conversations: Conversation[];
  /**
   * Every workspace, so an unnamed thread can be called by its folder.
   *
   * A lookup rather than "the current one": the rows know their own
   * `workspace_id`, and naming each from the composer's selection would give a
   * row a folder it is not in.
   */
  workspaces?: Workspace[];
  /** The thread the visible tab is showing, if any. */
  activeConversationId?: string;
  /** Start a new thread and open it. */
  onNewChat: () => void;
  /**
   * Start a thread *on* `parentConversationId` and open it.
   *
   * Separate from `onNewChat` because they are different things and the panel
   * has to be able to say which is which. `onNewChat` is the button: a new line
   * of inquiry with no parent. This is the menu: a thread on the chat you are
   * looking at. Wiring both to one function is what made a menu item labelled
   * "New thread" produce a brand-new empty chat.
   */
  onNewThread: (parentConversationId?: string) => void;
  /** Show a thread. Reuses its tab if one is already open. */
  onSelect: (conversationId: string) => void;
  /** Rename a thread in place. */
  onRename: (conversationId: string, title: string) => void;
  /** Hide a thread from the panel. Archived, never deleted. */
  onArchive: (conversationId: string) => void;
  /**
   * The three shell surfaces that are not threads.
   *
   * They used to sit in the header as labelled buttons, which made the header a
   * second toolbar for things that have nothing to do with each other and pushed
   * the workspace pill and the connection pill out of the room they needed. They
   * are here instead because the panel is already "the things you can open", and
   * a browser tab, a shell and the settings screen are exactly that.
   *
   * Optional because a caller that does not offer one should render one fewer
   * badge rather than a dead control. A disabled button still advertises
   * something that cannot be used, which is the thing this whole change is
   * trying to stop.
   */
  onOpenBrowser?: () => void;
  onOpenTerminal?: () => void;
  onOpenSettings?: () => void;
  /** Set by the shell when the engine is unreachable, so the list can say so. */
  loading?: boolean;
}

/**
 * A thread's name, or a placeholder when it has none.
 *
 * The engine deliberately stores no title until a turn gives it one — an empty
 * string is more honest than a name the user never chose. The placeholder is
 * `UNTITLED_THREAD_TITLE` and not a literal here, because the tab strip names an
 * unnamed thread the same way and the two must not be able to disagree.
 */
function threadTitle(conversation: Conversation): string {
  return conversation.title.trim() || UNTITLED_THREAD_TITLE;
}

/**
 * The first line of what was asked, shown under the title.
 *
 * A transcript's threads are easy to tell apart only if the panel shows what
 * each one was about. The title is the user's own name for it; this is the
 * engine's record of the first prompt, and the two together are what a person
 * actually recognises.
 */
const SidebarRow: React.FC<{
  conversation: Conversation;
  active: boolean;
  /** The name of the folder this thread is in, when the panel knows it. */
  folderName?: string;
  /** The thread this one was started on, when the panel can still name it. */
  parentTitle?: string;
  onSelect: (conversationId: string) => void;
  onRename: (conversationId: string, title: string) => void;
  onArchive: (conversationId: string) => void;
}> = ({
  conversation,
  active,
  folderName,
  parentTitle,
  onSelect,
  onRename,
  onArchive,
}) => (
  <div
    className={
      "group flex items-start gap-1.5 px-2 py-1 rounded-md cursor-pointer border " +
      (active
        ? "bg-codify-raised border-codify-border"
        : "border-transparent hover:bg-codify-raised/60")
    }
    onClick={() => onSelect(conversation.id)}
    // A row that could only be opened by clicking could only be *managed* by
    // clicking the two hover buttons, which are invisible until the pointer is
    // already over the row. `role`/`tabIndex`/Enter make it a control a keyboard
    // can reach, and the browser then delivers the Menu key and Shift+F10 here
    // as a `contextmenu` event — so the menu below is reachable without a
    // mouse, which is the entire reason it is not a hover-only affordance.
    role="button"
    tabIndex={0}
    data-thread-row="true"
    data-thread-id={conversation.id}
    onKeyDown={(event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onSelect(conversation.id);
      }
    }}
  >
    <MessageSquareText
      className={
        "w-3 h-3 flex-shrink-0 mt-0.5 " +
        (active ? "text-blue-400" : "text-gray-500")
      }
    />
    <div className="flex-1 min-w-0">
      {/*
        The row's name: the thread's own title, or the folder it is in when the
        thread has not named itself yet. "New chat" is the last resort — it
        names nothing, and three unnamed threads in three projects are three
        identical rows. The folder name is short and it is the only real fact
        about a thread that has said nothing.
      */}
      <div className="text-xs text-gray-200 truncate">
        {threadLabel(conversation.title, folderName)}
      </div>
      {/*
        A thread on another thread says so.

        Without this the panel is a flat list of names, and "a new thread" and
        "a new chat" are indistinguishable rows — which is precisely the
        confusion the `parent_id` column exists to end. When the parent is not in
        the list (it was archived, or this is a filtered view) the row says
        "thread" and stops: the honest answer is that there was a parent, not a
        name this panel cannot see.
      */}
      {conversation.parent_id && (
        <div className="text-2xs text-gray-500 truncate">
          {parentTitle ? `on “${parentTitle}”` : "thread"}
        </div>
      )}
      {conversation.archived && (
        <div className="text-2xs text-gray-500">archived</div>
      )}
    </div>
    <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100">
      <IconButton
        label="Rename conversation"
        title="Rename conversation"
        size="sm"
        onClick={() => onRename(conversation.id, threadTitle(conversation))}
      >
        <Pencil className="w-3 h-3" />
      </IconButton>
      <IconButton
        label="Archive conversation"
        title="Archive conversation"
        size="sm"
        onClick={() => onArchive(conversation.id)}
      >
        <Archive className="w-3 h-3" />
      </IconButton>
    </div>
  </div>
);

export const Sidebar: React.FC<SidebarProps> = ({
  conversations,
  workspaces = [],
  activeConversationId,
  onNewChat,
  onNewThread,
  onSelect,
  onRename,
  onArchive,
  onOpenBrowser,
  onOpenTerminal,
  onOpenSettings,
  loading = false,
}) => {
  /**
   * A right-click in the thread panel starts a new thread *on the thread the
   * gesture was made in*.
   *
   * ## What "new thread" has to mean
   *
   * A thread *on* the chat you are in, and not a fresh empty chat that happens
   * to share a word with it. The difference is one column — `parent_id` on the
   * conversation — and everything else follows from it: the panel can show what
   * a thread is a thread on, and a reader can tell a branch from a fresh start
   * by looking at it. Without the column the two are the same row with the same
   * label, and the menu is a button that lies.
   *
   * ## Why the whole panel and not one row
   *
   * Because that is the ask, and because the alternatives were both tried and
   * both were wrong. A right-click on a *row* opening only that thread's
   * actions (rename / archive) is a menu about a thread; a right-click on the
   * panel *listing* the threads is a menu about choosing one. Neither of them
   * starts anything, and a gesture whose entire purpose is "I want to work on
   * something new" should not need a second click to get there.
   *
   * It is on the `nav` rather than on each row, so a right-click lands on the
   * same thing whether it hit a row, the New chat button, or the gap between
   * them. A row must not stop the event on its way up: that is the version
   * where right-clicking *on a thread* does nothing while right-clicking beside
   * it works, which is worse than not having it at all.
   *
   * `preventDefault` is the half that makes it visible. Without it the webview
   * puts its own menu on screen — Reload, Inspect — and the right-click looks
   * exactly like the app ignoring it.
   */
  const onPanelContextMenu = (event: React.MouseEvent<HTMLElement>) => {
    event.preventDefault();
    const row = (event.target as HTMLElement).closest("[data-thread-row]");
    if (!row) {
      setMenu({ x: event.clientX, y: event.clientY });
      return;
    }
    const conversationId = row.getAttribute("data-thread-id");
    if (conversationId) {
      setMenu({ conversationId, x: event.clientX, y: event.clientY });
    }
  };

  /**
   * Which thread the open menu is for, and where the pointer was.
   *
   * State and not a boolean, because a menu that does not know which thread it
   * is for is a menu that can act on the wrong one — and archiving the wrong
   * thread is not a mistake the user can undo from where they are sitting.
   */
  const [menu, setMenu] = React.useState<{
    conversationId?: string;
    x: number;
    y: number;
  } | null>(null);

  return (
    <nav
      className="flex flex-col gap-1.5 w-60 flex-shrink-0 border-r border-codify-border bg-codify-chrome p-1.5"
      onContextMenu={onPanelContextMenu}
    >
      {/*
        The panel's one creation action, at the menu's density.

        `size="sm"` rather than `md` so the button is the same height and the
        same 11px type as the rows beneath it and the menu's items — a toolbar
        primitive at dialog size in a list of compact rows reads as a headline
        the panel did not intend. `align`/`weight` are the props rather than
        classes for the reason `Button.tsx` gives: a `justify-start` class and a
        `font-normal` class both lose to the primitive's own base on stylesheet
        order, which is how the label ended up centred and bold.
      */}
      <Button
        tone="subtle"
        size="sm"
        align="start"
        weight="normal"
        onClick={onNewChat}
        className="rounded-md"
      >
        <MessageSquarePlus className="w-3 h-3" />
        New chat
      </Button>

      <div className="flex-1 overflow-y-auto flex flex-col gap-0.5">
        {loading ? (
          <div className="text-2xs text-gray-500 px-2 py-1">Loading threads…</div>
        ) : conversations.length === 0 ? (
          // An empty panel is a state worth naming. Without this the sidebar is
          // a blank column and "New chat" is a button with no explanation of
          // what it will make.
          <div className="text-2xs text-gray-500 px-2 py-1 leading-relaxed">
            No conversations yet. Start one and it stays here between sessions.
          </div>
        ) : (
          conversations.map((c) => {
            // The parent's name comes from the row, not from a lookup in this
            // list. The list is this workspace's *live* threads, so looking the
            // parent up here failed the moment it was archived — and the label
            // degraded to a generic word with no way back, because the name was
            // still in the database and nothing on screen could reach it. The
            // engine joins it onto every child for exactly this reason.
            const folder = workspaces.find((w) => w.id === c.workspace_id);
            return (
              <SidebarRow
                key={c.id}
                conversation={c}
                active={c.id === activeConversationId}
                folderName={folder?.name}
                parentTitle={c.parent_title || undefined}
                onSelect={onSelect}
                onRename={onRename}
                onArchive={onArchive}
              />
            );
          })
        )}
      </div>

      {/* The three shell surfaces, at the foot of the panel, named and spread
          across its width.

          They came out of the header, where three labelled buttons sat in a row
          of things that have nothing to do with each other. They are at the
          bottom rather than beside "New chat" because the top of the panel is
          for starting a thread and the middle is the threads themselves; the
          foot is the one part of the column that is not either, which is what a
          browser tab, a shell and the settings screen are.

          ## Why the icon is above the name

          Measured in the running app, not guessed: laid out inline, the three
          came to 241px inside a 223px row, and Settings' right edge sat at 249px
          in a 240px panel — hanging outside it. `Button` carries
          `whitespace-nowrap`, so a squeezed label cannot wrap; it just overflows,
          which is the failure its own comment warns about.

          Stacking is what fits. A cell is then only as wide as its longest word
          (~41px for "Terminal") rather than that word *plus* a 12px icon and a
          6px gap, which is the difference between 241px of content and 183px of
          it. The names stay; the row stops overflowing.

          `flex-1` on each rather than a left-aligned cluster, and it is also the
          only version whose gaps stay even whichever two of the three are
          present — a fixed `gap` would leave the last one hard against the right
          edge whenever one is omitted. (`justify-between` is *not* used here:
          with `flex-1` filling the row there is no free space left for it to
          distribute, so it would be a class that claims to do this job and does
          nothing.)

          The padding is left alone deliberately. `Button`'s `sm` size sets
          `px-2.5`, and a `px-1` in `className` does **not** override it —
          Tailwind orders utilities in the stylesheet, not in the class
          attribute, so `px-2.5` wins however the two are written. The layout
          fits without fighting the cascade, which is better than reaching for
          an important modifier to lose an argument with it.

          `overflow-hidden` on the row and `min-w-0` on each cell mean a future
          longer name truncates instead of escaping the panel. That is the whole
          point: a control that can paint outside its own column is a bug that
          only shows up on one machine at one font size.

          `Button`, not `IconButton`: these are named now, and a visible name is
          the label. `title` still carries the longer sentence, which is the only
          place "the page opens in its own window" fits. The rule above
          separates them from the list so they read as the panel's own furniture
          rather than as another thread. */}
      <div className="flex items-stretch gap-1 border-t border-codify-border pt-1.5 overflow-hidden">
        {onOpenBrowser && (
          <Button
            tone="subtle"
            size="sm"
            onClick={onOpenBrowser}
            title="Browser — the page opens in its own window"
            className="flex-1 min-w-0 flex-col gap-1"
          >
            <Globe className="w-3 h-3 flex-shrink-0" />
            <span className="text-2xs truncate">Browser</span>
          </Button>
        )}
        {onOpenTerminal && (
          <Button
            tone="subtle"
            size="sm"
            onClick={onOpenTerminal}
            title="Terminal — a shell in this workspace"
            className="flex-1 min-w-0 flex-col gap-1"
          >
            <TerminalSquare className="w-3 h-3 flex-shrink-0" />
            <span className="text-2xs truncate">Terminal</span>
          </Button>
        )}
        {onOpenSettings && (
          <Button
            tone="subtle"
            size="sm"
            onClick={onOpenSettings}
            title="Keys & Endpoints"
            className="flex-1 min-w-0 flex-col gap-1"
          >
            <Settings className="w-3 h-3 flex-shrink-0" />
            <span className="text-2xs truncate">Settings</span>
          </Button>
        )}
      </div>

      {menu &&
        (() => {
          // Looked up from the list rather than stored in the menu state, so the
          // header shows the thread's *current* name. A menu carrying a copy of
          // the title would go stale the moment a turn named the thread.
          const target = menu.conversationId
            ? conversations.find((c) => c.id === menu.conversationId)
            : undefined;
          // The thread this gesture is about: the row under the pointer if the
          // right-click hit one, otherwise whatever is on screen. Resolved once,
          // here, and used for both the label the reader sees and the id the new
          // thread is created with — so the menu cannot say one thing and do
          // another.
          //
          // The panel is passed as the boundary, which is what keeps a tab left
          // over from another workspace from being offered as a parent: the
          // engine refuses a cross-workspace parent with a 422, and the user
          // would get an error banner from a right-click that should have
          // worked. `newThreadParentId` says why in full.
          const parentId = newThreadParentId(
            menu.conversationId,
            activeConversationId,
            conversations.map((c) => c.id),
          );
          const parent = parentId
            ? conversations.find((c) => c.id === parentId)
            : undefined;
          // A thread that has gone (archived by another window) leaves a menu
          // with no thread: still "New thread", no longer acting on a row that
          // is not there.
          return (
            <ThreadMenu
              title={target ? threadTitle(target) : undefined}
              parentTitle={parent ? threadTitle(parent) : undefined}
              x={menu.x}
              y={menu.y}
              onClose={() => setMenu(null)}
              onNewThread={() => onNewThread(parentId)}
              onRename={() =>
                target && onRename(target.id, threadTitle(target))
              }
              onArchive={() => target && onArchive(target.id)}
            />
          );
        })()}
    </nav>
  );
};
