/**
 * One provider, configured in one place: the credential, and the model.
 *
 * This row is the reason the provider tab exists. Before it, the screen had a
 * key field per provider and — one tab over — a model picker per *role*, with
 * the two never on screen at the same time. So the order of operations was
 * wrong: you were asked to trust a provider before you could see what it served,
 * and then asked to choose a model after you had already left the provider's
 * row. Putting the key and the model side by side is not a layout preference.
 * It is the two decisions in the order a person actually makes them.
 *
 * **Nothing is written by picking a model.** `PUT /settings/agents/{role}` is
 * the only route that may change agent config (docs/00 §6.2), so a provider row
 * cannot set a provider-wide default and call it done — it stages a choice and
 * offers a button that writes through that endpoint, once per role. The button
 * names the number of roles it will touch, because a bulk edit whose blast
 * radius is hidden is the kind that repoints eight roles on a stray click.
 *
 * The key is separate and immediate: `PUT /settings/keys` is its own route,
 * there is no "apply" for a credential, and saving one re-runs discovery so the
 * picker beside it fills in without a second visit.
 */

import React, { useState } from "react";
import {
  Check,
  Cpu,
  Eye,
  EyeOff,
  HardDrive,
  Key,
  Loader2,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import type { AgentRole, ModelOption, ProviderKeyStatus, ProviderModelStatus } from "../types";
import { ModelPicker } from "./ModelPicker";
import {
  applyHint,
  applyLabel,
  changedRoles,
  planApply,
  planSummary,
  rolesOnProvider,
} from "../providerSetup";
import { newCountLabel, newCountTitle } from "../modelFreshness";

export interface ProviderRowProps {
  keyStatus: ProviderKeyStatus;
  /** Live discovery for this provider: did it answer, and with what. */
  status?: ProviderModelStatus;
  /** This provider's slice of the discovered catalogue. */
  models: ModelOption[];
  /**
   * Ids this provider did not serve the last time the panel was opened.
   *
   * Counted in the header, not only badged in the menu: a release that is only
   * visible after opening a dropdown is a release nobody opens a dropdown for.
   */
  newIds?: readonly string[];
  /** Every role's current provider, for the apply step's blast radius. */
  roleProviders: { role: AgentRole; provider: string; model_name?: string }[];
  onSaveKey: (provider: string, apiKey: string) => Promise<void>;
  onApplyModel: (provider: string, model: string, roles: AgentRole[]) => Promise<void>;
  onRefresh?: (provider: string) => void;
  refreshing?: boolean;
  savingKey?: boolean;
  keySaved?: boolean;
}

export const ProviderRow: React.FC<ProviderRowProps> = ({
  keyStatus,
  status,
  models,
  newIds = [],
  roleProviders,
  onSaveKey,
  onApplyModel,
  onRefresh,
  refreshing = false,
  savingKey = false,
  keySaved = false,
}) => {
  const [key, setKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [staged, setStaged] = useState("");
  const [applying, setApplying] = useState(false);
  const [applied, setApplied] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { provider, needs_key: needsKey, has_key: hasKey } = keyStatus;
  /**
   * Counted against what the reader has actually been shown, not against the
   * whole list: a provider's 200 models are not 200 new models, and a badge that
   * says so is a badge nobody believes.
   */
  const newHere = newIds.filter((id) => models.some((m) => m.id === id)).length;
  const plan = planApply(roleProviders, provider, staged);
  const toChange = changedRoles(plan);
  const onThisProvider = rolesOnProvider(roleProviders, provider);
  /**
   * True only for the model just written, and it is cleared the moment a
   * different one is staged — otherwise the row keeps claiming a success for a
   * choice that was never applied.
   */
  const justApplied = applied !== null && applied === staged.trim() && staged.trim() !== "";

  const save = async () => {
    const trimmed = key.trim();
    if (!trimmed) return;
    setError(null);
    try {
      await onSaveKey(provider, trimmed);
      setKey("");
    } catch (err: any) {
      setError(err?.message || `Could not save the ${provider} key`);
    }
  };

  const apply = async () => {
    if (!toChange.length) return;
    setError(null);
    setApplying(true);
    try {
      await onApplyModel(
        provider,
        staged.trim(),
        toChange.map((s) => s.role),
      );
      setApplied(staged.trim());
    } catch (err: any) {
      setError(err?.message || `Could not apply ${staged} to ${provider}`);
    } finally {
      setApplying(false);
    }
  };

  const badge = (() => {
    if (hasKey) {
      return (
        <span className="flex items-center gap-1 text-2xs font-medium text-codify-success bg-codify-success/20 border border-codify-success/60 px-2 py-0.5 rounded-full flex-shrink-0">
          <ShieldCheck className="w-3 h-3" /> Configured
        </span>
      );
    }
    if (needsKey) {
      return (
        <span className="text-2xs text-codify-muted bg-codify-surface px-2 py-0.5 rounded-full border border-codify-border flex-shrink-0">
          Missing key
        </span>
      );
    }
    return (
      <span className="text-2xs text-codify-info bg-codify-info/20 border border-codify-info/60 px-2 py-0.5 rounded-full flex-shrink-0">
        Local / no key
      </span>
    );
  })();

  return (
    <div className="bg-codify-bg border border-codify-border rounded-xl p-3.5 space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Cpu className="w-4 h-4 text-codify-design flex-shrink-0" />
          <span className="font-semibold text-sm text-codify-primary capitalize truncate">{provider}</span>
          <span className="text-2xs text-codify-muted font-mono truncate">{keyStatus.protocol}</span>
          {status?.ok && (
            <span className="text-2xs text-codify-muted flex-shrink-0">{status.count} models</span>
          )}
          {newHere > 0 && (
            <span
              title={newCountTitle(provider, newHere)}
              className="text-2xs font-medium text-codify-design bg-codify-design/20 border border-codify-design/60 px-1.5 py-px rounded-full flex-shrink-0"
            >
              {newCountLabel(newHere)}
            </span>
          )}
          {status?.error && (
            <span className="text-2xs text-codify-warning/90 truncate" title={status.error}>
              — {status.error}
            </span>
          )}
        </div>
        {badge}
      </div>

      {/* The two decisions, side by side and in the order they are made. */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 items-start">
        <div className="flex flex-col gap-1.5 min-w-0">
          <label className="text-2xs font-semibold text-codify-muted uppercase tracking-wider flex items-center gap-1.5">
            <Key className="w-3 h-3" /> API key
          </label>
          {needsKey ? (
            <div className="flex items-stretch">
              <div className="relative flex-1 min-w-0">
                <input
                  type={showKey ? "text" : "password"}
                  placeholder={
                    hasKey ? "•••••••• stored — leave blank to keep" : `Paste your ${provider} key`
                  }
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") save();
                  }}
                  autoComplete="off"
                  spellCheck={false}
                  className="w-full bg-codify-surface border border-codify-border rounded-l-lg px-3 py-1.5 pr-8 text-xs text-codify-secondary focus:outline-none focus:border-codify-accent font-mono"
                />
                <button
                  type="button"
                  onClick={() => setShowKey(!showKey)}
                  aria-label={showKey ? "Hide the key" : "Show the key"}
                  className="absolute right-2 top-1.5 text-codify-muted hover:text-codify-secondary"
                >
                  {showKey ? <EyeOff size={13} /> : <Eye size={13} />}
                </button>
              </div>
              <button
                type="button"
                onClick={save}
                disabled={savingKey || !key.trim()}
                className={
                  "flex items-center gap-1 px-3 rounded-r-lg text-xs font-semibold transition-colors flex-shrink-0 border border-l-0 border-codify-border " +
                  (keySaved
                    ? "bg-codify-success/80 text-codify-bg"
                    : "bg-codify-accent text-codify-bg hover:brightness-110 disabled:opacity-40")
                }
              >
                {savingKey ? (
                  <Loader2 className="w-3 h-3 animate-spin" />
                ) : keySaved ? (
                  <Check className="w-3 h-3" />
                ) : null}
                {savingKey ? "" : keySaved ? "Saved" : "Save"}
              </button>
            </div>
          ) : (
            <p className="text-2xs text-codify-muted flex items-center gap-1.5">
              <HardDrive className="w-3 h-3 flex-shrink-0" />
              Runs on your own machine at <span className="font-mono">{keyStatus.base_url}</span> — no
              key to paste.
            </p>
          )}
        </div>

        <div className="flex flex-col gap-1.5 min-w-0">
          <label className="text-2xs font-semibold text-codify-muted uppercase tracking-wider">
            Model
          </label>
          <ModelPicker
            newIds={newIds}
            provider={provider}
            value={staged}
            onChange={setStaged}
            options={models}
            discoveryError={status?.error ?? null}
            onRefresh={onRefresh ? () => onRefresh(provider) : undefined}
            refreshing={refreshing}
            placeholder={models.length ? "Search or scroll the list" : "No models discovered yet"}
          />
        </div>
      </div>

      {/* The apply step, and its blast radius. */}
      <div className="flex items-center gap-2 flex-wrap border-t border-codify-border/60 pt-2.5">
        <button
          type="button"
          onClick={apply}
          disabled={applying || toChange.length === 0}
          title={applyHint({
            staged,
            changes: toChange.length,
            onProvider: onThisProvider.length,
          })}
          className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-2xs font-semibold bg-codify-raised hover:bg-codify-border text-codify-secondary disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {applying && <Loader2 className="w-3 h-3 animate-spin" />}
          {justApplied && <Check className="w-3 h-3 text-codify-success" />}
          {applyLabel({
            staged,
            changes: toChange.length,
            onProvider: onThisProvider.length,
            applied: justApplied,
          })}
        </button>

        {staged.trim() && toChange.length > 0 && (
          <span className="text-2xs text-codify-muted truncate" title={planSummary(plan)}>
            {planSummary(plan)}
          </span>
        )}
      </div>

      {error && (
        <p className="flex items-start gap-1.5 text-2xs text-codify-danger">
          <TriangleAlert className="w-3 h-3 flex-shrink-0 mt-0.5" />
          {error}
        </p>
      )}
    </div>
  );
};
