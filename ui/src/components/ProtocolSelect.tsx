import React from "react";
import { ProviderProtocol } from "../types";

interface ProtocolSelectProps {
  value: ProviderProtocol;
  onChange: (val: ProviderProtocol) => void;
}

export const ProtocolSelect: React.FC<ProtocolSelectProps> = ({ value, onChange }) => {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
        Protocol
      </label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as ProviderProtocol)}
        className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
      >
        <option value="openai_compat">OpenAI Compatible (chat/completions)</option>
        <option value="anthropic">Anthropic (v1/messages)</option>
        <option value="ollama">Ollama (api/generate)</option>
        <option value="google">Google Gemini (v1beta)</option>
      </select>
    </div>
  );
};
