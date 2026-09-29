/**
 * The health probe recovers when the engine moves ports under a live window.
 *
 * **The bug this test exists to hold.** The probe used to ask the shell for
 * fresh connection info (`refreshEngineInfoFromIpc`) on one condition only:
 * `health.ok && !health.authenticated` — an engine is *there* but does not
 * know us. That guard silently dropped the second reading of `!ok`: nothing
 * answered at the port we hold, and the reason may be that the engine is
 * alive somewhere else. The shell tracks the live process's handshake, so it
 * is the one party that can say where; without the `!ok` half of the
 * condition, a window holding a port with no listener stayed **Offline** poll
 * after poll — a red pill beside a perfectly healthy engine one port over —
 * until the whole app was restarted.
 *
 * **Why the engine has to move *after* the window boots.** An earlier draft
 * seeded a dead port and called that the bug, and it passed for the wrong
 * reason: `App` fetches engine info from the shell on mount (`fetchInfo` in
 * the mount effect), so a cold boot already recovers without the probe's help,
 * and the probe's own recovery never ran. That is not the bug — a window that
 * has just booted and found a dead port has not been wrong for long. The bug
 * is the *open* window: the engine restarted, took a new port, and the poll
 * — the only thing still running — has to notice. So the shell's answer here
 * changes after the mount effects have settled, and the test then waits for
 * the app's own 3s poll rather than reaching into a ref: the schedule is part
 * of the contract, and a test that skips it would pass even if the automatic
 * path were dead.
 *
 * **What is real here.** The production `App`, the production `api.ts` module
 * state (`currentEngine`, `setEngineInfo`, its localStorage write-through),
 * the production probe with its recovery branch and immediate re-probe. The
 * two ends are faked loudly: `fetch` refuses every request to the dead port
 * and answers only the fresh one (a routing table, as `terminalEndToEnd`
 * does), and `__TAURI_INTERNALS__` answers `codify_get_engine_info` with
 * whatever the shell believes *at that moment*, which the test changes under
 * the app's feet. Both fakes record their calls, because the fingerprint of
 * the fix is the *sequence*: health fails at the held port → the shell is
 * asked → the changed pair is applied → health is asked again, at the new port.
 *
 * This test fails against the pre-fix App — the second probe reads `!ok`,
 * skips the IPC branch, and the pill never leaves Offline — which is the only
 * property a pin needs.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");

const React = (await import("react")).default;
const h = React.createElement;

// ── the fakes ─────────────────────────────────────────────────────────────

/** The port the window holds at mount, where nothing listens. */
const DEAD_PORT = 7430;
/** The port the engine actually moved to, mid-test. */
const FRESH_PORT = 7440;
const STALE_TOKEN = "stale-token";
const FRESH_TOKEN = "fresh-token-abcdef";

/** Fetch: dead at the stale port, alive at the fresh one. Every call recorded. */
function fakeFetch(calls: Array<{ url: string }>): typeof fetch {
  return (async (input: RequestInfo | URL): Promise<Response> => {
    const url = String(input);
    calls.push({ url });
    const json = (body: unknown): Response =>
      ({ ok: true, status: 200, json: async () => body }) as unknown as Response;
    const refused = (): Response =>
      ({
        ok: false,
        status: 500,
        json: async () => ({}),
      }) as unknown as Response;
    if (url.includes(`:${DEAD_PORT}/`)) return refused();
    if (url.includes("/health")) return json({ ok: true, authenticated: true });
    if (url.includes("/models")) {
      // The catalog is an object (terminalEndToEnd's lesson: `/models/recent`
      // also contains "/models", so this branch must not catch it), and its
      // `models` list is what the picker's reducer iterates with `.find`.
      if (url.includes("/models/recent")) return json([]);
      return json({ models: [], providers: [], fetched_at: 0, cached: false });
    }
    // The rest of the app booting against the fresh engine answers empty,
    // the way a fresh engine's honest answers read (terminalEndToEnd's rule:
    // never an object where a list is iterated).
    return json([] as unknown[]);
  }) as unknown as typeof fetch;
}

/**
 * The shell's side of IPC: `codify_get_engine_info` reports whatever
 * `held` says the engine is right now — that is the whole point, because the
 * engine moving is a fact only the shell has. Everything else is refused like
 * a command the capability set never granted.
 */
function installShellIpc(calls: string[], held: () => { port: number; token: string }): void {
  let callbackSeq = 0;
  // `@tauri-apps/api`'s `unlisten` reaches this table *before* it invokes
  // `plugin:event|unlisten` (event.js:43) — an unmount during the test's
  // settle would otherwise throw out of a cleanup path.
  const registeredListeners = new Map<string, Map<number, unknown>>();
  (globalThis.window as unknown as Record<string, unknown>).__TAURI_EVENT_PLUGIN_INTERNALS__ = {
    unregisterListener(event: string, eventId: number): void {
      registeredListeners.get(event)?.delete(eventId);
    },
    registerListener(event: string, eventId: number, target: unknown): void {
      const table = registeredListeners.get(event) ?? new Map();
      table.set(eventId, target);
      registeredListeners.set(event, table);
    },
  };
  (globalThis as unknown as Record<string, unknown>).__TAURI_EVENT_PLUGIN_INTERNALS__ =
    (globalThis.window as unknown as Record<string, unknown>).__TAURI_EVENT_PLUGIN_INTERNALS__;
  const internals = {
    // `@tauri-apps/api`'s `listen` registers every handler through this
    // (event.js:79) before it invokes `plugin:event|listen` — the same shape
    // `terminalEndToEnd`'s fake carries, for the same reason.
    // The handler argument is deliberately dropped: this test listens to
    // nothing, and a fake that kept a handler table it never read would be
    // claiming to answer events it has no answer for.
    transformCallback(): number {
      return callbackSeq++;
    },
    async invoke(cmd: string): Promise<unknown> {
      calls.push(cmd);
      if (cmd === "codify_get_engine_info") {
        const { port, token } = held();
        return { port, token };
      }
      if (cmd === "plugin:event|listen") return 1;
      if (cmd === "plugin:event|unlisten") return null;
      throw new Error(`invoke refused: ${cmd}`);
    },
  };
  (globalThis.window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = internals;
  (globalThis as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = internals;
}

/** The header pill's own words — the state a person can see. */
function pillText(dom: { container: HTMLElement }): string {
  const pill = [...dom.container.querySelectorAll("span")].find((s) =>
    /^(Live|Offline|Checking|Auth stale)$/.test(s.textContent ?? ""),
  );
  return pill?.textContent ?? "(no pill)";
}

// ── the test ──────────────────────────────────────────────────────────────

test("a dead port makes the probe ask the shell, apply the moved pair, and go live", async () => {
  const fetchCalls: Array<{ url: string }> = [];
  const ipcCalls: string[] = [];
  // What the shell believes the engine is. Starts on the port the window
  // holds, and is changed mid-test the way a restarted engine changes it.
  let engine = { port: DEAD_PORT, token: STALE_TOKEN };
  const held = (): { port: number; token: string } => engine;
  const healthUrls = (): string[] =>
    fetchCalls.map((c) => c.url).filter((u) => u.includes("/health"));

  const realFetch = globalThis.fetch;
  // The engine's event stream is a WebSocket, and this test has no business
  // hearing it: a real socket would dial the very port the fake fetch is
  // refusing, which is the one socket dom.ts's rule exists to keep closed.
  // A silent stub is the honest state of an engine this test is inventing.
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
      // The stale connection is the bug's precondition: the client holds port
      // 7430, where nothing listens. Set *before* App is imported, because
      // `api.ts` reads localStorage at module load — this module-load read is
      // the same one that carries a previous session's port into a real boot.
      dom.window.localStorage.setItem("CODIFY_PORT", String(DEAD_PORT));
      dom.window.localStorage.setItem("CODIFY_TOKEN", STALE_TOKEN);
      // The harness mirrors globals onto globalThis (dom.ts's rule), and the
      // app reads `window`, so the seed has to land in both storages.
      globalThis.localStorage?.setItem?.("CODIFY_PORT", String(DEAD_PORT));
      globalThis.localStorage?.setItem?.("CODIFY_TOKEN", STALE_TOKEN);

      installShellIpc(ipcCalls, held);
      globalThis.fetch = fakeFetch(fetchCalls);

      const { App } = await import("../src/App.tsx");
      await dom.render(h(App));
      await dom.settle();

      // A window holding a port with no listener says so. Asserting it before
      // anything moves is what makes the later "Live" a change rather than a
      // tautology — and on the pre-fix App this is where the test would end
      // anyway, still reading Offline.
      assert.equal(
        pillText(dom),
        "Offline",
        "a window with nothing listening at its port should say Offline",
      );

      // The engine restarts on a new port. Nothing in the page is told; the
      // shell is the only party that watched the handshake.
      engine = { port: FRESH_PORT, token: FRESH_TOKEN };
      const ipcBefore = ipcCalls.filter((c) => c === "codify_get_engine_info").length;

      // Wait for the app's own poll — 3s by contract, while the tab is
      // visible — and give up only if the recovery never arrives. Waiting on
      // the schedule instead of a ref matters: the automatic path is the one
      // the bug lived in, and a test that drove `forceHealthProbeRef` by hand
      // would pass with the automatic half removed.
      const deadline = Date.now() + 8000;
      const act = React.act as (body: () => Promise<void> | void) => Promise<void>;
      while (Date.now() < deadline && !healthUrls().some((u) => u.includes(`:${FRESH_PORT}/`))) {
        // The sleep runs *inside* `act` (the same move `dom.settle` makes):
        // the poll's state updates land during it, and a window of real time
        // spent outside `act` is what turns a passing test into a warning.
        await act(async () => {
          await new Promise((r) => setTimeout(r, 25));
        });
      }
      await dom.settle();

      // 1. The fix's fingerprint: the shell was asked *again*, after the move.
      //    The pre-fix guard (`ok && !authenticated`) reads this probe's
      //    `!ok` and skips the branch, so on the old code this is where the
      //    test dies.
      assert.ok(
        ipcCalls.filter((c) => c === "codify_get_engine_info").length > ipcBefore,
        "the probe never asked the shell where the engine went — the !ok half " +
          "of the recovery condition is gone",
      );

      // 2. The fresh pair was applied, and it reached localStorage — the
      //    write-through that lets the next boot start honest.
      assert.equal(
        dom.window.localStorage.getItem("CODIFY_PORT"),
        String(FRESH_PORT),
        "the moved port was never applied — the shell answered but the " +
          "answer did not reach the client",
      );
      assert.equal(dom.window.localStorage.getItem("CODIFY_TOKEN"), FRESH_TOKEN);

      // 3. The probe ran again, at the moved port — the immediate re-probe
      //    that turns "the shell said so" into "the engine says so".
      assert.ok(
        healthUrls().some((u) => u.includes(`:${FRESH_PORT}/`)),
        `no health probe at the moved port (health: ${JSON.stringify(healthUrls())})`,
      );

      // 4. The user-visible verdict: the pill says Live. This is the state the
      //    bug stranded the window in, so it is the state the assertion names.
      assert.equal(pillText(dom), "Live", "the window never recovered");
    });
  } finally {
    // The fakes come down *after* `withDom` returns, because `withDom` unmounts
    // the root on its way out and App's cleanup unlistens through the event
    // plugin's own table. Deleting it from inside the body deletes it out from
    // under that unmount, and a pin that dies in teardown tests nothing.
    globalThis.fetch = realFetch;
    (globalThis as Record<string, unknown>).WebSocket = realWebSocket;
    await new Promise((r) => setTimeout(r, 0));
    delete (globalThis as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    delete (globalThis as unknown as Record<string, unknown>).__TAURI_EVENT_PLUGIN_INTERNALS__;
  }
});
