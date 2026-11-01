import React, { useCallback, useEffect, useState } from "react";
import { Archive, AlertCircle, Check } from "lucide-react";

import { getEngineSettings, saveEngineSettings } from "../api.ts";
import type { EngineSettings } from "../types.ts";
import { readRejection } from "../rejection.ts";
import { Checkbox } from "./ui/Checkbox";

/**
 * Settings → Sessions: the app-wide compaction defaults.
 *
 * Two values, both stored as engine settings (not per thread):
 *  - auto: compact a thread once enough old turns have accumulated;
 *  - keep: how many recent turns are always sent verbatim.
 *
 * The save is one PUT and the response echoes the clamped value, so the form
 * shows what the engine stored rather than what was typed. Manual compaction of
 * one thread is in the Session drawer, beside the transcript it acts on.
 */
export const SessionSettingsPane: React.FC = () => {
  const [settings, setSettings] = useState<EngineSettings | null>(null);
  const [keepDraft, setKeepDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await getEngineSettings();
      setSettings(next);
      setKeepDraft(String(next.session_compact_keep_turns?.value ?? ""));
      setError(null);
    } catch (err) {
      setError(readRejection(err, "Failed to read session settings"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (patch: { session_compact_auto?: boolean; session_compact_keep_turns?: number }) => {
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      await saveEngineSettings(patch);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 1500);
      // The engine clamps the number, so re-read what it actually stored.
      await load();
    } catch (err) {
      setError(readRejection(err, "Failed to save session settings"));
    } finally {
      setSaving(false);
    }
  };

  const auto = settings?.session_compact_auto;
  const keep = settings?.session_compact_keep_turns;

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-codify-border bg-codify-surface p-4 space-y-3">
        <div className="flex items-start gap-3">
          <Archive className="w-4 h-4 mt-0.5 text-codify-info shrink-0" />
          <div>
            <h3 className="text-sm font-semibold text-codify-primary">Thread memory</h3>
            <p className="text-xs text-codify-muted mt-1 leading-relaxed">
              A long thread is sent to the model with its recent turns verbatim and
              older turns replaced by a rolling summary. The raw transcript is
              never changed — compaction only changes what the model is shown.
            </p>
          </div>
        </div>

        {loading ? (
          <p className="text-xs text-codify-muted">Reading session settings…</p>
        ) : !auto || !keep ? (
          <p className="text-xs text-codify-muted">
            This engine does not expose compaction settings. Update it to use them.
          </p>
        ) : (
          <div className="space-y-4 pt-1">
            <Checkbox
              checked={auto.value === 1}
              disabled={saving}
              onChange={(next) => void save({ session_compact_auto: next })}
              className="items-start"
            >
              <span>
                <span className="block text-xs font-semibold text-codify-primary">
                  Compact automatically
                </span>
                <span className="block text-2xs text-codify-muted mt-0.5 leading-relaxed">
                  When enough older turns accumulate, replace them with a summary
                  before answering. Off still allows Compact now in the Session drawer.
                </span>
              </span>
            </Checkbox>

            <label className="block">
              <span className="block text-xs font-semibold text-codify-primary">
                Recent turns kept verbatim
              </span>
              <span className="block text-2xs text-codify-muted mt-0.5 leading-relaxed">
                Follow-ups refer to the latest exchange, so these are always sent
                as they were said. {keep.value} of {keep.min}–{keep.max}.
              </span>
              <input
                type="number"
                min={keep.min}
                max={keep.max}
                value={keepDraft}
                disabled={saving}
                onChange={(e) => setKeepDraft(e.target.value)}
                onBlur={() => {
                  const value = Number(keepDraft);
                  if (Number.isFinite(value) && value !== keep.value) {
                    void save({ session_compact_keep_turns: value });
                  } else {
                    setKeepDraft(String(keep.value));
                  }
                }}
                className="mt-2 w-28 rounded-lg border border-codify-border bg-codify-surface px-2.5 py-1.5 text-xs font-mono text-codify-primary"
              />
            </label>
          </div>
        )}

        {error && (
          <div className="flex items-start gap-2 text-xs text-codify-danger">
            <AlertCircle className="w-4 h-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}
        {saved && (
          <div className="flex items-center gap-2 text-xs text-codify-success">
            <Check className="w-4 h-4" />
            Saved
          </div>
        )}
      </div>
    </div>
  );
};
