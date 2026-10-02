/**
 * Putting a buffer on screen: the one module that imports CodeMirror's view, and so the one that is loaded lazily.
 *
 * `EditorPane` is eager (the shell mounts it), and everything heavy is behind `import("../editorMount")`, the way the
 * terminal's xterm is, so an app with no editor open pays nothing for one.
 *
 * What it does is small and all of it is about **where the text lives**. The store (`editorBuffers.ts`) holds the text, and
 * the view is a window onto it, so:
 *
 *  - the view is built from the buffer's state when that state already carries these extensions (a tab shown before: the
 *    undo history comes with it) and from the buffer's text otherwise;
 *  - every transaction goes to the store *first* (so the assistant's ranges are already mapped when the view redraws) and
 *    then to the view;
 *  - the view is attached to the store while it is mounted, so an assistant edit goes through it and joins the undo history,
 *    and detached on the way out so a stale view is never written to.
 */
import { Compartment, EditorState, StateField } from "@codemirror/state";
import type { Extension } from "@codemirror/state";
import {
  Decoration,
  EditorView,
  ViewPlugin,
  drawSelection,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
} from "@codemirror/view";
import type { DecorationSet } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, isolateHistory } from "@codemirror/commands";
import { HighlightStyle, bracketMatching, indentOnInput, syntaxHighlighting } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";
import { aiEdit } from "./editorBuffers";
import type { EditorBuffers } from "./editorBuffers";
import { loadLanguage } from "./editorLanguages";
import { SYNTAX, UI, rgb } from "./editorTheme";

/** The same stack the terminal uses: code is always monospace (`DESIGN.md`). */
const MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";

/** Present in every state this module builds, so a stored state can be told from a bare one. */
const ours = StateField.define<true>({ create: () => true, update: (value) => value });

/** The language slot. One object shared by every state: each state holds its own value in it. */
const language = new Compartment();

const theme = EditorView.theme({
  "&": {
    height: "100%",
    color: rgb(UI.text),
    backgroundColor: rgb(UI.background),
    // rem, so the UI scale moves it with the rest of the text.
    fontSize: "0.8125rem",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: MONO, lineHeight: "1.55", overflow: "auto" },
  ".cm-content": { caretColor: rgb(UI.caret), padding: "0.5rem 0" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: rgb(UI.caret) },
  ".cm-gutters": {
    color: rgb(UI.gutterText),
    backgroundColor: rgb(UI.gutter),
    border: "none",
    borderRight: `1px solid ${rgb(UI.border)}`,
  },
  ".cm-activeLine": { backgroundColor: rgb(UI.activeLine, 0.06) },
  ".cm-activeLineGutter": { backgroundColor: rgb(UI.activeLine, 0.06), color: rgb(UI.text) },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, ::selection": {
    backgroundColor: rgb(UI.selection, 0.3),
  },
  ".cm-matchingBracket": { backgroundColor: rgb(UI.match, 0.25), outline: "none" },
  ".cm-nonmatchingBracket": { color: rgb(SYNTAX.invalid) },
  ".cm-ai-edit": {
    backgroundColor: rgb(UI.assistant, 0.18),
    boxShadow: `inset 0 -1px ${rgb(UI.assistant, 0.6)}`,
    borderRadius: "2px",
  },
});

const highlighting = syntaxHighlighting(
  HighlightStyle.define([
    { tag: [t.keyword, t.controlKeyword, t.moduleKeyword, t.operatorKeyword, t.definitionKeyword], color: rgb(SYNTAX.keyword) },
    { tag: [t.string, t.special(t.string), t.regexp], color: rgb(SYNTAX.string) },
    { tag: [t.number, t.bool, t.null, t.atom], color: rgb(SYNTAX.number) },
    { tag: [t.comment, t.lineComment, t.blockComment], color: rgb(SYNTAX.comment), fontStyle: "italic" },
    { tag: [t.function(t.variableName), t.function(t.propertyName), t.definition(t.function(t.variableName))], color: rgb(SYNTAX.function) },
    { tag: [t.typeName, t.className, t.namespace], color: rgb(SYNTAX.type) },
    { tag: [t.propertyName, t.attributeName], color: rgb(SYNTAX.property) },
    { tag: [t.operator, t.punctuation, t.bracket], color: rgb(SYNTAX.punctuation) },
    { tag: t.heading, color: rgb(SYNTAX.keyword), fontWeight: "bold" },
    { tag: t.strong, fontWeight: "bold" },
    { tag: t.emphasis, fontStyle: "italic" },
    { tag: t.link, color: rgb(SYNTAX.function), textDecoration: "underline" },
    { tag: t.invalid, color: rgb(SYNTAX.invalid) },
  ]),
);

/** Colour the assistant's text, from the ranges the store holds (already mapped through whatever just happened). */
function assistantMarks(buffers: EditorBuffers, tabId: string): Extension {
  const mark = Decoration.mark({ class: "cm-ai-edit" });
  const build = (): DecorationSet => {
    // (A range beyond the end of the text is ignored by CodeMirror, not an error, so there is nothing to clamp.)
    const ranges = (buffers.get(tabId)?.aiRanges ?? []).filter((r) => r.from < r.to).sort((a, b) => a.from - b.from);
    return Decoration.set(ranges.map((r) => mark.range(r.from, r.to)));
  };
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;
      constructor() {
        this.decorations = build();
      }
      // Every update, because the ranges live in the store and not in the view: whatever changed, they are re-read.
      update() {
        this.decorations = build();
      }
    },
    { decorations: (plugin) => plugin.decorations },
  );
}

function extensionsFor(buffers: EditorBuffers, tabId: string, path: string): Extension[] {
  return [
    ours,
    theme,
    highlighting,
    lineNumbers(),
    highlightActiveLine(),
    highlightActiveLineGutter(),
    drawSelection(),
    bracketMatching(),
    indentOnInput(),
    history(),
    // The assistant's edit is its own undo step, not folded into whatever the person was just typing.
    EditorState.transactionExtender.of((tr) => (tr.annotation(aiEdit) ? { annotations: isolateHistory.of("full") } : null)),
    assistantMarks(buffers, tabId),
    language.of([]),
    EditorView.contentAttributes.of({ "aria-label": `Editing ${path}` }),
    keymap.of([
      // Ctrl+S in the pane, and only there: it is not a window-level chord, so nothing else has to give it up.
      {
        key: "Mod-s",
        run: () => {
          void buffers.save(tabId);
          // `true` is also what stops the browser's own Save-page dialog: CodeMirror prevents the default of a key it handled.
          return true;
        },
      },
      ...defaultKeymap,
      ...historyKeymap,
    ]),
  ];
}

export interface MountedEditor {
  readonly view: EditorView;
  /** Redraw the assistant's marks (the store changed which ranges there are without changing any text). */
  refresh(): void;
  /** Scroll the selection into view. */
  reveal(): void;
  destroy(): void;
}

export async function mountEditor(options: {
  parent: HTMLElement;
  buffers: EditorBuffers;
  tabId: string;
  autoFocus: boolean;
}): Promise<MountedEditor | null> {
  const { parent, buffers, tabId, autoFocus } = options;
  const buffer = buffers.get(tabId);
  if (!buffer || buffer.status !== "ready") return null;
  const reusable = buffer.state.field(ours, false) === true;
  const state = reusable
    ? buffer.state
    : EditorState.create({
        doc: buffer.state.doc,
        selection: buffer.state.selection,
        extensions: extensionsFor(buffers, tabId, buffer.path),
      });
  const view = new EditorView({
    state,
    parent,
    dispatchTransactions(transactions, v) {
      // The store first, so the assistant's ranges are mapped before the view redraws them.
      buffers.applyTransactions(tabId, transactions);
      v.update(transactions);
    },
  });
  buffers.attachView(tabId, view);
  if (autoFocus) view.focus();
  // The grammar arrives after the text is already on screen: reading it should not wait for a download.
  void loadLanguage(buffer.path).then((loaded) => {
    if (view.dom.isConnected) view.dispatch({ effects: language.reconfigure(loaded ?? []) });
  });
  return {
    view,
    refresh: () => view.dispatch({}),
    reveal: () => view.dispatch({ effects: EditorView.scrollIntoView(view.state.selection.main, { y: "center" }) }),
    destroy() {
      buffers.attachView(tabId, null);
      view.destroy();
    },
  };
}
