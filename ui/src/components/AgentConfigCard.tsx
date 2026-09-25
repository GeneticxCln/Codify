import React, { useState, useEffect } from "react";
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

const BUILTINS = new Set(["anthropic", "openai", "deepseek", "ollama", "google"]);

export const AgentConfigCard: React.FC<AgentConfigCardProps> = ({
  onRefreshModels,
  refreshingModels = false,
  role,
  store,
  models = [],
  providerStatus = [],
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
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-6 animate-pulse">
        <div className="h-6 w-32 bg-[#30363d] rounded mb-4" />
        <div className="h-24 bg-[#21262d] rounded" />
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
      setTimeout(() => setSaved(false), 2500);
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
  const isCustomProvider = !BUILTINS.has(active.provider);
  const isOllama = active.provider === "ollama";
  const needsKey = active.protocol !== "ollama";

  // ── Fallback target ──────────────────────────────────────────────────────
  const fallbackProvider = (active.fallback_provider || "").trim();
  const hasFallback = Boolean(fallbackProvider && (active.fallback_model_name || "").trim());
  const showFallback = Boolean(fallbackProvider) || fallbackOpen;
  const fallbackCustom = Boolean(fallbackProvider) && !BUILTINS.has(fallbackProvider);
  const fallbackModels = models.filter((m) => m.provider === fallbackProvider);
  const fallbackDiscovery = providerStatus.find((s) => s.provider === fallbackProvider);

  return (
    <div
      className={`bg-[#161b22] border rounded-lg p-5 flex flex-col gap-4 shadow-sm transition-colors ${
        stale ? "border-amber-700/70" : "border-[#30363d] hover:border-[#484f58]"
      }`}
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-[#30363d] pb-3">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-[#21262d] rounded border border-[#30363d]">
            {ROLE_ICONS[role]}
          </div>
          <div>
            <h3 className="font-semibold text-base text-gray-100 flex items-center gap-2">
              {active.display_name}
              <span className="text-xs px-2 py-0.5 bg-[#21262d] text-gray-400 rounded-full font-mono font-normal">
                {role}
              </span>
            </h3>
            <p className="text-xs text-gray-400">
              Assigned model: <span className="font-mono text-gray-300">{active.provider}/{active.model_name}</span>
              {hasFallback && (
                <>
                  <span className="text-gray-500"> · falls back to </span>
                  <span className="font-mono text-gray-400">
                    {fallbackProvider}/{active.fallback_model_name}
                  </span>
                </>
              )}
            </p>
            {info && (
              <p className="text-[11px] text-gray-500 mt-0.5 max-w-2xl leading-relaxed">
                <span className="text-gray-400">{info.job}</span>{" "}
                <span className="whitespace-nowrap text-gray-500">
                  · runs {info.timing}
                  {GOAL_LEVEL.includes(role) && role !== "laya" ? " (not per step)" : ""}
                </span>
              </p>
            )}
            {callStat && (callStat.last_call || callStat.last_error || (callStat.runs ?? 0) > 0) && (
              <p className="text-[11px] text-gray-500 mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                {(callStat.runs ?? 0) > 0 && (
                  <span
                    className="whitespace-nowrap text-gray-500"
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
                          ? "text-green-400/90"
                          : (callStat.success_rate ?? 0) >= 60
                            ? "text-amber-300/90"
                            : "text-red-300/90"
                      }`}
                    >
                      {callStat.success_rate == null ? "—" : `${callStat.success_rate}%`}
                    </span>{" "}
                    <span className="text-gray-600">
                      over {callStat.runs} run{callStat.runs === 1 ? "" : "s"}
                    </span>
                  </span>
                )}
                {callStat.last_call && (
                  <span className="whitespace-nowrap text-gray-400">
                    · last call {formatCallDuration(callStat.last_call.duration_ms)}
                    {callStat.last_call.provider && (
                      <span className="font-mono text-gray-500">
                        {" "}({callStat.last_call.provider}/{callStat.last_call.model})
                      </span>
                    )}
                  </span>
                )}
                {callStat.last_error && (
                  <span
                    className="whitespace-nowrap text-amber-400/90"
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
          {saveError && <span className="text-xs text-red-400 font-medium">{saveError}</span>}
          <button
            onClick={handleSave}
            disabled={saving}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded transition-colors ${
              saved
                ? "bg-green-600 text-white"
                : "bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50"
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
        <div className="flex items-start gap-2 text-[11px] text-amber-300 bg-amber-950/30 border border-amber-800/60 rounded-lg px-3 py-2">
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
        <div className="flex items-start gap-2 text-[11px] text-blue-300 bg-blue-950/30 border border-blue-800/60 rounded-lg px-3 py-2">
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
            className="flex-shrink-0 text-[11px] px-2 py-0.5 rounded border border-blue-700/60 hover:bg-blue-900/40 transition-colors"
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
      <div className="border border-[#30363d] rounded-lg p-3.5 flex flex-col gap-3 bg-[#0d1117]">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-xs font-semibold text-gray-200">
            <Route className="w-3.5 h-3.5 text-teal-400" />
            Fallback target
            {hasFallback ? (
              <span className="font-mono font-normal text-teal-300">
                {fallbackProvider}/{active.fallback_model_name}
              </span>
            ) : (
              <span className="font-normal text-gray-500">none</span>
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
            className="text-[11px] px-2 py-1 rounded border border-[#30363d] text-gray-300 hover:text-gray-100 hover:border-[#484f58] transition-colors"
          >
            {showFallback ? "Remove" : "Add a fallback"}
          </button>
        </div>

        {showFallback ? (
          <>
            <p className="text-[11px] text-gray-500 leading-relaxed">
              Used when the primary target cannot be called at all: no credential stored, the
              endpoint unreachable, the model no longer served, or a reply the contract cannot
              parse. Tried once per call — never a retry loop. The role's temperature and max
              tokens apply to both targets.
            </p>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <ProviderSelect
                value={fallbackProvider}
                onChange={(p) => updateDraft({ fallback_provider: p })}
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
              <div className="flex items-start gap-2 text-[11px] text-amber-300 bg-amber-950/30 border border-amber-800/60 rounded-lg px-3 py-2">
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
              <span className="text-[11px] text-amber-300/90">
                Not in force yet: a fallback needs a model as well as a provider, and half of one
                fails exactly when it is needed.
              </span>
            )}
          </>
        ) : (
          <p className="text-[11px] text-gray-500 leading-relaxed">
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
              className="text-xs font-semibold text-gray-400 uppercase tracking-wider"
            >
              Temperature
            </label>
            <span className="text-xs font-mono text-gray-300">{active.temperature}</span>
          </div>
          <input
            id={`temperature-${role}`}
            type="range"
            min={0}
            max={2}
            step={0.05}
            value={active.temperature}
            onChange={(e) => updateDraft({ temperature: parseFloat(e.target.value) })}
            className="w-full accent-blue-500 cursor-pointer"
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <div className="flex justify-between">
            <label className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
              Max Tokens
            </label>
            <span className="text-xs font-mono text-gray-300">{active.max_tokens.toLocaleString()}</span>
          </div>
          <input
            type="number"
            min={128}
            max={200000}
            step={256}
            value={active.max_tokens}
            onChange={(e) => updateDraft({ max_tokens: parseInt(e.target.value, 10) || 4096 })}
            className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-gray-200 font-mono"
          />
        </div>
      </div>

      {/* System Prompt Override */}
      <PromptOverrideEditor
        value={active.system_prompt_override}
        maxLength={32768}
        onChange={(p) => updateDraft({ system_prompt_override: p })}
      />

      {/* Footer Test Connection */}
      <div className="pt-2 border-t border-[#21262d]">
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
