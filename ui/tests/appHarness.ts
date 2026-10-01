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
  /**
   * Goals the engine already holds, each with the `conversation_id` of the thread it belongs to. A
   * thread that is opened lists them as its turns, the way `GET /conversations/{id}/turns` derives
   * them from goals. `PUT /goals/{id}/trace` answers like the engine: recording can start only while
   * the goal is PLANNING (409 `trace_locked` otherwise) and can be switched off at any time.
   */
  goals?: Array<Record<string, unknown> & { id: string; conversation_id: string; status: string }>;
  /** A remembered tab strip (`CODIFY_TABS`), as the app would have written it. */
  storedTabs?: unknown;
  /** Shell commands that answer with something other than the default `null`. */
  shellAnswers?: Record<string, unknown | ((args: Record<string, unknown>) => unknown)>;
  /** Shell commands that reject, with this message. */
  shellFails?: Record<string, string>;
  /**
   * What `/health` says: `up` (the default), `stale` (the engine answers and
   * refuses the token, a 401) or `down` (nothing is listening).
   */
  health?: "up" | "stale" | "down";
  /**
   * The one token the engine accepts. `/health` answers 401 to anything else, so
   * a window holding a stale token lands on the auth-stale state and a window
   * that learns the right one (from the shell, or pasted) recovers.
   */
  engineToken?: string;
  /**
   * Called with every shell command as it arrives, before it is answered. For a
   * test about a race: the callback can schedule an event to land between two
   * steps of the app, which the app's own timing never lets a test aim at.
   */
  onShell?: (cmd: string, args: Record<string, unknown>) => void;
  /**
   * Wrap the App in a `Profiler` and call this for every commit it makes. For a
   * claim about *not* rendering: a test that says a path never re-renders the app
   * has nothing else to count.
   */
  onCommit?: () => void;
  /** What `POST /workspaces/browse` answers (the folder dialog). Default: the person cancelled. */
  browse?: { status?: number; body: unknown };
  /**
   * Awaited for every engine request, after it is recorded and before it is answered. For a test
   * about ordering: holding one response while another completes is how a race the app's own
   * timing almost never produces is aimed at deliberately.
   */
  beforeRespond?: (call: EngineCall) => Promise<void> | void;
  /** Run as the standalone browser preview: no desktop shell is present at all. */
  standalone?: boolean;
  /** Values seeded into `localStorage` before the app loads. */
  localStorage?: Record<string, string>;
  /** Values seeded into `sessionStorage`: what an earlier page in this window left. */
  sessionStorage?: Record<string, string>;
  /** Rows the engine already holds for the shared tab strip (`GET /shell/tabs`). */
  engineTabs?: EngineTabRow[];
  /** Engine paths (prefix match) that answer 503, as an engine mid-restart does. */
  failRoutes?: string[];
  /**
   * Give every element a real rectangle. jsdom has no layout, so a browser pane
   * never measures itself and the app (correctly) refuses to seat a page in a
   * zero-sized pane; a test about seating needs the pane to have a size.
   */
  viewport?: { width: number; height: number };
  /**
   * Answers for routes this engine does not otherwise model, keyed `"METHOD /path"` — the voice
   * routes, say. Each is an answer or a function of the request body that returns one, and is
   * consulted before the defaults, so a test can also make a modelled route refuse.
   */
  answers?: Record<string, EngineAnswer | ((body: Record<string, unknown> | null) => EngineAnswer)>;
}

/** One answer from the engine: a JSON body, or a `Blob` for a route that answers with bytes. */
export interface EngineAnswer {
  status?: number;
  body: unknown;
}

export interface EngineTabRow {
  key: string;
  position: number;
  kind: "chat" | "browser";
  payload: string;
  updated_at?: number;
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
  /** The rows the engine currently holds for the shared strip. */
  engineTabs(): EngineTabRow[];
  /** The engine's live record of a goal, so a test can move it on behind the app's back. */
  engineGoal(id: string): Record<string, unknown> | undefined;
  /** Another window writing a row into the shared strip, behind this app's back. */
  putEngineTab(row: EngineTabRow): void;
  /** Emit a shell event to the listeners the app registered, the way Rust's `emit` does. */
  emit(event: string, payload: unknown): Promise<void>;
  /** Run a change inside React's `act`, so state it sets is flushed before you look. */
  act(body: () => void | Promise<void>): Promise<void>;
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
  const strip: EngineTabRow[] = (options.engineTabs ?? []).map((r) => ({ updated_at: 1, ...r }));
  const conversations: Conversation[] = (options.conversations ?? []).map((c) => ({ ...c }));
  const calls: string[] = [];
  const args: Array<Record<string, unknown>> = [];
  const engine: EngineCall[] = [];
  let created = 0;
  // What the engine remembers of the turns it was sent, so a later read of a thread finds them
  // the way the real one does (`GET /conversations/{id}/turns` derives them from goals).
  const goalsById = new Map<string, Record<string, unknown>>();
  const turnsByThread = new Map<string, Array<Record<string, unknown>>>();
  for (const seeded of options.goals ?? []) {
    const goal = {
      workspace_id: "ws-a",
      title: "A goal",
      description: "A goal",
      version: 1,
      dry_run: false,
      plan_only: false,
      parallel: false,
      created_at: 1,
      updated_at: 1,
      ...seeded,
    };
    goalsById.set(goal.id, goal);
    turnsByThread.set(goal.conversation_id, [
      ...(turnsByThread.get(goal.conversation_id) ?? []),
      {
        goal_id: goal.id,
        conversation_id: goal.conversation_id,
        prompt: goal.title,
        status: goal.status,
        created_at: 1,
      },
    ]);
  }

  // The shell's event bus, as the real internals keep it: `transformCallback`
  // hands out an id for each handler, `plugin:event|listen` binds an event name
  // to one, and `emit` calls every handler bound to the name.
  const callbacks = new Map<number, (payload: unknown) => void>();
  const listeners = new Map<string, Array<{ id: number; handler: (payload: unknown) => void }>>();
  let callbackSeq = 0;
  let eventSeq = 0;

  const internals: Record<string, unknown> = {
    transformCallback(callback: unknown): number {
      const id = ++callbackSeq;
      if (typeof callback === "function") callbacks.set(id, callback as (payload: unknown) => void);
      return id;
    },
    async invoke(cmd: string, a: Record<string, unknown> = {}): Promise<unknown> {
      calls.push(cmd);
      args.push(a);
      options.onShell?.(cmd, a);
      if (cmd === "plugin:event|listen") {
        const handler = callbacks.get(Number(a.handler));
        const name = String(a.event ?? "");
        const id = ++eventSeq;
        if (handler && name) listeners.set(name, [...(listeners.get(name) ?? []), { id, handler }]);
        return id;
      }
      if (cmd === "plugin:event|unlisten") {
        // A listener the app removed must stop hearing events, as in the real
        // shell: otherwise every effect re-run leaves a ghost handler behind and
        // one popup announcement opens as many tabs as the app has re-rendered.
        const name = String(a.event ?? "");
        listeners.set(name, (listeners.get(name) ?? []).filter((l) => l.id !== Number(a.eventId)));
        return null;
      }
      if (options.shellFails && cmd in options.shellFails) throw new Error(options.shellFails[cmd]);
      if (options.shellAnswers && cmd in options.shellAnswers) {
        const answer = options.shellAnswers[cmd];
        return typeof answer === "function" ? (answer as (x: Record<string, unknown>) => unknown)(a) : answer;
      }
      if (cmd === "codify_get_engine_info") return { port: 51820, token: options.engineToken ?? "e2e-token" };
      if (cmd === "codify_engine_status") return { error: null };
      if (cmd === "codify_terminal_open") return "term-1";
      if (cmd.startsWith("codify_list_") || cmd.includes("configs")) return [];
      return null;
    },
    metadata: { currentWebview: { windowLabel: "main" }, currentWindow: { label: "main" } },
  };

  const respond = (data: unknown, status = 200): Response =>
    ({
      ok: status < 400,
      status,
      json: async () => data,
      blob: async () => (data instanceof Blob ? data : new Blob([JSON.stringify(data)])),
    }) as unknown as Response;

  const route = (path: string, method: string, query: URLSearchParams, payload: Record<string, unknown> | null): Response => {
    if ((options.failRoutes ?? []).some((prefix) => path.startsWith(prefix))) {
      return respond({ code: "unavailable", message: "engine is restarting" }, 503);
    }
    const answer = options.answers?.[`${method} ${path}`];
    if (answer) {
      const { status, body } = typeof answer === "function" ? answer(payload) : answer;
      return respond(body, status ?? 200);
    }
    if (path === "/shell/tabs" && method === "GET") return respond([...strip].sort((a, b) => a.position - b.position));
    if (path === "/shell/tabs" && method === "PUT") {
      const row = { updated_at: 1, ...(payload as unknown as EngineTabRow) };
      const at = strip.findIndex((r) => r.key === row.key);
      if (at >= 0) strip[at] = row;
      else strip.push(row);
      return respond([...strip].sort((a, b) => a.position - b.position));
    }
    const removal = /^\/shell\/tabs\/([^/]+)$/.exec(path);
    if (removal && method === "DELETE") {
      const at = strip.findIndex((r) => r.key === decodeURIComponent(removal[1]));
      if (at >= 0) strip.splice(at, 1);
      return respond([...strip].sort((a, b) => a.position - b.position));
    }
    if (path === "/health") {
      if (options.health === "stale") return respond({ code: "unauthorized", message: "missing or invalid token" }, 401);
      return respond({ ok: true, authenticated: true });
    }
    if (path === "/workspaces/browse" && method === "POST") {
      return respond(options.browse?.body ?? { cancelled: true }, options.browse?.status ?? 200);
    }
    if (path === "/workspaces" && method === "POST") {
      // The engine's create: the folder the person typed becomes a project.
      const made = {
        id: `ws-new-${++created}`,
        name: String(payload?.name ?? ""),
        root_path: String(payload?.root_path ?? ""),
        design_contract_path: "",
        created_at: 1,
      };
      workspaces.push(made);
      return respond(made);
    }
    if (path === "/workspaces") return respond(workspaces);
    if (path === "/models/recent") return respond([]);
    // The two self-checks on Settings' Agent Roles tab. Both are answered with the shape the engine
    // sends, because a card that reads `status.policy` is not written for `[]`, the fallthrough below.
    if (path === "/settings/laya") {
      return respond({
        sdk_installed: false,
        sdk_disabled: false,
        sdk_error: null,
        questions: {},
        policy: { injection_block_threshold: 0.8, risk_warn_level: 3, clarify_warn_threshold: 0.6 },
      });
    }
    if (path === "/settings/runtime") {
      return respond({
        interpreter: {
          executable: "/home/me/Codify/.venv/bin/python",
          version: "3.12.4",
          version_info: [3, 12],
          implementation: "CPython",
          in_virtualenv: true,
          prefix: "/home/me/Codify/.venv",
          base_prefix: "/usr",
        },
        project_root: "/home/me/Codify",
        checkout_interpreter: "/home/me/Codify/.venv/bin/python3",
        laya_sdk: { importable: false, import_error: null, version: null, disabled_by_env: false },
        warnings: [],
      });
    }
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
      const goal = {
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
      };
      goalsById.set(goal.id, goal);
      turnsByThread.set(turn[1], [
        ...(turnsByThread.get(turn[1]) ?? []),
        { goal_id: goal.id, conversation_id: turn[1], prompt: goal.title, status: goal.status, created_at: 1 },
      ]);
      return respond(goal);
    }
    if (turn && method === "GET") return respond(turnsByThread.get(turn[1]) ?? []);
    // A goal that has finished shows its token usage; a card that reads `by_role` is not written for `[]`.
    const usage = /^\/goals\/([^/]+)\/usage$/.exec(path);
    if (usage && method === "GET") {
      return respond({
        goal_id: usage[1],
        calls: 0,
        totals: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
        by_role: {},
        by_model: {},
        parallel_peak: 1,
        parallel_waves: 0,
      });
    }
    const traceToggle = /^\/goals\/([^/]+)\/trace$/.exec(path);
    if (traceToggle && method === "GET") {
      return respond({ goal_id: traceToggle[1], calls: 0, by_role: {}, prompts_kept: false, recorded: [] });
    }
    if (traceToggle && method === "PUT") {
      const goal = goalsById.get(traceToggle[1]);
      if (!goal) return respond({ code: "not_found", message: "no such goal" }, 404);
      const enabled = payload?.enabled === true;
      if (enabled && goal.status !== "PLANNING") {
        return respond({ code: "trace_locked", message: "recording can only start before the run does" }, 409);
      }
      goal.trace = enabled;
      return respond(goal);
    }
    const goalRead = /^\/goals\/([^/]+)$/.exec(path);
    if (goalRead && method === "GET" && goalsById.has(goalRead[1])) return respond(goalsById.get(goalRead[1]));
    if (/^\/goals\/[^/]+\/events$/.test(path) && method === "GET") return respond([]);
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
    const call: EngineCall = { method, url: url.toString(), path: url.pathname, body: payload };
    engine.push(call);
    if (options.health === "down") throw new TypeError("Failed to fetch");
    await options.beforeRespond?.(call);
    if (
      options.engineToken !== undefined &&
      new Headers(init?.headers).get("authorization") !== `Bearer ${options.engineToken}`
    ) {
      return respond({ code: "unauthorized", message: "missing or invalid token" }, 401);
    }
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
      if (!options.standalone) {
        win.__TAURI_INTERNALS__ = internals;
        win.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
      }
      for (const [key, value] of Object.entries(options.localStorage ?? {})) {
        dom.window.localStorage.setItem(key, value);
      }
      for (const [key, value] of Object.entries(options.sessionStorage ?? {})) {
        dom.window.sessionStorage.setItem(key, value);
      }
      globalThis.fetch = fakeFetch;
      if (!(globalThis as Record<string, unknown>).self) {
        (globalThis as Record<string, unknown>).self = dom.window;
      }
      if (options.storedTabs !== undefined) {
        dom.window.localStorage.setItem("CODIFY_TABS", JSON.stringify(options.storedTabs));
      }
      if (options.viewport) {
        const { width, height } = options.viewport;
        const rect = { x: 0, y: 0, left: 0, top: 0, right: width, bottom: height, width, height, toJSON: () => ({}) };
        // One character cell, for xterm's measuring element: a cell as large as the
        // pane would make every terminal two columns wide.
        const cell = { ...rect, right: 9, bottom: 18, width: 9, height: 18 };
        Object.defineProperty(dom.window.HTMLElement.prototype, "getBoundingClientRect", {
          value(this: Element) {
            return (this.classList?.contains("xterm-char-measure-element") ? cell : rect) as DOMRect;
          },
          configurable: true,
          writable: true,
        });
        // xterm measures one character by the offset size of a 32-character probe;
        // jsdom answers 0, and a terminal with no character size has no grid.
        for (const [prop, of] of [["offsetWidth", (el: Element) => (el.classList?.contains("xterm-char-measure-element") ? 9 * 32 : width)], ["offsetHeight", (el: Element) => (el.classList?.contains("xterm-char-measure-element") ? 18 : height)]] as const) {
          Object.defineProperty(dom.window.HTMLElement.prototype, prop, {
            get(this: Element) {
              return of(this);
            },
            configurable: true,
          });
        }
        // xterm's fit addon sizes a terminal from the host's computed width and
        // height, which jsdom leaves empty; without them it never proposes a grid
        // and a pane never tells the shell its size.
        const realComputed = dom.window.getComputedStyle.bind(dom.window);
        dom.window.getComputedStyle = ((el: Element, pseudo?: string | null) => {
          const style = realComputed(el, pseudo);
          return new Proxy(style, {
            get(target, prop) {
              if (prop === "getPropertyValue") {
                return (name: string) => {
                  const own = target.getPropertyValue(name);
                  if (own && own !== "auto") return own;
                  if (name === "width") return `${width}px`;
                  if (name === "height") return `${height}px`;
                  // Unset padding is zero, not NaN: the fit addon does arithmetic on it.
                  return name.startsWith("padding-") ? "0px" : own;
                };
              }
              const value = Reflect.get(target, prop);
              return typeof value === "function" ? value.bind(target) : value;
            },
          });
        }) as typeof dom.window.getComputedStyle;
        // A ResizeObserver that reports once on observe, the way a real one does
        // when it starts watching an element that already has a size.
        const observer = class {
          constructor(private readonly callback: ResizeObserverCallback) {}
          observe(target: Element): void {
            this.callback([{ target, contentRect: rect as DOMRectReadOnly } as ResizeObserverEntry], this as never);
          }
          unobserve(): void {}
          disconnect(): void {}
        };
        for (const target of [globalThis, dom.window] as unknown as Record<string, unknown>[]) {
          Object.defineProperty(target, "ResizeObserver", { value: observer, configurable: true, writable: true });
        }
      }
      // `api.ts` decides its starting engine address once, when it is first
      // imported, and keeps it in module state. Without this, the first mount in a
      // process sees the seeded `CODIFY_PORT` and every later one inherits
      // whatever the previous test's app ended on — a test that seeds a stale
      // address for the app to (not) believe would silently test nothing.
      const api = await import("../src/api.ts");
      api.setEngineInfo({
        port: Number(options.localStorage?.CODIFY_PORT ?? 7430),
        token: options.localStorage?.CODIFY_TOKEN ?? "",
      });
      const React = (await import("react")).default;
      const { App } = await import("../src/App.tsx");

      const settle = async (): Promise<void> => {
        await dom.settle();
        for (let i = 0; i < 6; i++) {
          await new Promise((r) => setTimeout(r, 0));
          await new Promise((r) => requestAnimationFrame(() => r(null)));
        }
      };
      await dom.render(
        options.onCommit
          ? React.createElement(React.Profiler, { id: "app", onRender: () => options.onCommit?.() }, React.createElement(App))
          : React.createElement(App),
      );
      await settle();

      const act = React.act as (b: () => Promise<void> | void) => Promise<void>;
      try {
        await runBody();
      } finally {
        // Unmount, so the app's effects clean up. A mounted app keeps probing the
        // engine every few seconds through the shared API client, and the next
        // test's app would then be sharing its connection state with a ghost.
        await dom.render(React.createElement(React.Fragment));
      }
      async function runBody(): Promise<void> {
      await body({
        dom,
        shell: { calls, args },
        engine,
        settle,
        tabs: () => [...dom.container.querySelectorAll('[role="tab"]')] as HTMLElement[],
        engineTabs: () => [...strip],
        engineGoal: (id) => goalsById.get(id),
        putEngineTab: (row) => {
          const at = strip.findIndex((r) => r.key === row.key);
          if (at >= 0) strip[at] = { updated_at: 1, ...row };
          else strip.push({ updated_at: 1, ...row });
        },
        async emit(event, payload) {
          await act(async () => {
            for (const { handler } of listeners.get(event) ?? []) handler({ event, id: ++eventSeq, payload });
          });
        },
        act: (fn) => act(async () => { await fn(); }),
      });
      }
    });
  } finally {
    globalThis.fetch = realFetch;
    (globalThis as Record<string, unknown>).WebSocket = realWebSocket;
  }
}
