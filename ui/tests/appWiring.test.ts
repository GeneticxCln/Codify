/**
 * What the mounted App does when you use it.
 *
 * These replace tests in `shell.test.ts` that proved the same claims by reading
 * `App.tsx` and `Sidebar.tsx` as text ("`App.tsx` is not mounted by anything in
 * this suite, so the move itself is only checkable by reading it"). It is now:
 * each claim below is checked by clicking the control and looking at what the
 * app asked the engine or the shell to do, which is the thing the claim is about.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { EngineCall } from "./appHarness.ts";

const { withApp, conversation } = await import("./appHarness.ts");

const newTabButton = (root: HTMLElement): HTMLButtonElement => {
  const button = root.querySelector('button[title^="A new, empty tab"]');
  assert.ok(button, "the header has no New Tab control");
  return button as HTMLButtonElement;
};

const posts = (engine: EngineCall[], path: string) =>
  engine.filter((c) => c.method === "POST" && c.path === path);

test("New Tab creates nothing, so it cannot disturb the project's threads", async () => {
  // A tab is a project's window; the first prompt makes the thread. Pressing the
  // button to look around used to leave an empty conversation behind and a new
  // row in the panel.
  await withApp({}, async ({ dom, engine, settle, tabs }) => {
    assert.equal(tabs().length, 0, "the strip should start empty");
    await dom.click(newTabButton(dom.container));
    await settle();
    assert.equal(tabs().length, 1, "New Tab did not open a tab");
    assert.equal(posts(engine, "/conversations").length, 0, "New Tab created a thread on the engine");
  });
});

test("New Tab sits beside the CODIFY badge, and the strip does not own it", async () => {
  await withApp({}, async ({ dom, settle }) => {
    await dom.click(newTabButton(dom.container));
    await settle();
    const header = dom.container.querySelector("header");
    assert.ok(header, "the app has no header");
    const button = newTabButton(dom.container);
    const strip = dom.container.querySelector('[role="tablist"]');
    assert.ok(strip, "there is no tab strip");
    assert.ok(header.contains(button), "New Tab is not in the header");
    assert.ok(header.contains(strip), "the strip is not beside CODIFY in the header");
    assert.ok(!strip.contains(button), "the strip owns a second way to open a tab");

    const before = (a: Node, b: Node): boolean =>
      Boolean(a.compareDocumentPosition(b) & dom.window.Node.DOCUMENT_POSITION_FOLLOWING);
    const brand = [...header.querySelectorAll("*")].find(
      (el) => el.children.length === 0 && el.textContent?.trim() === "CODIFY",
    );
    assert.ok(brand, "the CODIFY badge is missing");
    assert.ok(before(brand, button), "New Tab is not after the CODIFY badge");
    assert.ok(before(button, strip), "New Tab is not before the strip, so the strip pushes it away");
  });
});

test("the header no longer owns Browser, Terminal and Settings; the panel does", async () => {
  await withApp({}, async ({ dom, shell, settle, tabs }) => {
    const header = dom.container.querySelector("header") as HTMLElement;
    const labelled = (root: Element, prefix: string): HTMLElement | null =>
      root.querySelector(`button[title^="${prefix}"], button[aria-label^="${prefix}"]`);
    for (const name of ["Browser", "Terminal", "Keys", "Settings"]) {
      assert.equal(labelled(header, name), null, `the header still has a ${name} button`);
    }
    // The header no longer says which folder the window is in either: a tab does.
    assert.ok(!/Alpha|e2e-alpha/.test(header.textContent ?? ""), "the header still names the current folder");

    // The panel has them, and they work: each does its job when pressed.
    const terminal = dom.container.querySelector('button[title^="Terminal"]') as HTMLElement | null;
    assert.ok(terminal, "the panel has no Terminal control");
    await dom.click(terminal);
    await settle();
    assert.ok(shell.calls.includes("codify_terminal_open"), "Terminal did not ask the shell for a terminal");

    const browser = dom.container.querySelector('button[title^="Browser"]') as HTMLElement | null;
    assert.ok(browser, "the panel has no Browser control");
    const before = tabs().length;
    await dom.click(browser);
    await settle();
    assert.equal(tabs().length, before + 1, "Browser did not open a browser tab");
  });
});

// ── the thread panel's right-click menu ──────────────────────────────────

const TWO_THREADS = [
  conversation({ id: "c1", title: "Refactor the parser" }),
  conversation({ id: "c2", title: "Add an index" }),
];

const rows = (root: HTMLElement): HTMLElement[] =>
  [...root.querySelectorAll('[data-thread-row="true"]')] as HTMLElement[];

const rightClick = async (
  dom: { window: Window & typeof globalThis },
  target: Element,
  act: (body: () => void) => Promise<void>,
): Promise<void> => {
  await act(() => {
    target.dispatchEvent(
      new dom.window.MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 60, clientY: 90 }),
    );
  });
};

/** React's `act`, so an event that sets state is flushed before we look. */
const inAct = async (body: () => void): Promise<void> => {
  const React = (await import("react")).default;
  const act = React.act as (b: () => Promise<void> | void) => Promise<void>;
  await act(async () => {
    body();
  });
};

test("a right-click on a row opens the menu for that row's thread, and its actions act on it", async () => {
  await withApp({ conversations: TWO_THREADS }, async ({ dom, engine, settle }) => {
    const threadRows = rows(dom.container);
    assert.equal(threadRows.length, 2, "the panel did not list the project's threads");
    const second = threadRows.find((r) => r.getAttribute("data-thread-id") === "c2");
    assert.ok(second, "the row does not say which thread it is");

    await rightClick(dom, second, inAct);
    await settle();
    const menu = dom.container.querySelector('[role="menu"]');
    assert.ok(menu, "a right-click on a row did not open the menu");
    assert.match(menu.textContent ?? "", /Add an index/, "the menu is not about the thread that was clicked");

    const archive = [...menu.querySelectorAll('[role="menuitem"]')].find((i) => /archive/i.test(i.textContent ?? ""));
    assert.ok(archive, "the menu offers no Archive");
    await dom.click(archive);
    await settle();
    const archived = posts(engine, "/conversations/c2/archive");
    assert.equal(archived.length, 1, "Archive did not act on the thread the menu was opened on");
    assert.equal(posts(engine, "/conversations/c1/archive").length, 0, "Archive acted on the wrong thread");
  });
});

test("a right-click on empty panel opens a menu with New thread alone", async () => {
  await withApp({ conversations: TWO_THREADS }, async ({ dom, settle }) => {
    const panel = rows(dom.container)[0]?.parentElement;
    assert.ok(panel, "no thread panel to click");
    // Empty space in the panel, not a row: the panel itself is the event target.
    await rightClick(dom, panel, inAct);
    await settle();
    const menu = dom.container.querySelector('[role="menu"]');
    assert.ok(menu, "a right-click on empty panel did not open the menu");
    const items = [...menu.querySelectorAll('[role="menuitem"]')].map((i) => i.textContent?.trim() ?? "");
    assert.equal(items.length, 1, `expected New thread alone, got ${JSON.stringify(items)}`);
    assert.match(items[0], /New thread/);
  });
});

test("the menu's New thread makes a thread ON the chat, not a new chat", async () => {
  // The failure this pins, in the shape it had: "New thread" wired to the new-chat
  // button's handler created a conversation with no parent — a brand-new chat
  // wearing a thread's name.
  await withApp({ conversations: TWO_THREADS }, async ({ dom, engine, settle }) => {
    const second = rows(dom.container).find((r) => r.getAttribute("data-thread-id") === "c2");
    assert.ok(second);
    await rightClick(dom, second, inAct);
    await settle();
    const item = [...dom.container.querySelectorAll('[role="menuitem"]')].find((i) =>
      /new thread/i.test(i.textContent ?? ""),
    );
    assert.ok(item, "the menu offers no New thread");
    await dom.click(item);
    await settle();
    const made = posts(engine, "/conversations");
    assert.equal(made.length, 1, "New thread did not create exactly one thread");
    assert.equal(made[0].body?.parent_id, "c2", "the new thread was not created on the thread that was right-clicked");
  });
});

// ── a thread's first prompt is its name ──────────────────────────────────

const { threadTitleFromPrompt } = await import("../src/threadTitle.ts");

const composer = (root: HTMLElement): HTMLTextAreaElement => {
  const box = root.querySelector("textarea");
  assert.ok(box, "the composer is missing");
  return box as HTMLTextAreaElement;
};

const PROMPT = "add a login page to the app";

/** A remembered strip with one chat tab open on this thread. */
const stripOn = (conversationId: string) => ({
  version: 1,
  layout: { tabs: [{ key: "k_test_1", kind: "chat", conversationId, workspaceId: "ws-a" }], activeIndex: 0 },
  pendingRemovals: [],
  pendingWrites: [],
});

test("a thread started by a prompt is named by the shared rule, not a raw slice", async () => {
  await withApp({}, async ({ dom, engine, settle }) => {
    await dom.fill(composer(dom.container), PROMPT);
    await dom.press(composer(dom.container), "Enter");
    await settle();
    const made = posts(engine, "/conversations");
    assert.equal(made.length, 1, "the first prompt did not create a thread");
    assert.equal(made[0].body?.title, threadTitleFromPrompt(PROMPT));
    assert.equal(posts(engine, "/conversations/c-new-1/turns").length, 1, "the turn was not sent on that thread");
  });
});

test("a turn names an unnamed thread from its prompt, and the thread's tab follows", async () => {
  const unnamed = conversation({ id: "c-open", title: "" });
  await withApp({ conversations: [unnamed], storedTabs: stripOn("c-open") }, async ({ dom, engine, settle, tabs }) => {
    await dom.fill(composer(dom.container), PROMPT);
    await dom.press(composer(dom.container), "Enter");
    await settle();
    const renamed = engine.filter((c) => c.method === "PATCH" && c.path === "/conversations/c-open");
    assert.equal(renamed.length, 1, "an unnamed thread was not named by its first prompt");
    assert.equal(renamed[0].body?.title, threadTitleFromPrompt(PROMPT));
    assert.ok(
      tabs().some((t) => (t.textContent ?? "").includes(threadTitleFromPrompt(PROMPT))),
      "the thread was named on the engine but its tab still shows the old name",
    );
  });
});

test("a turn never renames a thread that already has a name", async () => {
  // Without the guard, a thread the user renamed would be overwritten by the
  // prompt on every later turn.
  const named = conversation({ id: "c-open", title: "Refactor the parser" });
  await withApp({ conversations: [named], storedTabs: stripOn("c-open") }, async ({ dom, engine, settle }) => {
    await dom.fill(composer(dom.container), PROMPT);
    await dom.press(composer(dom.container), "Enter");
    await settle();
    assert.equal(posts(engine, "/conversations/c-open/turns").length, 1, "the turn was not sent");
    assert.equal(
      engine.filter((c) => c.method === "PATCH").length,
      0,
      "a thread that already had a name was renamed by a later prompt",
    );
  });
});

test("a thread that has a parent says which, in the panel", async () => {
  // The engine names the parent (`parent_title`); the row says "on “<title>”", and
  // says just "thread" when the parent is not known — never nothing.
  const known = { ...conversation({ id: "c2", title: "Add an index", parent_id: "c1" }), parent_title: "Refactor the parser" };
  const orphan = conversation({ id: "c3", title: "Loose end", parent_id: "gone" });
  const top = conversation({ id: "c1", title: "Refactor the parser" });
  await withApp({ conversations: [top, known as never, orphan] }, async ({ dom }) => {
    const row = (id: string): HTMLElement => {
      const found = [...dom.container.querySelectorAll('[data-thread-row="true"]')].find(
        (r) => r.getAttribute("data-thread-id") === id,
      );
      assert.ok(found, `the panel did not list ${id}`);
      return found as HTMLElement;
    };
    assert.match(row("c2").textContent ?? "", /on “Refactor the parser”/, "a child row does not name its parent");
    assert.match(row("c3").textContent ?? "", /thread/, "a child of an unknown parent says nothing about being a child");
    assert.doesNotMatch(row("c1").textContent ?? "", /on “|thread/, "a top-level row claims a parent");
  });
});

test("the empty transcript's skip link lands on the composer that is on screen", async () => {
  // A skip link is two halves that have to agree: the link's `href` and the
  // element it names. Renaming one without the other produces a link that
  // silently does nothing, so this asks the mounted app whether the id the link
  // points at is carried by the composer.
  await withApp({}, async ({ dom, settle }) => {
    await settle();
    const link = [...dom.container.querySelectorAll('a[href^="#"]')].find((a) => /prompt/i.test(a.textContent ?? ""));
    assert.ok(link, "the empty transcript renders no skip link to the composer");
    const target = dom.container.querySelector(`[id="${link.getAttribute("href")!.slice(1)}"]`);
    assert.ok(target, `the skip link points at ${link.getAttribute("href")} and nothing on screen carries that id`);
    assert.ok(
      target.querySelector("textarea") || target.tagName === "TEXTAREA" || target.closest("form, div")?.querySelector("textarea"),
      "the skip link's target is not the composer",
    );
  });
});
