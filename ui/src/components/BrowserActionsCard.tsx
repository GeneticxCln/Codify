import React, { useEffect, useState } from "react";
import { getEngineSettings, saveEngineSettings } from "../api";
import { readRejection } from "../rejection.ts";

/** Explicit consent for conductor actions in the user's embedded browser, independent from page reading/fetching. */
export const BrowserActionsCard: React.FC = () => {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEngineSettings()
      .then((settings) => {
        if (!cancelled && settings?.browser_actions) {
          setEnabled(settings.browser_actions.value === 1);
        }
      })
      .catch(() => {
        // No setting from an older engine means no permission control to use; actions remain unavailable.
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
      const result = await saveEngineSettings({ browser_actions: next });
      setEnabled(Number(result.saved.browser_actions) === 1);
      setMessage("Saved — this applies to the next turn.");
    } catch (error: unknown) {
      setEnabled(previous);
      setMessage(readRejection(error, "Could not change browser actions."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
      <div className="text-xs font-semibold text-codify-secondary">Browser actions</div>
      <label className="flex items-center gap-2 text-xs text-codify-secondary font-medium">
        <input
          type="checkbox"
          checked={enabled}
          disabled={saving}
          onChange={(event) => void save(event.target.checked)}
          className="accent-codify-accent"
        />
        Allow the assistant to navigate, click and type in browser pages
      </label>
      <p className="text-xs text-codify-warning leading-relaxed">
        Off by default. When enabled, a page can send text as you type, and clicking can submit it or act in a
        logged-in account. This is separate from reading the open page and from Web pages access.
      </p>
      {message && <p className="text-xs text-codify-muted" role="status">{message}</p>}
    </div>
  );
};
