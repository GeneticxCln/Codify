import React, { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Globe, RotateCw, TerminalSquare } from "lucide-react";
import {
  canGoBack,
  canGoForward,
  currentUrl,
  emptyHistory,
  type BrowserHistory,
} from "../browserHistory";
import { classifyBrowserAddress } from "../browserDispatch";
import type { BrowserBounds } from "../api";
import { IconButton } from "./ui/IconButton";

/**
 * The control surface — and now the viewport — for a browser tab.
 *
 * ## The page is in here
 *
 * `codify_browser_open` seats a child webview of the main window **on top of
 * this pane's content area** (`docs/09` §7.3): the pane measures its own
 * content rectangle in logical pixels and hands it to the shell, which places
 * the native view exactly there. The pane's chrome — back, forward, reload,
 * the address bar — stays above the page because the page starts below it.
 * This replaced a separate OS window, which put the page somewhere the user
 * had to go looking for and made every certificate error a dialog in a
 * window the app did not control.
 *
 * The measurements travel through `onBounds`; the shell resizes every page
 * with them. They are re-reported whenever the content area changes, which
 * is what makes a window resize move the page with the layout rather than
 * leaving it the size of the moment it was opened.
 *
 * ## The refusal that never leaves the room
 *
 * `classifyBrowserAddress` runs before any round trip: an address the shell
 * will refuse — non-web scheme, loopback, a numeric spelling of 127.0.0.1 —
 * is refused here, in the shell's own sentence, and no page is seated. The
 * shell stays the enforcement point; this is the same answer, earlier.
 *
 * The decisions live in `browserHistory.ts` and `browserDispatch.ts`; this is
 * the thin half — render the stack's state, send what the model says, and
 * hold the one piece of local state that is genuinely local: the text in the
 * address bar while it is being edited.
 */
export interface BrowserPaneProps {
  /** The tab id, which is also what the shell's commands take. */
  tabId: string;
  /** The address on screen. `undefined` until the tab has been given one. */
  url?: string;
  history?: BrowserHistory;
  /** Sent when the user commits an address. Receives a normalised address. */
  onNavigate: (url: string) => void;
  /** A page is about to be shown for the first time; the pane seats one. */
  onOpen: (url: string) => void;
  onBack: () => void;
  onForward: () => void;
  /** The shell's own words, shown unchanged. */
  error?: string | null;
  /**
   * The pane's content rectangle, in logical pixels, whenever it changes —
   * including once on mount. The shell places and resizes the native page
   * with it.
   */
  onBounds?: (bounds: BrowserBounds) => void;
  /** Toggle the page's DevTools inspector. Absent when the shell has none. */
  onToggleDevtools?: () => void;
  /** Whether the inspector is open right now, for the control's state. */
  devtoolsOpen?: boolean;
}

export const BrowserPane: React.FC<BrowserPaneProps> = ({
  tabId,
  url,
  history = emptyHistory(),
  onNavigate,
  onOpen,
  onBack,
  onForward,
  error = null,
  onBounds,
  onToggleDevtools,
  devtoolsOpen = false,
}) => {
  const shown = currentUrl(history) || url || "";
  // The address bar keeps its own text while it is being edited, which is what
  // makes a half-typed address survivable: driving the input from the stack
  // would overwrite "https://exa" with the committed address on every keystroke
  // that triggered a re-render. `committed` is what it snaps back to.
  const [draft, setDraft] = useState(shown);
  const [committed, setCommitted] = useState(shown);
  // A mirror refusal — the shell's sentence, delivered before the shell is
  // asked. Local, because it belongs to the address bar that produced it and
  // dies with the next commit.
  const [refused, setRefused] = useState<string | null>(null);

  useEffect(() => {
    setDraft(shown);
    setCommitted(shown);
  }, [shown]);

  // The content area is what the page is seated on, so it is the thing that
  // measures. A ResizeObserver, not a window-resize listener: the pane's
  // rectangle changes when the window changes, but also when the sidebar
  // collapses, when a split appears, and on the first layout after mount —
  // and every one of those must move the page, not just the first.
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const onBoundsRef = useRef(onBounds);
  useEffect(() => {
    onBoundsRef.current = onBounds;
  }, [onBounds]);
  useEffect(() => {
    const el = viewportRef.current;
    if (!el || !onBoundsRef.current) return;
    const report = (): void => {
      const rect = el.getBoundingClientRect();
      onBoundsRef.current?.({
        x: rect.left,
        y: rect.top,
        width: rect.width,
        height: rect.height,
      });
    };
    report();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(report);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const commit = (raw: string): void => {
    // Blank is the normal state of a half-cleared address bar, not a request to
    // navigate to nowhere — so it is swallowed here rather than classified.
    if (!raw.trim()) return;
    // The classifier owns the address-bar's one transformation (a bare host
    // gets `https://`), so it sees what the user typed, not a pre-normalised
    // form — one normaliser, not two.
    const classified = classifyBrowserAddress(raw, tabId);
    if (classified.kind === "refuse") {
      setRefused(classified.reason);
      return;
    }
    setRefused(null);
    setDraft(classified.url);
    setCommitted(classified.url);
    // `url` is undefined until a page exists for this tab, so the first
    // address seats one and every later one navigates it.
    if (url) onNavigate(classified.url);
    else onOpen(classified.url);
  };

  const isDraft = draft !== committed;
  const problem = refused ?? error;

  return (
    <section
      aria-label="Browser"
      data-tab-id={tabId}
      className="flex-1 flex flex-col min-h-0 bg-codify-surface"
    >
      <div className="flex items-center gap-1.5 px-2 py-1.5 border-b border-codify-border">
        <IconButton
          label="Back"
          title="Back"
          onClick={onBack}
          disabled={!canGoBack(history)}
        >
          <ArrowLeft className="w-3.5 h-3.5" />
        </IconButton>
        <IconButton
          label="Forward"
          title="Forward"
          onClick={onForward}
          disabled={!canGoForward(history)}
        >
          <ArrowRight className="w-3.5 h-3.5" />
        </IconButton>
        <IconButton
          label="Reload"
          title="Reload this page"
          onClick={() => shown && commit(shown)}
          disabled={!shown}
        >
          <RotateCw className="w-3.5 h-3.5" />
        </IconButton>
        {onToggleDevtools && (
          <IconButton
            label="DevTools"
            title={devtoolsOpen ? "Close the page inspector" : "Inspect this page"}
            onClick={onToggleDevtools}
            aria-pressed={devtoolsOpen}
          >
            <TerminalSquare className="w-3.5 h-3.5" />
          </IconButton>
        )}
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          // Enter commits, Escape abandons — one handler, because two on the
          // same input is two places to forget one of them. Escape is the
          // browser convention for "put back what was there", and it is the
          // only one of the two that is not a navigation.
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit(isDraft ? draft : shown);
            } else if (e.key === "Escape" && isDraft) {
              e.preventDefault();
              setDraft(committed);
            }
          }}
          // The address bar is the only text field in a browser tab, so
          // select-all is what a user pressing it expects: they are about to
          // type a different address, not edit the last character of this one.
          onFocus={(e) => e.currentTarget.select()}
          placeholder="Search or enter an address"
          aria-label="Address"
          spellCheck={false}
          autoComplete="off"
          className="flex-1 min-w-0 h-7 px-2 rounded-md text-xs font-mono bg-codify-bg border border-codify-border text-codify-primary placeholder:text-codify-muted focus:outline-none focus:border-codify-accent"
        />
      </div>

      {problem && (
        <div
          role="alert"
          className="mx-3 mt-2 p-2 rounded-lg text-xs bg-codify-danger/20 border border-codify-danger/60 text-codify-danger"
        >
          {problem}
        </div>
      )}

      {/* The page renders here — a native webview seated over this exact
          rectangle by the shell, which is why the div is empty and why it
          reports its geometry. When no page exists yet, the placeholder is
          what a user sees instead of a void. */}
      <div
        ref={viewportRef}
        data-testid="browser-viewport"
        className="flex-1 min-h-0"
      >
        {!shown && (
          <div className="w-full h-full flex items-center justify-center p-8">
            <div className="max-w-md text-center">
              <Globe className="w-8 h-8 mx-auto mb-3 text-codify-muted" />
              <p className="text-sm text-codify-primary">No address yet.</p>
              <p className="mt-1.5 text-xs text-codify-muted">
                Type an address above to open one.
              </p>
            </div>
          </div>
        )}
      </div>
    </section>
  );
};
