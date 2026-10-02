/**
 * Markdown, mounted: what an answer draws, and what hostile text cannot make it draw.
 *
 * `markdown.test.ts` owns the parser. This is the half that matters for safety, because a safe tree
 * is only safe if the renderer keeps it so: the model's words reach the page here, and these tests
 * look at the elements and attributes that actually exist after React has drawn them.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;

async function mount(
  dom: Dom,
  props: { text: string; onOpenLink?: (url: string) => void; inline?: boolean },
): Promise<void> {
  const { Markdown } = await import("../src/components/Markdown.tsx");
  await dom.render(h(Markdown, props));
}

const tags = (dom: Dom): string[] => [...dom.container.querySelectorAll("*")].map((e) => e.tagName.toLowerCase());
const count = (dom: Dom, selector: string): number => dom.container.querySelectorAll(selector).length;

/** Anything in the tree that could run code or load something, however it got there. */
function liveThings(dom: Dom): string[] {
  const found: string[] = [];
  const dangerous = new Set(["script", "img", "a", "iframe", "object", "embed", "link", "style", "form", "input", "video", "audio", "source", "meta", "base", "svg:image"]);
  for (const el of dom.container.querySelectorAll("*")) {
    const tag = el.tagName.toLowerCase();
    if (dangerous.has(tag)) found.push(`<${tag}>`);
    for (const attr of el.getAttributeNames()) {
      if (/^on/i.test(attr)) found.push(`${tag}[${attr}]`);
      if (["href", "src", "srcdoc", "action", "formaction", "xlink:href", "data"].includes(attr)) found.push(`${tag}[${attr}]`);
    }
  }
  return found;
}

test("an answer's structure is drawn: headings, emphasis, lists, quotes, rules and code", async () => {
  await withDom(async (dom) => {
    await mount(dom, {
      text: [
        "# Title",
        "",
        "Some **bold**, *italic* and `inline code`.",
        "",
        "- one",
        "- two",
        "  - nested",
        "",
        "3. three",
        "",
        "> quoted",
        "",
        "---",
        "",
        "```ts",
        "const x = 1;",
        "```",
      ].join("\n"),
    });
    assert.equal(count(dom, "h3"), 1, "a # heading is not drawn as a heading (h3, below the app's own headings)");
    assert.equal(dom.container.querySelector("h3")?.textContent, "Title");
    assert.equal(dom.container.querySelector("strong")?.textContent, "bold");
    assert.equal(dom.container.querySelector("em")?.textContent, "italic");
    assert.equal(dom.container.querySelector("p code")?.textContent, "inline code");
    assert.equal(count(dom, "ul > li"), 3, "bullets, with the nested one, are list items");
    assert.equal(count(dom, "ul ul"), 1, "a nested bullet is not nested");
    assert.equal(dom.container.querySelector("ol")?.getAttribute("start"), "3");
    assert.equal(count(dom, "blockquote"), 1);
    assert.equal(count(dom, "hr"), 1);
    assert.equal(dom.container.querySelector("pre code")?.textContent, "const x = 1;");
    assert.match(dom.text(), /ts\s*Copy/, "the code block has no language label and copy button");
    assert.ok(!dom.text().includes("**") && !dom.text().includes("```") && !dom.text().includes("## "), "markup survived into the text");
  });
});

test("nothing in an answer becomes a live element, whatever it says", async () => {
  await withDom(async (dom) => {
    const hostile = [
      "<script>alert(1)</script>",
      '<img src="x" onerror="alert(2)">',
      '<a href="javascript:alert(3)" onclick="alert(4)">raw anchor</a>',
      '<iframe src="https://evil.example"></iframe>',
      "[click](javascript:alert(5))",
      "[data](data:text/html,<script>alert(6)</script>)",
      "![pixel](https://tracker.example/p.png)",
      "![](javascript:alert(7))",
      "<https://auto.example/path>",
      "https://bare.example/x",
      "```html\n<script>alert(8)</script>\n```",
      "`<img src=x onerror=alert(9)>`",
      '<div style="position:fixed;inset:0">overlay</div>',
      "**<b onmouseover=alert(10)>x</b>**",
    ].join("\n\n");
    await mount(dom, { text: hostile, onOpenLink: () => {} });

    assert.deepEqual(liveThings(dom), [], "a hostile answer produced a live element or attribute");
    const text = dom.text();
    // Each hostile string is on the page as the characters it is made of, which is the whole of what
    // "not interpreted" means: it is visible, and it does nothing.
    for (const literal of [
      "<script>alert(1)</script>",
      '<img src="x" onerror="alert(2)">',
      '<a href="javascript:alert(3)" onclick="alert(4)">raw anchor</a>',
      '<iframe src="https://evil.example"></iframe>',
      '<div style="position:fixed;inset:0">overlay</div>',
      "<img src=x onerror=alert(9)>",
    ]) {
      assert.ok(text.includes(literal), `${literal} was interpreted instead of shown`);
    }
    assert.ok(text.includes("[image: pixel]"), "an image is not shown as its alt text");
    assert.ok(text.includes("[image: no description]"), "an image with no alt text is not shown as one");
    assert.equal(count(dom, "[style]"), 0, "a style attribute from the answer survived");
  });
});

test("a link opens the app's own browser tab, and only an http(s) address a browser tab can load", async () => {
  await withDom(async (dom) => {
    const opened: string[] = [];
    await mount(dom, {
      text: "See [the docs](https://example.com/docs?q=1) and <https://other.example/p>.",
      onOpenLink: (url) => opened.push(url),
    });
    const buttons = [...dom.container.querySelectorAll("button")].filter((b) => b.getAttribute("aria-label") !== "Copy code");
    assert.deepEqual(buttons.map((b) => b.textContent), ["the docs", "https://other.example/p"]);
    assert.equal(count(dom, "a"), 0, "a link is an anchor, which would navigate the app itself away");

    await dom.click(buttons[0]!);
    await dom.click(buttons[1]!);
    assert.deepEqual(opened, ["https://example.com/docs?q=1", "https://other.example/p"]);
  });
});

test("an address that is not a web page is its words, not a button", async () => {
  await withDom(async (dom) => {
    const opened: string[] = [];
    const refused = [
      "javascript:alert(1)",
      "JaVaScRiPt:alert(1)",
      "data:text/html,hi",
      "file:///etc/passwd",
      "mailto:a@b.c",
      "tauri://localhost/",
      "ftp://example.com/x",
      "example.com/bare",
      "/relative/path",
      "#anchor",
      "http://localhost:3000/",
      "http://127.0.0.1/",
      "http://0177.0.0.1/",
      "http://[::1]/",
      "http://a b.example/",
      "https://" + "a".repeat(2100) + ".example/",
    ];
    for (const href of refused) {
      // The parser stops at a space in a bare destination, so the address goes in angle brackets, which
      // lets every one of these reach the renderer as a link whose href the policy then has to judge.
      await mount(dom, { text: `[label](<${href}>)`, onOpenLink: (u) => opened.push(u) });
      assert.equal(count(dom, "button"), 0, `${JSON.stringify(href.slice(0, 60))} became a clickable link`);
      assert.ok(dom.text().includes("label") || href.includes("\n") || href.length > 2000, `the words of ${href.slice(0, 30)} vanished`);
    }
    assert.deepEqual(opened, []);
  });
});

test("without somewhere to open it, a link is its words, with the address in the tooltip", async () => {
  await withDom(async (dom) => {
    await mount(dom, { text: "[the docs](https://example.com/docs)" });
    assert.equal(count(dom, "button"), 0);
    const span = [...dom.container.querySelectorAll("span[title]")].find((s) => s.textContent === "the docs");
    assert.equal(span?.getAttribute("title"), "https://example.com/docs");
  });
});

test("a refused link says why in its tooltip, so it can still be read and copied", async () => {
  await withDom(async (dom) => {
    await mount(dom, { text: "[local](http://localhost:3000/) [js](javascript:alert(1))", onOpenLink: () => {} });
    const titles = [...dom.container.querySelectorAll("span[title]")].map((s) => s.getAttribute("title") ?? "");
    assert.ok(titles.some((t) => t.startsWith("http://localhost:3000/") && /loopback|non-loopback|refusing/.test(t)), `no reason on the localhost link: ${titles.join(" | ")}`);
    assert.ok(titles.some((t) => t.startsWith("javascript:alert(1)") && /not an http/.test(t)), `no reason on the javascript link: ${titles.join(" | ")}`);
  });
});

test("a link inside a link is not a button inside a button", async () => {
  await withDom(async (dom) => {
    await mount(dom, { text: "[outer [inner](https://in.example) text](https://out.example)", onOpenLink: () => {} });
    assert.equal(count(dom, "button button"), 0, "interactive content was nested inside a button");
  });
});

test("the copy button puts the code, and only the code, on the clipboard", async () => {
  await withDom(async (dom) => {
    const written: string[] = [];
    Object.defineProperty(dom.window.navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (text: string) => void written.push(text) },
    });
    await mount(dom, { text: "intro\n\n```bash\nmake test\nmake lint\n```\n\noutro" });
    await dom.click(dom.byLabel("Copy code"));
    await dom.settle();
    assert.deepEqual(written, ["make test\nmake lint"]);
    assert.match(dom.text(), /Copied/);
  });
});

test("when the clipboard cannot be reached the button says so instead of claiming it copied", async () => {
  await withDom(async (dom) => {
    Object.defineProperty(dom.window.navigator, "clipboard", {
      configurable: true,
      value: { writeText: async () => Promise.reject(new Error("denied")) },
    });
    // jsdom has no `execCommand`, so the fallback has nothing to do either.
    await mount(dom, { text: "```\nx\n```" });
    await dom.click(dom.byLabel("Copy code"));
    await dom.settle();
    assert.match(dom.text(), /Copy failed/);
    assert.ok(!/Copied/.test(dom.text()), "it claimed to copy when the clipboard refused");
  });
});

test("a reply in flight re-renders at every length without throwing, and half a fence already looks like code", async () => {
  await withDom(async (dom) => {
    const reply = "Here is the plan:\n\n1. **First** step\n2. Second `step`\n\n```py\nprint('hi')\nprint('there')\n```\n\nDone, see https://example.com/x.";
    for (let n = 0; n <= reply.length; n += 3) {
      await mount(dom, { text: reply.slice(0, n) });
    }
    await mount(dom, { text: reply });
    assert.equal(dom.container.querySelector("pre code")?.textContent, "print('hi')\nprint('there')");

    // Mid-stream: the fence is open, and the words so far are in a code box rather than as backticks.
    await mount(dom, { text: "Look:\n\n```py\nprint('hi')\npri" });
    assert.equal(dom.container.querySelector("pre code")?.textContent, "print('hi')\npri");
    assert.ok(!dom.text().includes("```"), "an open fence showed its backticks while it streamed");
    // Mid-word emphasis stays literal until it closes.
    await mount(dom, { text: "this is **bol" });
    assert.equal(dom.text(), "this is **bol");
  });
});

test("inline mode is one span of marks and no blocks, for a sentence", async () => {
  await withDom(async (dom) => {
    const opened: string[] = [];
    await mount(dom, { text: "Fix **the** `parse()` call, see [docs](https://example.com)", inline: true, onOpenLink: (u) => opened.push(u) });
    assert.deepEqual(
      [...new Set(tags(dom))].filter((t) => ["p", "div", "ul", "ol", "h3", "h4", "pre", "blockquote"].includes(t)),
      [],
      "inline mode drew a block element",
    );
    assert.equal(dom.container.querySelector("strong")?.textContent, "the");
    assert.equal(dom.container.querySelector("code")?.textContent, "parse()");
    await dom.click(dom.byButton("docs"));
    assert.deepEqual(opened, ["https://example.com/"]);
    // A heading marker in a sentence is a sentence, not a heading: inline mode is not a document.
    await mount(dom, { text: "# not a heading", inline: true });
    assert.equal(count(dom, "h3"), 0);
  });
});

test("emphasis and text colours are the theme's tokens, never a fixed palette", async () => {
  await withDom(async (dom) => {
    await mount(dom, { text: "# H\n\n**b** `c` [l](https://example.com) ~~d~~\n\n> q\n\n```\nx\n```", onOpenLink: () => {} });
    const classes = [...dom.container.querySelectorAll("[class]")].map((e) => e.getAttribute("class") ?? "").join(" ");
    assert.ok(!/\b(?:text|bg|border)-(?:red|green|blue|amber|gray|slate|zinc|neutral|stone|white|black)-\d{2,3}\b/.test(classes), "a Tailwind default palette class is in the output");
    assert.match(classes, /text-codify-/);
  });
});

test("a missing or non-string text draws nothing instead of throwing", async () => {
  await withDom(async (dom) => {
    // Engine payloads are loosely typed: a plan step can arrive with no description.
    for (const text of [undefined, null, 42, {}] as unknown as string[]) {
      await mount(dom, { text });
      assert.equal(dom.text(), "", `${String(text)} drew something`);
      await mount(dom, { text, inline: true });
      assert.equal(dom.text(), "");
    }
  });
});
