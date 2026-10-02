import { createContext, useContext } from "react";
import type { ClipSource } from "./clipboardHistory";

/**
 * How a Copy button tells the history what it copied.
 *
 * `navigator.clipboard.writeText` fires no `copy` event, so a button that copies through it is invisible to the
 * listeners and has to say so. It is a context rather than a prop so the Markdown renderer, which draws the
 * code blocks, is not given another argument by every place that renders an answer; outside a provider (a test,
 * a card drawn on its own) it does nothing, and the button still copies.
 */
export type ClipRecorder = (text: string, source: ClipSource) => void;

export const ClipboardRecorderContext = createContext<ClipRecorder>(() => {});

export function useClipboardRecorder(): ClipRecorder {
  return useContext(ClipboardRecorderContext);
}
