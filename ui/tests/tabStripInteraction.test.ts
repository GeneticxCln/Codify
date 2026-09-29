/**
 * The tab strip, pressed.
 *
 * `tabs.test.ts` is 52 tests of the *arithmetic* — where closing lands, what a
 * drag past the end does, when a browser close signal may mark a terminal — and
 * every one of them is a pure function, which is why they run without a DOM.
 * `shell.test.ts` renders the strip to a string and says what is on it.
 *
 * Neither can ask the question that matters most about a strip: **does pressing
 * a tab do anything?** A strip that renders nine beautiful tabs wired to no
 * handler at all produces byte-identical markup to a working one, and the
 * `readFileSync` assertions elsewhere in these tests only prove the prop names
 * are spelled the way the caller spells them.
 *
 * The claims here are the ones only a press can settle:
 *
 * - a tab names *itself* to `onFocus`, by id, and not by anything global;
 * - the × inside a tab closes that tab and **does not** also focus it — the
 *   `stopPropagation` that keeps a close from being a focus is invisible in
 *   markup, and its absence is the bug that closes a tab *and* makes the tab
 *   you just closed the active one;
 * - the new-tab control is not merely greyed out when no project is selected,
 *   it refuses;
 * - a tab with no thread name is named for the folder it is in, resolved from
 *   the id the tab carries rather than from the project the composer happens to
 *   be on.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
import type { Dom } from "./dom.ts";

import type { Tab } from "../src/tabs.ts";
import type { Workspace } from "../src/types.ts";

// ── fixtures ────────────────────────────────────────────────────────────

const tab = (over: Partial<Tab> = {}): Tab => ({
  id: "tab-1",
  kind: "chat",
  title: "Refactor the parser",
  conversationId: "c1",
  ...over,
});

const ws = (over: Partial<Workspace> = {}): Workspace => ({
  id: "w1",
  name: "Codify",
  root_path: "/home/quinton/Projects/Codify",
  design_contract_path: "",
  created_at: 0,
  ...over,
});

const React = (await import("react")).default;
const h = React.createElement;

/** Every callback the strip was handed, in the order they were made. */
interface Report {
  focused: string[];
  closed: string[];
  newTabs: number;
}

/**
 * The New Tab control, which the strip used to own and the header has now.
 *
 * Rendered beside the badge and above the strip, exactly as the header does it,
 * because the control's *distance* is the claim: a test that renders it on its
 * own cannot say that a growing strip no longer pushes it away.
 */
async function withNewTabButton(
  workspaceId: string | undefined,
  body: (dom: Dom, io: { pressed: number }) => Promise<void>,
): Promise<void> {
  const io = { pressed: 0 };
  await withDom(async (dom) => {
    const { NewTabButton } = await import("../src/components/NewTabButton.tsx");
    const { TabBar } = await import("../src/components/TabBar.tsx");
    await dom.render(
      h(
        "div",
        { className: "flex items-center gap-3" },
        h("span", null, "CODIFY"),
        h(NewTabButton, {
          workspaceId,
          onNewTab: () => {
            io.pressed += 1;
          },
        }),
        h(TabBar, {
          tabs: [tab({ id: "a" }), tab({ id: "b", title: "build watch" })],
          activeId: "a",
          workspaces: [],
          onFocus: () => {},
          onClose: () => {},
        }),
      ),
    );
    await body(dom, io);
  });
}

/**
 * Mount the strip over the given tabs.
 *
 * A DOM rather than a string, because the whole claim is what a press *does*:
 * the ids it reports, and what it declines to report.
 */
async function withStrip(
  props: {
    tabs: Tab[];
    activeId?: string | null;
    busyTabIds?: string[];
    unreadTerminalIds?: string[];
    workspaces?: Workspace[];
  },
  body: (dom: Dom, io: Report) => Promise<void>,
): Promise<void> {
  const report: Report = { focused: [], closed: [], newTabs: 0 };
  await withDom(async (dom) => {
    // Imported inside the DOM: `react-dom/client` reads `document` while it is
    // being imported, so a hoisted static import would hand it none.
    const { TabBar } = await import("../src/components/TabBar.tsx");
    await dom.render(
      h(TabBar, {
        tabs: props.tabs,
        activeId: props.activeId ?? (props.tabs[0]?.id ?? null),
        workspaces: props.workspaces ?? [],
        busyTabIds: props.busyTabIds ?? [],
        unreadTerminalIds: props.unreadTerminalIds ?? [],
        onFocus: (id: string) => {
          report.focused.push(id);
        },
        onClose: (id: string) => {
          report.closed.push(id);
        },
      }),
    );
    await body(dom, report);
  });
}

const tablist = (dom: Dom): HTMLElement[] =>
  [...dom.container.querySelectorAll('[role="tab"]')] as HTMLElement[];

const selectedId = (dom: Dom): string | null =>
  dom.container.querySelector('[role="tab"][aria-selected="true"]')?.getAttribute("aria-label") ?? null;

// ─────────────────────────────────────────────────────────────────────────────
// Pressing a tab
// ─────────────────────────────────────────────────────────────────────────────

test("pressing a tab asks for that tab, by id", async () => {
  await withStrip(
    { tabs: [tab({ id: "tab-1" }), tab({ id: "tab-2", title: "build watch" })], activeId: "tab-1" },
    async (dom, io) => {
      await dom.click(tablist(dom)[1]);
      assert.deepEqual(io.focused, ["tab-2"], "the strip focused something other than what was pressed");
      assert.deepEqual(io.closed, [], "pressing a tab closed one");
    },
  );
});

test("the × closes its own tab and does not also focus it", async () => {
  // The one that cannot be seen in markup. The close button is *inside* the
  // tab, so without `stopPropagation` the same click both closes a tab and
  // focuses it — and the strip is then left showing a tab that no longer
  // exists. A test that reads the rendered string cannot tell the two apart; a
  // person loses the tab they were on.
  //
  // The × is on the *background* tab, so a focus would be a visible mistake
  // rather than a harmless echo of what was already active.
  await withStrip(
    { tabs: [tab({ id: "tab-1" }), tab({ id: "tab-2", title: "build watch" })], activeId: "tab-1" },
    async (dom, io) => {
      await dom.click(dom.byLabel("Close conversation: build watch"));
      assert.deepEqual(io.closed, ["tab-2"], "the × closed the wrong tab");
      assert.deepEqual(io.focused, [], "closing a tab also focused it");
    },
  );
});

test("the strip asks and waits: it does not remove a tab on its own", async () => {
  await withStrip({ tabs: [tab({ id: "only" })], activeId: "only" }, async (dom, io) => {
    assert.equal(tablist(dom).length, 1);
    await dom.click(dom.byLabel("Close conversation: Refactor the parser"));
    assert.deepEqual(io.closed, ["only"]);
    // The strip is controlled: it asked, and the parent decides. A strip that
    // removed the tab itself would be showing a state the engine never had.
    assert.equal(tablist(dom).length, 1, "the strip removed a tab on its own");
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// What a tab is called
// ─────────────────────────────────────────────────────────────────────────────

test("a tab with no thread name is named for its own folder, not the current project", async () => {
  // The composer can be moved to another project after the tab opened. A tab
  // that took its folder from the composer would rename itself under the
  // user's feet; the strip resolves the id the tab *carries* instead.
  await withStrip(
    {
      tabs: [tab({ id: "a", title: "   ", workspaceId: "w1" }), tab({ id: "b", title: "   ", workspaceId: "w2" })],
      activeId: "a",
      workspaces: [ws({ id: "w1", name: "Codify" }), ws({ id: "w2", name: "Docs", root_path: "/w/docs" })],
    },
    async (dom) => {
      assert.equal(tablist(dom)[0].getAttribute("aria-label"), "Conversation: Codify in /home/quinton/Projects/Codify");
      assert.equal(tablist(dom)[1].getAttribute("aria-label"), "Conversation: Docs in /w/docs");
    },
  );
});

test("a named thread is named by the thread, and the folder is context not a second name", async () => {
  await withStrip(
    {
      tabs: [tab({ id: "a", title: "Refactor the parser", workspaceId: "w1" })],
      activeId: "a",
      workspaces: [ws({ id: "w1" })],
    },
    async (dom) => {
      assert.equal(
        tablist(dom)[0].getAttribute("aria-label"),
        "Conversation: Refactor the parser in /home/quinton/Projects/Codify",
      );
      assert.match(
        (tablist(dom)[0].textContent ?? "").replace(/\s+/g, " ").trim(),
        /^Refactor the parser/,
        "the folder is in the visible label, so the tab carries two names for one thing",
      );
    },
  );
});

test("each kind is told apart by its name, so a screen reader is not reading an icon", async () => {
  await withStrip(
    {
      tabs: [
        tab({ id: "a", kind: "chat", title: "Design the brand" }),
        tab({ id: "b", kind: "terminal", title: "zsh" }),
        tab({ id: "c", kind: "browser", title: "example.com" }),
      ],
    },
    async (dom) => {
      assert.deepEqual(
        tablist(dom).map((t) => t.getAttribute("aria-label")),
        ["Conversation: Design the brand", "Terminal: zsh", "Browser: example.com"],
      );
    },
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// Which one is showing
// ─────────────────────────────────────────────────────────────────────────────

test("the active tab is the one marked, and it is the one the keyboard lands on", async () => {
  await withStrip(
    {
      tabs: [tab({ id: "a" }), tab({ id: "b" }), tab({ id: "c" })],
      activeId: "b",
      busyTabIds: ["c"],
    },
    async (dom) => {
      assert.equal(selectedId(dom), "Conversation: Refactor the parser");
      assert.equal(tablist(dom).filter((t) => t.getAttribute("aria-selected") === "true").length, 1);
      // Roving tabindex: ⌘1..9 and Tab both depend on the keyboard being able to
      // enter the strip exactly once.
      assert.deepEqual(tablist(dom).map((t) => t.getAttribute("tabindex")), ["-1", "0", "-1"]);
      // A run in flight on a background tab is a fact about the strip, not only
      // about the pane on screen.
      assert.match(tablist(dom)[2].textContent ?? "", /Refactor the parser/);
    },
  );
});

test("the strip counts its tabs for the screen reader, one at a time", async () => {
  await withStrip({ tabs: [tab({ id: "a" }), tab({ id: "b" })], activeId: "a" }, async (dom) => {
    const positions = [...dom.container.querySelectorAll('[role="tab"]')]
      .map((t) => (t.textContent ?? "").replace(/\s+/g, " ").match(/\d of \d/)?.[0]);
    assert.deepEqual(positions, ["1 of 2", "2 of 2"]);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// New Tab, which the strip gave up
// ─────────────────────────────────────────────────────────────────────────────

test("the strip no longer offers New Tab", async () => {
  // The regression this change exists to prevent: a control at the end of a
  // list that grows, whose distance from the pointer grows with the number of
  // tabs — for the one control whose job is to make that number smaller.
  await withStrip(
    { tabs: [tab({ id: "a" }), tab({ id: "b" }), tab({ id: "c" })], activeId: "a" },
    async (dom) => {
      assert.deepEqual(
        dom.allByLabel("New tab"),
        [],
        "the strip grew a new-tab control again",
      );
      const tablist = dom.container.querySelector('[role="tablist"]')!;
      assert.equal(
        tablist.querySelectorAll("button").length,
        3,
        "a control is inside the tablist rather than beside it",
      );
    },
  );
});

test("New Tab makes a clean slate, and refuses without a project", async () => {
  await withNewTabButton("w1", async (dom, io) => {
    const button = dom.byLabel("New tab");
    assert.equal(button.hasAttribute("disabled"), false);
    assert.match(button.getAttribute("title") ?? "", /empty tab in the selected project/);
    await dom.click(button);
    assert.equal(io.pressed, 1, "the header's New Tab does nothing");
  });

  await withNewTabButton(undefined, async (dom, io) => {
    const button = dom.byLabel("New tab");
    assert.equal(button.hasAttribute("disabled"), true, "the control offers a tab with no project to open");
    assert.match(button.getAttribute("title") ?? "", /Select a project/);
    await dom.click(button);
    assert.equal(io.pressed, 0, "a disabled control still opened a tab");
  });
});

test("a clean slate is named for its project, not for the placeholder", async () => {
  // A tab with nothing in it has no thread name to show, so it falls back to
  // the folder — which is exactly right here, because the folder *is* the tab.
  // "New chat" would name nothing at all: not the project, not the thread, and
  // not which of two clean slates you are looking at.
  await withStrip(
    {
      tabs: [
        tab({ id: "a", title: "   ", workspaceId: "w1" }),
        tab({ id: "b", title: "   ", workspaceId: "w2" }),
      ],
      activeId: "a",
      workspaces: [ws({ id: "w1", name: "Codify" }), ws({ id: "w2", name: "Docs" })],
    },
    async (dom) => {
      assert.deepEqual(
        tablist(dom).map((t) => (t.textContent ?? "").replace(/\s+/g, " ").trim()),
        ["Codify 1 of 2", "Docs 2 of 2"],
      );
    },
  );
});

test("a background terminal that spoke while unwatched is badged", async () => {
  await withStrip(
    {
      tabs: [
        tab({ id: "a" }),
        tab({ id: "t-busy", kind: "terminal", title: "build watch", ptyId: "t-busy" }),
      ],
      activeId: "a",
      unreadTerminalIds: ["t-busy"],
    },
    async (dom) => {
      const dot = dom.container.querySelector('[data-testid="terminal-unread-dot"]');
      assert.ok(dot, "the unread dot is not rendered for the badged tab");
      // The dot sits inside its own tab, not the strip at large — a badge on
      // the wrong tab points the user somewhere nothing happened.
      const badgedTab = dot.closest('[role="tab"]');
      assert.match(
        badgedTab?.getAttribute("aria-label") ?? "",
        /new output/,
        "the accessible name must carry the fact the dot shows"
      );
      assert.match(badgedTab?.getAttribute("aria-label") ?? "", /build watch/);
    },
  );
});

test("a terminal the user is watching is not badged, and the dot lands on one tab only", async () => {
  await withStrip(
    {
      tabs: [
        tab({ id: "t-active", kind: "terminal", ptyId: "t-active" }),
        tab({ id: "t-quiet", kind: "terminal", title: "quiet shell", ptyId: "t-quiet" }),
      ],
      activeId: "t-active",
      unreadTerminalIds: ["t-quiet"],
    },
    async (dom) => {
      const dots = dom.container.querySelectorAll('[data-testid="terminal-unread-dot"]');
      assert.equal(dots.length, 1, "exactly one badged tab");
      const badgedTab = dots[0].closest('[role="tab"]');
      assert.notEqual(
        badgedTab?.getAttribute("aria-selected"),
        "true",
        "the tab the user is reading must not carry the badge"
      );
    },
  );
});

test("the close control is visible without a hover", () => {
  // The click behind it always worked and the button was always in the
  // accessibility tree — it was `opacity-0 group-hover:opacity-100`, which is
  // a control nobody can find. A report of "the X does not show on the tabs"
  // is a report about this class string, and the strip it was reported against
  // held a hundred truncated labels: hover is not something a person can
  // search. Read as source because visibility is a Tailwind class, and the
  // text harness above renders no stylesheet at all.
  const source = readFileSync(
    new URL("../src/components/TabBar.tsx", import.meta.url),
    "utf8"
  );
  const from = source.indexOf("aria-label={`Close ");
  assert.ok(from >= 0, "TabBar no longer renders a close button");
  // Comments stripped, and this is not cosmetic: the button's own comment
  // *names* the class it stopped using, so a check that read the prose would
  // fail on the fix's own explanation.
  const closeButton = source
    .slice(from, source.indexOf("</button>", from))
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
  assert.doesNotMatch(
    closeButton,
    /opacity-0|invisible|hidden/,
    "the close button is hidden again until the pointer happens to land on it, \
     which is a tab a user cannot see how to close"
  );
});
