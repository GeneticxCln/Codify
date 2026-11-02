import React, { useEffect, useState } from "react";
import { getEngineSettings, saveEngineSettings } from "../api";
import { readRejection } from "../rejection.ts";
import { Checkbox } from "./ui/Checkbox";

/**
 * Deep review (docs/21 P1): two fresh-context readers look at each step before the critic decides.
 *
 * It spends extra model calls on every reviewed step, so it is a person's switch and nothing a goal, a skill or the
 * assistant can turn on. The card is absent on an engine that predates the setting.
 */
export const DeepReviewCard: React.FC = () => {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEngineSettings()
      .then((settings) => {
        if (!cancelled && settings?.review_deep) setEnabled(settings.review_deep.value === 1);
      })
      .catch(() => {
        // An engine without the setting has nothing to switch; the card stays absent.
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
      const result = await saveEngineSettings({ review_deep: next });
      setEnabled(Number(result.saved.review_deep) === 1);
      setMessage("Saved — this applies to the next reviewed step.");
    } catch (error: unknown) {
      setEnabled(previous);
      setMessage(readRejection(error, "Could not change deep review."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
      <div className="text-xs font-semibold text-codify-secondary">Deep review</div>
      <Checkbox checked={enabled} disabled={saving} onChange={(next) => void save(next)}>
        Have two fresh readers review each step before the critic decides
      </Checkbox>
      <p className="text-xs text-codify-muted leading-relaxed">
        Off by default. Each reader sees only the request, the step and the diff, never the research the author
        used, and the critic still makes the decision. It costs about two extra model calls per reviewed step.
      </p>
      {message && <p className="text-xs text-codify-muted" role="status">{message}</p>}
    </div>
  );
};
