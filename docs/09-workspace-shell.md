# Codify — the workspace shell: conversations, tabs, terminal, browser

**Status: conversations, tabs, the terminal and the browser are all built, and
so are both panes.** The plan approved for this work had five phases; the first
four shipped, and what is here is what they describe — the schema and routes of
§2–§3, `src-tauri/src/terminal.rs` plus the xterm pane that drives it in §7.1,
the browser's isolation in `src-tauri/src/browser/` and
`src-tauri/capabilities/browser.json` in §7.2, and the pane that drives that in
§7.3.

**The page is embedded now.** A browser tab's webview is a child of the main
window, seated over the pane's measured content rectangle — the pane is the
address bar *and* the viewport. §7.3 has the mechanics and §7.2 the isolation
story for a page that now shares a window with the app's own UI.

## 1. What this fixes

The app had no conversation identity. `ui/src/App.tsx` held one
`messages: ChatMessage[]` in `useState`, every send called `createGoal()` — one
goal per turn, flat in one array — and reloading the window lost the thread.
History was a *drawer* that re-added goals one at a time. So "multi-conversation"
was not a missing feature but a missing concept: the app had exactly one
conversation and it evaporated when the window did.

A goal and a conversation are deliberately different rows. A **goal** is one run:
a plan, steps, a verifier, a commit. A **conversation** is the question several
runs answer. Collapsing them would mean one tab is one goal and the app cannot
hold two lines of inquiry in the same workspace.

## 2. Persistence

`engine/db.py`, alongside `workspaces` and before `goals` so the reference
resolves:

```sql
CREATE TABLE conversations (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT '',
  archived INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

ALTER TABLE goals ADD COLUMN conversation_id TEXT
  REFERENCES conversations(id) ON DELETE SET NULL;
```

Three decisions in that shape, and why:

- **Held in the engine, not the client.** A conversation in `localStorage` would
  vanish with the browser profile, which is the exact defect this replaces.
  `goals` were already durable and the thread that names them must be too.
- **Nullable.** A goal that predates conversations keeps `NULL` and reads as its
  own single-turn thread, so the migration orphans nothing. An install from
  before this column keeps every goal — asserted in
  `tests/test_conversations.py::MigrationTestCase`, which hand-writes the *old*
  schema on purpose, because a migration test that only ran against today's
  schema would pass no matter what the migration did.
- **Archived, never deleted.** A goal belongs to the thread it was asked in.
  `DELETE /conversations/{id}` drops the thread and `ON DELETE SET NULL` leaves
  every run in the history as its own thread — closing a tab is not a way to
  erase an audit trail.

Indexes: `idx_goals_conversation (conversation_id, created_at)` and
`idx_conversations_workspace (workspace_id, updated_at)`. Listing a thread's
turns is the hot read of a tab switch.

### 2.1 The tab strip, which is an engine record with a client mirror

The rule above is *records in the engine, view state in the client*, and a
conversation is a record. The tab strip used to argue it was the other half —
"only the shell process can re-create a webview, so only the client needs the
layout" — and that argument was correct until the first person opened a second
window and asked where the other window's tabs were. A layout one process holds
is a layout one window sees. The strip is now an engine record; what stayed in
the client is the half the rule was always about: `id`s and the active tab (this
process's webviews, this process's attention), and a mirror for the moments the
engine is not answering.

**What it closes.** Nothing remembered the strip at all. `tabState` was
`useState<TabState>(emptyTabs)`, so every browser tab died with the process —
not just the tab but the address it was on and its whole back/forward stack,
which is the half that is actually missed. Chat tabs were the least affected,
because a thread can always be re-opened from the panel, one click per thread
instead of one click for the arrangement.

**The two stores, and who owns what.**

- **The engine** holds `shell_tabs` (`engine/db.py`): `key`, `position`,
  `kind`, and a `payload` of JSON the engine never opens. Three authenticated
  routes (`engine/app.py`): `GET /shell/tabs` returns the strip ordered by
  position; `PUT /shell/tabs` upserts *one tab* and answers with the whole
  strip (a push is also a read — the writer learns the other window's state in
  the same round trip); `DELETE /shell/tabs/{key}` is idempotent and answers
  with what remains. `ShellTabService` validates only that the payload is
  JSON; what a payload must contain to be restorable is the client's law
  (`layoutSync.ts` decodes with the same refusals `tabPersistence.ts` applies
  to the mirror), because the engine cannot have an opinion about a shape it
  treats as opaque.
- **The client** mirrors the strip to `localStorage` under one key
  (`CODIFY_TABS`, `ui/src/tabPersistence.ts`), written through on every change
  *before* the engine is told. The mirror exists so a restore is synchronous
  at first render — a window whose pages would paint fine does not lose its
  strip because the engine is restarting — and so a crash leaves a readable
  layout whose tabs all carry keys.

**Identity is the key, and it is minted once.** Every remembered tab carries a
`k_<base36 time>_<counter>` key (`ensureKeys`), assigned when the tab is first
seen and never re-minted — not on restore, not on adoption. The key is the only
thing that says "the same tab" across windows: two rows for one address are two
tabs, and a window that re-keyed a tab on arrival would turn one shared tab into
two. `position` is order, not identity. Terminal tabs stay local (a PTY is a
live process of this shell; there is nothing to share), so they have no key and
no row.

**Every shape is checked on the way in**, because a stored value is not a type
and a row's payload is a stranger's JSON, and the degradation is always *less*:
an unreadable value is an empty strip, a tab that cannot be made whole is
dropped rather than repaired, and nothing here surfaces an error, for the reason
`modelFreshness.ts` gives. Two rules are worth naming:

- **An address the shell would refuse takes the whole tab with it.** Restored
  addresses go through `classifyBrowserAddress` — the same mirror a typed one
  goes through, so a stored `example.com` comes back as `https://example.com/`
  and a stored `http://127.0.0.1:7430/` is refused here rather than seated. The
  shell's `navigation_allowed` remains the enforcement point; this is the same
  refusal arriving before a webview exists.
- **A redirect moves the current entry; it does not add one.** The page reports
  its live address after a redirect and `setBrowserPageUrl` records that on the
  tab alone, so the stack's cursor names the address the redirect came *from* —
  which is every page that redirects, and most pages do. Writing that as-is
  would drop the whole stack on nearly every site, silently costing the user
  their back button, and the round-trip test is what caught it. The current entry
  is replaced at the cursor instead.

Capped at 24 tabs (keeping the tab that was showing, plus the leftmost of the
rest) and 100 history entries (the most recent, ending at the cursor). Both are
generous enough that a real session never reaches them and both exist to bound
what is written on every navigation.

**Restoring is synchronous, from the mirror, before the engine answers.** The
strip is rehydrated where it is created; what the engine eventually says is a
*merge* (below), not a replacement, so a window that rendered from the mirror
keeps the tabs it already showed. The *pages* cannot be seated at restore: a
webview opened before the pane has been measured is refused by the shell (§7.2's
zero-bounds refusal), and only the pane knows its own rectangle. So a separate
effect opens a restored tab's page once a real rectangle has arrived — through a
path that opens the page and **touches no tab state**, because
`handleOpenBrowser` creates the tab from an address bar and `openBrowserTab`
starts a fresh one-entry history, which would throw away the very stack the
restore brought back. `ui/tests/tabPersistence.test.ts` pins that wiring by
reading `App.tsx`, alongside the pure half.

Two honest limits, neither of which is hidden by a heuristic. A **workspace that
has been deleted** leaves its tabs behind: the tab stays (the strip is the
user's arrangement and is not silently pruned) and shows no folder pill, because
`workspaceId` is optional and a tab with no folder shows none rather than a
guess. A **chat tab whose thread is gone** hydrates into whatever the panel says
about a thread that is not there, which is the same thing clicking that thread in
History would have done.

**The merge, in one sentence and then in its rules.** A window reconciles the
engine's strip against its own — local wins on a key both have, the engine's
rows are adopted where it has none, and a local tab is dropped only when the
engine has *confirmed* it and then stopped listing it.

- **A keyed local tab wins over its row.** The window showing the page is the
  authority on it; the push that follows tells the engine, and the other window
  converges on its next round trip. Windows agree by exchange, not by a
  referee.
- **A row whose key nobody here has is somebody else's tab, and is adopted**
  with a fresh local `id` — the webview does not exist here yet, and the pane
  seats it from the row's own address. The engine's `position` is where it
  lands, so both windows show one order.
- **Only a *confirmed* absence is a close.** A key the engine held a moment ago
  and does not list now is gone everywhere; a key the engine has never heard of
  is simply not pushed yet. This `known`-qualification is the difference
  between sharing a strip and losing one: a fresh engine (a new `codify.db`, a
  restart in progress) answers with an empty strip that must retire nothing.
  The set of confirmed keys costs one boot-time read, is refreshed by every
  answer, and the alternative — trusting emptiness — emptied a full mirror in
  the test that pinned this rule.
- **A window's own unacknowledged closes are held back.** Until the engine has
  answered the `DELETE`, its row is the pre-close one; adopting it would put
  back the tab the user closed. The removal queue survives an offline close
  for exactly this reason.
- **A merge that changes nothing returns the state it was given.** The
  reconciler runs inside `setTabState((prev) => ...)`, so a rebuilt-but-equal
  strip is a re-render, and a re-render re-fires the push, and the push pulls
  again — the loop that kept `terminalEndToEnd` pending forever and would have
  request-looped against a real engine. Local tab objects are kept by identity,
  never re-decoded (a decode mints a fresh `id`), and the no-op answer is the
  *same* object. `ui/tests/layoutSync.test.ts` pins this contract directly.

## 3. HTTP surface

| Route | What it is for |
|---|---|
| `POST /conversations` | Start a thread. Empty title is the ordinary case. |
| `GET /conversations?workspace_id=&include_archived=` | The side panel, most recently touched first. Archived hidden unless asked. |
| `GET /conversations/{id}` | One thread. |
| `GET /conversations/{id}/turns` | The thread's turns, oldest first. Derived, not stored. |
| `PATCH /conversations/{id}` | Rename. The only mutable thing about a thread. |
| `POST /conversations/{id}/archive?archived=` | Hide from the panel, or restore. |
| `DELETE /conversations/{id}` | Drop the thread, keep the runs. |
| `PUT /goals/{id}/conversation` | Attach an *existing* goal to a thread — the other half of `POST /goals` (§6). |

`POST /goals` accepts an optional `conversation_id`; an existing goal joins a
thread through `PUT /goals/{id}/conversation`.

### 3.1 The link is checked, not free

A conversation is not a label. `GoalService._check_link` refuses a
`conversation_id` that does not exist (`unknown_conversation`, 404) and one that
belongs to another workspace (`conversation_workspace_mismatch`, 422). It is one
function called by both creation *and* the attach route: if attachment were
looser than creation, `PUT /goals/{id}/conversation` would be the reach-around
this section forbids. Without the workspace check a client could attach a run to
another workspace's thread and a tab would show one project's work under
another's name.

Creating a goal bumps its thread's `updated_at`. The panel is ordered by last
touch, so a thread nobody has spoken in for a week must not stay pinned above one
opened today.

### 3.2 Turns are derived

`ConversationTurn` is a shape the API draws from the goal that answers it, never a
second record. The prompt is the goal's description, falling back to its title for
a goal created before descriptions carried the full text.

### 3.3 Invariant 2 is untouched

Adding `conversation_id` to `GoalCreate` does not weaken `extra: "forbid"` —
docs/00 §6.2 rests on that. `agent_config` and anything else unlisted are still
refused on `POST /goals`, and `ConversationUpdate` forbids extras too, so a body
that tried to set `archived` or `workspace_id` through the *rename* route is a 422
rather than a silent rewrite. Both are asserted in
`tests/test_conversations.py`.

## 4. The tab model

`ui/src/tabs.ts` is a pure module, because tab arithmetic is impossible to see in
markup and easy to get subtly wrong. `ui/tests/tabs.test.ts` covers it without a
DOM.

```ts
type TabKind = "chat" | "terminal" | "browser" | "editor";
interface Tab {
  id: string;
  kind: TabKind;
  title: string;
  conversationId?: string;   // the thread a chat tab shows; absent on a clean slate
  workspaceId?: string;      // the project this tab belongs to — a chat tab's identity
  url?: string;              // a browser tab's current address
  path?: string;             // an editor tab's file, relative to its workspace's root (§13)
  // plus `history`, `ptyId` and `exited` for the browser and terminal kinds
}
interface TabState { tabs: Tab[]; activeId: string | null }
```

Settled decisions:

- **New Tab sits between the CODIFY badge and the strip; the side panel is the
  selected project's thread list, and it opens threads rather than tabs.**
  `App.tsx` renders `NewTabButton` in the header, immediately after the badge and
  immediately before one global `TabBar`, above the two-column workspace. It was
  the *last* thing in the strip, which is the wrong end for it: a strip of ten
  tabs is a ten-tab walk to the one control that shortens the strip, so the
  control whose cost rose with the number of open tabs was the one furthest from
  the pointer. It is disabled until a project is selected — a tab is a project's
  window, and there is no window without one. The side panel offers **New
  Project** (browse and add/select a folder) and nothing else in its button row;
  its **New Thread** button is gone, because it created a thread *and* opened a
  tab for it, which made it a second New Tab in a second place that also left a
  conversation behind before anything was typed. A thread is now made by typing
  in a clean slate. The panel's right-click menu still starts a thread *on*
  another thread (`parent_id`), which is a different act with a different parent
  and the only "new" the panel offers. Chat tabs stay open when the project
  picker changes, because each tab records its own `workspaceId` and a project
  switch must not close transcripts or live terminal/browser instances. The
  sidebar names the selected project and shows only that project's unarchived
  conversations. The API query is scoped by `workspace_id`; the client caches
  lists by workspace, ignores stale fetches, and the sidebar filters again at
  its boundary. With no project selected the panel says so and shows no threads.
  `ui/tests/shell.test.ts` covers placement, the absence of the panel's new-tab
  control, scope isolation, and the empty-project state;
  `ui/tests/tabStripInteraction.test.ts` covers the control itself and the fact
  that the strip no longer offers one.
- **One project per tab, and a project's threads live in it.** A chat tab is a
  *project's window*, not a thread's. Opening a thread is therefore a question
  about which project it belongs to and never about making room, and
  `openConversation(state, conversationId, title, workspaceId)` is the only way
  in. It answers in three steps, and each step is a case the previous one got
  wrong: a tab already showing the thread is **focused**; otherwise the tab you
  are looking at, if it is a chat tab in that project, **shows the thread**,
  replacing what was in it; otherwise a **new tab** for that project. The strip
  is the set of projects you currently have open, and several tabs of one
  project are possible — New Tab twice is two windows onto the same project,
  which is the control's whole use. This is *not* the single shared "Chats" tab
  that was tried and reversed: the project is still a tab, still in the strip,
  and still yours to close; what is not a tab is each individual thread.
- **Opening a thread you already have open focuses its tab; it does not open a
  second one, and it does not move it either.** `tabForConversation` keys on
  `conversationId`, so two tabs on one conversation — two live event streams over
  the same goal — are unreachable. Because two tabs of one project can coexist,
  this step has to come *before* the one above it: choosing a thread that is
  already open in a background tab must bring that tab forward rather than show
  the thread in the tab you are already in, which would leave one thread on
  screen twice. This is a *deduplication*, not a cap: there is no fixed number of
  chat tabs. Terminals and browser tabs are unaffected — those are one tab per
  live instance, because two terminals are two shells and two browser tabs are
  two webview windows, and collapsing either would make the second unreachable.
- **New Tab opens a clean slate, and creates nothing.** `openBlankTab(state,
  workspaceId)` is a chat tab with a project and no `conversationId` — a state a
  tab is allowed to be in. The old New Tab called `createConversation` before it
  opened anything, so a tab *was* a conversation from the moment it appeared, and
  pressing the button to look around left an empty conversation behind, named
  from a prompt nobody wrote, with a row in the side panel. The thread is now
  made by the first prompt, in the send path, which is the only place that has
  the prompt to name it with — so a tab opened and abandoned costs nothing. The
  send path then hands the new conversation to `openConversation`, which finds
  the very tab it was typed in (a clean slate in the same project) and fills it,
  so the first prompt does not leave an empty tab beside the thread it created.
- **Archiving a thread blanks its tab rather than closing it.** The tab is the
  project's window, so closing it would destroy a window the person is still
  working in, and archiving one thread should not be a way to lose a project.
  What is left is the same clean slate New Tab opens, in the same project. A
  thread with no tab is the ordinary case (archived from the panel without being
  read) and changes nothing.
- **A thread is called by its name, by its folder, or by nothing else.** The
  engine stores no title until something gives a thread one, so the strip and
  the panel both need a fallback and must use the *same* one: `threadLabel` in
  `threadTitle.ts`, used by `TabBar` and `Sidebar`.  A named thread is called by its name. An **unnamed** one is called by its
  **folder** — the workspace's short name, never its path. `"New chat"` is the
  last resort, for a thread that has neither. A **clean slate** is the second
  case, not the third: a tab with nothing in it is in a project, so the folder is
  a real answer, and "New chat" would name nothing at all.
  The folder is there because `"New chat"` names *nothing*: not the thread, not
  where it is, not which of three identical tabs you are looking at, and
  opening any one of them looks exactly like nothing happened. The folder names
  a real thing, and for a thread that has said nothing yet it is the only real
  thing there is. A path is not printed because a path is long, repeats across
  every tab in the same project, and the name is the part that differs between
  projects — the full path is the tooltip and the accessible name instead.
  Re-showing the *same* thread keeps the name its tab already had, so a tab
  opened before the engine named the thread picks that name up later.
- **A tab knows its own folder, and the header does not claim one.** The
  header used to carry a pill naming the composer's workspace — one global
  claim about which folder the app was in, which is wrong for every tab but the
  one the composer happens to point at. The folder is a fact about a *tab*, so it
  rides on `Tab.workspaceId` and the strip resolves it through the full
  workspace list. `openConversation` records it and `withWorkspace` fills it in
  for a tab opened from a goal before the conversation had been read.
  A tab with no recorded folder shows **no** folder: naming one from the
  composer would print a directory the thread is not in. Tabs staying open
  across a workspace switch is deliberate — two projects at once is legitimate
  and closing the strip would destroy live transcripts — which is exactly why
  the folder had to move off the header and onto the tab.
- **A thread's first prompt is its name.** This is the difference between the
  feature working and not working at all: a thread starts unnamed, and the engine
  stores exactly what it is told, so a tab showing a thread nobody has typed into
  is a tab labelled "New chat" and opening one is indistinguishable from opening
  nothing. `threadTitleFromPrompt` (`ui/src/threadTitle.ts`) collapses the
  prompt's whitespace and cuts it at 60 characters **on a word boundary** — a
  tab is 224px wide, and "so every row h" is a name you read twice. The turn
  calls it only for a thread it can see is unnamed, so a name the user chose is
  never overwritten, and `nameThread` moves the *tab* with the rename, because a
  name the engine accepted that the strip never learned about is the same
  invisible-thread problem one layer down.
- **Closing lands on the tab to the left**, or the one that slid into the slot
  when the closed tab was first. A transcript read to the bottom leaves you
  wanting the tab you were on before.
- **Closing the last tab leaves `activeId: null`.** An empty shell is the honest
  state; inventing a tab to land on would mean closing everything and still
  looking at a transcript. The header's New Tab button, beside the badge, is the
  way back.
- **Reordering clamps.** A drag that lands past the last tab is a move to the end,
  not a tab that vanishes.
- **A tab's id is not its conversation's id.** Tabs are generated; look up by
  thread when acting on one from a thread's id. This is also why every function
  that takes a conversation id looks its tab up by `conversationId` rather than
  matching ids — two tabs sharing an id must not let one close the other.

## 5. How multi-conversation is implemented, and the one thing to not break

`messages` in `App.tsx` stays **one flat list**, and `ChatMessage` gained a
`conversationId`. The visible transcript is a derived filter:

```ts
const visibleMessages = messages.filter(
  (m) => m.conversationId === activeConversationId
);
```

This is the whole of the change on the UI side, and it is deliberate. The
alternative — a store keyed by "the conversation currently on screen" — writes a
running goal's events into whichever thread happens to be visible when they
arrive. Threading the message instead means a goal streams into the thread it was
asked in even while the user is reading another one in another tab.
**If you change this, change it so the live-run path still targets messages by
id across all threads.**

The sidebar (`ui/src/components/Sidebar.tsx`) and the strip
(`ui/src/components/TabBar.tsx`) are rendered tests:
`ui/tests/shell.test.ts`. Each tab's kind is in its `aria-label`, because an icon
is a shape and a screen reader gets nothing from it; the strip is the only place
that knows a tab is busy when it is not the one on screen.

### 5.1 The thread right-click menu

**A right-click anywhere in the thread panel opens one menu.** It is the same
menu and the same handler whether the click landed on a thread row or on empty
panel beside them — one gesture, one control, no second code path to be right.

- **On a thread:** `New thread`, then `Rename` / `Archive` for that thread,
  under a header naming it. Archive is the only one marked as the item that
  takes something away, and there is no Delete: the engine keeps every run
  either way.
- **On empty panel:** `New thread` alone. Rename and Archive act on a thread
  that is not there, and offering them anyway is two controls that silently do
  nothing — the one thing this app does not ship. Starting one *is* everything
  you can do on empty panel.
- **There is no `Open`.** There was one, and it was the row's own click
  handler. On a thread that was already open, `openConversation` only *focused*
  its tab, so the item did nothing at all: a control that looked alive and was
  not, in the one place the reader had been told the menu could act on this
  thread. The row is a button — clicking it opens the thread, and that is where
  opening lives. A menu item that can only be a slower version of the click
  under the pointer is a decoration, not an action.

**`New thread` is the first item**, on purpose. A right-click in this panel is
the gesture for "I want to work on something new", and the item that does that
should not be the one the pointer has to travel to. It is also the only item
that needs no thread, which is what lets one menu serve both cases — that
distinction is `needsThread` in `ui/src/threadMenu.ts`, and
`threadMenuItems(hasThread)` filters on it.

**What a new thread is *on* is the whole point of the item.** It creates a
thread on the thread the gesture was made in — `conversations.parent_id`, set at
creation and nowhere else — and not a brand-new chat that happens to share a
word with it. A conversation with no `parent_id` *is* a new chat: a different
object, and the only symptom is an unrelated empty tab opening. An item
labelled "New thread" wired to the new-chat button is that defect exactly, so
`onNewChat` and `onNewThread` are separate handlers in `Sidebar` and the menu
uses the second.

- A right-click **on a row** branches off that row. You pointed at it.
- A right-click on **empty panel** branches off the thread you are looking at.
  With nothing open, a top-level thread — an ordinary outcome, not a failure.

`newThreadParentId(rowId, activeId, panelIds)` decides it, and the menu names
its target in the item's own line (`on "Refactor the parser"`) so the reader
finds out *before* the click rather than after the new tab is already open.

**The panel is the boundary on that choice**, and passing `panelIds` is how it
is enforced in one tested place rather than in a component's JSX. A tab from
another workspace stays open when the selector moves (see §4), so the active tab
can name a thread this panel is not showing. Branching off it would ask the
engine for a child in this workspace whose parent is in another, which it
refuses with `parent_workspace_mismatch` (a 422) — so a right-click that should
have created a thread produced an error banner, and printed no `on "…"` line
first because the name could not be resolved either. No parent *in the panel*
means a top-level thread, which is true and creates fine.

**A child's lineage is data, not a lookup.** The engine joins the parent's name
onto every conversation (`Conversation.parent_title`, from
`ConversationService._SELECT_WITH_PARENT`) rather than leaving the client to
resolve it. The panel lists one workspace's *live* threads and hides archived
ones, so a child whose parent was archived could not look that parent up in the
list it was given: the id was still on the row and the name still in the
database, but nothing on screen connected them, and the label degraded to a
generic word **permanently**. `parent_title` is `None` only when the parent row
is genuinely deleted — `ON DELETE SET NULL` orphans the child rather than
cascading it, because archiving a parent should not take its children's history
with it.

**The panel owns the gesture.** The handler is on the `nav`, and a row is found
by the attributes it carries (`data-thread-row`, `data-thread-id`) — a row used
to have its own handler, which is two paths for one behaviour and the reason a
right-click *on a thread* could do nothing while the same click beside it worked.
The menu then resolves the thread from the list, so it acts on the row that was
clicked and not on whichever thread happens to be first.

**`preventDefault` is not optional.** Without it the webview puts its own menu
on screen — Reload, Inspect — and the right-click looks exactly like the app
ignoring it, which is the symptom this handler exists to remove.

**Clamping is not decoration.** The panel is 240px wide, so *every* right-click
lands near an edge, and an unclamped menu hangs off the window — worst on the
last thread, which is where Archive is. It clamps to the **window** rather than
the panel, because pinned inside a 240px column the menu sits under the pointer,
which is where a stray click lands. Bounds smaller than the menu pin to the near
edge; the subtraction would otherwise produce a negative coordinate, a menu
positioned off-screen to fit. The component measures itself and re-clamps, and
`THREAD_MENU_FALLBACK_SIZE` is asserted to fit the items it is a fallback for —
growing the menu fails a test rather than quietly shrinking the guess.

Dismissal is a transparent sheet under the menu rather than a `document`
listener: nothing to unregister, and a right-click outside an open menu closes it
instead of putting the OS menu on top. The row is `role="button"` with
`tabIndex={0}`, so the menu is not mouse-only — the browser delivers the Menu key
and Shift+F10 as a `contextmenu` event.

### 5.2 The build that ships the frontend at all

`src-tauri/build.rs` declares `cargo:rerun-if-changed=../ui/dist`, and that line
is load-bearing in a way that is invisible until it is missing.

`tauri::generate_context!` lives in `lib.rs` and embeds `frontendDist` into the
binary at compile time. Cargo tracks that by the fingerprint of the crate
invoking the macro — so if only `.ts`/`.tsx` files change, `lib.rs` is untouched,
cargo calls the crate fresh, and **the binary keeps serving the previous
`ui/dist`**. Every source-level test passes against the new code while the app
serves the old one, which is precisely how a feature can be "implemented, tested
and shipped" and still do nothing when you click it. Touching `main.rs` does not
help: the macro is not there.

Declaring the dist directory as a build input puts the files in the fingerprint,
so any change to them re-runs the script and forces `lib.rs` to be re-expanded.
**When changing anything under `ui/src`, the build sequence is
`npm run build` then `cargo build`** — and if a UI change does not appear in the
app, check that sequence before suspecting the code.

### 5.3 The window itself: undecorated, and Wayland when there is one

The main window sets `"decorations": false` in `src-tauri/tauri.conf.json`. Under
a tiling compositor the title bar is dead weight the compositor will not draw
anyway, and a window drawn by GTK with client-side decorations puts the app's own
close button and maximise square on top of a layout the user is arranging by
keyboard. The close affordance does not go missing: it moves to the compositor,
which is where a niri/sway user already closes windows.

A frameless window is not the whole story, because GTK chooses its backend from
the environment and the choice is a coin flip when a session has **both** a
Wayland compositor and an X server — every Wayland session with Xwayland, which
is most of them. Land on X11 and the app is an X client inside a Wayland session:
Xwayland draws a title bar for it regardless of `decorations`, and a compositor
tiles a window whose chrome it does not own. So `lib.rs` names the backend:
`apply_display_backend()` runs **before** `Builder::build()` (GTK reads
`GDK_BACKEND` while it initialises, on that same call, so a variable set later is
a variable nothing reads) and sets it to `wayland` when a Wayland session is up
and nothing has overridden the choice.

The conditions are the point, and `display_backend` is a pure function precisely
so they can be tested without touching the process environment: an X11 session is
left exactly as it was (forcing Wayland onto a machine that has none is a window
that never appears, which is worse than a title bar), and an explicit
`GDK_BACKEND` wins — which is also how a user undoes this without a rebuild. The
launch says which one it did, once, because "the title bar is back" is otherwise
a bug whose cause cannot be read off the launch.

### 5.3.1 The pill, and what it does when the engine moves

The header pill is a poll, not a decoration: `checkEngineHealth` asks `/health`
every 3s (15s while the tab is hidden) and the pill reads `Live`, `Offline`,
`Checking` or `Auth stale`. Two of those four mean *the connection we hold is
stale*, and they want opposite answers about what to do.

`Auth stale` is the one the banner exists for: an engine is there, it rejects
our token, and the fresh token lives with the engine's spawner, so the page
cannot mint it. The other staleness is quieter and used to be misread. `!ok` was
taken to mean "the engine is down" — but nothing answering at the port we hold
has a second reading, *the engine is up on a different port*, and the two look
identical from inside the page. An engine that restarts and takes a new port
leaves an **open** window showing a red pill beside a perfectly healthy engine,
forever, because the boot-time fetch of engine info has long since run and the
poll was the only thing still looking.

So the poll asks the shell. `codify_get_engine_info` is the one party that
watched the live handshake, and on either staleness — `ok && !authenticated`
**or** `!ok` — the probe asks it; a changed answer is applied to the API client
and its localStorage write-through, the workspace and model loaders re-run
because a fresh token may name a different engine's providers, and the probe
re-runs **immediately** rather than leaving a user to stare at a wrong answer
for another three seconds. Nothing is invented: an unchanged answer means the
engine really is gone and the pill says `Offline`, which is the honest report.

The open window is the case worth testing, and it is the one
`ui/tests/healthProbeRecovery.test.ts` holds: a cold boot already recovers on
its own (the mount effect fetches engine info from the shell), so a test that
only seeds a dead port passes without this branch at all. The test therefore
mounts the real `App` against a fetch that refuses the held port, lets the boot
settle, asserts the pill honestly reads `Offline`, and *then* moves the engine —
which only the shell is told about — and waits for the app's own 3s poll. That
last part is deliberate: driving the probe through `forceHealthProbeRef` would
pass with the automatic path deleted, and the automatic path is where the bug
lived.

### 5.4 How the app ends, whichever way it is asked

Closing the window and being `SIGTERM`ed used to be the same event with two
outcomes. The window reaches Tauri's exit path, so `RunEvent::Exit` closed the
terminals and stopped the engine. A signal does not: `SIGTERM`'s default
disposition is to end the process where it stands, so the window went, the
terminals were left to whatever their PTY happened to do on EOF, and the engine
was left to notice that its parent had died. That last step is a backstop working
as designed — the engine watches the shell's pid and its own shutdown is bounded
(`04` §6.1) — but it is not this shell keeping its own promise, and it takes a
second and a half longer than doing it deliberately.

So `watch_shutdown_signals` puts the two paths on top of each other. A
`tokio::signal` stream per signal, spawned from `setup`, resolves when the signal
arrives and then does exactly what a closed window does: `release_children` closes
the terminals, stops the engine, and `AppHandle::exit(0)` produces the same
`RunEvent::Exit` the window-close path emits, so the window, the webview and the
compositor's idea of this surface all come down the ordinary route. tokio's
streams rather than `libc::signal` because what tokio installs writes to a pipe and
the work happens on a runtime thread — a signal handler may not touch the app's
state or call into Tauri at all.

Both paths can arrive for one exit, so `release_children` is claimed once
(`claim_shutdown`, over an `AtomicBool`): the second caller leaves rather than
paying a second bounded wait for a lock nobody holds and logging a second
"stopped" line for an engine that is already gone.

### 5.4.1 The engine is asked to stop, and only then killed

`SIGKILL` on the way out used to be the whole of it, on the grounds that a closing
window should not wait. That was right about the wait and wrong about the kill. A
killed engine writes nothing on its way out: a goal that was RUNNING stays RUNNING
in the database, with no event saying why, and the only repair is the *next* boot's
rescue (`04` §6.1) — a post-mortem, not a record, with a stretch of lying state in
between. The wait is bounded by the engine itself, so asking costs a few hundred
milliseconds in the ordinary case and a deadline in the worst one, and buys the
record at the moment it happens.

So `stop_engine` sends `SIGTERM`, then watches for the *exit* rather than for
silence, and escalates to `SIGKILL` only once the engine has outlasted the bound
it announced. Three things keep that honest, and the third is the one a person
found by quitting the app:

- **The bound is the engine's own.** `CODIFY_ENGINE token=… port=… hard_exit_s=6`
  carries `HARD_EXIT_GRACE_S` from the engine's boot handshake, so the shell is
  waiting out the deadline the engine promised rather than a number written down
  twice in two languages. A `hard_exit_s` that is not finite and positive is
  ignored — a bad bound must not stop the app booting — and the shell falls back
  to the engine's own default. Slack is added on top: the announcement is what the
  engine *intends*, not a guarantee it reaches, and a shell that waited exactly
  as long would kill an engine one scheduling hiccup before its own deadline,
  which is the very failure this arrangement exists to remove.
- **The wait is for the process, not the clock.** It ends the moment the engine is
  gone, and it stops early on the other signal too: the stdout reader sees the pipe
  close and clears the connection info, so an engine that crashed a moment ago is
  not waited for as a zombie answering signal 0. Closing the app after a crash is
  therefore still immediate.
- **The escalation is unconditional.** An engine that ignores `SIGTERM` is killed on
  a deadline, which is what `04` §6.1 promises. Nothing here is a reason to leave a
  process holding the port and the database.
- **The wait yields; it never blocks a thread.** The first version slept with
  `std::thread::sleep`, and the signal path runs this as a tokio task — so the
  sleeping worker starved the two tasks the wait depends on: the reaper that
  collects the child and the reader that sees its pipe close. A real engine asked
  this way left in 0.19s, sat unreaped as a zombie for the entire bound, and
  answered `kill(pid, 0)` the whole time; the shell waited 7.5s and then
  `SIGKILL`ed a process that had been gone since the first nap. `stop_engine` and
  its wait are therefore `async`, and `RunEvent::Exit` — which is not a future —
  drives them with `block_on` from the main thread, which is not a runtime worker.
  A test on a deliberately single-threaded runtime pins it, because a multi-threaded
  one hides a blocking wait behind a spare worker.

What this costs is a delay at exit, in the pathological case only, and the log says
so before it starts: `Asked the engine to stop (SIGTERM) — up to 7.5s before
SIGKILL`, then either `stopped inside its own bound` or `outlived its shutdown
bound — killed, so whatever it had not recorded is lost`.

Measured against a real engine with a goal forced to `RUNNING` and stopped by the
sequence above: gone in **0.19s**, the goal recorded `FAILED`, and its event log
carrying the reason — `the engine is shutting down while this goal was RUNNING`,
then an `engine_interrupted` error. The same run stopped the way the shell used to
stop it is still `RUNNING` afterwards, with no terminal event at all. That record
is the engine's, written in the lifespan's shutdown (`engine/app.py`); the backstop
and the shell's escalation both skip that block, which is exactly why they are the
second and third lines of defence and this is the first.

`SIGTERM` and `SIGINT` are the whole list. `SIGHUP` is deliberately absent: a
terminal that launched the app may have been started with it ignored, and a handler
installed over an inherited `SIG_IGN` would make closing that terminal kill an app
that was deliberately detached. What no handler can cover is a signal that cannot
be caught at all — `SIGKILL`, and a crash — which is the case the engine's own
watchdog and bound exist for. A keybind that quits the app pays §5.4.1's wait
before the window goes, which is why that section logs the bound it is waiting out.

### 5.5 The engine's stderr, where the window can read it

`Stdio::inherit` for the engine's stderr was right for a person with a terminal in
front of them and useless for a person using the app. The one diagnostic that
explains an engine is the engine's last word, and the bounded-shutdown backstop's
last word is exactly that: `[engine] shutdown unfinished after 6s — exiting
anyway`, printed and then `os._exit`ed. A hung websocket — a cancelled turn, a
closed laptop — took the engine away, and the app could say only that it was
offline, because the line explaining it had gone to a console nobody was looking
at.

So stderr is piped and read. A task drains it for the life of the process and
does two things with every line: keeps it in `engine_log::EngineLog` (200 lines,
500 characters each, 8 KiB of unterminated bytes tolerated — another process is
feeding this buffer, so it is bounded against both a long session and a process
that never sends a newline) and echoes it, byte for byte, so the terminal shows
exactly what `inherit` showed. Bytes are decoded only once a line is complete: a
multi-byte character split across two reads would otherwise become two
replacement characters. A panicking reader poisons the mutex; it does not
destroy the buffer.

`codify_engine_log` returns the tail, and the window asks for it **once, when it
notices the engine has stopped** — not while the engine is healthy, and not on a
timer. That is the only moment the tail has anything to say, and by then the
reader already holds everything the engine printed.

The window does not print the whole tail. `engineLog.ts` keeps the last few
*notable* lines — anything reading as a warning, error, traceback, or shutdown —
and then the last few lines of all, because a process that dies usually says why
last, and a panel that rendered nothing because nothing matched a keyword would be
the same blank panel this removed. The panel sits under the launcher's own
diagnosis when there is one, so the two read as cause and effect, and it is amber
because it is a quote rather than a second error.

Two things follow that are worth stating. The `engine_exited_problem` message
still names `python3 -m engine` and the directory: the tail is bounded, the whole
of it is not, and the tail is not there in the browser build at all — outside
Tauri there is no shell holding a pipe, so `fetchEngineStderr` answers `[]`. And
the grant is a named permission like every other command
(`allow-codify-engine-log`), so the command is unreachable until it is listed;
the browser module's ACL test fails the build's test leg when the two lists drift.

## 6. The restore gap, closed

Attaching an **existing** goal to a thread used to be client-side only. When
History restored a goal that predates conversations, the UI created a thread for
it so the restore landed in a visible tab — but the engine still had
`conversation_id` NULL on that row, so after a restart the goal read as its own
thread again. The link was the panel's, not the store's.

`PUT /goals/{id}/conversation` closes it, and the restore path calls it right
after creating the thread — best effort, with a failure surfaced rather than
silently swallowing the restore it was meant to file. The route goes through the
same `_check_link` as creation (unknown thread 404, cross-workspace 422) and
carries `extra: "forbid"`, so it is a single-purpose link move rather than a
general goal editor — the same reach-around invariant 2 closes on `POST /goals`.
Re-attaching **moves** the run: history is filed where the user just put it, not
permanently where it first landed, and the target thread's `updated_at` is
bumped so the panel orders it exactly as it does a fresh turn. The goal's
`version` is untouched — that counter guards status/step concurrency (`04` §2),
and filing a run somewhere must not invalidate a client's `expected_version`.

Asserted in `tests/test_conversations.py`: the headline test attaches a goal,
**closes and reopens the connection over the same file** — everything a restart
does — and reads the link back through `GET /goals/{id}` and
`GET /conversations/{id}/turns`; two more pin the refusals (404/422/422 +
extra-field) and the move-and-touch behaviour.

### 6.1 Reopening a thread, which used to open blank

The route the test above reads is also the one a client never called. The store
held what had streamed into it *this session*, and nothing asked the engine what a
thread had already said, so every thread was an empty pane after a reload — a user
whose history was in the database, in a tab, and invisible. History's restore
rehydrated a single goal, which is why the gap read as "restoring works, opening a
thread does not".

`hydrateThread` in `App.tsx` closes it, and the pieces are deliberately small:

* **The read.** `GET /conversations/{id}/turns` for the thread's turns — derived by
  the engine from the goals that answered them, so there is no second record to
  reconcile — and then `getGoal` + `getGoalEvents` per turn, which are the same
  events a live stream delivers. A restored turn and a live one are therefore one
  format: `threadHydration.turnMessages` mints the pair with the *goal id*
  (`user-<goal>`, `assistant-<goal>`), the same scheme `restoreGoal` uses, and
  `ChatTimeline` decides prose-vs-run-card from the goal either way.
* **When.** On the *visible* thread changing — clicked in the sidebar, restored with
  the tab, or arrived at by closing another — and gated on the engine answering,
  because a restored tab can name a thread before the handshake has set the port
  this client will use. Once per thread per session: the store is not persisted, so
  it is the whole truth about what has been shown, and a second pass can only
  repeat it. A *failed* read is deliberately not recorded, so the next opening
  retries instead of remembering the thread as empty.
* **Not duplicating what is already there.** This is the part that needed a rule
  rather than care: a live turn's message ids come from the clock
  (`user-${Date.now()}`) and a hydrated one's from the goal id, so nothing that
  compares ids can tell "the same turn, read twice" from "the same turn, twice".
  `turnsNeedingHydration` decides by **goal** instead, and it runs twice — once
  before the fetches (a thread reopened in one session should not spend a request
  per turn to learn nothing) and again inside the write, against the store as it is
  at that moment, because a turn dispatched since the first check is live and not
  history. `mergeThreadMessages` then owns only what ids *can* answer: a second
  read replaces its own pair instead of appending beside it. A turn that is still
  running re-subscribes to the very message it was hydrated from, so a thread whose
  last turn was in flight when the window closed goes on updating rather than
  freezing mid-answer. A rule by goal only holds once the message *has* a goal, and
  the optimistic message of a send used to learn its goal two awaits after the
  dispatch — so a read of the thread that landed in that window drew the first
  message of a brand-new thread twice (server: one goal; screen: two bubbles; after
  a reload: one — audit of 2026-09-29, M6). Two things close it, each pinned on its
  own in `ui/tests/newThreadOnce.test.ts`: a thread this very send just created is
  marked already read before its tab opens (it has no history, and the turn being
  dispatched is live), and the optimistic message claims its goal the moment the
  dispatch returns rather than after the follow-up read.

Asserted in `ui/tests/threadHydration.test.ts` — the decisions are pure and the
React half only fetches and applies them: the pair's shape and its goal-id scheme,
the user's bubble reading as the words typed rather than the engine's normalised
title, a turn already in the store not being fetched again, a re-read replacing
rather than appending, and the composition that keeps a live turn single while the
rest of its thread arrives.

## 7. The terminal and the browser

### 7.1 Terminal — built

`src-tauri/src/terminal.rs`. A user-driven PTY, and the boundary is explicit in
the module's own docs:

- **The shell owns it, not the engine.** `tests/test_no_unguarded_spawns.py`
  freezes every spawn in `engine/`, `benchmarks/` and `scripts/`, and now has a
  second, lexical half over `src-tauri/src/**/*.rs` (`RUST_GUARDED_SPAWN_SITES`)
  because the shell always started processes and a freeze scoped to the engine's
  languages is a freeze the shell walks around. See `docs/07` §2.1.
- **It never calls `SandboxService.run_command`.** That is the agent's privileged
  path and docs/00 §6.6 says only verifier-proposed argv reaches it. A user
  typing at a prompt is a different authority, and mixing the two breaks the
  invariant.
- **argv is `$SHELL`, never a request.** No caller can name a program.
- **`cwd` is pinned by `pin_cwd`.** The client sends a workspace *id*;
  `workspace_root_for` asks the engine what directory that is; `pin_cwd` refuses
  anything that is not an existing absolute directory. A caller that could send a
  path could point a user's shell at anywhere on the machine.
- **Closing reaps.** `codify_terminal_close` kills the shell, and so does the
  app's exit handler — a terminal left running after the window goes is a stray
  process holding the workspace, the same defect the engine kill prevents. The
  kill escalates: SIGHUP first, then a hard kill for a shell that ignores it,
  and the exit status is collected either way rather than leaving a zombie.

Commands: `codify_terminal_open`, `codify_terminal_write`,
`codify_terminal_resize`, `codify_terminal_close`. Events: `terminal-output`,
`terminal-exit`. Thirteen Rust tests pin the refusals, the reaping and the read
path; the freeze is mutation-tested — a rogue `Command::new(...).spawn()` in
`src-tauri/src/` fails `tests/test_no_unguarded_spawns.py`.

The reader thread is `pump_output`, taken out of `open` so it needs no
`AppHandle`: two of those tests drive a **real PTY** through it (a shell printing
non-ASCII text, and `cat` echoing back a line that was written), which is the
whole path from a shell's first byte to the text the `terminal-output` event
carries. It decodes with `Utf8Chunker`, not per-read `from_utf8_lossy`: a read
ends wherever it ends, and a character whose bytes straddle two reads used to
arrive as two replacement characters. The unfinished tail is held for the next
read; bytes that are actually invalid are replaced at once so garbage cannot
stall the stream.

#### The pane

`ui/src/components/TerminalPane.tsx` rendering
[`@xterm/xterm`](https://xtermjs.org) with `@xterm/addon-fit`, driven by
`ui/src/terminalModel.ts` and four calls in `ui/src/api.ts`. `App.tsx` renders
it instead of the transcript and composer when the active tab is a terminal tab,
and the header's Terminal button opens one in the selected workspace.

Decisions, and why:

| Decision | Why |
|---|---|
| **xterm is imported inside the effect** | It needs a real DOM and a measured font. A module-scope import would make the component unimportable by `node --test` — which is the only harness the UI has — so the pane's markup could never be rendered and the file could only be checked by reading its source. |
| **Its stylesheet is imported by `main.tsx`, not the component** | A CSS import is a bundler statement node cannot parse, so it would put the component back out of the harness's reach. |
| **Output never touches React state** | A PTY emits faster than a frame — `ls` in a large directory, a build, `yes`. Every chunk that went through `useState` would re-render the pane and everything above it. The subscription writes **straight into the xterm instance**; the only state in the component is `failed`, which changes twice in a terminal's life. |
| **The subscription goes up *before* xterm exists** | `terminal::open` spawns a reader thread that emits immediately, and the reply naming the terminal has already lost the race with the first prompt by the time a dynamic import finishes. A listener set up after xterm loses it, and a terminal that opens blank reads as broken rather than slow. Chunks land in an `OutputBuffer` keyed by id and this terminal's are flushed the moment the renderer exists. |
| **The PTY is opened at 80x24 and resized after** | A pane measures itself *after* it exists, so the first size is a constant. A shell redraws its prompt when it is resized, and the pane's `FitAddon` supplies the real grid within a frame. |
| **A measurement that yields no cells is refused, not clamped** | A hidden or not-yet-laid-out pane has no cells. Forwarding that gives the PTY a zero-cell screen and the shell re-lays out into it; holding the last good size costs nothing. |
| **A resize is sent only when the grid actually moved** | A window drag fires a `ResizeObserver` per frame, and each one is an IPC round trip to a process that re-wraps its scrollback. |
| **Keystrokes are not intercepted** | Every key belongs to whatever the shell drew — Ctrl-C, arrows, a bracketed-paste sequence, a tmux prefix. xterm turns a key into exactly the bytes a real terminal would send. The keys the *window* claims are handled one layer up, in `App.tsx`, before they reach the pane. |
| **An exited shell is written into the scrollback, not only into a banner** | Scrolling up after a command fails is the normal way to read a failure, and a banner outside the terminal is not in that scrollback. The tab stays open for the same reason; closing it reaps what is left. |
| **A terminal tab is named by the shell** | `terminal.rs` numbers its own sessions `term-1`, `term-2`, and the pane needs that exact string for every write, resize and close. One id, not a tab id with the PTY's beside it. |
| **A resize carries the pane's own id** | The pane is the only party that knows which PTY it is drawing. `App.handleTerminalResize` used to read the id out of `pendingTerminal`, which held the *most recently opened* terminal — so with two shells up, dragging the window resized the other one, and a terminal restored from scrollback was never resized at all. |

Two asymmetries with the browser pane worth naming, because they look like
inconsistencies and are not:

- **Closing a terminal tab calls the shell unconditionally; a browser tab's
  close is guarded on `tab.url`.** `terminal::close` returns `Ok(())` for an id
  it does not hold, so a shell that has already exited is the documented quiet
  path. `codify_browser_close` *refuses* a tab with no window, so the guard is
  there to keep that refusal off the user's screen.
- **The terminal is drawn in the main window; the browser is not.** §7.3.

`terminalModel.test.ts` covers the grid, the de-dupe and the buffer;
`terminalHistory.test.ts` covers the scrollback below; `terminalBuffer.test.ts`
covers the background backlog; `terminalPane.test.ts`
covers the markup and freezes the decisions a static render cannot reach.
`terminalEndToEnd.test.ts` is the one that sees the **relay** rather than a part
of it: it mounts `App` itself over a fake shell, opens two shells, runs a build
in the background one, switches tabs, and reads the result out of a real xterm.
The pieces above can each be right while the wiring between them is backwards —
and the double-filing and dropped-prompt defects above were both invisible to
all of them. **The
PTY itself is Rust's** and its tests live in `src-tauri/src/terminal.rs` — the
UI suite never spawns a shell, and a terminal that a user has to trust is one
whose refusals are tested next to the code that refuses.

#### Scrollback that outlives the tab

Closing a terminal tab reaps the shell, so the text the user was reading went
with it, and reopening the tab gave a bare prompt. `ui/src/terminalHistory.ts`
keeps a **bounded tail of the output stream, per workspace, in memory for the
session**, and a reopened pane writes it back into a fresh xterm before the live
shell says anything.

**The record keeps every byte; the replay does not keep every line.** A line
with no newline in it is trimmed when a pane *writes the tail back*, not when
the tail is appended to, because a shell's last word is almost always such a
line: the prompt, or half a command the user is still typing. Trimming at
append time read as tidiness and was the opposite — every session's record
stopped one line short of the truth and those bytes were gone from every store
the app has, not merely unshown. The half-line is a display problem, and
`replayFor` is where it is solved.

#### Output a terminal earns while no pane displays it

A pane mounts only while its tab is on screen (the active tab, or one of a split's two,
§12), and the shell behind a tab that is not never stops — so every byte a background build printed used to be emitted
to nobody and dropped, gone from the live pane on return and from the workspace
scrollback too, because the scrollback append lived in the same unmounting
listener. `ui/src/terminalBuffer.ts` is the fix, and it is a **separation of
recording from rendering**: rendering stays to what is on screen (the active tab; in a
split, its two panes, each owning its own terminal), the same decision the browser pane made;
recording moves to a subscription at
app scope, alive for as long as the app is.

The rule that keeps every byte displayed once and filed once is **ownership**.
A pane claims its terminal's id on mount (taking what the shell said in its
absence) and releases it on unmount; the recorder holds a chunk only when
nobody owns the terminal that said it, and leaves an owned terminal's chunk to
its pane, which displays and files it. **A pane files its own terminal's bytes
and nobody else's** — the pane for a chunk it displays, the store for the rest.
It used to file every chunk that arrived, before the pane check, on the theory
that anything a workspace's terminals said belongs to that workspace; that was
true when the pane was the only listener and became a duplicate the moment the
recorder existed, because the *sibling* pane then filed this terminal's
background build into the same record the backlog replays from, and the
returning pane printed every line of that build twice. An unowned terminal is
in one of three states, each with one answer: **backlog** — its tab exists but
its pane does not, so chunks are held bounded (64 KiB, the same bound the
workspace tail uses, no line trim, because a shell's warm-up is a prompt with
no newline) and replayed by the next pane to claim it; **abandoned** — the tab
was closed with the pane unmounted, and `handleCloseTab` retires the backlog
into the workspace's scrollback, honouring the guarantee above; **exit while
unowned** — the recorder files the backlog plus an exit marker on
`terminal-exit`, because a marker that only ever reached a mounted pane would
leave a background shell's record looking open.

Ordering on return is chronological and therefore display order: the workspace
replay (earlier sessions, under its seam), then a **resume seam** — *output
missed while this pane was away* — then the backlog, then the live stream. The
backlog continues the tail rather than replacing it: everything a mounted pane
displayed was already filed as it was displayed, so the backlog is strictly
newer, and without the seam the gap would read as one unbroken stream. Claiming
is also the moment the backlog is **filed**: the store deleted those bytes when
the pane took them, so the pane writing them into xterm is the one copy that
exists, and a shell's first prompt is nearly always backlog — the PTY prints
before the tab does, so the recorder catches it while no pane owns it. Filing
only what arrived *after* the claim left that prompt on screen and absent from
the workspace record, so the next pane in the workspace restored everything
except the start of the session.

**The badge.** `recordOutput` returns whether it kept a chunk — exactly the
moments a background terminal said something — and App turns those into an
unread set the tab strip renders as a dot next to the busy dot, with *new
output* in the tab's accessible name. Two guards keep it honest: a kept chunk
on the *active* tab (the claim window, between a pane mounting and its claim
landing) is not unread, because badging the tab the user is reading would make
the badge lie about attention; and the badge clears when the tab is shown,
dies with its tab, and survives a *background* exit — a shell that finished
unwatched is the one moment the badge matters most. What clears is an exit the
user watched (the owned case). The set lives in App, not the store: which tabs
the user has looked at is a view concern, and the store's durable answer — the
backlog — is already where the badge's evidence is.

| Decision | Why |
|---|---|
| **Record at the app, render in the pane** | A recorder keyed to any pane's mount has exactly the outage the feature exists to prevent. App scope is also the only scope that survives a tab switch, which is the common case. |
| **Ownership, not timestamps** | Deciding "was this chunk displayed?" from arrival times re-derives React's mount order and gets it wrong at the edges; a claim/release pair is the mount order, stated by the component that knows it. |
| **The backlog is bounded like the tail and has no line trim** | Two bounds would invite the question of which applies. The line trim exists for replay of a *stored* tail; a returning pane wants the prompt the shell is mid-writing, which trimming would swallow. |
| **A pane files only its own terminal's bytes** | One owner per chunk: the pane for what it displays, the store for the rest. Filing a sibling terminal's chunk is a second filer, and a second filer is how a background build ends up on screen twice. |
| **Merge at session boundaries only** | Chunks the pane displayed are already in the tail and never entered the backlog; chunks in the backlog were never filed, and the pane that claims them files them as it replays them. Disjoint by construction, so the merge is a concatenation and no byte is counted twice. |
| **Background exit files the marker itself** | The pane's marker is display-only and never reaches the tail; a background shell's record must end visibly, or it reads as open. |
| **The unread badge survives a background exit** | A shell that finished unwatched is the moment the user most needs pointing at the tab. What clears the badge is showing the tab, closing it, or watching the exit happen — never the exit itself. |

| Decision | Why |
|---|---|
| **The byte stream, not the screen** | Serializing xterm's own buffer with `addon-serialize` is a closer match and captures a full-screen program the way the user last saw it. The stream wins because it needs no extra dependency, is always current rather than captured at a chosen moment, and survives xterm being disposed at all — which a buffer snapshot only does if something remembered to take it. |
| **Per workspace, not per terminal** | The question being asked is "what was I doing *here*", and "here" is the workspace. Two terminal tabs in one workspace share a tail, so a second tab shows the first one's session. That is the feature, not a leak. |
| **This session only — never disk, `localStorage`, or the engine** | The content is whatever the user typed and whatever commands printed. `docs/09` §2 is where this project keeps its reasoning about what belongs in `~/.codify`, and that reasoning is not settled, so nothing is written. A durable version is a real decision, not a follow-up: a file in `CODIFY_HOME` written by `terminal.rs`, bounded and garbage-collectable, which is also where it could be *deleted* on request. `localStorage` would be neither. |
| **Bounded at 64 KiB, trimmed on every append** | Trimming on read instead means a terminal left open through a long build grows without limit for as long as the app is open. What survives is the *end* of the log: nobody scrolls back a megabyte to find the line they wanted. |
| **A cut inside a surrogate pair is dropped** | A JS string is UTF-16 and the limit is applied by slicing, so a cut between the halves of an emoji leaves a lone high surrogate. It renders as a replacement character at the top of every restore after, because the stored tail is the cut one. |
| **A cut mid-line is dropped** | `terminal.rs` reads in 4 KiB chunks, so a tail almost always stops mid-line. Replaying half a command above a new prompt reads as corruption rather than as a boundary. |
| **A seam line is drawn in front of it** | Without one, a build that failed twenty minutes ago comes back looking like something the shell just printed. Dim, on its own line, and it says *earlier session in this workspace*. |
| **Replayed before the live prompt** | The restored bytes are older, so writing the new shell's prompt above them reads as the new shell quoting the old one. |
| **The tab remembers its own workspace** | `pin_cwd` decided the shell's directory at open time. Reading the composer's current selection would change what that shell appears to be in, and which earlier session a reopened pane restores. |

**The honest limit: a restored line re-wraps.** The bytes were produced at the old
pane's width and are written into a terminal of whatever the new one is, so a log
read at 120 columns and reopened at 80 comes back with different line breaks.
Recording the width and refusing to replay at a different one would be a nicer
answer and a worse feature: most of the time the user closed the tab *because*
the window changed shape, and refusing to show them anything would be the wrong
answer to that.

There is deliberately **no Clear control**, so `clearTerminalHistory` has no call
site. It exists because a bounded store needs a way to be emptied that is not
"quit the app", and because a function with no caller is API waiting to be used
wrongly. When there is a Clear action it is one call, and the test for it is
already written.

### 7.2 Browser — built (shell layer)

`src-tauri/src/browser/`, `src-tauri/capabilities/browser.json`,
`src-tauri/permissions/shell.json`, commands
`codify_browser_open` / `codify_browser_navigate` / `codify_browser_focus` /
`codify_browser_resize` / `codify_browser_close` /
`codify_browser_devtools_open` / `codify_browser_devtools_close` /
`codify_browser_devtools_state` / `codify_browser_devtools_available` —
exactly those nine, and the list is not a memory: it is checked against
`invoke_handler![]` in both directions by
`the_docs_name_the_browser_commands_that_exist`. A tenth command, in this list
once, handed a page's current URL to the operating system's own opener; §7.3
says why it is gone, and naming it here would only put a dead command back
into circulation.

One embedded **page** per browser tab, labelled `browser-<tab>` — a child
webview of the main window, not a window of its own. This is the first surface
in Codify that renders untrusted content, and it now renders it *inside* the
window the app's own UI runs in. Five layers of isolation, and the Rust tests
in `browser/tests.rs` pin them:

**1. An empty capability set.** `capabilities/browser.json` covers
`browser-*` with an empty `permissions` list — no `invoke`, no event listeners,
no filesystem, no shell. The file exists to declare the posture in the open: a
label no capability mentions would be equally safe and much easier to widen by
accident. The test `the_browser_capability_set_is_empty` parses the committed
capability files — it does not assume them — and fails if any permission ever
reaches a browser label, whether through `browser.json` itself, a widened `*`
window/webview pattern in any other capability, or an inline capability
`tauri.conf.json` gains (`tauri_conf_inlines_nothing_for_browser_webviews`
closes that hole). It also requires `browser.json` to exist *and* to match a
label built by `webview_label`, so renaming the prefix without the capability
(or the reverse) fails instead of silently producing webviews outside their
own declaration.

**2. A loopback URL guard, checked before navigation and on every navigation.**
`navigation_allowed` accepts only `http`/`https` and refuses `localhost`,
`*.localhost` (RFC 6761, which covers `tauri.localhost` on Windows), all of
127/8, `::1`, `0.0.0.0`, `::`, IPv4-mapped IPv6 forms like
`[::ffff:127.0.0.1]`, and the canonicalised spellings the WHATWG URL parser
reduces to them — `http://2130706433/`, `http://0x7f000001/` and
`http://0177.0.0.1/` *are* 127.0.0.1, and `http://0/` is 0.0.0.0. It is
enforced twice: `open`/`navigate` refuse before anything exists, and
`on_navigation` is asked about every navigation the webview makes —
page-initiated, redirect, or scripted — so no caller routes around the first
check. This is `docs/03` §1.2's reasoning inverted: the engine's guard
*requires* loopback for a provider `base_url`, the browser's *refuses* it,
because untrusted content must not be able to sit on the origin of the app's
own services. The tables are `loopback_and_non_web_schemes_are_refused` and
`ordinary_sites_are_allowed`.

**3. An app ACL manifest — which is what makes layers 1 and 2 mean
something.** Tauri only enforces capabilities against application commands
(`codify_*`) when the app defines an ACL manifest. With none defined, a
**local**-origin invoke of any `codify_*` command is allowed with no capability
check at all — local meaning relative to the `devUrl` or the app's own assets
(`tauri://localhost`, `http://tauri.localhost`). That was the whole reason the
main window worked on `core:default` alone, and it meant layer 1 had nothing to
deny with: a browser webview was kept out by its *origin*, and only by its
origin. `src-tauri/permissions/shell.json` is now the manifest — twenty
`allow-codify-*` permissions, one per command, collected into a `shell` set.
Two properties matter more than the count:

- **Named, not a wildcard.** A command added to `invoke_handler!` without a
  permission is *stripped from the handler* by `filter_unused_commands` at
  compile time, and the UI's invoke fails at runtime as "command not found"
  with no compile error naming the cause. `the_grant_is_exactly_the_commands_the_handler_defines`
  parses both lists and fails on any drift, in either direction.
- **Referenced exactly once, by the `main` capability.** Unprefixed permission
  references resolve against the app manifest, so each is another place the
  grant can widen. `the_app_manifest_is_referenced_by_exactly_one_capability`
  pins that to `default.json` — whose `windows` is `["main"]` — and
  `the_grant_reaches_main_and_no_browser_label` then resolves the grant through
  Tauri and requires every command to match `main` and no `browser-*` label, in
  `ExecutionContext::Local`. `the_app_acl_manifest_closes_the_local_origin_bypass`
  asserts the flag itself (`has_app_acl`), using tauri-build's own condition for
  deciding that a manifest exists.

**4. The origin rule, now defence in depth rather than the boundary.** Remote
origins never had a pass: Tauri rejects a remote-origin invoke unless a
capability with an explicit `remote` URL pattern resolved it, and this app
configures none. So layer 2's loopback refusal no longer carries the guarantee
on its own — a page that reached a local origin would gain nothing (layer 3) —
but the two checks fail independently, and the guard is what stops untrusted
content from being seated where the app's own scripts run at all:
`http://localhost:5173` is the dev UI, and `tauri://localhost` is the app
itself.

**What this does not claim.** The guard is a navigation policy, not a network
filter: subresource requests (images, iframes, `fetch`) to loopback are not
intercepted, and they never needed to be — every engine route requires the
bearer token (`docs/00` §6.3), and the token reaches a webview only through a
`codify_get_engine_info` invoke that a `browser-*` label cannot make. A hostname
that *resolves* to loopback through DNS (`127.0.0.1.nip.io`) passes a lexical
guard by construction; that limit is pinned in `ordinary_sites_are_allowed` so
it cannot later be "fixed" into a DNS lookup inside the navigation callback. The
capability set is the boundary that does not care what the host resolves to, and
the app ACL manifest is what makes that set mean anything. And the manifest
gates Tauri's `invoke` surface and nothing else: the engine subprocess, the
PTYs and the webview windows are all created from Rust.

**The close signal — there is none, on purpose.** An earlier build opened each
browser tab as a separate OS window, which the user could close with the
window's own button without the tab strip's knowledge; that build emitted
`browser-window-closed` (carrying the tab id) from a `Destroyed` handler so the
strip could follow. The pane is now a **child webview** of the main window
(§7.2). A child has no window events of its own — `WebviewEvent` carries
drag-and-drop and nothing else (checked against tauri 2.11.6) — so the tab strip
is the only closer, and the event, its handler and its listener are gone with
the window they announced. Closing goes one way: the strip's button, Ctrl+W and the
pane all reach `codify_browser_close` through `handleCloseTab` (§7.3), the one
seam, so there are not two ways to orphan a running page.

What can still end a page without the strip asking is a load failure (layer 4 of
the shell's browser guard) and a WebKit web-process crash, which nothing
observable reports; the docs say so rather than pretend otherwise.

Pinned by `the_ui_listens_for_the_events_this_module_emits` (`browser/tests.rs`), which
reads `ui/src/shellEvents.ts` and fails if the two ends of an event name drift —
no type system spans a Rust constant and a TypeScript string — and which also
fails if `browser-window-closed` reappears in the UI, since a listener for an
event nobody emits is code pretending to handle something. On the UI side,
`ui/tests/shellEvents.test.ts` pins that a malformed payload names no tab at all,
and that subscribing outside the desktop shell is a no-op rather than a throw.

### 7.3 The browser pane — built

`ui/src/components/BrowserPane.tsx`, driven by `ui/src/browserHistory.ts`,
`ui/src/browserDispatch.ts`, `ui/src/tabs.ts` and five calls in `ui/src/api.ts`
(`openBrowserWebview` → `codify_browser_open`, `navigateBrowserWebview` →
`codify_browser_navigate`, `focusBrowserWebview` → `codify_browser_focus`,
`resizeBrowserWebviews` → `codify_browser_resize`, `closeBrowserWebview` →
`codify_browser_close`).
`App.tsx` renders the pane **instead of** the transcript and composer when the
active tab is a browser tab, and the header's Browser button opens a tab with no
address.

**The pane is the address bar *and* the viewport.** The page is a child webview
of the main window (§7.2), seated over the pane's measured content rectangle:
the pane reports its geometry (`onBounds`, logical pixels, from a
ResizeObserver on the content div), the shell places the native view exactly
there, and the coalesced `codify_browser_resize` keeps it there through every
layout change.

**…and on Linux, "the shell places it" was not true for as long as the pane has
existed.** This is the one paragraph of §7.3 that a reader should not take on
trust, because it was wrong, it looked exactly like a rendering bug, and the
whole smoke suite was green through it:

- Tauri builds **every** webview into the window's default `gtk::Box` —
  `tauri-runtime-wry`'s `WebviewKind::WindowChild => build_gtk(default_vbox())`
  — including the app's own UI and every page.
- wry's `add_to_container` takes the `GtkBox` branch and does
  `pack_start(webview, true, true, 0)`: expand and fill, **with the bounds it
  was handed never read**. Its `GtkFixed` branch is the one that honours
  `put(webview, x, y)`, and `set_bounds` afterwards is guarded by
  `is_in_fixed_parent`, which wry records at *creation* — so on Linux both the
  position and every later `resize` were silent no-ops.
- A `GtkBox` lays children out in a line and cannot overlap them, so two
  expanding children in one window is a window split in half. Users saw exactly
  that: the browser opening over **half** of Codify, and then over all of it.
- The paint smoke could not see any of it, because it asked for `0,0 800×600` —
  which is what "the box gave the page the whole window" already looks like.

**The fix is the container the toolkit needs.** Tauri exposes no way to create a
webview into a container of our choosing (its builder has no `build_gtk`, its
`Webview` no `gtk_widget`), so `browser/page_layer.rs` does the
container work itself from `Window::default_vbox()`: on the first page, the
app's webview becomes the main child of a `gtk::Overlay` this crate owns, every
page then lives in a `gtk::Fixed` layered above it, and placement is driven by
hand — `fixed.move_` plus `set_size_request` on every `open` and every
`resize`, with nothing calling wry's `set_bounds`, which would stay a no-op on
this platform however the tree is arranged. The layer is built lazily, so a user
who never opens a browser tab has a window that was never restructured; the
whole module is `cfg`-gated to exactly the platforms whose toolkit behaves this
way, and every other platform keeps the native `set_bounds` path.

**…and that container is built on GTK's thread, which is not the thread the
command runs on.** `codify_browser_open` and its siblings are `async` commands,
so Tauri runs them on a tokio worker, and GTK asserts on every widget call —
its assert reads "GTK may only be used from the main thread". The first real
click on a browser tab died on that assert, before the page existed, and the
paint smoke could not see it: the smoke seats its page from `setup`, on the main
thread, and a user's click does not. So `page_layer` has exactly one door,
`on_main`, and every entry point (`prepare`, `adopt`, `place`, `release`,
`geometry`) goes through it. It asks the toolkit's own question before touching
a widget — `gtk::glib::MainContext::default().is_owner()`: tao initialises GTK
on the main thread and `gtk::init` **acquires and leaks** the default main
context (gtk-rs#186), so that thread is its owner for the life of the process.
When this already is that thread the work runs inline, because hopping from the
main thread would queue a closure behind the call that is waiting for it;
otherwise it goes through `Window::run_on_main_thread` and blocks for the
answer, the same shape Tauri itself uses for `add_child`.
`browser::tests::the_layer_touches_gtk_only_from_the_thread_gtk_allows` names
the regression and fails the build if a door stops asking.

**And the smoke now measures what it never did.** It seats its page at
`24,24 560×420` — deliberately not the origin and not the window's size, so "the
toolkit discarded the request and used the whole window" cannot coincide with a
pass — and 700ms later reads the widget's **real GTK allocation** and fails the
run if it is not that rectangle. Measured on this machine: `embed-smoke: page
geometry (24, 24, 560, 420) — the page is where the pane asked`. The control run
is the part that matters: with the placement call removed, the smoke exits **1**
with `browser page "browser-smoke" is not in the page layer`, so the check bites
rather than describing whatever it finds. The read is taken from a spawned task
— a tokio worker, the thread a click arrives on — so the smoke exercises the
thread the command does, and a GTK assert inside it is caught and reported as a
named `FAILED` instead of leaving the run to finish and report a green that says
nothing about where the page is. Every other leg of that smoke measures
the page's *content* — a page covering the entire app passes all of them, which
is how this stayed hidden. This replaced a separate OS window, which the docs here used to
defend — and the defence was real, which is why it is worth quoting against
itself:

> Embedding the page in the main window means building a **child** webview. A
> child webview reports its *parent's* window label, and `resolve_access` in
> Tauri matches `windows` patterns against that window label with an **or**
> against the webview's own. `capabilities/default.json` grants on
> `windows: ["main"]`, so an embedded page would resolve the entire `codify_*`
> grant — `codify_get_engine_info`, and with it the boot token. Making it safe
> is not hard (re-point the capability at `webviews: ["main"]`, and
> `capabilities/browser.json` at `webviews: ["browser-*"]`), but it re-opens the
> isolation argument of §7.2, and a child webview has no window event, so the
> close signal of §7.2 would have to be rebuilt as well. That is a change worth
> making deliberately; it is not a side effect of adding an address bar.

That is what shipped, deliberately, and each cost named there was paid:

- **The grant was re-pointed, in the same change.** `default.json` matches on
  `webviews: ["main"]` and `browser.json` on `webviews: ["browser-*"]`; both
  files match on `webviews` and on nothing else, and
  `browser::tests::no_capability_grants_through_a_window_pattern` fails the build
  if any capability that grants anything ever matches through a `windows`
  pattern again — the shape that would let a child inherit the grant. The
  reach test now asserts `main` matches through the *webview* label.
- **The close signal was not rebuilt, because the problem dissolved.** A child
  webview cannot close itself and has no window events, so there is nothing to
  announce: the strip is the only closer, and the separate-window build's
  `browser-window-closed` event is gone from both ends (the Rust test reading
  `shellEvents.ts` fails if the name comes back). A page can still ask for
  something — a popup — and that is the one event the shell now emits: refused
  at the webview, announced to the UI, opened by the user as a real tab
  through the same guarded path as a typed address.

**Why embed at all: the user asked for the browser to be in the app, and the
separate window was also the certificate-error story.** A page that failed TLS
verification painted WebKit's interstitial in a window of its own, detached
from the tab that asked for it — and the first report of this app's browser
was exactly that: `google.com` opening "a separate window with some
certificate error". Embedded, the same interstitial paints *in the tab*, and
the failure has no code path of ours to lie about: wry 0.55.1's
`PageLoadEvent` is `Started | Finished` and nothing else (checked against the
vendored source), so the shell is never told of a failed load and never
pretends to handle one. The certificate question is answered where it lives —
the system trust store, which is what WebKitGTK verifies against — and never
by a flag on this app: there is no bypass, offered or hidden.

**The page reports itself, and the strip says so.** Three shell events carry a
page's life to the UI: `browser-page-loading` and `browser-page-loaded` (from
tauri's `on_page_load`, the payload carrying the address being loaded) and
`browser-page-titled` (from `on_document_title_changed`). The tab strip shows
two facts from them: a **loading marker** — a pulse, not a progress bar,
because the runtime has no load-fraction event and a fake percentage would be
a lie with a keyframe — and the **page's own title**, which replaces the host
the moment the page names itself. The load events also keep the address bar
honest: the *live* address lands in `Tab.url`, so a redirect is what the bar
says, while the history stack keeps the visit the user actually made.

The loading marker is **bounded on purpose**. The runtime has no failed-load
event (wry 0.55.1's `PageLoadEvent` is `Started | Finished` and nothing else,
checked against the vendored source), so "loading" means "Started, no Finished
yet" — and a page that dies into WebKit's TLS interstitial never sends
Finished. The marker expires on a timer (`LOADING_TIMEOUT_MS`, 15s, pinned in
`ui/tests/browserPageState.test.ts`); the interstitial is the story, not a
spinner that lies forever. The decisions live in `ui/src/browserPageState.ts`
(`node --test`-able, like every pure module here); a tab that is gone drops
the fact by shape, and both reducers answer with the *same* state object when
nothing matched, so a stale event cannot re-render anything.

**One escape, and it does not widen the page.** The pane carries a **DevTools**
control and nothing else that leaves. The inspector is a shell-side surface
opened *over* the webview — it grants the page nothing, and the crate builds
with tauri's `devtools` feature so it exists in release builds too; the
pane's control states the inspector's real state (`aria-pressed` from
`codify_browser_devtools_state`, confirmed — not the press).

There used to be a second one. **Open in system browser** handed the page's
**current** URL to the OS's own opener (`open::that_detached`), on the
reasonable argument that some page needs the profile, extensions and
certificate trust a real browser carries. It is gone, and the reason is the
shape of the answer rather than the quality of the argument: a page that can
be opened outside Codify is a page whose session, cookies and credentials
leave with it, and the answer to "this site renders badly in an embedded
view" is to make the embedded view handle the site. **Every website Codify
can open is a tab in Codify, a popup is a tab in Codify, and there is no
code in the shell that could hand a URL to anything else** — which is an
absence, and absences need pinning: the browser module's
`the_only_escape_is_the_inspector_and_the_module_spawns_nothing` asserts
`open_external`, `open::that_detached` and `Command::new` are *absent* (the
inverse of the assertion that used to require them), and
`the_open_crate_is_not_a_dependency_and_nothing_reaches_for_it` holds the
stronger claim the first one could not — that no `open` dependency is declared
in `Cargo.toml` **and** no live code in any file under `src/` reaches for one.
That gap was real rather than theoretical: the command was gone, the test was
green, and `open = "5"` sat in the manifest outliving its last caller, pulling
`windows-sys` and `is-wsl` into a build graph that had no user for them. An
unused dependency is not an error to any leg of the gate, so nothing said so
until something looked.
`the_ui_knows_the_escape_commands` reads `ui/src/api.ts` so the two ends
cannot drift, and `ui/tests/browserPane.test.ts` asserts the pane renders no
such control even for a page whose address it knows.

#### What the embed actually renders, and what the smoke can see

`make smoke-embed --url <url>` seats a real page in a real webview on a real
display. It now reports **two** things, because one of them was answering the
wrong question: a first paint, and what the page says it is showing
(`SMOKE_PROBE_SCRIPT`, reported over the title and printed as
`embed-smoke: report …`). A run ends when it has both, or at 20s.

The second half exists because a paint verdict is worthless for a *site*. Every
page composites its first frame in about a second — including one that is
showing the user an error. Measured on this checkout before the probe existed:
`www.youtube.com`, `accounts.google.com` and a GitHub issue each reported
`painted` in 1.2–3.6s, which said nothing at all about whether any of them
worked.

| Site | Reports | Verdict |
|---|---|---|
| `github.com/tauri-apps/tauri/issues/1` | real page title, 3469 chars, 250 subresources, `readyState=interactive` | **works** |
| `www.reddit.com/` | real page title, 9060 chars, 74 scripts, 113 subresources, `readyState=complete` | **works** |
| `accounts.google.com` | `Sign in - Google Accounts`, 135 chars, **2 frames** | **unmeasured** — the probe cannot see inside a cross-origin frame |
| `www.youtube.com/` | between **6 and 2090** chars across runs, 2 frames | **unreliable** |
| `www.youtube.com/watch?v=…` | **0 chars, no title, 0 media**, `readyState=loading` after 842ms | **the load stalls** |

Three of those five rows are not "broken", and the difference matters more than
any of them:

- **`content-behind-an-iframe` is a limit of the probe, not a verdict on the
  site.** `innerText` reaches the top document only, so a sign-in flow that puts
  its form in a cross-origin frame renders perfectly and reads here as 135
  characters. Google did **not** serve its embedded-OAuth refusal on this
  measurement — the refusal markers (`may not be secure` and friends) are
  matched and did not fire — so nothing here says Google blocks the embed.
  Whether a sign-in *completes* is not measured at all, and it is the thing a
  user would actually care about.
- **The YouTube variance is the finding.** The same URL reported 6 characters in
  one run and 2090 in the next. A page that renders differently on two
  consecutive runs is not a page whose rendering has been diagnosed, and no
  `canPlayType` reading settles it: this WebKit answered `probably` for both
  H.264 and AAC on a machine where **no `<video>` element ever appeared** on any
  YouTube page. `canPlayType` is an engine-level claim about the MIME database,
  not evidence that a frame of video decodes — proving playback needs a real
  `play()` and a `timeupdate`, which no paint smoke can supply.
- **What is still unknown, stated as unknown.** Why a YouTube watch page
  renders nothing: no subresource failure was captured, no console output, and
  no network record. The candidates worth *measuring* are a blocked or failed
  subresource, WebKitGTK's default cookie policy (no third-party cookies, which
  would explain a sign-in flow stalling), and the user-agent string — and each
  is a hypothesis, not a fix. Changing any of them on this evidence would be a
  guess dressed as a repair, which is the one thing this table exists to stop.
  Nothing here is routed anywhere: the hatch that would have sent a failing
  user to another browser was removed above, and the honest answer to "this site
  does not work in the embed" is this sentence rather than another app.

#### What a blank page is made of

The table above says "renders nothing" for a page that stopped progressing. The
smoke now captures the **mechanism** alongside the symptom, because the two are
different bugs with different fixes, and a symptom sends the reader to the
wrong layer. The page is asked, from the first script and before any of its own
code runs: which subresources **failed** (a capture-phase `error` listener —
a resource that 404s fires on the element, and a bubbling listener on `window`
hears nothing), what the page **threw** (`unhandledrejection`, which is how a
modern SPA fails: no error event, no failed resource, a blank page), what it
**logged** (`console.warn`/`error`, patched before the page runs), how many
subresources **completed**, its `readyState`, and its **user agent**.

Run against a YouTube watch page, that is the whole answer:

```
readyState=loading after 842ms, 12 script(s), 0 stylesheet(s), 8 subresource(s) completed
ERROR: WebKit encountered an internal error … internallyFailedLoadTimerFired()
```

**The page stops making progress about 800ms in and never reaches `complete`.**
Not one subresource failed, nothing threw, nothing was logged, no codec was
asked for, and no video element ever existed — the document simply stalls
mid-load until WebKit's own network process gives up on it
(`internallyFailedLoadTimerFired` is that watchdog). The last probe report
arrives at 842ms, which is itself part of the finding: the page's own timers
had stopped running by then.

And it is **not** "embedded browsers can't render modern sites", which is the
reading the symptom invites. Measured on the same machine, the same run: Reddit
completes at 5266ms with 74 scripts and 113 subresources and renders 9060
characters; GitHub reaches `interactive` with 250 subresources completed. Heavy
SPAs work. The fault is in what YouTube's delivery does to this engine, and
until something measures *which* request stalls, the honest sentence is that —
not a guess at a fix, and not a hatch to another browser.

**The user agent is now honest, and it was not the cause.** wry's WebKitGTK
default announced `Version/60.5 Safari/605.1.15` — Safari 15.4, released in
2022, with Apple's build number for it — on a **WebKitGTK 2.52** engine.
`browser::page_user_agent` now gives every page
`Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko)
Codify/<version>`, with the crate's version read at compile time. The
`AppleWebKit/<n> (KHTML, like Gecko)` token stays on purpose: the engine really
is WebKit, it is the one token every parser already looks for, and dropping it
would make Codify read as an unknown client to more sites than the lie ever
did. The Safari release, the `Version/` token and the Apple build number go.
`a_page_advertises_codify_and_not_a_2022_release_of_safari` and
`every_browser_page_is_given_the_user_agent` pin both halves.

**Re-measured after the change, and the verdict is "not fixed".** Four runs of
the same two URLs on the same machine:

| URL | Run | Verdict | `readyState` | Text | Media |
|---|---|---|---|---|---|
| `youtube.com/` | 1 | `rendered` | `complete` @ 2028ms | 2090 chars | 1 element |
| `youtube.com/` | 2 | `content-behind-an-iframe` | `loading` @ 998ms | 6 chars | 0 |
| `youtube.com/watch?v=…` | 1 | `content-behind-an-iframe` | `complete` @ 2029ms | 21 chars | **1 element** |
| `youtube.com/watch?v=…` | 2 | `rendered-nothing` | `loading` @ 1112ms | 21 chars | 0 |

So it is still a coin flip per run, on both pages, and the honest conclusion is
that the user agent was a **true statement and a red herring**: the stall is
not a site branching on a version string. Two things did move, and both are
about what the *completing* runs do — the watch page now reaches `complete`
with a `<video>` element present and `h264=probably aac=probably`, where the
earlier measurement had no video element at all, and the homepage's good runs
complete 92 subresources rather than 59. The next thing to measure is *which*
request stalls, and the probe already has the field for it: `performance`
entries and `readyState` at ~1s say the load stops between two subresources,
not which one.

**Does the string matter? Measured, with a switch to measure it.**
`CODIFY_PAGE_USER_AGENT` (`browser::UA_ENV`) replaces the string for every page,
and `make smoke-embed --user-agent {honest,safari-default,chrome}` sends each of
the three. It is an env var rather than a setting because it cannot be
persisted by accident and it is visible in the process listing; empty falls back
to the honest default, so a harness that forgets to pass one gets the real
thing. `an_override_replaces_the_whole_string_and_nothing_else` pins that an
override reaches the page byte-for-byte — a comparison that quietly sent
something else would produce a confident conclusion about a string no page ever
saw.

Four sites, two runs each, eight runs per column, twenty-four in all, on this
machine. Verdict, then characters of text and `readyState`:

| Site | honest | safari-default (the control) | chrome |
|---|---|---|---|
| `youtube.com/` | iframe 6ch loading · **rendered 2093ch complete** | iframe 6ch loading · iframe 6ch loading | iframe 6ch loading · iframe 6ch loading |
| `…/watch?v=…` | nothing 0ch loading · iframe 20ch loading | **rendered 2378ch complete** · nothing 0ch loading | nothing 0ch loading · iframe 27ch interactive |
| `github.com/python/cpython/issues` | rendered 2411ch · rendered 2608ch | rendered 2608ch · rendered 2608ch | rendered 2608ch · rendered 2608ch |
| `accounts.google.com/` | iframe 135ch complete · iframe 135ch complete | iframe 135ch complete · iframe 135ch complete | iframe 135ch complete · iframe 135ch complete |

**No site in this set behaves differently by user agent.** Every verdict that
differs between columns also differs *within* a column — YouTube produces
`rendered` and `rendered-nothing` under every one of the three strings, which
is the run-to-run variance documented above. GitHub's issue list is
character-for-character identical across all three (2608 chars, 250
subresources), and Google's sign-in is an iframe in all six runs. The one good
YouTube render in the `honest` column is a sample of one, not a result, and
saying otherwise would be the same mistake as reading a single run.

Two things the matrix *did* settle, both UA-independent: **GitHub's CSP blocks
the bridge channel** (`never-answered` under all three strings, so `read_page`
cannot work there whatever we call ourselves), and Google's sign-in always puts
its content behind an iframe. The honest string stays because it is true, not
because it changes a verdict — and the stalls are network, not identity.

**Which request is outstanding, and a verdict for it.** Every field the probe
reported counted something that had *happened*: resources completed (`rp`),
scripts present (`sc`), failures (`x`), throws (`j`). None of them can see a
request still in flight, and that is the question a stuck page asks. Resource
timing cannot close the gap, and the reason is worth writing down because it
is load-bearing: **a resource timing entry only exists once a response has
finished**, and a cross-origin response with no `Timing-Allow-Origin` produces
no entry at all — in flight or complete. So `rp` counts what arrived and is
blind to what has not, which on a video site is exactly the half that matters:
youtube.com's media lives on googlevideo.com.

So the outstanding half is counted where the request is *made*. The probe
patches `window.fetch` and `XMLHttpRequest.prototype.send` — strictly
observationally, the page's own call is what returns — and reads a media
element that is fetching *right now* off the element itself
(`networkState === 2` is `NETWORK_LOADING`, with a `currentSrc`). A
capture-phase `loadstart` gives a real start time; anything the report finds
already loading reports **no age** rather than a zero, because "waiting 0ms"
for something that has been waiting for two seconds is the most plausible-looking
lie available here. The report grows three fields: `q` (the outstanding rows:
kind, address, method, age), `le` (the document's own `loadEventEnd`, so
"still loading" and "its load event has never fired" stop being the same
sentence) and `lz` (the last thing that *arrived*, from the resource timing
entries — the other half of `q`, and on a stuck page the gap between the two is
the finding).

The run reads those into a **second verdict**: `loaded`, `waiting-on-request`,
`waiting-on-media`, `load-never-fired`, `no-stall-data`, `no-report`. The names
follow the rule the rest of the harness follows — a limit of the measurement
must never read as a fault in the site. `no-stall-data` is the load-bearing
one: a report with no `q` at all (a build predating the field, a page that
defeated the patches, a report the channel cut) is *not* a page with nothing
outstanding, and reading it as one is the same confident mistake a missing
bridge line used to cause. `load-never-fired` names the other honest limit: the
document has not finished and nothing outstanding is visible, which means what
it waits for is something this probe cannot see.

**Measured, twenty runs of the watch page, and what each one named.** The
verdict is worth having precisely because building it produced this.

| Verdict | Runs | What was named |
|---|---|---|
| `waiting-on-request` | 1, 10, 20 | `POST rr2---sn-8xgn5uxa-cxgz.googlevideo.com/videoplayback?` (fetch, 155ms); `POST rr8---sn-8xgn5uxa-cxge.googlevideo.com/videoplayback?` (fetch, 123ms); a `MEDIASOURCE blob:https://www.youtube.com/6648ae0b…`; `POST www.youtube.com/api/stats/qoe?`; `POST www.youtube.com/youtubei/v1/player?` |
| `waiting-on-media` | — | (the shape the tests pin; every real run's media row is a `blob:` handle, so it lands in the row above) |
| `load-never-fired` | 2, 11, 16, 18, 19 | nothing visible, and the sentence says why: last arrival `i.ytimg.com/generate_204` at 111ms / `fonts.googleapis.com/css2?` at 836ms / `i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg` at 945ms, then nothing |
| `loaded` | 4, 6, 7, 9, 12, 13, 15, 17 | a `MEDIASOURCE blob:…` and telemetry POSTs (`/api/jnn/v1/GenerateIT`), which are polls; `readyState=complete` at ~2.0s |
| `truncated-report` | 3, 5, 8, 14 | the payload was cut mid-`ua`; the stall verdict beside it read `no-stall-data`, never `loaded` |

**The answer, on the runs where it stalled.** Two `POST` requests to
`videoplayback` on **two different googlevideo CDN hosts** (`rr2---sn-8xgn5uxa-cxgz`
and `rr8---sn-8xgn5uxa-cxge`), 123ms and 155ms old when the report was taken,
with the document's load event still unfired and the last thing that had arrived
being a `tpc.googlesyndication.com/sodar/…` beacon two seconds earlier. That is
the page asking two CDNs for media segments and getting neither, and the
harness now says it in a sentence instead of leaving `readyState=loading` and 92
completed subresources as the whole story.

**Four defects the measurement exposed, all of them in the measurement.**

1. **The channel is about 980 characters wide, and a report that overflows it
   comes back cut off mid-field.** Measured repeatedly and at the same
   character: a 907-character payload arrived whole, a 981-character one was cut
   inside `ua`. A cut payload decodes to nothing, so before this the run reported
   `no-report` about a page that had described itself — a harness limit
   reported as a silent site. Fixed on both ends, and the residue is **named**:
   `truncated-report`, with the stall verdict beside it reading `no-stall-data`
   rather than `loaded`.
2. **No per-field cap predicts the total for a page nobody has run before**, so
   the page measures its own encoded payload and spends the channel in order of
   usefulness: `c9`, `x`, `j`, `lz`, `q`, `ua`, dropping until it fits and
   saying which in `df`. Measured doing its job — one run came to 940 characters,
   dropped `lz`, and kept all three outstanding requests, which is the trade the
   order exists to make. What no verdict is built on is never droppable: what
   the page shows, its frame, codec and refusal state, and its readyState.
3. **A `blob:` URL is a Media Source handle, not an address on the network.**
   YouTube's player names one on every watch-page run. Printed as host+path it
   reads as `www.youtube.com/6648ae0b-…`, which is a fetch that was never
   requested. The probe keeps the scheme, labels the row `MEDIASOURCE`, and the
   sentence says what it is: the bytes come from requests the page cannot see, so
   the one behind it is not named here.
4. **The harness's own traffic appeared in its own findings.** One run answered
   "waiting on `GET codify-bridge://reply/smoke0001/0/0/…`" — the page replying
   to the smoke's question, reported as youtube.com waiting on something. A
   measurement inside its own results is worse than one that is blind, because
   it is believed. The probe skips the bridge's scheme, and a test pins it
   against `webview_bridge::BRIDGE_SCHEME`, because a renamed bridge would put
   the harness back in the middle with nothing failing.

Plus one that is only a disagreement rather than a defect: `readyState`
`complete` beside `loadEventEnd` `0` in the same report, seen in four runs of
seventeen. The engine answers those at different moments, so the page prints the
disagreement instead of the alarming half — a verdict that says the document
finished must not sit beside "its load event has never fired".

**What the verdict does not claim.** An outstanding request is a request the
page has not got back, which is a fact. That it is *what is holding the
document* is a different claim the page cannot make: the load event does not
wait for a `fetch`, so an open XHR and an undelivered subresource can both be
true at once with only one of them the story. The sentence therefore names what
has not come back, prints the last thing that did arrive where there is room,
and leaves the reader to look at the gap.

**So: which request stalls YouTube.** Two `videoplayback` POSTs to googlevideo
CDN, on the runs where it names anything at all. Of twenty runs: **eight
finished** (`readyState=complete` at ~2.0s, outstanding rows are Media Source
handles and telemetry POSTs, which are polls), **eight never finished** — three
of them naming the two CDN requests above, five naming nothing the probe can see,
which the verdict says in those words rather than inventing a request — and
**four lost their report to the channel**, which is now a named verdict rather
than a silent one. The variance is real and it is **not** the user agent (the
matrix above). The next thing worth measuring is the network: whether
googlevideo is reachable from this machine at all, which is a question no amount
of reading the page's own performance entries can answer, because the entries
for those requests never arrive.

**A real defect this re-run exposed, in the bridge rather than in the browser**
— and fixing it turned up a second, larger one underneath. The smoke's third leg
had never passed. It printed `bridge fail There is no browser tab called
"smoke". The open tabs are: none.` and then printed `PASS … and read back
through the bridge` anyway, because the clause was attached whenever *any*
`embed-smoke: bridge` line had been parsed and silence was treated as the only
failure. Three things were wrong, and each is pinned:

1. **`get_webview_window("main")` returns `None` from the moment the first page
   is seated.** Tauri's `is_webview_window` is defined as "has no child
   webviews", so `main` stops being one exactly when there is a page to talk
   to, and `open_tabs` answered "there are no browser tabs open" about an app
   with one open. This was never a smoke problem: **`read_page` could not have
   worked in the running app either.** Both lookups now go through the manager —
   `app.webviews()` and `app.get_webview(&label)` — and the same trap was fixed
   in `lib.rs`, where a second launch unminimised through
   `get_webview_window("main")` and so revealed nothing at all, silently.
   `a_page_is_found_by_its_label_and_not_through_the_main_window` fails if the
   call comes back.
2. **Two interpolated values were not JavaScript literals.** `var ID =
   smoke0001;` and `var SCHEME = codify-bridge;` are both plausible-looking and
   both throw on the *first* line of every script — a `ReferenceError` for an
   undeclared variable, before `send()` is ever called. The only evidence was
   the page's own console, which nothing was reading; the id's *shape* check
   passed the whole time, because `smoke0001` is a well-formed id. Both are
   JSON-escaped now, and
   `every_value_the_template_interpolates_is_a_literal_and_not_a_bare_word`
   checks all six holes in all three scripts.
3. **A failed read is a failure, not a line.** `bridge_clause` derives the
   PASS clause from the *verdict*: only `answered` says "read back through the
   bridge", `answered-nothing` says the page was reached and had nothing to
   give, and anything else gets no claim at all. `bridge_failed` keeps a
   refused leg out of the PASS branch, so `make smoke-embed` exits non-zero and
   says why — "This run is a FAIL because the AI cannot read the page, not
   because of this site."

Measured after all three, on this machine:

| URL | Bridge leg | Run |
|---|---|---|
| `example.org/` | `ok` — 129 chars, one link, `readyState=complete` | PASS in 1.8s |
| `youtube.com/` | `ok` — url, title, links; `answered-nothing` because the document was still `loading` at 1.5s | PASS in 3.3s |
| `reddit.com/` | `never-answered` — the page's CSP forbids every channel (four `unhandled rejection: Load failed`) | **FAIL after 17.0s**, exit 1 |

The third row is the one that used to print PASS.

The judgement half — what a report *means* — is pure and lives in
`scripts/embed_smoke.py`, with `tests/test_embed_probe.py` freezing it against
the reports above, because the display is the slow half of this harness and the
verdict is the half worth freezing.

#### Driving a page from the agent (built)

The conductor gets page verbs on an **existing** role — never a ninth one — and
the agent can navigate, read a page's text, click and type, with the page
staying a sealed guest. Four verbs, two mechanisms:

| Verb | Tool | How it travels | What comes back |
|---|---|---|---|
| move a tab | `navigate_page` | `browser::navigate`, host-side, no page involvement | the shell's own echo |
| read | `read_page` | the page answers over the reply scheme | text, links, address, title |
| click | `click_page` | the page clicks itself and answers | which element, where it pointed |
| type | `type_page` | the page types into a field and answers | the tag, the character count |

`back` and `forward` are deliberately **not** here. The webview's history is
unreachable from Rust (tauri 2.11 has `navigate` and `reload` and nothing
else), so the stack lives in `browserHistory.ts` beside the user; a model
asking to go back is asking for a thing only the shell's own strip knows, and
the honest answer today is the history it can already read from a page read.

**The page cannot answer through IPC, and that is deliberate.** Tauri resolves
every IPC against the calling webview's ACL, and a remote origin is denied app
commands outright (tauri 2.11's `webview/mod.rs`: the check runs when the
command is a plugin command, or the app has an ACL manifest, or *the origin is
not local* — a page at `https://example.com` is the third case whatever the
manifest says). `capabilities/browser.json` is empty and a test fails the build
if it is not. So there is no `invoke` for a page to call, and the answer
channel cannot be one.

**The channel is a custom URI scheme, and it is one-directional.** The page is
asked to fetch `codify-bridge://reply/<id>/<seq>/<last>/<chunk>`; the shell's
scheme handler answers **204 to everything on that scheme**, used or not, so a
page never gets a console line for a reply it was entitled to send. Three
delivery attempts per chunk — `fetch`, `new Image().src`,
`navigator.sendBeacon` — because a page's CSP can forbid any one of them, and a
bridge that reported "the page said nothing" when the cause was a CSP would
send everyone looking in the wrong place. The failure mode is stated rather
than discovered in the field: **a page that forbids all three cannot be driven**,
and the shell says so in a sentence after its own 15s timeout rather than
hanging.

One handler, four ops, two kinds of thing — and the difference is the whole
security argument. `navigate` is three lines of Rust that touch the webview
from outside and never let the page speak, so a page cannot make itself look
like it navigated, or refuse to, to influence the model. `read`, `click` and
`type` need the page's cooperation, so everything they return is *the page's
claim*: a click reports what was clicked, never that it landed, and the way to
see where a click went is to read the page again. The click script refuses a
disabled control rather than reporting a click that would have done nothing,
and the type script refuses a `<div>` or a checkbox rather than pretending a
field was filled.

**Typing is the only verb that writes, so it is the one that says the least.**
`element.value = x` is silently discarded by any page that has replaced the
instance setter — which is what a React-controlled input does — so the script
sets the value through the element's *own* `value` setter and then dispatches
`input` and `change`, because a page that listens for them is how "type"
becomes "submit". What it never does is *press* anything: `format_action` ends
a typed result with *"This tool typed and pressed nothing, so it confirmed
nothing — but do not assume the text stayed put: a page may send what it is
given as you type, and clicking something that submits is what submits it"*,
and the tool's own description says so before the model has typed anything. A
model told a field was typed into will otherwise tell a user it signed in.

That wording is deliberately weaker than it used to be. The sentence claimed
"nothing was submitted, sent or confirmed", which the engine cannot know: a
search box that queries on every keystroke sends the text the moment it lands,
and nothing on this side can see that happen. What it can honestly claim is
which button *it* pressed. It is the same reason `navigate_page` carries no
egress guard — see `03` §1.5.

**Every answer is cleaned to a fixed shape before the model sees it.**
`_clean_result`, `_clean_navigation` and `_clean_action` each return a literal
dict of the fields the formatter already knows how to print, so a page cannot
smuggle a key of its own into the model's context by naming one — the one
containment rule every verb in this file obeys, and the reason a page's answer
is a data structure rather than a blob. An acting answer falls back to *the
selector the engine asked for*, so a page that reports only `{"ok": true}`
still produces a result the model can tie to an action rather than to nothing.

**Both acting verbs refuse locally what the shell would refuse anyway**, so a
mistake costs no round trip the user watches: no selector, a selector over 200
characters, no text, more than 2000 characters of it. And the ops are fixed
strings in `webview_bridge.py`, never the model's to choose — a model that
could name its own op would be choosing from a set nobody enumerated, which is
the difference between a capability and an unbounded one.

**Refusals are one sentence, in one place, shown before the round trip.**
`ui/src/browserDispatch.ts` mirrors `navigation_allowed` — same rules, same
canonicalised loopback spellings, the shell's wording verbatim — and the pane
classifies what the user typed before any shell call. The mirror is pinned in
both directions: the Rust test
`the_ui_dispatch_mirror_agrees_with_the_navigation_guard` reads the UI test's
tables and runs them through the real guard (through the pane's own
normalisation, including the typed-scheme refusal that stops Node's URL parser
rescuing `tauri://`'s authority into a host), and the UI test asserts every
allowance is one the guard makes. A mirror that disagreed would dead-end
working addresses or seat pages the shell refuses; the two-directional pin is
what makes it a mirror instead of a second opinion.

**Why the app keeps its own history.** Tauri v2 exposes `navigate` and `reload`
on a webview and nothing else — no `go_back`, no `can_go_back` (checked against
tauri 2.11.6) — so the webview's history is unreachable and the stack lives in
`browserHistory.ts`. The decisions there, and why each one is a decision:

| Decision | Why |
|---|---|
| `index: -1` before anything is visited | One bounds rule covers a tab that has never navigated and one sitting on its first entry. |
| A new address truncates what is ahead | Going back and then typing something new is a change of mind; forward must not walk into the branch the user abandoned. |
| Re-visiting the address on screen is *not* a new entry | Reload is a navigate to the current URL, because the shell has no reload command. Without this, every reload pushed a history entry and back became a way to re-reload. |
| A bare host gets `https://` in front | `Url::parse` in Rust has no base to resolve against, so `example.com` arrives as a relative URL and is refused. The scheme test is `^https?://` and nothing looser — "anything with a colon is a scheme" reads `localhost:3000` as a scheme named `localhost`. |
| The loopback rule is **not** reimplemented here | `browser/mod.rs` is the authority and its message is shown verbatim. A second implementation in TypeScript would be the one that rots, because nothing would test it against the Rust original. |

**The history lives on the tab**, as `Tab.history`, rather than in a side table
keyed by tab id. It is why closing a browser tab cannot leave a stale entry
behind: the stack dies with the thing it describes.

**`Tab.url` means "this tab owns a webview"**, and it is load-bearing twice.
The pane calls `open` where it would otherwise call `navigate` — the shell
refuses to navigate a tab that has no window, which is the answer to "has this
tab got a page yet" without catching an error to find out. And `handleCloseTab`
skips `codify_browser_close` for a tab with no `url`, because the shell's "no
browser tab is open" is not an error a user should see for closing an empty tab.

**The tab id is the shell's handle on the page, and there is one of them.**
`browser::open` names the window `browser-<tab_id>`; `browser::navigate` and
`browser::close` look that same label up. So the id in `Tab.id` is not a UI
detail — it is the only thing that connects the tab in the strip to the window on
the desktop, and both the `open` call and `openBrowserTab` have to be handed the
same string. They were not, at first: `handleOpenBrowser` minted an id for the
shell and `openBrowserTab` minted a second one for the tab, so the first address
opened a window and *every address after it* was refused as "no such browser
tab". A tab that can only be navigated once is a browser that looks like it
works.

**A refused address is reported in the tab the user typed it in.** The first
address in a tab is refused *before* that tab has a page, so there is no
viewport to put a message in — and the message was being filed against the id of
a tab that was never created, which the pane's `pendingBrowser?.tabId ===
activeTab.id` test does not match. Typing an address did nothing: no
navigation, no error, no alert. The refusal now lands on the tab that owns the
address bar, so the next attempt is a retype.

**Outside the desktop shell both panes refuse, by name.** A page is a
`WebviewWindow` and a shell is a PTY; both live in the Rust process, and the
engine has never exposed either over HTTP. In a plain browser tab
`fallbackHttpInvoke` rejects all seven `codify_browser_*` / `codify_terminal_*`
commands with one shared sentence naming the desktop shell, because every caller
in `App.tsx` already renders `err.message` into the pane that asked. The
alternative — reaching the `default:` branch — threw `Unknown command:
codify_browser_open`, which tells a user their app is broken rather than that it
is a browser, and (per the paragraph above) produced no visible message at all.

**Two browser tabs on one address are two tabs**, the opposite of
`openConversation`. A duplicate chat tab is a duplicated transcript streaming
the same events twice; two browser tabs are two pages with two histories, and
collapsing them would make the second unreachable.

**What the suite does now, and what it still cannot.** `browserHistory.test.ts`
covers the stack, `browserDispatch.test.ts` the mirror (pinned in both
directions against the Rust guard), `browserPane.test.ts` the markup and the
wiring freezes, and `browserPaneInteraction.test.ts` drives the pane through
the DOM harness: typing, Enter, a refusal appearing and clearing, Escape
restoring the committed address, and the mount-time bounds report. What the
harness still cannot reach is the native half — a real webview seated on the
reported rectangle needs a display — and that is where the delivery path's last
step used to be exercised *nowhere*: for as long as the pane existed, Linux
ignored the rectangle and the smoke asked for the one rectangle that made the
ignorance invisible. `make smoke-embed` now reads the widget's real allocation
and fails the run when it is not what the pane asked for (§7.3). The
freezes exist for the wiring that was silently wrong before: the id handed to
`codify_browser_open` is the tab's own id, and a refusal is keyed to a tab
that exists. Both were found by typing an address into a running dev server
and watching nothing happen — neither had a test, and a static render could
not have found either.

**Both panes were found the same way, and the fixes are the same shape.** Run in
a plain browser tab — no Rust process — every pane command reached the
`default:` branch of the HTTP fallback, and a terminal that would not start
reported itself to `pendingTerminal.error`, which was rendered only when
`pendingTerminal.ptyId` matched the active tab. A *failed* open leaves that id
null, so it matched nothing. That `error` prop has been removed from
`TerminalPane` rather than left as a second, permanently-empty channel: a pane
cannot have failed before it exists, so the two failures that can reach it are
its own `failed` state, and a refused start goes to the app-wide banner. A
prop nothing can fill is a channel that looks like reporting and is not. The
`pendingTerminal` state itself is gone for the same reason: both its readers were
wrong, and the tab is already the record of which PTY is open.

**The app-wide banner is dismissible**, and it has to be. It is where a refusal
lands when there is no pane to put it in, and `setError(null)` is only ever
called by whichever handler owns the *next* action — so a message from an action
the user does not repeat stayed on screen indefinitely. A refusal the reader has
understood and cannot clear is an obstacle, not a report. The control is an
`IconButton`, so its `label` is a required prop and a dismiss icon that reached
production without an accessible name is a type error rather than something a
test has to remember to check.

### 7.4 First paint, and the smoke that measures it

Everything above stops one step short of the display. A page can pass the
guard, resolve no capability, report its own load — and still never put a
pixel on the screen. The Rust tests here are the ones that freeze that
boundary deliberately: the guard and the capability set are testable without a
compositor, and *a webview that resolves its guard, loads its HTML and paints*
is the one claim only a real run can make.

**Four ways that goes wrong, three of them outside this crate's reach.** They
are diagnosed rather than worked around, because a heuristic that "fixes" the
sandbox by disabling it weakens isolation for everyone to unblock one machine:

1. **The single-instance guard.** `tauri-plugin-single-instance` claims the
   session-bus name inside `Builder::build()` — before this app's own `setup`,
   which is why the engine launch sits behind it. On `NameTaken` the plugin has
   the **newcomer** call the incumbent's `ExecuteCallback` and then
   `std::process::exit(0)` (checked against the vendored source,
   `tauri-plugin-single-instance-2.4.5/src/platform_impl/linux.rs:69-88`). So a
   second launch is not a hung first paint: it is an exit, before any webview
   exists, **with status 0 and no output of its own** — the "Second launch…"
   line a person waits for is printed by the *other* instance. Measured here by
   running two smokes at once: one painted in 0.9s, the other died at 0.1s
   with no mode line at all.
2. **WebKit's bubblewrap sandbox.** If `/usr/bin/bwrap` is missing or
   `kernel.unprivileged_userns_clone` is 0, the web process never starts: the
   page loads nothing and paints nothing. This is a broken webview, not a
   hanging one.
3. **The DMABUF renderer.** On some driver/compositor combinations first paint
   hangs in the GL path. The known workaround is
   `WEBKIT_DISABLE_DMABUF_RENDERER=1`, and **this app never sets it itself** —
   it is the user's environment to decide, which is why the diagnostic reads
   the variable and says who set it.
4. **A zero-sized pane** (mechanism 0 of §7.2) seats nothing at all; the
   refusal names the recovery so the pane can re-ask.

`browser::log_environment_diagnostics` prints one line per fact — session bus,
sandbox, DMABUF, display backend — at the top of `run()`, before anything can
fail. It changes no behaviour, and `run_prints_the_environment_diagnostics_first`
holds that it stays first: a boot that dies in `build()` has already said
which fact it died on.

**`make smoke-embed` is the run that measures it.** It builds the shell if
needed, launches it with `CODEIFY_EMBED_SMOKE=<url>` and never starts an
engine, relays the output, and passes only on a paint:

- The page is seated exactly as `codify_browser_open` seats a tab — same guard,
  same `add_child`, same `page_layer` placement — at `24,24 560×420`, and the
  run **checks the toolkit's own allocation** against that rectangle 700ms
  later, from a worker thread rather than from `setup`, so the check covers the
  thread a user's click arrives on and not only the one that already worked. The
  geometry *is* the variable now: it used to be 800×600 at the
  origin, which a discarded request imitates perfectly (§7.3).
- `SMOKE_PAINT_SCRIPT` is injected at document start and asks for **two**
  `requestAnimationFrame`s. The second callback is the one that proves the
  compositor consumed a frame; "the document loaded" is weaker again, which is
  the whole reason a script exists.
- **The page announces by setting its document title**, and the shell's own
  `on_document_title_changed` hook re-announces it as a Tauri event. This is a
  measured correction, not a preference. The first version had the page call
  `__TAURI_INTERNALS__.invoke("plugin:event|emit_to", …)` itself, on the
  reasoning that a one-way event is harmless. It is not: `emit_to` is an
  ACL-governed command like any other, this page holds the empty `browser-*`
  capability set *on purpose*, and the invoke was refused. So the smoke could
  only ever fail, and its failure read as "first paint is broken" on a machine
  where painting was fine — on a Wayland session whose four diagnostics all
  read healthy, the page was in fact painting in **0.9s** while the smoke
  reported a 20s timeout. The title hook is the one channel a page needs no
  permission for, and it is the same mechanism the tab strip's live title
  already depends on. `the_paint_script_announces_the_event_the_lib_listens_for`
  holds the choice: the script may not grow an `invoke` back, and
  `SMOKE_PAINT_MARKER == SMOKE_PAINTED_EVENT` keeps the page's word and the
  shell's word from drifting.
- **The process status is real.** `AppHandle::exit` only *requests* an exit
  (it raises `RunEvent::ExitRequested`/`Exit` and unwinds; tauri 2.11.6
  `app.rs:574`), so the code it is given rides on the event and the process
  still ends **0** — a failed first paint that exits 0 is a failure to
  anything reading the status. `smoke_exit` flushes both streams and calls
  `std::process::exit` instead, which is safe here and only here: smoke mode
  never starts an engine, so there is no child to reap and no lease to break.

**The harness is written to say which story it is in**, because "it didn't
work" is the failure mode of a first-paint report. It distinguishes the two
cases that look identical in the child's output — a *stale binary* (no
diagnostics, no mode line: it printed neither, so the binary predates both) and
the *guard refusing a second launch* (diagnostics present, then silence: the
launch ended inside `build()`) — and replays the boot diagnostics with every
failure. Both are "this run measured nothing", and the difference is a
`cargo build` versus stopping another instance.

`make smoke-embed` is a local target, deliberately not part of `check` or `ci`:
it needs a display, and the Actions workflow is manual-only
anyway (see `check.yml`). The unit tests keep the wiring honest; this keeps the
claim honest.

#### The strip comes back: `make smoke-tabs`

The embed smoke proves a page paints; it says nothing about §2.1's other half —
that a strip the engine holds comes back as tabs a window shows. Every unit test
of that chain stops one step short of the whole: `tests/test_shell_tabs.py`
proves the engine's three routes, the UI suites prove the mirror, the merge and
the restore against fakes, and none of them can claim the chain a second window
performs without thinking — engine row → pull → adopt → restore → seat — because
its middle lives in a React effect that only runs in a real window, and its two
ends live in two different processes.

`make smoke-tabs` runs the claim. It builds the shell if needed, launches it
with `CODEIFY_TABS_SMOKE=1` under a throwaway `CODIFY_HOME` — the one
non-negotiable in the harness: the smoke writes rows into an engine's database,
and without the isolation the seed would land in the developer's real strip
and come back on their next boot — and the shell does the rest as its own mode
(`tabs_smoke_mode` in `src-tauri/src/lib.rs`):

1. boot **the ordinary way** — engine, handshake, UI, exactly as a user's
   launch, because the round trip is the subject and a special-cased boot would
   be measuring a different thing;
2. wait for the handshake, then seed `PUT /shell/tabs` with one browser row
   through the same authenticated route a window writes through — a refusal is
   a finding, since the smoke sends exactly what a real window sends;
3. focus the window, because attention is what the pull runs on (§2.1's honest
   cost — a smoke that never looks at the window would be testing a pull that
   never happens);
4. wait for a webview seated at the seed's address, then read the strip back
   from the engine, print one `tabs-smoke: restored <kind> <key>` per row,
   `tabs-smoke: PASS`, and exit for real through `smoke_exit`. While waiting it
   samples the window's own page every few seconds (`tabs-smoke: probe {…}` —
   tab count, localStorage engine port, visibility, error-banner text), because
   a failure that names its stage is useful and a failure that shows the strip
   growing or not growing while it names it is usable.

The proof is the seat, and it is really two halves. The strip half lives in the
**key**: the seed is `k_tabs_smoke_seed`, which satisfies the UI's own key law
(`k_` prefix, bounded length) but **cannot be minted by `tabKey`** — that
factory emits exactly two `_` separators with base36 between them, and the seed
carries a third. A key that cannot be minted can only be received, so a seed
key in the strip means the row arrived through the engine and was adopted as a
stranger by the merge. The webview half lives in the **address**: the UI seats
a webview only for the active tab's address (by local `id`, never by key — the
label cannot be the fingerprint), and that address exists in the UI only
because the seeded row was restored into it. A webview at the seed's address
therefore means the restored tab was not just data but a page the window is
showing. The UI has no smoke branch at all — everything the verdict needs is
already observable from the shell, so the production path is the only path
there is.

The harness (`scripts/tabs_smoke.py`) reads the run through the `tabs-smoke:`
lines and nothing else, requires the seed in the strip its PASS claims, and
diagnoses a silent child the same way the embed smoke does: the two stories a
silent boot can tell — a stale binary and the single-instance guard's newcomer
exit — are printed together, because stdout cannot distinguish them. Like
`smoke-embed`, it is a local target, not part of `check`/`ci`, for the same
reason: it needs a display and a real engine boot. Its judgement logic, unlike
the launch, is testable without either — `tests/test_tabs_smoke.py` replays
recorded runs through a fake child and holds the contract strings from both
sides.

### 7.5 The AI reads the tab (built)

The conductor can call `read_page` and be told what the webview the user is
looking at is showing. It is the first path between a running turn and a live
page, and it is split across two processes that had never spoken: the question
goes engine → shell over the bridge routes in `04` §9, the shell puts it to a
webview with `Webview::eval`, and the answer comes back on a custom URI scheme
(`03` §1.6).

**"The page you are looking at" has to be a fact, not a guess.** `is_visible`
is not on tauri 2.11's `Webview` surface — `browser::focus` shows one view and
hides the rest precisely because it cannot ask which is which — so the shell
cannot work it out after the fact. `codify_browser_focus` calls
`webview_bridge::note_active(&tab_id)` on the way through, which is the only
moment the answer is known. From there `resolve_tab` prefers it, falls back to
the only open tab, and **refuses with the tab names** when there are several
and none was named. A model guessing which of five tabs to read is the failure
this ordering exists to prevent.

**It can move the tab, and the guard is the user's guard.** `navigate_page` lets
the model put an address in the bar, and it does that by calling
`browser::navigate` — the function `codify_browser_navigate` calls for a click.
So a model-proposed URL meets `parse_navigation` rather than a reimplementation
of it: http(s) only, no loopback in any spelling, and `on_navigation` guards the
redirects afterwards (`03` §1.5). `navigate_tab` in `webview_bridge.rs` contains
no host check at all, and a test fails the build if one appears.

What it will not do is open a *new* tab. `browser::open` takes the content
rectangle the pane measures and owns, so a tab the bridge invented would have no
geometry and would sit somewhere the pane does not describe; `resolve_tab`
refuses and names the open tabs instead.

Reading and navigating are still one operation at a time. Queueing them would
let a read of the page and a move of the page interleave, so a read could return
the text of a page the user is no longer looking at, with nothing saying so.

**And the smoke measures it.** The first-paint smoke has three deliverables now:
the page composited a frame, the page described itself, and *the same page read
back through the bridge*. The third is `webview_bridge::smoke_probe`, which
builds a request locally and hands it to the same `serve` the engine's poll loop
uses — smoke mode never starts an engine (the browser module's
`the_smoke_mode_is_gated_reports_and_never_starts_the_engine`), so there is no
`/bridge/next` to poll and nothing to hand the question out. Everything the
answer depends on is therefore measured for real: the `eval`, the custom
scheme, the chunk reassembly, the JSON. Only the two HTTP routes are not, and
those are frozen without a display in `tests/test_webview_bridge.py`.

A page that renders beautifully and answers nothing fails the smoke, by name.
That is the failure this leg exists to catch, because a `Content-Security-Policy`
forbidding `connect-src`, `img-src` *and* `sendBeacon` looks from the outside
exactly like a working site — the symptom a user would otherwise report is "the
AI says the page is blank". `--no-bridge` is the escape hatch, opt-out rather
than opt-in on purpose: a smoke that quietly stops measuring the bridge is how a
bridge that cannot reach a page becomes a passing build.

The reply channel carries text only and is deliberately **not** the document
title, which is the one channel a page with no capability is guaranteed to have
and which `browser/smoke.rs`'s probe uses. The title renames the tab, and a
user reading a page while the model reads it too is not the smoke's throwaway
webview. `webview_bridge.rs` explains the rest, including why the script tries
`fetch`, `Image().src` and `sendBeacon` rather than one of them.

## 8. The keyboard layer (built)

`ui/src/shortcuts.ts` maps a keystroke to a shell action, `ui/src/commandPalette.ts`
holds what Ctrl+K can find, `ui/src/components/CommandPalette.tsx` renders the
overlay, and `App.tsx` dispatches through one capture-phase `window` listener.
All three logic modules are pure and tested in `ui/tests/` (`shortcuts.test.ts`,
`commandPalette.test.ts`), for the reason `tabs.ts` is: the mapping is a table
of decisions — AltGr, shifted digits, what Ctrl+9 means — that markup cannot show.

| Keystroke | Acts as |
|---|---|
| Ctrl+T | New Tab — a clean, empty tab in the selected project, the same act as the header's New Tab button |
| Ctrl+W | Close the active tab, landing on the left neighbour (§4 arithmetic) |
| Ctrl+1..8 | Focus the tab in that strip position |
| Ctrl+9 | Focus the **last** tab — the browser convention, so a strip past nine stays reachable at its end |
| Ctrl+B | Hide or show the left panel (§8.1) |
| Ctrl+= (or Ctrl++) | UI scale up one step, 100 → 112.5 → 125 → 150 → 175% (docs/02 §3.3) |
| Ctrl+- | UI scale down one step |
| Ctrl+0 | UI scale back to the 125% default |
| Ctrl+K | Command palette: open tabs, every conversation in this workspace, and every settings destination (`Provider keys & endpoints`, `Agent roles & prompts`, `Audio: microphone, dictation & read-aloud`, `Appearance, themes & UI scale`, `About Codify`), token-filtered with title-prefix hits ranked first |

Decisions, and why:

- **The modifier is Ctrl, and only Ctrl.** `Super` belongs to the desktop, so an event carrying
  it is never ours, alone or chorded with Ctrl. `Alt` never fires a shortcut: AltGr reports Ctrl+Alt together, and without
  that guard AltGr+T on a European layout would open a tab mid-sentence.
- **Letters require no Shift** (Ctrl+Shift+T is somebody else's reopen-last-tab
  muscle memory); **digits ignore Shift** and are read from `code`, so the
  physical `1` key works on layouts where `key` is `!`.
- **The scale keys ignore Shift and read both `key` and `code`.** `+` is Shift+`=` on many layouts and
  its own key on others (German), so Ctrl+Shift+= must scale as well as Ctrl+=, and reset is the
  physical `0` because that key types `à` on AZERTY. A held key is one step: auto-repeat would run
  the window from 100% to 175% in a blink.
- **The listener runs in the capture phase** — a shell shortcut beats the
  focused control — and consumes its keystroke with `preventDefault`. Ctrl+W closes
  the active tab through the same `handleCloseTab` seam as the strip's close
  button, because a browser tab's embedded page has to be told to go (§7.2) —
  the strip is the only closer a child webview has; the listener therefore
  re-binds when the tab set changes,
  which is cheap, and buys the single seam that keeps a page from being orphaned
  by closing its tab one way and kept alive by closing it another.
- **Escape belongs to the palette alone** while it is open: the input stops
  its propagation, because `SettingsModal` and the model dropdowns close on a
  *window-level* Escape and one keystroke must never close two surfaces. The
  panel also prevents mousedown's default so a click inside it cannot blur
  the input onto `<body>`, where Escape would miss the palette entirely — and
  focus returns to the control Ctrl+K interrupted when the palette closes.
- A thread with no title is "New chat" in the palette, matching
  `Sidebar.threadTitle` — two surfaces that named the same row differently
  would be a bug the eye would find first.

Honest limit: this layer is registered in the DOM, the *last* stop — a compositor
or window manager that consumes a keystroke before the webview reaches it (a desktop that
binds Ctrl+W itself, say) degrades to that binding. The
tests pin the mapping, not the platform.

`TabBar`'s module docs promise exactly these tab shortcuts; this section is
the half that keeps the promise.

### 8.1 Hiding the left panel

The left panel (threads, New Project, Browser, Terminal, Settings) can be hidden with the first
control in the header, an icon button that reads **Hide left panel** / **Show left panel**, or with
Ctrl+B. The choice is remembered across restarts (`codify.sidebar` in `localStorage`, `ui/src/sidebarPref.ts`).

- **View state, not a tab record.** How the window is laid out is not what is open in it, so the flag
  lives beside the theme and the UI scale, and never in a `Tab`, `tabPersistence.ts` or the engine's
  `/shell/tabs` (§2.1).
- **Unmounted, not collapsed, so the room is real.** The panel is a flex sibling of the centre
  column (the same rule as the drawers). Hiding it removes it from the layout, so the transcript,
  a terminal or a browser pane widens. A browser pane's native webview is told its new rectangle
  by the pane's own `ResizeObserver` (§7.3), which fires because the column changed size; an
  overlay or a slide-over would have been painted *under* the native view.
- **The button lives in the header** because the header is the one bar that is always there. A toggle
  inside the panel could not bring the panel back. It is before the tab strip, so it does not move as
  tabs open, and it is not a tab.
- **Open is the only default.** A missing, empty or unrecognised stored value means open; only the
  exact word `closed` hides it. A panel kept hidden by a misread word would take Browser, Terminal
  and Settings out of reach with no hint why.
- **With a terminal in front, Ctrl+B is the shell's.** It is tmux's prefix and readline's
  back-a-character, so the key passes through untouched (the handler returns before
  `preventDefault`). The header button still works with a pointer, so the panel is never out of reach.
- **What is not in the header or the palette.** While the panel is hidden, Browser and Terminal are one
  click away (show the panel). Settings stays reachable through the palette (Ctrl+K).
- **The panel gives way to a drawer, as a derived state.** At 125% the panel (`w-60`, 15rem), the Stats
  drawer (`w-[28rem]`) and a centre column that can still hold the composer did not fit a 1280px window,
  and the transcript was squeezed to about 400px. While a drawer is open and the row is narrower than
  panel + drawer + 30rem (`sidebarYields`, `ui/src/drawers.ts`: 15 + 28 + 30 = 73rem for Stats, 65rem
  for History), the panel is not drawn. The threshold is in **rem**, so it follows the UI scale; a pixel
  media query would be right at one scale and wrong at the next. It is measured with a `ResizeObserver`
  on the row (`useSidebarYield.ts`), and a row that cannot be measured never hides anything.
  **It is never stored.** `codify.sidebar` is written only by the person's own press of the toggle, so
  closing the drawer brings the panel back as it was, and a window that was widened meanwhile never
  remembers a panel it was only asked to put aside. The toggle reports what is on screen (**Show left
  panel** while a drawer has displaced it), and pressing it then closes the drawer, because the two do
  not fit; an ordinary press is an ordinary hide. The drawers stay flex siblings, never overlays, for
  the reason above, with caps (Stats 45%, History 40% of the row) so the centre column keeps at least
  the rest even where the panel is hidden and the window is smaller than the rule assumed.
  `ui/tests/drawers.test.ts` holds the rule; `drawerLayout.test.ts` mounts the App at 100, 125 and 175%
  in a window whose width it controls.

## 9. Voice: the mic beside Send, and answers read aloud (built)

The composer has a mic button immediately left of Send (`ui/src/components/MicButton.tsx`). It turns
speech into text in the prompt. Each answer has a speaker that reads it aloud (§9.1). Setup lives
in Settings → Audio (docs/02 §3.2), and the engine side is in docs/04 §3.0.2.

- **The engine records, not the webview.** The webview is WebKitGTK through wry, and wry neither
  enables media capture nor answers a permission request. Granting it the microphone would mean
  new shell code, and a mic grant to a webview that sits beside an in-app browser. The button
  therefore asks the engine to start PipeWire's `pw-record`, shows the elapsed time, and asks it
  to stop. The engine sends the recording to the dictation provider, answers with the words, and
  deletes the recording.
- **Dictation fills the prompt and never sends it.** The words are placed at the caret, read when
  they *arrive* rather than when the button was pressed, because the person may have typed on
  while speaking. A space is added only where two words would otherwise touch (`insertDictation`,
  `ui/src/speech.ts`). Afterwards the caret sits after the dictated words, and Send is still the
  person's own press.
- **Esc while recording discards the recording.** The engine deletes the file and sends nothing to
  any provider. That Esc is caught on the document in the capture phase and goes no further:
  the prompt's own Esc stops a running goal, and one keystroke must not do both. A click on the
  button also leaves focus on the button (in WebKit, nowhere), so a listener on the prompt would
  miss the key. The rule is the same with the palette or Settings open: the first Esc discards the
  recording, and the next one closes them.
- **The engine's limit is a stop, not a loss.** At `max_seconds` (120 s), the engine has already
  ended the recording and kept it. The button then stops by itself and transcribes what was said.
- **Unset is a route to the fix.** While dictation has no provider, the engine refuses with
  `stt_not_configured` before anything is spawned, and the click opens Settings → Audio instead.
  Any other refusal, such as PipeWire missing or the provider failing, is shown beside the mic in
  the engine's words.
- **The microphone does not outlive the composer.** If the composer unmounts mid-recording, it
  cancels the recording.

`ui/tests/micButton.test.ts` mounts the whole App for each of these, except Esc and unmount. Those
two mount the button alone, in front of a stand-in prompt that records whether the Esc reached it.

### 9.1 Answers read aloud

A turn's finished answer has a **Read aloud** button under it (`ui/src/components/SpeakButton.tsx`).
The engine asks the read-aloud provider for a WAV (`POST /audio/speak`), and the webview plays it
through an `<audio>` element fed a `blob:` URL. The CSP already allows `media-src blob:`, and
playback needs no permission.

- **The words, not the markup.** `speakableText` (`ui/src/speech.ts`) drops emphasis, heading and
  list marks. It reads a link as its text and a code block as "(code omitted)": a voice spelling
  out forty lines of code is worse than silence. `clipForSpeech` cuts at a sentence end inside the
  engine's 4,096-character limit.
- **One voice at a time.** There is one player. Starting an answer stops whichever one is playing,
  and a stop pressed while the audio is still being fetched wins over audio that arrives late.
- **Only the finished answer.** While a turn is still arriving, its streamed snapshot stands in for
  the answer (§10.15). That snapshot has no speaker, because a voice reading a draft that is then
  replaced has said the wrong thing.
- **Auto-read is for answers you watched arrive.** With "read each answer aloud as it arrives" on in
  Settings → Audio, an answer reads itself only if this window saw its turn unfinished (in flight,
  or still being dispatched). It is read once, when the reply is in and the engine says the turn is
  over (`answersToRead`). Opening a thread, restoring one from History or reloading never starts
  talking. The switch is asked of the engine when an answer is due rather than held in the
  transcript, so turning it on takes effect for the next answer. When several answers land
  together only the newest is read. A turn that planned is a run, not an answer, and is never read.
- **A failure is a sentence.** If read-aloud cannot run, for example because it has no provider or
  the provider refused, the engine's reason is shown beside that answer's button. That includes an
  auto-read nobody pressed.

Not yet: pipeline goals' summaries have no speaker; a turn is the conversational case this was
built for.

`ui/tests/speakButton.test.ts` holds the rule and mounts the transcript. It then re-renders the
transcript the way the app feeds it, with a turn in flight and then the same turn finished, because
that difference is the whole of what auto-read decides.

## 10. What a turn is

Everything above builds a place to *put* a conversation. Nothing above made one
possible, and the gap was total: until this section, every message in this app
was a `POST /goals`, so typing "hi" started a librarian, a designer, a planner
and a fixer and produced a **plan for a greeting**. The transcript was a filing
system for goals wearing a chat UI's clothes, and the failure was invisible
because the app looked like it was answering.

### 10.1 The signal already existed

`engine/laya.py` has asked every goal what kind of request it is since the gate
was written. `LAYA_QUESTIONS["intent"]` is a typed `choice` over
`code_change | question | ops_command | other`, `evaluate_policy` reads the
answer, and `LayaDecision.intent` exposes it. **Every use of that answer was a
warning string** — the one branch that fired was `ops_command`, and it only
appended to `warnings`.

So the fix was not a new model path. It was honouring a signal the engine was
already producing and discarding, which is a different kind of change and a much
smaller one: `run_chat` asks the gate, and `intent == "question"` answers.

### 10.2 A turn is a goal, and that is not a shortcut

`events.goal_id` is `NOT NULL REFERENCES goals(id)` (`engine/db.py`). The event
log *is* the WebSocket, the audit trail, the usage books and the stats feed. A
turn stored in a `turns` table would have nowhere to write a single streamed
token, and building a second event system beside the first is how a project
ends up with two histories that disagree.

So a turn is a goal with `mode: "chat"` and no steps ever. Reusing the row is
what buys the client the entire streaming and audit surface for free: the UI
posts, subscribes to `/ws/goals/{id}`, and renders — unchanged.

| `GoalMode` | What it runs |
|---|---|
| `normal` | the 8-role pipeline |
| `design` | the pipeline, with DESIGN.md as the deliverable |
| `knowledge` | the pipeline, with CODIFY.md as the deliverable |
| `chat` | a turn: the gate, then one answer — or the conductor |

### 10.3 One door

`GoalCreate` **refuses** `mode: "chat"`, so there is exactly one route that can
make a turn and one place that decides what it becomes. `TurnCreate` carries no
`mode`, no `dry_run`, no `plan_only`: the client does not choose the shape of a
turn, because a client that could choose is the client that produced a plan for
"hi". Both refusals are invariant 8 (docs/00 §6.8) and both are in
`tests/test_turns.py::TestOneDoor`.

`TurnCreate` is also `extra: "forbid"`, so `agent_config` is a 422 — invariant 2
holds on the new route for the same reason it holds on `POST /goals`.

### 10.4 No gate means the conductor

A fresh install has no gate configured, and `engine == "skipped"` is the ordinary
first-run state. A question branch that fires on *no classification* would mean
an unconfigured install silently answers code changes from a chat call — the
exact failure this section removes, in a new place.

So the routing is: the conductor decides, and it decides everything. When one is
configured it runs whatever the gate said — a question, a change, or no
classification at all — and §10.14 is what it may then do. When there is no
conductor the older rule stands, unchanged: `intent == "question"` **and** the
gate answered → answer, and anything else — `code_change`, `ops_command`,
`other`, or no gate at all — runs the pipeline, with a log line saying which and
why.

The two are not in tension. The pipeline is a superset of answering, so with no
conductor, guessing wrong costs a slower answer while the reverse guess costs a
code change the user believed was acted on. When a conductor is configured but
*fails* — its model errors, says nothing, or spends its whole call budget without
producing an answer or a plan — the pipeline does **not** run as a floor: a
question, a greeting or an unclassified request gets a single plain reply, and a
request the gate read as a change ends honestly (§10.14). A second driver
quietly taking over a conductor that failed was the same fault as the sweep
behind an approved plan: it spent twice, and it planned something the conductor
never chose.

A blocked turn is a blocked goal: same gate, same `laya_blocked` code, same
event. The gate guards the engine, not a pipeline.

### 10.5 Memory, from rows that already exist

Nothing read prior turns, so "now do the other one" was unintelligible and a
second turn was a stranger. `GoalService.turn_history` derives
`(prompt, reply)` pairs from the thread's `mode='chat'` goals — no new table, and
no second copy of a conversation that could disagree with the transcript.

Only chat goals are read. A pipeline run's "reply" is a commit subject and step
summaries, and feeding those to a turn as though a person had said them is how a
thread fills with noise. A turn that was cancelled or failed is still returned,
with an empty reply: dropping it would lose the fact that the user asked
something.

The history is bounded (`TURN_HISTORY_TURNS`) and each turn is clipped keeping
both ends, so a long thread cannot grow a prompt without bound and the most
recent exchange — the one a follow-up refers to — is never the part dropped.

### 10.6 The conductor

`engine/conductor.py` is the other half. The gate routes; the conductor
*dispatches*. It is a loop, not a role: docs/00 §6.1 fixes `AgentRole` at eight,
so a ninth row in `agent_configs` would be a ninth role the moment anything
iterated it. It is configured through `engine_settings` instead, and measured
through the ordinary `agent_assigned` / `usage` events.

**Judgement is the model's; authority is the engine's.** Every move is a call
the pipeline already makes, through the same service:

| Move | Routed through | Inherits |
|---|---|---|
| `read_file` | `LibraryService.read` | path escape refused by `FileSystemService` |
| `search_code` | `LibraryService.search` | same |
| `git_history` | `GitService.read_only` | `sandbox.validate_argv(mode="read_only")` → `engine/git_readonly.py`: subcommands, their exact options, and every positional, one owner; 60 s bound, no credentials in the child's environment |
| `run_command` | `SandboxService.run_command` | `validate_argv`, `test` mode (docs/00 §6.6) once the goal is approved; `read_only` before that, so a turn cannot start the repository's code |
| `read_page`, `navigate_page`, `click_page`, `type_page` | `WebviewBridge` (`§7.3`, `§7.5`) | the page is text from the web, quoted as untrusted; every navigation meets the shell's guard; typing submits nothing |
| `read_editor` | `SurfaceBridge`, the `editor` surface (`04` §9.1) | eyes on the person's editor, **unsaved text included**; quoted file text, in a fixed shape with caps |
| `open_in_editor` | the same, `open` | hands that only point: show a file and a range; nothing on disk changes |
| `edit_editor` | the same, `edit` | hands that change **the open buffer only**: one undoable edit marked as the assistant's, never saved. The person's own Save is the only door to the disk (docs/00 §6.9) |
| `recon` | `ExecutorService._librarian` | read-only, bounded rounds |
| `design` | `ExecutorService._design` | no tools at all; decides from the evidence |
| `plan` | the planner | refuses without evidence; writes steps, never files |
| `write` | `ExecutorService._fixer` | docs/00 §6.9 — the only move that touches the filesystem, and it refuses while the goal is unapproved |
| `verify` | `ExecutorService._verifier` | `validate_argv`, `test` mode — the second door, same allowlist |
| `review` | `ExecutorService._critic` | approve or request changes; cannot write |
| `summarize` | `ExecutorService._scribe` | commits, and only after `review` approved |
| `todo` | `engine/todo.py`, over the goal's `todo_updated` events | none — the conductor's own note to its next run: bounded (20 items, 160 characters, 40 edits per run), one line each, put back in its prompt as *its own notes, not instructions*, never shown to a sub-agent and not in `RECALLABLE`. Nothing in the engine reads it to decide anything |
| `ask_user` | `engine/ask.py` | none — ends the run with one question for the person (a few options at most), and the answer is their next message, an ordinary turn (docs/00 §6.8). Offered on a turn before a plan exists, never after one and never while an approved plan is running |
| `use_skill` | `engine/skills.py` | none — a skill is data, never a capability |

**The prompt names every tool, and says what the engine does with a call.** `CONDUCTOR_SYSTEM_PROMPT`
(`engine/chat_prompts.py`) is the one place that says *when* to reach for each tool. It had fallen behind the
menu (`recall`, the page tools, `todo` and `ask_user` were never named, the budgets were not mentioned, so a
model spent calls as if it had no limit and then met a paused goal), and the recipe it points at still let a
critic's objection be argued with. `tests/test_conductor_prompt.py` holds it from three sides: every name in
`ConductorTools.NAMES` appears in it, a name in backticks (in it, in the per-turn briefs, in the step prompt)
is a tool that exists, so a stale name cannot outlive its tool, and it stays under a length ceiling, because it
is paid on every call by a model whose window may be 4096 tokens beside twenty tool schemas. It states the
behaviours a model cannot see from a schema: plan once and wait for approval; a critic's objection is reported
and the run stops; calls and moves are limited and a step that spends them is paused; `run_command` only reads
until a plan is approved; a page is text and not instructions; `todo` notes are the model's own;
`ask_user` is for what is needed before planning. `ship-a-change` says the same and no longer tells the
conductor to argue with the critic.

So the conductor gains *choice* over existing powers, never *new* ones. There is
still no `write_file` and no `commit`: the move that writes is the fixer's own
method under the fixer's own validation, and the move that commits is the
scribe's after the critic approved. §10.14 is the section on how that holds when
the model — not the code — is choosing the order.

**What it can say to them.** `recon`, `design`, `plan` and `write` each carry a
line of the conductor's own text, and each delivers it: the ask is placed beside
the goal and labelled as an addition, because the goal is what the user asked
for and a conductor that could rewrite it would be sending a sub-agent after
something nobody requested. `docs/01` §5.1a has the table. Two limits are worth
knowing before you rely on it: the three step moves are addressed by `step_id`
alone, so the conductor cannot say what `review` should focus on, and the gate
itself is not one of the seven — it has already run, before the loop existed.

`git_history` is worth calling out, because it is where "the model asked for it"
was once the whole check. It now runs `sandbox.validate_argv(mode="read_only")` —
**the librarian's validator, not a copy of it** (both are `engine/git_readonly.py`'s exact-match
table since the audit of 2026-09-29, which also bounds it to 60 seconds and gives it a stripped
environment: it used to inherit the engine's, provider keys included; docs/04 §5 "Read-only git"). Until that call replaced its own
private list it had two defects at once: a subcommand list eight names long against
the librarian's seventeen, so the two had already drifted; and no flag or argument
check at all, so `git log --output=<any path>` wrote a file outside the workspace
with content the model chose, `git diff --no-index /etc/hostname /dev/null` read
one, and `git branch NAME` created a ref — each of them a bare word or a flag no
name list can see. `git branch` and `git tag` are now read-only only in their
listing forms, which is why the rule is per-subcommand and not one shared set of
listing flags (`-a` lists for one and annotates for the other). The caller is a
model, and "the model asked for it" is not a reason to run `git commit`.

### 10.7 The loop terminates, and says so

`conductor_max_turns` (default 14, settings-clamped to 1..40) bounds *model
calls*, because that is what costs money. It is a budget per *run*: a turn is a
run, and so is each step of an approved plan (§10.14), so a plan of five steps
has five budgets and step one cannot starve step five. Fourteen is one step's
worth (four moves, a failed `verify` sent back through `write`, and the reads
between); a turn needs far fewer. The bound is a hard stop, not advice:
when it is reached the loop makes one final call, **drops** whatever tools that
reply asks for, and returns its text with a sentence saying it was cut off. An
earlier version appended a "you are out of calls" nudge and then honoured the
next request anyway, so a model that kept asking kept the loop running forever —
`tests/test_conductor.py::TestTheCap` found it.

A turn that was cut off says so. One that trails off mid-thought reads as a bug
rather than as the bound it is.

### 10.8 Failure is a sentence, not a crash

An invented tool name, a malformed argument, a tool that raised — each comes back
as text the model can read and recover from, because a loop that dies on a bad
call is a loop that stops the first time a model is slightly wrong. The refusal
branch names the *menu the model was given* rather than the dispatch table, so a
tool that is offered but unwired is named as unavailable rather than silently
missing from the list.

`ApiError` and `CommandNotAllowed` are the two exceptions: those are the engine
refusing, and the sentence says retrying will not help — a recoverable-looking
refusal teaches the model to keep asking.

### 10.9 The conductor is an upgrade, never a prerequisite

`_conduct` returns `None` — and the turn degrades to a single model call — when
the provider has no tool support, when the role has no configured model, or when
the provider fails mid-loop. A user must be able to ask a question on any
install; shipping this feature with a "your provider is too old" failure would
have replaced one unusable behaviour with another.

### 10.10 What this does not change

The gate still gates. The sandbox still sandboxes, through the same validator.
The fixer is still the only writer. The eight roles are still eight. The engine
still binds loopback behind a bearer token. A code change still runs the
identical pipeline it ran before any of this existed — reached now by a model's
judgement instead of by a user's keystroke, which is the whole difference.

### 10.11 What a real model found

`tests/test_turns.py` and `tests/test_conductor.py` are hermetic by
requirement — a test double is what lets them assert "the planner was never
called". Nothing in them could find the four defects below, because a double
returns what the implementation expects and a real model returns what a real
model does. All four were found by running the engine against a live local
Ollama (`scripts/drive_a_turn.py`), and each is now a test named for it.

| # | Defect | What it looked like | Where |
|---|---|---|---|
| 1 | Ollama writes tool calls into `message.content` as JSON and sends **no** `message.tool_calls` | the raw JSON was shown to the user as the answer | `toolcall.coerce_tool_reply` |
| 2 | Ollama parses `function.arguments` as an **object**; OpenAI specifies a **JSON string** | `400 Value looks like object, but can't find closing '}' symbol` on the second half of every loop | `toolcall.to_ollama_messages` |
| 3 | The LLM-fallback gate's prompt listed `code_change\|question\|ops_command\|other` with **no definitions**, while `risk` got a full scale | `"hi"` classified `other` → 7-step pipeline → 217 seconds | `default_prompts.DEFAULT_PROMPTS["laya"]` |
| 4 | A code model replies with JSON anyway when told not to | `{"error": "I cannot read or access files…"}` shown to the user | `executor._as_prose` |

Two lessons worth keeping:

- **A test double cannot check a wire format.** Defects 1 and 2 are pure
  message translation. They are now pinned by
  `tests/test_conductor.py::TestProvidersThatWriteToolCallsAsText`, which
  stores the *verbatim* reply a live model returned, and by a test asserting
  the two dialects disagree on `arguments`. Unit tests over the neutral
  shape were necessary and not sufficient.
- **Defect 3 was a prompt, not a program.** The gate's whole job was to
  classify `intent`, the enum was reachable, and nobody had checked that the
  model could map anything onto it. Every assertion in `test_laya.py` was green
  because they test `evaluate_policy`, not whether the *model* produces an
  answer the policy can use.

### 10.12 The one known cost

A turn that routes to the pipeline gates **twice**: `run_chat` asks once to
decide whether to delegate at all, and `run_planning` gates again as its own
first stage. Both decisions are recorded on the log, so nothing is hidden, and
the second is the gate `run_planning`'s contract requires — but it is one
redundant typed call on every code change initiated from a turn. Passing the
first decision through is a one-parameter change to `run_planning` and was
deliberately not done here: it touches the function every other caller depends
on, and a green gate was worth more than the saving on this change.

### 10.13 Verifying it for real

```
python3 -m scripts.drive_a_turn "hi"
python3 -m scripts.drive_a_turn "Read greeter.py and tell me what greet returns"
python3 -m scripts.drive_a_turn "Add a docstring to greeter.py"
python3 -m scripts.drive_a_turn "Remember my favourite colour is teal" \
                               "What is my favourite colour?"
```

`--approve` presses Start on the plan a turn makes and reports how the run ended (finished, paused with the
engine's reason, or failed; the exit status is non-zero unless it completed). With no model at all,
`FAKE_CONDUCTOR=1 python3 scripts/fake_ollama.py` plays a scripted conductor over the real Ollama wire format
(`CONTRIBUTING.md` lists its scenarios), and `tests/test_fake_conductor.py` runs the whole path against it: a turn
that plans, Start, the four step moves, one fixer call, one commit, the conductor's calls booked as `conductor`,
a stalled conductor paused as `conductor_stopped`, a question and the turn that answers it, and a note kept
across the run. What that proves is the loop, the wire, the driver and git together; what it cannot prove is that
a real small model behaves like the script (§10.14a is the measurement that does).

The four expected outcomes, all observed:

1. **question → conductor answers in prose, 0 steps.** ~3-4s, where the same
   prompt before this work ran the eight roles for 217s.
2. **question needing a file → `conductor called read_file({"path": …})`,
   then a correct prose answer.** The loop, the tool result round trip and
   `coerce_tool_reply` all live here.
3. **code change → the conductor decides.** With a conductor configured it
   plans, and the goal ends `PENDING` awaiting approval with nothing written;
   with none, the gate's `code_change` routes straight to the pipeline and the
   outcome is the same. §10.14 is the path in between.
4. **memory → turn 2 answers from turn 1**, which it cannot do from its own
   prompt: `turn_history` is the only source.

It starts the engine in-process against a throwaway `CODIFY_HOME` and posts
over the real route with the boot token, so it exercises the client's path and
never the developer's state.

It is deliberately *not* part of `make check`: it needs a model, and a gate
that depends on somebody's machine is the thing docs/00 §6 is against.

### 10.14 The conductor chooses the sequence

§10.6 describes the conductor as a dispatcher with five read-only tools and one
`delegate` that ran the whole recipe. That was the first shape of the idea, and
it left the sequence compiled in: `delegate` fired the librarian, the designer
and the planner whether or not the request needed them, and there was no way for
the model to say "change this one step" or "verify only that".

The sequence is now a decision. Four things changed and one deliberately did not.

**The order became a skill.** `engine/builtin_skills/ship-a-change.md` is the
recipe that used to live in `run_planning` and `run_step`, written as
instructions. It is discovered by name and one-line description, its body is
fetched with `use_skill` only when it is wanted, and a
`<workspace>/.codify/skills/*.md` file of the same name replaces it — with the
replacement announced in the transcript, because a silently shadowed recipe is
worse than an obvious one. `context-transfer.md` is the directory's other
built-in and the proof that the set is not a pipeline with one entry.

**A skill is data, not a capability.** `.codify/skills/` arrives with a cloned
repository, which makes it untrusted input. A skill can sequence moves that
already exist; it cannot define a move, cannot widen `validate_argv`, and cannot
reach the write gate — that gate reads the goal's *stored status*, not anything
the model was told. The worst a hostile skill can do is argue, and an argument
cannot open a door. `tests/test_skills.py::TestASkillCannotEmpower` holds it.

**A skill may say which moves it is written around, and that is a hint and never a grant.** A header line
`moves: recon, plan, write` names them. The names are checked against the moves that exist
(`ConductorTools.NAMES`, passed into `load_skills` so `engine/skills.py` imports nothing of the conductor); one
that is not a move is dropped and reported with the skill's other problems, never obeyed, and a header key
nothing reads is reported too (it used to vanish, which made a misspelt `moves:` look exactly like a skill that
declared none). Names in a report are clipped, because a header is untrusted text. What `use_skill` does with
the list is say, after the body, which declared moves are not on the menu *right now* (`write` before there is
a plan, say), so the model is told before it is refused. It reads the menu and never adds to it, speaks only of
names that are real moves, and a skill whose moves are all offered comes back exactly as written. `ship-a-change`
declares its seven. `tests/test_skill_moves.py` holds it, and `TestASkillCannotEmpower` has a hostile skill
declaring `write, run_command, delete_everything`.

**Links are not followed, at either level.** A skill *file* that is a symlink is refused, and so is a
skills *directory* that does not resolve to exactly `<workspace>/.codify/skills` — a link at `.codify`
or at `skills` would otherwise load a far directory's files as instructions and put the first line of
each in the conductor's menu with no tool call. Each refusal is reported as a problem, never skipped in
silence, and the built-ins still load. A workspace that is itself opened through a link is fine: the
comparison is against the workspace's own resolved path.

**A second built-in, for handing the thread over.**
`engine/builtin_skills/context-transfer.md` is the other one: when a
conversation is long enough that the model has started losing track, load it and
package the thread into a single pasteable block for a new thread. It is a
recipe for prose, not for the workspace — it changes nothing and reaches no
move, which is the point of putting it beside `ship-a-change` in the same
directory: a skill is a *kind* of thing, and the set is not a pipeline with one
entry. It is the one built-in whose value is a list of things it refuses to
carry across, so `tests/test_skills.py` pins those phrases rather than its
prose — a handoff that dropped the gate's real result, or a key, would be worse
than no handoff.

**The stages became moves.** `recon`, `design`, `plan`, `write`, `verify`,
`review` and `summarize` are each one of the pipeline's own methods, wrapped in
the same `self._stage(goal_id, …)` block the recipe uses. So the accounting, the
`stage_result` vocabulary and the stats screen needed no new case, and
`tests/test_metrics.py` still re-derives the call sites from the source and fails
if a move is added without one. They are also more forgiving than the recipe was:
`TestsFailed` and `CriticRejection` come back as values the conductor can act on
rather than unwinding a step, so a conductor that cannot make a test pass can
re-plan or report where a step could only fail.

**What did not change is who is allowed to do what.** docs/00 §6.9: only the
fixer writes, and the move refuses while the goal is unapproved. The seam is
`RUNNING`, reachable only through `POST /goals/{id}/start` — a person saying yes.
So a turn that produces a plan ends with the plan in front of the user and
**nothing written**; the approval starts it, and `run_conductor_resume` hands the
approved plan back to the conductor to execute.

**After Start the conductor owns every step it is given.** It is one conductor
*run* per open step, each with its own budget and a prompt that names the plan in
outline and gives one step in full (the critic's earlier notes ride along as
quoted data when there are any). One run over the whole plan piled every move's
result into one context, which a local model's window cannot hold for a plan of
any size. Each run is re-derived from rows — the goal, its steps, the step's diff
events — rather than a persisted transcript, which is the same choice §10.5
already makes for turn history: a resumed step recovers the *files* an earlier
run wrote (so `verify` works without a second `write`) but never a verdict or an
approval, which describe files as they were when they were given.

The step's stored status is the judge, never the conductor's last sentence. A
step that is `COMPLETED` after the run was completed, however the run ended. One
that is not leaves the goal `PAUSED` with a reason from `models.PAUSE_CODES`
(`conductor_budget`: it used its calls; `conductor_provider`: its model could not
be reached; `conductor_stopped`: it stopped without finishing; and the critic's
`critic_rejected`), and **nothing else touches the step**. There is no engine
sweep behind the conductor: that sweep re-ran any step left open through the
fixed recipe from scratch, so a conductor that wrote a step and ran out of calls
before `summarize` had the fixer run on it a second time, and "the conductor
drives execution" was a switch that could double-write. Only the person's Start
resumes a paused goal, at the step that was left open; Retry resumes with that
step in focus, and keeps the critic's notes for the conductor that will act on
them (the recipe's fixer never reads them, so a recipe retry still clears them).
A critic that asks for changes pauses the goal and the conductor is told so, with
every reason, and told to stop; the next `write` is allowed again only after
Start (`tests/test_conductor_drives.py`).

**Where the recipe still drives an approved plan.** An install with no
tool-capable model, a goal with `parallel` set (the engine's batcher proves which
steps touch disjoint paths and a conductor has no such proof), and
`conductor_drives_execution = 0`, which turns the conductor off without a rebuild.
Benchmarks and `scripts/replay_trace.py` call the recipe directly and measure it.

**A turn the conductor could not finish ends honestly; there is no second
pipeline.** If the conductor's model errors, returns empty content, or spends its
whole call budget without producing an answer or a plan, `run_chat` used to run
the sequence it would have run before the conductor existed. On an install that
has a conductor it no longer does. What the turn ends in depends on what was
asked: a question, a greeting or a request nothing classified gets **one plain
streamed reply** (answering is safe, and a plain reply cannot change a file), with
a warning that the conductor did not finish; a request the gate read as a change
gets **no pipeline** — if the conductor ran out of calls but had words, they are
the turn's reply, with a warning that nothing was changed and that the budget is
a setting; if its model failed, or said nothing, the turn **fails** with
`conductor_failed` and a sentence naming the provider's *code* (never its
message, which is third-party text) that says nothing was changed and what to do
about it. A plan made before the conductor stopped still stands. Where there is
no conductor at all (no tool-capable model), nothing changed: the recipe is the
driver there, as it always was (`tests/test_conductor_turn_end.py`).

One distinction the design rests on, and the reason declining and failing are
modelled separately: **a conductor that declines is obeyed; a conductor that
fails is caught.** Judging that no change is needed is a decision, and running
the recipe over the top of it would make the brain a suggestion. Producing
neither an answer nor a plan is not a decision, and it is *caught*: the turn says
so and nothing is done in the conductor's name that it did not choose.
`TestDecliningIsObeyedAndFailingIsCaught` holds the first and
`tests/test_conductor_turn_end.py` the second.

*Empty is silence, not an answer.* A reply of no text and no tool call used to
count as finished — the turn completed with the literal words "(no answer)" and
the floor never ran, which a small model that spends its budget thinking, or a
server that answers `{}`, produces on demand. An empty answer with no plan now
is handled exactly as an error is (a plain streamed reply for a question; for a
change, a failed turn that says the model said nothing and that nothing was
changed); an empty final word *after* a plan stands, because the plan is the
turn's result. "(no answer)" is still what a person sees if the plain reply is
silent too, since there is nothing left to try
(`tests/test_empty_conductor_reply.py`).

### 10.14a What the live runs actually showed

The five runs below were against `qwen2.5-coder:7b` on a local Ollama, through
`scripts/drive_a_turn.py`, and each one changed the code:

1. **The gate's verdict was not reaching the conductor.** A `code_change`
   request arrived looking like any other prompt, so the model answered it with
   a clarifying question instead of planning. Fixed by `_intent_brief`: the gate
   classifies on every request and its answer was being computed and dropped,
   which is the same discarded signal this whole section started from.
2. **The skill menu alone did not make the model load a skill.** It answered
   from the transcript instead. Naming the skill in the brief for a change
   request fixed it: `conductor called use_skill({"name": "ship-a-change"})`.
3. **A 7B narrates the sequence instead of calling it.** After loading the skill
   and calling `recon`, it replied "2. `plan` — Turn the request plus the
   evidence into steps" and stopped, having called nothing. One bounded
   `nudge` — a single reminder, never a loop — turns this into a real call in
   some runs and not others.
4. **A call written inside a prose fence was shown to the user as the answer.**
   `coerce_tool_reply` recovered a whole-body envelope and a whole-body fence,
   but not prose that narrates a move and then writes the call underneath it in
   a ```json block. It does now, guarded the same way — the recovered name must
   be a tool that was actually offered — and the verbatim reply is pinned in
   `tests/test_conductor.py::TestACallWrittenInsideProse`.
5. **A decline that is right looks exactly like a decline that is wrong.**
   Asked to document `greeter.py`, the conductor read the file, found the
   function already documented, and said so — correct, evidence-based, and
   correctly obeyed. The same shape then appeared where the model *intended* to
   act and never did. Nothing in the text separates those two, so the engine
   does not guess: it obeys both, and logs a warning when the gate read the
   request as a change and no plan came out, so an untouched workspace cannot
   read as an updated one.
6. **The verdict was an order, and the classifier is not reliable enough to
   give orders.** With the SDK gate live, "hi" took 78 s: the gate said
   `code_change`, the brief said anything but a `question` "means the user wants
   the workspace changed", and a 7B loaded `ship-a-change`, answered the skill's
   text as if it were the user, and sent the librarian to analyse the repository
   for a greeting. Two causes, both fixed. The gate was handed the goal's
   execution mode for a *turn* — `direct-apply`, an action word — and measured
   offline on the real SDK that alone flips "hi" and "hey" to `code_change` (with
   `chat` they are `other`; `hello`, `thanks`, questions and real changes are
   unaffected), so `build_state` now says `chat` for a turn. And the brief is now
   **advice**: only the labels that describe a change (`code_change`,
   `ops_command`) point at the recipe and arm the reminder, they say the label
   is a guess and to go by the user's words, and `question`, `other` and an
   unlabelled request are answered directly. The system prompt and `recon` say
   the same from the other side: the read tools are the conductor's own, the
   sub-agents are for what is broad or what the user asks for. The no-conductor
   floor above is a different decision and is unchanged.

The honest conclusion from 3, 5 and 6: **the architecture is sound and a 7B local
model is not good enough to drive it reliably.** That is the risk this section
named before it was built, it is why a goal the conductor cannot finish pauses
for a person rather than failing, and it is why `conductor_drives_execution`
exists. A model that can call three tools in a row
without narrating them is a different machine, not a different design.

### 10.15 A benign turn is a conversation

The gate runs on every turn — it is what decides the turn's shape — and it never
stopped running. What it did do was *report*: `run_chat` published the verdict,
and the transcript drew it as a card over the answer, so typing "hi" produced

```
Laya gate passed (Laya via fallback model)
intent question   risk 0.00   injection 0.01   / block at 0.85
Hello! What would you like to work on?
```

which reads as a pipeline that vetted the person before answering them. That is
the behaviour this section removes, in the one place it was still on screen.

The announcement is now conditional, and the parts are separated:

* **The gate still gates.** Same call, same stage: the `laya` row in
  `STAGE_OUTCOMES` records `allow`/`block`/`skipped`/`unavailable` for a turn
  exactly as it does for a goal, so the stats screen and the silent-role audit
  count it the same way. `tests/test_turns.py::test_the_gate_ran_before_the_answer`
  reads that record to prove the gate ran, because the verdict is no longer in the
  turn's log.
* **A blocked turn is still a blocked goal** — same code, same `laya_blocked`
  error, and the card *is* published, so a refusal has its reason on screen
  (§10.4).
* **A warned turn shows the warning.** Silence is for a verdict that decided
  nothing a person can act on; a warning is the opposite of that.
* **Only that.** A benign turn publishes its reply and its `goal_status` and
  nothing else about the gate: no `agent_assigned`, no `laya_decision`.

The transcript's half of the same rule is `ui/src/turnTranscript.ts`, because a
rule that only exists inside a component's render is a rule nothing can assert.
It says what a turn's message *is*: the reply as prose; the conductor's streamed
snapshot standing in while it arrives; the gate's card along with any warnings
and errors beside it; and no narration of the engine's own tool calls
("conductor called `read_file`…"), which stay in the event log and in the audit
export. A turn that *planned* keeps the run's card, approve button and all —
`isConversationalTurn` is the chat mode **and** no steps, because the turn and the
run it turned into share one goal row.

`tests/test_turns.py::test_a_benign_turn_does_not_report_the_gate` and
`ui/tests/turnTranscript.test.ts` hold both ends: the engine's silence, and a
rendered turn with no gate line in it.

### 10.16 A turn ends where the engine says it did

"After every message the AI has to stop" is not a matter of taste: the engine
writes `goal_status: COMPLETED`, and the UI has to believe it. It did not. The
tab strip's busy dot read

```
m.isStreaming === true || isGoalActive(m.goal?.status)
```

and `isStreaming` is a flag this client sets at dispatch that nothing clears when
the engine finishes — not the terminal `goal_status`, not the stream closing. So
a turn answered in three seconds left its tab pulsing for the rest of the
session, and the transcript said the assistant was still working after it had
stopped.

The fix is to stop asking two questions and take the engine's answer: once a
message has a goal, the goal's status decides (`isMessageBusy`,
`ui/src/goalActions.ts`). `isStreaming` keeps the one job it can do honestly —
the gap between dispatching a goal and having a row to read — and it is cleared
when the terminal status arrives as well, so a finished message carries no stale
flag for the next reader. `ui/tests/goalActions.test.ts` pins all three cases: a
finished goal is not busy whatever the flag says, a live goal is, and a message
with no goal yet is busy only while its dispatch is pending.

### 10.17 An answer is Markdown, and nothing in it is interpreted as markup

A model answers in Markdown, and the transcript drew it raw: `##`, `**` and fences, all showing.
The usual remedy is a Markdown library plus an HTML sanitiser plus `dangerouslySetInnerHTML`, and this
app has none of the three: no Markdown library is installed even transitively, and nothing under
`ui/src` sets inner HTML. A reply can be steered by any file or page the model read, so adding the one
door that turns it into live markup, for the sake of headings, was the wrong trade. The renderer is
therefore structural rather than defensive:

* **`ui/src/markdown.ts` is a parser that returns data.** Text in, a tree out: no DOM, no React, no
  HTML. **`ui/src/components/Markdown.tsx`** turns the tree into React elements, and React escapes
  every string it is given, so there is no step at which text is read as markup and nothing to forget
  to sanitise. `<script>` in an answer is the characters `<script>`. Headings, paragraphs, bullet and
  numbered lists (nested by indentation), fenced code, quotes, rules, inline code, bold, italic,
  strikethrough, links, images and bare URLs are understood. Raw HTML, tables, footnotes and setext
  headings are not: each degrades to readable text. A single newline is a line break, not a space,
  because plain-text answers were drawn with `whitespace-pre-wrap` until now and a model that wrote two
  lines meant two lines.
* **An unterminated fence runs to the end.** The streamed snapshot (§10.15) is re-parsed on every
  update, so half a code block is the normal state of a reply in flight; it looks like code while it
  arrives instead of snapping into shape at the end. Unclosed `**` or a lone backtick stay literal
  until they close.
* **The input is untrusted and is parsed in bounded time.** Nothing is quadratic in the length of a
  line: an opener with no closer is remembered, a link label and URL are scanned to a fixed length,
  and hand-written scanners replace regexes that could backtrack. Nesting is capped at `MAX_DEPTH`
  (8), and past it the content is still all there as text. `ui/tests/markdown.test.ts` feeds
  200,000-character hostile inputs against a time budget and random input against "never throws".
* **No `<img>`, and no `<a href>`.** An image is never fetched: the answer shows `[image: alt text]`.
  A link is a *button*, because the main webview has no `on_navigation` guard and a real anchor would
  navigate the app itself away, taking the UI with it. A click calls `onOpenLink(url)`, which `App.tsx`
  turns into a new browser tab through the same path a page's popup request takes (§7.2): the shell's
  `navigation_allowed` still decides, and a refusal is shown in that tab. There is still no system
  opener (§7.2); a link is the one route from an answer to a web page, and it is the app's own browser.
* **What may be a link** is `ui/src/markdownLinks.ts`: an explicit `http://` or `https://` address, one
  that `classifyBrowserAddress` accepts (so never a loopback or unspecified host), with no whitespace or
  control characters. `javascript:`, `data:`, `file:`, `mailto:`, `tauri://`, relative paths, anchors and
  bare hosts are words with the address in a tooltip. It is stricter than the address bar on purpose:
  a model's reply is not a person typing, and a bare word does not get to claim to be a website.
* **Where it is used.** The turn's answer and its streamed snapshot; the Knowledge and Design
  deliverable bodies (`DeliverableText.tsx`), which open **Rendered** and have a **Source** button that
  shows the exact bytes in a `<pre>` (the file the next run will treat as fact, or that the user may
  pin as a contract, is never only shown interpreted); and a plan step's description, with inline marks
  only. Role replies, logs, errors and the audit report stay raw: tests assert their JSON. The person's
  own message keeps its line breaks and wraps a long word (`whitespace-pre-wrap break-words`).
* **Speech is unchanged.** `speakableText` (§9.1) already drops the marks and reads a code block as
  "(code omitted)"; it reads the engine's string, not the rendered tree.

`ui/tests/markdown.test.ts` is the parser (structure and hostile input), `markdownRender.test.ts` is
the mounted renderer (no live element or attribute, whatever the answer says; link behaviour; copy),
`deliverableText.test.ts` is the Rendered/Source toggle, and `answerLinks.test.ts` mounts the whole App
and clicks a link in a real answer through to `codify_browser_open`.

### 10.18a A paused goal says why

`PAUSED` has several causes and the badge alone names none of them: the person pressed Pause, the critic
asked for changes, or the conductor could not finish a step (§10.14). An engine pause carries a `reason_code`
from a closed set and a `reason` sentence the engine wrote on its `goal_status` event (`docs/04` §1.4). The
goal's card draws them in a banner **above the buttons that resume it** (`PauseBanner`, rules in
`ui/src/pauseReason.ts`): the cause as a heading, the engine's sentence under it (which ends in what to do),
and, for the critic's pause, the critic's own reasons quoted from the step's notes as plain text.

* **The newest `goal_status` event is the whole state.** A Start publishes `RUNNING` with no reason, so a
  resumed goal stops showing one with no bookkeeping, and a goal record that has not caught up with the event
  cannot keep a stale banner alive.
* **The person's own Pause shows nothing.** It carries no code; they know why.
* **A code this build does not know is not drawn.** A half-drawn pause is worse than the plain `PAUSED` badge.
* **Wrapping, not clipping.** The sentence ends in the next action; a clipped one would end before it.
* The conductor settings card says the same from the other side: a step that runs out of calls pauses the
  goal, where a turn answers with what it has.

Proven by `ui/tests/pauseReason.test.ts` (the rules) and `ui/tests/pauseApp.test.ts` (the card and the
notification through the whole App).

### 10.18 The notification inbox: what happened while you were looking elsewhere

A **Notifications** button in the header, between Stats and History, opens a drawer of what this window
saw happen. It is in-app only: no desktop notification and no sound. It is kept on the client and nowhere
else, because each of its sources is a fact only this window knows. A goal's result is already
durable in History, and the other three are not facts the engine keeps. The rules live in
`ui/src/notifications.ts`, pure and DOM-free, so each has a test that needs no renderer.

| Source | When it is news | When it is not |
|---|---|---|
| **Goal finished or failed** | The end of a goal stream **this window opened** (`onTerminal`). A failure names the first failed step. | Opening a thread or restoring History opens no stream for a finished goal, so old results are never announced (the `answersToRead` rule, auto-read's). **Cancelled** is silent: the person did it. |
| **Goal paused by the engine** | A `goal_status` for a pause that carries a `reason_code` (the critic asked for changes, or the conductor could not finish a step) **and is the goal's current state**: the event's version is the goal's version (`pausedNotification`). Titled with the cause, detailed with the engine's sentence, and opens the goal. | The person's own Pause carries no code, and a pause the stream replayed after the goal had moved on has an old version. A code this build does not know is not announced. |
| **Plan ready for approval** | A `goal_status` that finds the goal `PENDING` and the plan will really wait: plan-only, dry-run, or a composer mode other than Direct Apply. | In Direct Apply the plan starts itself (the poll starts a `PENDING` goal), so "waiting for you" would be false. |
| **Engine connection** | A change that **held for 4 s**: offline, back, token refused, restored. The engine's last stderr line is the offline detail. | `checking` never settles. The first settled state is only a baseline, so an engine that is up when the window opens says nothing, and a flap that reverses inside the settle time is one that never happened. |
| **Model list change** | A `model_catalog_changed` frame with something in it, counted per provider (added and removed). docs/06 §6. | An empty diff, `model_catalog_checked`, and a payload that does not read. |

* **No duplicates, because the engine replays.** Every entry has an id that names the event
  (`goal:<id>:<status>:<updated_at>`, `paused:<id>:<version>`, `plan:<id>:<version>`,
  `models:<fetched_at>:<diff>`), and an id already
  in the list is dropped. A goal that is retried and fails again is a new entry, because `updated_at` moved.
* **Opening is reading.** Everything is marked read when the drawer opens, and as entries arrive while it
  stays open, so the header count clears at once. The dot on a row is for the ones that were new when the
  drawer opened, and it lasts as long as the drawer does, so the person can still see what they had not seen.
  The drawer also offers **Mark all read** and **Clear**.
* **A row goes where the event is.** A goal reopens the way History reopens one (`restoreGoal`); an engine
  entry opens Settings on About, which holds the engine card, and a model entry opens Provider Keys.
* **Remembered, and never trusted.** The list (newest first, capped at 100) is stored under
  `CODIFY_NOTIFICATIONS` and read back through a validator: a bad entry is dropped on its own and never
  repaired, a list that cannot be read is an empty list, a storage that throws is an empty list on load and
  a no-op on save, and the target of an entry is a closed set. The first render does not write back what it
  just read: a storage that failed to read would be overwritten with the empty list it produced.
* **What it deliberately leaves out.** The error banner (mirroring it would flood), a missing key (a derived
  state the App cannot see), the motion banner, and anything that is not about something that happened.

`ui/tests/notifications.test.ts` is the rules; `notificationsApp.test.ts` mounts the whole App against
recorded sockets (the harness's `ctx.sockets`: the test is the engine on the other end) and checks each
source is actually connected, that the header order is Stats, Notifications, History, that three drawers are
one at a time, and that the list survives a restart.

### 10.19 The conductor asks, and the answer is a turn

A conductor that cannot go on without something only the person knows used to have one way to say so: prose
that happened to end in a question mark. Nothing knew it was a question, so a model could ask and then carry
on in the same reply, the window could not offer the choices as choices, and the nudge that tells a stuck
model to "ask in one sentence and stop" had nothing that made it stop. `ask_user` is that something.

It is a tool whose whole effect is to end the run (`EndTurn`, `engine/conductor.py`): the loop stops at the
call and drops whatever else the same reply asked for. It carries a question (at most 500 characters) and
optionally two to four options (each at most 80 characters, one line, a repeat said once; one option is not a
choice and is refused). What the engine publishes is the turn's ordinary reply with the question as prose,
numbered options included, and the same question as data on the same event (`question`, `docs/04` §1.4), so
history and read-aloud carry it and the window can draw it.

* **Not a capability, and not a new door.** It cannot approve a plan, write or run anything. The answer is the
  person's next message, an ordinary turn through `POST /conversations/{id}/turns`: a button press sends the
  option's words exactly as typing them would, there is no "answer" route, and nothing is held between the
  question and the reply (invariant 8, §10.3). The reply is in the next turn's history like any reply, so
  "Postgres" means something to the model that asked.
* **Offered only where it can be answered and seen.** Never while the goal is `RUNNING` (an approved run has
  nobody sitting at it; a step ends finished, or paused with a reason, §10.14), and never once a plan exists
  (a turn that planned is drawn as its plan, so a question after it would not be seen; the answer to a plan is
  Start, an edit, or a message). Both are enforced twice, by the menu and by the tool, which reads the stored
  rows like `write` does.
* **A question is a finished turn.** A change request that ends in a question is not "the conductor finished
  without planning anything": it asked for the one thing it needs to start, and the engine says nothing else
  about it.
* **The buttons are for the moment.** `TurnExchange` draws the options only while nothing follows the
  question (`answerable`): once the person has answered, or sent anything else, the question stays in the
  history as words and offers nothing. Options are re-bounded in `ui/src/turnTranscript.ts` rather than
  trusted, and are text on a button, never markup.

Proven by `tests/test_ask_user.py` (the rules, the loop stopping, where it is offered, the turn it ends) and
`ui/tests/askUser.test.ts` (the reading, and the buttons through the whole App sending an ordinary turn).

## 11. The clipboard history (built)

An icon-only **Clipboard** button sits immediately after Notifications in the header and opens a fourth drawer, in
the same slot as the others, of what was copied, cut or pasted in this window. It is a flex sibling of the centre
column and not a popover, for the reason the others are (§8.1), and it takes History's width, so the left panel's
yield rule (`ui/src/drawers.ts`) treats it the same. The rules live in `ui/src/clipboardHistory.ts`, pure and
DOM-free, the listeners in `ui/src/useClipboardHistory.ts`, the view in `ui/src/components/ClipboardDrawer.tsx`.
The button has no word on it, so its name is its `aria-label` ("Clipboard history"), and its title begins with
"Clipboard", not with Browser, Terminal, Keys or Settings, which is how the panel's own buttons are found.

### 11.1 Inside Codify only

It sees what passes through **this window** and nothing else. The document's own `copy`, `cut` and `paste` events
are given to any page for its own content without asking for anything, so there is no permission to grant, no
watcher on the system clipboard, nothing in another program and no new process (`docs/03` §1.10, `docs/07`).
What is outside it is outside it: a copy made in another application, and a copy made inside a browser tab, which
is a separate webview with a page of its own (§7.3). Taking those would need a system-wide watcher with its own
permission and its own spawn site, which was weighed and declined.

* **Copy and cut are heard after the handlers beneath them have run**, so text a terminal writes into the event is
  what is kept (xterm draws its own selection, which the window cannot see). Failing that, the selection: read
  from the field itself when it is a text field, because `window.getSelection()` reports nothing for one.
* **Paste is heard before them**, so a handler that stops the event cannot hide it.
* **A Copy button that writes through `navigator.clipboard` fires no event at all**, so it says what it copied
  through a context (`ClipboardRecorderContext`): a code block's Copy, and only once the copy has worked. The
  fallback Copy path uses a scratch field marked `data-clipboard="off"`, so the same copy is not read a second
  time as the person's selection.

### 11.2 What it never keeps, and says so

The drawer's standing note says what is kept, where, and what never is, because a history that quietly kept a
password would be worse than none.

* **A password field, and anything inside `data-clipboard="off"`, is never read.** Both key fields (the Provider Keys
  row's and the agent card's) carry the mark, because once the eye is pressed each is a plain text field and no
  longer private by type; `clipboardPrivacy.test.ts` reads the source and fails on any input that can be a
  password field and lacks it.
* **Anything that looks like a credential is refused** (`looksSecret`): private key blocks, `sk-`, `AIza`,
  `nvapi-`, GitHub, Slack and AWS token prefixes, JWTs, `Bearer` and `Authorization: Basic|Token` values, a
  secret-named variable assigned a long literal, and a long mixed-case token run. The shapes mirror the engine's
  `redact_secrets` (`docs/00` §6.4) and add the common prefixes. It is a **heuristic, not a guarantee**: a
  credential in a shape it does not know is kept, which is what Delete and Clear unpinned are for. Hashes, UUIDs,
  paths, URLs and camelCase names are left alone on purpose, or the history would refuse its own contents.
* **Whitespace alone, and anything over 10,000 characters, are refused, and never cut.** Half a snippet pasted
  back is a silent corruption.
* **A refusal that was expected is a sentence.** A key or an over-long clip is explained in the drawer until the
  next thing is kept or the note is dismissed. An empty selection and a private field say nothing: the first is a
  Ctrl+C with nothing selected, and the second is private by being one.

### 11.3 Remembered here, and never trusted

The list (newest first, 50 unpinned and up to 20 pinned) is stored in this window's `localStorage` under
`CODIFY_CLIPBOARD`, never sent to the engine and never in the database, so invariant 7 is untouched. A repeat moves
the item to the top and keeps its pin, its id and where it first came from. It is read back through a validator that
drops a bad entry on its own, **re-runs the secret check** on every entry (someone, or an older build, may have put
a key in the stored list), collapses repeats, demotes pins beyond the ceiling and bounds the whole. A storage that
throws is an empty list on load and a no-op on save, and the first render does not write back what it just read.

### 11.4 What each button does

| Button | What it does | When it cannot |
|---|---|---|
| **Copy again** | Writes the clip to the system clipboard and brings it to the top. | If the system clipboard refuses, the drawer says so and changes nothing. |
| **Insert into the message box** | Puts the clip in the composer at the caret, replacing the selection, **exactly as it is** (`insertAtCaret`: no space is added where two words would touch, because a clip is code or a path), leaves the caret after it and gives the box the focus. It never sends. | Disabled, with the reason, unless a chat view is showing: a browser or terminal tab replaces the composer. |
| **Paste into the terminal** | Goes through xterm's own `paste`, so the shell's bracketed-paste mode applies and the text reaches the shell as ordinary input. | Disabled unless a terminal tab with a running shell is showing, and says which. |
| **Pin** | Keeps the clip first and past the 50 limit. | At 20 pinned the Pin button of every other clip is disabled and says to unpin one first (the model refuses the pin too). |
| **Delete**, **Clear unpinned** | Remove one clip, or every clip but the pinned. | |

* **A multi-line clip is not pasted into a shell that would run each line.** A terminal that is not in
  bracketed-paste mode treats every newline as Enter, so "cd build\nmake" would run both before anyone had read
  either, and the history holds text from anywhere (a page, a model's answer). Any newline counts, a trailing one
  included: that one is the Enter that runs the command. Once the shell has asked for bracketed paste (a modern
  bash, zsh or fish does) the text is wrapped and held back, and goes in. The refusal says why.
  `terminalPaste.ts` holds the rule, and the pane registers itself there for as long as it is mounted.
* **A request to insert is not replayed.** A message box ignores the request that was already pending when it
  mounted, so one that is rebuilt (a tab away and back) does not repeat an insert made into the last one.
* **A message about a press** ("could not copy", "not pasted") belongs to the drawer that was open when it was
  made: it is cleared by the next copy, insert or paste that works, by being dismissed, and when the drawer closes.

### 11.5 Proven, and not

`ui/tests/clipboardHistory.test.ts` is the rules, `clipboardCapture.test.ts` the listeners (a password field, an
off region, text a handler wrote into the event, a paste a handler stopped, listeners balanced on unmount),
`clipboardDrawer.test.ts` the view, `terminalPaste.test.ts` the paste rule and registry, and `clipboardApp.test.ts`
the whole App: the button's place, four drawers one at a time, a code block's Copy through the real transcript, Insert
into the real composer, Paste into a real xterm in and out of bracketed mode, and the lifetime of each message.

Not verified: the events in a **real WebKitGTK window**. jsdom has no `ClipboardEvent` and the tests build theirs
by hand with the one property the code reads, so that the shell's webview delivers `copy`, `cut` and `paste` to
the document, and that xterm writes its selection into the event's `clipboardData`, is read from the specification
and not seen. If a terminal selection does not appear in the history, that is the place to look.

## 12. Split panes (built)

The centre column can show two views side by side: **a chat, a terminal, an editor or a browser page beside any of those**, but never two chats and never two pages. A split is made three
ways: a right-click on a tab (or the Menu key on a focused one), an entry in the command palette (Ctrl+K, type
"split"), and **Ctrl+.**, which splits and, pressed again, closes. The rules are `ui/src/panes.ts`, pure and DOM-free like
`tabs.ts`; the divider and the two panes are `ui/src/components/SplitPanes.tsx`; the hook that keeps the split in step
with the tab strip is `ui/src/useSplit.ts`.

### 12.1 Beside the tabs, never in them

A split is `{ panes: [leftId, rightId], focused: 0 | 1 }` held by `useSplit` **next to** the tab state. `tabs.ts`,
`tabPersistence.ts`, `layoutSync.ts` and the engine's `/shell/tabs` never see it (§2.1: view state is not a `Tab`), so none of
their return shapes can drop it, and it cannot reach storage. `tabState.activeId` keeps meaning *the tab I am working in*, which is
now **the focused pane's tab**, so Ctrl+W, the strip's `aria-selected`, thread hydration and the Ctrl+B terminal passthrough
mean what they did.

### 12.2 What may be paired, and why that is the line

| | |
|---|---|
| Chat + terminal, terminal + terminal, **and any of those with an editor** (§13) and **a browser page** (§12.8) | allowed. An editor shares nothing a second one would fight over: no message box, no running goal, no `insertRequest`. Two editors are two files. A page takes the rectangle the shell was already given: it is the one native view in the column. |
| **Browser page + browser page** | refused. A page is a native child webview seated over one measured rectangle (§7.3): Rust applies one `Bounds` to every page and shows one at a time. Two visible pages would need a rectangle for each and a set of visible pages: the shell changes §12.8 lists, which this does not make. |
| Chat + chat | refused. There is one message box, one "a goal is running" state and one `insertRequest`. |

A refusal is a sentence (`PAIR_REFUSALS`): in a tab's menu the item stays reachable with its reason under it (`aria-disabled`, not
`disabled`, so a keyboard user hears why), and a chord that cannot work says so in a thin `role="status"` line above the panes, which goes with the next change of tab.

### 12.3 When it shows, and what moves it

A split **shows** while the active tab is one of its two. When the active tab is somewhere else (`resolveSplit`):

1. it takes the **focused** pane's place if that is a valid pair; else
2. the **other** pane's place if that is (a chat opened while the terminal had focus replaces the chat, never makes two); else
3. the split **waits**: kept, not drawn, and back as it was when you return to either pane's tab. This is what a **browser page arriving from
   outside** always does, whatever it could pair with: a tab picked out of the strip, the header's Browser button, or a link clicked in a chat
   that is already beside something must not rearrange the split it was reached from. (A page goes beside something only by being asked
   for, §12.8.)

A pane whose tab has gone ends the split. Closing a showing pane's tab ends it and goes to the *other* pane's tab. Ctrl+. with no
split picks who to split with by **distance in the strip, a tie going right** (`splitPartner`): a chat takes the nearest live terminal, editor **or page**, whichever is nearer; an
editor takes the nearest chat (the assistant it is being edited with), then the nearest live terminal, then a page; a terminal takes the nearest other live terminal, then the nearest
chat, then a page, and never picks an editor for you; a page takes the nearest chat (what it is being read for), then the nearest live terminal, then an editor, and never another page;
with nobody, a new terminal opens in the same folder. A shell that has exited is never chosen for you.

### 12.4 The chat follows its tab, not the focus

With the terminal beside it focused, the transcript is still the one on screen: `activeConversationId` is the **chat pane's**
conversation, so hydration, sending and the sidebar's highlight stay with it. Focus is the pane last used: a press in a pane, or focus
arriving in it (the clipboard drawer's Insert focuses the message box, so it focuses the chat pane), moves it, and the strip follows.
Selecting a thread from the sidebar while the terminal has focus replaces the chat pane's tab, rather than rewriting the chat tab in place as it does outside a split.

### 12.5 Room

Each pane is `minmax(22rem, Nfr)` (`MIN_PANE_REM`, a judgement pinned by a test), so the browser holds both at their minimum however the
ratio and the window disagree. The rule in `drawers.ts` is derived and never stored, like §8.1's: the **left panel gives way** to a split the way it does to a
drawer, when the row cannot hold it (`sidebarYields(..., split)`); and when two panes still do not fit (`splitFits`), only the **focused** pane is
drawn and the split is kept, so widening the window or closing a drawer brings the other back. The divider position is the one thing remembered
(`codify.splitRatio`, `splitPref.ts`): it is a view preference, clamped to what both panes need at the window's size when used.
The divider is a `role="separator"`: drag it (it holds the pointer), arrow keys, Home and End, double-click for an even split.
The composer's dropdowns are `position: fixed` and open at their button, which was always inside the window while the composer spanned
it; in the right-hand pane they would run off the screen, so they are pulled back inside it (`clampPickerLeft`, `threadMenu.ts`).

**A split does not come back after a restart.** A split is never written (§12.1), and most pairings contain a terminal or an editor, neither of which is restored (§13.2);
a chat and a page are both restored, and are shown one at a time again, because the pair between them is not.

### 12.6 Terminal panes, two at once

Two panes exposed two things a single pane had hidden. A pane kept **every terminal's output** in its buffer and drained only its own, so
a build in a second shell grew a mounted pane's buffer for as long as it was mounted (and two panes would each have hoarded the other's):
it keeps only its own (`bufferOwnOutput`). And a pane took the keyboard whenever xterm was ready, which would let the second half of
a split take it from the pane in use: `autoFocus` (on by default) is off for a pane that appears unfocused, and clicking still focuses.
The clipboard drawer follows: Insert needs a chat pane in view, Paste goes to the focused terminal (else the one beside the chat), so in chat + terminal both work whichever pane has focus.

### 12.7 Proven, and not

`ui/tests/panes.test.ts` is the rules, `splitPanes.test.ts` the divider, `useSplitFits.test.ts` the measuring, `tabMenu.test.ts` the
menu, `terminalPaneFocus.test.ts` the focus, `pickerClamp.test.ts` the dropdowns, `splitBrowser.test.ts` a page in a split (which page the shell is told to show, the drag, the
overlays, a link in an answer; what it is told is the evidence, since jsdom has no native view), and `splitApp.test.ts` and `splitAppLayout.test.ts` the whole App with real
xterm panes (two files, because forty of them outgrow one process's memory): all three ways in, both views live, the transcript and sending staying with the chat while
the terminal is focused, replacement, closing, the page waiting, the panel yielding and a pane dropping, nothing in storage, the clipboard drawer in a split.

Not verified: the **real window**. jsdom has no layout, so the collapse rule and the divider's maths are tested with the harness's stated
widths, not seen; the feel of dragging, xterm re-fitting while it drags, and focus under the desktop's own shortcuts are for a person to check.
One thing the tests found about xterm itself: it schedules work on a timer shortly after layout and does not cancel it on dispose, so a pane unmounted inside
that window throws from the timer (`reading 'dimensions'`). It is harmless to the app and needs a split closed within a few milliseconds of being made,
so the tests wait a beat; it is the same for a tab opened and closed that fast.

### 12.8 A browser page in a split

A page can be one of the two panes. What makes it different from the others is that it is **not in the DOM**: `BrowserPane` is its address bar and
the rectangle it measures, and the shell seats a native view over that rectangle (§7.3). In a split the rectangle is the pane's, half the column
instead of all of it, and nothing in Rust has to know: the pane reports its own measurements (`onBounds`, from a `ResizeObserver`) as it always did, so
moving the divider, the window's size and the sidebar's give-way all move the page the way they moved it when it filled the column. There is still **one
page on screen at a time**, which is why two pages are refused (§12.2).

What a native view costs, and where each cost is paid:

- **Which page the shell shows is what is drawn, not what is active.** `browser::focus` shows the page it is named and hides the others, and it used to be
  told the active tab's. With a chat beside a page the chat can have the focus, and the active tab is the chat, so the shell is told **the page in the drawn
  split** (`shownPageId` in `App.tsx`). A split that is showing but does not fit draws only the focused pane, and then the page is shown only if it is that pane.
- **The shell is told where the page's pane is as soon as it is measured** (`handleBrowserBounds`): a burst of measurements is sent as its first at once and its
  last when it settles, where it used to wait out a 120 ms quiet period for all of it. A page put beside a chat is shown the moment the shell is told which
  page to show, before its pane has been measured, so a trailing-only send left it over the chat, full-width, for that long.
- **A drag of the divider hides the page for its length** (`SplitPanes`'s `onDragChange`). A native view takes the pointer over its rectangle, so
  a drag that crossed the page would stop receiving moves; this is a precaution, taken whether or not the platform would have kept the capture, and the page
  comes back, at its new size, when the divider is let go.
- **Anything drawn over the column hides the page**: the command palette, a tab's menu and Settings. A native view paints above every DOM overlay (§8.1), so an
  overlay that overlapped the page would be under it. This applies to a page that fills the column as well, where it was an omission.
- **A link in an answer opens beside the answer** when the tab it was clicked in is alone in the column and the pair fits (`splitFits`), with the page focused:
  it is what was just asked for. With a split already showing, or no room, it opens a full-column tab as before and the split waits (§12.3).
- **Clicking inside the page does not move the pane focus.** The page is a native view and the DOM never sees the press; the address bar and the pane's
  header are DOM and do. So the coloured edge can stay on the chat while the keyboard is in the page. Keystrokes go where the toolkit's focus is, so typing is
  right, and Ctrl+W (a DOM shortcut) does not fire from inside a page; what the edge says is the thing that can be wrong. Closing it would need the shell to
  report a focus change on the page's own widget, which is a Rust change.

Not made, and listed so it is a choice: **two pages at once** (a rectangle per page in `browser::resize`, a visibility call that names a set, `note_active` and the
assistant's "page in view" made a set), and **the pane focus following a click in the page**.

## 13. The editor (built)

A fourth kind of tab: **one file, in the centre column**, that can sit beside a chat, a terminal or another editor (§12). The assistant
gets **eyes** and **hands** on it (`read_editor`, `open_in_editor`, `edit_editor`; §10.6) through a bridge that is not specific to files, so
the next surface that needs to be seen and driven (the terminal is the obvious one) is a registration and not a redesign (§13.7). The text is
`ui/src/editorBuffers.ts`, the three operations `ui/src/editorSurface.ts`, the window's half of the bridge `ui/src/surfaceLoop.ts`, the pane
`ui/src/components/EditorPane.tsx`, the engine's half `engine/surfaces.py` and `engine/surface_editor.py`, and the person's door to the disk is
`PUT /workspaces/{id}/file` (`04` §3.0.3).

### 13.1 One file, one tab, and the text lives above the pane

`openEditorTab` dedupes by **(workspace, path)**: a file is never two buffers that could diverge, the way a conversation is never two tabs.
The centre column mounts only the tab in front (and, in a split, two), so a pane that owned its text would lose it whenever its tab was not
showing, and the assistant could not read a file whose tab was in the background. So the text is not in the pane: `editorBuffers.ts` is a
module store above the panes, as `terminalBuffer.ts` is, holding per tab a CodeMirror **`EditorState`** (text, selection and undo history), the version
read from disk and the ranges the assistant changed. The pane is a window onto it: showing another tab and coming back keeps what was typed and how to undo it.

Because the state is plain data, the assistant's hands and eyes run **with no view at all**. When a view is mounted the store hands edits to it
(`attachView`) so the person sees them and undoes them in the same history; when none is, the same transaction is applied to the state. `applyTransactions`
is the one place the store learns of a change, whoever made it.

CodeMirror is a **lazy chunk**, as xterm is: `EditorPane` imports only the *types* of `editorMount.ts`, which is `import()`ed when a pane mounts, and each
language (JavaScript and TypeScript, Python, JSON, Markdown, CSS, HTML; anything else plain) is a chunk of its own. An app that never opens a file never
loads an editor. The theme reads the `--codify-*-rgb` variables the other panes use and follows a theme or scale change.

### 13.2 Local only: nothing about an editor is stored or restored

An editor tab is **local, like a terminal** (`isLocalTab`): it has no key, is not in `CODIFY_TABS`, and the engine's `/shell/tabs` never hears of it (that route
closes `kind` to `chat | browser`, and a fourth kind there would be a 422 retry loop on an older engine and a 500 on an older engine reading a newer database). There
are three places that used to treat "not a terminal, not a chat" as "a browser page" (`persistedTab`, `layoutSync`'s `encodeTab`, `ensureKeys` and `planChanges`, and
`App`'s clip and pane code); each has an explicit editor branch, and a test that an editor never reaches the storage key or the engine.

So **a restart reopens no editors**, and neither does it a split that contained one (§12.5). **Unsaved text is lost if the window is killed.** Tauri has no
close prompt this can rely on, so the app does what it already does elsewhere: closing the *tab* asks (`window.confirm`; Ctrl+W is the same close), closing the window does not.
That is stated here because it is a loss a person can suffer.

### 13.3 Saving is the person's, and says what it would overwrite

Save is **Ctrl+S inside the editor** and a button; it is not a window chord. It sends `{path, content, base_version}` and the engine replaces a file that exists
(`04` §3.0.3). What the editor adds is what a text file needs to survive being edited as text:

- A file is edited as **LF**. A byte-order mark is stripped and put back, and CRLF is restored on save. A file with **mixed endings, or a bare CR,** is refused
  with a sentence rather than normalised, because saving it would change lines the person never touched.
- *Dirty* means **the text differs from the disk**, not "was typed in": typing a character and deleting it is clean again.
- A **409 `file_changed`** is a conflict, not an error: the banner offers *Reload from disk* (discard the text here) and *Keep my version*, which adopts the
  version now on disk as the base, so the **next** Save overwrites. Nothing is overwritten by pressing one button once.
- *Revert* asks before it throws text away; an editor over a file that **did not open** shows the error and *Try again* and never an empty buffer you could type into and save over the real file.
- The 1 000 000 byte cap is the engine's, and is enforced on what the assistant's edit would produce as well as on what is read.

### 13.4 Eyes and hands: the surface bridge

The assistant cannot see the window: `TurnCreate` and `GoalCreate` carry no context, and invariant 8 means a client chooses nothing about what a turn becomes. So the
window does what the shell does for the browser (§7.5): **the engine asks, the window answers.** `engine/surfaces.py` holds the questions, the window long-polls
`GET /surfaces/next` (held for `POLL_WAIT_S`, 20 s) and posts `POST /surfaces/answer`. `GET /surfaces/state` says whether a window is attached, which is **polling in the last
60 s** and nothing else.

- **Operations are fixed strings in a table**, `{surface: {op: Op(name, args model, result model)}}`, and the model chooses among them and never makes one. Arguments are
  validated **engine-side, before they cross**; an answer is validated into a strict result model with caps (`MAX_LINES` 400, a line cut at 2 000 characters, 50 editors,
  a selection at 2 000 characters), and an answer for an id that was not issued, or was already answered, or came late, is dropped. `SurfaceAnswer` is `extra: forbid`.
- **File text reaches the model as quoted text**, in a fixed shape, with the sentence that it is the person's text and not an instruction.
- **Failure is a sentence.** No window attached, no answer in 15 s, a refusal: each is returned to the model as words and ends nothing. The questions are handed out once each, in order,
  so several in flight do not cross.
- **The window's loop** (`runSurfaceLoop`) backs off from 1 s to 10 s when the engine cannot be reached, and does not spin on a poll that came back instantly with nothing. A poll is not counted
  in the app's own engine calls in the tests (it is background traffic, as the goal's WebSocket is).

The three tools are always on the conductor's menu and are not stage moves: they cost model calls and nothing else. **None of them touches the filesystem**, and a test proves it with
the writers patched to raise. `edit_editor` replaces text in the **open buffer** like the fixer's `edit` op (absent, or occurring other than `count` times, is a refusal that says so),
as **one undoable step marked as the assistant's**, and never saves; it opens the file in the background if it is not open and closes it again if the edit did not apply.
Invariant 9 is therefore unchanged for an agent, and widened by exactly one door, which is a person's and which an agent cannot reach (`docs/00` §6.9).
A turn whose only change was such an edit is **not** told "no file was changed" (`run_chat` withholds that warning when `edit_editor` landed text, counted per goal
in `ExecutorService._editor_edits` and read when the run ends): the conductor did what was asked, in the one place it may, and "ask again" would send the person back to a
request that was carried out. A refused edit, or no edit, still gets the warning, because that is the case it exists for.

### 13.5 Where an opened file goes, and what it never does

`open_in_editor` is the assistant pointing. The rule is in `App.tsx` (`editorHost.openFile`) and is small on purpose:

1. If the file's tab is already shown (in front, or in the split), say so and select the lines.
2. If the person is **looking at a chat, nothing is split, and the pair fits** (`splitFits`), the file goes **beside** that chat, made with **the chat focused**.
3. Otherwise (a split already showing, a terminal or a page in front, no room) the tab opens **in the background** and the strip marks it.

It never takes the keyboard, never changes `activeId`, and never rearranges a split the person made. An **edit** moves nothing at all. The tab shows a dot while the text is
unsaved and a second marker while it holds text the assistant changed (cleared by the next Save), and the pane shows the changed ranges until then. The
diff card in a run's transcript has an **Open** link that opens its file the same way a person would (in front, which is their choice).

### 13.6 A file the fixer changed reaches an editor that has it open

On the goal stream's `file_change_summary` (not a dry run), an editor with **clean** text for a touched path reloads silently; one with unsaved text is **flagged** and keeps the person's
text (*Reload from disk* / *Keep my version*, as for a 409). A path no editor has open asks the engine for nothing.

### 13.7 Adding a surface

The next surface (the terminal's screen, a page) is meant to be one of these and not a new mechanism:

1. **Engine:** an `Op` table for the surface (`name`, an args model with `extra: forbid`, a result model with caps) registered in `surfaces.py`; handlers in `conductor_tools.py`
   that call `SurfaceBridge.ask`; the tool specs in `conductor.py` and one sentence in the prompt (`test_conductor_prompt` fails if a tool is not named, or the prompt outgrows its ceiling).
2. **Window:** `registry.register("<surface>", handlers)` where the loop is started in `App.tsx`; handlers answer from state that exists above the component (a store, not a mounted view).
3. **Say what it may not do, in a test:** the proof that none of it reaches the filesystem or the sandbox, and a source-scan if it is a new door (§13.4 is held by
   `TestAPersonsSaveIsTheOneOtherDoor`).
4. **Docs:** a row in §10.6 and `04` §9.1, and the not-verified list here.

### 13.8 Proven, and not

Proven: `tests/test_file_door.py`, `test_workspace_files.py` (the routes and the door), `test_surfaces.py`, `test_surface_editor.py`, `test_surface_routes.py`, `test_editor_tools.py` (the bridge, the
operations and the no-disk proof) on the engine; `ui/tests/editorBuffers.test.ts` (text, endings, dirty, ranges, conflicts), `editorSurface.test.ts`, `surfaceLoop.test.ts`, `editorPane.test.ts`, `editorTab.test.ts`,
`tabBarEditor.test.ts`, and the whole App with **real CodeMirror** in `editorApp.test.ts` and `editorAppSurface.test.ts` (two files for the reason §12.7 gives), `diffOpen.test.ts` for the diff card.

Not verified: the **real window**. jsdom has no layout, so selection, scrolling, input methods (IME), and the feel of a split with a CodeMirror pane are for a person to check in WebKitGTK under the real compositor; the
only real renderer that has run this is Chromium. Dirty buffers are lost if the window is killed (§13.2). The window now **polls `/surfaces/next` for as long as the app and the engine are up**, an always-on request that is cheap and is
the one new thing a reviewer should look at. The conductor prompt has about 180 characters of room under its ceiling, so the next tool must trim wording and not raise it.
