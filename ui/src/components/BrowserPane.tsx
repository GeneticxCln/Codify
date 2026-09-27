import React, { useEffect, useState } from "react";
import { ArrowLeft, ArrowRight, Globe, RotateCw } from "lucide-react";
import {
  canGoBack,
  canGoForward,
  currentUrl,
  emptyHistory,
  normaliseAddress,
  type BrowserHistory,
} from "../browserHistory";
import { IconButton } from "./ui/IconButton";

/**
 * The control surface for a browser tab.
 *
 * ## The page is not in here, and the pane says so
 *
 * `codify_browser_open` builds a `WebviewWindow`, so the page is a **separate OS
 * window** (`docs/09` §7.2). This pane is where the address goes, not where the
 * document renders, and it says so in a line of its own rather than leaving the
 * user to wonder why the chrome is here and the site is not. Embedding the page
 * in the main window is a real change — a child webview reports its parent's
 * window label, and `capabilities/default.json` grants on `windows: ["main"]`,
 * so it would hand the untrusted page the whole `codify_*` grant until that
 * capability was re-pointed at `webviews`. That trade is worth making
 * deliberately and not as a side effect of adding an address bar.
 *
 * The decisions live in `browserHistory.ts`; this is the thin half — render the
 * stack's state, send what the model says, and hold the one piece of local
 * state that is genuinely local: the text in the address bar while it is being
 * edited.
 */
export interface BrowserPaneProps {
  /** The tab id, which is also what the shell's commands take. */
  tabId: string;
  /** The address on screen. `undefined` until the tab has been given one. */
  url?: string;
  history?: BrowserHistory;
  /** Sent when the user commits an address. Receives a normalised address. */
  onNavigate: (url: string) => void;
  /** A page is about to be shown for the first time; the pane opens a window. */
  onOpen: (url: string) => void;
  onBack: () => void;
  onForward: () => void;
  /** The shell's own words, shown unchanged. */
  error?: string | null;
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
}) => {
  const shown = currentUrl(history) || url || "";
  // The address bar keeps its own text while it is being edited, which is what
  // makes a half-typed address survivable: driving the input from the stack
  // would overwrite "https://exa" with the committed address on every keystroke
  // that triggered a re-render. `committed` is what it snaps back to.
  const [draft, setDraft] = useState(shown);
  const [committed, setCommitted] = useState(shown);

  useEffect(() => {
    setDraft(shown);
    setCommitted(shown);
  }, [shown]);

  const commit = (raw: string): void => {
    const address = normaliseAddress(raw);
    // Blank is the normal state of a half-cleared address bar, not a request to
    // navigate to nowhere — so it is swallowed here rather than sent to the
    // shell to be refused.
    if (!address) return;
    setDraft(address);
    setCommitted(address);
    // `url` is undefined until a webview exists for this tab, so the first
    // address opens one and every later one navigates it.
    if (url) onNavigate(address);
    else onOpen(address);
  };

  const isDraft = draft !== committed;

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

      {error && (
        <div
          role="alert"
          className="mx-3 mt-2 p-2 rounded-lg text-xs bg-red-950/40 border border-red-800 text-red-300"
        >
          {error}
        </div>
      )}

      <div className="flex-1 flex items-center justify-center p-8">
        <div className="max-w-md text-center">
          <Globe className="w-8 h-8 mx-auto mb-3 text-codify-muted" />
          <p className="text-sm text-codify-primary">
            {shown ? "The page is open in its own window." : "No address yet."}
          </p>
          <p className="mt-1.5 text-xs text-codify-muted">
            {shown
              ? "Use the address bar to go somewhere else, or Back and Forward to move through this tab's history."
              : "Type an address above to open one."}
          </p>
        </div>
      </div>
    </section>
  );
};
