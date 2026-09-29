/**
 * A browser tab's address history: where it has been, and where it is.
 *
 * A pure module for the reason `tabs.ts` and `shortcuts.ts` are pure. Back and
 * forward are a stack with a cursor, and every interesting bug in a stack is a
 * decision about an edge — re-visiting the current address, visiting something
 * new after going back, pressing back at the start. Markup cannot show any of
 * them, so they live here where `node --test` can reach them directly.
 *
 * The shell has no back/forward. Tauri v2 exposes `navigate` and `reload` on a
 * webview and nothing else — no `go_back`, no `can_go_back` (checked against
 * tauri 2.11.6) — so the webview's own history is not reachable and the app
 * keeps its own. That is the whole reason this file exists rather than a
 * two-line `navigate(previousUrl)`.
 *
 * ## What this module is not
 *
 * It is not the authority on what may be navigated to. `browser.rs` is: the
 * loopback guard lives there, in Rust, and it is the same guard whether the
 * navigation came from here, from a redirect, or from the page itself. This
 * module's only opinion is cosmetic — that a bare `example.com` should reach
 * the shell as `https://example.com` rather than as a string Rust cannot parse.
 * Everything else, including every refusal, arrives as an error from the shell
 * and is shown to the user as the shell worded it. A second implementation of
 * the guard here would be a second thing to keep true, and it would be the one
 * that rots, because nothing would test it against the Rust original.
 */

/** Where a browser tab has been, and where in that it now is. */
export interface BrowserHistory {
  /** Every address this tab has been sent to, oldest first. */
  entries: string[];
  /**
   * The cursor into `entries`. `-1` before anything has been visited, so the
   * bounds read the same for a tab that has never navigated as for one sitting
   * on its first entry.
   */
  index: number;
}

/**
 * A tab that has not been sent anywhere yet.
 *
 * `index: -1` rather than `0` on an empty list, so one rule covers a tab that
 * has never navigated and a tab sitting on its first entry: `canGoBack` is
 * `index > 0` and `canGoForward` is `index < entries.length - 1`. With `0` the
 * second test would be `-1 < 0`, which reads as a cursor pointing before the
 * start.
 */
export function emptyHistory(): BrowserHistory {
  return { entries: [], index: -1 };
}

/** The address on screen, or `""` for a tab that has never navigated. */
export function currentUrl(history: BrowserHistory): string {
  return history.entries[history.index] ?? "";
}

export function canGoBack(history: BrowserHistory): boolean {
  return history.index > 0;
}

export function canGoForward(history: BrowserHistory): boolean {
  return history.index >= 0 && history.index < history.entries.length - 1;
}

/**
 * Go somewhere new.
 *
 * A new address truncates everything ahead of the cursor, which is the rule
 * that makes forward mean "somewhere I have been since" rather than "a second
 * timeline that accumulates forever". Going back and then typing a new address
 * is a change of mind, and a browser that kept the abandoned branch would let
 * forward walk into a page the user had already decided against.
 *
 * Re-visiting the address already on screen is *not* a new entry, and that is
 * what makes Reload — which is a navigate to the current URL, because the shell
 * has no reload command — leave the history alone. Without it, every reload
 * pushed an entry and back/forward became a way to re-reload the same page.
 */
export function visit(history: BrowserHistory, url: string): BrowserHistory {
  if (url === currentUrl(history)) return history;
  return {
    entries: [...history.entries.slice(0, history.index + 1), url],
    index: history.index + 1,
  };
}

/** One step back, or `null` when there is nowhere to go. */
export function goBack(
  history: BrowserHistory
): { history: BrowserHistory; url: string } | null {
  if (!canGoBack(history)) return null;
  const index = history.index - 1;
  return { history: { ...history, index }, url: history.entries[index] };
}

/** One step forward, or `null` at the newest address. */
export function goForward(
  history: BrowserHistory
): { history: BrowserHistory; url: string } | null {
  if (!canGoForward(history)) return null;
  const index = history.index + 1;
  return { history: { ...history, index }, url: history.entries[index] };
}

/** One address the shell is currently loading, per tab, before it lands. */
export type InFlightNavigations = Record<string, string>;

/**
 * A page announced an address. Is it a new place the user went?
 *
 * The stack was only ever fed by navigations the UI *asked for* — the address
 * bar, Back, Forward, a restored tab — so a link the user clicked inside a page
 * was a place they had been and the stack had never heard of it. Back then had
 * nothing to offer but the last address they typed, which is the shape of the
 * complaint this function answers: search, click through to a site, press Back,
 * land on the search rather than where the click came from.
 *
 * So every address a page announces is offered here, and the only question is
 * whether **the shell asked for this exact one**:
 *
 * - It did (`commands[tabId] === url`): the load event is the answer to a
 *   navigation the caller already recorded through [`visit`]. Recording it
 *   twice would put the same address on the stack twice, and the command is
 *   forgotten so a later `Started` for the same URL cannot match a spent one.
 * - It did not: the page went somewhere on its own — a link, a form, a
 *   `location.assign` — and that is a visit, recorded like any other.
 *
 * A **redirect** is the cost, named rather than hidden: its target does not
 * match the command, so it becomes an entry of its own and Back steps through
 * it. The alternative — matching on "some command is in flight" — makes Back
 * walk into the place the command is *leaving*, which is the bug this file
 * exists to avoid, and the cost here is one extra Back press in a case a user
 * rarely reaches on purpose. What still is not caught at all is a page that
 * changes its own address without loading a document (`pushState` on a
 * single-page app — a video page on a site built that way); the load events
 * carry no such navigation, and a script in the page would be the only way to
 * see it.
 *
 * A command for the tab is dropped either way. A page-initiated navigation
 * supersedes whatever the shell was loading, and leaving a spent command in the
 * map would let the *next* `Started` for that same address look like an answer
 * to it.
 */
export function pageNavigation(
  commands: InFlightNavigations,
  tabId: string,
  url: string,
  history: BrowserHistory
): { commands: InFlightNavigations; history: BrowserHistory } {
  const answered = commands[tabId] === url;
  const { [tabId]: _spent, ...rest } = commands;
  const next: InFlightNavigations = rest;
  return answered
    ? { commands: next, history }
    : { commands: next, history: visit(history, url) };
}

/**
 * `http://` or `https://`, or a prefix that makes the address one.
 *
 * Kept for callers that want the raw normalisation; the browser pane now
 * classifies through `browserDispatch.classifyBrowserAddress`, which owns
 * this same transformation (and the refusals) so the two cannot disagree.
 *
 * The bare host is the case worth having: `Url::parse` in Rust has no base to
 * resolve against, so `example.com` reaches `browser::parse_navigation` as a
 * relative URL and comes back as "not a navigable URL". The user did nothing
 * wrong, so the pane does the one transformation that makes their input
 * absolute and leaves every other judgement to the shell.
 *
 * The scheme test is `^https?://` and nothing looser, on purpose. A looser test
 * — "anything with a colon is a scheme" — reads `localhost:3000` as a scheme
 * named `localhost`, and a rule that prefixes nothing for `example.com` but
 * also prefixes nothing for `file:///etc/passwd` is not a rule about schemes at
 * all. Only the two the guard permits are recognised; anything else gets
 * `https://` in front of it and is refused by `navigation_allowed`, which is
 * where that decision belongs.
 *
 * `""` for blank input rather than an exception: a half-typed address bar is
 * the normal state of an address bar, and the pane asks "is there anything to
 * send" rather than catching.
 */
export function normaliseAddress(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "";
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  return `https://${trimmed}`;
}

/**
 * The host of an address, for a tab's title.
 *
 * Falls back to the address itself, and to `""` for an unparseable one, because
 * this runs during a render: a tab title that throws takes the strip with it,
 * and a tab titled with a full URL is a worse answer than a host but not a
 * broken one. The host rather than the whole URL because the strip is narrow
 * and a user reads it by shape — `docs.rs` tells them which page, a 90-character
 * URL with a query string tells them nothing.
 */
export function hostOf(url: string): string {
  if (!url) return "";
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}
