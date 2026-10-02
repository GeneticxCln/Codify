/**
 * The editor's engine (CodeMirror 6) runs where this suite runs.
 *
 * Two facts the rest of the editor's tests lean on, so a harness change that breaks either fails here by name instead of
 * as forty unrelated failures:
 *
 *  1. `EditorState` is plain data and needs no DOM at all. The AI's eyes and hands (`editorSurface.ts`) are written against
 *     it, so they are tested headless, with no view and no jsdom.
 *  2. An `EditorView` mounts in `withDom`, given the few browser senses `dom.ts` answers: `MutationObserver`, `Range`
 *     and `Selection` from jsdom, and a range that has no geometry because there is no layout.
 *
 * What this cannot say is how an editor *looks* or *feels*: there is no layout here, so a position is never asserted.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { withDom } from "./dom.ts";

test("an editor's state is plain data: a transaction changes the text and carries its annotation", async () => {
  const { EditorState, Annotation } = await import("@codemirror/state");
  const ai = Annotation.define<boolean>();
  const before = EditorState.create({ doc: "hello world\nsecond line\n" });
  const tr = before.update({ changes: { from: 0, to: 5, insert: "HELLO" }, annotations: ai.of(true) });
  assert.equal(tr.state.doc.toString(), "HELLO world\nsecond line\n");
  assert.equal(tr.annotation(ai), true);
  assert.equal(tr.state.doc.lines, 3);
  assert.equal(before.doc.toString(), "hello world\nsecond line\n", "the state it came from is untouched");
});

test("a language package parses a document under the same loader", async () => {
  const { EditorState } = await import("@codemirror/state");
  const { javascript } = await import("@codemirror/lang-javascript");
  const { syntaxTree } = await import("@codemirror/language");
  const state = EditorState.create({ doc: "const a = 1;\nfunction f() { return a }\n", extensions: [javascript()] });
  const names: string[] = [];
  syntaxTree(state).iterate({ enter: (node) => void names.push(node.name) });
  assert.ok(names.includes("FunctionDeclaration"), `parsed as JavaScript, saw ${names.slice(0, 8).join(", ")}`);
});

test("a view mounts, shows its text, follows a dispatch and goes away cleanly", async () => {
  await withDom(async (dom) => {
    const { EditorView } = await import("@codemirror/view");
    const { EditorState } = await import("@codemirror/state");
    const view = new EditorView({
      state: EditorState.create({ doc: "one\ntwo\nthree" }),
      parent: dom.container,
    });
    assert.equal(dom.container.querySelectorAll(".cm-content").length, 1);
    view.dispatch({ changes: { from: 0, insert: "zero\n" }, selection: { anchor: 2, head: 4 } });
    assert.equal(view.state.doc.toString(), "zero\none\ntwo\nthree");
    assert.equal(view.state.selection.main.from, 2);
    assert.equal(view.state.selection.main.to, 4);
    assert.ok((dom.container.textContent ?? "").includes("zero"), "the DOM shows the change");
    view.destroy();
    assert.equal(dom.container.querySelectorAll(".cm-content").length, 0, "destroy removes the editor's DOM");
  });
});
