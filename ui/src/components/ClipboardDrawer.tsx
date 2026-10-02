import React, { useEffect, useMemo, useState } from "react";
import { ClipboardList, Copy, Pin, PinOff, Terminal, TextCursorInput, Trash2, X } from "lucide-react";
import {
  MAX_PINNED,
  previewOf,
  searchClips,
  SKIP_MESSAGES,
  sourceLabel,
  type Clip,
  type SkipReason,
} from "../clipboardHistory";
import { relativeTime } from "../notifications";
import { IconButton } from "./ui/IconButton";

/**
 * The clipboard drawer: what was copied, cut or pasted in this window, in the same place as the other drawers.
 *
 * A drawer and not a popover for the reason the others are: the native browser webview paints above every DOM
 * overlay (`docs/09` §8.1), so a dropdown from the header would be drawn under a browser tab's page.
 *
 * It is a view. The list, the note and the two "can this go anywhere" answers come in; every press goes out as
 * a call, and `App.tsx` decides what a press does (`docs/09` §11). A clip is drawn as plain text through React's
 * own escaping, bounded to a few lines, and never as markup: it is whatever someone copied, from anywhere.
 *
 * The standing note under the header is not decoration. This is the one place that says what is kept, where,
 * and what never is, because a history that quietly kept a password would be worse than none.
 */

/** A minute is the finest the labels speak in, so a minute is how often they need redrawing. */
const REFRESH_MS = 60_000;

const ACTION_ICON = "w-3.5 h-3.5";

export interface ClipboardDrawerProps {
  /** As kept. The drawer orders them (pinned first, newest first). */
  clips: readonly Clip[];
  /** Why the last thing was not kept, while that is the latest news. */
  lastSkip: SkipReason | null;
  /** What the last press could not do ("not pasted: that shell has exited"), until it is dismissed. */
  notice: string | null;
  /** There is a message box to put text in: a chat thread is the one in view. */
  canInsert: boolean;
  /** There is a terminal to paste into: a terminal tab is the one in view, and its shell is still running. */
  canPasteToTerminal: boolean;
  /** The terminal tab in view has finished, which is its own reason, and not the same as there being no terminal. */
  terminalExited?: boolean;
  onCopy: (clip: Clip) => void;
  onInsert: (clip: Clip) => void;
  onPasteToTerminal: (clip: Clip) => void;
  onPin: (id: string) => void;
  onRemove: (id: string) => void;
  onClearUnpinned: () => void;
  onDismissSkip: () => void;
  onDismissNotice: () => void;
  onClose: () => void;
}

const Note: React.FC<{ text: string; label: string; onDismiss: () => void }> = ({ text, label, onDismiss }) => (
  <div
    role="status"
    className="mx-2 mt-2 flex items-start gap-2 rounded-lg border border-codify-warning bg-codify-warning/10 px-2.5 py-2 text-2xs leading-relaxed text-codify-secondary"
  >
    <span className="min-w-0 flex-1 break-words">{text}</span>
    <IconButton label={label} onClick={onDismiss} className="!w-5 !h-5">
      <X className="w-3 h-3" />
    </IconButton>
  </div>
);

export const ClipboardDrawer: React.FC<ClipboardDrawerProps> = ({
  clips,
  lastSkip,
  notice,
  canInsert,
  canPasteToTerminal,
  terminalExited = false,
  onCopy,
  onInsert,
  onPasteToTerminal,
  onPin,
  onRemove,
  onClearUnpinned,
  onDismissSkip,
  onDismissNotice,
  onClose,
}) => {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), REFRESH_MS);
    return () => clearInterval(timer);
  }, []);

  const [query, setQuery] = useState("");
  const shown = useMemo(() => searchClips(clips, query), [clips, query]);
  const searching = query.trim().length > 0;
  const anyUnpinned = clips.some((c) => !c.pinned);
  // The model refuses a pin past the ceiling; a press that quietly did nothing would read as a broken button.
  const atPinLimit = clips.filter((c) => c.pinned).length >= MAX_PINNED;

  return (
    <aside
      aria-label="Clipboard"
      className="w-80 max-w-[40%] shrink-0 bg-codify-surface border-l border-codify-border flex flex-col"
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-codify-border">
        <div className="flex items-center gap-2 text-sm font-semibold text-codify-primary">
          <ClipboardList className="w-4 h-4 text-codify-info" aria-hidden="true" />
          Clipboard
        </div>
        <div className="flex items-center gap-2">
          <IconButton
            label="Clear unpinned clips"
            title="Clear unpinned clips — pinned ones are kept"
            onClick={onClearUnpinned}
            disabled={!anyUnpinned}
          >
            <Trash2 className="w-3.5 h-3.5" />
          </IconButton>
          <IconButton label="Close clipboard" onClick={onClose}>
            <X className="w-4 h-4" />
          </IconButton>
        </div>
      </div>

      <p className="px-4 py-2 border-b border-codify-border text-2xs leading-relaxed text-codify-muted">
        Kept only in this window, and never sent anywhere. Keys, tokens and anything from a password field are
        never kept.
      </p>

      {lastSkip && <Note text={SKIP_MESSAGES[lastSkip]} label="Dismiss note" onDismiss={onDismissSkip} />}
      {notice && <Note text={notice} label="Dismiss message" onDismiss={onDismissNotice} />}

      <div className="px-2 pt-2">
        {/* Never read by the history itself: a search term pasted here is not something to keep. */}
        <input
          type="search"
          aria-label="Search clipboard history"
          placeholder="Search clipboard history"
          data-clipboard="off"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="w-full bg-codify-bg border border-codify-border rounded px-2.5 py-1.5 text-xs text-codify-secondary placeholder:text-codify-muted focus:outline-none focus:border-codify-accent"
        />
      </div>

      <div className="flex-1 overflow-y-auto overflow-x-hidden p-2">
        {shown.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-4 py-10 text-center text-xs text-codify-muted">
            <ClipboardList className="w-5 h-5" aria-hidden="true" />
            {searching ? (
              <p className="font-semibold text-codify-secondary">No clips match</p>
            ) : (
              <>
                <p className="font-semibold text-codify-secondary">Nothing yet</p>
                <p className="leading-relaxed">
                  Text you copy, cut or paste in Codify shows up here, so it can be copied again, put in the
                  message box or pasted into the terminal.
                </p>
              </>
            )}
          </div>
        ) : (
          <ul className="space-y-1">
            {shown.map((clip) => {
              const preview = previewOf(clip.text);
              const first = preview.lines[0] ?? "";
              return (
                <li
                  key={clip.id}
                  aria-label={`Clip: ${first.length > 60 ? `${first.slice(0, 60)}…` : first}`}
                  className="rounded-lg border border-codify-border bg-codify-bg px-2.5 py-2"
                >
                  <pre className="max-h-32 overflow-hidden whitespace-pre-wrap break-words font-mono text-2xs leading-relaxed text-codify-secondary">
                    {preview.lines.join("\n")}
                  </pre>
                  {preview.hiddenLines > 0 && (
                    <p className="mt-0.5 text-2xs text-codify-muted">
                      {preview.hiddenLines} more {preview.hiddenLines === 1 ? "line" : "lines"}
                    </p>
                  )}
                  <div className="mt-1.5 flex items-center gap-1">
                    <span className="min-w-0 flex-1 truncate text-2xs text-codify-muted">
                      {sourceLabel(clip.source)} · {relativeTime(clip.at, now)} · {preview.chars.toLocaleString("en-US")}{" "}
                      characters
                    </span>
                    <IconButton label="Copy again" onClick={() => onCopy(clip)}>
                      <Copy className={ACTION_ICON} />
                    </IconButton>
                    <IconButton
                      label="Insert into the message box"
                      title={canInsert ? "Insert into the message box" : "Open a chat thread to insert into its message box"}
                      onClick={() => onInsert(clip)}
                      disabled={!canInsert}
                    >
                      <TextCursorInput className={ACTION_ICON} />
                    </IconButton>
                    <IconButton
                      label="Paste into the terminal"
                      title={
                        canPasteToTerminal
                          ? "Paste into the terminal"
                          : terminalExited
                            ? "That shell has exited"
                            : "Open a terminal tab to paste into it"
                      }
                      onClick={() => onPasteToTerminal(clip)}
                      disabled={!canPasteToTerminal}
                    >
                      <Terminal className={ACTION_ICON} />
                    </IconButton>
                    <IconButton
                      label={clip.pinned ? "Unpin clip" : "Pin clip"}
                      title={
                        !clip.pinned && atPinLimit
                          ? `${MAX_PINNED} clips are pinned: unpin one first`
                          : clip.pinned
                            ? "Unpin clip"
                            : "Pin clip"
                      }
                      aria-pressed={clip.pinned}
                      disabled={!clip.pinned && atPinLimit}
                      onClick={() => onPin(clip.id)}
                    >
                      {clip.pinned ? (
                        <PinOff className={`${ACTION_ICON} text-codify-accent`} />
                      ) : (
                        <Pin className={ACTION_ICON} />
                      )}
                    </IconButton>
                    <IconButton label="Delete clip" onClick={() => onRemove(clip.id)}>
                      <X className={ACTION_ICON} />
                    </IconButton>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </aside>
  );
};
