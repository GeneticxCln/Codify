import type { Suggestion, SuggestionKind } from "./types.ts";

/**
 * The suggestions inbox, decided without a DOM (docs/09 §10.22).
 *
 * Pure, and importing only types, so every rule here has a test that needs no renderer. The engine fills the inbox
 * and the window only ever reads it: nothing in this file, or in the code that uses it, sends a turn. Accepting is
 * the composer's `handleSendMessage` with the offer's own words, run on a person's click.
 */

/** What each kind is called. A closed set on the engine's side, so an unknown one is shown rather than dropped. */
const KIND_LABELS: Record<SuggestionKind, string> = {
  failing_command: "Failing command",
  paused_goal: "Paused goal",
  stale_health_review: "Health review",
  red_ci: "CI is red",
};

export function kindLabel(kind: string): string {
  return (KIND_LABELS as Record<string, string>)[kind] ?? "Suggestion";
}

/** How long Snooze hides an offer. A day: long enough to be out of the way, short enough to come back. */
export const SNOOZE_HOURS = 24;

/** How often the window re-reads the inbox while the engine is up. The engine looks every ten minutes. */
export const POLL_MS = 60_000;

export interface AcceptState {
  allowed: boolean;
  /** Why not, when it is not allowed. The button says so rather than going quiet. */
  reason: string | null;
}

/**
 * Whether the offer's words can be sent right now. The composer sends into the selected project, and an offer about
 * another project sent there would be a turn about the wrong repository, so it waits until that project is selected.
 */
export function acceptState(
  suggestion: Pick<Suggestion, "workspace_id">,
  selectedWorkspaceId: string | undefined,
  busy: boolean,
): AcceptState {
  if (busy) return { allowed: false, reason: "Sending…" };
  if (!selectedWorkspaceId) return { allowed: false, reason: "Select a project first." };
  if (selectedWorkspaceId !== suggestion.workspace_id) {
    return { allowed: false, reason: "This is about a different project. Select it to send this." };
  }
  return { allowed: true, reason: null };
}

/** The count on the bell: what is unread plus what is waiting in the inbox. Zero draws nothing. */
export function bellCount(unread: number, suggestions: number): number {
  return Math.max(0, unread) + Math.max(0, suggestions);
}

/** The bell's accessible name, saying what the number is made of. */
export function bellLabel(unread: number, suggestions: number): string {
  const parts: string[] = [];
  if (unread > 0) parts.push(`${unread} unread`);
  if (suggestions > 0) parts.push(`${suggestions} ${suggestions === 1 ? "suggestion" : "suggestions"}`);
  return parts.length ? `Notifications, ${parts.join(", ")}` : "Notifications";
}

/** The ids present in a list, so a refresh can tell which offers are new without comparing whole objects. */
export function idsOf(list: readonly Pick<Suggestion, "id">[]): Set<string> {
  return new Set(list.map((s) => s.id));
}
