/**
 * The Markdown parser: what an answer becomes, and what hostile text cannot make it do.
 *
 * Pure: text in, a tree out. The renderer and the link policy have their own files; this one is about
 * the two properties the parser owns. It reads what a model writes the way a person means it, and it
 * is bounded and total on what an attacker writes: it never throws, never loops, and never takes
 * longer than a budget however large or nasty the input.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { parseMarkdown, parseInline, inlinePlainText, MAX_DEPTH } = await import("../src/markdown.ts");
type Block = ReturnType<typeof parseMarkdown>[number];
type Inline = ReturnType<typeof parseInline>[number];

/** A compact, comparable picture of an inline run: `strong(em(x))`, `code(x)`, `link(href|text)`. */
function show(nodes: readonly Inline[]): string {
  return nodes
    .map((n): string => {
      switch (n.kind) {
        case "text":
          return JSON.stringify(n.text);
        case "code":
          return `code(${n.text})`;
        case "br":
          return "br";
        case "image":
          return `image(${n.alt}|${n.src})`;
        case "link":
          return `link(${n.href}|${show(n.children)})`;
        default:
          return `${n.kind}(${show(n.children)})`;
      }
    })
    .join(" ");
}

function blocksOf(src: string): Block[] {
  return parseMarkdown(src);
}

test("headings, paragraphs and rules", () => {
  const blocks = blocksOf("# One\n\n### Three ###\n\nbody text\n\n---\n\n#not a heading");
  assert.deepEqual(
    blocks.map((b) => b.kind + (b.kind === "heading" ? String(b.level) : "")),
    ["heading1", "heading3", "paragraph", "rule", "paragraph"],
  );
  assert.equal(show((blocks[1] as Extract<Block, { kind: "heading" }>).inlines), '"Three"', "closing # run not stripped");
  assert.equal(show((blocks[4] as Extract<Block, { kind: "paragraph" }>).inlines), '"#not a heading"');
  assert.equal(blocksOf("#")[0]?.kind, "heading", "a bare # is an empty heading, not text");
});

test("a single newline is a line break, so plain-text answers keep their lines", () => {
  const [p] = blocksOf("line one\nline two");
  assert.equal(show((p as Extract<Block, { kind: "paragraph" }>).inlines), '"line one" br "line two"');
});

test("emphasis: bold, italic, both, strike, and what is not emphasis", () => {
  assert.equal(show(parseInline("**b** *i* ***both*** ~~gone~~")), 'strong("b") " " em("i") " " strong(em("both")) " " del("gone")');
  assert.equal(show(parseInline("2 * 3 * 4")), '"2 * 3 * 4"', "spaced stars are arithmetic");
  assert.equal(show(parseInline("snake_case_name and a_b")), '"snake_case_name and a_b"', "intraword underscores are a name");
  assert.equal(show(parseInline("_em_ here")), 'em("em") " here"');
  assert.equal(show(parseInline("foo_bar_ baz")), '"foo_bar_ baz"', "an underscore inside a word opened emphasis");
  assert.equal(show(parseInline("(_em_)")), '"(" em("em") ")"', "punctuation before an underscore is not a word");
  assert.equal(show(parseInline("**bold with *nested* inside**")), 'strong("bold with " em("nested") " inside")');
  assert.equal(show(parseInline("*a **b** c*")), 'em("a " strong("b") " c")', "the ** inside * was taken for its closer");
  assert.equal(show(parseInline("price is 5~10")), '"price is 5~10"', "a single tilde is text");
});

test("an unterminated opener is literal text, which is what a streamed reply looks like mid-word", () => {
  assert.equal(show(parseInline("**half")), '"**half"');
  assert.equal(show(parseInline("some `code")), '"some `code"');
  assert.equal(show(parseInline("a [link](http://x")), '"a [link](http://x"');
});

test("inline code keeps its contents verbatim, and stars inside it do not emphasise", () => {
  assert.equal(show(parseInline("run `a*b*c` now")), '"run " code(a*b*c) " now"');
  assert.equal(show(parseInline("``tick ` inside``")), "code(tick ` inside)");
  assert.equal(show(parseInline("*not `closed*` here*")), 'em("not " code(closed*) " here")', "a star in code closed the emphasis");
});

test("escapes make the next character literal", () => {
  assert.equal(show(parseInline("\\*not bold\\* and \\`not code\\`")), '"*not bold* and `not code`"');
  assert.equal(show(parseInline("a \\ b")), '"a \\\\ b"', "a lone backslash is a backslash");
});

test("links: labelled, autolinked, bare, and the sentence punctuation around a bare URL", () => {
  assert.equal(show(parseInline("[the docs](https://a.dev/x)")), 'link(https://a.dev/x|"the docs")');
  assert.equal(show(parseInline('[t](https://a.dev "a title")')), 'link(https://a.dev|"t")');
  assert.equal(show(parseInline("see <https://a.dev/p>.")), '"see " link(https://a.dev/p|"https://a.dev/p") "."');
  assert.equal(show(parseInline("go to https://a.dev/p, then")), '"go to " link(https://a.dev/p|"https://a.dev/p") ", then"');
  assert.equal(show(parseInline("(see https://a.dev/p)")), '"(see " link(https://a.dev/p|"https://a.dev/p") ")"');
  assert.equal(show(parseInline("[a [nested] label](https://a.dev)")), 'link(https://a.dev|"a [nested] label")');
  assert.equal(show(parseInline("[wiki](https://a.dev/f_(x))")), 'link(https://a.dev/f_(x)|"wiki")');
});

test("a bracket that is not a link does not stop a later one from being a link", () => {
  assert.equal(
    show(parseInline("arr[0] and [real](https://a.dev)")),
    '"arr[0] and " link(https://a.dev|"real")',
    "a failed [ poisoned the rest of the line",
  );
  assert.equal(show(parseInline("[ [a](https://b.dev)")), '"[ " link(https://b.dev|"a")');
});

test("a link's href is carried raw, and an image is data, not a fetch", () => {
  // The parser does not judge a URL; markdownLinks.ts does. What matters here is that a hostile one
  // is *kept as data* and never interpreted.
  assert.equal(show(parseInline("[x](javascript:alert(1))")), 'link(javascript:alert(1)|"x")');
  assert.equal(show(parseInline("![alt text](https://a.dev/i.png)")), "image(alt text|https://a.dev/i.png)");
  assert.equal(inlinePlainText(parseInline("![alt text](https://a.dev/i.png)")), "alt text");
});

test("raw HTML is text: nothing is interpreted as markup", () => {
  const hostile = '<script>alert(1)</script><img src=x onerror=alert(1)><b>x</b>';
  assert.equal(show(parseInline(hostile)), JSON.stringify(hostile));
  const [p] = blocksOf("<div onclick=x>hi</div>");
  assert.equal(show((p as Extract<Block, { kind: "paragraph" }>).inlines), '"<div onclick=x>hi</div>"');
});

test("fenced code: language, tilde fences, and an unterminated fence runs to the end", () => {
  const [a] = blocksOf("```ts\nconst x = 1;\n```") as Extract<Block, { kind: "code" }>[];
  assert.deepEqual({ lang: a!.lang, text: a!.text, closed: a!.closed }, { lang: "ts", text: "const x = 1;", closed: true });

  const [open] = blocksOf("```py\nprint(1)\nstill going") as Extract<Block, { kind: "code" }>[];
  assert.deepEqual({ text: open!.text, closed: open!.closed }, { text: "print(1)\nstill going", closed: false });

  const [tilde] = blocksOf("~~~\n```\ninside\n```\n~~~") as Extract<Block, { kind: "code" }>[];
  assert.equal(tilde!.text, "```\ninside\n```", "a shorter or different fence closed a tilde fence");

  const [longer] = blocksOf("````\n```\nx\n```\n````") as Extract<Block, { kind: "code" }>[];
  assert.equal(longer!.text, "```\nx\n```", "a three-tick line closed a four-tick fence");

  assert.equal(blocksOf("```x``` is inline-ish")[0]?.kind, "paragraph", "a one-line ``` with a closing run opened a block");
});

test("code fences keep markdown-looking text literally", () => {
  const [c] = blocksOf("```\n# not a heading\n**not bold**\n- not a list\n```") as Extract<Block, { kind: "code" }>[];
  assert.equal(c!.text, "# not a heading\n**not bold**\n- not a list");
});

test("lists: bullets, numbers (with their start), nesting, continuation, and a kind change", () => {
  const [ul] = blocksOf("- a\n- b\n  continued\n  - nested 1\n  - nested 2\n- c") as Extract<Block, { kind: "list" }>[];
  assert.equal(ul!.items.length, 3);
  assert.equal(show(ul!.items[1]!.inlines), '"b" br "continued"');
  const nested = ul!.items[1]!.children[0] as Extract<Block, { kind: "list" }>;
  assert.equal(nested.items.length, 2);

  const [ol] = blocksOf("3. three\n4. four") as Extract<Block, { kind: "list" }>[];
  assert.deepEqual({ ordered: ol!.ordered, start: ol!.start, n: ol!.items.length }, { ordered: true, start: 3, n: 2 });

  const mixed = blocksOf("- bullet\n1. numbered");
  assert.deepEqual(mixed.map((b) => (b.kind === "list" ? (b.ordered ? "ol" : "ul") : b.kind)), ["ul", "ol"]);

  assert.equal(blocksOf("intro:\n- a\n- b")[1]?.kind, "list", "a list could not follow a line of prose without a blank line");
  assert.equal(blocksOf("- a\n\n- b").length, 1, "a blank line between items split the list");
  assert.equal(blocksOf("- a\n\nparagraph").length, 2);
});

test("blockquotes nest, and contain blocks", () => {
  const [q] = blocksOf("> quoted\n> more\n>\n> - item") as Extract<Block, { kind: "quote" }>[];
  assert.equal(q!.kind, "quote");
  assert.deepEqual(q!.blocks.map((b) => b.kind), ["paragraph", "list"]);
  const [outer] = blocksOf("> > deep") as Extract<Block, { kind: "quote" }>[];
  assert.equal((outer!.blocks[0] as Extract<Block, { kind: "quote" }>).kind, "quote");
});

test("a typical model answer parses to the blocks a person would see", () => {
  const answer = [
    "## The fix",
    "",
    "The bug is in `parse()`. It **never** resets the buffer, see [the issue](https://github.com/x/y/issues/1).",
    "",
    "1. Reset the buffer",
    "2. Re-run the tests:",
    "",
    "```bash",
    "make test",
    "```",
    "",
    "> Note: this changes the public API.",
  ].join("\n");
  assert.deepEqual(
    blocksOf(answer).map((b) => b.kind),
    ["heading", "paragraph", "list", "code", "quote"],
  );
});

test("empty and whitespace-only input is no blocks, and CRLF reads like LF", () => {
  assert.deepEqual(blocksOf(""), []);
  assert.deepEqual(blocksOf("  \n\t\n   "), []);
  assert.deepEqual(blocksOf("# a\r\n\r\ntext\r\n"), blocksOf("# a\n\ntext\n"));
});

test("inlinePlainText is the words without the markup", () => {
  assert.equal(inlinePlainText(parseInline("a **b** `c` [d](https://e.dev) ~~f~~")), "a b c d f");
});

// ── hostile and large input ─────────────────────────────────────────────────

/** Run `fn`, failing if it takes longer than `ms`: a pathological input must be bounded, not just correct. */
function within<T>(ms: number, label: string, fn: () => T): T {
  const started = performance.now();
  const value = fn();
  const took = performance.now() - started;
  assert.ok(took < ms, `${label} took ${took.toFixed(0)}ms (budget ${ms}ms): the parser is not bounded on it`);
  return value;
}

const BIG = 200_000;

test("hostile inputs are linear, not quadratic", () => {
  const cases: Array<[string, string]> = [
    ["unmatched stars", "*".repeat(BIG)],
    ["alternating open stars", "*a ".repeat(BIG / 3)],
    ["unmatched underscores", "_a ".repeat(BIG / 3)],
    ["unmatched tildes", "~~a ".repeat(BIG / 4)],
    ["unmatched backticks", "`".repeat(BIG)],
    ["backtick ladder", Array.from({ length: 500 }, (_, i) => "`".repeat(i + 1)).join(" a ")],
    ["open brackets", "[".repeat(BIG)],
    ["bracket then paren", "[a](".repeat(BIG / 4)],
    ["open images", "![".repeat(BIG / 2)],
    ["angle brackets", "<".repeat(BIG)],
    ["autolink starts", "<http://".repeat(BIG / 8)],
    ["bare scheme", "http://".repeat(BIG / 7)],
    ["one huge URL", "http://" + "a".repeat(BIG)],
    ["one huge word after hashes", "# " + "x".repeat(BIG)],
    ["whitespace heading", "# " + "\t".repeat(BIG) + "x"],
    ["fence with a long info string", "```" + "a".repeat(BIG) + "`"],
    ["long rule candidate", "- ".repeat(BIG / 2) + "x"],
    ["item with long indent", " ".repeat(BIG) + "x"],
    ["many one-line fences", "```\n".repeat(BIG / 4)],
    ["many blank lines", "\n".repeat(BIG)],
    ["many items", "- a\n".repeat(BIG / 4)],
    ["deep list", Array.from({ length: 2000 }, (_, i) => `${" ".repeat(i)}- x`).join("\n")],
    ["deep quote", ">".repeat(BIG)],
    ["deep quote lines", ">\n".repeat(BIG / 2)],
    ["deep nesting of emphasis", "*".repeat(5000) + "a" + "*".repeat(5000)],
    ["nested links", "[".repeat(2000) + "a" + "](x)".repeat(2000)],
  ];
  for (const [label, input] of cases) {
    within(2500, label, () => {
      const blocks = parseMarkdown(input);
      assert.ok(Array.isArray(blocks), label);
    });
  }
});

test("the parser is total: it never throws on odd or random input", () => {
  const alphabet = ["*", "_", "~", "`", "[", "]", "(", ")", "!", "<", ">", "#", "-", "+", "1.", " ", "\n", "\t", "\\", "http://a.b", "x", "```", ">", "|"];
  let seed = 12345;
  const rand = (n: number): number => {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    return seed % n;
  };
  for (let round = 0; round < 400; round += 1) {
    const input = Array.from({ length: 1 + rand(60) }, () => alphabet[rand(alphabet.length)]!).join("");
    assert.doesNotThrow(() => parseMarkdown(input), JSON.stringify(input));
  }
  const odd = ["\0", String.fromCharCode(0x2028), String.fromCharCode(0xd800), "\u{1d4b3}**\u{1d4b4}**", String.fromCharCode(0x200b) + "**zero-width**"];
  for (const input of odd) {
    assert.doesNotThrow(() => parseMarkdown(input), JSON.stringify(input));
  }
});

test("nesting is capped, and past the cap the content is still all there as text", () => {
  // The cap exists so a hostile document cannot build a tree as deep as it is long. What it must
  // not do is lose the text: the words are still in the answer.
  const deepQuote = `${">".repeat(MAX_DEPTH + 20)} the words`;
  const depthOf = (blocks: Block[]): number =>
    blocks.reduce((m, b) => Math.max(m, b.kind === "quote" ? 1 + depthOf(b.blocks) : 0), 0);
  assert.ok(depthOf(parseMarkdown(deepQuote)) <= MAX_DEPTH, "quotes nested past the cap");
  const text = (blocks: Block[]): string =>
    blocks.map((b) => (b.kind === "paragraph" ? inlinePlainText(b.inlines) : b.kind === "quote" ? text(b.blocks) : "")).join("");
  assert.match(text(parseMarkdown(deepQuote)), /the words/);

  const deepList = Array.from({ length: MAX_DEPTH + 10 }, (_, i) => `${"  ".repeat(i)}- level${i}`).join("\n");
  const listDepth = (blocks: Block[]): number =>
    blocks.reduce((m, b) => Math.max(m, b.kind === "list" ? 1 + Math.max(0, ...b.items.map((it) => listDepth(it.children))) : 0), 0);
  assert.ok(listDepth(parseMarkdown(deepList)) <= MAX_DEPTH, "lists nested past the cap");
  const flat = JSON.stringify(parseMarkdown(deepList));
  for (let i = 0; i < MAX_DEPTH + 10; i += 1) assert.ok(flat.includes(`level${i}`), `level${i} was lost past the cap`);
});

test("a long ordinary document parses fast", () => {
  const para = "Some **bold** text with `code`, a [link](https://a.dev) and more words to fill the line.\n";
  const doc = Array.from({ length: 4000 }, (_, i) => `## Heading ${i}\n\n${para}\n- item a\n- item b\n\n\`\`\`ts\nlet x = ${i};\n\`\`\`\n`).join("\n");
  const blocks = within(2500, `a ${(doc.length / 1000).toFixed(0)}kB document`, () => parseMarkdown(doc));
  assert.equal(blocks.filter((b) => b.kind === "heading").length, 4000);
});
