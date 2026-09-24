import React, { useEffect, useState } from "react";
import {
  AgentConfig,
  ModelCatalog,
  ModelOption,
  ProviderKeyStatus,
  ProviderModelStatus,
} from "../types";
import { fetchAgentConfigs, fetchModelCatalog, fetchProviderKeys } from "../api";
import { diagnoseFailure, Verdict } from "../failureDiagnosis";
import {
  AlertTriangle,
  CheckCircle2,
  Info,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Wand2,
  X,
} from "lucide-react";

interface FailureDiagnosisPanelProps {
  error: { code: string; message: string; role?: string | null };
  onClose: () => void;
  /** Jump to the screen that holds the fix. */
  onOpenSettings: (tab: "keys" | "agents") => void;
}

const LEVEL_STYLE: Record<Verdict["level"], { wrap: string; icon: React.ReactNode }> = {
  blocker: {
    wrap: "bg-red-950/30 border-red-800/70 text-red-200",
    icon: <ShieldAlert className="w-4 h-4 text-red-400 flex-shrink-0 mt-0.5" />,
  },
  warning: {
    wrap: "bg-amber-950/30 border-amber-800/70 text-amber-200",
    icon: <AlertTriangle className="w-4 h-4 text-amber-400 flex-shrink-0 mt-0.5" />,
  },
  info: {
    wrap: "bg-[#0d1117] border-[#30363d] text-gray-300",
    icon: <Info className="w-4 h-4 text-blue-400 flex-shrink-0 mt-0.5" />,
  },
};

const Fact: React.FC<{ label: string; value: React.ReactNode }> = ({ label, value }) => (
  <div className="flex items-baseline justify-between gap-3 py-1 border-b border-[#21262d] last:border-b-0">
    <span className="text-[11px] text-gray-500 uppercase tracking-wider">{label}</span>
    <span className="text-[11px] font-mono text-gray-200 text-right break-all">{value}</span>
  </div>
);

/**
 * "Why did this fail?" — the failing role's config, credential, and live catalog
 * side by side, with the reason ranked first.
 *
 * Everything is read fresh when it opens: a stale answer here would be worse
 * than none, since the user's next move depends on it.
 */
export const FailureDiagnosisPanel: React.FC<FailureDiagnosisPanelProps> = ({
  error,
  onClose,
  onOpenSettings,
}) => {
  const [loading, setLoading] = useState(true);
  const [configs, setConfigs] = useState<AgentConfig[]>([]);
  const [keys, setKeys] = useState<ProviderKeyStatus[]>([]);
  const [catalog, setCatalog] = useState<ModelOption[]>([]);
  const [status, setStatus] = useState<ProviderModelStatus[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [cfg, keyList, cat] = await Promise.all([
        fetchAgentConfigs(),
        fetchProviderKeys(),
        fetchModelCatalog(true),
      ]);
      setConfigs(cfg ?? []);
      setKeys(keyList ?? []);
      setCatalog((cat as ModelCatalog).models ?? []);
      setStatus((cat as ModelCatalog).providers ?? []);
    } catch (err: any) {
      setLoadError(err?.message || "Could not read the engine's current state.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [error.code, error.message]);

  const config = configs.find((c) => c.role === error.role);
  const provider = config?.provider;
  const key = keys.find((k) => k.provider === provider);
  const discovery = status.find((s) => s.provider === provider);
  const verdicts = diagnoseFailure({
    code: error.code,
    message: error.message,
    role: error.role,
    config,
    keys: key,
    discovery,
    catalog,
  });

  return (
    <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Failure diagnosis"
        className="bg-[#161b22] border border-[#30363d] rounded-2xl shadow-2xl w-full max-w-2xl max-h-[85vh] flex flex-col"
      >
        <div className="flex items-start justify-between gap-3 border-b border-[#30363d] px-5 py-3.5">
          <div className="flex flex-col gap-0.5 min-w-0">
            <h3 className="text-sm font-bold text-gray-100 flex items-center gap-2">
              <ShieldAlert className="w-4 h-4 text-red-400" />
              Why did this fail?
            </h3>
            <p className="text-[11px] text-gray-400 break-words">
              <span className="font-mono">{error.code}</span>
              {error.role && (
                <>
                  {" · role "}
                  <span className="font-mono text-gray-300">{error.role}</span>
                </>
              )}
            </p>
          </div>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={load}
              disabled={loading}
              title="Re-read the engine's current state"
              aria-label="Re-read the engine's current state"
              className="text-gray-400 hover:text-gray-200 p-1.5 rounded-lg hover:bg-[#21262d] disabled:opacity-40"
            >
              <RefreshCw className={loading ? "w-4 h-4 animate-spin" : "w-4 h-4"} />
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close failure diagnosis"
              className="text-gray-400 hover:text-gray-200 p-1.5 rounded-lg hover:bg-[#21262d]"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4 flex flex-col gap-3">
          {loading && (
            <div className="flex items-center gap-2 text-xs text-gray-400">
              <Loader2 className="w-3.5 h-3.5 animate-spin" /> Reading the role's config, credential and
              catalog...
            </div>
          )}

          {!loading && loadError && (
            <div className="border border-red-800/70 bg-red-950/30 rounded-xl px-3.5 py-2.5 text-xs text-red-200">
              Could not read the engine's current state: {loadError} The verdicts below are
              based on the failure alone.
            </div>
          )}

          {!loading && !loadError && (
            <>
              {verdicts.map((v, i) => (
                <div key={i} className={`border rounded-xl px-3.5 py-2.5 ${LEVEL_STYLE[v.level].wrap}`}>
                  <div className="flex items-start gap-2">
                    {LEVEL_STYLE[v.level].icon}
                    <div className="flex flex-col gap-1 min-w-0">
                      <span className="text-xs font-semibold">{v.title}</span>
                      <span className="text-[11px] leading-relaxed opacity-90">{v.detail}</span>
                      {v.fix && (
                        <button
                          type="button"
                          onClick={() => onOpenSettings(v.fix!.tab)}
                          className="mt-1 flex w-fit items-center gap-1.5 px-2.5 py-1 bg-blue-600 hover:bg-blue-500 text-white text-[11px] font-semibold rounded-lg transition-colors"
                        >
                          <Wand2 className="w-3 h-3" />
                          {v.fix.label}
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              ))}

              <div className="bg-[#0d1117] border border-[#30363d] rounded-xl px-3.5 py-2.5 flex flex-col gap-2">
                <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
                  What the engine holds
                </span>
                {config ? (
                  <div className="flex flex-col">
                    <Fact label="role" value={config.role} />
                    <Fact label="provider" value={`${config.provider} (${config.protocol})`} />
                    <Fact
                      label="model"
                      value={
                        config.model_name ? (
                          config.model_name
                        ) : (
                          <span className="text-amber-300">none chosen</span>
                        )
                      }
                    />
                    {config.base_url && <Fact label="endpoint" value={config.base_url} />}
                    <Fact
                      label="credential"
                      value={
                        /* A provider that needs no key reports has_key=true because it
                           is *usable*, not because something is stored — asking about
                           has_key first claimed a credential that does not exist. */
                        key?.needs_key === false ? (
                          <span className="text-blue-300">not needed ({config.provider})</span>
                        ) : key?.has_key ? (
                          <span className="text-green-400">
                            stored in {key.storage === "file" ? "local file" : "OS keychain"}
                          </span>
                        ) : key ? (
                          <span className="text-red-400">missing</span>
                        ) : (
                          <span className="text-gray-400">unknown</span>
                        )
                      }
                    />
                    <Fact
                      label="provider models"
                      value={
                        discovery?.ok ? (
                          <span className="text-green-400">{discovery.count} discovered</span>
                        ) : (
                          <span className="text-amber-300">
                            {discovery?.error || "not checked / unreachable"}
                          </span>
                        )
                      }
                    />
                  </div>
                ) : (
                  <span className="text-[11px] text-gray-500 flex items-center gap-1.5">
                    <CheckCircle2 className="w-3.5 h-3.5" />
                    No role attribution on this failure — nothing to look up.
                  </span>
                )}
              </div>

              <p className="text-[11px] text-gray-500 leading-relaxed">
                Engine said: <span className="font-mono text-gray-400">{error.message}</span>
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  );
};
