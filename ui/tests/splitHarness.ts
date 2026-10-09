/**
 * The harness the split-pane App tests share.
 *
 * Split across files because every test mounts the whole App with real xterm panes, and a file of forty of them
 * outgrows the process's memory (it was killed at about a hundred and fifty seconds). Each file is its own process.
 *
 * `panes.test.ts` is the rules, `splitPanes.test.ts` the divider, `tabMenu.test.ts` the menu. What none of them can show is
 * that `App.tsx` joins them: that a chat and a real xterm are both live in one window, that the transcript stays
 * while the terminal beside it has the focus, that the three ways in all end in the same place, that closing a
 * pane's tab, opening a browser page or narrowing the window does what `docs/09` §12 says, and that nothing about a split
 * reaches the tab strip's storage.
 *
 * Real xterm panes over the harness's fake shell, as `terminalWiring.test.ts` has; jsdom has no layout, so the window's
 * width comes from the harness's `viewport`.
 */
import { beforeEach } from "node:test";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext, AppOptions } from "./appHarness.ts";
export type { AppContext, AppOptions };

const { withApp: harnessApp, conversation: harnessConversation } = await import("./appHarness.ts");
export const React = (await import("react")).default;

/**
 * Real time, for xterm. A terminal schedules work on timers when it is laid out (the scroll area syncs a few
 * milliseconds after a resize) and does not cancel it when it is disposed, so a pane unmounted inside that window throws
 * from a timer that outlives it (`reading 'dimensions'`). A person cannot open and close a split that fast, and a
 * test can: these tests wait a beat after anything that mounts a terminal, and again before the app is torn down.
 * That is about xterm and the speed of a test, not about splitting.
 */
export const beat = (ms = 120): Promise<void> => {
  const elapsed = new Promise<void>((resolve) => setTimeout(resolve, ms));
  // Inside `act` while an app is mounted: the app's own timers and its fake engine's answers land during the wait, and each
  // is a state update React would otherwise report as outside `act`.
  return mounted ? mounted.act(() => elapsed) : elapsed;
};
/** The app a `withApp` body is running against, for `beat`. Tests in a file run one after another, so there is one at most. */
let mounted: AppContext | null = null;
/** A click, then a beat: closing or opening a split mounts or unmounts a terminal, and the next step must not undo it inside xterm's window. */
export const click = async (ctx: AppContext, el: Element): Promise<void> => {
  await ctx.dom.click(el);
  await beat();
};
export const conversation = harnessConversation;
export const withApp: typeof harnessApp = (options, body) =>
  harnessApp(options, async (ctx) => {
    mounted = ctx;
    try {
      await body(ctx);
      await beat();
    } finally {
      mounted = null;
    }
  });

export const WIDE = { width: 2400, height: 900 };

// The terminal store (`terminalBuffer.ts`) is module state that outlives an app, as it does in production, so every
// test names its own shells: a terminal id one test left owned would be a different test's problem.
let run = 0;
beforeEach(() => {
  run += 1;
});
export const id = (name: string): string => `sp-${name}-${run}`;

export const THREADS = [conversation({ id: "c1", title: "Refactor the parser" }), conversation({ id: "c2", title: "Add an index" })];
export const reply = (goal: string, text: string) => ({
  id: `e-${goal}`,
  goal_id: goal,
  step_id: null,
  type: "log",
  payload: { turn: true, message: text },
  timestamp: 2,
  sequence: 1,
});
export const SEEDED: Partial<AppOptions> = {
  conversations: THREADS,
  goals: [
    { id: "g1", conversation_id: "c1", title: "Refactor?", status: "COMPLETED", mode: "chat" },
    { id: "g2", conversation_id: "c2", title: "Index?", status: "COMPLETED", mode: "chat" },
  ],
  goalEvents: { g1: [reply("g1", "Reply from the first thread")], g2: [reply("g2", "Reply from the second thread")] },
};

/** A shell that hands out these ids, one per `codify_terminal_open`, in order. */
export const shells = (...ids: string[]): AppOptions["shellAnswers"] => {
  let next = 0;
  return { codify_terminal_open: () => ids[Math.min(next++, ids.length - 1)] };
};

export const commands = (ctx: AppContext, name: string): Array<Record<string, unknown>> =>
  ctx.shell.calls.flatMap((c, i) => (c === name ? [ctx.shell.args[i]!] : []));

export const panes = (ctx: AppContext): HTMLElement[] => [...ctx.dom.container.querySelectorAll<HTMLElement>("[data-pane]")];
export const pane = (ctx: AppContext, side: 0 | 1): HTMLElement => {
  const found = panes(ctx).find((p) => p.getAttribute("data-pane") === String(side));
  if (!found) throw new Error(`no pane ${side}; there are ${panes(ctx).length}`);
  return found;
};
export const focusedSide = (ctx: AppContext): string | null =>
  panes(ctx).find((p) => p.getAttribute("data-focused") === "true")?.getAttribute("data-pane") ?? null;
export const terminalIn = (root: Element): string | null => root.querySelector("[data-terminal-id]")?.getAttribute("data-terminal-id") ?? null;
export const screenOf = (ctx: AppContext, terminalId: string): string =>
  ctx.dom.container.querySelector(`[data-terminal-id="${terminalId}"] .xterm-rows`)?.textContent ?? "";
export const composer = (ctx: AppContext): HTMLTextAreaElement | null =>
  ctx.dom.container.querySelector('textarea[aria-label="Chat prompt"]');
export const selectedTab = (ctx: AppContext): string =>
  ctx.tabs().find((t) => t.getAttribute("aria-selected") === "true")?.getAttribute("aria-label") ?? "";
export const tabNamed = (ctx: AppContext, prefix: string): HTMLElement => {
  const found = ctx.tabs().find((t) => (t.getAttribute("aria-label") ?? "").startsWith(prefix));
  if (!found) throw new Error(`no tab "${prefix}": ${ctx.tabs().map((t) => t.getAttribute("aria-label")).join(" | ")}`);
  return found;
};
export const terminalTabs = (ctx: AppContext): HTMLElement[] => ctx.tabs().filter((t) => (t.getAttribute("aria-label") ?? "").startsWith("Terminal:"));

export async function openShell(ctx: AppContext): Promise<void> {
  await click(ctx, ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement);
  await ctx.settle();
  await beat();
}
export async function openThread(ctx: AppContext, id: string): Promise<void> {
  await click(ctx, ctx.dom.container.querySelector(`[data-thread-id="${id}"]`) as Element);
  await ctx.settle();
}

/** Ctrl+<key> on the document, returning whether the app claimed it. */
export async function chord(ctx: AppContext, key: string, code: string): Promise<boolean> {
  const event = new ctx.dom.window.KeyboardEvent("keydown", { key, code, ctrlKey: true, bubbles: true, cancelable: true });
  await React.act(async () => {
    ctx.dom.window.document.body.dispatchEvent(event);
  });
  await ctx.settle();
  return event.defaultPrevented;
}
export const split = async (ctx: AppContext): Promise<boolean> => {
  const claimed = await chord(ctx, ".", "Period");
  await beat();
  return claimed;
};

export async function rightClick(ctx: AppContext, el: Element): Promise<void> {
  await ctx.act(async () => {
    el.dispatchEvent(new ctx.dom.window.MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 30, clientY: 20 }));
  });
}
export const menuItems = (ctx: AppContext): HTMLElement[] => [...ctx.dom.container.querySelectorAll<HTMLElement>('[role="menu"] [role="menuitem"]')];

export async function pressIn(ctx: AppContext, el: Element): Promise<void> {
  await ctx.act(async () => {
    el.dispatchEvent(new ctx.dom.window.MouseEvent("pointerdown", { bubbles: true, cancelable: true }));
  });
}

/** A shell and a thread open, the thread in view: the starting point for most of these. */
export async function withChatAndShell(body: (ctx: AppContext) => Promise<void>, extra: Partial<AppOptions> = {}): Promise<void> {
  await withApp({ viewport: WIDE, ...SEEDED, shellAnswers: shells(id("t1"), id("t2"), id("t3")), ...extra }, async (ctx) => {
    await openShell(ctx);
    await openThread(ctx, "c1");
    await body(ctx);
  });
}

export const CLIPBOARD_KEY = "CODIFY_CLIPBOARD";
export function clipboardEvent(ctx: AppContext, type: "paste", text: string): Event {
  const event = new ctx.dom.window.Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, "clipboardData", { value: { getData: () => text, setData: () => {} } });
  return event;
}
export const pasteSomething = (ctx: AppContext, text: string): Promise<void> =>
  ctx.act(async () => void ctx.dom.window.document.body.dispatchEvent(clipboardEvent(ctx, "paste", text)));
export const clipRow = (ctx: AppContext, text: string): HTMLElement => {
  const found = [...ctx.dom.container.querySelectorAll<HTMLElement>('aside[aria-label="Clipboard"] li')].find((li) => li.querySelector("pre")?.textContent === text);
  if (!found) throw new Error(`no clip "${text}"`);
  return found;
};

export const STRIP = (tabs: Array<{ key: string; conversationId: string; workspaceId: string }>) => ({
  version: 1,
  layout: { tabs: tabs.map((t) => ({ kind: "chat", ...t })), activeIndex: 0 },
  pendingRemovals: [],
  pendingWrites: [],
});
