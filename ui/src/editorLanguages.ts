/**
 * Which language a file is, and loading its parser.
 *
 * Lazy by language: a person opening a Markdown file should not download the TypeScript grammar. `languageKeyFor` is the
 * table, pure and judged by the **last dot of the file's own name** (a folder called `x.py` does not make `x.py/readme` a
 * Python file, and `.gitignore` is a dotfile and not an extension of nothing). A file the table does not know is plain
 * text, which is a supported state and not a failure.
 */
import type { Extension } from "@codemirror/state";

export const LANGUAGE_KEYS = [
  "javascript",
  "jsx",
  "typescript",
  "tsx",
  "python",
  "json",
  "markdown",
  "css",
  "html",
] as const;

export type LanguageKey = (typeof LANGUAGE_KEYS)[number];

const BY_EXTENSION: Record<string, LanguageKey> = {
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "jsx",
  ts: "typescript",
  tsx: "tsx",
  py: "python",
  json: "json",
  md: "markdown",
  markdown: "markdown",
  css: "css",
  html: "html",
  htm: "html",
};

export function languageKeyFor(path: string): LanguageKey | null {
  const name = path.split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  if (dot <= 0) return null;
  return BY_EXTENSION[name.slice(dot + 1).toLowerCase()] ?? null;
}

/** The parser for a file, or null when it is plain text. Each language is its own chunk. */
export async function loadLanguage(path: string): Promise<Extension | null> {
  switch (languageKeyFor(path)) {
    case "javascript":
      return (await import("@codemirror/lang-javascript")).javascript();
    case "jsx":
      return (await import("@codemirror/lang-javascript")).javascript({ jsx: true });
    case "typescript":
      return (await import("@codemirror/lang-javascript")).javascript({ typescript: true });
    case "tsx":
      return (await import("@codemirror/lang-javascript")).javascript({ typescript: true, jsx: true });
    case "python":
      return (await import("@codemirror/lang-python")).python();
    case "json":
      return (await import("@codemirror/lang-json")).json();
    case "markdown":
      return (await import("@codemirror/lang-markdown")).markdown();
    case "css":
      return (await import("@codemirror/lang-css")).css();
    case "html":
      return (await import("@codemirror/lang-html")).html();
    default:
      return null;
  }
}
