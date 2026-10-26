/**
 * Notifications, through the whole App: the button, the drawer, and the five sources actually connected.
 *
 * `notifications.test.ts` holds the rules. What it cannot show is that `App.tsx` calls them: that the end
 * of a goal this window watched becomes an entry, that a catalogue frame the engine pushed is no longer
 * thrown away, that the engine going down is noticed once and not on every flap. These tests are the
 * engine on the other end of recorded sockets: they open a thread with a goal in flight, move the goal on
 * behind the app's back and push the same frames the engine would.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext, AppOptions } from "./appHarness.ts";

const { withApp, waitFor, conversation } = await import("./appHarness.ts");
const { engineNoticeTiming } = await import("../src/useNotifications.ts");
const { ENGINE_STATE_COPY } = await import("../src/statusTone.ts");

// Long enough for a flap to be cancelled, short enough that a test does not sit waiting for the real four seconds.
engineNoticeTiming.settleMs = 40;

const STORAGE_KEY = "CODIFY_NOTIFICATIONS";

const header = (ctx: AppContext): HTMLElement => ctx.dom.container.querySelector("header") as HTMLElement;
const bell = (ctx: AppContext): HTMLElement => {
  const found = [...header(ctx).querySelectorAll("button")].find((b) => /^Notifications/.test(b.getAttribute("aria-label") ?? ""));
  if (!found) throw new Error("the header has no Notifications button");
  return found as HTMLElement;
};
const drawer = (ctx: AppContext): Element | null => ctx.dom.container.querySelector('aside[aria-label="Notifications"]');
const badge = (ctx: AppContext): string => (bell(ctx).textContent ?? "").replace(/Notifications/, "").trim();
const rows = (ctx: AppContext): string[] =>
  [...(drawer(ctx)?.querySelectorAll("button:not([aria-label]), div.rounded-lg") ?? [])].map((r) => (r.textContent ?? "").replace(/\s+/g, " ").trim());
const open = (ctx: AppContext): Promise<void> => ctx.dom.click(bell(ctx));
const stored = (ctx: AppContext): unknown[] => JSON.parse(ctx.dom.window.localStorage.getItem(STORAGE_KEY) ?? "[]");
/** The header pill's own words — the state a person can see, rather than a probe the test assumed landed. */
const pill = (ctx: AppContext): string => {
  const labels: string[] = Object.values(ENGINE_STATE_COPY).map((copy) => copy.label);
  return [...header(ctx).querySelectorAll("span")].find((s) => labels.includes((s.textContent ?? "").trim()))?.textContent?.trim() ?? "";
};

const THREAD = conversation({ id: "c1", title: "Thread" });
const goalRow = (over: Record<string, unknown> = {}) => ({
  id: "g1",
  conversation_id: "c1",
  title: "Cache the build",
  status: "RUNNING",
  mode: "normal",
  steps: [] as unknown[],
  ...over,
});

const statusFrame = (goalId: string, status: string, sequence: number) => ({
  id: `ev-${goalId}-${sequence}`,
  goal_id: goalId,
  step_id: null,
  type: "goal_status",
  payload: { status },
  timestamp: sequence,
  sequence,
});

const failedStep = {
  id: "s2",
  goal_id: "g1",
  ordinal: 1,
  title: "Verify the cache is reused",
  description: "Run it twice",
  suggested_paths: [] as string[],
  status: "FAILED",
  review_notes: null,
  last_agent_role: "verifier",
};

/** Mount with one thread and the given goals, the thread open, so each goal's stream is the window's own. */
async function withThread(
  goals: Array<Record<string, unknown>>,
  body: (ctx: AppContext) => Promise<void>,
  extra: Partial<AppOptions> = {},
): Promise<void> {
  await withApp({ conversations: [THREAD], goals: goals as never, ...extra }, async (ctx) => {
    await ctx.dom.click(ctx.dom.container.querySelector('[data-thread-row="true"]') as Element);
    await ctx.settle();
    await body(ctx);
  });
}

const goalSocket = (ctx: AppContext, id = "g1") => {
  const s = ctx.sockets.find((x) => x.url.endsWith(`/ws/goals/${id}`));
  if (!s) throw new Error(`the app opened no stream for ${id}: ${ctx.sockets.map((x) => x.url).join(", ")}`);
  return s;
};

/** End a goal the way the engine does: the record changes, then the status event arrives. */
async function endGoal(ctx: AppContext, status: string, patch: Record<string, unknown> = {}, sequence = 5): Promise<void> {
  Object.assign(ctx.engineGoal("g1")!, { status, updated_at: 100, ...patch });
  await goalSocket(ctx).deliver(statusFrame("g1", status, sequence));
  await ctx.settle();
}

// ── the button and the drawer ───────────────────────────────────────────────

test("the Notifications button sits between Stats and History, and shows no count when there is nothing", async () => {
  await withApp({}, async (ctx) => {
    const toggles = [...header(ctx).querySelectorAll("button[aria-pressed]")].map((b) => (b.textContent ?? "").trim());
    const at = (name: RegExp) => toggles.findIndex((t) => name.test(t));
    assert.ok(at(/^Stats/) >= 0 && at(/^Notifications/) >= 0 && at(/^History/) >= 0, `header toggles: ${toggles.join(" | ")}`);
    assert.ok(at(/^Stats/) < at(/^Notifications/) && at(/^Notifications/) < at(/^History/), `wrong order: ${toggles.join(" | ")}`);
    assert.equal(bell(ctx).getAttribute("aria-label"), "Notifications");
    assert.equal(badge(ctx), "", "a zero was drawn");
    // How the panel's own buttons are found (docs/09 §7): this title must not be mistaken for them.
    assert.ok(!/^(Browser|Terminal|Keys|Settings)/.test(bell(ctx).getAttribute("title") ?? ""));
  });
});

test("the three drawers are one at a time, and each button is armed only for its own", async () => {
  await withApp({ viewport: { width: 2400, height: 900 } }, async (ctx) => {
    const armed = (re: RegExp): string | null =>
      [...header(ctx).querySelectorAll("button[aria-pressed]")].find((b) => re.test(b.textContent ?? ""))?.getAttribute("aria-pressed") ?? null;
    const which = (): string[] =>
      [
        ctx.dom.container.querySelector('[aria-label="Close statistics"]') ? "stats" : "",
        drawer(ctx) ? "notifications" : "",
        ctx.dom.container.querySelector('[aria-label="Close goal history"]') ? "history" : "",
      ].filter(Boolean);

    await ctx.dom.click(ctx.dom.byButton("Stats"));
    assert.deepEqual(which(), ["stats"]);
    await open(ctx);
    assert.deepEqual(which(), ["notifications"], "opening Notifications left Stats open");
    assert.equal(armed(/^Notifications/), "true");
    assert.equal(armed(/^Stats/), "false");
    await ctx.dom.click(ctx.dom.byButton("History"));
    assert.deepEqual(which(), ["history"]);
    assert.equal(armed(/^Notifications/), "false");
    await open(ctx);
    await open(ctx);
    assert.deepEqual(which(), [], "pressing the open drawer's button did not close it");
  });
});

test("an empty drawer says what it is for", async () => {
  await withApp({}, async (ctx) => {
    await open(ctx);
    assert.ok(drawer(ctx));
    const text = drawer(ctx)!.textContent ?? "";
    assert.match(text, /Nothing yet/);
    for (const topic of [/finish or fail/, /waiting for your approval/, /engine connection/, /model\s+list/]) assert.match(text, topic);
    // Nothing to mark read or clear, so those say so rather than doing nothing.
    assert.equal((ctx.dom.byLabel("Mark all notifications read") as HTMLButtonElement).disabled, true);
    assert.equal((ctx.dom.byLabel("Clear notifications") as HTMLButtonElement).disabled, true);
  });
});

test("the drawer is a flex sibling of the centre column, never an overlay", async () => {
  await withApp({}, async (ctx) => {
    await open(ctx);
    const aside = drawer(ctx) as Element;
    assert.ok(aside.parentElement === ctx.dom.container.querySelector("main"), "the drawer is not a child of the row the centre column is in");
    assert.ok(aside.classList.contains("w-80") && aside.classList.contains("max-w-[40%]") && aside.classList.contains("shrink-0"));
    assert.ok(!/\b(fixed|absolute)\b/.test(aside.getAttribute("class") ?? ""), "the drawer is positioned over the page, where a native browser view would paint above it");
  });
});

// ── source 1: a goal finished or failed ─────────────────────────────────────

test("a goal this window watched finishing is an entry, and opening the drawer reads it", async () => {
  await withThread([goalRow()], async (ctx) => {
    assert.equal(badge(ctx), "", "the goal announced something before it ended");
    await endGoal(ctx, "COMPLETED");
    assert.equal(badge(ctx), "1");
    assert.equal(bell(ctx).getAttribute("aria-label"), "Notifications, 1 unread");

    await open(ctx);
    const shown = rows(ctx);
    assert.equal(shown.length, 1);
    assert.match(shown[0]!, /Goal finished/);
    assert.match(shown[0]!, /Cache the build/);
    assert.match(shown[0]!, /just now/);
    assert.ok(drawer(ctx)!.querySelector('[aria-label="Unread"]'), "the new entry has no unread dot while the drawer is open");
    // Opening is reading: the count is gone, and what is stored says so too.
    assert.equal(badge(ctx), "");
    assert.deepEqual((stored(ctx) as Array<{ read: boolean }>).map((n) => n.read), [true]);
  });
});

test("a failure names the step that failed", async () => {
  await withThread([goalRow({ steps: [failedStep] })], async (ctx) => {
    await endGoal(ctx, "FAILED");
    await open(ctx);
    assert.match(rows(ctx)[0]!, /Goal failed/);
    assert.match(rows(ctx)[0]!, /Step 2: Verify the cache is reused failed/);
  });
});

test("a cancelled goal is silent, because the person did it", async () => {
  await withThread([goalRow()], async (ctx) => {
    await endGoal(ctx, "CANCELLED");
    assert.equal(badge(ctx), "");
    await open(ctx);
    assert.equal(rows(ctx).length, 0);
  });
});

test("a goal that had already finished when its thread was opened is not announced", async () => {
  // Finding an answer that was already there is not news: opening a thread or restoring History opens
  // no stream for a finished goal, so nothing can end.
  await withThread([goalRow({ status: "COMPLETED" }), goalRow({ id: "g2", title: "Old failure", status: "FAILED" })], async (ctx) => {
    assert.equal(badge(ctx), "");
    assert.equal(ctx.sockets.filter((s) => /\/ws\/goals\//.test(s.url)).length, 0, "a stream was opened for a goal that had already ended");
    await open(ctx);
    assert.equal(rows(ctx).length, 0);
  });
});

test("the same ending delivered twice is one entry, since the engine replays events on a reconnect", async () => {
  await withThread([goalRow()], async (ctx) => {
    await endGoal(ctx, "COMPLETED");
    await goalSocket(ctx).deliver(statusFrame("g1", "COMPLETED", 5));
    await ctx.settle();
    assert.equal(badge(ctx), "1", "a replayed ending counted twice");
    await open(ctx);
    assert.equal(rows(ctx).length, 1);
  });
});

// ── source 2: a plan is waiting ─────────────────────────────────────────────

const planSteps = [
  { id: "s1", goal_id: "g1", ordinal: 0, title: "Add the limit", description: "d", suggested_paths: [], status: "PENDING", review_notes: null, last_agent_role: null },
  { id: "s2", goal_id: "g1", ordinal: 1, title: "Test it", description: "d", suggested_paths: [], status: "PENDING", review_notes: null, last_agent_role: null },
];

test("a plan that will wait for approval is announced", async () => {
  await withThread([goalRow({ status: "PLANNING", plan_only: true })], async (ctx) => {
    Object.assign(ctx.engineGoal("g1")!, { status: "PENDING", steps: planSteps, version: 2 });
    await goalSocket(ctx).deliver(statusFrame("g1", "PENDING", 2));
    await ctx.settle();
    assert.equal(badge(ctx), "1");
    await open(ctx);
    assert.match(rows(ctx)[0]!, /Plan ready for your approval/);
    assert.match(rows(ctx)[0]!, /2 steps · Cache the build/);
  });
});

test("a plan that starts itself in Direct Apply is not announced as waiting", async () => {
  await withThread([goalRow({ status: "PLANNING", plan_only: false, dry_run: false })], async (ctx) => {
    Object.assign(ctx.engineGoal("g1")!, { status: "PENDING", steps: planSteps });
    await goalSocket(ctx).deliver(statusFrame("g1", "PENDING", 2));
    await ctx.settle();
    assert.equal(badge(ctx), "", "a plan that starts by itself was announced as waiting for the person");
  });
});

// ── source 4: the model catalogue ───────────────────────────────────────────

const engineSocket = (ctx: AppContext) => {
  const s = ctx.sockets.find((x) => x.url.endsWith("/ws/engine"));
  if (!s) throw new Error(`the app opened no engine stream: ${ctx.sockets.map((x) => x.url).join(", ")}`);
  return s;
};

test("a model list change is announced with counts per provider, and the frame is no longer thrown away", async () => {
  await withApp({}, async (ctx) => {
    await engineSocket(ctx).deliver({
      type: "model_catalog_changed",
      payload: { added: { nvidia: ["a", "b", "c"], groq: ["x"] }, removed: { nvidia: ["old"] }, fetched_at: 77 },
    });
    await ctx.settle();
    assert.equal(badge(ctx), "1");
    await open(ctx);
    assert.match(rows(ctx)[0]!, /Model list changed/);
    assert.match(rows(ctx)[0]!, /Groq \+1 · NVIDIA \+3 −1/);
  });
});

test("a catalogue check that found nothing, an empty diff and a bad frame are not announced", async () => {
  await withApp({}, async (ctx) => {
    const socket = engineSocket(ctx);
    await socket.deliver({ type: "model_catalog_checked", payload: { fetched_at: 5 } });
    await socket.deliver({ type: "model_catalog_changed", payload: { added: {}, removed: {}, fetched_at: 6 } });
    await socket.deliver({ type: "model_catalog_changed", payload: "garbage" });
    await socket.deliver({ type: "something_else", payload: {} });
    await ctx.settle();
    assert.equal(badge(ctx), "");
  });
});

// ── source 3: the engine connection ─────────────────────────────────────────

test("an engine that is up when the window opens says nothing", async () => {
  await withApp({}, async (ctx) => {
    await ctx.act(() => new Promise<void>((r) => setTimeout(r, engineNoticeTiming.settleMs * 3)));
    await ctx.settle();
    assert.equal(badge(ctx), "");
  });
});

test("an engine that refuses the token is announced once, and again when it recovers", async () => {
  const options: AppOptions = { health: "stale" };
  // This engine is refused from the first probe, so the notice's timer is already running when the test body starts. At the
  // file's 40 ms it can be due before the body has reached its first `act`, and a timer that fires between two `act`s is
  // an update React was not told about; at 300 it cannot be.
  const shortened = engineNoticeTiming.settleMs;
  engineNoticeTiming.settleMs = 300;
  await withApp(options, async (ctx) => {
    await waitFor(ctx, "the refused-token notice", () => badge(ctx) === "1", { timeoutMs: 5000 });
    assert.equal(badge(ctx), "1");
    await open(ctx);
    assert.match(rows(ctx)[0]!, /Engine refused the token/);
    await ctx.dom.click(bell(ctx));

    options.health = "up";
    await ctx.dom.click(ctx.dom.byButton("Retry now"));
    await open(ctx);
    await waitFor(ctx, "the connection-restored notice", () => /Engine connection restored/.test(rows(ctx)[0] ?? ""), { timeoutMs: 5000 });
    assert.match(rows(ctx)[0]!, /Engine connection restored/, `rows: ${rows(ctx).join(" || ")}`);
    assert.equal(rows(ctx).length, 2);
  }).finally(() => {
    engineNoticeTiming.settleMs = shortened;
  });
});

test("an engine that goes away and comes back is two notices, and a flap that reverses inside the settle time is none", async () => {
  const options: AppOptions = {};
  await withApp(options, async (ctx) => {
    await waitFor(ctx, "the opening probe to settle on a live engine", () => pill(ctx) === ENGINE_STATE_COPY.live.label);
    // Down, then back before the settle time passes: the connection never settled, so it is not news.
    const longer = engineNoticeTiming.settleMs;
    engineNoticeTiming.settleMs = 6000;
    options.health = "down";
    await waitFor(ctx, "the pill to say the engine is offline", () => pill(ctx) === ENGINE_STATE_COPY.offline.label);
    const downAt = Date.now();

    options.health = "up";
    await waitFor(ctx, "the pill to say the engine is live again", () => pill(ctx) === ENGINE_STATE_COPY.live.label);
    // The claim is about the notice this flap must *not* produce, so the wait has to outlast the settle time the down
    // state started — measured from the state change itself, so a slow probe does not shorten the window. The poll is
    // 3 s and the settle time 6 s: a reversal slower than that is not a flap, and would prove nothing either way.
    assert.ok(Date.now() - downAt < engineNoticeTiming.settleMs, "the reversal outlasted the settle time, so this was not a flap");
    const left = engineNoticeTiming.settleMs - (Date.now() - downAt);
    await ctx.act(() => new Promise<void>((r) => setTimeout(r, left + 250)));
    engineNoticeTiming.settleMs = longer;
    assert.equal(badge(ctx), "", "a flap that reversed inside the settle time was announced");
  });
});

test("a program in a terminal asking for attention is one entry, and its row goes to that tab", async () => {
  await withThread([], async (ctx) => {
    const terminalButton = ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement | null;
    assert.ok(terminalButton, "the sidebar has no Terminal control");
    await ctx.dom.click(terminalButton);
    await ctx.settle();

    // The terminal is the tab in front: the person is looking at it, so this is not news.
    await ctx.emit("terminal-notification", { id: "term-1", seq: 1, title: "Build", body: "finished" });
    await ctx.settle();
    assert.equal(badge(ctx), "", "the person was watching the terminal and was told about it");

    // A notification from a terminal this window never opened names nothing at all.
    await ctx.emit("terminal-notification", { id: "term-9", seq: 1, title: "Ghost", body: "" });
    await ctx.settle();
    assert.equal(badge(ctx), "", "a closed terminal's report made an entry");

    // Behind the chat tab now: this one is news.
    await ctx.dom.click(ctx.dom.container.querySelector('[data-thread-row="true"]') as Element);
    await ctx.settle();
    await ctx.emit("terminal-notification", { id: "term-1", seq: 2, title: "Build", body: "finished" });
    await ctx.settle();
    assert.equal(badge(ctx), "1");
    await open(ctx);
    const listed = rows(ctx).join(" ").replace(/\s+/g, "");
    assert.match(listed, /Build/);
    assert.match(listed, /finished/);

    // And the row goes where the event is: the terminal's tab, not the chat's.
    await ctx.dom.click(ctx.dom.byText("Build").closest("button") as Element);
    await ctx.settle();
    const shown = [...ctx.dom.container.querySelectorAll('[role="tab"]')].find((t) =>
      /Terminal/.test(t.textContent ?? ""),
    );
    assert.equal(shown?.getAttribute("aria-selected"), "true", "the row did not go to the terminal's tab");
  });
});

// ── the palette and the panel header: what needs you, without opening the drawer ─────────────────

/** Ctrl+K from the window, the way the shortcut is actually pressed. */
const openPalette = async (ctx: AppContext): Promise<void> => {
  await ctx.dom.press(ctx.dom.window.document.body, "k", { code: "KeyK", ctrlKey: true });
  await ctx.settle();
};

/** The panel header's quiet second line — the newest unread title, or empty. */
const sidebarHeader = (ctx: AppContext): string =>
  (ctx.dom.container.querySelector('div[role="heading"][aria-level="2"]')?.textContent ?? "").replace(/\s+/g, " ").trim();

const paletteRows = (ctx: AppContext): string[] =>
  [...ctx.dom.container.querySelectorAll('[role="option"]')].map((row) => (row.textContent ?? "").replace(/\s+/g, " ").trim());

test("the panel header names the newest unread entry, and stops naming it once it is read", async () => {
  await withThread([goalRow()], async (ctx) => {
    assert.doesNotMatch(sidebarHeader(ctx), /Goal finished/, "an empty inbox put a line in the header");

    await endGoal(ctx, "COMPLETED");
    assert.match(sidebarHeader(ctx), /Goal finished/, "the newest unread entry is not under the project name");

    // Opening the drawer is reading the list (`docs/09` §10.18), so the line goes too.
    await open(ctx);
    assert.doesNotMatch(sidebarHeader(ctx), /Goal finished/, "a read entry still names itself in the header");
  });
});

test("the palette's first row goes to the newest unread goal", async () => {
  await withThread([goalRow()], async (ctx) => {
    await endGoal(ctx, "COMPLETED");
    await openPalette(ctx);

    const rows = paletteRows(ctx);
    assert.match(rows[0] ?? "", /Newest unread: Goal finished/, `first row: ${rows[0] ?? "(none)"}`);
    assert.match(ctx.dom.byLabel("Palette results").textContent ?? "", /Needs you/, "the unread row is filed under the split heading");

    await ctx.dom.press(ctx.dom.byLabel("Command palette"), "Enter");
    await ctx.settle();
    await ctx.settle();
    assert.ok(
      ctx.engine.some((call) => call.method === "GET" && call.path === "/goals/g1"),
      "the palette's first row did not reopen the goal it named",
    );
  });
});

test("the palette's first row goes to the settings page an engine entry names", async () => {
  const entry = {
    id: "engine:offline:5", kind: "engine", tone: "danger", title: "Engine went offline",
    at: Date.now(), read: false, target: { kind: "settings", tab: "about" },
  };
  await withApp({ localStorage: { [STORAGE_KEY]: JSON.stringify([entry]) } }, async (ctx) => {
    assert.equal(badge(ctx), "1");
    await openPalette(ctx);
    assert.match(paletteRows(ctx)[0] ?? "", /Newest unread: Engine went offline/);

    await ctx.dom.press(ctx.dom.byLabel("Command palette"), "Enter");
    await ctx.settle();
    await ctx.settle();
    assert.match(ctx.dom.text(), /Keyboard shortcuts/, "the palette did not land on the About page the entry points at");
  });
});

test("the palette's first row goes to the terminal tab a terminal entry names", async () => {
  await withThread([], async (ctx) => {
    const terminalButton = ctx.dom.container.querySelector('button[title^="Terminal"]') as HTMLElement | null;
    assert.ok(terminalButton, "the sidebar has no Terminal control");
    await ctx.dom.click(terminalButton);
    await ctx.settle();
    // Behind the chat tab now, so the report is news rather than something the person is watching.
    await ctx.dom.click(ctx.dom.container.querySelector('[data-thread-row="true"]') as Element);
    await ctx.settle();
    await ctx.emit("terminal-notification", { id: "term-1", seq: 7, title: "Build", body: "finished" });
    await ctx.settle();
    assert.equal(badge(ctx), "1");

    await openPalette(ctx);
    assert.match(paletteRows(ctx)[0] ?? "", /Newest unread: Build/);
    await ctx.dom.press(ctx.dom.byLabel("Command palette"), "Enter");
    await ctx.settle();

    const shown = [...ctx.dom.container.querySelectorAll('[role="tab"]')].find((t) => /Terminal/.test(t.textContent ?? ""));
    assert.equal(shown?.getAttribute("aria-selected"), "true", "the palette did not go to the terminal's tab");
  });
});

// ── what is remembered ──────────────────────────────────────────────────────

test("the list is remembered across a restart, with its read state", async () => {
  const entries = [
    { id: "goal:g9:COMPLETED:1", kind: "goal", tone: "success", title: "Goal finished", detail: "An old one", at: Date.now() - 3_600_000, read: true, target: { kind: "goal", goalId: "g9" } },
    { id: "engine:offline:5", kind: "engine", tone: "danger", title: "Engine went offline", at: Date.now() - 60_000, read: false, target: { kind: "settings", tab: "about" } },
  ];
  await withApp({ localStorage: { [STORAGE_KEY]: JSON.stringify(entries) } }, async (ctx) => {
    assert.equal(badge(ctx), "1", "a restart forgot which were unread");
    await open(ctx);
    const shown = rows(ctx);
    assert.equal(shown.length, 2);
    assert.match(shown[0]!, /Goal finished.*1 h ago/);
    assert.match(shown[1]!, /Engine went offline/);
  });
});

test("one corrupt stored row costs the person nothing else, and a corrupt list is an empty one", async () => {
  const good = { id: "ok", kind: "goal", tone: "success", title: "Kept", at: Date.now(), read: true };
  await withApp({ localStorage: { [STORAGE_KEY]: JSON.stringify([{ id: 1 }, good, "junk"]) } }, async (ctx) => {
    await open(ctx);
    assert.deepEqual(rows(ctx).map((r) => r.replace(/just now|\d+ \w+ ago/, "").trim()), ["Kept"]);
  });
  await withApp({ localStorage: { [STORAGE_KEY]: "{{not json" } }, async (ctx) => {
    assert.equal(badge(ctx), "");
    await open(ctx);
    assert.equal(rows(ctx).length, 0);
  });
});

test("a new entry is written back, and a restart that read it does not rewrite it first", async () => {
  await withThread([goalRow()], async (ctx) => {
    assert.equal(ctx.dom.window.localStorage.getItem(STORAGE_KEY), null, "reading an empty list wrote one");
    await endGoal(ctx, "COMPLETED");
    assert.equal(stored(ctx).length, 1);
  });
});

// ── the controls and where a row goes ───────────────────────────────────────

test("Mark all read and Clear do what they say", async () => {
  const entries = [
    { id: "a", kind: "goal", tone: "success", title: "A", at: Date.now(), read: false },
    { id: "b", kind: "goal", tone: "danger", title: "B", at: Date.now(), read: false },
  ];
  await withApp({ localStorage: { [STORAGE_KEY]: JSON.stringify(entries) } }, async (ctx) => {
    assert.equal(badge(ctx), "2");
    await open(ctx);
    assert.equal(badge(ctx), "", "opening did not read them");
    await ctx.dom.click(ctx.dom.byLabel("Clear notifications"));
    assert.equal(rows(ctx).length, 0);
    assert.match(drawer(ctx)!.textContent ?? "", /Nothing yet/);
    assert.deepEqual(stored(ctx), [], "clearing was not written back");
    await ctx.dom.click(ctx.dom.byLabel("Close notifications"));
    assert.equal(drawer(ctx), null);
  });
});

test("pressing a model entry opens Settings on Provider Keys, and an engine entry on About", async () => {
  const entries = [
    { id: "m", kind: "models", tone: "info", title: "Model list changed", at: Date.now(), read: true, target: { kind: "settings", tab: "keys" } },
    { id: "e", kind: "engine", tone: "danger", title: "Engine went offline", at: Date.now() - 1000, read: true, target: { kind: "settings", tab: "about" } },
  ];
  const tabs = (ctx: AppContext): string[] =>
    [...ctx.dom.container.querySelectorAll("button")]
      .map((b) => (b.textContent ?? "").trim())
      .filter((t) => ["Provider Keys", "Agent Roles", "Audio", "Appearance", "About"].includes(t));
  // One entry per mount: Settings is a modal, and the next press would be behind it.
  for (const [title, aboutShowing] of [["Model list changed", false], ["Engine went offline", true]] as const) {
    await withApp({ localStorage: { [STORAGE_KEY]: JSON.stringify(entries) } }, async (ctx) => {
      assert.equal(tabs(ctx).length, 0, "Settings was already open");
      await open(ctx);
      await ctx.dom.click(ctx.dom.byText(title).closest("button") as Element);
      await ctx.settle();
      assert.equal(tabs(ctx).length, 5, `${title}: Settings did not open`);
      // About is the only tab that shows the shortcut list, so it says which tab is in front.
      assert.equal(/Keyboard shortcuts/.test(ctx.dom.text()), aboutShowing, `${title}: the wrong Settings tab is showing`);
    });
  }
});

test("pressing a goal entry reopens that goal's transcript", async () => {
  const entries = [{ id: "g", kind: "goal", tone: "success", title: "Goal finished", detail: "Old build", at: Date.now(), read: true, target: { kind: "goal", goalId: "g1" } }];
  await withApp(
    { conversations: [THREAD], goals: [goalRow({ status: "COMPLETED", title: "Old build" })] as never, localStorage: { [STORAGE_KEY]: JSON.stringify(entries) } },
    async (ctx) => {
      await open(ctx);
      const before = ctx.dom.text().split("Old build").length - 1;
      await ctx.dom.click(ctx.dom.byText("Goal finished").closest("button") as Element);
      await ctx.settle();
      await ctx.settle();
      assert.ok(ctx.engine.some((c) => c.method === "GET" && c.path === "/goals/g1"), "the goal was not asked for");
      assert.ok(ctx.dom.text().split("Old build").length - 1 > before, "the goal's transcript did not appear");
    },
  );
});

test("entries that arrive while the drawer is open are read at once, and keep their dot until it closes", async () => {
  await withThread([goalRow()], async (ctx) => {
    await open(ctx);
    assert.equal(rows(ctx).length, 0);
    await endGoal(ctx, "COMPLETED");
    assert.equal(rows(ctx).length, 1);
    assert.equal(badge(ctx), "", "the person was looking at it, and it still counted as unread");
    assert.ok(drawer(ctx)!.querySelector('[aria-label="Unread"]'));
    // Closed and reopened, it is an old entry: no dot.
    await ctx.dom.click(bell(ctx));
    await open(ctx);
    assert.ok(drawer(ctx)!.querySelector('[aria-label="Unread"]') === null, "a reopened drawer still marks old entries unread");
  });
});
