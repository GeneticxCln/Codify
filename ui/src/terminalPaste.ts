/**
 * Pasting text into a terminal from outside it: the clipboard drawer's "Paste into the terminal".
 *
 * The pane owns its xterm, so the drawer cannot reach it directly; the pane registers a function here for as
 * long as it is mounted, and the drawer asks by terminal id. The registry is module state for the reason the
 * terminal store (`terminalBuffer.ts`) is: it must outlive any one render, and two windows never share it.
 *
 * What goes in goes through xterm's own `paste`, which honours the shell's bracketed-paste mode, the same
 * path a person's Ctrl+Shift+V takes. A shell that is not in that mode treats every newline as Enter, so a
 * multi-line clip would run line by line before anyone read it: that is refused, with the reason, rather than
 * pasted. The history holds whatever was copied from anywhere (a page, a model's answer), which is the case
 * the rule is for.
 */

export type PasteOutcome = "pasted" | "needs-bracketed-paste" | "exited" | "no-terminal";

export const PASTE_MESSAGES: Readonly<Record<Exclude<PasteOutcome, "pasted">, string>> = {
  "needs-bracketed-paste":
    "Not pasted: it has more than one line, and this shell is not holding pasted text back, so each line would run as it arrived.",
  exited: "Not pasted: that shell has exited.",
  "no-terminal": "Open a terminal tab to paste into it.",
};

/** The part of xterm's `Terminal` this needs, so the rule has a test that does not load a renderer. */
export interface PasteSurface {
  paste(text: string): void;
  focus(): void;
  readonly modes: { readonly bracketedPasteMode: boolean };
}

/** Any newline, a trailing one included: that one is the Enter that runs the command. */
const HAS_NEWLINE = /[\r\n]/;

/**
 * Strip bracketed-paste breakouts (`\x1b[200~`, `\x1b[201~`, C1 `\x9b200~`, `\x9b201~`) and raw C0/C1 control
 * characters other than Tab/LF/CR so untrusted clipboard text cannot escape bracketed-paste mode or execute
 * control sequences directly in the host shell.
 */
// eslint-disable-next-line no-control-regex -- intentionally stripping terminal escape/control sequences from pasted clipboard text
const UNSAFE_PASTE_CONTROLS = /(?:\x1b\[20[01]~|\x9b20[01]~|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f])/g;

export function pasteInto(surface: PasteSurface, text: string, exited: boolean): PasteOutcome {
  if (exited) return "exited";
  if (HAS_NEWLINE.test(text) && !surface.modes.bracketedPasteMode) return "needs-bracketed-paste";
  const safe = text.replace(UNSAFE_PASTE_CONTROLS, "");
  surface.paste(safe);
  surface.focus();
  return "pasted";
}

type Paster = (text: string) => PasteOutcome;
const pasters = new Map<string, Paster>();

/**
 * Register a pane's paste for its terminal; returns the function that takes it back. Taking it back removes
 * only this registration: a re-keyed tab mounts the new pane before the old one's cleanup runs, and the old
 * one leaving must not unregister the new one.
 */
export function registerTerminalPaste(terminalId: string, paster: Paster): () => void {
  pasters.set(terminalId, paster);
  return () => {
    if (pasters.get(terminalId) === paster) pasters.delete(terminalId);
  };
}

export function pasteIntoTerminal(terminalId: string, text: string): PasteOutcome {
  return pasters.get(terminalId)?.(text) ?? "no-terminal";
}
