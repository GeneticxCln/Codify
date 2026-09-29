/**
 * The terminal story, end to end, in the real App.
 *
 * Everything else in this suite tests a piece. This test mounts **the App
 * itself** over a fake shell and asks the question the audit was really
 * about: a user opens two shells, a build runs in the background one, they
 * wander off to the other tab — **is any of it lost?**
 *
 * What is real here, and why that matters:
 *
 * - **App.tsx, Sidebar, TabBar, TerminalPane** — the production components,
 *   not stand-ins. A test of extracted helpers can pass while the wiring is
 *   backwards; this cannot.
 * - **The Tauri event path.** `listenShellEvent` goes through the real
 *   `@tauri-apps/api/event` `listen()`, which registers the handler with
 *   `window.__TAURI_INTERNALS__` — faked here with a listener table. Emitting
 *   `terminal-output` walks the same code a Rust `sink.emit` walks, payload
 *   and all, so a renamed event or a reshaped payload fails here.
 * - **xterm itself.** The pane's dynamic `import("@xterm/xterm")` loads the
 *   real renderer (verified to open and paint under jsdom), and assertions
 *   read xterm's **buffer** — the same bytes a user would read.
 *
 * What is faked, loudly: the PTY layer (`codify_terminal_*` invoke — this
 * suite must never spawn a shell), the engine's HTTP surface (fetch is
 * stubbed to a routing table), and nothing else. No store function is
 * stubbed, no module is mocked: if the relay has a hole, the output does not
 * reach the buffer and the assertion says so.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
import type { Dom } from "./dom.ts";

const React = (await import("react")).default;
const h = React.createElement;

/**
 * Run one step inside `act`, the way `dom.click` does.
 *
 * Emitting shell output and typing are both app-visible: the recorder badges
 * the tab, the pane writes into xterm, and React re-renders for each. Done
 * outside `act` they still work — which is why the first draft of this test
 * passed while printing a page of "not wrapped in act" warnings, which is a
 * test asserting nothing about anything.
 */
const step = async (body: () => void | Promise<void>): Promise<void> => {
  const { act } = (await import("react")) as unknown as {
    act: (b: () => Promise<void> | void) => Promise<void>;
  };
  await act(async () => {
    await body();
  });
};

// ── the fake shell ────────────────────────────────────────────────────────

interface FakeShell {
  /** Event name → registered handlers, as the real internals keep them. */
  listeners: Map<string, Array<{ event: string; handler: (payload: unknown) => void }>>;
  /** Every `codify_terminal_*` invoke, in order. */
  calls: Array<{ cmd: string; args: Record<string, unknown> }>;
  /** The PTYs the shell has opened, keyed by id. */
  terminals: Map<string, { cols: number; rows: number }>;
  /** Emit to the webview the way Rust's `sink.emit` does. */
  emit(event: string, payload: unknown): Promise<void>;
  /** Install/undo the internals global. */
  install(): void;
  uninstall(): void;
}

function fakeShell(): FakeShell & { internals: Record<string, unknown> } {
  const listeners: FakeShell["listeners"] = new Map();
  const calls: FakeShell["calls"] = [];
  const terminals: FakeShell["terminals"] = new Map();
  /** What `plugin:event|listen` handed out, so `unlisten` can take it back. */
  const registrations = new Map<number, { event: string; handler: (payload: unknown) => void }>();
  let nextId = 0;
  let callbackSeq = 0;
  let eventSeq = 0;
  // The handler `transformCallback` just saw, awaiting its `plugin:event|listen`
  // invoke to name the event it belongs to.
  let pendingHandler: ((payload: unknown) => void) | null = null;
  const internals: Record<string, unknown> = {
    transformCallback(callback: unknown): number {
      // `listen()` hands over the *bare* handler function and passes the event
      // name separately in the `plugin:event|listen` args — so the registration
      // is recorded here against the pending event name from that invoke, not
      // from the callback's shape. (The first draft read `{event, handler}` off
      // the callback and matched nothing: every listener silently missed.)
      const id = callbackSeq++;
      if (typeof callback === "function") {
        pendingHandler = callback as (payload: unknown) => void;
      }
      return id;
    },
    async invoke(cmd: string, args: Record<string, unknown> = {}): Promise<unknown> {
      calls.push({ cmd, args });
      if (cmd === "plugin:event|listen") {
        const event = String(args.event ?? "");
        const eventId = ++eventSeq;
        if (pendingHandler && event) {
          const registration = { event, handler: pendingHandler };
          registrations.set(eventId, registration);
          const list = listeners.get(event) ?? [];
          list.push(registration);
          listeners.set(event, list);
          pendingHandler = null;
        }
        return eventId;
      }
      // Unlisten is not optional detail. A tab switch unmounts a pane, and a
      // pane's cleanup calls the unlisten `listen()` handed back; a fake that
      // returned an id and forgot it kept every dead pane's handler subscribed
      // (six live `terminal-output` listeners after a handful of switches), and
      // a test that then counts listeners — or that reads bytes — is measuring
      // the fake.
      if (cmd === "plugin:event|unlisten") {
        const eventId = Number(args.eventId);
        const registration = registrations.get(eventId);
        if (registration) {
          registrations.delete(eventId);
          const list = (listeners.get(registration.event) ?? []).filter(
            (r) => r !== registration
          );
          if (list.length) listeners.set(registration.event, list);
          else listeners.delete(registration.event);
        }
        return null;
      }
      if (cmd === "codify_terminal_open") {
        const id = `term-${++nextId}`;
        terminals.set(id, { cols: Number(args.cols), rows: Number(args.rows) });
        return id;
      }
      if (cmd === "codify_terminal_write" || cmd === "codify_terminal_resize") {
        return null;
      }
      if (cmd === "codify_terminal_close") return null;
      if (cmd === "codify_get_engine_info") return { port: 51820, token: "e2e-token" };
      if (cmd === "codify_engine_status") return { error: null };
      // List-shaped invokes answer with a list, never null — the settings
      // surface's memos run even while the modal is closed, and a null there
      // is a crash that no real shell produces.
      if (cmd.startsWith("codify_list_") || cmd.includes("configs")) return [];
      if (cmd === "plugin:event|emit") return null;
      return null;
    },
    // `Channel` and a few plugin internals read these; unused on this path.
    metadata: { currentWebview: { windowLabel: "main" }, currentWindow: { label: "main" } },
  };
  return {
    listeners,
    calls,
    terminals,
    internals,
    async emit(event, payload) {
      // The **Event record**, not the payload. `listen()` hands
      // `transformCallback` the handler it was given, and Rust's side calls it
      // with `{event, id, payload}` — which is why `listenShellEvent` reads
      // `received.payload`. Passing the bare payload here made every chunk
      // arrive as `undefined`, `readTerminalOutput` rejected it, and the shell
      // sat empty while the test looked for a bug in the relay that was not
      // there. (Both diagnostics said "no output at all", which is what a
      // dropped-by-validation event looks like.)
      for (const l of listeners.get(event) ?? []) {
        l.handler({ event, id: ++callbackSeq, payload });
      }
    },
    install() {
      // On the *window*, not merely on globalThis: `listenShellEvent` and
      // `tauriInvoke` check `window.__TAURI_INTERNALS__`, and under the DOM
      // harness `window` is the jsdom window — a fake that lives only on
      // globalThis is invisible to every check, and every terminal call
      // silently falls through to the HTTP fallback instead.
      (globalThis.window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = internals;
      (globalThis.window as unknown as Record<string, unknown>).__TAURI_EVENT_PLUGIN_INTERNALS__ = {
        unregisterListener() {},
      };
    },
    uninstall() {
      // `withDom`'s own teardown restores `globalThis.window`, so by the time a
      // test's `finally` runs, the jsdom window may already be gone — and the
      // fakes went with it, which is the cleanup having already happened.
      const win = globalThis.window as unknown as Record<string, unknown> | undefined;
      if (win) {
        delete win.__TAURI_INTERNALS__;
        delete win.__TAURI_EVENT_PLUGIN_INTERNALS__;
      }
    },
  };
}

// ── the fake engine's HTTP surface ────────────────────────────────────────

const WORKSPACES = [
  {
    id: "ws-alpha",
    name: "Alpha",
    root_path: "/tmp/e2e-alpha",
    design_contract_path: "",
    created_at: 1,
  },
  {
    id: "ws-beta",
    name: "Beta",
    root_path: "/tmp/e2e-beta",
    design_contract_path: "",
    created_at: 2,
  },
];

function fakeFetch(): typeof fetch {
  return (async (input: RequestInfo | URL): Promise<Response> => {
    const url = String(input);
    const json = (body: unknown): Response =>
      ({ ok: true, status: 200, json: async () => body }) as unknown as Response;
    if (url.includes("/health")) return json({ ok: true, authenticated: true });
    if (url.includes("/workspaces")) return json(WORKSPACES);
    // Order matters, and the reason is the whole lesson of the first run of
    // this file: `GET /models/recent` also contains "/models", so a single
    // `includes` branch answered the *list* route with `{models: []}` and
    // `buildModelSignals` died on `recentRuns is not iterable`. The catalogue
    // is an object; the recent-runs route is a list. Match the specific one first.
    if (url.includes("/models/recent")) return json([]);
    if (url.includes("/models")) return json({ models: [] });
    // Every remaining engine route answers with an empty *list* — never an
    // object, never null: the settings surface's memos iterate what comes back
    // (`recentRuns is not iterable` was this test's teacher), and a fresh
    // engine's honest answer is "nothing yet" anyway.
    return json([] as unknown[]);
  }) as unknown as typeof fetch;
}

// ── harness ───────────────────────────────────────────────────────────────

const rafSweep = async (rounds = 6): Promise<void> => {
  for (let i = 0; i < rounds; i++) {
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => requestAnimationFrame(() => r(null)));
  }
};

/** The text xterm currently shows for the pane mounted under `terminalId`. */
function screenOf(dom: Dom, terminalId: string): string {
  const host = dom.container.querySelector(`[data-terminal-id="${terminalId}"]`);
  const rows = host?.querySelector(".xterm-rows");
  return rows?.textContent ?? "";
}

/** xterm's textarea lives inside the pane; keystrokes there are what the user types. */
function typeInto(dom: Dom, terminalId: string, text: string): void {
  const host = dom.container.querySelector(`[data-terminal-id="${terminalId}"]`);
  const textarea = host?.querySelector("textarea");
  assert.ok(textarea, `no xterm textarea in the ${terminalId} pane to type into`);
  // A keyboard produces a `keydown` and a `keypress`, and a browser suppresses
  // the second when a handler cancels the first — which is exactly how xterm
  // works, and why both halves are here. xterm takes a letter from `keydown`
  // (reading `keyCode`, which a real keypress carries and `new
  // KeyboardEvent({key})` does not: a zero keyCode means "not a key I can
  // type") and takes a **space** from the `keypress` instead, because its
  // keydown table starts above the space bar. Dispatch both, in that order, and
  // only follow with the `keypress` when nothing cancelled the `keydown` —
  // which is the browser's own rule, and the reason a hand-rolled keyboard
  // that fires both unconditionally types "echohi" and then "echo hhei".
  for (const ch of text) {
    const keyCode =
      ch === " " ? 32 : /[a-z]/i.test(ch) ? ch.toUpperCase().charCodeAt(0) : ch.charCodeAt(0);
    const down = new dom.window.KeyboardEvent("keydown", {
      key: ch,
      keyCode,
      which: keyCode,
      bubbles: true,
      cancelable: true,
    });
    textarea.dispatchEvent(down);
    if (down.defaultPrevented) continue;
    textarea.dispatchEvent(
      new dom.window.KeyboardEvent("keypress", {
        key: ch,
        keyCode,
        which: keyCode,
        charCode: keyCode,
        bubbles: true,
        cancelable: true,
      }),
    );
  }
}

/** The strip's tabs, in open order. */
const stripTabs = (dom: Dom): HTMLElement[] =>
  [...dom.container.querySelectorAll('[role="tab"]')] as HTMLElement[];

test("two shells, a background build, a tab switch — and nothing is lost", async () => {
  const shell = fakeShell();
  const realFetch = globalThis.fetch;
  // The engine's WS stream is a different channel from the terminal's —
  // terminal output arrives by Tauri event, never by websocket — and a real
  // socket here would dial 127.0.0.1, the one thing this harness refuses
  // (dom.ts's own rule for `fetch`). A silent stub is the honest state of a
  // fake engine: no stream, no retries, nothing for this test to hear.
  const realWebSocket = globalThis.WebSocket;
  class NoWebSocket {
    url: string;
    constructor(url: string) {
      this.url = url;
    }
    close(): void {}
    addEventListener(): void {}
    removeEventListener(): void {}
  }
  (globalThis as Record<string, unknown>).WebSocket = NoWebSocket;

  try {
    await withDom(async (dom) => {
      // Inside the harness, and in this order: `install` writes onto the
      // harness's jsdom window (the app reads `window`, not globalThis), and
      // the fetch router goes over **dom.ts's own stub**, which `withDom`
      // installs at startup and which would otherwise reject every engine
      // call — the first run of this test died on exactly that, with a
      // workspace list that never loaded and a Terminal button that silently
      // refused everything.
      shell.install();
      globalThis.fetch = fakeFetch();
      // xterm ships a browser bundle that reaches for `self`. Node's module
      // scope has no `self` — jsdom has it *inside the window* — so importing
      // xterm outside a page throws `self is not defined` and the pane shows
      // that as a red `role="alert"` instead of a shell. Point Node's `self` at
      // the harness window (where `self === window` already, in jsdom), which
      // is what the bundle would have found had a browser loaded it.
      if (!(globalThis as Record<string, unknown>).self) {
        (globalThis as Record<string, unknown>).self = dom.window;
      }
      const { App } = await import("../src/App.tsx");
      await dom.render(h(App));
      await dom.settle();
      await rafSweep();

      // The shell came up: engine info arrived, the workspace list loaded, and
      // the first workspace was auto-selected — which is what makes the
      // sidebar's Terminal control live. Probed by the button's `disabled`
      // state rather than a DOM sentinel: `handleOpenTerminal` reads
      // `selectedWs`, and the button is enabled exactly when there is one.
      const terminalButton = dom.container.querySelector(
        'button[title^="Terminal"]'
      ) as HTMLButtonElement | null;
      assert.ok(terminalButton, "the sidebar has no Terminal control");
      // Readiness is read off the control that actually tracks workspace
      // selection — New Tab, which is disabled until `selectedWs` exists. The
      // sidebar's Terminal button carries no disabled state, so it proves
      // nothing; the first draft of this test asserted on it and clicked a
      // handler that bailed on a missing workspace.
      const newTab = dom.container.querySelector(
        'button[title^="A new, empty tab"]'
      ) as HTMLButtonElement | null;
      assert.ok(newTab, "the header has no New Tab control");
      assert.ok(
        !newTab.disabled,
        "no workspace was ever selected, so no shell can be opened"
      );
      assert.equal(
        dom.container.querySelector('[role="alert"]')?.textContent ?? "",
        "",
        "the app mounted into an error state"
      );
      await dom.click(terminalButton);
      await rafSweep();
      await step(() => shell.emit("terminal-output", { id: "term-1", data: "quinton@alpha:~$ " }));
      await rafSweep();

      assert.ok(
        screenOf(dom, "term-1").includes("quinton@alpha"),
        `shell 1's prompt never reached xterm: ${JSON.stringify(screenOf(dom, "term-1").slice(0, 60))}`
      );

      // ── open shell 2, which makes shell 1 a background terminal ────────
      await dom.click(terminalButton);
      await rafSweep();
      await step(() => shell.emit("terminal-output", { id: "term-2", data: "quinton@beta:~$ " }));
      await rafSweep();
      assert.ok(
        screenOf(dom, "term-2").includes("quinton@beta"),
        "shell 2's prompt never reached its xterm"
      );

      // ── the build, in the background, in many chunks ───────────────────
      // Switch back to shell 1 first so the build starts while its tab is
      // active — the realistic order — and then leave for tab 2 before the
      // heavy output arrives. Chunks are small, the way a real PTY reads.
      await dom.click(stripTabs(dom)[0]);
      await rafSweep();
      await step(() => shell.emit("terminal-output", { id: "term-1", data: "make build\r\n" }));
      await rafSweep();
      const chunks = [
        "gcc -c engine.c\r\n", "gcc -c sandbox.c\r\n", "linking…\r\n",
        "warning: unused var\r\n", "build finished in 2.4s\r\n", "OK 1075 tests\r\n",
      ];
      await dom.click(stripTabs(dom)[1]); // walk away mid-build
      await rafSweep();
      await step(async () => {
        for (const chunk of chunks) {
          await shell.emit("terminal-output", { id: "term-1", data: chunk });
        }
      });
      await rafSweep();

      // ── come back, and read the whole build ────────────────────────────
      await dom.click(stripTabs(dom)[0]);
      await rafSweep();
      const screen = screenOf(dom, "term-1");
      for (const want of [
        "quinton@alpha:~$", "make build", "gcc -c engine.c", "gcc -c sandbox.c",
        "linking…", "warning: unused var", "build finished in 2.4s", "OK 1075 tests",
      ]) {
        assert.ok(
          screen.includes(want),
          `output lost across the tab switch: ${JSON.stringify(want)} is not on screen`
        );
      }
      // And in order: the prompt is above the build, the build above its OK.
      const at = (s: string): number => screen.indexOf(s);
      assert.ok(at("quinton@alpha") < at("make build"), "the prompt is below the build");
      assert.ok(at("make build") < at("build finished"), "the build is out of order");
      assert.ok(at("build finished") < at("OK 1075"), "the summary is out of order");

      // The resume seam marks the gap, so the missed output does not read as
      // if it had been on screen all along.
      assert.match(screen, /output missed while this pane was away/);

      // Once, not twice. The screen is the user-visible half of the promise, and
      // it had been broken: every mounted pane filed *every* chunk into its own
      // workspace's scrollback, so the pane showing tab 2 filed tab 1's
      // background build, and the returning pane replayed each of those lines
      // from the tail **and** from the backlog.
      const once = (haystack: string, needle: string): number =>
        haystack.split(needle).length - 1;
      for (const line of [
        "make build", "gcc -c engine.c", "gcc -c sandbox.c",
        "linking…", "build finished in 2.4s", "OK 1075 tests",
      ]) {
        assert.equal(
          once(screen, line), 1,
          `"${line}" is on screen ${once(screen, line)} times — output is being filed twice`
        );
      }

      // And the workspace record itself, which is what the *next* pane in this
      // workspace restores — so this is the same promise one tab further out.
      // The prompt is the interesting one: it has no newline, and the record
      // used to drop every line without one, which is every shell's last word.
      const { readTerminalHistory } = await import("../src/terminalHistory.ts");
      const record = readTerminalHistory("ws-alpha");
      for (const line of [
        "quinton@alpha:~$", "make build", "gcc -c engine.c", "gcc -c sandbox.c",
        "linking…", "warning: unused var", "build finished in 2.4s", "OK 1075 tests",
      ]) {
        assert.ok(
          record.includes(line),
          `the workspace record lost ${JSON.stringify(line)} across the switch`
        );
        assert.equal(
          once(record, line), 1,
          `the workspace record holds "${line}" ${once(record, line)} times`
        );
      }

      // ── the other direction: a build behind tab 2, then home ───────────
      await step(() => shell.emit("terminal-output", { id: "term-2", data: "npm test\r\n" }));
      await dom.click(stripTabs(dom)[1]);
      await rafSweep();
      await step(async () => {
        for (let i = 1; i <= 20; i++) {
          await shell.emit("terminal-output", { id: "term-2", data: `pass ${i}\r\n` });
        }
      });
      await dom.click(stripTabs(dom)[0]);
      await rafSweep();
      await dom.click(stripTabs(dom)[1]);
      await rafSweep();
      const screen2 = screenOf(dom, "term-2");
      assert.ok(screen2.includes("npm test"), "tab 2's first line was lost");
      for (let i = 1; i <= 20; i++) {
        assert.ok(
          screen2.includes(`pass ${i}`),
          `tab 2 lost "pass ${i}" across the switch away and back`
        );
      }

      // ── and the shell still takes keystrokes after all of it ───────────
      await step(async () => {
        typeInto(dom, "term-2", "echo hi");
      });
      await rafSweep();
      const writes = shell.calls.filter((c) => c.cmd === "codify_terminal_write");
      const typed = writes
        .filter((c) => c.args.terminalId === "term-2")
        .map((c) => c.args.data)
        .join("");
      assert.equal(
        typed,
        "echo hi",
        `the shell's stdin did not survive the round trip: ${JSON.stringify(typed)}`
      );
    });
  } finally {
    shell.uninstall();
    globalThis.fetch = realFetch;
    (globalThis as Record<string, unknown>).WebSocket = realWebSocket;
  }
});
