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
 * `tail` cut to at most `limit` bytes, whole characters only.
 *
 * A JS string is UTF-16, and slicing a *suffix* to a byte count can cut an emoji
 * in half — but only in one direction. The kept text always ends on a real
 * character, so the damage a suffix slice can do is at the *start*: a lone low
 * surrogate, which renders as a replacement character at the top of the restored
 * scrollback and would be there on every restore from then on, because the
 * stored tail is the cut one. Dropping it costs at most one character and leaves
 * a string that renders; keeping it would put a replacement glyph above every
 * future restore.
 *
 * Deliberately no line trimming here: the backlog a terminal earns while no pane
 * displays it (`terminalBuffer.ts`) is replayed for *display*, and a shell's
 * warm-up is a prompt with no newline in it — trimming to whole lines would
 * swallow exactly the bytes the pane is waiting for. The workspace record trims
 * its trailing partial line at **replay** rather than here, for the same reason:
 * see `replayFor`.
 */
export function trimTail(tail: string, limit: number = MAX_SCROLLBACK_BYTES): string {
  if (tail.length <= limit) return tail;
  const next = tail.slice(tail.length - limit);
  const first = next.charCodeAt(0);
  if (first >= 0xdc00 && first <= 0xdfff) return next.slice(1);
  return next;
}

/**
 * `tail` with `chunk` appended, never exceeding `limit` bytes.
 *
 * Trimming happens on every append rather than on read, because the alternative
 * is a string that grows to the size of a full build and only then gets cut —
 * which for a terminal in a long-running app is a slow leak rather than a bound.
 *
 * The bound and the surrogate repair live in `trimTail`, and that is all this
 * does: the record keeps every byte it is given, **including a trailing line with
 * no newline in it**. It used to drop that line here, which read as tidiness and
 * was the opposite — a shell's prompt has no newline, so every session's record
 * ended one line short of the truth, and those bytes were gone from every store
 * the app has. A pane that went away and came back restored the build and not
 * the prompt under it. The half-line is a *display* problem, and `replayFor` is
 * where it is solved.
 */
export function appendScrollback(
  tail: string,
  chunk: string,
  limit: number = MAX_SCROLLBACK_BYTES
): string {
  if (!chunk) return tail;
  return trimTail(tail + chunk, limit);
}

/**
 * What a reopened pane should write: the tail, with the seam in front of it.
 *
 * A partial trailing line is **not** replayed. `terminal.rs` emits what it has
 * read in batches (a few milliseconds' worth, see `BATCH_WINDOW` there) and a
 * batch ends wherever the output happened to be, so a record almost always stops mid-line;
 * putting half a command on screen above the new prompt reads as corruption
 * rather than as a boundary. The bytes stay in the record — they are this
 * session's, and the next line that arrives completes them — and it is the
 * showing that trims, not the keeping.
 *
 * Empty for a workspace that has never had a terminal, and empty for one whose
 * record is *only* a partial line, which is the case the pane has to handle
 * anyway — and why the caller writes nothing rather than a blank line.
 */
export function replayFor(tail: string): string {
  if (!tail) return "";
  const lastNewline = tail.lastIndexOf("\n");
  if (lastNewline === -1) return "";
  const complete = tail.slice(0, lastNewline + 1);
  return SEAM + complete;
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
 * Each call replaces the stored string (JavaScript strings are immutable, so there is no
 * appending in place to be had): it builds `tail + chunk` and trims it to `limit`, which costs up to
 * about `limit` bytes of copying per call, whatever the size of the chunk. What keeps that cheap
 * is how often this is called, so the number to watch is calls per second, and `terminal.rs`
 * is what bounds it (about 125 batches a second at most; before it batched, one call per 4 KiB
 * the PTY read, measured at 135 to 2,400 a second on a full-screen colour repaint).
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
