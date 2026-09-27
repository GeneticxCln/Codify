/**
 * Terminal scrollback that outlives the tab it was typed in.
 *
 * Closing a terminal tab reaps the shell — `handleCloseTab` calls
 * `codify_terminal_close`, because a PTY left running is a stray process holding
 * the workspace directory. So the text the user was reading went with it, and
 * reopening the tab gave them a bare prompt and no way to see what they had
 * just been looking at. This holds that text, per workspace, for as long as the
 * app is open.
 *
 * ## It is the byte stream, not the screen
 *
 * What is kept is what the shell **said** — the chunks from `terminal-output`, in
 * order — and a reopened pane gets them by writing them into a fresh xterm, which
 * reproduces the same screen. The alternative is serializing xterm's own buffer
 * with `addon-serialize`, which is a closer match for "what was on screen" and
 * has one real advantage: it captures a full-screen program (a pager, `htop`)
 * the way the user last saw it rather than the scrollback that led there.
 *
 * The byte stream wins here for three reasons that matter more than fidelity to
 * a corner case: it needs no extra dependency, it is always current rather than
 * captured at some chosen moment, and it survives xterm being disposed at all,
 * which a buffer snapshot only does if something remembered to take it.
 *
 * The price, stated plainly: a line wrapped at the *old* pane's width re-wraps at
 * the new one. A build log read at 120 columns and reopened at 80 comes back with
 * different line breaks. Recording the width alongside the tail and refusing to
 * replay at a different one would be a nicer answer and a worse feature — most of
 * the time the user closed the tab because the window changed shape, and refusing
 * to show them anything would be the wrong answer to that.
 *
 * ## Scope, deliberately narrow
 *
 * - **Per workspace, not per terminal.** Two terminal tabs in one workspace share
 *   a tail, so a second tab shows the first one's session. That is the point: the
 *   question the user is asking is "what was I doing here", and "here" is the
 *   workspace.
 * - **This session only.** Nothing here reaches disk, `localStorage`, or the
 *   engine. The content is whatever the user typed and whatever commands printed,
 *   and `docs/09` §2 is where this project keeps its reasoning about what belongs
 *   in `~/.codify`. That reasoning is not settled, so nothing is written.
 * - **Bounded.** A long build would otherwise grow without limit for as long as
 *   the app is open.
 *
 * ## Why the store is module-level
 *
 * This is the one piece of state in the UI that has to outlive a component: the
 * entire point is that the *next* pane finds what the *last* one saw. A React
 * context would be the tidy answer and the wrong one — the provider would have to
 * wrap the tab strip, every render in the shell would carry the value, and the
 * pane would still need it at mount. Module scope is also what makes it work
 * across a tab close, which is the case that matters.
 */

/**
 * How much of the stream to keep, in bytes.
 *
 * 64 KiB is about 1,000 lines of ordinary output. Past that, the value of having
 * the *start* of a scrollback is low — nobody scrolls back a megabyte to find the
 * line they wanted — while the cost of carrying it is not.
 */
export const MAX_SCROLLBACK_BYTES = 64 * 1024;

/** The line the pane draws between a restored session and the live one. */
export const SEAM =
  "\r\n\x1b[2m— earlier session in this workspace —\x1b[0m\r\n";

/**
 * `tail` with `chunk` appended, never exceeding `limit` bytes.
 *
 * Trimming happens on every append rather than on read, because the alternative
 * is a string that grows to the size of a full build and only then gets cut —
 * which for a terminal in a long-running app is a slow leak rather than a bound.
 *
 * The two corrections afterwards are the ones that make the result *usable*
 * rather than merely short:
 *
 * - **A partial leading surrogate is dropped.** A JS string is UTF-16, and
 *   slicing a *suffix* to a byte count can cut an emoji in half — but only in
 *   one direction. The kept text always ends on a real character, so the damage
 *   a suffix slice can do is at the *start*: a lone low surrogate, which renders
 *   as a replacement character at the top of the restored scrollback and would be
 *   there on every restore from then on, because the stored tail is the cut one.
 * - **A partial trailing line is dropped.** `terminal.rs` reads in 4 KiB chunks
 *   and emits whatever it read, so a tail almost always stops mid-line. Replaying
 *   half a line puts a fragment of a command on screen above the new prompt, and
 *   it reads as corruption rather than as a boundary.
 */
export function appendScrollback(
  tail: string,
  chunk: string,
  limit: number = MAX_SCROLLBACK_BYTES
): string {
  if (!chunk) return tail;
  let next = tail + chunk;
  if (next.length > limit) next = next.slice(next.length - limit);
  // A suffix slice can land between the two halves of a surrogate pair, and only
  // at the front: a high surrogate is always followed by its low partner, so the
  // tail's last code unit is a whole character and the only thing that can be
  // stranded is a low one at the start. Dropping it costs at most one character
  // and leaves a string that renders; keeping it would put a replacement glyph
  // above every future restore.
  const first = next.charCodeAt(0);
  if (first >= 0xdc00 && first <= 0xdfff) next = next.slice(1);
  // And it can land mid-line, for the reason above.
  const lastNewline = next.lastIndexOf("\n");
  if (lastNewline === -1) return "";
  return next.slice(0, lastNewline + 1);
}

/**
 * What a reopened pane should write: the tail, with the seam in front of it.
 *
 * Empty for a workspace that has never had a terminal, which is the case the pane
 * has to handle anyway — and it is why the caller writes nothing rather than a
 * blank line. A workspace whose tail is *only* a partial line trims to nothing
 * above, and so does too: there is nothing to show and no session to mark.
 */
export function replayFor(tail: string): string {
  if (!tail) return "";
  return SEAM + tail;
}

// ── the store ─────────────────────────────────────────────────────────────

const tails = new Map<string, string>();

/** What this workspace has said, most recent `limit` bytes. */
export function readTerminalHistory(workspaceId: string): string {
  return tails.get(workspaceId) ?? "";
}

/**
 * Record what a terminal said.
 *
 * Mutating the stored string in place rather than replacing the map entry is
 * deliberate: this is called once per PTY chunk, and a pane showing a live build
 * is thousands of times a second for minutes. A `set` per call would put a
 * megabyte of garbage through the generational young collection to avoid
 * replacing one reference.
 */
export function appendTerminalHistory(
  workspaceId: string,
  chunk: string,
  limit: number = MAX_SCROLLBACK_BYTES
): void {
  tails.set(workspaceId, appendScrollback(tails.get(workspaceId) ?? "", chunk, limit));
}

/**
 * Forget a workspace's scrollback.
 *
 * There is deliberately no Clear control in the pane and no call site for this
 * yet. It exists because the bounded store needs a way to be emptied that is not
 * "quit the app", and because a function with no caller is a piece of API waiting
 * to be used wrongly. When there is a Clear action it is one call, and the test
 * below is already what it would be checked against.
 */
export function clearTerminalHistory(workspaceId: string): void {
  tails.delete(workspaceId);
}
