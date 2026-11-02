import React, { useEffect, useState } from "react";

import { getEngineSettings, saveEngineSettings } from "../api";
import { readRejection } from "../rejection.ts";
import { Checkbox } from "./ui/Checkbox";

/**
 * `laya_required`: whether an unavailable gate refuses the request.
 *
 * Off by default. A fresh install with no gate model is a deliberate skip, not a failure, and
 * locking every turn behind a model nobody configured would be worse than running unguarded.
 * The switch is for a person who has a gate and wants "not checked" to mean "not answered".
 */
export const LayaRequiredToggle: React.FC = () => {
  const [required, setRequired] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEngineSettings()
      .then((settings) => {
        if (cancelled || !settings?.laya_required) return;
        setRequired(settings.laya_required.value === 1);
      })
      .catch(() => {
        // An engine that predates the setting gets no toggle, not one whose save is refused.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (required === null) return null;

  const save = async (next: boolean) => {
    const previous = required;
    setRequired(next);
    setSaving(true);
    setMessage(null);
    try {
      const result = await saveEngineSettings({ laya_required: next });
      setRequired(Number(result.saved.laya_required) === 1);
    } catch (error: unknown) {
      setRequired(previous);
      setMessage(readRejection(error, "Could not change the gate requirement."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-1.5 border-t border-codify-border pt-2">
      <Checkbox checked={required} disabled={saving} onChange={(next) => void save(next)}>
        Require the pre-flight gate — refuse a turn it cannot check
      </Checkbox>
      <p className="text-xs text-codify-muted leading-relaxed">
        Off by default. When on, a gate that is configured but cannot answer refuses the turn or
        goal instead of running unguarded. A gate nobody configured remains a deliberate skip, so
        a fresh install is not locked out.
      </p>
      {message && <p className="text-xs text-codify-danger">{message}</p>}
    </div>
  );
};
