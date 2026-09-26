import React from "react";
import { MessageSquare } from "lucide-react";

interface PromptOverrideEditorProps {
  value?: string | null;
  maxLength?: number;
  onChange: (val: string) => void;
}

export const PromptOverrideEditor: React.FC<PromptOverrideEditorProps> = ({
  value = "",
  maxLength = 32768,
  onChange,
}) => {
  const currentLen = (value || "").length;

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex justify-between items-center">
        <label className="text-xs font-semibold text-gray-400 uppercase tracking-wider flex items-center gap-1.5">
          <MessageSquare className="w-3.5 h-3.5" /> System Prompt Override
        </label>
        <span
          className={`text-xs ${
            currentLen > maxLength ? "text-red-400 font-bold" : "text-gray-500"
          }`}
        >
          {currentLen.toLocaleString()} / {maxLength.toLocaleString()} chars
        </span>
      </div>
      <textarea
        rows={4}
        placeholder="Leave blank to inherit the built-in system role contract prompt."
        value={value || ""}
        maxLength={maxLength}
        onChange={(e) => onChange(e.target.value)}
        className="w-full bg-codify-bg border border-codify-border rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500 font-mono resize-y"
      />
    </div>
  );
};
