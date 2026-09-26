import React, { useState } from "react";
import { Key, Eye, EyeOff } from "lucide-react";

interface ApiKeyFieldProps {
  hasExistingKey?: boolean;
  onChange: (val: string) => void;
}

export const ApiKeyField: React.FC<ApiKeyFieldProps> = ({
  hasExistingKey,
  onChange,
}) => {
  const [show, setShow] = useState(false);
  const [val, setVal] = useState("");

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setVal(e.target.value);
    onChange(e.target.value);
  };

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex justify-between items-center">
        <label className="text-xs font-semibold text-gray-400 uppercase tracking-wider flex items-center gap-1.5">
          <Key className="w-3.5 h-3.5" /> API Key
        </label>
        {hasExistingKey && !val && (
          <span className="text-xs text-green-400 font-medium">
            ✓ Securely stored in OS Keyring
          </span>
        )}
      </div>
      <div className="relative">
        <input
          type={show ? "text" : "password"}
          placeholder={
            hasExistingKey
              ? "•••••••••••••••• (Leave blank to keep existing key)"
              : "Enter API key"
          }
          value={val}
          onChange={handleChange}
          className="w-full bg-codify-bg border border-codify-border rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500 font-mono pr-10"
        />
        <button
          type="button"
          onClick={() => setShow(!show)}
          className="absolute right-2.5 top-2.5 text-gray-400 hover:text-gray-200"
          tabIndex={-1}
        >
          {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
        </button>
      </div>
    </div>
  );
};
