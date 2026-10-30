import React from "react";
import type { ProviderProtocol } from "../types.ts";
import { PROTOCOL_LABELS } from "../providerLabels.ts";
import { SelectMenu } from "./SelectMenu";

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
      <SelectMenu
        label="Protocol"
        value={value}
        options={(Object.keys(PROTOCOL_LABELS) as ProviderProtocol[]).map((p) => ({
          value: p,
          label: PROTOCOL_LABELS[p].long,
        }))}
        onChange={(next) => onChange(next as ProviderProtocol)}
      />
    </div>
  );
};
