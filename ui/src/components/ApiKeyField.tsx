import React, { useState } from "react";
import { Key, Eye, EyeOff } from "lucide-react";

interface ApiKeyFieldProps {
  hasExistingKey?: boolean;
  value?: string;
  onChange: (val: string) => void;
}

export const ApiKeyField: React.FC<ApiKeyFieldProps> = ({
  hasExistingKey,
  value,
  onChange,
}) => {
  const [show, setShow] = useState(false);
  const [localVal, setLocalVal] = useState("");
  const val = value !== undefined ? value : localVal;

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (value === undefined) setLocalVal(e.target.value);
    onChange(e.target.value);
  };

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex justify-between items-center">
        <label className="text-xs font-semibold text-codify-muted uppercase tracking-wider flex items-center gap-1.5">
          <Key className="w-3.5 h-3.5" /> API Key
        </label>
        {hasExistingKey && !val && (
          <span className="text-xs text-codify-success font-medium">
            ✓ Securely stored in OS Keyring
          </span>
        )}
      </div>
      <div className="relative">
        <input
          type={show ? "text" : "password"}
          // Shown, it is a text field and no longer private by type; it is still a key.
          data-clipboard="off"
          placeholder={
            hasExistingKey
              ? "•••••••••••••••• (Leave blank to keep existing key)"
              : "Enter API key"
          }
          value={val}
          onChange={handleChange}
          className="w-full bg-codify-bg border border-codify-border rounded-sm px-3 py-2 text-sm text-codify-secondary focus:outline-hidden focus:border-codify-accent font-mono pr-10"
        />
        <button
          type="button"
          onClick={() => setShow(!show)}
          className="absolute right-2.5 top-2.5 text-codify-muted hover:text-codify-secondary"
          tabIndex={-1}
        >
          {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
        </button>
      </div>
    </div>
  );
};
