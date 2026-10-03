import React from "react";
import { PauseCircle } from "lucide-react";

import type { PauseReason } from "../pauseReason.ts";

/**
 * Why this goal is paused, above the buttons that resume it.
 *
 * The engine wrote the sentence (`pauseReason.ts`); this only draws it. Everything wraps and nothing is
 * truncated by the layout: the sentence ends in what the person does next, and a clipped one would end before
 * it. The critic's own reasons, when it is the critic that paused the goal, are quoted below as plain text.
 */
export const PauseBanner: React.FC<{ pause: PauseReason }> = ({ pause }) => (
  <div
    role="status"
    data-pause-code={pause.code}
    className="flex items-start gap-2 text-xs text-codify-warning-ink bg-codify-warning/20 border border-codify-warning/60 rounded-lg p-3"
  >
    <PauseCircle className="w-4 h-4 shrink-0 mt-0.5" />
    <div className="min-w-0 flex flex-col gap-1">
      <span className="font-semibold">{pause.heading}</span>
      <span className="leading-relaxed break-words">{pause.reason}</span>
      {pause.notes && (
        <div className="mt-1 flex flex-col gap-0.5">
          <span className="font-semibold">The critic's reasons</span>
          <span className="whitespace-pre-wrap break-words leading-relaxed">{pause.notes}</span>
        </div>
      )}
    </div>
  </div>
);
