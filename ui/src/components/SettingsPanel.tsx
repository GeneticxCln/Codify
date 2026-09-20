import React from "react";
import { AgentRole } from "../types";
import { AgentConfigCard } from "./AgentConfigCard";
import { Sliders, ShieldCheck } from "lucide-react";

const ORDERED_ROLES: AgentRole[] = ["planner", "coder", "tester", "reviewer", "summarizer"];

export const SettingsPanel: React.FC = () => {
  return (
    <div className="max-w-5xl mx-auto py-6 px-4 flex flex-col gap-6">
      <div className="flex flex-col gap-1 border-b border-[#30363d] pb-4">
        <h2 className="text-xl font-bold text-gray-100 flex items-center gap-2">
          <Sliders className="w-5 h-5 text-blue-400" />
          Sub-Agent Configuration
        </h2>
        <p className="text-sm text-gray-400 flex items-center gap-2">
          <ShieldCheck className="w-4 h-4 text-green-400" />
          Settings is the exclusive mutator for sub-agent models and credentials. All API keys are securely persisted into your local OS Keyring.
        </p>
      </div>

      <div className="flex flex-col gap-6">
        {ORDERED_ROLES.map((role) => (
          <AgentConfigCard key={role} role={role} />
        ))}
      </div>
    </div>
  );
};
