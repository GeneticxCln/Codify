/**
 * The editor's buffers: every file open in an editor tab, what it says right now, and what has not been saved.
 *
 * **They live above the panes.** The centre column mounts only the tab in front (in a split, two), so a pane that owned its
 * text would lose it whenever its tab was not showing. This is the same reason a terminal's scrollback lives in
 * `terminalBuffer.ts`, and for the same reason the store is a module and not React state: it must outlive any component.
 *
 * **A buffer is a CodeMirror `EditorState`**, which is plain data: the text, the selection and (once a pane has mounted)
 * the undo history. That is what lets the assistant's eyes and hands (`editorSurface.ts`) run with no view and no DOM:
 * reading a file, selecting lines and replacing text are all operations on a state. When a pane *is* showing the buffer it
 * attaches its view, and an edit goes through the view so the pane's undo history sees it; the view reports every
 * transaction back through `applyTransactions`, which is the one place the store learns what changed.
 *
 * **Nothing here writes a file except `save`**, which is a person's Save: it sends the text and the version it read to
 * `PUT /workspaces/{id}/file` and nothing else reaches the disk (`docs/00` §6.9). `edit` changes a buffer, and
 * only a buffer.
 *
 * What a file is, here, mirrors the engine (`engine/fs.py`): UTF-8 text, at most `MAX_EDIT_BYTES`. A file is edited as LF
 * with its byte-order mark taken off and put back on save, and CRLF restored; a file that mixes line endings, or uses a bare
 * carriage return, is refused rather than quietly rewritten, because "a save never changes a line the person did not touch"
 * is not a promise that can be kept for it.
 */
import { EditorSelection, EditorState, Annotation } from "@codemirror/state";
import type { Transaction, TransactionSpec } from "@codemirror/state";

/** The largest file the editor will hold: the engine's `MAX_EDIT_BYTES`, because a person must be able to save what they see. */
export const MAX_EDIT_BYTES = 1_000_000;

export type LineEnding = "lf" | "crlf";

export interface FileIo {
  read(workspaceId: string, path: string): Promise<{ content: string; version: string }>;
  save(
    workspaceId: string,
    body: { path: string; content: string; base_version: string },
  ): Promise<{ path: string; version: string; size: number }>;
}

/** What a pane gives the store so an edit can go through its view. */
export interface ViewHandle {
  readonly state: EditorState;
  dispatch(spec: TransactionSpec): void;
}

export interface AiRange {
  from: number;
  to: number;
}

/**
 * Carried by the transaction an assistant edit makes, naming where the new text landed. The store reads it back in
 * `applyTransactions`, so a pane's view and a headless state mark the assistant's text by the same road.
 */
export const aiEdit = Annotation.define<AiRange[]>();

export type Conflict =
  | { kind: "changed"; currentVersion: string }
  | { kind: "deleted" };

export interface EditorBuffer {
  readonly tabId: string;
  readonly workspaceId: string;
  readonly path: string;
  readonly status: "loading" | "ready" | "error";
  /** Why a buffer could not be opened, in a sentence. */
  readonly error?: string;
  readonly state: EditorState;
  /** The text as it is on disk (LF, no byte-order mark): what "unsaved" is measured against. */
  readonly diskText: string;
  /** The version of the bytes `diskText` came from: what a save names. */
  readonly version: string;
  readonly eol: LineEnding;
  readonly bom: boolean;
  /** The text differs from the disk. */
  readonly dirty: boolean;
  /** Where the assistant's text is, for highlighting. Mapped through every later change; gone on save or revert. */
  readonly aiRanges: readonly AiRange[];
  /** The assistant has changed this text since it was last saved or reloaded (a deletion leaves no range to show). */
  readonly aiChanged: boolean;
  /** The disk changed under unsaved text, or the file is gone. */
  readonly conflict: Conflict | null;
  readonly saving: boolean;
  /** Why the last save did not happen, when it was neither a success nor a conflict. */
  readonly saveError?: string;
  /** Bumped when the assistant (or anything else) selects lines, so the pane scrolls to them once. */
  readonly revealToken: number;
  /**
   * Bumped when the store *replaces* the text (the file loaded, or was reloaded or reverted), as opposed to the person or
   * the assistant changing it. A pane showing the buffer starts over from the new text when this moves.
   */
  readonly epoch: number;
}

export interface BuffersSnapshot {
  readonly version: number;
  readonly byTab: ReadonlyMap<string, EditorBuffer>;
}

export type EditOutcome =
  | { ok: true; replaced: number; fromLine: number; toLine: number }
  | { ok: false; reason: string };

export type SaveOutcome = { ok: true } | { ok: false; conflict: true } | { ok: false; error: string };

// ── what a file is, as the editor holds it ─────────────────────────────────────

export type Normalized = { text: string; eol: LineEnding; bom: boolean } | { error: string };

const MIXED_ENDINGS =
  "This file mixes line endings (or uses bare carriage returns), which the editor cannot keep intact when it saves. " +
  "Open it in another editor, or make its endings consistent first.";

/** A file's bytes-as-text, as the editor edits it: LF, no byte-order mark, and what it takes to put them back. */
export function normalizeForEditing(content: string): Normalized {
  const bom = content.startsWith("\uFEFF");
  const body = bom ? content.slice(1) : content;
  if (!body.includes("\r")) return { text: body, eol: "lf", bom };
  if (/\r(?!\n)/.test(body) || /(?<!\r)\n/.test(body)) return { error: MIXED_ENDINGS };
  return { text: body.replace(/\r\n/g, "\n"), eol: "crlf", bom };
}

/** The inverse of `normalizeForEditing`: what to write so a file the person did not touch comes back byte for byte. */
export function serializeForDisk(text: string, how: { eol: LineEnding; bom: boolean }): string {
  const body = how.eol === "crlf" ? text.replace(/\n/g, "\r\n") : text;
  return how.bom ? `\uFEFF${body}` : body;
}

/** An engine error's message without its `code: ` prefix, which is for the code and not for the person. */
function plain(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/^[a-z][a-z_]*: /, "");
}

const codeOf = (error: unknown): string | undefined => (error as { code?: string } | null)?.code;

const byteLength = (text: string): number => new TextEncoder().encode(text).length;

/** Lines as a person counts them: a final newline ends the last line instead of starting another, and an empty file has none. */
function realLines(state: EditorState): number {
  const { doc } = state;
  if (doc.length === 0) return 0;
  return doc.lines > 1 && doc.line(doc.lines).length === 0 ? doc.lines - 1 : doc.lines;
}

// ── the store ────────────────────────────────────────────────────────────────

export interface EditorBuffers {
  open(tabId: string, file: { workspaceId: string; path: string }): Promise<void>;
  get(tabId: string): EditorBuffer | undefined;
  find(workspaceId: string, path: string): EditorBuffer | undefined;
  close(tabId: string): void;
  subscribe(listener: () => void): () => void;
  getSnapshot(): BuffersSnapshot;
  unsavedIds(snapshot: BuffersSnapshot): string[];
  assistantEditedIds(snapshot: BuffersSnapshot): string[];
  lineCount(tabId: string): number;
  attachView(tabId: string, view: ViewHandle | null): void;
  applyTransactions(tabId: string, transactions: readonly Transaction[]): void;
  edit(tabId: string, change: { oldText: string; newText: string; count: number }): EditOutcome;
  selectLines(tabId: string, from: number, to?: number): void;
  save(tabId: string, options?: { overwrite?: boolean }): Promise<SaveOutcome>;
  reload(tabId: string): Promise<{ ok: true } | { ok: false; error: string }>;
  noteDiskChange(workspaceId: string, paths: readonly string[]): Promise<void>;
  keepMine(tabId: string): void;
}

const EMPTY = EditorState.create({ doc: "" });

export function createEditorBuffers(io: FileIo): EditorBuffers {
  const buffers = new Map<string, EditorBuffer>();
  const views = new Map<string, ViewHandle>();
  const loading = new Map<string, Promise<void>>();
  const saves = new Map<string, Promise<SaveOutcome>>();
  const listeners = new Set<() => void>();
  let snapshot: BuffersSnapshot = { version: 0, byTab: new Map() };

  const publish = (): void => {
    snapshot = { version: snapshot.version + 1, byTab: new Map(buffers) };
    for (const listener of [...listeners]) listener();
  };

  const put = (tabId: string, change: Partial<EditorBuffer>): EditorBuffer | undefined => {
    const current = buffers.get(tabId);
    if (!current) return undefined;
    const next = { ...current, ...change };
    buffers.set(tabId, next);
    return next;
  };

  /** Dirty is "the text is not what is on disk", so typing a letter and deleting it is not unsaved. */
  const isDirty = (state: EditorState, diskText: string): boolean =>
    state.doc.length !== diskText.length || state.doc.toString() !== diskText;

  const loadInto = async (tabId: string): Promise<void> => {
    const entry = buffers.get(tabId);
    if (!entry) return;
    try {
      const file = await io.read(entry.workspaceId, entry.path);
      if (buffers.get(tabId) !== entry && !buffers.has(tabId)) return;
      const normalized = normalizeForEditing(file.content);
      if ("error" in normalized) {
        put(tabId, { status: "error", error: normalized.error, state: EMPTY, diskText: "", dirty: false });
      } else {
        put(tabId, {
          status: "ready",
          error: undefined,
          state: EditorState.create({ doc: normalized.text }),
          diskText: normalized.text,
          version: file.version,
          eol: normalized.eol,
          bom: normalized.bom,
          dirty: false,
          aiRanges: [],
          aiChanged: false,
          conflict: null,
          saveError: undefined,
          epoch: entry.epoch + 1,
        });
      }
    } catch (error) {
      put(tabId, { status: "error", error: plain(error), state: EMPTY, diskText: "", dirty: false });
    }
    publish();
  };

  const store: EditorBuffers = {
    open(tabId, file) {
      const existing = buffers.get(tabId);
      if (existing && existing.status !== "error") return loading.get(tabId) ?? Promise.resolve();
      buffers.set(tabId, {
        tabId,
        workspaceId: file.workspaceId,
        path: file.path,
        status: "loading",
        state: EMPTY,
        diskText: "",
        version: "",
        eol: "lf",
        bom: false,
        dirty: false,
        aiRanges: [],
        aiChanged: false,
        conflict: null,
        saving: false,
        revealToken: existing?.revealToken ?? 0,
        epoch: existing?.epoch ?? 0,
      });
      publish();
      const promise = loadInto(tabId).finally(() => void loading.delete(tabId));
      loading.set(tabId, promise);
      return promise;
    },

    get: (tabId) => buffers.get(tabId),

    find(workspaceId, path) {
      for (const buffer of buffers.values()) {
        if (buffer.workspaceId === workspaceId && buffer.path === path) return buffer;
      }
      return undefined;
    },

    close(tabId) {
      buffers.delete(tabId);
      views.delete(tabId);
      loading.delete(tabId);
      saves.delete(tabId);
      publish();
    },

    subscribe(listener) {
      listeners.add(listener);
      return () => void listeners.delete(listener);
    },

    getSnapshot: () => snapshot,

    unsavedIds: (snap) => [...snap.byTab.values()].filter((b) => b.status === "ready" && b.dirty).map((b) => b.tabId),

    assistantEditedIds: (snap) =>
      [...snap.byTab.values()].filter((b) => b.status === "ready" && b.aiChanged).map((b) => b.tabId),

    lineCount(tabId) {
      const buffer = buffers.get(tabId);
      return buffer ? realLines(buffer.state) : 0;
    },

    attachView(tabId, view) {
      if (view) views.set(tabId, view);
      else views.delete(tabId);
    },

    applyTransactions(tabId, transactions) {
      const buffer = buffers.get(tabId);
      const last = transactions.at(-1);
      if (!buffer || !last) return;
      let ranges: readonly AiRange[] = buffer.aiRanges;
      let aiChanged = buffer.aiChanged;
      for (const tr of transactions) {
        if (tr.docChanged) {
          // A range follows the text it covers. Its ends lean *inward*, so typing at either edge lands outside it and
          // typing inside it grows it. A range that a change covers whole (the person selected it and typed) is
          // dropped: mapping alone would keep it, stretched over their text, and mark what they wrote as the assistant's.
          const covered: Array<[number, number]> = [];
          tr.changes.iterChanges((fromA, toA) => void covered.push([fromA, toA]));
          ranges = ranges
            .filter((r) => !covered.some(([fromA, toA]) => fromA <= r.from && toA >= r.to))
            .map((r) => ({ from: tr.changes.mapPos(r.from, 1), to: tr.changes.mapPos(r.to, -1) }))
            .filter((r) => r.from < r.to);
        }
        const added = tr.annotation(aiEdit);
        if (added) {
          ranges = [...ranges, ...added.filter((r) => r.from < r.to)];
          aiChanged = true;
        }
      }
      const dirty = isDirty(last.state, buffer.diskText);
      // Back to what is on disk: nothing is unsaved, so nothing is "changed by the assistant" either.
      if (!dirty && (ranges.length > 0 || aiChanged)) {
        ranges = [];
        aiChanged = false;
      }
      // The same array when nothing about the ranges changed (a selection moved, a view refreshed): a pane watches
      // this to know when to redraw, and a new array on every transaction would make that a loop.
      put(tabId, { state: last.state, dirty, aiRanges: ranges, aiChanged });
      publish();
    },

    edit(tabId, { oldText, newText, count }) {
      const buffer = buffers.get(tabId);
      if (!buffer) return { ok: false, reason: "That file is not open in the editor." };
      if (buffer.status !== "ready") {
        return {
          ok: false,
          reason:
            buffer.status === "loading"
              ? `${buffer.path} is still loading in the editor.`
              : `${buffer.path} could not be opened in the editor: ${buffer.error ?? "unknown reason"}`,
        };
      }
      // What the model types has LF line breaks; the buffer is LF whatever the file is on disk.
      const find = oldText.replace(/\r\n/g, "\n");
      const put_ = newText.replace(/\r\n/g, "\n");
      const text = buffer.state.doc.toString();
      const at: number[] = [];
      for (let i = text.indexOf(find); i !== -1; i = text.indexOf(find, i + find.length)) at.push(i);
      if (at.length === 0) {
        return {
          ok: false,
          reason: `That text is not in ${buffer.path} as the editor holds it. Read it with read_editor and copy old_text exactly.`,
        };
      }
      if (count > 0 && at.length !== count) {
        return {
          ok: false,
          reason:
            `old_text occurs ${at.length} times in ${buffer.path}, not ${count}. Make it longer so it is unique, ` +
            "or say how many there are with count (0 means every one).",
        };
      }
      const changes = at.map((from) => ({ from, to: from + find.length, insert: put_ }));
      // Where the new text lands, in the new document's coordinates: each earlier replacement has shifted the later ones.
      let delta = 0;
      const ranges = changes.map((c) => {
        const from = c.from + delta;
        delta += c.insert.length - (c.to - c.from);
        return { from, to: from + c.insert.length };
      });
      const spec: TransactionSpec = { changes, annotations: aiEdit.of(ranges) };
      const tr = buffer.state.update(spec);
      // Counted in bytes, as the engine counts. Only a document big enough to be over by three bytes a character is worth encoding.
      if (tr.newDoc.length * 3 > MAX_EDIT_BYTES && byteLength(tr.newDoc.toString()) > MAX_EDIT_BYTES) {
        return {
          ok: false,
          reason: `That edit would make ${buffer.path} too large: the editor holds files up to ${MAX_EDIT_BYTES.toLocaleString("en-US")} bytes.`,
        };
      }
      const first = ranges[0];
      const lastRange = ranges[ranges.length - 1];
      const fromLine = tr.newDoc.lineAt(first.from).number;
      const toLine = tr.newDoc.lineAt(Math.max(lastRange.to - (lastRange.to > lastRange.from && put_.endsWith("\n") ? 1 : 0), lastRange.from)).number;
      const view = views.get(tabId);
      if (view) view.dispatch(spec);
      else store.applyTransactions(tabId, [tr]);
      return { ok: true, replaced: at.length, fromLine, toLine };
    },

    selectLines(tabId, from, to) {
      const buffer = buffers.get(tabId);
      if (!buffer || buffer.status !== "ready") return;
      const { doc } = buffer.state;
      const last = Math.max(1, realLines(buffer.state));
      const start = Math.min(Math.max(1, Math.floor(from)), last);
      const end = Math.min(Math.max(start, Math.floor(to ?? start)), last);
      const selection = EditorSelection.single(doc.line(start).from, doc.line(end).to);
      const view = views.get(tabId);
      if (view) view.dispatch({ selection });
      else put(tabId, { state: buffer.state.update({ selection }).state });
      put(tabId, { revealToken: (buffers.get(tabId)?.revealToken ?? 0) + 1 });
      publish();
    },

    save(tabId, options = {}) {
      const running = saves.get(tabId);
      if (running) return running;
      const buffer = buffers.get(tabId);
      if (!buffer || buffer.status !== "ready") return Promise.resolve({ ok: false, error: "That file is not open." });
      if (!buffer.dirty) return Promise.resolve({ ok: true });
      const text = buffer.state.doc.toString();
      const base =
        options.overwrite && buffer.conflict?.kind === "changed" ? buffer.conflict.currentVersion : buffer.version;
      put(tabId, { saving: true, saveError: undefined });
      publish();
      const run = (async (): Promise<SaveOutcome> => {
        try {
          const saved = await io.save(buffer.workspaceId, {
            path: buffer.path,
            content: serializeForDisk(text, buffer),
            base_version: base,
          });
          const now = buffers.get(tabId);
          if (!now) return { ok: true };
          put(tabId, {
            diskText: text,
            version: saved.version,
            conflict: null,
            aiRanges: [],
            aiChanged: false,
            dirty: isDirty(now.state, text),
          });
          return { ok: true };
        } catch (error) {
          if (codeOf(error) === "file_changed") {
            const current = (error as { extra?: { current_version?: unknown } }).extra?.current_version;
            put(tabId, { conflict: { kind: "changed", currentVersion: typeof current === "string" ? current : "" } });
            return { ok: false, conflict: true };
          }
          put(tabId, { saveError: plain(error) });
          return { ok: false, error: plain(error) };
        } finally {
          put(tabId, { saving: false });
          saves.delete(tabId);
          publish();
        }
      })();
      saves.set(tabId, run);
      return run;
    },

    async reload(tabId) {
      const buffer = buffers.get(tabId);
      if (!buffer) return { ok: false, error: "That file is not open." };
      try {
        const file = await io.read(buffer.workspaceId, buffer.path);
        const normalized = normalizeForEditing(file.content);
        if ("error" in normalized) return { ok: false, error: normalized.error };
        const current = buffers.get(tabId);
        if (!current) return { ok: true };
        put(tabId, {
          state: EditorState.create({ doc: normalized.text }),
          diskText: normalized.text,
          version: file.version,
          eol: normalized.eol,
          bom: normalized.bom,
          dirty: false,
          aiRanges: [],
          aiChanged: false,
          conflict: null,
          saveError: undefined,
          epoch: current.epoch + 1,
        });
        // A pane showing it must start over from the new text, and `epoch` is how it knows.
        publish();
        return { ok: true };
      } catch (error) {
        if (codeOf(error) === "file_missing") {
          put(tabId, { conflict: { kind: "deleted" } });
          publish();
        }
        return { ok: false, error: plain(error) };
      }
    },

    async noteDiskChange(workspaceId, paths) {
      const touched = [...buffers.values()].filter(
        (b) => b.workspaceId === workspaceId && b.status === "ready" && paths.includes(b.path),
      );
      for (const buffer of touched) {
        try {
          const file = await io.read(buffer.workspaceId, buffer.path);
          const now = buffers.get(buffer.tabId);
          if (!now || file.version === now.version) continue;
          if (!now.dirty) await store.reload(buffer.tabId);
          else {
            put(buffer.tabId, { conflict: { kind: "changed", currentVersion: file.version } });
            publish();
          }
        } catch (error) {
          if (codeOf(error) === "file_missing") {
            put(buffer.tabId, { conflict: { kind: "deleted" } });
            publish();
          }
        }
      }
    },

    keepMine(tabId) {
      const buffer = buffers.get(tabId);
      if (!buffer?.conflict) return;
      // Adopting the version that is on disk is what makes the next Save an overwrite rather than another conflict.
      put(tabId, {
        version: buffer.conflict.kind === "changed" ? buffer.conflict.currentVersion : buffer.version,
        conflict: null,
      });
      publish();
    },
  };
  return store;
}
