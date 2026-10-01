/**
 * A small, safe Markdown parser: text in, a tree out. No HTML, no DOM, no React.
 *
 * ## Why it is written here and not imported
 *
 * An answer from a model is Markdown, and until this existed it was drawn raw: `##`, `**`, and
 * fences all showing. The usual fix is `marked` plus `DOMPurify` and `dangerouslySetInnerHTML`, and
 * this app has none of those: no Markdown library exists even transitively, and nothing in
 * `ui/src` sets inner HTML. Adding the one escape hatch that turns a model's reply (which can be
 * steered by any file or web page it read) into live markup is a bad trade for headings.
 *
 * So the safety here is structural. The parser produces *data*; `components/Markdown.tsx` turns the
 * data into React elements; React escapes every string it is given. There is no step at which text
 * is interpreted as markup, so there is nothing to sanitise and nothing to forget to sanitise.
 * `<script>` in an answer is the literal characters `<script>`.
 *
 * ## What it accepts, and the deliberate omissions
 *
 * Headings, paragraphs, bullet and numbered lists (nested by indentation), fenced code,
 * blockquotes, rules, inline code, bold, italic, strikethrough, links, images and bare URLs.
 *
 * - **Single newlines are line breaks.** CommonMark folds them into a space; this does not, because
 *   plain-text answers were drawn with `whitespace-pre-wrap` until now and a model that wrote
 *   `line one\nline two` meant two lines. It is also what lets a pipe table, which this does not
 *   parse, degrade into rows a person can still read.
 * - **No raw HTML.** `<b>x</b>` is text. A model that wants bold writes `**`.
 * - **No tables, footnotes, setext headings or reference links.** Each degrades to readable text.
 * - **An unterminated fence runs to the end of the input.** The streamed snapshot of a reply is
 *   re-parsed on every update, so "half a code block" is the normal state of a reply in flight, and
 *   it has to look like code while it arrives rather than like a pile of backticks that snaps into
 *   shape at the end.
 * - **Links and images are data only.** A link carries the raw `href`; `markdownLinks.ts` decides
 *   whether it may be opened. An image is never fetched: the renderer shows its alt text.
 *
 * ## Bounded work
 *
 * The input is untrusted and can be large, so nothing here is quadratic in the length of a line:
 * an opener with no closer is remembered, so a run of ten thousand unmatched `*` is one failed
 * search and not ten thousand. Nesting is capped (`MAX_DEPTH`), and past the cap the rest is plain
 * text. `markdown.test.ts` feeds it megabyte-sized hostile inputs against a time budget.
 */

export type Inline =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "strong"; children: Inline[] }
  | { kind: "em"; children: Inline[] }
  | { kind: "del"; children: Inline[] }
  | { kind: "link"; href: string; children: Inline[] }
  | { kind: "image"; alt: string; src: string }
  | { kind: "br" };

export interface ListItem {
  inlines: Inline[];
  /** Lists nested under this item, by indentation. */
  children: Block[];
}

export type Block =
  | { kind: "heading"; level: 1 | 2 | 3 | 4 | 5 | 6; inlines: Inline[] }
  | { kind: "paragraph"; inlines: Inline[] }
  | { kind: "list"; ordered: boolean; start: number; items: ListItem[] }
  | { kind: "code"; lang: string; text: string; closed: boolean }
  | { kind: "quote"; blocks: Block[] }
  | { kind: "rule" };

/** How deep quotes, lists and emphasis may nest before the rest is read as plain text. */
export const MAX_DEPTH = 8;

// ── inline ──────────────────────────────────────────────────────────────────

const isSpace = (c: string | undefined): boolean => c === undefined || /\s/.test(c);
const isWord = (c: string | undefined): boolean => c !== undefined && /[\p{L}\p{N}]/u.test(c);

/** Adds text to a run, merging with a text node already at the end so `a\*b` is one node. */
function pushText(out: Inline[], text: string): void {
  if (!text) return;
  const last = out[out.length - 1];
  if (last && last.kind === "text") last.text += text;
  else out.push({ kind: "text", text });
}

/**
 * The index of the closing run of `delim` for an opener whose content starts at `from`, or -1.
 *
 * A closer is a run of exactly `delim.length` of the delimiter that follows a non-space (so
 * `* not emphasis *` stays text), and for underscores does not sit inside a word (`snake_case_name`
 * is a name, not italics). A longer or shorter run is skipped whole, so the second star of a `**`
 * inside an `*em*` is never mistaken for the end of it. A backslash escapes the next character, and
 * an inline code span is skipped whole so a `*` inside backticks closes nothing.
 */
function findCloser(src: string, from: number, delim: string): number {
  const ch = delim[0]!;
  let i = from;
  while (i < src.length) {
    const c = src[i]!;
    if (c === "\\") {
      i += 2;
      continue;
    }
    if (c === "`") {
      let run = 1;
      while (src[i + run] === "`") run += 1;
      const close = src.indexOf("`".repeat(run), i + run);
      i = close >= 0 ? close + run : i + run;
      continue;
    }
    if (c === ch) {
      let run = 1;
      while (src[i + run] === ch) run += 1;
      if (run === delim.length && i > from && !isSpace(src[i - 1])) {
        const intraword = ch === "_" && isWord(src[i + run]);
        if (!intraword) return i;
      }
      i += run;
      continue;
    }
    i += 1;
  }
  return -1;
}

/** The longest link label and URL that are read as a link; longer is text. */
const MAX_LABEL = 500;
const MAX_URL = 2000;

/** The end of a `[label](url)` starting at `open` (the `[`), as `{ label, url, end }`, or null. */
function matchLink(
  src: string,
  open: number,
): { label: string; url: string; end: number } | null {
  let depth = 0;
  let i = open;
  // Bounded: a label longer than this is prose with a bracket in it, and an unbounded scan from
  // every `[` of a hostile `[[[[[…` would be quadratic.
  const limit = Math.min(src.length, open + MAX_LABEL);
  for (; i < limit; i += 1) {
    const c = src[i]!;
    if (c === "\\") {
      i += 1;
      continue;
    }
    if (c === "[") depth += 1;
    else if (c === "]") {
      depth -= 1;
      if (depth === 0) break;
    }
    // A label does not span a blank line, which keeps a stray `[` from eating a paragraph.
    else if (c === "\n" && src[i + 1] === "\n") return null;
  }
  if (depth !== 0 || i >= limit || src[i + 1] !== "(") return null;
  const label = src.slice(open + 1, i);
  let j = i + 2;
  let parens = 0;
  let url = "";
  if (src[j] === "<") {
    const close = src.indexOf(">", j + 1);
    if (close < 0 || close - j > MAX_URL || src.slice(j + 1, close).includes("\n")) return null;
    url = src.slice(j + 1, close);
    j = close + 1;
  } else {
    const start = j;
    const urlLimit = Math.min(src.length, j + MAX_URL);
    for (; j < urlLimit; j += 1) {
      const c = src[j]!;
      if (c === "\n" || c === " ") break;
      if (c === "(") parens += 1;
      else if (c === ")") {
        if (parens === 0) break;
        parens -= 1;
      }
    }
    url = src.slice(start, j);
  }
  // An optional "title" is read and dropped.
  const title = /^\s+(?:"[^"\n]*"|'[^'\n]*')/.exec(src.slice(j));
  if (title) j += title[0].length;
  if (src[j] !== ")") return null;
  return { label, url: url.trim(), end: j + 1 };
}

/** Sticky, so a match is tried at one index without copying the rest of the input. */
const AUTOLINK = /<(https?:\/\/[^\s<>]{1,2000})>/iy;
const BARE_URL = /https?:\/\/[^\s<>]{1,2000}/iy;

/** Trailing punctuation that belongs to the sentence around a bare URL, not to the URL. */
const URL_TAIL = /[.,;:!?'")\]]+$/;

/**
 * Parse one run of inline text.
 *
 * `depth` is how many emphasis or link levels enclose it; at `MAX_DEPTH` the run is plain text.
 * `noCloser` remembers delimiters already searched for in vain *in this run*: if no `**` closes
 * after position p, none closes after any later q, so a later `**` is text without another search.
 */
export function parseInline(src: string, depth = 0): Inline[] {
  const out: Inline[] = [];
  if (depth >= MAX_DEPTH) {
    // Still honour newlines, so the cap never flattens a paragraph into one line.
    src.split("\n").forEach((line, n) => {
      if (n > 0) out.push({ kind: "br" });
      pushText(out, line);
    });
    return out;
  }
  const noCloser = new Set<string>();
  let i = 0;
  let plain = "";
  const flush = (): void => {
    pushText(out, plain);
    plain = "";
  };

  while (i < src.length) {
    const c = src[i]!;

    if (c === "\n") {
      flush();
      out.push({ kind: "br" });
      i += 1;
      continue;
    }
    if (c === "\\" && i + 1 < src.length && /[\\`*_{}[\]()#+\-.!>~|<]/.test(src[i + 1]!)) {
      plain += src[i + 1];
      i += 2;
      continue;
    }

    if (c === "`") {
      let run = 1;
      while (src[i + run] === "`") run += 1;
      const fence = "`".repeat(run);
      // Not memoised, because it cannot help: if no run of this length or longer follows, no later
      // opener of it exists either, so a failed search here is also the last one.
      const close = src.indexOf(fence, i + run);
      if (close < 0) {
        plain += fence;
        i += run;
        continue;
      }
      flush();
      let code = src.slice(i + run, close).replace(/\n/g, " ");
      // One space of padding on each side is how a span that starts or ends with a backtick is written.
      if (code.length > 2 && code.startsWith(" ") && code.endsWith(" ") && code.trim()) {
        code = code.slice(1, -1);
      }
      out.push({ kind: "code", text: code });
      i = close + run;
      continue;
    }

    if ((c === "!" && src[i + 1] === "[") || c === "[") {
      const image = c === "!";
      // Not memoised on failure: `arr[0]` is not a link, and a real link may follow it. The scan is
      // bounded instead (`MAX_LABEL`), which is what keeps a run of `[` linear.
      const link = matchLink(src, image ? i + 1 : i);
      if (link) {
        flush();
        if (image) out.push({ kind: "image", alt: link.label, src: link.url });
        else out.push({ kind: "link", href: link.url, children: parseInline(link.label, depth + 1) });
        i = link.end;
        continue;
      }
      plain += image ? "![" : "[";
      i += image ? 2 : 1;
      continue;
    }

    if (c === "<") {
      AUTOLINK.lastIndex = i;
      const auto = AUTOLINK.exec(src);
      if (auto) {
        flush();
        out.push({ kind: "link", href: auto[1]!, children: [{ kind: "text", text: auto[1]! }] });
        i += auto[0].length;
        continue;
      }
    }

    if ((c === "h" || c === "H") && !isWord(src[i - 1])) {
      BARE_URL.lastIndex = i;
      const match = BARE_URL.exec(src);
      if (match) {
        const text = match[0].replace(URL_TAIL, "");
        if (text.length > "https://".length) {
          flush();
          out.push({ kind: "link", href: text, children: [{ kind: "text", text }] });
          i += text.length;
          continue;
        }
      }
    }

    if (c === "*" || c === "_" || c === "~") {
      let run = 1;
      while (src[i + run] === c) run += 1;
      // Strikethrough is `~~`; a single tilde is text. Emphasis is one or two, and three is both.
      const want = c === "~" ? (run >= 2 ? 2 : 0) : Math.min(run, 3);
      const opens = want > 0 && !isSpace(src[i + run]) && !(c === "_" && isWord(src[i - 1]));
      if (opens) {
        const delim = c.repeat(want);
        const key = `${delim}@`;
        const close = noCloser.has(key) ? -1 : findCloser(src, i + want, delim);
        if (close >= 0) {
          flush();
          const inner = src.slice(i + want, close);
          const kids = parseInline(inner, depth + 1);
          if (want === 3) out.push({ kind: "strong", children: [{ kind: "em", children: kids }] });
          else if (want === 2) out.push({ kind: c === "~" ? "del" : "strong", children: kids });
          else out.push({ kind: "em", children: kids });
          i = close + want;
          continue;
        }
        noCloser.add(key);
      }
      plain += c.repeat(run);
      i += run;
      continue;
    }

    plain += c;
    i += 1;
  }
  flush();
  return out;
}

// ── blocks ──────────────────────────────────────────────────────────────────

const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const QUOTE = /^ {0,3}>[ \t]?/;
const ITEM = /^([ \t]*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;

const indentOf = (ws: string): number => ws.replace(/\t/g, "    ").length;

/**
 * The opening of a fenced block, or null. Hand-scanned: a regex that lets the info string overlap
 * its own tail is quadratic on one long line, and this is fed untrusted text.
 */
function openFence(line: string): { ch: string; len: number; lang: string } | null {
  let i = 0;
  while (i < 3 && line[i] === " ") i += 1;
  const ch = line[i];
  if (ch !== "`" && ch !== "~") return null;
  let len = 0;
  while (line[i + len] === ch) len += 1;
  if (len < 3) return null;
  const info = line.slice(i + len);
  // A backtick fence's info string cannot contain a backtick, or `` ```x``` `` would open a block.
  if (ch === "`" && info.includes("`")) return null;
  const word = info.trim().split(/\s+/, 1)[0] ?? "";
  return { ch, len, lang: word };
}

function isClosingFence(line: string, ch: string, len: number): boolean {
  let i = 0;
  while (i < 3 && line[i] === " ") i += 1;
  let run = 0;
  while (line[i + run] === ch) run += 1;
  return run >= len && line.slice(i + run).trim() === "";
}

/** An ATX heading as `{ level, text }`, or null. Linear: no regex over the heading's own text. */
function atxHeading(line: string): { level: 1 | 2 | 3 | 4 | 5 | 6; text: string } | null {
  let i = 0;
  while (i < 3 && line[i] === " ") i += 1;
  let level = 0;
  while (line[i + level] === "#") level += 1;
  if (level < 1 || level > 6) return null;
  const rest = line.slice(i + level);
  if (rest !== "" && rest[0] !== " " && rest[0] !== "\t") return null;
  let text = rest.trim();
  // An optional closing run of `#`, only when separated from the text by whitespace.
  let end = text.length;
  while (end > 0 && text[end - 1] === "#") end -= 1;
  if (end < text.length && (end === 0 || text[end - 1] === " " || text[end - 1] === "\t")) {
    text = text.slice(0, end).trimEnd();
  }
  return { level: level as 1 | 2 | 3 | 4 | 5 | 6, text };
}

/** Whether a line begins something other than a paragraph, and so ends one. */
function startsBlock(line: string): boolean {
  return (
    openFence(line) !== null ||
    atxHeading(line) !== null ||
    RULE.test(line) ||
    QUOTE.test(line) ||
    ITEM.test(line)
  );
}

/** Parse a document. Always returns; never throws, whatever it is given. */
export function parseMarkdown(src: string, depth = 0): Block[] {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i]!;
    if (!line.trim()) {
      i += 1;
      continue;
    }

    const fence = openFence(line);
    if (fence) {
      const body: string[] = [];
      let closed = false;
      i += 1;
      while (i < lines.length) {
        const candidate = lines[i]!;
        i += 1;
        if (isClosingFence(candidate, fence.ch, fence.len)) {
          closed = true;
          break;
        }
        body.push(candidate);
      }
      blocks.push({ kind: "code", lang: fence.lang, text: body.join("\n"), closed });
      continue;
    }

    const heading = atxHeading(line);
    if (heading) {
      blocks.push({ kind: "heading", level: heading.level, inlines: parseInline(heading.text, depth + 1) });
      i += 1;
      continue;
    }

    if (RULE.test(line)) {
      blocks.push({ kind: "rule" });
      i += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      const inner: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i]!)) {
        inner.push(lines[i]!.replace(QUOTE, ""));
        i += 1;
      }
      // Past the cap the quote's content is plain text rather than another level.
      blocks.push({
        kind: "quote",
        blocks:
          depth + 1 >= MAX_DEPTH
            ? [{ kind: "paragraph", inlines: parseInline(inner.join("\n"), MAX_DEPTH) }]
            : parseMarkdown(inner.join("\n"), depth + 1),
      });
      continue;
    }

    if (ITEM.test(line)) {
      const taken: string[] = [];
      while (i < lines.length) {
        const current = lines[i]!;
        if (!current.trim()) {
          // A blank line continues the list only if the next real line is an item or is indented under one.
          let next = i + 1;
          while (next < lines.length && !lines[next]!.trim()) next += 1;
          const following = lines[next];
          if (following !== undefined && (ITEM.test(following) || /^[ \t]{2,}\S/.test(following))) {
            i = next;
            continue;
          }
          break;
        }
        if (!ITEM.test(current) && !/^[ \t]+\S/.test(current)) {
          // An unindented, non-item line ends the list unless it is plain continuation text.
          if (startsBlock(current)) break;
          if (taken.length === 0) break;
        }
        taken.push(current);
        i += 1;
      }
      blocks.push(...listBlocks(taken, depth));
      continue;
    }

    // A paragraph: lines up to a blank line or the start of another block.
    const para: string[] = [line];
    i += 1;
    while (i < lines.length && lines[i]!.trim() && !startsBlock(lines[i]!)) {
      para.push(lines[i]!);
      i += 1;
    }
    blocks.push({ kind: "paragraph", inlines: parseInline(para.join("\n").trim(), depth + 1) });
  }
  return blocks;
}

/**
 * The list (or lists) a run of item lines makes. Deeper indentation nests under the item above, and a
 * change of marker kind (`-` then `1.`) at one level starts a new list, as it does everywhere.
 */
function listBlocks(lines: string[], depth: number): Block[] {
  interface Entry {
    indent: number;
    ordered: boolean;
    start: number;
    text: string[];
    kids: Entry[];
  }
  const roots: Entry[] = [];
  const stack: Entry[] = [];
  let last: Entry | undefined;
  for (const raw of lines) {
    const item = ITEM.exec(raw);
    if (!item) {
      // A continuation line belongs to the item above it.
      if (last && raw.trim()) last.text.push(raw.trim());
      continue;
    }
    const marker = item[2]!;
    const ordered = /\d/.test(marker);
    const entry: Entry = {
      indent: indentOf(item[1]!),
      ordered,
      start: ordered ? Number.parseInt(marker, 10) : 1,
      text: [item[3]!],
      kids: [],
    };
    while (stack.length > 0 && stack[stack.length - 1]!.indent >= entry.indent) stack.pop();
    (stack.length > 0 ? stack[stack.length - 1]!.kids : roots).push(entry);
    stack.push(entry);
    last = entry;
  }

  /** An entry and everything under it as lines of text, for nesting past the cap. */
  const flatten = (e: Entry): string[] => [...e.text, ...e.kids.flatMap(flatten)];

  const group = (entries: Entry[], level: number): Block[] => {
    const out: Block[] = [];
    for (const e of entries) {
      const capped = level + 1 >= MAX_DEPTH;
      const item: ListItem = {
        inlines: parseInline((capped ? flatten(e) : e.text).join("\n"), level + 1),
        children: capped ? [] : group(e.kids, level + 1),
      };
      const prev = out[out.length - 1];
      if (prev && prev.kind === "list" && prev.ordered === e.ordered) prev.items.push(item);
      else out.push({ kind: "list", ordered: e.ordered, start: e.start, items: [item] });
    }
    return out;
  };
  return group(roots, depth);
}

// ── plain text ──────────────────────────────────────────────────────────────

/** The words of a run of inlines, with no markup: what a person would read aloud. */
export function inlinePlainText(inlines: readonly Inline[]): string {
  return inlines
    .map((n) => {
      switch (n.kind) {
        case "text":
        case "code":
          return n.text;
        case "image":
          return n.alt;
        case "br":
          return "\n";
        default:
          return inlinePlainText(n.children);
      }
    })
    .join("");
}
