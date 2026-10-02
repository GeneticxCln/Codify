/**
 * Notifications, pure: the list, what is stored, and what each of the four sources makes of an event.
 *
 * Every rule that keeps the inbox from crying wolf is stated here without a renderer: a cancelled goal is
 * silent, a plan that starts itself is not "waiting", an engine that is simply up says nothing, the same
 * ending twice is one entry, and one corrupt stored row costs the person nothing else.
 * `notificationsApp.test.ts` mounts the whole App to prove each source is actually connected.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppNotification } from "../src/notifications.ts";
import type { PlanStep } from "../src/types.ts";

const N = await import("../src/notifications.ts");

const note = (id: string, over: Partial<AppNotification> = {}): AppNotification => ({
  id,
  kind: "goal",
  tone: "success",
  title: `title ${id}`,
  at: 1_000,
  read: false,
  ...over,
});

const step = (ordinal: number, title: string, status: string): PlanStep =>
  ({ id: `s${ordinal}`, ordinal, title, status, description: "", suggested_paths: [] }) as unknown as PlanStep;

const goal = (over: Record<string, unknown> = {}) =>
  ({ id: "g1", title: "Fix the parser", status: "COMPLETED", updated_at: 50, ...over }) as Parameters<typeof N.goalNotification>[0];

// ── the list ────────────────────────────────────────────────────────────────

test("a new notification goes to the front, and the same id twice is one entry", () => {
  const a = N.appendNotification([], note("a"));
  const b = N.appendNotification(a, note("b"));
  assert.deepEqual(b.map((n) => n.id), ["b", "a"]);
  const again = N.appendNotification(b, note("a", { title: "a replay" }));
  assert.equal(again, b, "a duplicate returned a new array, so a state setter would re-render for nothing");
  assert.equal(again.find((n) => n.id === "a")!.title, "title a", "a replay replaced the entry the person already had");
});

test("the list keeps the newest hundred, and drops the oldest", () => {
  let list: AppNotification[] = [];
  for (let i = 0; i < N.MAX_NOTIFICATIONS + 15; i += 1) list = N.appendNotification(list, note(`n${i}`));
  assert.equal(list.length, N.MAX_NOTIFICATIONS);
  assert.equal(list[0]!.id, `n${N.MAX_NOTIFICATIONS + 14}`);
  assert.equal(list[list.length - 1]!.id, "n15", "the cap kept the oldest instead of the newest");
});

test("unread arithmetic: counted, cleared, and a clear of nothing is the same list", () => {
  const list = [note("a"), note("b", { read: true }), note("c")];
  assert.equal(N.unreadCount(list), 2);
  const read = N.markAllRead(list);
  assert.equal(N.unreadCount(read), 0);
  assert.equal(read.length, 3);
  assert.equal(N.markAllRead(read), read, "marking an all-read list read made a new array");
  assert.equal(N.markAllRead([]).length, 0);
  assert.deepEqual(list.map((n) => n.read), [false, true, false], "marking read mutated its input");
});

// ── what is stored ──────────────────────────────────────────────────────────

test("a list survives a round trip exactly", () => {
  const list = [
    note("a", { detail: "the detail", target: { kind: "goal", goalId: "g1" } }),
    note("b", { kind: "engine", tone: "danger", read: true, target: { kind: "settings", tab: "about" } }),
    note("c", { kind: "models", tone: "info", target: { kind: "settings", tab: "keys" } }),
  ];
  assert.deepEqual(N.parseNotifications(N.serializeNotifications(list)), list);
});

test("anything unreadable is an empty list, and never an error", () => {
  for (const raw of [null, undefined, "", "not json", "{", "{}", "42", '"x"', "null", "true", '{"a":1}']) {
    assert.deepEqual(N.parseNotifications(raw as string), [], `${String(raw)} was not an empty list`);
  }
});

test("one bad entry is dropped on its own, and the rest are kept, never repaired", () => {
  const good = note("good", { detail: "kept" });
  const bad: unknown[] = [
    null,
    "a string",
    7,
    {},
    { ...good, id: "" },
    { ...good, id: 5 },
    { ...good, id: "k1", kind: "weather" },
    { ...good, id: "k2", tone: "purple" },
    { ...good, id: "k3", title: "" },
    { ...good, id: "k4", title: 9 },
    { ...good, id: "k5", at: "yesterday" },
    { ...good, id: "k6", at: Number.NaN },
    { ...good, id: "k7", read: "yes" },
    { ...good, id: "k8", detail: 3 },
    { ...good, id: "k9", target: { kind: "goal" } },
    { ...good, id: "k10", target: { kind: "goal", goalId: "" } },
    { ...good, id: "k11", target: { kind: "settings", tab: "danger-zone" } },
    { ...good, id: "k12", target: { kind: "open-url", url: "https://example.com" } },
    { ...good, id: "k13", target: "goal:g1" },
  ];
  const parsed = N.parseNotifications(JSON.stringify([bad[0], good, ...bad.slice(1)]));
  assert.deepEqual(parsed, [good]);
});

test("a stored id appearing twice is one entry, the cap applies on load, and long strings are clipped", () => {
  const dup = N.parseNotifications(JSON.stringify([note("a", { title: "first" }), note("a", { title: "second" })]));
  assert.deepEqual(dup.map((n) => n.title), ["first"]);

  const many = Array.from({ length: N.MAX_NOTIFICATIONS + 40 }, (_, i) => note(`n${i}`));
  assert.equal(N.parseNotifications(JSON.stringify(many)).length, N.MAX_NOTIFICATIONS);

  const long = N.parseNotifications(JSON.stringify([note("x", { title: "t".repeat(5000), detail: "d".repeat(5000) })]))[0]!;
  assert.ok(long.title.length <= 120 && long.detail!.length <= 300, "a stored string was not clipped");
  assert.ok(long.title.endsWith("…"));
});

test("a storage that throws is an empty list on load and a silent no-op on save", () => {
  const angry = {
    getItem: () => {
      throw new Error("SecurityError");
    },
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
  };
  assert.deepEqual(N.loadNotifications(angry), []);
  assert.doesNotThrow(() => N.saveNotifications(angry, [note("a")]));
  assert.deepEqual(N.loadNotifications(null), []);
  assert.doesNotThrow(() => N.saveNotifications(undefined, [note("a")]));
});

test("load and save use the one key, and a stored list is read back", () => {
  const store = new Map<string, string>();
  const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
  N.saveNotifications(storage, [note("a")]);
  assert.deepEqual([...store.keys()], ["CODIFY_NOTIFICATIONS"]);
  assert.deepEqual(N.loadNotifications(storage).map((n) => n.id), ["a"]);
});

// ── source 1: a goal finished or failed ─────────────────────────────────────

test("a finished goal is a success, a failed one a failure, and nothing else is news", () => {
  const done = N.goalNotification(goal({ status: "COMPLETED" }), 5)!;
  assert.deepEqual({ kind: done.kind, tone: done.tone, title: done.title, detail: done.detail }, { kind: "goal", tone: "success", title: "Goal finished", detail: "Fix the parser" });
  assert.deepEqual(done.target, { kind: "goal", goalId: "g1" });
  assert.equal(done.read, false);
  assert.equal(done.at, 5);

  const failed = N.goalNotification(goal({ status: "FAILED" }), 5)!;
  assert.deepEqual({ tone: failed.tone, title: failed.title }, { tone: "danger", title: "Goal failed" });

  for (const status of ["CANCELLED", "RUNNING", "PENDING", "PLANNING", "PAUSED"]) {
    assert.equal(N.goalNotification(goal({ status }), 5), null, `${status} was announced`);
  }
});

test("a failure names the step that failed, from the goal's own steps", () => {
  const steps = [step(0, "Enable incremental builds", "COMPLETED"), step(1, "Verify the cache is reused", "FAILED"), step(2, "Wrap up", "PENDING")];
  assert.equal(N.goalFailureDetail({ steps }), "Step 2: Verify the cache is reused failed");
  assert.equal(N.goalNotification(goal({ status: "FAILED", steps }), 5)!.detail, "Fix the parser · Step 2: Verify the cache is reused failed");
  assert.equal(N.goalFailureDetail({ steps: [step(0, "ok", "COMPLETED")] }), undefined);
  assert.equal(N.goalFailureDetail({}), undefined);
  // A success never carries a failure reason, even if an earlier step failed and was retried.
  assert.equal(N.goalNotification(goal({ status: "COMPLETED", steps }), 5)!.detail, "Fix the parser");
});

test("the same ending is one id however often it is replayed, and a retry that fails again is another", () => {
  const first = N.goalNotification(goal({ status: "FAILED", updated_at: 50.9 }), 1)!;
  const replay = N.goalNotification(goal({ status: "FAILED", updated_at: 50.2 }), 2)!;
  const retried = N.goalNotification(goal({ status: "FAILED", updated_at: 90 }), 3)!;
  const finished = N.goalNotification(goal({ status: "COMPLETED", updated_at: 50 }), 4)!;
  assert.equal(first.id, replay.id, "the engine replays events on a reconnect, and this ending was announced twice");
  assert.notEqual(first.id, retried.id, "a second failure after a retry was swallowed as a replay");
  assert.notEqual(first.id, finished.id);
  assert.equal(N.appendNotification(N.appendNotification([], first), replay).length, 1);
});

// ── source 2: a plan is waiting ─────────────────────────────────────────────

test("a plan is news only when it will wait for a person", () => {
  const pending = (over: Record<string, unknown> = {}) => goal({ status: "PENDING", ...over });
  // Direct Apply starts a plan by itself: telling the person it is waiting would be false.
  assert.equal(N.planNotification(pending(), "direct", 1), null);
  // Everything that does wait:
  assert.ok(N.planNotification(pending(), "plan", 1));
  assert.ok(N.planNotification(pending(), "anything-else", 1));
  assert.ok(N.planNotification(pending({ plan_only: true }), "direct", 1));
  assert.ok(N.planNotification(pending({ dry_run: true }), "direct", 1));
  // Only a goal that is actually PENDING has a plan waiting.
  for (const status of ["PLANNING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "PAUSED"]) {
    assert.equal(N.planNotification(goal({ status, plan_only: true }), "plan", 1), null, `${status} was a waiting plan`);
  }
});

test("a waiting plan says how many steps and what for, and goes to its goal", () => {
  const two = N.planNotification(goal({ status: "PENDING", plan_only: true, steps: [step(0, "a", "PENDING"), step(1, "b", "PENDING")], version: 3 }), "plan", 9)!;
  assert.deepEqual({ tone: two.tone, title: two.title, detail: two.detail, id: two.id }, { tone: "warning", title: "Plan ready for your approval", detail: "2 steps · Fix the parser", id: "plan:g1:3" });
  assert.deepEqual(two.target, { kind: "goal", goalId: "g1" });
  assert.equal(N.planNotification(goal({ status: "PENDING", plan_only: true, steps: [step(0, "a", "PENDING")] }), "plan", 1)!.detail, "1 step · Fix the parser");
  assert.equal(N.planNotification(goal({ status: "PENDING", plan_only: true }), "plan", 1)!.detail, "Fix the parser", "no steps still reads as a sentence");
  // A re-plan is a new version, and so a new notification; the same plan seen twice is one.
  const v1 = N.planNotification(goal({ status: "PENDING", plan_only: true, version: 1 }), "plan", 1)!;
  const v2 = N.planNotification(goal({ status: "PENDING", plan_only: true, version: 2 }), "plan", 1)!;
  assert.notEqual(v1.id, v2.id);
});

// ── source 3: the engine connection ─────────────────────────────────────────

test("an engine that is simply up when the window opens says nothing, and one that is down says so", () => {
  assert.equal(N.engineNotification(null, "live", 1000), null);
  const down = N.engineNotification(null, "offline", 1000)!;
  assert.deepEqual({ tone: down.tone, title: down.title }, { tone: "danger", title: "Engine is not running" });
  const stale = N.engineNotification(null, "auth-stale", 1000)!;
  assert.deepEqual({ tone: stale.tone, title: stale.title }, { tone: "warning", title: "Engine refused the token" });
});

test("each settled change of the connection is one notification, with the right tone", () => {
  const cases: Array<[Parameters<typeof N.engineNotification>[0], Parameters<typeof N.engineNotification>[1], string, string]> = [
    ["live", "offline", "danger", "Engine went offline"],
    ["offline", "live", "success", "Engine is back"],
    ["live", "auth-stale", "warning", "Engine refused the token"],
    ["auth-stale", "live", "success", "Engine connection restored"],
    ["offline", "auth-stale", "warning", "Engine refused the token"],
    ["auth-stale", "offline", "danger", "Engine went offline"],
  ];
  for (const [from, to, tone, title] of cases) {
    const n = N.engineNotification(from, to, 5000)!;
    assert.deepEqual({ tone: n.tone, title: n.title, kind: n.kind }, { tone, title, kind: "engine" }, `${from} -> ${to}`);
    assert.deepEqual(n.target, { kind: "settings", tab: "about" });
  }
  for (const state of ["live", "offline", "auth-stale"] as const) {
    assert.equal(N.engineNotification(state, state, 5000), null, `${state} -> ${state} was a change`);
  }
});

test("an outage carries the engine's own last words, and falls back to a sentence", () => {
  assert.equal(N.engineNotification("live", "offline", 1, "Traceback: address already in use")!.detail, "Traceback: address already in use");
  assert.match(N.engineNotification("live", "offline", 1)!.detail!, /stopped answering/);
  assert.match(N.engineNotification("live", "offline", 1, "   ")!.detail!, /stopped answering/, "a blank stderr line was shown as the reason");
  assert.ok(N.engineNotification("live", "offline", 1, "x".repeat(2000))!.detail!.length <= 300);
});

// ── source 4: the model catalogue ───────────────────────────────────────────

test("a model change is counted per provider, added and removed, under the provider's name", () => {
  const n = N.catalogNotification({ added: { nvidia: ["a", "b", "c"], groq: ["x"] }, removed: { nvidia: ["old"] }, fetched_at: 77 }, 5)!;
  assert.deepEqual({ kind: n.kind, tone: n.tone, title: n.title }, { kind: "models", tone: "info", title: "Model list changed" });
  assert.equal(n.detail, "Groq +1 · NVIDIA +3 −1");
  assert.deepEqual(n.target, { kind: "settings", tab: "keys" });
  // Removals alone are news too: the "new" badge on a provider row never shows them.
  assert.equal(N.catalogNotification({ added: {}, removed: { openai: ["gone"] }, fetched_at: 1 }, 5)!.detail, "OpenAI −1");
});

test("a diff with nothing in it, and a payload this build cannot read, are not news", () => {
  assert.equal(N.catalogNotification({ added: {}, removed: {}, fetched_at: 1 }, 5), null);
  assert.equal(N.catalogNotification({ added: { nvidia: [] }, removed: { groq: [] }, fetched_at: 1 }, 5), null);
  for (const junk of [null, undefined, "x", 3, [], { added: "x" }, { added: { nvidia: "a" } }, { added: { nvidia: [1, 2] } }, { removed: null }]) {
    assert.equal(N.catalogNotification(junk, 5), null, `${JSON.stringify(junk)} was announced`);
  }
});

test("a long list of providers is the first three and a count, and the id names the diff", () => {
  const added = Object.fromEntries(["a", "b", "c", "d", "e"].map((p) => [p, ["m"]]));
  const n = N.catalogNotification({ added, removed: {}, fetched_at: 1 }, 5)!;
  assert.equal(n.detail, "A +1 · B +1 · C +1 · and 2 more");
  const again = N.catalogNotification({ added, removed: {}, fetched_at: 1 }, 99)!;
  assert.equal(n.id, again.id, "the same diff replayed was a second notification");
  const other = N.catalogNotification({ added: { a: ["m", "n"] }, removed: {}, fetched_at: 1 }, 5)!;
  assert.notEqual(other.id, N.catalogNotification({ added: { a: ["m"] }, removed: {}, fetched_at: 1 }, 5)!.id, "two different diffs in one second collided");
});

test("a custom provider is shown readably, never by a raw slug", () => {
  assert.equal(N.catalogNotification({ added: { "my-gateway": ["m"] }, removed: {}, fetched_at: 1 }, 5)!.detail, "My Gateway +1");
});

// ── words ───────────────────────────────────────────────────────────────────

test("relative time reads the way a person would say it, and never goes negative", () => {
  const T = 10_000_000_000;
  const ago = (ms: number): string => N.relativeTime(T - ms, T);
  assert.equal(ago(0), "just now");
  assert.equal(ago(44_000), "just now");
  assert.equal(ago(45_000), "1 min ago");
  assert.equal(ago(5 * 60_000), "5 min ago");
  assert.equal(ago(59 * 60_000), "59 min ago");
  assert.equal(ago(60 * 60_000), "1 h ago");
  assert.equal(ago(23 * 3_600_000), "23 h ago");
  assert.equal(ago(24 * 3_600_000), "yesterday");
  assert.equal(ago(3 * 86_400_000), "3 days ago");
  assert.match(ago(30 * 86_400_000), /\d/, "an old notification has no date");
  assert.equal(N.relativeTime(T + 60_000, T), "just now", "a clock that went backwards read as the future");
});
