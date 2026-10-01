import { classifyBrowserAddress } from "./browserDispatch";

/**
 * What a link in an answer may become: the app's own browser tab, or plain text. Nothing else.
 *
 * ## Why a link is never an `<a href>`
 *
 * The main webview has no `on_navigation` guard, so a real anchor that is clicked would navigate the
 * *app itself* away to the page, taking the UI, the boot token in memory and every open tab with it.
 * There is also no system opener (`docs/09` rules it out), so there is no "open in my browser"
 * either. The one place this app opens a web page is its own browser pane, which runs every address
 * through the shell's `navigation_allowed`. So a link in an answer is a **button** that asks for
 * exactly that: `onOpenLink(url)`, which `App.tsx` turns into a browser tab through the same path a
 * page's popup request takes.
 *
 * ## What may be opened
 *
 * Only an explicit `http://` or `https://` address, and only one `classifyBrowserAddress` accepts
 * (so never a loopback or unspecified host). `[x](example.com)` is text, not a link, even though the
 * address bar would take a bare host: a model's reply is not a person typing, and "this host is a
 * website" is not something a bare word in an answer gets to claim. `javascript:`, `data:`, `file:`,
 * `mailto:`, `tauri://`, relative paths and anchors are all text. The text keeps the address in its
 * `title`, so a refused link is still visible and copyable, just not clickable.
 */
export type LinkAction =
  /** Open `url` in a browser tab. `url` is the address as `classifyBrowserAddress` re-serialised it. */
  | { kind: "open"; url: string }
  /** Show the words only. `why` is for the tooltip. */
  | { kind: "text"; why: string };

/** Longer than this is not a link a person follows; it is also the parser's own cap. */
const MAX_HREF = 2000;

/** Whitespace (`\s` covers NBSP and the Unicode line separators too) and control characters: legal in nothing a browser would be asked to open. */
const UNSAFE_CHARS = /[\s\x00-\x1f\x7f-\x9f]/;

export function linkAction(href: string): LinkAction {
  const raw = href.trim();
  if (!raw) return { kind: "text", why: "empty link" };
  if (raw.length > MAX_HREF) return { kind: "text", why: "link too long to open" };
  if (UNSAFE_CHARS.test(raw)) return { kind: "text", why: "link contains characters a web address cannot" };
  // An explicit web scheme, or it is not a web link. This is the check that keeps `javascript:`,
  // `data:` and `file:` out, and it is stricter than the address bar on purpose (see above).
  if (!/^https?:\/\//i.test(raw)) return { kind: "text", why: "not an http(s) link" };
  const classified = classifyBrowserAddress(raw, "markdown-link");
  if (classified.kind !== "shell") return { kind: "text", why: classified.reason };
  return { kind: "open", url: classified.url };
}
