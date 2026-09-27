import React, { useEffect, useState } from "react";

interface ProviderSelectProps {
  value: string;
  onChange: (val: string) => void;
  /**
   * The provider slugs the engine ships, from `GET /settings/providers`.
   *
   * Required, and there is deliberately no default: this list used to be a
   * second copy in the UI and it went stale the moment the engine gained a
   * provider, so `openrouter` and `groq` became invisible here *and* were
   * mistaken downstream for custom endpoints. `engine/models.py`
   * `BUILTIN_PROVIDERS` is the only place this is defined; making the prop
   * required means the compiler, not a review, is what stops the copy returning.
   */
  builtins: string[];
}

export const ProviderSelect: React.FC<ProviderSelectProps> = ({
  value,
  onChange,
  builtins,
}) => {
  const [isCustom, setIsCustom] = useState(!builtins.includes(value));

  // The stored provider can change under this control — a repair, a bulk
  // assign, or "Load server values" — and a control still showing the custom
  // text box for a provider the engine ships is asking the user to fix
  // something that is not broken.
  useEffect(() => {
    setIsCustom(!builtins.includes(value));
  }, [builtins, value]);

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
          className="flex-1 bg-codify-bg border border-codify-border rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
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
            className="flex-1 bg-codify-bg border border-codify-border rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
          />
        )}
      </div>
    </div>
  );
};
