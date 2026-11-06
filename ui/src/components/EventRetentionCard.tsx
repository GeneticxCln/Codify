import React, { useEffect, useState } from "react";
import { getEngineSettings, saveEngineSettings } from "../api";
import { NumberSetting } from "./NumberSetting";

/**
 * How long a finished goal's timeline is kept (`event_retention_days`, docs/04 §8.3).
 *
 * A goal's timeline is the store's copy of the prompts, diffs and command output of the run, so it is the most
 * sensitive thing Codify keeps, and until this card the setting that deletes it had no control: it was a number only
 * a hand-written request could see or change, and it defaults to a year. The card says what is deleted, when, and
 * what is never touched, because a retention setting that does not say so is a setting nobody dares change.
 *
 * Absent on an engine that predates the setting.
 */
export const EventRetentionCard: React.FC = () => {
  const [bounds, setBounds] = useState<{ value: number; min: number; max: number } | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEngineSettings()
      .then((settings) => {
        const setting = settings?.event_retention_days;
        if (!cancelled && setting) setBounds(setting);
      })
      .catch(() => {
        // An engine without the setting has nothing to change; the card stays absent.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (bounds === null) return null;

  return (
    <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
      <div className="text-xs font-semibold text-codify-secondary">Goal timelines</div>
      <NumberSetting
        id="event-retention"
        label="Keep ended goals' timelines for (days)"
        value={bounds.value}
        min={bounds.min}
        max={bounds.max}
        save={async (days) => Number((await saveEngineSettings({ event_retention_days: days })).saved.event_retention_days)}
        saved={(days) =>
          days === 0
            ? "Saved — timelines are kept until you delete the goal."
            : `Saved — timelines of goals that ended more than ${days} days ago are deleted the next time Codify starts.`
        }
        refusal="Could not save the timeline retention setting."
      />
      <p className="text-xs text-codify-muted leading-relaxed">
        A goal&apos;s timeline holds the prompts, diffs and command output of its run. When Codify starts, the timelines of goals
        that have <span className="text-codify-secondary">ended</span> (completed, failed or cancelled) and are older than
        this are deleted. <span className="text-codify-secondary">0 keeps everything.</span> A goal that is running, paused
        or waiting for your approval is never touched, and the goal itself, its steps and its statistics stay.
      </p>
    </div>
  );
};
