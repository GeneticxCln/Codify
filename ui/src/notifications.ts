import { PAUSE_HEADINGS, isPauseCode } from "./pauseReason.ts";
import { providerLabel } from "./providerLabels.ts";
import type { EngineState } from "./statusTone.ts";
import type { Event, Goal, ModelCatalogChanged, SettingsTab } from "./types.ts";

/**
 * Notifications: what happened while you were looking at something else.
 *
 * ## What they are, and are not
 *
 * Five kinds of event, each a fact only *this window* saw happen: a goal this window was watching
 * finished, failed or was paused by the engine (the critic asked for changes, or the conductor could not
 * finish a step); a plan came back and is waiting for the person to approve it; the engine
 * connection changed; a provider's model list moved; a program in a terminal asked for attention with
 * one of the standard escape sequences. They are kept on the client and nowhere else.
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
 * - **Cancelled is silent.** The person did it; telling them is noise. So is the person's own Pause:
 *   only a pause the *engine* made carries a reason, and only that is announced.
 * - **A pause is news only if it is the goal's current state.** The engine replays a goal's events on every
 *   reconnect, so a pause is announced only when the goal's version is the pause's own version.
 * - **A plan is news only when it will wait.** In Direct Apply a plan starts itself, so "ready for your
 *   approval" would be a lie; in plan-only, dry-run or any other mode it waits for a person.
 * - **Engine changes are debounced and the first state is not news.** A flapping connection is one
 *   notice at most, and an engine that is simply up when the window opens says nothing.
 * - **A model change is the engine's diff, counted.** Added and removed per provider. The "new" badge
 *   on a provider row never shows removals, so this is the one place that says a model went away. It
 *   does not duplicate `CODIFY_SEEN_MODELS`: that decides what is *new*, this reports what *changed*.
 * - **No duplicates.** An id names the event, and the engine replays goal events on every reconnect,
 *   so the same ending can arrive twice; the second is dropped.
 * - **A terminal is rate-limited and quiet while it has the screen.** A build script can print the
 *   sequence in a loop, and a notification about the terminal you are looking at is not news. Both
 *   checks live here, so the rule has a test that needs no renderer.
 * - **A terminal entry is not restored.** It names a live shell, and `term-1:1` happens again next
 *   launch: a stored one would either collide with this run's first report or jump to a shell that is
 *   gone. A terminal tab is not restored either, for the same reason.
 *
 * What is deliberately not here: the error banner (mirroring it would flood), a missing key (a derived
 * state the App cannot see), the motion banner, and any "you have mail" count that is not about
 * something that happened.
 *
 * Pure and DOM-free, so every rule has a test that needs no renderer; `useNotifications.ts` is the
 * thin part that holds the list and the timer.
 */

export type NotificationKind = "goal" | "plan" | "engine" | "models" | "terminal";
export type NotificationTone = "success" | "danger" | "warning" | "info";

/** Where clicking a notification goes. A closed set: a stored one that is not in it is dropped on load. */
export type NotificationTarget =
  | { kind: "goal"; goalId: string }
  | { kind: "settings"; tab: SettingsTab }
  /**
   * A tab in this window. Accepted from a live event and **never stored**: a terminal id is `term-3` in every
   * session, so a stored jump would land on this run's third terminal — a different shell, possibly a different
   * workspace. `validTarget` refuses it on load, and a terminal entry is not restored at all.
   */
  | { kind: "tab"; tabId: string };

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

const KINDS: ReadonlySet<string> = new Set(["goal", "plan", "engine", "models", "terminal"]);
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

/**
 * The unread entry that happened last, or null when nothing is unread.
 *
 * By `at` rather than by list position: the list is newest-first when this app writes it, but a caller that
 * merged stored and live entries should not have to know that, and an inbox that jumped to the second-newest
 * entry because a list arrived in another order would be wrong in a way no test of `appendNotification` sees.
 * Ties keep the earlier entry, which is the newest-first list's own answer.
 */
export function newestUnread(list: readonly AppNotification[]): AppNotification | null {
  let newest: AppNotification | null = null;
  for (const item of list) {
    if (item.read) continue;
    if (newest === null || item.at > newest.at) newest = item;
  }
  return newest;
}

/** Everything read. Returns the same array when nothing was unread, so opening a drawer on an empty inbox does not re-render. */
export function markAllRead(list: readonly AppNotification[]): AppNotification[] {
  if (unreadCount(list) === 0) return list as AppNotification[];
  return list.map((n) => (n.read ? n : { ...n, read: true }));
}

// ── what is stored ──────────────────────────────────────────────────────────

/**
 * The target a stored entry carries: the target, `undefined` for one that is recognised but cannot be stored (a
 * tab), or `null` for one this build cannot read, which drops the whole entry rather than inventing a destination.
 */
function validTarget(raw: unknown): NotificationTarget | undefined | null {
  if (raw === undefined) return undefined;
  if (!raw || typeof raw !== "object") return null;
  const t = raw as { kind?: unknown; goalId?: unknown; tab?: unknown };
  // A tab target is recognised and deliberately not stored (`NotificationTarget`): `undefined` says recognisable
  // but not storable, where `null` would drop the whole row. (A terminal entry is dropped before this anyway.)
  if (t.kind === "tab") return undefined;
  if (t.kind === "goal" && typeof t.goalId === "string" && t.goalId.length > 0 && t.goalId.length <= MAX_ID) {
    return { kind: "goal", goalId: t.goalId };
  }
  if (t.kind === "settings" && typeof t.tab === "string" && SETTINGS_TABS.has(t.tab)) {
    return { kind: "settings", tab: t.tab as SettingsTab };
  }
  // `{kind: "tab"}` is deliberately not a storable target: see `NotificationTarget`.
  return null;
}

function validEntry(raw: unknown): AppNotification | null {
  if (!raw || typeof raw !== "object") return null;
  const e = raw as Record<string, unknown>;
  if (typeof e.id !== "string" || e.id.length === 0 || e.id.length > MAX_ID) return null;
  if (typeof e.kind !== "string" || !KINDS.has(e.kind)) return null;
  if (typeof e.tone !== "string" || !TONES.has(e.tone)) return null;
  // A terminal entry names a live shell: its id is `terminal:<term>:<seq>`, unique within one run and repeated
  // next launch (`term-1:1` happens again), so restoring it would either collide with this run's first report or
  // point at a shell that no longer exists. Terminal tabs are not restored either, for the same reason.
  if (e.kind === "terminal") return null;
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
 * A goal this window is watching was paused by the engine, and says why. `ev` is the `goal_status` event that
 * just arrived and `goal` the record fetched after it. `null` for the person's own Pause (it carries no reason
 * code), for a code this build does not know, and for a pause that is not the goal's current state: the
 * stream replays history on a reconnect, and a pause the goal has since moved on from is not news.
 *
 * The id carries the goal's version, so a goal that pauses, is resumed and pauses again is two notifications
 * and the same pause replayed is one.
 */
export function pausedNotification(ev: Pick<Event, "type" | "payload">, goal: GoalLike, now: number): AppNotification | null {
  if (ev.type !== "goal_status") return null;
  const payload = ev.payload ?? {};
  if (payload.status !== "PAUSED" || goal.status !== "PAUSED") return null;
  if (!isPauseCode(payload.reason_code) || typeof payload.reason !== "string" || payload.reason.trim().length === 0) return null;
  if (typeof payload.version !== "number" || goal.version !== payload.version) return null;
  return {
    id: `paused:${goal.id}:${payload.version}`,
    kind: "goal",
    tone: "warning",
    title: PAUSE_HEADINGS[payload.reason_code],
    detail: clip(`${goal.title} · ${payload.reason.trim()}`, MAX_DETAIL),
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

/**
 * The least time between two entries from one terminal. A shell can print the sequence in a loop — a build
 * script that wraps every command does — and an inbox that reports the same terminal forty times is an inbox
 * nobody reads. Three seconds is the smallest gap that still lets a person see two real results arrive.
 */
export const TERMINAL_NOTICE_MIN_GAP_MS = 3_000;

/**
 * A program in a terminal asked for attention (`terminal-notification`, OSC 9/777/99).
 *
 * `tabId` is the tab the shell's event names, resolved by the window, and the entry points at it — a live tab
 * target, never a stored one (`NotificationTarget` says why). `lastAt` is the previous entry this terminal made
 * in this window, if any; a report inside [`TERMINAL_NOTICE_MIN_GAP_MS`] of it is dropped. The caller keeps that
 * timestamp, and must not move it on a dropped report: a script printing faster than the gap would otherwise
 * push the next real entry out forever.
 *
 * An OSC 9 carries only a body and an OSC 99 title half only a title, so whichever half is present becomes the
 * title (the one line a drawer reads first) and the other becomes the detail, when both are there.
 */
export function terminalNotification(
  notice: { id: string; seq: number; tabId: string; title: string; body: string },
  now: number,
  lastAt?: number,
): AppNotification | null {
  const named = notice.title.trim();
  const said = notice.body.trim();
  if (!notice.tabId || (named.length === 0 && said.length === 0)) return null;
  if (lastAt !== undefined && now - lastAt < TERMINAL_NOTICE_MIN_GAP_MS) return null;
  const heading = named.length > 0 ? named : said;
  const detail = named.length > 0 && said.length > 0 ? said : undefined;
  return {
    id: `terminal:${notice.id}:${notice.seq}`,
    kind: "terminal",
    tone: "info",
    title: clip(heading, MAX_TITLE),
    ...(detail ? { detail: clip(detail, MAX_DETAIL) } : {}),
    at: now,
    read: false,
    target: { kind: "tab", tabId: notice.tabId },
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
