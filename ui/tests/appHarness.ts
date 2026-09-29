/**
 * The whole App, mounted, against a fake shell and a recording fake engine.
 *
 * Tests that only read `App.tsx` as text prove the words are there, not that
 * anything happens: a control wired to nothing has the same source-level
 * fingerprints as a working one. This lets a test click the control and look at
 * what the app then *asked the engine to do*.
 *
 * What is faked, and how honestly:
 * - the desktop shell (`__TAURI_INTERNALS__`): every command is recorded in
 *   `shell.calls`, answers are minimal and stated below;
 * - the engine: `fetch` is a router that records every request (method, URL,
 *   parsed body) in `engine`, and answers the handful of routes the shell needs
 *   to come up, with conversations kept in memory so a create is visible in the
 *   next list;
 * - websockets: silent, since nothing here listens for a stream.
 */
import { withDom } from "./dom.ts";
import type { Dom } from "./dom.ts";
import type { Conversation } from "../src/types.ts";

export interface EngineCall {
  method: string;
  url: string;
  path: string;
  body: Record<string, unknown> | null;
}

export interface AppOptions {
  /** Workspaces the engine reports. The first is auto-selected by the app. */
  workspaces?: Array<{ id: string; name: string; root_path: string }>;
  /** Conversations the engine already holds. */
  conversations?: Conversation[];
  /** A remembered tab strip (`CODIFY_TABS`), as the app would have written it. */
  storedTabs?: unknown;
}

export interface AppContext {
  dom: Dom;
  /** Every shell command the app invoked, in order. */
  shell: { calls: string[]; args: Array<Record<string, unknown>> };
  /** Every engine request the app made, in order. */
  engine: EngineCall[];
  /** Let effects, timers and animation frames run. */
  settle(): Promise<void>;
  /** The strip's tabs. */
  tabs(): HTMLElement[];
}

const DEFAULT_WORKSPACES = [
  { id: "ws-a", name: "Alpha", root_path: "/tmp/e2e-alpha" },
];

export function conversation(over: Partial<Conversation> & { id: string }): Conversation {
  return {
    workspace_id: "ws-a",
    title: "",
    archived: false,
    parent_id: null,
    created_at: 1,
    updated_at: 1,
    ...over,
  } as Conversation;
}

export async function withApp(
  options: AppOptions,
  body: (ctx: AppContext) => Promise<void>,
): Promise<void> {
  const workspaces = (options.workspaces ?? DEFAULT_WORKSPACES).map((w) => ({
    design_contract_path: "",
    created_at: 1,
    ...w,
  }));
  // Copied one level deeper than the list: a test that archives a thread must not
  // archive it for the next test that shares the fixture.
  const conversations: Conversation[] = (options.conversations ?? []).map((c) => ({ ...c }));
  const calls: string[] = [];
  const args: Array<Record<string, unknown>> = [];
  const engine: EngineCall[] = [];
  let created = 0;

  const internals: Record<string, unknown> = {
    transformCallback(): number {
      return 0;
    },
    async invoke(cmd: string, a: Record<string, unknown> = {}): Promise<unknown> {
      calls.push(cmd);
      args.push(a);
      if (cmd === "plugin:event|listen") return 1;
      if (cmd === "codify_get_engine_info") return { port: 51820, token: "e2e-token" };
      if (cmd === "codify_engine_status") return { error: null };
      if (cmd === "codify_terminal_open") return "term-1";
      if (cmd.startsWith("codify_list_") || cmd.includes("configs")) return [];
      return null;
    },
    metadata: { currentWebview: { windowLabel: "main" }, currentWindow: { label: "main" } },
  };

  const respond = (data: unknown, status = 200): Response =>
    ({ ok: status < 400, status, json: async () => data }) as unknown as Response;

  const route = (path: string, method: string, query: URLSearchParams, payload: Record<string, unknown> | null): Response => {
    if (path === "/health") return respond({ ok: true, authenticated: true });
    if (path === "/workspaces") return respond(workspaces);
    if (path === "/models/recent") return respond([]);
    if (path === "/models") {
      // One chat model, so the composer will send: with none, the app refuses to
      // start a turn ("No model available") before it reaches the engine.
      return respond({
        models: [{ id: "test-model", name: "test-model", provider: "ollama", description: "", supports_chat: true }],
        providers: [],
        fetched_at: 0,
        cached: false,
      });
    }
    if (path === "/conversations" && method === "GET") {
      const ws = query.get("workspace_id");
      return respond(conversations.filter((c) => (!ws || c.workspace_id === ws) && !c.archived));
    }
    if (path === "/conversations" && method === "POST") {
      const made = conversation({
        id: `c-new-${++created}`,
        workspace_id: String(payload?.workspace_id ?? ""),
        title: String(payload?.title ?? ""),
        parent_id: (payload?.parent_id as string | undefined) ?? null,
      });
      conversations.push(made);
      return respond(made);
    }
    const one = /^\/conversations\/([^/]+)$/.exec(path);
    if (one && method === "PATCH") {
      const found = conversations.find((c) => c.id === one[1]);
      if (found && typeof payload?.title === "string") found.title = payload.title;
      return respond(found ?? {});
    }
    const turn = /^\/conversations\/([^/]+)\/turns$/.exec(path);
    if (turn && method === "POST") {
      return respond({
        id: `goal-${++created}`,
        workspace_id: "ws-a",
        conversation_id: turn[1],
        title: String(payload?.prompt ?? ""),
        description: String(payload?.prompt ?? ""),
        status: "PLANNING",
        version: 1,
        dry_run: true,
        plan_only: false,
        parallel: false,
        created_at: 1,
        updated_at: 1,
      });
    }
    const archive = /^\/conversations\/([^/]+)\/archive$/.exec(path);
    if (archive && method === "POST") {
      const found = conversations.find((c) => c.id === archive[1]);
      if (found) found.archived = true;
      return respond(found ?? {});
    }
    return respond([]);
  };

  const fakeFetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input));
    const method = (init?.method ?? "GET").toUpperCase();
    let payload: Record<string, unknown> | null = null;
    if (typeof init?.body === "string") {
      try {
        payload = JSON.parse(init.body) as Record<string, unknown>;
      } catch {
        payload = null;
      }
    }
    engine.push({ method, url: url.toString(), path: url.pathname, body: payload });
    return route(url.pathname, method, url.searchParams, payload);
  }) as unknown as typeof fetch;

  const realFetch = globalThis.fetch;
  const realWebSocket = globalThis.WebSocket;
  (globalThis as Record<string, unknown>).WebSocket = class {
    close(): void {}
    addEventListener(): void {}
    removeEventListener(): void {}
  };
  try {
    await withDom(async (dom) => {
      const win = globalThis.window as unknown as Record<string, unknown>;
      win.__TAURI_INTERNALS__ = internals;
      win.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
      globalThis.fetch = fakeFetch;
      if (!(globalThis as Record<string, unknown>).self) {
        (globalThis as Record<string, unknown>).self = dom.window;
      }
      if (options.storedTabs !== undefined) {
        dom.window.localStorage.setItem("CODIFY_TABS", JSON.stringify(options.storedTabs));
      }
      const React = (await import("react")).default;
      const { App } = await import("../src/App.tsx");

      const settle = async (): Promise<void> => {
        await dom.settle();
        for (let i = 0; i < 6; i++) {
          await new Promise((r) => setTimeout(r, 0));
          await new Promise((r) => requestAnimationFrame(() => r(null)));
        }
      };
      await dom.render(React.createElement(App));
      await settle();

      await body({
        dom,
        shell: { calls, args },
        engine,
        settle,
        tabs: () => [...dom.container.querySelectorAll('[role="tab"]')] as HTMLElement[],
      });
    });
  } finally {
    globalThis.fetch = realFetch;
    (globalThis as Record<string, unknown>).WebSocket = realWebSocket;
  }
}
