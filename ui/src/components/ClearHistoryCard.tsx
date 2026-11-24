import React, { useState } from "react";
import { AlertCircle, Check, Trash2 } from "lucide-react";

import { clearAllHistory, HISTORY_CLEAR_PHRASE } from "../api.ts";
import type { ClearedHistory } from "../types.ts";
import { readRejection } from "../rejection.ts";
import { Button } from "./ui/Button";

export interface ClearHistoryCardProps {
  /** Invoked after the engine deletes all history so the open window drops in-memory transcripts at once. */
  onCleared?: (result: ClearedHistory) => void;
}

function formatSummary(result: ClearedHistory): string {
  const r = result.removed;
  const parts: string[] = [
    `${r.threads} ${r.threads === 1 ? "thread" : "threads"}`,
    `${r.turns} ${r.turns === 1 ? "turn" : "turns"}`,
    `${r.events} timeline ${r.events === 1 ? "event" : "events"}`,
  ];
  const extras =
    r.recordings + r.summaries + r.observations + r.suggestions + r.stats_days + r.plan_steps + r.proposals;
  if (extras > 0) {
    parts.push(`${extras} derived ${extras === 1 ? "record" : "records"}`);
  }
  const disk = result.scrubbed
    ? "database rebuilt on disk; workspace files untouched."
    : "rows removed (another reader held the database open, so free pages will clear on the next vacuum); workspace files untouched.";
  return `Deleted ${parts.join(", ")} — ${disk}`;
}

/**
 * Settings → Sessions: delete every thread, run and derived record with a typed confirmation phrase.
 *
 * Both this card and `POST /history/clear` check `HISTORY_CLEAR_PHRASE`, so neither an accidental click nor a bare
 * request can wipe a machine's history. Keys, role configurations, settings, MCP servers, standing notes and files in
 * workspaces are kept.
 */
export const ClearHistoryCard: React.FC<ClearHistoryCardProps> = ({ onCleared }) => {
  const [armed, setArmed] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<string | null>(null);

  const matches = draft.trim().toLowerCase() === HISTORY_CLEAR_PHRASE;

  const cancel = () => {
    if (busy) return;
    setArmed(false);
    setDraft("");
    setError(null);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!matches || busy) return;
    setBusy(true);
    setError(null);
    setSummary(null);
    try {
      const result = await clearAllHistory(draft);
      setArmed(false);
      setDraft("");
      setSummary(formatSummary(result));
      onCleared?.(result);
    } catch (err) {
      setError(readRejection(err, "Could not delete history"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-xl border border-codify-border bg-codify-surface p-4 space-y-3">
      <div className="flex items-start gap-3">
        <Trash2 className="w-4 h-4 mt-0.5 text-codify-danger shrink-0" />
        <div>
          <h3 className="text-sm font-semibold text-codify-primary">Delete all history</h3>
          <p className="text-xs text-codify-muted mt-1 leading-relaxed">
            Permanently removes every thread (including archived ones), every message and run, timelines, plans,
            dry-run proposals, recordings, thread summaries, workspace observations, suggestions and daily statistics,
            then rebuilds the database file so deleted text does not linger on disk.{" "}
            <span className="text-codify-secondary">
              Your API keys, agent roles, settings, MCP servers, standing notes, open tabs and workspace files are NOT
              touched.
            </span>
          </p>
        </div>
      </div>

      {!armed ? (
        <div>
          <Button
            tone="danger"
            onClick={() => {
              setArmed(true);
              setError(null);
              setSummary(null);
            }}
          >
            Delete all history…
          </Button>
        </div>
      ) : (
        <form onSubmit={(e) => void submit(e)} className="space-y-3 pt-1">
          <label className="block">
            <span className="block text-xs font-semibold text-codify-primary">
              Type <span className="font-mono text-codify-danger">{HISTORY_CLEAR_PHRASE}</span> to confirm
            </span>
            <input
              type="text"
              value={draft}
              disabled={busy}
              autoComplete="off"
              spellCheck={false}
              placeholder={HISTORY_CLEAR_PHRASE}
              onChange={(e) => setDraft(e.target.value)}
              className="mt-1.5 w-full max-w-xs rounded-lg border border-codify-border bg-codify-bg px-2.5 py-1.5 text-xs font-mono text-codify-primary focus:outline-hidden focus:border-codify-accent"
            />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" tone="danger" disabled={!matches || busy}>
              {busy ? "Deleting…" : "Permanently delete history"}
            </Button>
            <Button tone="subtle" disabled={busy} onClick={cancel}>
              Cancel
            </Button>
          </div>
        </form>
      )}

      {error && (
        <div className="flex items-start gap-2 text-xs text-codify-danger">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}
      {summary && (
        <div className="flex items-start gap-2 text-xs text-codify-success">
          <Check className="w-4 h-4 shrink-0 mt-0.5" />
          <span>{summary}</span>
        </div>
      )}
    </div>
  );
};
