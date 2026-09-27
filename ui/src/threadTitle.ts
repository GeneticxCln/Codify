/**
 * What a thread is called.
 *
 * Two rules, both small, both with a real failure behind them — which is why
 * they are here rather than inline in the two surfaces that need them.
 *
 * ## Why the placeholder is one constant
 *
 * The engine deliberately stores no title until something gives a thread one:
 * an empty string is more honest than a name nobody chose. But the tab strip
 * and the side panel both have to *say* something, and if they each reach for
 * their own string then a tab and the row above it can disagree about what the
 * same thread is called — and a person reading both at once has no way to tell
 * which of them is wrong.
 *
 * ## Why a thread is named from its first prompt
 *
 * A thread made by the "New chat" button starts with no name, and nothing else
 * was ever going to give it one. That was survivable while every thread shared
 * one tab. It is not survivable now: a tab per thread means three untitled
 * threads are three tabs all labelled "New chat", and opening one looks exactly
 * like nothing happened — which is the bug this function exists to remove.
 */
export const UNTITLED_THREAD_TITLE = "New chat";

/** Long enough to say what the thread is, short enough to fit a tab. */
export const THREAD_TITLE_LIMIT = 60;

/**
 * What a thread is *called on screen*, given the name of the folder it is in.
 *
 * ## The rule
 *
 * A named thread is called by its name. An **unnamed** one is called by its
 * **folder**. `"New chat"` is the last resort, when the thread has neither.
 *
 * ## Why the folder and not "New chat"
 *
 * Because "New chat" is not information. Three unnamed threads in three
 * projects are three tabs reading "New chat", and opening any one of them looks
 * exactly like nothing happened — the placeholder names *nothing*: not the
 * thread, not where it is, not which of the three you are looking at. The folder
 * names a real thing, and for a thread that has said nothing yet it is the only
 * real thing there is.
 *
 * It is also why nothing more than the folder name is printed. A path is long,
 * a path repeats across every tab in the same project, and a strip of tabs that
 * each spend 200px on `/home/quinton/Projects/Codify` is unreadable — the name
 * is the part of the path that differs between projects, so it is the part worth
 * printing. The full path stays in the tooltip.
 *
 * ## The comparison against the placeholder
 *
 * `UNTITLED_THREAD_TITLE` is the marker the whole naming layer uses for "no name
 * yet" — `openConversation` writes it into a tab, so it arrives here as a title.
 * Comparing against the constant rather than a literal string keeps the two
 * sides from drifting into two different spellings of "unnamed".
 */
export function threadLabel(title: string, folderName?: string): string {
  const name = (title ?? "").trim();
  if (name && name !== UNTITLED_THREAD_TITLE) return name;
  return (folderName ?? "").trim() || UNTITLED_THREAD_TITLE;
}

/**
 * A thread's name, taken from the first thing asked in it.
 *
 * ## Why the prompt is cut at a word
 *
 * A 200-character slice of a paragraph ends mid-word — "add a migration for the
 * users table so every row h" — and a tab is 224px wide, so the difference
 * between a cut at a word and a cut mid-word is the difference between a name
 * you recognise and a name you have to read twice. Whitespace is collapsed
 * first because a prompt pasted with newlines would otherwise put four words on
 * four lines in a one-line tab.
 *
 * An empty or whitespace-only prompt falls back to the placeholder rather than
 * returning an empty string: this value is about to be shown, and `""` is not a
 * name.
 */
export function threadTitleFromPrompt(
  prompt: string,
  limit: number = THREAD_TITLE_LIMIT
): string {
  const flat = prompt.replace(/\s+/g, " ").trim();
  if (!flat) return UNTITLED_THREAD_TITLE;
  if (flat.length <= limit) return flat;
  const head = flat.slice(0, limit);
  // A cut that lands exactly on a word boundary keeps the word. Asking for 12
  // characters of "Add an index to the goals table" and getting "Add an" back
  // is a name worse than the one that fitted — the last word is whole, it just
  // happens to end on the last character.
  if (flat[head.length] === " ") return head;
  // Otherwise cut back to the last space inside the limit, so the name ends on
  // a whole word. A prompt with no space at all in its first `limit` characters
  // — a base64 blob, a stack trace — has no boundary to cut at, and is cut hard.
  const lastSpace = head.lastIndexOf(" ");
  return (lastSpace > 0 ? head.slice(0, lastSpace) : head).trimEnd();
}
