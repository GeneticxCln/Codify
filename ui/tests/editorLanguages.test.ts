/**
 * Which language a file is, and that each one loads and parses.
 *
 * The language packages are loaded lazily (a person opening a Markdown file should not download the Rust grammar),
 * so there are two questions: which one a path asks for (a pure table, here), and whether the package behind each answer
 * actually yields a parser that understands a line of that language (loaded under the same loader the app's tests use).
 * A file the table does not know is plain text, which is a supported state and not a failure.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { languageKeyFor, loadLanguage, LANGUAGE_KEYS } = await import("../src/editorLanguages.ts");
const { EditorState } = await import("@codemirror/state");
const { syntaxTree } = await import("@codemirror/language");

test("a path asks for the language its extension names, whatever its folder", () => {
  const table: Array<[string, string | null]> = [
    ["src/app.ts", "typescript"],
    ["src/App.tsx", "tsx"],
    ["lib/util.js", "javascript"],
    ["lib/util.mjs", "javascript"],
    ["lib/util.cjs", "javascript"],
    ["lib/View.jsx", "jsx"],
    ["scripts/run.py", "python"],
    ["package.json", "json"],
    ["tsconfig.jsonc", null],
    ["docs/README.md", "markdown"],
    ["docs/notes.markdown", "markdown"],
    ["ui/src/index.css", "css"],
    ["ui/index.html", "html"],
    ["ui/page.htm", "html"],
    ["Makefile", null],
    ["LICENSE", null],
    ["notes.txt", null],
    [".gitignore", null],
    ["a.b/c", null],
  ];

  for (const [path, key] of table) assert.equal(languageKeyFor(path), key, path);
});

test("the extension is judged by its last dot and without regard to case", () => {
  assert.equal(languageKeyFor("A.PY"), "python");
  assert.equal(languageKeyFor("dir.py/readme"), null, "a folder called x.py is not a Python file");
  assert.equal(languageKeyFor("archive.tar.js"), "javascript");
  assert.equal(languageKeyFor("py"), null);
  assert.equal(languageKeyFor("src/.py"), null, "a dotfile in a folder is still a dotfile, not a Python file called nothing");
  assert.equal(languageKeyFor(".py"), null, "a dotfile is named for what follows the dot, not an extension of nothing");
});

test("a file with no language loads as plain text", async () => {
  assert.equal(await loadLanguage("Makefile"), null);
  assert.equal(await loadLanguage("notes.txt"), null);
});

const SAMPLES: Record<string, { source: string; node: string }> = {
  javascript: { source: "const a = 1;\nfunction f() { return a }\n", node: "FunctionDeclaration" },
  jsx: { source: "const x = <div className='a'>hi</div>;\n", node: "JSXElement" },
  typescript: { source: "interface A { b: number }\nconst c: A = { b: 1 };\n", node: "InterfaceDeclaration" },
  tsx: { source: "const x: number = 1;\nconst y = <b>{x}</b>;\n", node: "JSXElement" },
  python: { source: "def f(x):\n    return x + 1\n", node: "FunctionDefinition" },
  json: { source: '{ "a": [1, 2, 3] }\n', node: "Array" },
  markdown: { source: "# Title\n\nSome *text*.\n", node: "ATXHeading1" },
  css: { source: "a { color: red; }\n", node: "RuleSet" },
  html: { source: "<p class=\"x\">hi</p>\n", node: "Element" },
};

for (const key of LANGUAGE_KEYS) {
  test(`${key} loads, and its parser understands a line of ${key}`, async () => {
    const sample = SAMPLES[key];
    assert.ok(sample, `no sample for ${key}: a language was added without a test that it parses`);
    const path = { javascript: "a.js", jsx: "a.jsx", typescript: "a.ts", tsx: "a.tsx", python: "a.py", json: "a.json", markdown: "a.md", css: "a.css", html: "a.html" }[key] as string;

    const extension = await loadLanguage(path);
    assert.ok(extension, `${path} loaded no language`);
    const state = EditorState.create({ doc: sample.source, extensions: [extension] });
    const names: string[] = [];
    syntaxTree(state).iterate({ enter: (node) => void names.push(node.name) });

    assert.ok(names.includes(sample.node), `${key}: expected a ${sample.node}, saw ${[...new Set(names)].slice(0, 12).join(", ")}`);
  });
}

test("every key the table can return is one the loader knows, so no file asks for a language that is not there", () => {
  for (const key of LANGUAGE_KEYS) assert.ok(SAMPLES[key], key);
  for (const path of ["a.ts", "a.tsx", "a.js", "a.jsx", "a.py", "a.json", "a.md", "a.css", "a.html"]) {
    const key = languageKeyFor(path);
    assert.ok(key !== null && (LANGUAGE_KEYS as readonly string[]).includes(key), path);
  }
});
