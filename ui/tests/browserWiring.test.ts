/**
 * What the mounted App does about a browser page.
 *
 * `browserPane.test.ts` renders `BrowserPane` on its own; what it cannot see is
 * `App.tsx` putting it to work. These tests replace a set that read `App.tsx`
 * as text for the decisions below: they open a browser tab, type an address,
 * close the tab or emit the shell's page events, and assert on what the app
 * then asked the shell to do and what it put on screen.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withApp } = await import("./appHarness.ts");
import type { AppContext } from "./appHarness.ts";

const VIEWPORT = { width: 900, height: 600 };
const stored = (tabs: Array<Record<string, unknown>>, activeIndex = 0) => ({
  version: 1,
  layout: { tabs, activeIndex },
  pendingRemovals: [],
  pendingWrites: [],
});

const commands = (shell: AppContext["shell"], name: string) =>
  shell.calls.flatMap((c, i) => (c === name ? [shell.args[i]] : []));

const browserButton = (root: HTMLElement) => root.querySelector('button[title^="Browser"]') as HTMLElement;
const address = (root: HTMLElement) => root.querySelector('input[aria-label="Address"]') as HTMLInputElement;
const closers = (root: HTMLElement) => [...root.querySelectorAll('button[aria-label^="Close"]')] as HTMLElement[];

/** Open a browser tab and give it its first address, the way a person does. */
async function seatAPage({ dom, settle }: AppContext, url: string): Promise<void> {
  await dom.click(browserButton(dom.container));
  await settle();
  await dom.fill(address(dom.container), url);
  await dom.press(address(dom.container), "Enter");
  await settle();
}

test("closing a browser tab with no page does not ask the shell to close one", async () => {
  // `close` in browser.rs refuses a tab that has no page, so calling it for a tab
  // the user never gave an address would put "no browser tab is open" on screen as
  // an error they caused by closing it.
  await withApp({ viewport: VIEWPORT }, async ({ dom, shell, settle }) => {
    await dom.click(browserButton(dom.container));
    await settle();
    assert.equal(closers(dom.container).length, 1);
    await dom.click(closers(dom.container)[0]);
    await settle();
    assert.equal(commands(shell, "codify_browser_close").length, 0, "the shell was asked to close a page that never opened");
  });
});

test("closing a browser tab that has a page closes that page, or a webview is orphaned", async () => {
  await withApp({ viewport: VIEWPORT }, async (ctx) => {
    await seatAPage(ctx, "https://example.com/a");
    const opened = commands(ctx.shell, "codify_browser_open");
    assert.equal(opened.length, 1);
    await ctx.dom.click(closers(ctx.dom.container)[0]);
    await ctx.settle();
    const closed = commands(ctx.shell, "codify_browser_close");
    assert.equal(closed.length, 1, "closing the tab left its page running");
    assert.equal(closed[0].tabId, opened[0].tabId, "a different page was closed");
  });
});

test("closing a restored tab whose page was never seated does not ask the shell to close one", async () => {
  // Every webview died with the last process. A restored tab in the background
  // has an address and no page until it is looked at, so closing it must not
  // put the shell's "no browser tab is open" on screen.
  const PAGES = stored(
    [
      { key: "k_page_1", kind: "browser", url: "https://example.com/a" },
      { key: "k_page_2", kind: "browser", url: "https://example.com/b" },
    ],
    0,
  );
  await withApp({ storedTabs: PAGES, viewport: VIEWPORT }, async ({ dom, shell, settle }) => {
    await settle();
    const seated = commands(shell, "codify_browser_open");
    assert.equal(seated.length, 1, "only the tab on screen has a page");
    await dom.click(closers(dom.container)[1]);
    await settle();
    assert.equal(commands(shell, "codify_browser_close").length, 0, "the shell was asked to close a page that was never seated");
    assert.ok(dom.container.querySelector('[role="alert"]') === null, "closing an unseated tab put an error on screen");
    await dom.click(closers(dom.container)[0]);
    await settle();
    const closed = commands(shell, "codify_browser_close");
    assert.equal(closed.length, 1, "the seated page was left running");
    assert.equal(closed[0].tabId, seated[0].tabId);
  });
});

test("the first address seats a page under the tab's own id, with the pane's bounds, and the next one navigates that page", async () => {
  await withApp({ viewport: VIEWPORT }, async (ctx) => {
    await seatAPage(ctx, "https://example.com/a");
    const opened = commands(ctx.shell, "codify_browser_open");
    assert.equal(opened.length, 1);
    const bounds = opened[0].bounds as { width: number; height: number };
    assert.ok(bounds.width > 0 && bounds.height > 0, "the open call carried no measured rectangle");
    assert.equal(opened[0].url, "https://example.com/a");

    // The tab the page fills is the tab the shell was told about: the layout
    // mirror is what names the tab, and its key is the id the shell holds.
    const mirror = JSON.parse(ctx.dom.window.localStorage.getItem("CODIFY_TABS") ?? "{}");
    const tab = mirror.layout.tabs.find((t: { kind: string }) => t.kind === "browser");
    assert.equal(tab.url, "https://example.com/a", "the tab was not filled in with the address");

    await ctx.dom.fill(address(ctx.dom.container), "https://example.com/b");
    await ctx.dom.press(address(ctx.dom.container), "Enter");
    await ctx.settle();
    const navigated = commands(ctx.shell, "codify_browser_navigate");
    assert.equal(navigated.length, 1, "the second address did not navigate");
    assert.equal(navigated[0].tabId, opened[0].tabId, "the second address named a different page than the first");
    assert.equal(commands(ctx.shell, "codify_browser_open").length, 1, "the second address opened another page");
  });
});

test("a refused first address is reported in the tab it was typed in, and opens nothing", async () => {
  await withApp({ viewport: VIEWPORT }, async ({ dom, shell, settle }) => {
    await dom.click(browserButton(dom.container));
    await settle();
    await dom.fill(address(dom.container), "javascript:alert(1)");
    await dom.press(address(dom.container), "Enter");
    await settle();
    assert.equal(commands(shell, "codify_browser_open").length, 0, "a refused address reached the shell");
    const alert = dom.container.querySelector('[role="alert"]');
    assert.ok(alert, "typing a refused address did nothing at all: no error on screen");
    assert.ok((alert.textContent ?? "").length > 0);
  });
});

test("a browser tab replaces the transcript rather than sitting under it", async () => {
  await withApp({ viewport: VIEWPORT }, async ({ dom, settle }) => {
    const chatBefore = dom.container.querySelector('textarea, [aria-label="Message"]');
    await dom.click(browserButton(dom.container));
    await settle();
    assert.ok(address(dom.container), "no address bar for the browser tab");
    if (chatBefore) {
      assert.ok(dom.container.querySelector('textarea') === null, "the chat composer is still on screen under the browser pane");
    }
  });
});

test("exactly one page is visible at a time: focus follows the active tab", async () => {
  // Visibility is the stacking order for embedded pages. Without this every page
  // is stacked on the newest one, which is a browser showing four pages at once.
  const PAGES = stored(
    [
      { key: "k_chat", kind: "chat", conversationId: "c1", workspaceId: "ws-a" },
      { key: "k_page", kind: "browser", url: "https://example.com/a" },
    ],
    1,
  );
  await withApp({ storedTabs: PAGES, viewport: VIEWPORT }, async ({ dom, shell, settle, tabs }) => {
    await settle();
    const focusedWhileOnPage = commands(shell, "codify_browser_focus").map((a) => a.tabId);
    assert.ok(focusedWhileOnPage.length > 0 && focusedWhileOnPage.at(-1) !== "", "the page on screen was never brought forward");
    const pageId = focusedWhileOnPage.at(-1);
    await dom.click(tabs()[0]);
    await settle();
    assert.equal(commands(shell, "codify_browser_focus").at(-1)?.tabId, "", "leaving for a chat did not hide every page");
    await dom.click(tabs()[1]);
    await settle();
    assert.equal(commands(shell, "codify_browser_focus").at(-1)?.tabId, pageId, "returning to the page did not bring it back");
  });
});

test("the inspector control appears only when the shell has one, and opens and closes through the shell", async () => {
  await withApp(
    { viewport: VIEWPORT, shellAnswers: { codify_browser_devtools_available: true } },
    async (ctx) => {
      await seatAPage(ctx, "https://example.com/a");
      const toggle = () => ctx.dom.container.querySelector('button[aria-label="DevTools"]') as HTMLElement;
      assert.ok(toggle(), "the shell has an inspector and the control is missing");
      assert.equal(toggle().getAttribute("aria-pressed"), "false");
      await ctx.dom.click(toggle());
      await ctx.settle();
      assert.equal(commands(ctx.shell, "codify_browser_devtools_open").length, 1);
      assert.equal(toggle().getAttribute("aria-pressed"), "true", "the confirmed open did not move the state");
      await ctx.dom.click(toggle());
      await ctx.settle();
      assert.equal(commands(ctx.shell, "codify_browser_devtools_close").length, 1);
      assert.equal(toggle().getAttribute("aria-pressed"), "false");
    },
  );
});

test("no inspector control when the shell has none, and no hand-off to the system browser ever", async () => {
  await withApp(
    { viewport: VIEWPORT, shellAnswers: { codify_browser_devtools_available: false } },
    async (ctx) => {
      await seatAPage(ctx, "https://example.com/a");
      assert.ok(ctx.dom.container.querySelector('button[aria-label="DevTools"]') === null, "the app offered an inspector the shell does not have");
      assert.doesNotMatch(ctx.dom.container.innerHTML, /system browser|Open in system|external/i);
      assert.deepEqual(
        ctx.shell.calls.filter((c) => /external|open_url|system_browser/i.test(c)),
        [],
        "the app asked the shell to hand a page to the operating system",
      );
    },
  );
});

test("a redirect reaches the address bar, and the page's own title reaches the strip", async () => {
  // A redirect that never reached `Tab.url` is an address bar that lies after every
  // hop, and a title that never lands is a strip of hosts forever.
  await withApp({ viewport: VIEWPORT }, async (ctx) => {
    await seatAPage(ctx, "https://example.com/a");
    const tabId = commands(ctx.shell, "codify_browser_open")[0].tabId;
    // A redirect is a second load: the page reports it starting at the address
    // it reached, then finishing there.
    await ctx.emit("browser-page-loading", { tab_id: tabId, url: "https://example.com/landed" });
    await ctx.emit("browser-page-loaded", { tab_id: tabId, url: "https://example.com/landed" });
    await ctx.settle();
    assert.equal(address(ctx.dom.container).value, "https://example.com/landed", "the redirect did not reach the address bar");
    const mirror = JSON.parse(ctx.dom.window.localStorage.getItem("CODIFY_TABS") ?? "{}");
    assert.equal(
      mirror.layout.tabs.find((t: { kind: string }) => t.kind === "browser").url,
      "https://example.com/landed",
      "the live address never reached the tab, so a restart would reopen the old one",
    );
    await ctx.emit("browser-page-titled", { tab_id: tabId, url: "https://example.com/landed", title: "The Landing Page" });
    await ctx.settle();
    assert.match(ctx.dom.text(), /The Landing Page/, "the page's title never reached the strip");
  });
});

test("a page that is still loading is marked, and a finished one is not", async () => {
  await withApp({ viewport: VIEWPORT }, async (ctx) => {
    await seatAPage(ctx, "https://example.com/a");
    const tabId = commands(ctx.shell, "codify_browser_open")[0].tabId;
    const marked = () => ctx.dom.container.querySelectorAll('[data-loading="true"]').length;
    const before = marked();
    await ctx.emit("browser-page-loading", { tab_id: tabId, url: "https://example.com/a" });
    await ctx.settle();
    assert.ok(marked() > before, "a page that reported loading was not marked as loading");
    await ctx.emit("browser-page-loaded", { tab_id: tabId, url: "https://example.com/a" });
    await ctx.settle();
    assert.equal(marked(), before, "the marker stayed after the page finished");
  });
});

test("a popup request opens a tab through the guarded path, and never a window", async () => {
  // Google sign-in and every target=_blank link depend on this: the shell refuses
  // the popup and announces it, and the announcement is what the user acts on.
  await withApp({ viewport: VIEWPORT }, async (ctx) => {
    await seatAPage(ctx, "https://example.com/a");
    const source = commands(ctx.shell, "codify_browser_open")[0].tabId;
    const opened: string[] = [];
    (ctx.dom.window as unknown as { open: (u?: string) => null }).open = (u) => {
      opened.push(String(u));
      return null;
    };
    await ctx.emit("browser-popup-requested", { tab_id: source, url: "https://accounts.example.com/signin" });
    await ctx.settle();
    const seats = commands(ctx.shell, "codify_browser_open");
    assert.equal(seats.length, 2, "the popup did not open a tab");
    assert.equal(seats[1].url, "https://accounts.example.com/signin");
    assert.notEqual(seats[1].tabId, source, "the popup replaced the page that asked for it");
    assert.equal(ctx.tabs().length, 2, "the popup left an empty tab behind or opened more than one");
    assert.deepEqual(opened, [], "the popup was opened as a window of its own");
  });
});

test("a popup announcement that fails validation opens nothing", async () => {
  await withApp({ viewport: VIEWPORT }, async (ctx) => {
    await seatAPage(ctx, "https://example.com/a");
    const tabsBefore = ctx.tabs().length;
    for (const payload of [null, "https://x.example", {}, { tab_id: "", url: "https://x.example" }, { tab_id: "t", url: "" }]) {
      await ctx.emit("browser-popup-requested", payload);
    }
    await ctx.settle();
    assert.equal(ctx.tabs().length, tabsBefore, "a malformed announcement opened a tab");
    assert.equal(commands(ctx.shell, "codify_browser_open").length, 1, "a malformed announcement reached the shell");
  });
});
