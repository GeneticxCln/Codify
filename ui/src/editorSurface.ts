/**
 * The assistant's eyes and hands on the editor, answered from the buffers.
 *
 * The engine asks a question (`engine/surfaces.py`: `editor` surface, ops `read`, `open`, `edit`), the window polls it
 * and answers it here, and what comes back is checked against a fixed shape on the engine's side before the model sees a
 * word of it. This module is the window's half of that, and it is written against `EditorBuffers` (plain
 * `EditorState`s) and a small `EditorHost` standing in for the App, so all of it runs with no view and no DOM.
 *
 *  - **`read` sees what the person sees**: the text in the editor, unsaved changes included (the disk is exactly what they
 *    are not looking at), and where the cursor and the selection are. It never goes back to the disk.
 *  - **`open` points**: it makes the file's tab, loads it, selects the lines. *Where* the tab goes is the host's decision
 *    (`beside` the conversation, `front`, or `background`), and the answer says which.
 *  - **`edit` changes a buffer and only a buffer.** It never saves, and nothing in this file calls `save`: a person's
 *    Save is the one way text reaches the disk (`docs/00` §6.9). A file that is not open is opened *in the background* to
 *    be edited, so an edit never moves the person.
 *
 * Every question is about **one workspace**, the one the run belongs to. Another workspace's editors are not listed and
 * not reachable, however a path is spelled.
 *
 * It leaves nothing behind. An open that fails closes the tab it just made; an edit that does not apply changes
 * nothing, and if it opened the file only to try, closes it again. A tab the person already had is never closed here.
 */
import type { EditorBuffer, EditorBuffers } from "./editorBuffers";
import type { SurfaceRequest } from "./types";

/** The most lines one read brings back: the engine's own cap, so the two cut in the same place. */
const MAX_READ_LINES = 400;

export interface EditorHost {
  /** The editor tabs on screen right now, and the one with focus. */
  view(): { shown: readonly string[]; focused: string | null };
  /**
   * Open (or find) the tab for a file. `show` is the assistant pointing at something: the host puts it beside the
   * conversation if that is what the person is looking at and it fits, and otherwise leaves it in the background with a
   * marker. `background` is an edit: the person is never moved. Neither takes the keyboard.
   */
  openFile(
    file: { workspaceId: string; path: string },
    intent: "show" | "background",
  ): { tabId: string; opened: boolean; shown: "beside" | "front" | "background" };
  /** Close a tab this module opened and could not use. */
  closeFile(tabId: string): void;
}

export type SurfaceReply = { ok: true; result: unknown } | { ok: false; error: string };

const refuse = (error: string): SurfaceReply => ({ ok: false, error });
const done = (result: unknown): SurfaceReply => ({ ok: true, result });

export function createEditorSurface(
  buffers: EditorBuffers,
  host: EditorHost,
): (request: SurfaceRequest) => Promise<SurfaceReply> {
  /** Where the cursor is, as a person counts: 1-based line and column. */
  const cursorOf = (b: EditorBuffer): { line: number; column: number } => {
    const head = b.state.selection.main.head;
    const line = b.state.doc.lineAt(head);
    return { line: line.number, column: head - line.from + 1 };
  };

  const selectionOf = (b: EditorBuffer): { from_line: number; to_line: number; text: string } | null => {
    const { from, to } = b.state.selection.main;
    if (from === to) return null;
    return {
      from_line: b.state.doc.lineAt(from).number,
      to_line: b.state.doc.lineAt(to).number,
      text: b.state.sliceDoc(from, to),
    };
  };

  const read = (workspaceId: string, args: Record<string, unknown>): SurfaceReply => {
    const snapshot = buffers.getSnapshot();
    const { shown, focused } = host.view();
    const mine = [...snapshot.byTab.values()].filter((b) => b.workspaceId === workspaceId && b.status === "ready");
    const open = mine.map((b) => ({
      path: b.path,
      dirty: b.dirty,
      in_view: shown.includes(b.tabId),
      focused: focused === b.tabId,
      ai_changed: b.aiChanged,
      cursor: cursorOf(b),
      selection: selectionOf(b),
    }));
    const path = typeof args.path === "string" ? args.path : null;
    if (path === null) return done({ open, file: null });

    const b = buffers.find(workspaceId, path);
    if (!b || b.status === "error") {
      return refuse(
        `${path} is not open in the editor, so there is no text of the person's to read. Open it with open_in_editor, ` +
          "or read the file on disk with read_file.",
      );
    }
    if (b.status === "loading") return refuse(`${path} is still loading in the editor. Try again in a moment.`);
    const total = buffers.lineCount(b.tabId);
    const from = typeof args.from_line === "number" ? args.from_line : 1;
    const want = Math.min(typeof args.to_line === "number" ? args.to_line : total, total);
    const last = Math.min(want, from + MAX_READ_LINES - 1);
    const lines: string[] = [];
    for (let n = from; n <= last; n += 1) lines.push(b.state.doc.line(n).text);
    return done({
      open,
      file: {
        path: b.path,
        dirty: b.dirty,
        ai_changed: b.aiChanged,
        from_line: from,
        total_lines: total,
        lines,
        truncated: last < want,
      },
    });
  };

  /**
   * The file's buffer, loaded: opened through the host if it is not there. When it cannot be had, returns the reason
   * and has closed any tab it made for the attempt.
   */
  const ensure = async (
    workspaceId: string,
    path: string,
    intent: "show" | "background",
  ): Promise<{ tabId: string; opened: boolean; shown: "beside" | "front" | "background" } | { error: string }> => {
    const placed = host.openFile({ workspaceId, path }, intent);
    await buffers.open(placed.tabId, { workspaceId, path });
    const b = buffers.get(placed.tabId);
    if (!b || b.status !== "ready") {
      if (placed.opened) host.closeFile(placed.tabId);
      return { error: b?.error ?? `${path} could not be opened in the editor.` };
    }
    return placed;
  };

  const open = async (workspaceId: string, args: Record<string, unknown>): Promise<SurfaceReply> => {
    const path = String(args.path ?? "");
    const placed = await ensure(workspaceId, path, "show");
    if ("error" in placed) return refuse(`${path} was not opened: ${placed.error}`);
    const result: Record<string, unknown> = { path, opened: placed.opened, shown: placed.shown };
    if (typeof args.line === "number") {
      buffers.selectLines(placed.tabId, args.line, typeof args.end_line === "number" ? args.end_line : undefined);
      const b = buffers.get(placed.tabId) as EditorBuffer;
      const { from, to } = b.state.selection.main;
      result.from_line = b.state.doc.lineAt(from).number;
      result.to_line = b.state.doc.lineAt(to).number;
    }
    return done(result);
  };

  const edit = async (workspaceId: string, args: Record<string, unknown>): Promise<SurfaceReply> => {
    const path = String(args.path ?? "");
    const placed = await ensure(workspaceId, path, "background");
    if ("error" in placed) return refuse(`${path} was not edited: ${placed.error}`);
    const outcome = buffers.edit(placed.tabId, {
      oldText: String(args.old_text ?? ""),
      newText: String(args.new_text ?? ""),
      count: typeof args.count === "number" ? args.count : 1,
    });
    if (!outcome.ok) {
      // Nothing changed. A tab that exists only because this edit needed the file is not left behind.
      if (placed.opened) host.closeFile(placed.tabId);
      return refuse(outcome.reason);
    }
    return done({
      path,
      replaced: outcome.replaced,
      from_line: outcome.fromLine,
      to_line: outcome.toLine,
      opened: placed.opened,
      dirty: true,
    });
  };

  return async (request) => {
    try {
      switch (request.op) {
        case "read":
          return read(request.workspace_id, request.args);
        case "open":
          return await open(request.workspace_id, request.args);
        case "edit":
          return await edit(request.workspace_id, request.args);
        default:
          return refuse(`The editor has no operation called ${request.op}.`);
      }
    } catch (error) {
      return refuse(error instanceof Error ? error.message : String(error));
    }
  };
}
