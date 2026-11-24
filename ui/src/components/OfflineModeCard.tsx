import React, { useEffect, useState } from "react";
import { WifiOff } from "lucide-react";

import { getEngineSettings, saveEngineSettings } from "../api";
import { readRejection } from "../rejection.ts";
import { Checkbox } from "./ui/Checkbox";

interface OfflineModeCardProps {
  /** Refresh discovered models after switching offline mode so remote providers drop or return right away. */
  onChanged?: () => void;
}

/**
 * One switch that keeps Codify strictly local: restricts chat and agent roles to loopback endpoints
 * and turns off web fetch, remote MCP, update checks, GitHub CI suggestions, and remote provider pings.
 */
export const OfflineModeCard: React.FC<OfflineModeCardProps> = ({ onChanged }) => {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEngineSettings()
      .then((settings) => {
        if (!cancelled && settings?.offline_mode) {
          setEnabled(settings.offline_mode.value === 1);
        }
      })
      .catch(() => {
        // Older engine without offline_mode: keep the card hidden rather than saving blind.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (enabled === null) return null;

  const save = async (next: boolean) => {
    const previous = enabled;
    setEnabled(next);
    setSaving(true);
    setMessage(null);
    try {
      const result = await saveEngineSettings({ offline_mode: next });
      const active = Number(result.saved.offline_mode) === 1;
      setEnabled(active);
      onChanged?.();
      setMessage(
        active
          ? "Offline mode is on — only local models run, and outbound checks are paused."
          : "Offline mode is off — remote providers and configured outbound checks are available again.",
      );
    } catch (error: unknown) {
      setEnabled(previous);
      setMessage(readRejection(error, "Could not update offline mode."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
      <div className="flex items-center gap-2 text-xs font-semibold text-codify-secondary">
        <WifiOff className="w-3.5 h-3.5 text-codify-warning" />
        Offline mode
      </div>
      <Checkbox checked={enabled} disabled={saving} onChange={(next) => void save(next)}>
        Keep everything local (disable cloud models and outbound checks)
      </Checkbox>
      <p className="text-xs text-codify-muted leading-relaxed">
        When enabled, chat and agent roles may only call local loopback models, and web fetch, remote MCP
        servers, app update checks, CI inbox checks, and remote provider health pings are all turned off
        together.
      </p>
      {message && (
        <p className="text-xs text-codify-muted" role="status">
          {message}
        </p>
      )}
    </div>
  );
};
