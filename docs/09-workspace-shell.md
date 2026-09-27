# Codify — the workspace shell: conversations, tabs, terminal, browser

**Status: conversations, tabs, the terminal and the browser are all built, and
so are both panes.** The plan approved for this work had five phases; the first
four shipped, and what is here is what they describe — the schema and routes of
§2–§3, `src-tauri/src/terminal.rs` plus the xterm pane that drives it in §7.1,
the browser's isolation in `src-tauri/src/browser.rs` and
`src-tauri/capabilities/browser.json` in §7.2, and the pane that drives that in
§7.3.

**What is still not built is an embedded page.** A browser tab is a separate OS
window, so its pane is an address bar and not a viewport. §7.3 explains why that
is a decision rather than an omission, and what it would cost to change.

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
type TabKind = "chat" | "terminal" | "browser";
interface Tab {
  id: string;
  kind: TabKind;
  title: string;
  conversationId?: string;   // the thread a chat tab shows
  workspaceId?: string;      // the folder this tab belongs to
  url?: string;              // a browser tab's current address
  // plus `history`, `ptyId` and `exited` for the browser and terminal kinds
}
interface TabState { tabs: Tab[]; activeId: string | null }
```

Settled decisions:

- **One tab per thread.** The strip grows with the threads you have open, and
  starting a new thread adds a tab rather than replacing the one you were
  reading. A single shared "Chats" tab was tried and reversed: it looked tidier
  and it was wrong in the way that matters — a new thread silently took over the
  chat column, and the strip held no record of what was open. The side panel
  already lists every thread, so the strip's job is not to be a second list of
  them; it is the set of things currently open, which for a chat means the
  thread. `openConversation(state, conversationId, title)` is the only way in.
- **Opening a thread you already have open focuses its tab; it does not open a
  second one.** `tabForConversation` keys on `conversationId`, so two tabs on one
  conversation — two live event streams over the same goal — are unreachable.
  That is a *deduplication*, not a cap: there is no fixed number of chat tabs.
  Terminals and browser tabs are unaffected — those are one tab per live
  instance, because two terminals are two shells and two browser tabs are two
  webview windows, and collapsing either would make the second unreachable.
- **A thread is called by its name, by its folder, or by nothing else.** The
  engine stores no title until something gives a thread one, so the strip and
  the panel both need a fallback and must use the *same* one: `threadLabel` in
  `threadTitle.ts`, used by `TabBar` and `Sidebar`. A named thread is called by
  its name. An **unnamed** one is called by its **folder** — the workspace's
  short name, never its path. `"New chat"` is the last resort, for a thread that
  has neither.
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
- **A thread's first prompt is its name.** This became load-bearing with one tab
  per thread, and it is the difference between the feature working and not
  working at all: a thread made by "New chat" starts unnamed, and the engine
  stores exactly what it is told, so three untitled threads are three tabs all
  labelled "New chat" and opening one is indistinguishable from opening
  nothing. `threadTitleFromPrompt` (`ui/src/threadTitle.ts`) collapses the
  prompt's whitespace and cuts it at 60 characters **on a word boundary** — a
  tab is 224px wide, and "so every row h" is a name you read twice. The turn
  calls it only for a thread it can see is unnamed, so a name the user chose is
  never overwritten, and `nameThread` moves the *tab* with the rename, because a
  name the engine accepted that the strip never learned about is the same
  invisible-thread problem one layer down.
- **Archiving a thread closes its tab** (`closeConversation`). A tab left
  pointing at an archived conversation is a transcript with no row above it. A
  thread that was never opened has no tab and changes nothing — which is the
  ordinary case, and the reason the lookup is by thread and not "close
  something".
- **Closing lands on the tab to the left**, or the one that slid into the slot
  when the closed tab was first. A transcript read to the bottom leaves you
  wanting the tab you were on before.
- **Closing the last tab leaves `activeId: null`.** An empty shell is the honest
  state; inventing a tab to land on would mean closing everything and still
  looking at a transcript. "New chat" is the way back.
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
`browser.rs`'s ACL test fails the build's test leg when the two lists drift.

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
  freezing mid-answer.

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
  process holding the workspace, the same defect the engine kill prevents.

Commands: `codify_terminal_open`, `codify_terminal_write`,
`codify_terminal_resize`, `codify_terminal_close`. Events: `terminal-output`,
`terminal-exit`. Eight Rust tests pin the refusals and the reaping; the freeze is
mutation-tested — a rogue `Command::new(...).spawn()` in `src-tauri/src/` fails
`tests/test_no_unguarded_spawns.py`.

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
`terminalHistory.test.ts` covers the scrollback below; `terminalPane.test.ts`
covers the markup and freezes the decisions a static render cannot reach. **The
PTY itself is Rust's** and its tests live in `src-tauri/src/terminal.rs` — the
UI suite never spawns a shell, and a terminal that a user has to trust is one
whose refusals are tested next to the code that refuses.

#### Scrollback that outlives the tab

Closing a terminal tab reaps the shell, so the text the user was reading went
with it, and reopening the tab gave a bare prompt. `ui/src/terminalHistory.ts`
keeps a **bounded tail of the output stream, per workspace, in memory for the
session**, and a reopened pane writes it back into a fresh xterm before the live
shell says anything.

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

`src-tauri/src/browser.rs`, `src-tauri/capabilities/browser.json`,
`src-tauri/permissions/shell.json`, commands
`codify_browser_open` / `codify_browser_navigate` / `codify_browser_close`.
One webview window per browser tab, labelled `browser-<tab>`. This is the first
surface in Codify that renders untrusted content; every other pixel is
engine-owned or shell-owned. Four layers of isolation, and the Rust tests in
`browser.rs` pin them:

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
origin. `src-tauri/permissions/shell.json` is now the manifest — thirteen
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

**The close signal.** A browser tab's webview is a real OS window, so it can be
closed without the tab bar's permission — the user clicking that window's own
close button — and nothing about that passes through the strip. `open()`
registers [`reports_closed`] on the window and emits `browser-window-closed`
carrying the **tab id**, not the webview label: the label is Rust's business,
the tab id is the UI's, and the other end should not have to know how webviews
are labelled to close the right tab. The main window turns that into a tab
close through `closeBrowserTab`, which lands on the same neighbour any close
does and refuses an id that is not a browser tab.

- **Broadcast like the terminal's events, received only by the main window.** A
  browser webview holds an empty capability set, so it could not have registered
  a listener for one in the first place.
- **`Destroyed`, not `CloseRequested`.** A close can be *asked for* and still be
  prevented; announcing a tab closed for a window that is still open would drop
  the tab and leave the page running with nothing pointing at it.
- **Both directions are the same event.** Closing the tab in the strip calls
  `codify_browser_close` through `handleCloseTab` — the one seam the strip's
  button, ⌘W and the pane all go through (§7.3) — and the `Destroyed` that
  follows arrives back as an event the handler ignores, because the tab is
  already gone. So the two closes are repeatable in any order, and nothing needs
  to know which side started it. Two seams would be two ways to orphan a
  running page.

Pinned by `a_close_is_reported_on_destroyed_and_on_nothing_else` (a tab is
never dropped for a resize or a focus change), `the_closed_payload_names_the_tab`
(the wire shape), and `the_ui_listens_for_the_event_this_module_emits` — a test
that reads `ui/src/shellEvents.ts` and fails if the two ends of the name ever
drift, since no type system spans a Rust constant and a TypeScript string. On the
UI side, `ui/tests/shellEvents.test.ts` pins that a malformed payload names no
tab at all, and that subscribing outside the desktop shell is a no-op rather than
a throw.

### 7.3 The browser pane — built

`ui/src/components/BrowserPane.tsx`, driven by `ui/src/browserHistory.ts`,
`ui/src/tabs.ts` and three calls in `ui/src/api.ts`
(`openBrowserWebview` → `codify_browser_open`, `navigateBrowserWebview` →
`codify_browser_navigate`, `closeBrowserWebview` → `codify_browser_close`).
`App.tsx` renders the pane **instead of** the transcript and composer when the
active tab is a browser tab, and the header's Browser button opens a tab with no
address.

**The pane is the address bar, not a viewport, and it says so on screen.** The
page is a separate OS window (§7.2), so this is chrome with no document under
it — the pane carries a line saying the page is in its own window rather than
leaving the user to work out why. That is the honest shape for what the shell
builds, and it is worth being explicit about the alternative:

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
| The loopback rule is **not** reimplemented here | `browser.rs` is the authority and its message is shown verbatim. A second implementation in TypeScript would be the one that rots, because nothing would test it against the Rust original. |

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
the same events twice; two browser tabs are two webview windows with two
histories, and collapsing them would make the second unreachable.

**What the suite cannot do.** `browserHistory.test.ts` covers the stack and
`browserPane.test.ts` the markup, but the harness renders statically: it runs no
effects and no event handlers, so nothing presses Back. The delivery path
(Enter in the address bar reaching a real window) needs a display and is not
exercised here at all. What the freezes in `browserPane.test.ts` *do* cover is
the wiring that was silently wrong: the id handed to `codify_browser_open` is
the tab's own id, and a refusal is keyed to a tab that exists. Both were found
by typing an address into a running dev server and watching nothing happen —
neither had a test, and a test that renders the pane in isolation could not have
found either.

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

## 8. The keyboard layer (built)

`ui/src/shortcuts.ts` maps a keystroke to a shell action, `ui/src/commandPalette.ts`
holds what ⌘K can find, `ui/src/components/CommandPalette.tsx` renders the
overlay, and `App.tsx` dispatches through one capture-phase `window` listener.
All three logic modules are pure and tested in `ui/tests/` (`shortcuts.test.ts`,
`commandPalette.test.ts`), for the reason `tabs.ts` is: the mapping is a table
of decisions — AltGr, shifted digits, what ⌘9 means — that markup cannot show.

| Keystroke | Acts as |
|---|---|
| ⌘/Ctrl+T | New tab — a new conversation, the same act as the sidebar's "New chat" |
| ⌘/Ctrl+W | Close the active tab, landing on the left neighbour (§4 arithmetic) |
| ⌘/Ctrl+1..8 | Focus the tab in that strip position |
| ⌘/Ctrl+9 | Focus the **last** tab — the browser convention, so a strip past nine stays reachable at its end |
| ⌘/Ctrl+K | Command palette: open tabs, every conversation in this workspace, and both settings destinations (`Provider keys & endpoints`, `Agent roles & prompts`), token-filtered with title-prefix hits ranked first |

Decisions, and why:

- **The modifier is Cmd *or* Ctrl, either** — one rule, no platform sniffing.
  `Alt` never fires a shortcut: AltGr reports Ctrl+Alt together, and without
  that guard AltGr+T on a European layout would open a tab mid-sentence.
- **Letters require no Shift** (⌘⇧T is somebody else's reopen-last-tab
  muscle memory); **digits ignore Shift** and are read from `code`, so the
  physical `1` key works on layouts where `key` is `!`.
- **The listener runs in the capture phase** — a shell shortcut beats the
  focused control — and consumes its keystroke with `preventDefault`. ⌘W closes
  the active tab through the same `handleCloseTab` seam as the strip's close
  button, because a browser tab's webview is a separate OS window that has to be
  told to go (§7.2); the listener therefore re-binds when the tab set changes,
  which is cheap, and buys the single seam that keeps a page from being orphaned
  by closing its tab one way and kept alive by closing it another.
- **Escape belongs to the palette alone** while it is open: the input stops
  its propagation, because `SettingsModal` and the model dropdowns close on a
  *window-level* Escape and one keystroke must never close two surfaces. The
  panel also prevents mousedown's default so a click inside it cannot blur
  the input onto `<body>`, where Escape would miss the palette entirely — and
  focus returns to the control ⌘K interrupted when the palette closes.
- A thread with no title is "New chat" in the palette, matching
  `Sidebar.threadTitle` — two surfaces that named the same row differently
  would be a bug the eye would find first.

Honest limit: this layer is registered in the DOM, the *last* stop — an OS
that consumes a keystroke before the webview reaches it (a macOS build where
⌘W natively closes the window, say) degrades to that platform default. The
tests pin the mapping, not the platform.

`TabBar`'s module docs promise exactly these tab shortcuts; this section is
the half that keeps the promise.

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
code change the user believed was acted on. And when a conductor is configured
but *fails* — its model errors, or it spends its whole call budget without
producing an answer or a plan — that same pipeline runs as the floor, and the
transcript says so.

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
| `git_history` | `GitService.read_only` | an explicit subcommand list, not a prefix rule |
| `run_command` | `SandboxService.run_command` | `validate_argv`, `test` mode (docs/00 §6.6) |
| `recon` | `ExecutorService._librarian` | read-only, bounded rounds |
| `design` | `ExecutorService._design` | no tools at all; decides from the evidence |
| `plan` | the planner | refuses without evidence; writes steps, never files |
| `write` | `ExecutorService._fixer` | docs/00 §6.9 — the only move that touches the filesystem, and it refuses while the goal is unapproved |
| `verify` | `ExecutorService._verifier` | `validate_argv`, `test` mode — the second door, same allowlist |
| `review` | `ExecutorService._critic` | approve or request changes; cannot write |
| `summarize` | `ExecutorService._scribe` | commits, and only after `review` approved |
| `use_skill` | `engine/skills.py` | none — a skill is data, never a capability |

So the conductor gains *choice* over existing powers, never *new* ones. There is
still no `write_file` and no `commit`: the move that writes is the fixer's own
method under the fixer's own validation, and the move that commits is the
scribe's after the critic approved. §10.14 is the section on how that holds when
the model — not the code — is choosing the order.

`git_history` is worth calling out: it is an allowlist of subcommand *names*, not
a prefix rule, because `log` is safe and `log --output=x` is not. The caller is a
model, and "the model asked for it" is not a reason to run `git commit`.

### 10.7 The loop terminates, and says so

`conductor_max_turns` (default 8, settings-clamped to 1..40) bounds *model
calls*, because that is what costs money. The bound is a hard stop, not advice:
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
approved plan back to the same conductor to execute. That run is re-derived from
rows — the goal, its steps, the conversation — rather than a persisted
transcript, which is the same choice §10.5 already makes for turn history.

**The recipe is the floor.** If the conductor's model errors, or it spends its
whole call budget without producing an answer or a plan, `run_chat` runs the
sequence it would have run before the conductor existed and says that it did. If
the conductor drives an approved plan but leaves steps open, the engine finishes
them. A model that is bad at this therefore costs a plan some time and nothing
else, and `conductor_drives_execution = 0` turns the arrangement off without a
rebuild.

One distinction the design rests on, and the reason declining and failing are
modelled separately: **a conductor that declines is obeyed; a conductor that
fails is caught.** Judging that no change is needed is a decision, and running
the recipe over the top of it would make the brain a suggestion. Producing
neither an answer nor a plan is not a decision, and falling back beats failing
the turn. `TestDecliningIsObeyedAndFailingIsCaught` holds both.

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

The honest conclusion from 3 and 5: **the architecture is sound and a 7B local
model is not good enough to drive it reliably.** That is the risk this section
named before it was built, it is why the recipe is the floor, and it is why
`conductor_drives_execution` exists. A model that can call three tools in a row
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
