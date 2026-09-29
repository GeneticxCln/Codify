/**
 * What a browser tab knows about its page's life: loading, loaded, titled.
 *
 * The shell announces a page's load start and completion (`browser-page-*`,
 * from `on_page_load`) and its document title (`browser-page-titled`, from
 * `on_document_title_changed`). Those are facts arriving out of order, for
 * tabs that may be gone, about addresses that may have been superseded —
 * which is every interesting bug in event-driven state, so the decisions
 * live here where `node --test` can reach them:
 *
 * - **A redirect is an address.** The load events carry the address being
 *   loaded, which is the page's *live* answer — what the address bar should
 *   say and what the tab is titled by, not the last address the user typed.
 *   `Page.url` and `Tab.title` follow the page, and the history stack does
 *   not (a redirect is the same visit, not a new one).
 * - **A load that never finishes must not spin forever.** The runtime has
 *   no failed-load event (wry 0.55.1's `PageLoadEvent` is `Started |
 *   Finished`, checked against the vendored source), so "loading" here
 *   means "Started, no Finished yet" — and a page that dies into WebKit's
 *   TLS interstitial emits no Finished at all. The marker is therefore
 *   bounded: expiry time is a property of this module, pinned by a test,
 *   and the expiry is what stops the spinner, not a guessed success.
 * - **Events for tabs that are gone are dropped by shape**, not by luck:
 *   every reducer takes the state and answers with it unchanged when the
 *   id names no browser tab.
 *
 * The shell stays the enforcement point for what may be *navigated to*;
 * nothing here re-runs that guard. What a page reports about itself — its
 * address, its title, its progress — is data, and the reducer's only job is
 * to land it on the right tab.
 */

/** How long a "loading" marker may live with no Finished event. */
export const LOADING_TIMEOUT_MS = 15_000;

/** What the shell announced about one page. */
export interface BrowserPageState {
  tab_id: string;
  url: string;
  title?: string;
}

/**
 * The tab-id in a page-life payload, or null when it is not one.
 *
 * Validated rather than cast, for the same reason every shell-event reader
 * is: the payload crosses a process boundary and lands on a reducer that
 * changes tab state, so a malformed payload must change nothing at all.
 * The URL is *not* required to parse here — a page-life fact is data the
 * tab records, not a navigation the shell will be asked to perform.
 */
export function readBrowserPageState(payload: unknown): BrowserPageState | null {
  if (typeof payload !== "object" || payload === null) return null;
  const { tab_id, url, title } = payload as {
    tab_id?: unknown;
    url?: unknown;
    title?: unknown;
  };
  if (typeof tab_id !== "string" || tab_id.length === 0) return null;
  if (typeof url !== "string" || url.length === 0) return null;
  if (title !== undefined && typeof title !== "string") return null;
  return title === undefined
    ? { tab_id, url }
    : { tab_id, url, title };
}

/** Is this tab one the reducer may write to? Unknown or non-browser: no. */
function findBrowserTab(
  tabs: Array<{ id: string; kind: string; url?: string }>,
  tabId: string
): { id: string; kind: string; url?: string } | undefined {
  return tabs.find((t) => t.id === tabId && t.kind === "browser");
}

/**
 * A load started on this tab's page.
 *
 * Returns the tab ids that are loading — the caller holds the schedule for
 * the bounded expiry; this module's decisions are which tabs the fact
 * applies to and how long it may last, not where the timer lives.
 */
export function pageLoadStarted(
  tabs: Array<{ id: string; kind: string; url?: string }>,
  tabId: string
): string[] | null {
  if (!findBrowserTab(tabs, tabId)) return null;
  return [tabId];
}

/**
 * A load finished on this tab's page.
 *
 * Returns the **live address** the page actually arrived at — the payload's
 * URL — which is what `Tab.url` should say now (redirects included), or
 * null when the id names no browser tab. The history stack is *not*
 * rewritten here: the visit the user made is the visit they made, and a
 * redirect is where that visit landed, not a second place they went.
 */
export function pageLoadFinishedPayload(
  payload: BrowserPageState
): { tabId: string; url: string } | null {
  if (!payload.tab_id || !payload.url) return null;
  return { tabId: payload.tab_id, url: payload.url };
}

/**
 * A page named itself.
 *
 * Returns the title, or null when the payload is not a titled fact or the
 * title is empty — an empty document title is *no* answer, and the tab
 * keeps the host it was showing rather than displaying nothing.
 */
export function pageTitlePayload(
  payload: BrowserPageState
): { tabId: string; title: string } | null {
  if (!payload.title || payload.title.trim().length === 0) return null;
  return { tabId: payload.tab_id, title: payload.title.trim() };
}
