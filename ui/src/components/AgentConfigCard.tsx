import React, { useState, useEffect } from "react";
import { AgentConfig, AgentRole, ProviderProtocol } from "../types";
import { useAgentConfigs } from "../hooks/useAgentConfigs";
import { ProviderSelect } from "./ProviderSelect";
import { ProtocolSelect } from "./ProtocolSelect";
import { ModelSelect } from "./ModelSelect";
import { ApiKeyField } from "./ApiKeyField";
import { BaseUrlField } from "./BaseUrlField";
import { PromptOverrideEditor } from "./PromptOverrideEditor";
import { TestConnectionButton } from "./TestConnectionButton";
import { Save, Check, Bot, Sparkles, Terminal, ShieldCheck, FileText } from "lucide-react";

interface AgentConfigCardProps {
  role: AgentRole;
}

const ROLE_ICONS: Record<AgentRole, React.ReactNode> = {
  planner: <Sparkles className="w-5 h-5 text-purple-400" />,
  coder: <Terminal className="w-5 h-5 text-blue-400" />,
  tester: <Bot className="w-5 h-5 text-green-400" />,
  reviewer: <ShieldCheck className="w-5 h-5 text-amber-400" />,
  summarizer: <FileText className="w-5 h-5 text-indigo-400" />,
};

const BUILTINS = new Set(["anthropic", "openai", "deepseek", "ollama", "google"]);

export const AgentConfigCard: React.FC<AgentConfigCardProps> = ({ role }) => {
  const { configs, update, testConnection } = useAgentConfigs();
  const config = configs.find((c) => c.role === role);

  const [draft, setDraft] = useState<AgentConfig | undefined>(config);
  const [pendingApiKey, setPendingApiKey] = useState<string | undefined>();
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Sync draft when server config arrives
  useEffect(() => {
    if (config) {
      setDraft((prev) => (prev ? { ...config, ...prev } : config));
    }
  }, [config]);

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
      await update(role, {
        display_name: active.display_name,
        provider: active.provider,
        protocol: active.protocol,
        model_name: active.model_name,
        api_key: pendingApiKey || undefined,
        base_url: active.base_url || undefined,
        temperature: active.temperature,
        max_tokens: active.max_tokens,
        system_prompt_override: active.system_prompt_override?.trim() || null,
      });
      setPendingApiKey(undefined);
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } catch (err: any) {
      setSaveError(err.message || "Failed to save configuration");
    } finally {
      setSaving(false);
    }
  };

  const isCustomProvider = !BUILTINS.has(active.provider);
  const isOllama = active.provider === "ollama";
  const needsKey = active.protocol !== "ollama";

  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-5 flex flex-col gap-4 shadow-sm hover:border-[#484f58] transition-colors">
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
            </p>
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

      {/* Grid Form Fields */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <ProviderSelect
          value={active.provider}
          onChange={(p) => setDraft({ ...active, provider: p })}
        />

        {isCustomProvider ? (
          <ProtocolSelect
            value={active.protocol}
            onChange={(proto: ProviderProtocol) => setDraft({ ...active, protocol: proto })}
          />
        ) : (
          <ModelSelect
            provider={active.provider}
            value={active.model_name}
            onChange={(m) => setDraft({ ...active, model_name: m })}
          />
        )}
      </div>

      {isCustomProvider && (
        <ModelSelect
          provider={active.provider}
          value={active.model_name}
          onChange={(m) => setDraft({ ...active, model_name: m })}
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
            onChange={(u) => setDraft({ ...active, base_url: u })}
          />
        )}
      </div>

      {/* Numeric Sliders / Steppers */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="flex flex-col gap-1.5">
          <div className="flex justify-between">
            <label className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
              Temperature
            </label>
            <span className="text-xs font-mono text-gray-300">{active.temperature}</span>
          </div>
          <input
            type="range"
            min={0}
            max={2}
            step={0.05}
            value={active.temperature}
            onChange={(e) => setDraft({ ...active, temperature: parseFloat(e.target.value) })}
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
            onChange={(e) => setDraft({ ...active, max_tokens: parseInt(e.target.value, 10) || 4096 })}
            className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-gray-200 font-mono"
          />
        </div>
      </div>

      {/* System Prompt Override */}
      <PromptOverrideEditor
        value={active.system_prompt_override}
        maxLength={32768}
        onChange={(p) => setDraft({ ...active, system_prompt_override: p })}
      />

      {/* Footer Test Connection */}
      <div className="pt-2 border-t border-[#21262d]">
        <TestConnectionButton onTest={() => testConnection(role)} />
      </div>
    </div>
  );
};
