/**
 * The output a terminal earns while no pane is displaying it.
 *
 * ## The problem this store exists for
 *
 * A terminal pane mounts only when its tab is active — an exclusive ternary in
 * `App.tsx` renders the browser pane, the terminal pane or the transcript, one
 * at a time. The shell behind an inactive tab never stops: `terminal.rs`'s
 * reader thread emits `terminal-output` for as long as the PTY has bytes. A
 * pane that is not mounted has no subscription, so every byte a background
 * build printed used to be emitted to nobody and dropped — gone from the live
 * pane on return and from the workspace scrollback too, because the
 * scrollback append lived in the same unmounting listener.
 *
 * The fix separates **recording** from **rendering**. Rendering stays
 * active-tab-only (one xterm per pane area, the same decision the browser pane
 * made). Recording moves up to the app: one subscription, mounted for as long
 * as the app is, filing each chunk for whichever terminal said it.
 *
 * ## The rule that makes one copy the whole design: ownership
 *
 * A chunk must be **displayed exactly once and filed exactly once**, and after
 * that the recorder must not touch it again. So the store tracks *owners*: a
 * pane claims its terminal's id on mount and releases it on unmount. The
 * recorder stores a chunk only when **nobody owns** the terminal that said it;
 * an owned terminal's chunk is the pane's business — the pane both displays it
 * and files it into the workspace scrollback, as it always did.
 *
 * A terminal with no owner falls into exactly one of three states, and each
 * has one answer:
 *
 * - **backlog** — its tab exists but its pane does not (a background tab). The
 *   chunk is held here, bounded, in order. When the pane returns it claims the
 *   id, drains the backlog, and writes it into xterm: the build finished while
 *   the user was reading the transcript, and the transcript of that build is
 *   on screen.
 * - **abandoned** — the tab is gone (`handleCloseTab` releases the owner, since
 *   the pane was never mounted to do it) but the shell kept talking until the
 *   PTY closed. Its backlog drains into the *workspace* scrollback, honouring
 *   the guarantee `terminalHistory.ts` already states: what a terminal said is
 *   part of what the next pane in this workspace restores, including a tab the
 *   user has since closed.
 * - **exit while unowned** — the shell finished in the background. Same path as
 *   abandoned — backlog into history — plus the exit marker, because a marker
 *   that only ever reached a mounted pane would leave a background shell's
 *   record looking open.
 *
 * The merge, not the append, is what keeps every byte counted once. Chunks
 * that accumulated while the pane was away are the *only* copy of those bytes
 * anywhere — the pane never saw them — so draining them into history cannot
 * double-file. Chunks the pane saw were filed by the pane and never entered
 * the backlog at all, because the owner was set. Session history and backlog
 * are therefore disjoint by construction, and the merge is a plain
 * concatenation in arrival order.
 *
 * ## Module scope, like `terminalHistory.ts`
 *
 * The whole point is outliving a component: the recorder's subscription must
 * not be keyed to any pane's mount, and the backlog must be here when a pane
 * mounts minutes later. Pure functions over module state, no React, for the
 * same reasons `terminalHistory.ts` gives.
 */

/** How much backlog one background terminal may hold, in bytes.
 *
 * Sized to `terminalHistory`'s bound: a terminal held in the background for a
 * whole session is exactly the "nobody scrolled back a megabyte" case that bound
 * was set for, and two bounds would only invite the question of which one
 * applies.
 */
export const MAX_BACKLOG_BYTES = 64 * 1024;

/**
 * The line a background shell's record gets when it finishes unseen.
 *
 * Same shape and styling as the pane's own exit marker, so a session whose
 * exit was recorded by the store reads like one whose exit was recorded by a
 * pane — the difference is where the bytes came from, not what the user sees.
 */
export const EXIT_MARKER = "\r\n\x1b[2m— process exited —\x1b[0m\r\n";

/**
 * The line a returning pane draws between what it had shown before and the
 * backlog earned while it was away.
 *
 * The backlog continues the workspace tail — everything the pane displayed is
 * already in the tail, so the backlog is strictly newer — and without a seam
 * the two read as one unbroken stream from a shell that never had a gap. The
 * wording says what actually happened, because "while you were away" is the
 * honest description of the bytes on either side of it.
 */
export const RESUME_SEAM =
  "\r\n\x1b[2m— output missed while this pane was away —\x1b[0m\r\n";

import { appendTerminalHistory, trimTail } from "./terminalHistory.ts";

interface TerminalBacklog {
  /** Chunks in arrival order; the stream, not the screen. */
  text: string;
  /** Set once the shell has finished, however the record ends. */
  exited: boolean;
}

const backlogs = new Map<string, TerminalBacklog>();
const owners = new Set<string>();

function backlogged(state: TerminalBacklog, chunk: string): TerminalBacklog {
  return { text: trimTail(state.text + chunk, MAX_BACKLOG_BYTES), exited: state.exited };
}

/**
 * Record one output chunk, or leave it for its owner.
 *
 * Returns whether the recorder kept the chunk. `false` is the common case for
 * the active tab — the pane is displaying and filing that chunk itself — and
 * the store deliberately does nothing with it: a second filer is how bytes
 * come to be on screen twice, and that failure is worse than losing one.
 *
 * The boolean is also the tab strip's unread badge event source (App.tsx
 * turns `true` into a dot on the tab): `true` is exactly "no pane displayed
 * this", which is exactly "the user did not just see it". The badge's own
 * guards — the claim window on the active tab, clearing on focus, close and
 * watched exits — live with the view, in App.
 */
export function recordOutput(id: string, data: string): boolean {
  if (!data) return false;
  if (owners.has(id)) return false;
  const state = backlogs.get(id) ?? { text: "", exited: false };
  backlogs.set(id, backlogged(state, data));
  return true;
}

/** A pane took over: drain what was earned, and stop recording from here. */
export function claimTerminal(id: string): string {
  owners.add(id);
  const state = backlogs.get(id);
  backlogs.delete(id);
  return state?.text ?? "";
}

/** A pane let go (unmount, or a tab the user closed with the pane unmounted). */
export function releaseTerminal(id: string): void {
  owners.delete(id);
}

/**
 * The shell finished while nobody owned it: file the record into the
 * workspace's scrollback and forget the terminal.
 *
 * Called by the recorder on `terminal-exit` for an unowned id, and by
 * `handleCloseTab` for a background tab whose pane never claimed it. Returns
 * what was filed, so the caller can report it; empty when the terminal was
 * owned — its pane is the filer, and an exit there is written into xterm and
 * history by the pane, not twice by two layers.
 */
export function retireTerminal(id: string, workspaceId?: string): string {
  owners.delete(id);
  const state = backlogs.get(id);
  backlogs.delete(id);
  const record = (state?.text ?? "") + (state?.exited ? EXIT_MARKER : "");
  if (record && workspaceId) {
    // Filed through the same bounded append every other writer uses, so the
    // merged record obeys one bound and gets the same line-boundary trim.
    appendTerminalHistory(workspaceId, record);
  }
  return record;
}

/**
 * The shell finished. Recorded on the terminal's own backlog entry so an exit
 * during the unowned gap is never lost — including the case where the exit is
 * the *first* thing the recorder hears, which is why an entry is created here
 * for an unowned id even with nothing buffered ahead of it.
 *
 * An owned terminal's exit is none of this store's business: its pane writes
 * the marker into xterm and files the session itself, and an entry created
 * here would sit unclaimed forever.
 */
export function recordExit(id: string): void {
  if (owners.has(id)) return;
  const state = backlogs.get(id);
  backlogs.set(id, { text: state?.text ?? "", exited: true });
}

/** Whether a pane currently owns this terminal. */
export function ownsTerminal(id: string): boolean {
  return owners.has(id);
}

/** What is still held for one terminal — the tests' window into the store. */
export function readBacklog(id: string): string {
  return backlogs.get(id)?.text ?? "";
}
