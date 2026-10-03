import React, { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Brain, RotateCcw, Save, ShieldCheck } from "lucide-react";

import { getEngineSettings, saveEngineSettings } from "../api";
import { canSaveChoice, ConductorChoice, conductorFallback, conductorStatus, needsClear } from "../conductorSettings";
import { readRejection } from "../rejection.ts";
import { staleTarget } from "../staleModel";
import type { ModelOption, ProviderModelStatus } from "../types.ts";
import { ModelSelect } from "./ModelSelect";
import { ProviderSelect } from "./ProviderSelect";

interface ConductorSettingsCardProps {
  /** Discovered models, so the field autocompletes real ids like a role's does. */
  models?: ModelOption[];
  /** Per-provider discovery outcome, so a retired model can be flagged. */
  providerStatus?: ProviderModelStatus[];
  /** The provider slugs the engine ships (`GET /settings/providers`). */
  builtins: string[];
  /** The scribe's stored model, so "borrowed" can name it rather than shrug. */
  scribeModel?: string;
  /** The scribe's stored *fallback* model — where the conductor's second target
   * comes from while it is still borrowing the scribe's row. */
  scribeFallbackModel?: string;
  onRefreshModels?: () => void;
  refreshingModels?: boolean;
}

/**
 * One whole-number engine setting, with its own draft and save.
 *
 * The three budget fields differ only in label and bounds, and the panel's own
 * width/retention fields had already grown the same save-and-echo-the-clamp
 * block three times over. Extracted here rather than copied again, and kept
 * inside this file because nothing outside the card needs it.
 */
const NumberSetting: React.FC<{
  id: string;
  label: string;
  hint: string;
  value: number | null;
  min: number;
  max: number;
  onSave: (value: number) => Promise<number>;
}> = ({ id, label, hint, value, min, max, onSave }) => {
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    if (value !== null) setDraft(String(value));
  }, [value]);

  const save = async () => {
    const parsed = parseInt(draft, 10);
    if (!Number.isFinite(parsed)) {
      setMsg({ ok: false, text: "Enter a whole number." });
      return;
    }
    setSaving(true);
    setMsg(null);
    try {
      const saved = await onSave(parsed);
      setDraft(String(saved));
      setMsg(
        saved === parsed
          ? { ok: true, text: "Saved — the next run uses it." }
          : { ok: true, text: `Clamped to ${saved} (allowed ${min}–${max}).` }
      );
    } catch (err: any) {
      setMsg({ ok: false, text: readRejection(err, "Could not save this setting.") });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={id} className="text-xs text-codify-secondary font-medium" title={hint}>
          {label}
        </label>
        <input
          id={id}
          type="number"
          min={min}
          max={max}
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            setMsg(null);
          }}
          disabled={value === null || saving}
          className="w-20 bg-codify-surface border border-codify-border rounded-lg px-2.5 py-1.5 text-xs text-codify-secondary focus:outline-hidden focus:border-codify-accent font-mono disabled:opacity-40"
        />
        <button
          type="button"
          onClick={save}
          disabled={value === null || saving || draft === String(value)}
          className="px-3 py-1.5 bg-codify-accent text-codify-bg hover:brightness-110 text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
        >
          {saving ? "Saving..." : "Save"}
        </button>
        {msg && (
          <span className={`text-xs ${msg.ok ? "text-codify-success" : "text-codify-danger"}`}>{msg.text}</span>
        )}
      </div>
      <p className="text-xs text-codify-muted leading-relaxed">{hint}</p>
    </div>
  );
};

/**
 * The conductor's own model and budgets.
 *
 * The conductor is a loop rather than a ninth role, so this is not a role card
 * and does not live with the eight. It is a card here for one reason: the
 * conductor is the only component in the app whose model a user could not
 * choose, because the keys it reads were, for a long time, writable by nothing
 * at all — so it silently ran on the scribe's model on every install.
 *
 * The two model fields are a pair the engine only honours whole. Half a pair is
 * stored and then ignored (`_conductor_config` borrows the scribe's row), which
 * is why Save refuses one here and clearing is its own button: a setting the
 * user can change and watch nothing happen is worse than a setting that is
 * visibly refused.
 */
export const ConductorSettingsCard: React.FC<ConductorSettingsCardProps> = ({
  models = [],
  providerStatus = [],
  builtins,
  scribeModel = "",
  scribeFallbackModel = "",
  onRefreshModels,
  refreshingModels = false,
}) => {
  const [stored, setStored] = useState<ConductorChoice | null>(null);
  const [draft, setDraft] = useState<ConductorChoice>({ provider: "", model: "" });
  const [storedFallback, setStoredFallback] = useState<ConductorChoice>({
    provider: "",
    model: "",
  });
  const [draftFallback, setDraftFallback] = useState<ConductorChoice>({
    provider: "",
    model: "",
  });
  const [turns, setTurns] = useState<{ value: number; min: number; max: number } | null>(null);
  const [moves, setMoves] = useState<{ value: number; min: number; max: number } | null>(null);
  const [drives, setDrives] = useState<number | null>(null);
  const [drivesSaving, setDrivesSaving] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEngineSettings()
      .then((s) => {
        if (cancelled || !s) return;
        // Optional on purpose: an engine that predates the setting answers
        // without these, and an absent card beats a card whose save 400s.
        if (!s.conductor_provider || !s.conductor_model) return;
        const choice = {
          provider: s.conductor_provider.value,
          model: s.conductor_model.value,
        };
        setStored(choice);
        setDraft(choice);
        if (s.conductor_fallback_provider && s.conductor_fallback_model) {
          const pair = {
            provider: s.conductor_fallback_provider.value,
            model: s.conductor_fallback_model.value,
          };
          setStoredFallback(pair);
          setDraftFallback(pair);
        }
        if (s.conductor_max_turns) {
          setTurns({
            value: s.conductor_max_turns.value,
            min: s.conductor_max_turns.min,
            max: s.conductor_max_turns.max,
          });
        }
        if (s.conductor_max_moves) {
          setMoves({
            value: s.conductor_max_moves.value,
            min: s.conductor_max_moves.min,
            max: s.conductor_max_moves.max,
          });
        }
        if (s.conductor_drives_execution) {
          setDrives(s.conductor_drives_execution.value);
        }
        setLoadError(null);
      })
      .catch((err: any) => {
        if (cancelled) return;
        setLoadError(readRejection(err, "Could not load the conductor settings."));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const status = useMemo(
    () => conductorStatus(draft, scribeModel),
    [draft, scribeModel]
  );
  const fallbackStatus = useMemo(
    () => conductorFallback(draft, draftFallback, scribeFallbackModel),
    [draft, draftFallback, scribeFallbackModel]
  );
  const verdict = useMemo(() => canSaveChoice(draft), [draft]);
  const fallbackVerdict = useMemo(() => canSaveChoice(draftFallback), [draftFallback]);
  const canClear = stored ? needsClear(stored, draft) : false;
  const canClearFallback = needsClear(storedFallback, draftFallback);
  const dirty = stored
    ? draft.provider.trim() !== stored.provider.trim() ||
      draft.model.trim() !== stored.model.trim()
    : false;
  const dirtyFallback =
    draftFallback.provider.trim() !== storedFallback.provider.trim() ||
    draftFallback.model.trim() !== storedFallback.model.trim();

  const providerModels = useMemo(
    () => models.filter((m) => m.provider === draft.provider),
    [models, draft.provider]
  );
  const discovery = useMemo(
    () => providerStatus.find((s) => s.provider === draft.provider),
    [providerStatus, draft.provider]
  );
  const fallbackModels = useMemo(
    () => models.filter((m) => m.provider === draftFallback.provider),
    [models, draftFallback.provider]
  );
  const fallbackDiscovery = useMemo(
    () => providerStatus.find((s) => s.provider === draftFallback.provider),
    [providerStatus, draftFallback.provider]
  );
  const staleFallback = useMemo(
    () => staleTarget(draftFallback.provider, draftFallback.model, models, providerStatus),
    [draftFallback, models, providerStatus]
  );

  // The same rule the role cards use, so the conductor cannot be the one place
  // a retired model goes unmentioned. A provider that failed to answer says
  // nothing, and `staleTarget` is what keeps that distinction.
  const stale = useMemo(
    () => staleTarget(draft.provider, draft.model, models, providerStatus),
    [draft, models, providerStatus]
  );

  if (stored === null) {
    return loadError ? (
      <div className="flex items-start gap-2 text-xs text-codify-warning-ink bg-codify-warning/20 border border-codify-warning/60 rounded-lg p-3">
        <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
        <span className="leading-relaxed">Conductor settings unavailable — {loadError}</span>
      </div>
    ) : null;
  }

  const saveChoice = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const res = await saveEngineSettings({
        conductor_provider: draft.provider.trim(),
        conductor_model: draft.model.trim(),
      });
      const next = {
        provider: String(res.saved.conductor_provider ?? ""),
        model: String(res.saved.conductor_model ?? ""),
      };
      // Read back the echo rather than the draft: a provider the engine
      // normalised would otherwise look unsaved for ever.
      setStored(next);
      setDraft(next);
      setMsg({ ok: true, text: "Saved — the next turn uses it, no restart needed." });
    } catch (err: any) {
      setMsg({ ok: false, text: readRejection(err, "Could not save the conductor model.") });
    } finally {
      setSaving(false);
    }
  };

  const clearChoice = async () => {
    setSaving(true);
    setMsg(null);
    try {
      await saveEngineSettings({ conductor_provider: "", conductor_model: "" });
      const next = { provider: "", model: "" };
      setStored(next);
      setDraft(next);
      setMsg({
        ok: true,
        text: "Cleared — the conductor is back on the scribe's model, and so is the plain chat turn.",
      });
    } catch (err: any) {
      setMsg({ ok: false, text: readRejection(err, "Could not clear the conductor model.") });
    } finally {
      setSaving(false);
    }
  };

  const saveFallback = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const res = await saveEngineSettings({
        conductor_fallback_provider: draftFallback.provider.trim(),
        conductor_fallback_model: draftFallback.model.trim(),
      });
      const next = {
        provider: String(res.saved.conductor_fallback_provider ?? ""),
        model: String(res.saved.conductor_fallback_model ?? ""),
      };
      setStoredFallback(next);
      setDraftFallback(next);
      setMsg({
        ok: true,
        text: "Saved — a provider that cannot serve a call now moves the turn instead of ending it.",
      });
    } catch (err: any) {
      setMsg({ ok: false, text: readRejection(err, "Could not save the conductor fallback.") });
    } finally {
      setSaving(false);
    }
  };

  const clearFallback = async () => {
    setSaving(true);
    setMsg(null);
    try {
      await saveEngineSettings({ conductor_fallback_provider: "", conductor_fallback_model: "" });
      const next = { provider: "", model: "" };
      setStoredFallback(next);
      setDraftFallback(next);
      setMsg({ ok: true, text: "Fallback cleared — a dead model will end the turn." });
    } catch (err: any) {
      setMsg({ ok: false, text: readRejection(err, "Could not clear the conductor fallback.") });
    } finally {
      setSaving(false);
    }
  };

  const saveDrives = async (next: boolean) => {
    setDrivesSaving(true);
    try {
      const res = await saveEngineSettings({ conductor_drives_execution: next });
      setDrives(Number(res.saved.conductor_drives_execution));
    } catch (err: any) {
      setMsg({ ok: false, text: readRejection(err, "Could not change who drives execution.") });
    } finally {
      setDrivesSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
      <div className="flex items-center gap-2 text-xs font-semibold text-codify-secondary">
        <Brain className="w-3.5 h-3.5 text-codify-design" />
        Conductor
      </div>
      <p className="text-xs text-codify-muted leading-relaxed">
        The one model that decides which sub-agent runs and in what order. It is a loop rather than a
        ninth role, so it is configured here instead of on a role card; the sub-agents keep the
        models chosen on the cards below, and nothing on this card changes those.
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <ProviderSelect
          value={draft.provider}
          onChange={(p) => {
            setDraft((d) => ({ ...d, provider: p }));
            setMsg(null);
          }}
          builtins={builtins}
        />
        <ModelSelect
          provider={draft.provider}
          value={draft.model}
          onChange={(m) => {
            setDraft((d) => ({ ...d, model: m }));
            setMsg(null);
          }}
          options={providerModels}
          discovery={discovery}
          onRefresh={onRefreshModels}
          refreshing={refreshingModels}
        />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={saveChoice}
          disabled={saving || !verdict.ok || !dirty}
          title={verdict.ok ? undefined : verdict.text}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-codify-accent text-codify-bg hover:brightness-110 text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
        >
          <Save className="w-3.5 h-3.5" />
          {saving ? "Saving..." : "Save"}
        </button>
        {canClear && (
          <button
            type="button"
            onClick={clearChoice}
            disabled={saving}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-codify-surface hover:bg-codify-border border border-codify-border text-codify-secondary text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
          >
            <RotateCcw className="w-3.5 h-3.5" />
            Use the scribe&apos;s model
          </button>
        )}
        {msg && (
          <span className={`text-xs ${msg.ok ? "text-codify-success" : "text-codify-danger"}`}>{msg.text}</span>
        )}
      </div>

      <p
        className={`text-xs leading-relaxed ${
          status.kind === "incomplete" ? "text-codify-warning" : "text-codify-muted"
        }`}
      >
        {status.text}
      </p>
      {stale && (
        <div className="flex items-start gap-2 text-xs text-codify-warning-ink bg-codify-warning/20 border border-codify-warning/60 rounded-lg p-3">
          <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
          <span className="leading-relaxed">
            {stale.provider} no longer reports <span className="font-mono">{stale.model}</span> (
            {stale.reportedCount} model{stale.reportedCount === 1 ? "" : "s"} in the last discovery).
            Saving it will not make the conductor run.
          </span>
        </div>
      )}

      <div className="flex flex-col gap-2.5 border-t border-codify-border pt-2.5">
        <div className="flex items-center gap-2 text-xs font-semibold text-codify-secondary">
          <ShieldCheck className="w-3.5 h-3.5 text-codify-warning" />
          Fallback
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <ProviderSelect
            value={draftFallback.provider}
            onChange={(p) => {
              setDraftFallback((d) => ({ ...d, provider: p }));
              setMsg(null);
            }}
            builtins={builtins}
          />
          <ModelSelect
            provider={draftFallback.provider}
            value={draftFallback.model}
            onChange={(m) => {
              setDraftFallback((d) => ({ ...d, model: m }));
              setMsg(null);
            }}
            options={fallbackModels}
            discovery={fallbackDiscovery}
            onRefresh={onRefreshModels}
            refreshing={refreshingModels}
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={saveFallback}
            disabled={saving || !fallbackVerdict.ok || !dirtyFallback}
            title={fallbackVerdict.ok ? undefined : fallbackVerdict.text}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-codify-surface hover:bg-codify-border border border-codify-border text-codify-secondary text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
          >
            <Save className="w-3.5 h-3.5" />
            {saving ? "Saving..." : "Save fallback"}
          </button>
          {canClearFallback && (
            <button
              type="button"
              onClick={clearFallback}
              disabled={saving}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-codify-surface hover:bg-codify-border border border-codify-border text-codify-secondary text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              No fallback
            </button>
          )}
        </div>
        <p
          className={`text-xs leading-relaxed ${
            fallbackStatus.source === "none" ? "text-codify-warning" : "text-codify-muted"
          }`}
        >
          {fallbackStatus.text}
        </p>
        {staleFallback && (
          <div className="flex items-start gap-2 text-xs text-codify-warning-ink bg-codify-warning/20 border border-codify-warning/60 rounded-lg p-3">
            <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
            <span className="leading-relaxed">
              {staleFallback.provider} no longer reports{" "}
              <span className="font-mono">{staleFallback.model}</span>, so saving it would store a
              fallback that cannot answer either.
            </span>
          </div>
        )}
      </div>

      {turns && moves && (
        <div className="flex flex-col gap-3 border-t border-codify-border pt-2.5">
          <NumberSetting
            id="conductor-max-turns"
            label="Tool-calling turns per run"
            hint="How many times one conductor run may call the model before the engine stops it. A turn answers with what it has; each step of an approved plan is a run of its own, and one that runs out pauses the goal so you can press Start to carry on."
            value={turns.value}
            min={turns.min}
            max={turns.max}
            onSave={async (value) => {
              const res = await saveEngineSettings({ conductor_max_turns: value });
              const saved = Number(res.saved.conductor_max_turns);
              setTurns({ value: saved, min: turns.min, max: turns.max });
              return saved;
            }}
          />
          <NumberSetting
            id="conductor-max-moves"
            label="Stage moves per run"
            hint="How many sub-agent runs one turn may drive (recon, design, plan, write, verify, review, summarize). Counted apart from the turns above, because eight cheap file reads should not starve the change you asked for."
            value={moves.value}
            min={moves.min}
            max={moves.max}
            onSave={async (value) => {
              const res = await saveEngineSettings({ conductor_max_moves: value });
              const saved = Number(res.saved.conductor_max_moves);
              setMoves({ value: saved, min: moves.min, max: moves.max });
              return saved;
            }}
          />
          <div className="flex flex-col gap-1.5">
            <label className="flex items-center gap-2 text-xs text-codify-secondary font-medium">
              <input
                type="checkbox"
                checked={drives === 1}
                disabled={drives === null || drivesSaving}
                onChange={(e) => saveDrives(e.target.checked)}
                className="accent-codify-accent"
              />
              The conductor drives an approved plan
            </label>
            <p className="text-xs text-codify-muted leading-relaxed">
              Off, the engine walks the steps in its own fixed order and the conductor only plans.
              The escape hatch for a model not yet trusted with the order, and it needs no rebuild.
            </p>
          </div>
        </div>
      )}
    </div>
  );
};
