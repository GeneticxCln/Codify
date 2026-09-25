import React, { useState, useEffect, useCallback } from "react";
import {
  ModelCatalog,
  ModelOption,
  ProviderKeyStatus,
  ProviderModelStatus,
  RecentRunModel,
} from "../types";
import { fetchModelCatalog, fetchProviderKeys, saveProviderKey } from "../api";
import { SettingsPanel } from "./SettingsPanel";
import { X, Key, Check, ShieldCheck, Cpu, Sliders, HardDrive, Lock, AlertCircle, RefreshCw } from "lucide-react";

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
  /** Which tab to open on (defaults to keys). */
  initialTab?: "keys" | "agents";
  /**
   * Models that recently answered, handed down so a role's model field badges the
   * ids the app has actually run — the same hint the chat's menu shows.
   */
  recentRuns?: RecentRunModel[];
}

/**
 * The settings screen.
 *
 * It is a full-height panel, not a dialog: eight role cards and seven providers do
 * not fit in a 2xl box with a 26rem scroll region, and shrinking them into one
 * made every card half cut off. Left column = where credentials live, right
 * column = which agent gets which model, both scrolling independently.
 */
export const SettingsModal: React.FC<SettingsModalProps> = ({
  isOpen,
  onClose,
  initialTab = "keys",
  recentRuns = [],
}) => {
  const [tab, setTab] = useState<"keys" | "agents">(initialTab);
  const [keys, setKeys] = useState<ProviderKeyStatus[]>([]);
  const [inputValues, setInputValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<Record<string, boolean>>({});
  const [savedSuccess, setSavedSuccess] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  // Discovered models, so a freshly saved key immediately shows what that
  // provider offers — and a failing provider shows why it offered nothing.
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelStatus, setModelStatus] = useState<ProviderModelStatus[]>([]);

  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  const loadCatalog = useCallback(async (refresh = false) => {
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      const catalog: ModelCatalog = await fetchModelCatalog(refresh);
      setModels(catalog.models ?? []);
      setModelStatus(catalog.providers ?? []);
    } catch (err: any) {
      setCatalogError(err?.message || "Could not discover models from the engine.");
    } finally {
      setCatalogLoading(false);
    }
  }, []);

  useEffect(() => {
    setTab(initialTab);
  }, [initialTab, isOpen]);

  useEffect(() => {
    if (isOpen && tab === "keys") {
      loadKeys();
    }
    if (isOpen) {
      loadCatalog(true);
    }
  }, [isOpen, tab, loadCatalog]);

  // Escape closes, like every other overlay in the app.
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen, onClose]);

  const loadKeys = async () => {
    try {
      const data = await fetchProviderKeys();
      setKeys(data);
    } catch (err: any) {
      setError(err.message || "Failed to load provider credentials");
    }
  };

  const handleSaveKey = async (provider: string) => {
    const key = inputValues[provider]?.trim();
    if (!key) return;

    setSaving((prev) => ({ ...prev, [provider]: true }));
    setError(null);
    try {
      await saveProviderKey(provider, key);
      setSavedSuccess((prev) => ({ ...prev, [provider]: true }));
      setInputValues((prev) => ({ ...prev, [provider]: "" }));
      await loadKeys();
      // The engine invalidates its catalog on key save; refresh so this
      // provider's models are listed without closing the dialog.
      await loadCatalog(true);
      setTimeout(() => {
        setSavedSuccess((prev) => ({ ...prev, [provider]: false }));
      }, 2500);
    } catch (err: any) {
      setError(err.message || `Failed to save ${provider} key`);
    } finally {
      setSaving((prev) => ({ ...prev, [provider]: false }));
    }
  };

  if (!isOpen) return null;

  const storage = keys[0]?.storage;
  const storageDetail = keys[0]?.storage_detail;
  // Three ways to end up in a file, and only one of them is a missing keyring.
  // An older engine does not send the reason, so the absent case keeps the old copy.
  const storageReason = keys[0]?.storage_reason ?? (storage === "file" ? "no_keyring" : "keyring");
  const needsKeyCount = keys.filter((k) => k.needs_key && !k.has_key).length;

  return (
    <div className="fixed inset-0 bg-black/75 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-[#161b22] border border-[#30363d] rounded-2xl shadow-2xl w-full max-w-6xl h-[90vh] flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-start justify-between border-b border-[#30363d] px-6 py-4 flex-shrink-0">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-blue-600/20 border border-blue-500/30 flex items-center justify-center text-blue-400">
              <Key className="w-4 h-4" />
            </div>
            <div>
              <h2 className="text-base font-bold text-gray-100">Settings</h2>
              <p className="text-xs text-gray-400">
                {tab === "keys"
                  ? needsKeyCount > 0
                    ? `${needsKeyCount} of ${keys.length} providers still need a key — their models cannot be listed or called until then.`
                    : "Every keyed provider has a credential. Models are discovered live from each provider."
                  : "Each role's model, endpoint, temperature, and prompt. Roles run in the order of the pipeline."}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-400 hover:text-gray-200 p-1.5 rounded-lg hover:bg-[#21262d] transition-colors"
            title="Close (Esc)"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Tab bar */}
        <div className="flex items-center gap-1 px-6 pt-3 flex-shrink-0">
          <button
            type="button"
            onClick={() => setTab("keys")}
            className={`flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
              tab === "keys"
                ? "bg-[#21262d] text-gray-100 border border-[#30363d]"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            <Key className="w-3.5 h-3.5" /> Provider Keys
            {needsKeyCount > 0 && (
              <span className="ml-0.5 text-[10px] px-1.5 rounded-full bg-amber-950/60 border border-amber-800/60 text-amber-300">
                {needsKeyCount}
              </span>
            )}
          </button>
          <button
            type="button"
            onClick={() => setTab("agents")}
            className={`flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
              tab === "agents"
                ? "bg-[#21262d] text-gray-100 border border-[#30363d]"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            <Sliders className="w-3.5 h-3.5" /> Agent Roles
          </button>
        </div>

        {/* Body — the only scrolling region, full panel height */}
        <div className="flex-1 min-h-0 overflow-y-auto px-6 py-4">
          {tab === "keys" && (
            <div className="space-y-4">
              {error && (
                <div className="flex items-start gap-2 p-3 bg-red-950/40 border border-red-800 rounded-xl text-xs text-red-300">
                  <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                  <span>{error}</span>
                </div>
              )}
              {catalogError && (
                <div className="flex items-start gap-2 p-3 bg-amber-950/30 border border-amber-800/60 rounded-xl text-xs text-amber-300">
                  <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                  <span>{catalogError}</span>
                </div>
              )}

              {/* Where a key actually goes. Saying "your OS keychain" when the key
                  is really written to a file would be a lie the user cannot see. */}
              {storage && (
                <div className="flex items-start gap-2 text-[11px] text-gray-400 bg-[#0d1117] border border-[#30363d] rounded-xl px-3.5 py-2.5">
                  {storage === "keyring" ? (
                    <Lock className="w-3.5 h-3.5 flex-shrink-0 mt-0.5 text-green-400" />
                  ) : (
                    <HardDrive className="w-3.5 h-3.5 flex-shrink-0 mt-0.5 text-amber-400" />
                  )}
                  <span className="leading-relaxed">
                    {storageReason === "keyring" ? (
                      <>Keys are stored in your OS keychain.</>
                    ) : storageReason === "isolated_run" ? (
                      <>
                        Keys are stored in <span className="font-mono text-gray-300">{storageDetail}</span>
                        . That is by design here, not a missing keychain: this engine was started with its
                        own state directory, so it cannot read or write the real one.
                      </>
                    ) : (
                      <>
                        Keys are stored in <span className="font-mono text-gray-300">{storageDetail}</span>{" "}
                        — this machine has no usable OS keychain, so Codify keeps them in an owner-only
                        file instead. Install <span className="font-mono text-gray-300">keyring</span>{" "}
                        (with a Secret Service / Keychain backend) to move them there.
                      </>
                    )}{" "}
                    Keys are also read from environment variables (
                    <span className="font-mono">OPENAI_API_KEY</span>,{" "}
                    <span className="font-mono">ANTHROPIC_API_KEY</span>, …) when none is stored.
                  </span>
                </div>
              )}

              {/* Provider list — two columns, so seven providers do not need a scroll */}
              <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
                {keys.map((k) => (
                  <div
                    key={k.provider}
                    className="bg-[#0d1117] border border-[#30363d] rounded-xl p-3.5 space-y-2"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2 min-w-0">
                        <Cpu className="w-4 h-4 text-purple-400 flex-shrink-0" />
                        <span className="font-semibold text-xs text-gray-200 capitalize truncate">
                          {k.provider}
                        </span>
                        <span className="text-[10px] text-gray-500 font-mono truncate">
                          ({k.protocol})
                        </span>

                        {/* Live discovery result for this provider. */}
                        {models.length > 0 || modelStatus.length > 0
                          ? (() => {
                              const status = modelStatus.find((p) => p.provider === k.provider);
                              if (status?.ok) {
                                return (
                                  <span className="text-[10px] text-gray-500 flex-shrink-0">
                                    {status.count} models
                                  </span>
                                );
                              }
                              if (status?.error) {
                                return (
                                  <span className="text-[10px] text-amber-400/90 truncate">
                                    — {status.error}
                                  </span>
                                );
                              }
                              return null;
                            })()
                          : null}
                      </div>

                      {k.has_key ? (
                        <span className="flex items-center gap-1 text-[11px] font-medium text-green-400 bg-green-950/40 border border-green-800/60 px-2 py-0.5 rounded-full flex-shrink-0">
                          <ShieldCheck className="w-3 h-3" /> Configured
                        </span>
                      ) : k.needs_key ? (
                        <span className="text-[11px] text-gray-500 bg-[#161b22] px-2 py-0.5 rounded-full border border-[#30363d] flex-shrink-0">
                          Missing Key
                        </span>
                      ) : (
                        <span className="text-[11px] text-blue-400 bg-blue-950/40 border border-blue-800/60 px-2 py-0.5 rounded-full flex-shrink-0">
                          Local / No Key
                        </span>
                      )}
                    </div>

                    {k.needs_key && (
                      <div className="flex items-center gap-2 pt-1">
                        <input
                          type="password"
                          placeholder={`Enter ${k.provider} API key...`}
                          value={inputValues[k.provider] || ""}
                          onChange={(e) =>
                            setInputValues((prev) => ({ ...prev, [k.provider]: e.target.value }))
                          }
                          className="flex-1 min-w-0 bg-[#161b22] border border-[#30363d] rounded-lg px-3 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500 font-mono"
                        />
                        <button
                          type="button"
                          onClick={() => handleSaveKey(k.provider)}
                          disabled={saving[k.provider] || !inputValues[k.provider]?.trim()}
                          className={`px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1 transition-colors flex-shrink-0 ${
                            savedSuccess[k.provider]
                              ? "bg-green-600 text-white"
                              : "bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-40"
                          }`}
                        >
                          {savedSuccess[k.provider] ? (
                            <>
                              <Check className="w-3.5 h-3.5" /> Saved
                            </>
                          ) : saving[k.provider] ? (
                            "Saving..."
                          ) : (
                            "Save Key"
                          )}
                        </button>
                      </div>
                    )}

                    {!k.needs_key && (
                      <p className="text-[10px] text-gray-500 pl-6">
                        Local server at <span className="font-mono">{k.base_url}</span> — its downloaded
                        models are discovered automatically, no key.
                      </p>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {tab === "agents" && (
            <SettingsPanel
              embedded
              models={models}
              providerStatus={modelStatus}
              recentRuns={recentRuns}
              onRefreshModels={() => loadCatalog(true)}
              refreshingModels={catalogLoading}
            />
          )}
        </div>

        {/* Footer */}
        <div className="border-t border-[#30363d] px-6 py-3 flex items-center justify-between flex-shrink-0">
          <span className="flex items-center gap-2 text-[11px] text-gray-500">
            {models.length} models discovered across {modelStatus.filter((s) => s.ok).length} providers
            <button
              type="button"
              onClick={() => loadCatalog(true)}
              disabled={catalogLoading}
              title="Ask every configured provider what it serves right now"
              className="flex items-center gap-1 text-gray-400 hover:text-gray-200 disabled:opacity-50 cursor-pointer"
            >
              <RefreshCw className={catalogLoading ? "w-3 h-3 animate-spin" : "w-3 h-3"} />
              Refresh
            </button>
          </span>
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-1.5 bg-[#21262d] hover:bg-[#30363d] text-gray-200 rounded-lg text-xs font-semibold transition-colors"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
};
