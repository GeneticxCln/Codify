import React, { useState } from "react";
import { readRejection } from "../rejection.ts";

/**
 * One whole-number engine setting: a field, a Save, and what the engine actually stored.
 *
 * The engine clamps a number into the setting's band, so the field never claims the value it asked for: after a save
 * it shows what came back and says when that is not what was typed. The same shape the recording-retention field in
 * Settings has, as a component, so a setting that needs a number does not need another copy of the draft, the busy
 * flag and the message.
 */
export interface NumberSettingProps {
  /** The input's id, which the label points at. */
  id: string;
  label: string;
  /** What the engine holds now. */
  value: number;
  /** The engine's band for it, which it clamps into. */
  min: number;
  max: number;
  disabled?: boolean;
  /** Save a value and answer the one the engine stored. */
  save: (value: number) => Promise<number>;
  /** What to say when the engine stored exactly what was asked. */
  saved: (stored: number) => string;
  /** What to say when the save was refused and the engine gave no sentence of its own. */
  refusal: string;
}

export const NumberSetting: React.FC<NumberSettingProps> = ({
  id,
  label,
  value,
  min,
  max,
  disabled = false,
  save,
  saved,
  refusal,
}) => {
  const [stored, setStored] = useState(value);
  const [draft, setDraft] = useState(String(value));
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  const submit = async () => {
    if (!/^\d+$/.test(draft.trim())) {
      setMessage({ ok: false, text: `Enter a whole number from ${min} to ${max}.` });
      return;
    }
    const asked = parseInt(draft, 10);
    setSaving(true);
    setMessage(null);
    try {
      const now = await save(asked);
      setStored(now);
      setDraft(String(now));
      setMessage(
        now === asked
          ? { ok: true, text: saved(now) }
          : { ok: true, text: `Clamped to ${now} (allowed ${min}–${max}).` },
      );
    } catch (error: unknown) {
      setMessage({ ok: false, text: readRejection(error, refusal) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <label htmlFor={id} className="text-xs text-codify-secondary font-medium">
        {label}
      </label>
      <input
        id={id}
        type="number"
        min={min}
        max={max}
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value);
          setMessage(null);
        }}
        disabled={disabled || saving}
        className="w-20 bg-codify-surface border border-codify-border rounded-lg px-2.5 py-1.5 text-xs text-codify-secondary focus:outline-hidden focus:border-codify-accent font-mono disabled:opacity-40"
      />
      <button
        type="button"
        onClick={() => void submit()}
        disabled={disabled || saving || draft === String(stored)}
        aria-label={`Save ${label}`}
        className="px-3 py-1.5 bg-codify-accent text-codify-bg hover:brightness-110 text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
      >
        {saving ? "Saving..." : "Save"}
      </button>
      {message && (
        <span className={`text-xs ${message.ok ? "text-codify-success" : "text-codify-danger"}`} role="status">
          {message.text}
        </span>
      )}
    </div>
  );
};
