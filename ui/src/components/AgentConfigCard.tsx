import React, { useState, useEffect, useRef } from "react";
import {
  AgentCallStat,
  AgentConfig,
  AgentRole,
  ModelOption,
  ProviderModelStatus,
  ProviderProtocol,
  RoleInfo,
} from "../types";
import type { AgentConfigStore } from "../hooks/useAgentConfigs";
import { EMPTY_SIGNALS, ModelSignals } from "../modelSignals";
import type { StaleModel } from "../staleModel";
import { ProviderSelect } from "./ProviderSelect";
import { ProtocolSelect } from "./ProtocolSelect";
import { ModelSelect } from "./ModelSelect";
import { ApiKeyField } from "./ApiKeyField";
import { BaseUrlField } from "./BaseUrlField";
import { PromptOverrideEditor } from "./PromptOverrideEditor";
import { TestConnectionButton } from "./TestConnectionButton";
import {
  Save,
  Check,
  BookOpen,
  Hammer,
  Palette,
  PenLine,
  Sparkles,
  PlayCircle,
  ShieldCheck,
  Zap,
  AlertTriangle,
  Route,
} from "lucide-react";

interface AgentConfigCardProps {
  role: AgentRole;
  /**
   * Config store owned by SettingsPanel — one fetch for every role card, and a
   * save in any card refreshes what the others (and the summary) see.
   */
  store: AgentConfigStore;
  /** Discovered models; the ones for this card's provider autocomplete the field. */
  models?: ModelOption[];
  /** Raw per-provider discovery outcomes, so the model field can state the reason. */
  providerStatus?: ProviderModelStatus[];
  /**
   * The provider slugs the engine ships (`GET /settings/providers`).
   *
   * This decides what counts as a *custom* provider, and that is not cosmetic:
   * a custom provider gets a protocol picker and a base-URL box, because the
   * engine cannot know how to reach it. Offering those to a provider the engine
   * ships a fixed protocol for invites the user to break a working setup. The
   * list used to be a hardcoded copy of five slugs that quietly fell behind the
   * engine's seven.
   */
  builtinProviders: string[];
  /**
   * Set by SettingsPanel when this role's SAVED model is missing from its
   * provider's live catalog. Computed there so the per-card warning and the
   * panel summary are the same verdict, from the same rule.
   */
  stale?: StaleModel | null;
  /** The same verdict for the role's fallback target, which rots just as quietly. */
  staleFallback?: StaleModel | null;
  /**
   * Which roles run each discovered model, and which models recently answered.
   * The same signals the chat's menu orders by, so an id sits in the same place
   * and carries the same badges in both pickers.
   */
  signals?: ModelSignals;
  /** What the engine says this slot is for, and when it runs. */
  info?: RoleInfo;
  /** The newest outcome for this role's model calls — how long the last one
   * took and what the last failure was. Null when the role has never run or the
   * engine predates the stats endpoint. */
  callStat?: AgentCallStat | null;
  /** Ask every provider what it serves right now — offered inside the field. */
  onRefreshModels?: () => void;
  refreshingModels?: boolean;
}

/** Call duration in the same vocabulary the chat's usage card uses: ms below a
 * second, then seconds. Null (a call whose duration was never recorded) reads as
 * "duration unknown" rather than a fake "0ms". */
function formatCallDuration(durationMs: number | null): string {
  if (durationMs == null) return "(duration unknown)";
  if (durationMs < 1000) return `${Math.round(durationMs)}ms`;
  return `${(durationMs / 1000).toFixed(1)}s`;
}

// One icon per ability: the librarian reads, the design agent locks a direction,
// the fixer writes, the verifier runs, the critic judges, the scribe records.
//
// These eight hues are **not** theme tokens, deliberately, and they are the one
// thing in the settings panel that does not follow the active theme. Mapping them
// onto the status tones was tried and is wrong twice over: there are eight roles
// and six tones, so it silently gives two pairs the same colour — the collision
// `tailwind.config.js` records having already cost one pair of features its
// identity — and it would claim a role *is* a status, which is what the tones
// mean. A role's colour says which role this is; a tone says what is happening to
// it. Giving the contract eight role tokens is the fix, and it belongs to whoever
// wants role identity to be part of the theme rather than a fact about roles.
//
// It has now been swept into tokens *twice*, by two different passes over the
// tree, and reverted twice. That is the argument for what a comment cannot do:
// `ui/tests/hardcodedPalette.test.ts` names these eight classes as exceptions and
// fails if any of them stops appearing, so the third sweep breaks a test instead
// of quietly making four roles two colours.
const ROLE_ICONS: Record<AgentRole, React.ReactNode> = {
  laya: <Zap className="w-5 h-5 text-emerald-400" />,
  librarian: <BookOpen className="w-5 h-5 text-cyan-400" />,
  design: <Palette className="w-5 h-5 text-pink-400" />,
  planner: <Sparkles className="w-5 h-5 text-purple-400" />,
  fixer: <Hammer className="w-5 h-5 text-blue-400" />,
  verifier: <PlayCircle className="w-5 h-5 text-green-400" />,
  critic: <ShieldCheck className="w-5 h-5 text-amber-400" />,
  scribe: <PenLine className="w-5 h-5 text-indigo-400" />,
};

/** Roles whose work happens before planning, so the card can label them. */
const GOAL_LEVEL: AgentRole[] = ["laya", "librarian", "design", "planner"];

export const AgentConfigCard: React.FC<AgentConfigCardProps> = ({
  onRefreshModels,
  refreshingModels = false,
  role,
  store,
  models = [],
  providerStatus = [],
  builtinProviders,
  stale = null,
  staleFallback = null,
  info,
  signals = EMPTY_SIGNALS,
  callStat = null,
}) => {
  const { configs, update, testConnection } = store;
  const config = configs.find((c) => c.role === role);

  const [draft, setDraft] = useState<AgentConfig | undefined>(config);
  const [dirty, setDirty] = useState(false);
  const [pendingApiKey, setPendingApiKey] = useState<string | undefined>();
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  // The timer that takes the "Saved" tick back down. Held so a second save
  // restarts the window instead of racing the first one, and so closing the
  // card cancels it rather than leaving it to set state on nothing.
  const savedTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(savedTimer.current), []);
  const [saveError, setSaveError] = useState<string | null>(null);
  // Set when the server config moved underneath unsaved edits (e.g. the
  // panel's "Fix roles that can't run" refreshed the store). The draft keeps
  // the user's edits — but silently keeping them would hide the repair, so
  // the card says so and offers the server values in one click.
  const [externallyUpdated, setExternallyUpdated] = useState(false);
  // The fallback section is collapsed until it is in use, so it never grows the
  // card for the roles that do not have one.
  const [fallbackOpen, setFallbackOpen] = useState(false);

  // Sync draft when the server config arrives, but never clobber fields the
  // user has already edited locally (the hook refetches after every save).
  const lastServerJson = React.useRef<string | null>(null);
  useEffect(() => {
    if (!config) return;
    const serverJson = JSON.stringify(config);
    if (lastServerJson.current === null) {
      lastServerJson.current = serverJson;
      if (!dirty) setDraft(config);
      return;
    }
    if (serverJson === lastServerJson.current) return;
    lastServerJson.current = serverJson;
    if (dirty) {
      // The server moved while edits were unsaved (e.g. a panel-level repair
      // refreshed the store) — keep the user's edits, but flag it instead of
      // silently discarding either side.
      setExternallyUpdated(true);
    } else {
      setDraft(config);
      setExternallyUpdated(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config]);

  const updateDraft = (patch: Partial<AgentConfig>) => {
    setDirty(true);
    setDraft((prev) => ({ ...(prev ?? config!), ...patch }));
  };

  if (!draft && !config) {
    return (
      <div className="bg-codify-surface border border-codify-border rounded-lg p-6 animate-pulse">
        <div className="h-6 w-32 bg-codify-border rounded mb-4" />
        <div className="h-24 bg-codify-raised rounded" />
      </div>
    );
  }

  const active = draft || config!;

  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const saved = await update(role, {
        display_name: active.display_name,
        provider: active.provider,
        protocol: active.protocol,
        model_name: active.model_name,
        api_key: pendingApiKey || undefined,
        base_url: active.base_url || undefined,
        temperature: active.temperature,
        max_tokens: active.max_tokens,
        // null is meaningful here: it returns the role to Ollama's default
        // window, so an emptied field must arrive as null rather than as
        // "no change".
        num_ctx: active.num_ctx ?? null,
        // Same reasoning: null returns the role to Ollama's own window rather
        // than leaving a stale value behind, so an emptied field must arrive
        // as null and not as "no change".
        keep_alive: active.keep_alive?.trim() || null,
        system_prompt_override: active.system_prompt_override?.trim() || null,
        // An empty string is how the engine is told "no fallback": the desktop
        // shell passes this patch through a typed struct, where an explicit null
        // and an absent field arrive identically.
        fallback_provider: (active.fallback_provider || "").trim(),
        fallback_model_name: (active.fallback_model_name || "").trim(),
        fallback_protocol: active.fallback_protocol || undefined,
        fallback_base_url: active.fallback_base_url || undefined,
      });
      // Reset draft to server-normalised value (BUG-10 fix)
      setDraft(saved);
      setDirty(false);
      setExternallyUpdated(false);
      setPendingApiKey(undefined);
      setSaved(true);
      clearTimeout(savedTimer.current);
      savedTimer.current = setTimeout(() => setSaved(false), 2500);
    } catch (err: any) {
      setSaveError(err.message || "Failed to save configuration");
    } finally {
      setSaving(false);
    }
  };

  // Only this provider's discovered models — offering another provider's ids
  // is how a role ends up configured with a model its endpoint cannot serve.
  const providerModels = models.filter((m) => m.provider === active.provider);
  const discovery = providerStatus.find((s) => s.provider === active.provider);
  const isCustomProvider = !builtinProviders.includes(active.provider);
  const isOllama = active.provider === "ollama";
  const needsKey = active.protocol !== "ollama";

  // ── Fallback target ──────────────────────────────────────────────────────
  const fallbackProvider = (active.fallback_provider || "").trim();
  const hasFallback = Boolean(fallbackProvider && (active.fallback_model_name || "").trim());
  const showFallback = Boolean(fallbackProvider) || fallbackOpen;
  const fallbackCustom = Boolean(fallbackProvider) && !builtinProviders.includes(fallbackProvider);
  const fallbackModels = models.filter((m) => m.provider === fallbackProvider);
  const fallbackDiscovery = providerStatus.find((s) => s.provider === fallbackProvider);

  return (
    <div
      className={`bg-codify-surface border rounded-lg p-5 flex flex-col gap-4 shadow-sm transition-colors ${
        stale ? "border-codify-warning/70" : "border-codify-border hover:border-codify-border-strong"
      }`}
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-codify-border pb-3">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-codify-raised rounded border border-codify-border">
            {ROLE_ICONS[role]}
          </div>
          <div>
            <h3 className="font-semibold text-base text-codify-primary flex items-center gap-2">
              {active.display_name}
              <span className="text-xs px-2 py-0.5 bg-codify-raised text-codify-muted rounded-full font-mono font-normal">
                {role}
              </span>
            </h3>
            <p className="text-xs text-codify-muted">
              Assigned model: <span className="font-mono text-codify-secondary">{active.provider}/{active.model_name}</span>
              {hasFallback && (
                <>
                  <span className="text-codify-muted"> · falls back to </span>
                  <span className="font-mono text-codify-muted">
                    {fallbackProvider}/{active.fallback_model_name}
                  </span>
                </>
              )}
            </p>
            {info && (
              <p className="text-xs text-codify-muted mt-0.5 max-w-2xl leading-relaxed">
                <span className="text-codify-muted">{info.job}</span>{" "}
                <span className="whitespace-nowrap text-codify-muted">
                  · runs {info.timing}
                  {GOAL_LEVEL.includes(role) && role !== "laya" ? " (not per step)" : ""}
                </span>
              </p>
            )}
            {callStat && (callStat.last_call || callStat.last_error || (callStat.runs ?? 0) > 0) && (
              <p className="text-xs text-codify-muted mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                {(callStat.runs ?? 0) > 0 && (
                  <span
                    className="whitespace-nowrap text-codify-muted"
                    title={
                      Object.entries(callStat.outcomes ?? {})
                        .map(([name, n]) => `${n} ${name.replace(/_/g, " ")}`)
                        .join(" · ") || undefined
                    }
                  >
                    · did its job{" "}
                    <span
                      className={`font-mono ${
                        (callStat.success_rate ?? 0) >= 90
                          ? "text-codify-success"
                          : (callStat.success_rate ?? 0) >= 60
                            ? "text-codify-warning"
                            : "text-codify-danger"
                      }`}
                    >
                      {callStat.success_rate == null ? "—" : `${callStat.success_rate}%`}
                    </span>{" "}
                    <span className="text-codify-muted">
                      over {callStat.runs} run{callStat.runs === 1 ? "" : "s"}
                    </span>
                  </span>
                )}
                {callStat.last_call && (
                  <span className="whitespace-nowrap text-codify-muted">
                    · last call {formatCallDuration(callStat.last_call.duration_ms)}
                    {callStat.last_call.provider && (
                      <span className="font-mono text-codify-muted">
                        {" "}({callStat.last_call.provider}/{callStat.last_call.model})
                      </span>
                    )}
                  </span>
                )}
                {callStat.last_error && (
                  <span
                    className="whitespace-nowrap text-codify-warning"
                    title={callStat.last_error.message ?? undefined}
                  >
                    · last error <span className="font-mono">{callStat.last_error.code}</span>
                  </span>
                )}
              </p>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2">
          {saveError && <span className="text-xs text-codify-danger font-medium">{saveError}</span>}
          <button
            onClick={handleSave}
            disabled={saving}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded transition-colors ${
              saved
                ? "bg-codify-success text-codify-bg"
                : "bg-codify-accent text-codify-bg hover:brightness-110 disabled:opacity-50"
            }`}
          >
            {saved ? <Check className="w-3.5 h-3.5" /> : <Save className="w-3.5 h-3.5" />}
            {saved ? "Saved" : saving ? "Saving..." : "Save"}
          </button>
        </div>
      </div>

      {/* Saved model missing from the provider's catalog.
         
          This is the failure the stored config cannot show by itself: the role
          is written to the database and only dies later, mid-goal, when a call
          comes back 404 for a model the provider retired or renamed. Verdict
          computed by SettingsPanel, so it matches the panel's summary. */}
      {stale && (
        <div className="flex items-start gap-2 text-xs text-codify-warning-ink bg-codify-warning/20 border border-codify-warning/60 rounded-lg px-3 py-2">
          <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
          <span className="leading-relaxed">
            {stale.reportedCount > 0 ? (
              <>
                Saved model <span className="font-mono font-semibold">{stale.model}</span> is not
                among the {stale.reportedCount} model
                {stale.reportedCount === 1 ? "" : "s"}{" "}
                <span className="font-semibold">{stale.provider}</span> reports right now — it may
                have been retired or renamed. Pick a current one below, or keep it if this is a
                private alias.
              </>
            ) : (
              <>
                <span className="font-semibold">{stale.provider}</span> reported no models at all,
                so the saved model <span className="font-mono font-semibold">{stale.model}</span>{" "}
                could not be verified. Check this provider's endpoint and key.
              </>
            )}
          </span>
        </div>
      )}

      {/* The server config changed underneath unsaved edits (e.g. the panel's
          repair refreshed the store). The draft keeps the user's edits — this
          names that fact and offers the server values in one click. */}
      {externallyUpdated && dirty && (
        <div className="flex items-start gap-2 text-xs text-codify-info-ink bg-codify-info/15 border border-codify-info/60 rounded-lg px-3 py-2">
          <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
          <span className="leading-relaxed flex-1">
            The saved config changed underneath your unsaved edits (a repair or another save).
            Your edits are kept — Save to overwrite, or load the server values.
          </span>
          <button
            type="button"
            onClick={() => {
              if (config) {
                setDraft(config);
                lastServerJson.current = JSON.stringify(config);
              }
              setDirty(false);
              setExternallyUpdated(false);
            }}
            className="flex-shrink-0 text-xs px-2 py-0.5 rounded border border-codify-info/60 hover:bg-codify-info/20 transition-colors"
          >
            Load server values
          </button>
        </div>
      )}

      {/* Grid Form Fields */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <ProviderSelect
          value={active.provider}
          onChange={(p) => updateDraft({ provider: p })}
          builtins={builtinProviders}
        />

        {isCustomProvider ? (
          <ProtocolSelect
            value={active.protocol}
            onChange={(proto: ProviderProtocol) => updateDraft({ protocol: proto })}
          />
        ) : (
          <ModelSelect
            provider={active.provider}
            value={active.model_name}
            onChange={(m) => updateDraft({ model_name: m })}
            options={providerModels}
            discovery={discovery}
            signals={signals}
            onRefresh={onRefreshModels}
            refreshing={refreshingModels}
          />
        )}
      </div>

      {isCustomProvider && (
        <ModelSelect
          provider={active.provider}
          value={active.model_name}
          onChange={(m) => updateDraft({ model_name: m })}
          options={providerModels}
          discovery={discovery}
          signals={signals}
          onRefresh={onRefreshModels}
          refreshing={refreshingModels}
        />
      )}

      {/* API Key & Base URL */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {needsKey && (
          <ApiKeyField
            hasExistingKey={Boolean(active.api_key_ref)}
            onChange={(k) => setPendingApiKey(k)}
          />
        )}

        {(isOllama || isCustomProvider) && (
          <BaseUrlField
            value={active.base_url || ""}
            localOnly={active.protocol === "ollama"}
            onChange={(u) => updateDraft({ base_url: u })}
          />
        )}
      </div>

      {/* Fallback target: the second place this role may be called.

          It is collapsed until it is used, and the engine's own rule decides what
          counts as one — both a provider and a model, or it is not a fallback. */}
      <div className="border border-codify-border rounded-lg p-3.5 flex flex-col gap-3 bg-codify-bg">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-xs font-semibold text-codify-secondary">
            <Route className="w-3.5 h-3.5 text-codify-knowledge" />
            Fallback target
            {hasFallback ? (
              <span className="font-mono font-normal text-codify-knowledge">
                {fallbackProvider}/{active.fallback_model_name}
              </span>
            ) : (
              <span className="font-normal text-codify-muted">none</span>
            )}
          </div>
          <button
            type="button"
            onClick={() => {
              if (showFallback) {
                // Clearing the provider clears the model and endpoint with it, so
                // a removed fallback cannot leave a stale target behind.
                updateDraft({
                  fallback_provider: "",
                  fallback_model_name: "",
                  fallback_protocol: null,
                  fallback_base_url: null,
                });
                setFallbackOpen(false);
              } else {
                // Ollama is the one provider that cannot fail for want of a key, so
                // it is the useful starting point — no model is invented, the field
                // still starts empty.
                updateDraft({ fallback_provider: "ollama" });
                setFallbackOpen(true);
              }
            }}
            className="text-xs px-2 py-1 rounded border border-codify-border text-codify-secondary hover:text-codify-primary hover:border-codify-border-strong transition-colors"
          >
            {showFallback ? "Remove" : "Add a fallback"}
          </button>
        </div>

        {showFallback ? (
          <>
            <p className="text-xs text-codify-muted leading-relaxed">
              Used when the primary target cannot be called at all: no credential stored, the
              endpoint unreachable, the model no longer served, or a reply the contract cannot
              parse. Tried once per call — never a retry loop. The role's temperature and max
              tokens apply to both targets.
            </p>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <ProviderSelect
                value={fallbackProvider}
                onChange={(p) => updateDraft({ fallback_provider: p })}
                builtins={builtinProviders}
              />
              <ModelSelect
                provider={fallbackProvider}
                value={active.fallback_model_name || ""}
                onChange={(m) => updateDraft({ fallback_model_name: m })}
                options={fallbackModels}
                discovery={fallbackDiscovery}
                signals={signals}
                onRefresh={onRefreshModels}
                refreshing={refreshingModels}
              />
            </div>
            {/* Endpoint for the same cases the primary target shows one: a custom
                slug always needs it, and a local providers' fallback can point at a
                different server than the primary's — worth seeing rather than
                inheriting by surprise. */}
            <div className={fallbackCustom ? "grid grid-cols-1 md:grid-cols-2 gap-4" : ""}>
              {fallbackCustom && (
                <ProtocolSelect
                  value={active.fallback_protocol || "openai_compat"}
                  onChange={(proto: ProviderProtocol) => updateDraft({ fallback_protocol: proto })}
                />
              )}
              {(fallbackCustom || active.fallback_protocol === "ollama") && (
                <BaseUrlField
                  value={active.fallback_base_url || ""}
                  localOnly={active.fallback_protocol === "ollama"}
                  onChange={(u) => updateDraft({ fallback_base_url: u })}
                />
              )}
            </div>
            {staleFallback && (
              <div className="flex items-start gap-2 text-xs text-codify-warning-ink bg-codify-warning/20 border border-codify-warning/60 rounded-lg px-3 py-2">
                <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                <span className="leading-relaxed">
                  {staleFallback.reportedCount > 0 ? (
                    <>
                      Fallback model <span className="font-mono font-semibold">{staleFallback.model}</span>{" "}
                      is not among the {staleFallback.reportedCount} model
                      {staleFallback.reportedCount === 1 ? "" : "s"}{" "}
                      <span className="font-semibold">{staleFallback.provider}</span> reports right now.
                      A fallback nobody verified is only discovered to be broken at the moment it
                      is needed — pick a current one.
                    </>
                  ) : (
                    <>
                      <span className="font-semibold">{staleFallback.provider}</span> reported no
                      models at all, so the fallback model{" "}
                      <span className="font-mono font-semibold">{staleFallback.model}</span> could not
                      be verified. Check this provider's endpoint and key.
                    </>
                  )}
                </span>
              </div>
            )}
            {!hasFallback && (
              <span className="text-xs text-codify-warning">
                Not in force yet: a fallback needs a model as well as a provider, and half of one
                fails exactly when it is needed.
              </span>
            )}
          </>
        ) : (
          <p className="text-xs text-codify-muted leading-relaxed">
            Without one, a failure on the primary target ends the goal. Adding a fallback lets a
            goal keep running when a key is missing or a provider is down.
          </p>
        )}
      </div>

      {/* Numeric Sliders / Steppers */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="flex flex-col gap-1.5">
          <div className="flex justify-between">
            <label
              htmlFor={`temperature-${role}`}
              className="text-xs font-semibold text-codify-muted uppercase tracking-wider"
            >
              Temperature
            </label>
            <span className="text-xs font-mono text-codify-secondary">{active.temperature}</span>
          </div>
          <input
            id={`temperature-${role}`}
            type="range"
            min={0}
            max={2}
            step={0.05}
            value={active.temperature}
            onChange={(e) => updateDraft({ temperature: parseFloat(e.target.value) })}
            className="w-full accent-codify-accent cursor-pointer"
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <div className="flex justify-between">
            <label className="text-xs font-semibold text-codify-muted uppercase tracking-wider">
              Max Tokens
            </label>
            <span className="text-xs font-mono text-codify-secondary">{active.max_tokens.toLocaleString()}</span>
          </div>
          <input
            type="number"
            min={128}
            max={200000}
            step={256}
            value={active.max_tokens}
            onChange={(e) => updateDraft({ max_tokens: parseInt(e.target.value, 10) || 4096 })}
            className="bg-codify-bg border border-codify-border rounded px-3 py-1.5 text-sm text-codify-secondary font-mono"
          />
        </div>
      </div>

      {/* Context window — Ollama only. OpenAI-style APIs size it server-side,
          so the field would be a lie on every other provider. */}
      {active.protocol === "ollama" && (
        <div className="flex flex-col gap-1.5">
          <div className="flex justify-between">
            <label
              htmlFor={`${role}-num-ctx`}
              className="text-xs font-semibold text-codify-muted uppercase tracking-wider"
            >
              Context Window
            </label>
            <span className="text-xs font-mono text-codify-secondary">
              {active.num_ctx ? active.num_ctx.toLocaleString() : "server default (4096)"}
            </span>
          </div>
          <input
            id={`${role}-num-ctx`}
            type="number"
            min={512}
            max={1000000}
            step={1024}
            placeholder="4096"
            value={active.num_ctx ?? ""}
            onChange={(e) => {
              const raw = e.target.value.trim();
              updateDraft({ num_ctx: raw === "" ? null : parseInt(raw, 10) || null });
            }}
            className="bg-codify-bg border border-codify-border rounded px-3 py-1.5 text-sm text-codify-secondary font-mono"
          />
          {/* The one warning worth its row: an empty field here is not "smaller
              and cheaper", it is "silently truncates whatever does not fit" —
              the failure mode that ships degraded plans with no error anywhere. */}
          <p className="text-xs leading-relaxed text-codify-muted">
            Raise this for roles that send large prompts (evidence packs, contracts).
            Ollama silently truncates to its default — 4096 — which degrades the
            reply without erroring. qwen2.5-coder supports 32768.
          </p>
        </div>
      )}

      {/* Keep-alive — Ollama only, and beside the window because the two are
          different questions about the same machine. This one is about the gap
          *between* calls, not within one: Ollama unloads a model five minutes
          after its last request, so a role you touch every ten minutes pays a
          full reload each time. The wording says so rather than implying it
          makes the current call faster, which it does not. */}
      {active.protocol === "ollama" && (
        <div className="flex flex-col gap-1.5">
          <div className="flex justify-between">
            <label
              htmlFor={`${role}-keep-alive`}
              className="text-xs font-semibold text-codify-muted uppercase tracking-wider"
            >
              Keep Alive
            </label>
            <span className="text-xs font-mono text-codify-secondary">
              {active.keep_alive?.trim() || "server default (5m)"}
            </span>
          </div>
          <input
            id={`${role}-keep-alive`}
            type="text"
            maxLength={32}
            placeholder="5m"
            value={active.keep_alive ?? ""}
            onChange={(e) => updateDraft({ keep_alive: e.target.value })}
            className="bg-codify-bg border border-codify-border rounded px-3 py-1.5 text-sm text-codify-secondary font-mono"
          />
          <p className="text-xs leading-relaxed text-codify-muted">
            How long Ollama holds this model loaded after a request finishes — a
            duration (<code>30m</code>, <code>1h30m</code>), bare seconds,{" "}
            <code>-1</code> for until the server stops. It does not make a call
            faster; it stops the model being reloaded between goals. Longer
            means more memory held.
          </p>
        </div>
      )}

      {/* System Prompt Override */}
      <PromptOverrideEditor
        value={active.system_prompt_override}
        maxLength={32768}
        onChange={(p) => updateDraft({ system_prompt_override: p })}
      />

      {/* Footer Test Connection */}
      <div className="pt-2 border-t border-codify-raised">
        <TestConnectionButton
          onTest={() => {
            // A connection test with no model chosen cannot succeed, and the raw
            // provider error ("ollama 400") would not say why.
            if (!(active.model_name || "").trim()) {
              return Promise.resolve({
                ok: false,
                message: "Choose a model first — the test calls the model, not just the endpoint.",
              });
            }
            return testConnection(role);
          }}
        />
      </div>
    </div>
  );
};
