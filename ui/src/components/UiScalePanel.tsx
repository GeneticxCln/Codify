import React, { useRef } from "react";
import { Maximize2 } from "lucide-react";

import { Panel } from "./ui/Panel";
import { DEFAULT_UI_SCALE, UI_SCALE_STEPS, useUiScale, writeUiScale, type UiScale } from "../uiScale";

/** `112.5` reads as `112.5%`, `125` as `125%`. */
const label = (scale: UiScale): string => `${scale}%`;

/**
 * Settings → Appearance → UI scale: how big the whole window is.
 *
 * A choice from a short list, not a slider, for the reason `uiScale.ts` gives: layout is checked at
 * these sizes, and a value between them is one nobody tested. The choice is applied the moment it is
 * made (the preview below is the window itself, already at that size), and it is the same store
 * Ctrl +, Ctrl - and Ctrl 0 write, so the two can never disagree.
 *
 * A real radiogroup, so the arrow keys the role promises are delivered: moving selects, because in a
 * single-choice control arriving at an option is choosing it.
 */
export const UiScalePanel: React.FC = () => {
  const scale = useUiScale();
  const rows = useRef<Array<HTMLButtonElement | null>>([]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    const forward = event.key === "ArrowRight" || event.key === "ArrowDown";
    const back = event.key === "ArrowLeft" || event.key === "ArrowUp";
    if (!forward && !back) return;
    event.preventDefault();
    const at = UI_SCALE_STEPS.indexOf(scale);
    const next = Math.min(UI_SCALE_STEPS.length - 1, Math.max(0, at + (forward ? 1 : -1)));
    const step = UI_SCALE_STEPS[next];
    if (step === undefined) return;
    writeUiScale(step);
    rows.current[next]?.focus();
  };

  return (
    <Panel title={<><Maximize2 className="w-3.5 h-3.5" /> UI scale</>}>
      <div className="space-y-3">
        <p className="text-xs text-codify-muted leading-relaxed">
          How big everything is: text, icons and spacing together. It applies at once and is
          remembered. <kbd className="font-mono">Ctrl +</kbd>, <kbd className="font-mono">Ctrl -</kbd>{" "}
          and <kbd className="font-mono">Ctrl 0</kbd> do the same from the keyboard.
        </p>
        <div
          role="radiogroup"
          aria-label="UI scale"
          onKeyDown={onKeyDown}
          className="flex flex-wrap gap-2"
        >
          {UI_SCALE_STEPS.map((step, i) => {
            const chosen = step === scale;
            return (
              <button
                key={step}
                type="button"
                role="radio"
                aria-checked={chosen}
                // A radiogroup is one tab stop, and the arrow keys move within it.
                tabIndex={chosen ? 0 : -1}
                ref={(el) => {
                  rows.current[i] = el;
                }}
                onClick={() => writeUiScale(step)}
                className={
                  "rounded-lg border px-3 py-1.5 text-xs font-medium cursor-pointer transition-colors " +
                  (chosen
                    ? "border-codify-border-strong bg-codify-raised text-codify-primary"
                    : "border-codify-border text-codify-secondary hover:bg-codify-raised/60")
                }
              >
                {label(step)}
                {step === DEFAULT_UI_SCALE && <span className="ml-1.5 text-2xs text-codify-muted">default</span>}
              </button>
            );
          })}
        </div>
        <div className="rounded-lg border border-codify-border bg-codify-bg p-3 space-y-1">
          <p className="text-sm text-codify-primary">The quick brown fox jumps over the lazy dog.</p>
          <p className="text-2xs text-codify-muted">
            This is the smallest text in the app, at {label(scale)}.
          </p>
        </div>
      </div>
    </Panel>
  );
};
