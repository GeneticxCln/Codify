import React, { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { FileCode } from "lucide-react";
import type { EditorBuffer, EditorBuffers } from "../editorBuffers";
import type { MountedEditor } from "../editorMount";
import { Button } from "./ui/Button";

/**
 * One file, in the centre column: its text, whether it is saved, and how to save it.
 *
 * ## The pane is a window onto a buffer, not the owner of one
 *
 * The centre column mounts only the tab in front (and, in a split, two), so a pane that owned its text would lose it
 * whenever its tab was not showing. The text lives in `editorBuffers.ts`; this component subscribes to it, and the view that
 * draws it is made when the pane mounts and thrown away when it goes. What the pane keeps is nothing: show another tab and
 * come back, and what was typed, and how to undo it, is where it was left.
 *
 * ## CodeMirror is a lazy chunk
 *
 * The view, its theme and its grammars are behind `import("../editorMount")`, the way xterm is behind the terminal pane's
 * effect. This file imports only the *types* of that module, so an app that never opens a file never loads an editor.
 *
 * ## What it will not do
 *
 * It will not show an editor over a file that did not open (an empty editor you could type into and save over the real file
 * is the one thing worse than an error), will not discard text without asking (Revert asks), and will not overwrite a
 * file that changed on disk without being told to (a conflict offers *Reload* and *Keep my version*, and keeping is only a
 * decision to overwrite on the next Save). Saving is Ctrl+S in the editor and the button; Ctrl+S is not a window-level
 * chord, so nothing else has to give it up. A pane that appears unfocused, as the second half of a split does, does not take
 * the keyboard.
 */

export interface EditorPaneProps {
  tabId: string;
  buffers: EditorBuffers;
  /** Take the keyboard once the editor is ready. Off for a pane that appears without being the one in use. */
  autoFocus?: boolean;
}

const lineColumn = (buffer: EditorBuffer): { line: number; column: number } => {
  const head = buffer.state.selection.main.head;
  const line = buffer.state.doc.lineAt(head);
  return { line: line.number, column: head - line.from + 1 };
};

export const EditorPane: React.FC<EditorPaneProps> = ({ tabId, buffers, autoFocus = true }) => {
  const snapshot = useSyncExternalStore(buffers.subscribe, buffers.getSnapshot);
  const buffer = snapshot.byTab.get(tabId);
  const hostRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<MountedEditor | null>(null);
  const [confirmingRevert, setConfirmingRevert] = useState(false);

  const ready = buffer?.status === "ready";
  const epoch = buffer?.epoch ?? 0;
  const revealToken = buffer?.revealToken ?? 0;
  const aiRanges = buffer?.aiRanges;

  // The view: made when the buffer is ready, and again when the store replaces its text (a reload), and gone with the pane.
  useEffect(() => {
    const host = hostRef.current;
    if (!ready || !host) return;
    let cancelled = false;
    let mounted: MountedEditor | null = null;
    void (async () => {
      const { mountEditor } = await import("../editorMount");
      if (cancelled) return;
      mounted = await mountEditor({ parent: host, buffers, tabId, autoFocus });
      if (cancelled) {
        mounted?.destroy();
        mounted = null;
        return;
      }
      editorRef.current = mounted;
    })();
    return () => {
      cancelled = true;
      mounted?.destroy();
      mounted = null;
      editorRef.current = null;
    };
    // `autoFocus` is read once, when the view is made: a pane that becomes the focused one later is focused by the
    // person clicking it, which the shell handles, and must not be yanked back to by a re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabId, ready, epoch, buffers]);

  // The assistant's marks, when the store changed which ranges there are without changing a character (a save).
  useEffect(() => {
    editorRef.current?.refresh();
  }, [aiRanges]);

  // Scroll to a selection the assistant (or anything else) made.
  useEffect(() => {
    if (revealToken > 0) editorRef.current?.reveal();
  }, [revealToken]);

  if (!buffer || buffer.status === "loading") {
    return (
      <div role="status" className="flex h-full items-center justify-center bg-codify-bg text-xs text-codify-muted">
        Opening{buffer ? ` ${buffer.path}` : ""}…
      </div>
    );
  }

  if (buffer.status === "error") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 bg-codify-bg px-6 text-center">
        <FileCode className="h-5 w-5 text-codify-muted" aria-hidden />
        <p className="font-mono text-xs text-codify-secondary">{buffer.path}</p>
        <p role="alert" className="max-w-md text-xs text-codify-danger-ink">
          {buffer.error ?? "This file could not be opened."}
        </p>
        <Button onClick={() => void buffers.open(tabId, { workspaceId: buffer.workspaceId, path: buffer.path })}>
          Try again
        </Button>
      </div>
    );
  }

  const { line, column } = lineColumn(buffer);
  const conflict = buffer.conflict;

  return (
    <div className="flex h-full min-h-0 flex-col bg-codify-bg" data-editor-tab={tabId}>
      <div className="flex flex-shrink-0 items-center gap-2 border-b border-codify-border bg-codify-surface px-3 py-1.5">
        <FileCode className="h-3.5 w-3.5 flex-shrink-0 text-codify-info" aria-hidden />
        <span className="min-w-0 truncate font-mono text-xs text-codify-secondary" title={buffer.path}>
          {buffer.path}
        </span>
        {buffer.dirty && <span className="flex-shrink-0 text-2xs text-codify-warning-ink">Unsaved</span>}
        {buffer.aiChanged && <span className="flex-shrink-0 text-2xs text-codify-info-ink">Changed by the assistant</span>}
        <div className="ml-auto flex flex-shrink-0 items-center gap-1.5">
          {confirmingRevert ? (
            <>
              <span className="text-2xs text-codify-muted">Throw away your changes?</span>
              <Button
                tone="danger"
                onClick={() => {
                  setConfirmingRevert(false);
                  void buffers.reload(tabId);
                }}
              >
                Discard
              </Button>
              <Button onClick={() => setConfirmingRevert(false)}>Cancel</Button>
            </>
          ) : (
            <>
              {buffer.dirty && <Button onClick={() => setConfirmingRevert(true)}>Revert</Button>}
              <Button
                tone="primary"
                disabled={!buffer.dirty || buffer.saving}
                onClick={() => void buffers.save(tabId)}
                title="Save (Ctrl+S)"
              >
                {buffer.saving ? "Saving…" : "Save"}
              </Button>
            </>
          )}
        </div>
      </div>

      {conflict?.kind === "changed" && (
        <div
          role="alert"
          className="flex flex-shrink-0 items-center gap-2 border-b border-codify-warning/60 bg-codify-warning/20 px-3 py-2 text-xs text-codify-warning-ink"
        >
          <span className="min-w-0 flex-1">
            {buffer.path} changed on disk after you opened it. Saving would overwrite that.
          </span>
          <Button onClick={() => void buffers.reload(tabId)}>Reload from disk</Button>
          <Button onClick={() => buffers.keepMine(tabId)}>Keep my version</Button>
        </div>
      )}
      {conflict?.kind === "deleted" && (
        <div
          role="alert"
          className="flex-shrink-0 border-b border-codify-warning/60 bg-codify-warning/20 px-3 py-2 text-xs text-codify-warning-ink"
        >
          {buffer.path} was deleted from disk, and saving cannot bring it back. The text here is still yours: copy it
          somewhere before you close this tab.
        </div>
      )}
      {buffer.saveError && (
        <div
          role="alert"
          className="flex-shrink-0 border-b border-codify-danger/30 bg-codify-danger/10 px-3 py-2 text-xs text-codify-danger-ink"
        >
          Could not save: {buffer.saveError}
        </div>
      )}

      <div ref={hostRef} className="min-h-0 flex-1" />

      <div
        data-testid="editor-status"
        className="flex flex-shrink-0 items-center gap-3 border-t border-codify-border bg-codify-surface px-3 py-1 text-2xs text-codify-muted"
      >
        <span>
          Ln {line}, Col {column}
        </span>
        <span>{buffer.eol === "crlf" ? "CRLF" : "LF"}</span>
        <span>UTF-8</span>
      </div>
    </div>
  );
};
