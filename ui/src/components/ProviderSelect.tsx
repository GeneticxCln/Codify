import React, { useEffect, useState } from "react";
import { providerLabel } from "../providerLabels";
import { FIELD_CLASS, SEARCHABLE_FROM, SelectMenu } from "./SelectMenu";

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
      <label className="text-xs font-semibold text-codify-muted uppercase tracking-wider">
        Provider
      </label>
      <div className="flex gap-2">
        <SelectMenu
          label="Provider"
          value={isCustom ? "custom" : value}
          options={[
            ...builtins.map((b) => ({ value: b, label: providerLabel(b) })),
            { value: "custom", label: "Custom Provider..." },
          ]}
          // The custom row is an option too, so it counts towards the length at
          // which the list stops being quicker to scan than to filter.
          searchable={builtins.length + 1 >= SEARCHABLE_FROM}
          searchPlaceholder="Search providers…"
          onChange={(next) => {
            if (next === "custom") {
              setIsCustom(true);
            } else {
              setIsCustom(false);
              onChange(next);
            }
          }}
          className={FIELD_CLASS + " flex-1"}
        />
        {isCustom && (
          <input
            type="text"
            placeholder="slug (e.g. openrouter, groq)"
            value={value}
            onChange={(e) => onChange(e.target.value.toLowerCase().trim())}
            className="flex-1 bg-codify-bg border border-codify-border rounded-sm px-3 py-2 text-sm text-codify-secondary focus:outline-hidden focus:border-codify-accent"
          />
        )}
      </div>
    </div>
  );
};
