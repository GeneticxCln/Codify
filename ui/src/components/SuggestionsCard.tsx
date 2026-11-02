import React, { useEffect, useState } from "react";
import { getEngineSettings, saveEngineSettings } from "../api";
import { readRejection } from "../rejection.ts";
import { Checkbox } from "./ui/Checkbox";

/**
 * The inbox's switches (docs/09 §10.22, docs/02 §3.0a): whether the engine looks for things worth a person's
 * attention, and whether it may also ask GitHub about a red CI run.
 *
 * The second is its own switch, off by default, with its network behaviour written beside it: of the four detectors
 * it is the only one that contacts another machine without a button being pressed. The card is absent on an engine
 * that predates the settings.
 */
export const SuggestionsCard: React.FC = () => {
  const [looking, setLooking] = useState<boolean | null>(null);
  const [ci, setCi] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEngineSettings()
      .then((settings) => {
        if (cancelled || !settings?.suggestions) return;
        setLooking(settings.suggestions.value === 1);
        setCi(settings.suggest_ci?.value === 1);
      })
      .catch(() => {
        // An engine without the settings has nothing to switch; the card stays absent.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (looking === null) return null;

  const save = async (patch: { suggestions?: boolean; suggest_ci?: boolean }) => {
    const before = { looking, ci };
    if (patch.suggestions !== undefined) setLooking(patch.suggestions);
    if (patch.suggest_ci !== undefined) setCi(patch.suggest_ci);
    setSaving(true);
    setMessage(null);
    try {
      const result = await saveEngineSettings(patch);
      setLooking(Number(result.saved.suggestions ?? (before.looking ? 1 : 0)) === 1);
      setCi(Number(result.saved.suggest_ci ?? (before.ci ? 1 : 0)) === 1);
      setMessage("Saved.");
    } catch (error: unknown) {
      setLooking(before.looking);
      setCi(before.ci);
      setMessage(readRejection(error, "Could not change the suggestion settings."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
      <div className="text-xs font-semibold text-codify-secondary">Suggestions</div>
      <Checkbox checked={looking} disabled={saving} onChange={(next) => void save({ suggestions: next })}>
        Let Codify point out things worth your attention
      </Checkbox>
      <p className="text-xs text-codify-muted leading-relaxed">
        On by default. It notices a command that keeps failing, a goal paused for hours and a health review that is
        due, and lists them in Notifications. It reads what Codify already stores, makes no model call and never starts
        anything: a suggestion only does something when you press Send to chat.
      </p>
      <Checkbox checked={ci} disabled={saving || !looking} onChange={(next) => void save({ suggest_ci: next })}>
        Also check GitHub for a failing CI run
      </Checkbox>
      <p className="text-xs text-codify-muted leading-relaxed">
        Off by default, because this one contacts another machine on its own. When on, Codify asks api.github.com for the
        checks on your project&apos;s current commit, using the repository named by its own <code>origin</code>. It sends no
        credential, asks at most every 30 minutes, and only for a project you used that day.
      </p>
      {message && <p className="text-xs text-codify-muted" role="status">{message}</p>}
    </div>
  );
};
