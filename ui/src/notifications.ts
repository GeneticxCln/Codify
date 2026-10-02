import { providerLabel } from "./providerLabels.ts";
import type { EngineState } from "./statusTone.ts";
import type { Goal, ModelCatalogChanged, SettingsTab } from "./types.ts";

/**
 * Notifications: what happened while you were looking at something else.
 *
 * ## What they are, and are not
 *
 * Four kinds of event, each a fact only *this window* saw happen: a goal this window was watching
 * finished or failed; a plan came back and is waiting for the person to approve it; the engine
 * connection changed; a provider's model list moved. They are kept on the client and nowhere else.
 * A goal's result is already durable (History has it), and the other three are not facts the engine
 * keeps, so a server-side list would have had nothing to hold that the window did not already know.
 * In-app only: no desktop notification, no sound. The button is in the header and the list is a
 * drawer (`docs/09` §10.18).
 *
 * ## The rules that keep it from crying wolf
 *
 * - **Watched only, for goal results.** A goal announces its end only if this window saw it in flight
 *   (its stream is open), the same rule auto-read uses (`answersToRead`). Opening a thread or restoring
 *   History never announces old results: finding an answer that was already there is not news.
 * - **Cancelled is silent.** The person did it; telling them is noise.
 * - **A plan is news only when it will wait.** In Direct Apply a plan starts itself, so "ready for your
 *   approval" would be a lie; in plan-only, dry-run or any other mode it waits for a person.
 * - **Engine changes are debounced and the first state is not news.** A flapping connection is one
 *   notice at most, and an engine that is simply up when the window opens says nothing.
 * - **A model change is the engine's diff, counted.** Added and removed per provider. The "new" badge
 *   on a provider row never shows removals, so this is the one place that says a model went away. It
 *   does not duplicate `CODIFY_SEEN_MODELS`: that decides what is *new*, this reports what *changed*.
 * - **No duplicates.** An id names the event, and the engine replays goal events on every reconnect,
 *   so the same ending can arrive twice; the second is dropped.
 *
 * What is deliberately not here: the error banner (mirroring it would flood), a missing key (a derived
 * state the App cannot see), the motion banner, and any "you have mail" count that is not about
 * something that happened.
 *
 * Pure and DOM-free, so every rule has a test that needs no renderer; `useNotifications.ts` is the
 * thin part that holds the list and the timer.
 */

export type NotificationKind = "goal" | "plan" | "engine" | "models";
export type NotificationTone = "success" | "danger" | "warning" | "info";

/** Where clicking a notification goes. A closed set: a stored one that is not in it is dropped on load. */
export type NotificationTarget =
  | { kind: "goal"; goalId: string }
  | { kind: "settings"; tab: SettingsTab };

export interface AppNotification {
  /** Names the event, so the same event can be recognised if it arrives twice. */
  id: string;
  kind: NotificationKind;
  tone: NotificationTone;
  title: string;
  detail?: string;
  /** Epoch milliseconds. */
  at: number;
  read: boolean;
  target?: NotificationTarget;
}

export const NOTIFICATIONS_KEY = "CODIFY_NOTIFICATIONS";
/** The list keeps the newest this many. A list nobody can scroll to the end of is not a record. */
export const MAX_NOTIFICATIONS = 100;

const MAX_TITLE = 120;
const MAX_DETAIL = 300;
const MAX_ID = 200;

const KINDS: ReadonlySet<string> = new Set(["goal", "plan", "engine", "models"]);
const TONES: ReadonlySet<string> = new Set(["success", "danger", "warning", "info"]);
const SETTINGS_TABS: ReadonlySet<string> = new Set(["keys", "agents", "audio", "appearance", "about"]);

const clip = (text: string, max: number): string => (text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`);

// ── the list ────────────────────────────────────────────────────────────────

/**
 * Add a notification to the front (newest first). A notification whose id is already in the list is
 * dropped, and the list is returned unchanged (the same array, so a state setter sees no change).
 */
export function appendNotification(list: readonly AppNotification[], incoming: AppNotification): AppNotification[] {
  if (list.some((n) => n.id === incoming.id)) return list as AppNotification[];
  return [incoming, ...list].slice(0, MAX_NOTIFICATIONS);
}

export function unreadCount(list: readonly AppNotification[]): number {
  return list.reduce((n, item) => (item.read ? n : n + 1), 0);
}

/** Everything read. Returns the same array when nothing was unread, so opening a drawer on an empty inbox does not re-render. */
export function markAllRead(list: readonly AppNotification[]): AppNotification[] {
  if (unreadCount(list) === 0) return list as AppNotification[];
  return list.map((n) => (n.read ? n : { ...n, read: true }));
}

// ── what is stored ──────────────────────────────────────────────────────────

/** Whether one stored entry is a notification this build can draw. Never repaired: it is kept as it is or dropped. */
function validTarget(raw: unknown): NotificationTarget | undefined | null {
  if (raw === undefined) return undefined;
  if (!raw || typeof raw !== "object") return null;
  const t = raw as { kind?: unknown; goalId?: unknown; tab?: unknown };
  if (t.kind === "goal" && typeof t.goalId === "string" && t.goalId.length > 0 && t.goalId.length <= MAX_ID) {
    return { kind: "goal", goalId: t.goalId };
  }
  if (t.kind === "settings" && typeof t.tab === "string" && SETTINGS_TABS.has(t.tab)) {
    return { kind: "settings", tab: t.tab as SettingsTab };
  }
  return null;
}

function validEntry(raw: unknown): AppNotification | null {
  if (!raw || typeof raw !== "object") return null;
  const e = raw as Record<string, unknown>;
  if (typeof e.id !== "string" || e.id.length === 0 || e.id.length > MAX_ID) return null;
  if (typeof e.kind !== "string" || !KINDS.has(e.kind)) return null;
  if (typeof e.tone !== "string" || !TONES.has(e.tone)) return null;
  if (typeof e.title !== "string" || e.title.length === 0) return null;
  if (e.detail !== undefined && typeof e.detail !== "string") return null;
  if (typeof e.at !== "number" || !Number.isFinite(e.at)) return null;
  if (typeof e.read !== "boolean") return null;
  const target = validTarget(e.target);
  if (target === null) return null;
  const out: AppNotification = {
    id: e.id,
    kind: e.kind as NotificationKind,
    tone: e.tone as NotificationTone,
    title: clip(e.title, MAX_TITLE),
    at: e.at,
    read: e.read,
  };
  if (typeof e.detail === "string" && e.detail.length > 0) out.detail = clip(e.detail, MAX_DETAIL);
  if (target) out.target = target;
  return out;
}

/**
 * The list a stored string holds. Anything unreadable is an empty list and a bad entry is dropped on its
 * own: one corrupt row must not cost the person the other ninety-nine, and a list that cannot be read
 * is not a reason to stop the app from starting. Capped, de-duplicated by id, and never repaired.
 */
export function parseNotifications(raw: string | null | undefined): AppNotification[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const seen = new Set<string>();
  const out: AppNotification[] = [];
  for (const entry of parsed) {
    const ok = validEntry(entry);
    if (!ok || seen.has(ok.id)) continue;
    seen.add(ok.id);
    out.push(ok);
    if (out.length >= MAX_NOTIFICATIONS) break;
  }
  return out;
}

export function serializeNotifications(list: readonly AppNotification[]): string {
  return JSON.stringify(list.slice(0, MAX_NOTIFICATIONS));
}

/** The slice of `Storage` this module touches, injectable so a test needs no browser. */
export interface NotificationStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** Read the stored list. A storage that throws (a private window, blocked site data) is an empty list. */
export function loadNotifications(storage: NotificationStorage | null | undefined): AppNotification[] {
  try {
    return parseNotifications(storage?.getItem(NOTIFICATIONS_KEY));
  } catch {
    return [];
  }
}

/** Write the list. A write that fails (quota, a private window) is dropped: notifications are a convenience, never a reason to throw. */
export function saveNotifications(storage: NotificationStorage | null | undefined, list: readonly AppNotification[]): void {
  try {
    storage?.setItem(NOTIFICATIONS_KEY, serializeNotifications(list));
  } catch {
    /* the list lives in memory for this session */
  }
}

// ── the four sources ────────────────────────────────────────────────────────

type GoalLike = Pick<Goal, "id" | "title" | "status" | "updated_at"> &
  Partial<Pick<Goal, "steps" | "plan_only" | "dry_run" | "version">>;

/** The first step that failed, as a sentence; undefined when none did. */
export function goalFailureDetail(goal: Pick<Goal, "steps">): string | undefined {
  const failed = goal.steps?.find((s) => s.status === "FAILED");
  return failed ? `Step ${failed.ordinal + 1}: ${failed.title} failed` : undefined;
}

/**
 * A goal this window watched has ended. `COMPLETED` is a finish and `FAILED` is a failure; `CANCELLED`
 * and anything not terminal is `null`, because the person cancelled it themselves and a goal that has not
 * ended has nothing to say. The caller is responsible for the *watched* rule: it calls this from the end
 * of a stream it opened, which is the same thing.
 *
 * The id carries `updated_at`, so a goal that is retried and fails again is a second notification and
 * the same ending replayed on a reconnect is not.
 */
export function goalNotification(goal: GoalLike, now: number): AppNotification | null {
  if (goal.status !== "COMPLETED" && goal.status !== "FAILED") return null;
  const failed = goal.status === "FAILED";
  const reason = failed ? goalFailureDetail(goal) : undefined;
  return {
    id: `goal:${goal.id}:${goal.status}:${Math.floor(goal.updated_at)}`,
    kind: "goal",
    tone: failed ? "danger" : "success",
    title: failed ? "Goal failed" : "Goal finished",
    detail: clip(reason ? `${goal.title} · ${reason}` : goal.title, MAX_DETAIL),
    at: now,
    read: false,
    target: { kind: "goal", goalId: goal.id },
  };
}

/**
 * Whether a plan that has just come back will wait for a person. A goal in `PENDING` starts itself only
 * in Direct Apply (`App.tsx`'s poll starts it when the composer is in `direct`); a plan-only or dry-run
 * goal, or any other mode, waits.
 */
export function planAwaitsApproval(goal: GoalLike, composerMode: string): boolean {
  return goal.status === "PENDING" && (goal.plan_only === true || goal.dry_run === true || composerMode !== "direct");
}

export function planNotification(goal: GoalLike, composerMode: string, now: number): AppNotification | null {
  if (!planAwaitsApproval(goal, composerMode)) return null;
  const steps = goal.steps?.length ?? 0;
  const count = steps > 0 ? `${steps} step${steps === 1 ? "" : "s"} · ` : "";
  return {
    id: `plan:${goal.id}:${goal.version ?? Math.floor(goal.updated_at)}`,
    kind: "plan",
    tone: "warning",
    title: "Plan ready for your approval",
    detail: clip(`${count}${goal.title}`, MAX_DETAIL),
    at: now,
    read: false,
    target: { kind: "goal", goalId: goal.id },
  };
}

/**
 * What a *settled* change of the engine connection means. `prev` is the last settled state (`null` before
 * any), `next` is the one that has now held for the settle time (`checking` never settles, and is not an
 * argument). Returns `null` for no change, and for an engine that is simply up when the window opens.
 *
 * The debounce is the caller's: a flap that reverses inside the settle time never reaches this function.
 */
export function engineNotification(
  prev: EngineState | null,
  next: Exclude<EngineState, "checking">,
  now: number,
  detail?: string,
): AppNotification | null {
  if (prev === next) return null;
  const base = { kind: "engine" as const, at: now, read: false, target: { kind: "settings" as const, tab: "about" as const } };
  const id = `engine:${next}:${Math.floor(now / 1000)}`;
  if (next === "live") {
    if (prev === null) return null;
    return {
      ...base,
      id,
      tone: "success",
      title: prev === "auth-stale" ? "Engine connection restored" : "Engine is back",
      detail: "Codify is connected to its engine again.",
    };
  }
  if (next === "offline") {
    return {
      ...base,
      id,
      tone: "danger",
      title: prev === null ? "Engine is not running" : "Engine went offline",
      detail: clip(detail?.trim() || "The engine stopped answering. Settings → About has what it said on the way out.", MAX_DETAIL),
    };
  }
  return {
    ...base,
    id,
    tone: "warning",
    title: "Engine refused the token",
    detail: "The engine is up but does not accept this window's token. The banner under the header has the fix.",
  };
}

const countOf = (record: unknown, provider: string): number => {
  if (!record || typeof record !== "object") return 0;
  const list = (record as Record<string, unknown>)[provider];
  return Array.isArray(list) ? list.filter((m) => typeof m === "string").length : 0;
};

/**
 * The engine said a provider's model list moved. Returns `null` when the diff holds nothing (a frame
 * with empty lists is the engine saying it looked, not that anything changed), and for a payload this
 * build cannot read: the frame crosses a process boundary, so its shape is checked and not assumed.
 */
export function catalogNotification(payload: unknown, now: number): AppNotification | null {
  if (!payload || typeof payload !== "object") return null;
  const diff = payload as Partial<ModelCatalogChanged>;
  const providers = new Set<string>([
    ...(diff.added && typeof diff.added === "object" ? Object.keys(diff.added) : []),
    ...(diff.removed && typeof diff.removed === "object" ? Object.keys(diff.removed) : []),
  ]);
  const parts: string[] = [];
  for (const provider of [...providers].sort()) {
    const added = countOf(diff.added, provider);
    const removed = countOf(diff.removed, provider);
    if (added === 0 && removed === 0) continue;
    const marks = [added > 0 ? `+${added}` : "", removed > 0 ? `−${removed}` : ""].filter(Boolean).join(" ");
    parts.push(`${providerLabel(provider) || provider} ${marks}`);
  }
  if (parts.length === 0) return null;
  const shown = parts.slice(0, 3);
  const more = parts.length - shown.length;
  const fetched = typeof diff.fetched_at === "number" && Number.isFinite(diff.fetched_at) ? diff.fetched_at : Math.floor(now / 1000);
  return {
    id: `models:${fetched}:${parts.join("|")}`,
    kind: "models",
    tone: "info",
    title: "Model list changed",
    detail: clip(more > 0 ? `${shown.join(" · ")} · and ${more} more` : shown.join(" · "), MAX_DETAIL),
    at: now,
    read: false,
    target: { kind: "settings", tab: "keys" },
  };
}

// ── words ───────────────────────────────────────────────────────────────────

/** "just now", "5 min ago", "3 h ago", "yesterday", then the date. Never negative: a clock that went backwards reads "just now". */
export function relativeTime(at: number, now: number): string {
  const seconds = Math.max(0, Math.floor((now - at) / 1000));
  if (seconds < 45) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${Math.max(1, minutes)} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  return new Date(at).toLocaleDateString();
}
