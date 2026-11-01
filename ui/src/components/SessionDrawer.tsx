import React, { useCallback, useEffect, useState } from "react";
import { AlertCircle, Archive, RefreshCw, X } from "lucide-react";

import {
  compactConversation,
  getCompactionStatus,
  undoCompaction,
} from "../api.ts";
import type { CompactionStatus } from "../types.ts";
import { readRejection } from "../rejection.ts";
import { IconButton } from "./ui/IconButton";

/**
 * The Session drawer: what this thread's memory currently is.
 *
 * A drawer rather than a Settings tab because it is about *this* conversation,
 * not the app: it opens beside the transcript, shows the live summary and lets a
 * person compact or undo without leaving the thread. The app-wide defaults (auto
 * on/off, how many turns stay verbatim) live in Settings → Sessions.
 */
export const SessionDrawer: React.FC<{
  conversationId?: string;
  onClose: () => void;
}> = ({ conversationId, onClose }) => {
  const [status, setStatus] = useState<CompactionStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState<"compact" | "undo" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!conversationId) {
      setStatus(null);
      return;
    }
    setLoading(true);
    try {
      setStatus(await getCompactionStatus(conversationId));
      setError(null);
    } catch (err) {
      setError(readRejection(err, "Failed to read this session"));
    } finally {
      setLoading(false);
    }
  }, [conversationId]);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (which: "compact" | "undo") => {
    if (!conversationId) return;
    setSaving(which);
    setError(null);
    try {
      const next =
        which === "compact"
          ? await compactConversation(conversationId)
          : await undoCompaction(conversationId);
      setStatus(next);
    } catch (err) {
      setError(readRejection(err, which === "compact" ? "Compaction failed" : "Undo failed"));
    } finally {
      setSaving(null);
    }
  };

  return (
    <aside className="w-[22rem] max-w-[42%] shrink-0 bg-codify-surface border-l border-codify-border flex flex-col">
      <div className="flex items-center justify-between px-4 py-3 border-b border-codify-border">
        <div className="flex items-center gap-2 text-sm font-semibold text-codify-primary">
          <Archive className="w-4 h-4 text-codify-info" />
          Session
        </div>
        <div className="flex items-center gap-2">
          <IconButton label="Reload session" onClick={() => void load()} disabled={loading || !conversationId}>
            {loading ? (
              <div className="w-3.5 h-3.5 border-2 border-current/30 border-t-current rounded-full animate-spin" />
            ) : (
              <RefreshCw className="w-3.5 h-3.5" />
            )}
          </IconButton>
          <IconButton label="Close session" onClick={onClose}>
            <X className="w-4 h-4" />
          </IconButton>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto overflow-x-hidden p-3 space-y-3">
        {!conversationId ? (
          <p className="text-xs text-codify-muted px-1 py-3 leading-relaxed">
            Open a chat thread to see how it is being summarised.
          </p>
        ) : error ? (
          <div className="flex items-start gap-2 text-xs text-codify-danger px-1 py-2">
            <AlertCircle className="w-4 h-4 shrink-0" />
            <span>{error}</span>
          </div>
        ) : status ? (
          <>
            <div className="rounded-lg border border-codify-border px-3 py-2.5 space-y-1">
              <div className="flex items-center justify-between text-xs">
                <span className="text-codify-muted">Turns in this thread</span>
                <span className="font-mono text-codify-primary">{status.turns}</span>
              </div>
              <div className="flex items-center justify-between text-xs">
                <span className="text-codify-muted">Kept verbatim</span>
                <span className="font-mono text-codify-primary">{status.keep_turns}</span>
              </div>
              <div className="flex items-center justify-between text-xs">
                <span className="text-codify-muted">Auto-compaction</span>
                <span className="text-codify-primary">{status.auto ? "On" : "Off"}</span>
              </div>
              <div className="flex items-center justify-between text-xs">
                <span className="text-codify-muted">Ready to compact</span>
                <span className="font-mono text-codify-primary">{status.compactable}</span>
              </div>
            </div>

            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => void act("compact")}
                disabled={saving !== null || status.compactable === 0}
                className="flex-1 text-xs font-semibold px-3 py-2 rounded-lg bg-codify-raised border border-codify-border text-codify-primary disabled:opacity-50 disabled:cursor-not-allowed hover:border-codify-info transition-colors"
              >
                {saving === "compact" ? "Compacting…" : "Compact now"}
              </button>
              <button
                type="button"
                onClick={() => void act("undo")}
                disabled={saving !== null || status.latest === null}
                className="text-xs font-semibold px-3 py-2 rounded-lg border border-codify-border text-codify-muted disabled:opacity-50 disabled:cursor-not-allowed hover:text-codify-primary transition-colors"
              >
                Undo
              </button>
            </div>

            {status.compactable === 0 && (
              <p className="text-2xs text-codify-muted px-1 leading-relaxed">
                Nothing older than the kept turns is waiting for a summary.
              </p>
            )}

            {status.latest ? (
              <div className="rounded-lg border border-codify-border overflow-hidden">
                <div className="px-3 py-2 border-b border-codify-border text-xs text-codify-muted">
                  Latest summary · {status.latest.turns} turns ·{" "}
                  {new Date(status.latest.created_at * 1000).toLocaleString()}
                </div>
                <pre className="px-3 py-2.5 text-2xs leading-relaxed text-codify-secondary whitespace-pre-wrap break-words max-h-72 overflow-y-auto font-sans">
                  {status.latest.summary}
                </pre>
              </div>
            ) : (
              <p className="text-2xs text-codify-muted px-1 leading-relaxed">
                This thread has no summary yet. Auto-compaction adds one when the
                conversation grows past the kept turns.
              </p>
            )}
          </>
        ) : (
          <p className="text-xs text-codify-muted px-1 py-3">Reading this session…</p>
        )}
      </div>
    </aside>
  );
};
