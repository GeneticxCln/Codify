/**
 * Which theme variable paints which part of the editor.
 *
 * CodeMirror paints itself, so the class-based theme layer in `index.css` never reached it. It does not need concrete
 * colours, though: its theme is CSS, and a CSS theme can say `rgb(var(--codify-bg-rgb))`, which follows a theme change with
 * no code at all (the terminal could not do that, because xterm parses its colours in JavaScript; see `terminalTheme.ts`).
 *
 * This file is only the mapping, as names, so it can be read and tested without CodeMirror and so the lazily loaded
 * `editorMount.ts` has nothing to decide. Two rules hold it together, and `ui/tests/editorTheme.test.ts` pins both:
 *
 *  - every variable here is one the theme layer manages (`MANAGED_VARS`) as an **rgb triple**, because alpha needs one;
 *  - **text** colours come only from tokens that are measured for contrast on the background: the status *inks*
 *    (`toneInk.ts` moves each tone just far enough to read as text), `primary`, `secondary` and `muted`. A syntax colour is
 *    then as legible in every theme as the status text the app already shows, and a twentieth theme needs no edit here.
 */

/** The window's own colours, by the part of the editor they paint. */
export const UI = {
  background: "--codify-bg-rgb",
  text: "--codify-primary-rgb",
  gutter: "--codify-surface-rgb",
  gutterText: "--codify-muted-rgb",
  border: "--codify-border-rgb",
  caret: "--codify-primary-rgb",
  selection: "--codify-info-rgb",
  activeLine: "--codify-primary-rgb",
  /** What the assistant changed: the info hue, the one that means "look here" on a tab, as a tint with an underline. */
  assistant: "--codify-info-rgb",
  /** A bracket and its partner. */
  match: "--codify-accent-rgb",
} as const;

/** The syntax roles, by the ink that reads as text on the editor's background. */
export const SYNTAX = {
  keyword: "--codify-accent-ink-rgb",
  string: "--codify-success-ink-rgb",
  number: "--codify-warning-ink-rgb",
  function: "--codify-info-ink-rgb",
  type: "--codify-design-ink-rgb",
  property: "--codify-knowledge-ink-rgb",
  invalid: "--codify-danger-ink-rgb",
  comment: "--codify-muted-rgb",
  punctuation: "--codify-secondary-rgb",
} as const;

/** A theme variable as a colour, with an optional alpha, in the form `index.css` writes it. */
export function rgb(variable: string, alpha?: number): string {
  return alpha === undefined ? `rgb(var(${variable}))` : `rgb(var(${variable}) / ${alpha})`;
}
