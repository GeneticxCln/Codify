import React, { useState } from "react";

interface ProviderSelectProps {
  value: string;
  onChange: (val: string) => void;
  builtins?: string[];
}

const DEFAULT_BUILTINS = ["anthropic", "openai", "deepseek", "ollama", "google"];

export const ProviderSelect: React.FC<ProviderSelectProps> = ({
  value,
  onChange,
  builtins = DEFAULT_BUILTINS,
}) => {
  const [isCustom, setIsCustom] = useState(!builtins.includes(value));

  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
        Provider
      </label>
      <div className="flex gap-2">
        <select
          value={isCustom ? "custom" : value}
          onChange={(e) => {
            if (e.target.value === "custom") {
              setIsCustom(true);
            } else {
              setIsCustom(false);
              onChange(e.target.value);
            }
          }}
          className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
        >
          {builtins.map((b) => (
            <option key={b} value={b}>
              {b.toUpperCase()}
            </option>
          ))}
          <option value="custom">Custom Provider...</option>
        </select>
        {isCustom && (
          <input
            type="text"
            placeholder="slug (e.g. openrouter, groq)"
            value={value}
            onChange={(e) => onChange(e.target.value.toLowerCase().trim())}
            className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
          />
        )}
      </div>
    </div>
  );
};
