import React, { useState } from "react";
import { Globe, AlertTriangle } from "lucide-react";

interface BaseUrlFieldProps {
  value: string;
  localOnly?: boolean;
  onChange: (val: string) => void;
}

export const BaseUrlField: React.FC<BaseUrlFieldProps> = ({
  value,
  localOnly = false,
  onChange,
}) => {
  const [warning, setWarning] = useState<string | null>(null);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    onChange(val);

    if (localOnly && val) {
      try {
        const parsed = new URL(val);
        if (!["127.0.0.1", "localhost"].includes(parsed.hostname)) {
          setWarning("Local provider base_url must point at localhost or 127.0.0.1");
          return;
        }
        if (parsed.protocol !== "http:") {
          setWarning("Local provider base_url must use http:// (unencrypted loopback)");
          return;
        }
        setWarning(null);
      } catch {
        setWarning("Invalid URL format");
      }
    } else {
      setWarning(null);
    }
  };

  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-xs font-semibold text-codify-muted uppercase tracking-wider flex items-center gap-1.5">
        <Globe className="w-3.5 h-3.5" /> Base Endpoint URL
      </label>
      <input
        type="text"
        placeholder={localOnly ? "http://127.0.0.1:11434" : "https://api.example.com/v1"}
        value={value}
        onChange={handleChange}
        className={`bg-codify-bg border rounded px-3 py-2 text-sm text-codify-secondary focus:outline-none font-mono ${
          // The warning state already *is* the focus colour, so the old
          // `focus:border-codify-warning` on top of `border-codify-warning` changed
          // nothing. One token says it once.
          warning ? "border-codify-warning" : "border-codify-border focus:border-codify-accent"
        }`}
      />
      {warning && (
        <div className="flex items-center gap-1.5 text-xs text-codify-warning mt-0.5">
          <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
          <span>{warning}</span>
        </div>
      )}
    </div>
  );
};
