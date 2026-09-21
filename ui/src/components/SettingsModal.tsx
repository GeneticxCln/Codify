import React, { useState, useEffect } from "react";
import { ProviderKeyStatus } from "../types";
import { fetchProviderKeys, saveProviderKey } from "../api";
import { X, Key, Check, ShieldCheck, Cpu } from "lucide-react";

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export const SettingsModal: React.FC<SettingsModalProps> = ({ isOpen, onClose }) => {
  const [keys, setKeys] = useState<ProviderKeyStatus[]>([]);
  const [inputValues, setInputValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<Record<string, boolean>>({});
  const [savedSuccess, setSavedSuccess] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen) {
      loadKeys();
    }
  }, [isOpen]);

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

  return (
    <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-[#161b22] border border-[#30363d] rounded-2xl p-6 max-w-xl w-full shadow-2xl space-y-5">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-[#30363d] pb-3">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-xl bg-blue-600/20 border border-blue-500/30 flex items-center justify-center text-blue-400">
              <Key className="w-4 h-4" />
            </div>
            <div>
              <h2 className="text-base font-bold text-gray-100">API Keys & Provider Harness</h2>
              <p className="text-xs text-gray-400">
                Credentials are saved to your local OS Keyring or detected from environment variables.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-400 hover:text-gray-200 p-1 rounded-lg hover:bg-[#21262d] transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {error && (
          <div className="p-3 bg-red-950/40 border border-red-800 rounded-xl text-xs text-red-300">
            {error}
          </div>
        )}

        {/* Provider List */}
        <div className="max-h-96 overflow-y-auto space-y-3 pr-1">
          {keys.map((k) => (
            <div
              key={k.provider}
              className="bg-[#0d1117] border border-[#30363d] rounded-xl p-3.5 space-y-2"
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Cpu className="w-4 h-4 text-purple-400" />
                  <span className="font-semibold text-xs text-gray-200 capitalize">
                    {k.provider}
                  </span>
                  <span className="text-[10px] text-gray-500 font-mono">({k.protocol})</span>
                </div>

                {k.has_key ? (
                  <span className="flex items-center gap-1 text-[11px] font-medium text-green-400 bg-green-950/40 border border-green-800/60 px-2 py-0.5 rounded-full">
                    <ShieldCheck className="w-3 h-3" /> Configured
                  </span>
                ) : k.needs_key ? (
                  <span className="text-[11px] text-gray-500 bg-[#161b22] px-2 py-0.5 rounded-full border border-[#30363d]">
                    Missing Key
                  </span>
                ) : (
                  <span className="text-[11px] text-blue-400 bg-blue-950/40 border border-blue-800/60 px-2 py-0.5 rounded-full">
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
                    className="flex-1 bg-[#161b22] border border-[#30363d] rounded-lg px-3 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500 font-mono"
                  />
                  <button
                    type="button"
                    onClick={() => handleSaveKey(k.provider)}
                    disabled={saving[k.provider] || !inputValues[k.provider]?.trim()}
                    className={`px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1 transition-colors ${
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
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="pt-2 border-t border-[#30363d] flex justify-end">
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
