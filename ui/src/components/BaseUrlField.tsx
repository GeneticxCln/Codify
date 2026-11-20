import React from "react";
import { Globe, AlertTriangle } from "lucide-react";

interface BaseUrlFieldProps {
  value: string;
  localOnly?: boolean;
  onChange: (val: string) => void;
}

/**
 * What is wrong with this base URL, if anything.
 *
 * Derived from the props on every render rather than remembered from the last keystroke: a
 * provider switch or a reset changes `value`/`localOnly` without a change event, and a
 * remembered warning would then describe a URL that is no longer in the field.
 */
export function baseUrlWarning(value: string, localOnly: boolean): string | null {
  if (!localOnly || !value) return null;
  try {
    const parsed = new URL(value);
    if (parsed.hostname !== "127.0.0.1" && parsed.hostname !== "localhost") {
      return "Local provider base_url must point at localhost or 127.0.0.1";
    }
    if (parsed.protocol !== "http:") {
      return "Local provider base_url must use http:// (unencrypted loopback)";
    }
    return null;
  } catch {
    return "Invalid URL format";
  }
}

export const BaseUrlField: React.FC<BaseUrlFieldProps> = ({
  value,
  localOnly = false,
  onChange,
}) => {
  const warning = baseUrlWarning(value, localOnly);

  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-xs font-semibold text-codify-muted uppercase tracking-wider flex items-center gap-1.5">
        <Globe className="w-3.5 h-3.5" /> Base Endpoint URL
      </label>
      <input
        type="text"
        placeholder={localOnly ? "http://127.0.0.1:11434" : "https://api.example.com/v1"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={`bg-codify-bg border rounded px-3 py-2 text-sm text-codify-secondary focus:outline-hidden font-mono ${
          // The warning state already *is* the focus colour, so the old
          // `focus:border-codify-warning` on top of `border-codify-warning` changed
          // nothing. One token says it once.
          warning ? "border-codify-warning" : "border-codify-border focus:border-codify-accent"
        }`}
      />
      {warning && (
        <div className="flex items-center gap-1.5 text-xs text-codify-warning mt-0.5">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
          <span>{warning}</span>
        </div>
      )}
    </div>
  );
};
