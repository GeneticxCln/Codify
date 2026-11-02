import React from "react";
import { Lightbulb, RefreshCw } from "lucide-react";
import { acceptState, kindLabel, SNOOZE_HOURS } from "../suggestions.ts";
import type { Suggestion } from "../types.ts";

/**
 * The inbox's top section in the notifications drawer: what the engine noticed, and three choices for each.
 *
 * Send puts the offer's words into the chat as the person's own message (their click, through the composer, so the
 * gate and the write gate apply exactly as if they had typed it). Snooze and Dismiss change one row. Nothing here
 * runs anything by itself, and an offer about a project that is not the selected one waits to be sent (`acceptState`).
 */
export const SuggestionsSection: React.FC<{
  items: readonly Suggestion[];
  snoozed: number;
  busyId: string | null;
  error: string | null;
  selectedWorkspaceId: string | undefined;
  /** Project names by id, for the line that says which project an offer is about. */
  workspaceNames: Readonly<Record<string, string>>;
  onSend: (suggestion: Suggestion) => void;
  onSnooze: (id: string) => void;
  onDismiss: (id: string) => void;
  onScan: () => void;
}> = ({ items, snoozed, busyId, error, selectedWorkspaceId, workspaceNames, onSend, onSnooze, onDismiss, onScan }) => (
  <section aria-label="Suggestions" className="space-y-1 pb-2 mb-2 border-b border-codify-border">
    <div className="flex items-center justify-between px-1 pt-1">
      <div className="flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-wide text-codify-muted">
        <Lightbulb className="h-3 w-3" aria-hidden="true" />
        Suggestions
      </div>
      <button
        type="button"
        onClick={onScan}
        aria-label="Check for suggestions now"
        className="flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs text-codify-muted hover:text-codify-primary hover:bg-codify-raised"
      >
        <RefreshCw className="h-3 w-3" aria-hidden="true" />
        Check now
      </button>
    </div>

    {error && (
      <p className="px-1 text-2xs text-codify-danger" role="alert">
        {error}
      </p>
    )}

    {items.length === 0 ? (
      <p className="px-1 py-1 text-2xs leading-relaxed text-codify-muted">
        Nothing to suggest.
        {snoozed > 0 ? ` ${snoozed} snoozed.` : " The engine looks for a command that keeps failing, a goal paused for hours and a health review that is due."}
      </p>
    ) : (
      items.map((s) => {
        const state = acceptState(s, selectedWorkspaceId, busyId === s.id);
        const project = workspaceNames[s.workspace_id];
        return (
          <div key={s.id} className="rounded-md border border-codify-border bg-codify-bg px-2.5 py-2">
            <div className="flex items-baseline gap-2">
              <span className="rounded bg-codify-raised px-1.5 text-2xs text-codify-secondary">{kindLabel(s.kind)}</span>
              {project && <span className="min-w-0 truncate text-2xs text-codify-muted">{project}</span>}
            </div>
            <p className="mt-1 break-words text-xs font-semibold text-codify-primary">{s.title}</p>
            {s.detail && <p className="mt-0.5 break-words text-2xs leading-relaxed text-codify-secondary">{s.detail}</p>}
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <button
                type="button"
                disabled={!state.allowed}
                title={state.reason ?? "Send this to the chat as your own message. Nothing changes until you approve a plan."}
                onClick={() => onSend(s)}
                aria-label={`Send to chat: ${s.title}`}
                className="rounded bg-codify-accent px-2 py-0.5 text-2xs font-semibold text-codify-bg disabled:cursor-not-allowed disabled:opacity-40"
              >
                Send to chat
              </button>
              <button
                type="button"
                disabled={busyId === s.id}
                onClick={() => onSnooze(s.id)}
                aria-label={`Snooze ${SNOOZE_HOURS} hours: ${s.title}`}
                className="rounded px-2 py-0.5 text-2xs text-codify-secondary hover:bg-codify-raised disabled:opacity-40"
              >
                Snooze {SNOOZE_HOURS} h
              </button>
              <button
                type="button"
                disabled={busyId === s.id}
                onClick={() => onDismiss(s.id)}
                aria-label={`Dismiss: ${s.title}`}
                className="rounded px-2 py-0.5 text-2xs text-codify-secondary hover:bg-codify-raised disabled:opacity-40"
              >
                Dismiss
              </button>
            </div>
            {!state.allowed && state.reason && busyId !== s.id && (
              <p className="mt-1 text-2xs text-codify-muted">{state.reason}</p>
            )}
          </div>
        );
      })
    )}
  </section>
);
