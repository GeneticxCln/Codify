import React from "react";

interface ModelSelectProps {
  provider: string;
  value: string;
  onChange: (val: string) => void;
}

export const ModelSelect: React.FC<ModelSelectProps> = ({ value, onChange }) => {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
        Model Identifier
      </label>
      <input
        type="text"
        placeholder="e.g. claude-sonnet-4-6, gpt-4.1-mini, qwen2.5-coder"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500 font-mono"
      />
    </div>
  );
};
