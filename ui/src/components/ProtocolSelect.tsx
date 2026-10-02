import React from "react";
import type { ProviderProtocol } from "../types.ts";
import { PROTOCOL_LABELS } from "../providerLabels.ts";

interface ProtocolSelectProps {
  value: ProviderProtocol;
  onChange: (val: ProviderProtocol) => void;
}

export const ProtocolSelect: React.FC<ProtocolSelectProps> = ({ value, onChange }) => {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-xs font-semibold text-codify-muted uppercase tracking-wider">
        Protocol
      </label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as ProviderProtocol)}
        className="bg-codify-bg border border-codify-border rounded px-3 py-2 text-sm text-codify-secondary focus:outline-none focus:border-codify-accent"
      >
        {(Object.keys(PROTOCOL_LABELS) as ProviderProtocol[]).map((p) => (
          <option key={p} value={p}>
            {PROTOCOL_LABELS[p].long}
          </option>
        ))}
      </select>
    </div>
  );
};
