/**
 * Rules that are about the source's text, and can only be checked as text.
 *
 * Claims about what the source's own comments say, and the one-spelling rules.
 *
 * These are static by nature: the thing under test is prose, not behaviour. A
 * comment that describes a shape the code no longer has is a defect that behaves
 * like code (the next change is written by whoever reads it) while producing no
 * failing test, no build error and no wrong pixels. Everything else that used to
 * read `App.tsx` as text now mounts it; what stays here is only what a mounted
 * app cannot say.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

test("the shell's own prose says the page is in the window, not beside it", () => {
  // Comments are load-bearing here. `App.tsx` is 3,000 lines of decisions
  // whose *reasons* live in the comments, and the next change to the browser
  // is written by whoever reads them — so a comment that describes a separate
  // OS window is a defect that behaves like code, while producing no failing
  // test, no build error and no wrong pixels.
  //
  // It happened: two comments in `App.tsx` still described the page as a
  // separate OS window, and `docs/09` §7.2's command list still named the
  // system-browser hand-off that was deleted, after the browser module had stopped
  // building one and a test was failing if it did. Nothing about the running
  // app was wrong. Everything a reader would conclude from it was.
  const app = readFileSync(
    new URL("../src/App.tsx", import.meta.url),
    "utf8"
  );
  const pane = readFileSync(
    new URL("../src/components/BrowserPane.tsx", import.meta.url),
    "utf8"
  );

  // Positive, in both files: the page is a child webview of this window. An
  // absence check alone would pass on a file that stopped talking about the
  // question at all.
  assert.match(
    app,
    /child\s+webview/,
    "App.tsx no longer says the page is a child webview of this window — the \
     shape the shell actually builds (browser/mod.rs: Window::add_child) should be \
     the shape the comment claims"
  );
  assert.match(
    pane,
    /child webview/,
    "BrowserPane.tsx no longer says its page is a child webview of the main \
     window"
  );

  // Negative, in `App.tsx`: the two stale sentences, as they read. Both are
  // refusals of a browser webview being a window of its own — the one place
  // `BrowserPane.tsx` may say it is, in the past tense, is its own history
  // note, so this scan is deliberately scoped to the file that held them.
  // (Mutation-checked: restoring either sentence fails this.)
  assert.doesNotMatch(
    app,
    /separate (?:OS )?window/i,
    "App.tsx describes the browser page as a separate OS window. It is not: \
     codify_browser_open seats a child webview of the main window over \
     BrowserPane's content area (docs/09 §7.3). A comment saying otherwise \
     sends the next change back to a shape the shell refuses to build"
  );
});

test("no surface invents its own name for an unnamed thread", () => {
  // The one-string rule. `UNTITLED_THREAD_TITLE` is imported by the tab strip
  // and the panel and defined once; a second literal "New chat" in either is a
  // tab and a row that can disagree about the same thread.
  for (const file of ["../src/tabs.ts", "../src/components/Sidebar.tsx"]) {
    const src = readFileSync(new URL(file, import.meta.url), "utf8");
    const withoutComments = src
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/^\s*\/\/.*$/gm, "");
    const literals = withoutComments.match(/"New chat"/g) ?? [];
    assert.equal(
      literals.length,
      0,
      `${file} hard-codes "New chat" instead of using UNTITLED_THREAD_TITLE`
    );
  }
});

/**
 * The calls that hand an assertion a DOM element or a list of them.
 *
 * `assert.equal(element, null)` looks harmless and is not: building the
 * `AssertionError` walks the element's object graph, and one that React mounted
 * carries fiber pointers back into the whole app. Failing once exhausted 25 GB
 * and got the developer's desktop session killed by the OS. It fails *only when
 * the assertion fails*, so a green suite proves nothing about it, which is why
 * it is a rule about the text. The safe spelling compares a boolean:
 * `assert.ok(el === null, msg)`, or `.length` for a list.
 *
 * A heuristic, on purpose: it knows the query helpers this suite uses, not every
 * variable that might hold an element.
 */
function elementAssertions(text: string): string[] {
  const call = /assert\.(?:equal|strictEqual|notEqual|notStrictEqual|deepEqual|deepStrictEqual)\(/g;
  const element = /(?:querySelector|querySelectorAll|closest|byLabel|byText|allByLabel|banner|pane|control)\((?:[^()]|\([^()]*\))*\)$|activeElement$/;
  const found: string[] = [];
  for (const m of text.matchAll(call)) {
    const args: string[] = [];
    let depth = 0;
    let quote: string | null = null;
    let cur = "";
    for (let i = (m.index ?? 0) + m[0].length; i < text.length; i++) {
      const c = text[i];
      if (quote) {
        cur += c;
        if (c === "\\") cur += text[++i];
        else if (c === quote) quote = null;
      } else if (c === '"' || c === "'" || c === "`") {
        quote = c;
        cur += c;
      } else if ("([{".includes(c)) {
        depth++;
        cur += c;
      } else if (")]}".includes(c)) {
        if (depth === 0) break;
        depth--;
        cur += c;
      } else if (c === "," && depth === 0) {
        args.push(cur.trim());
        cur = "";
        if (args.length === 2) break;
      } else cur += c;
    }
    if (args.length < 2 && cur.trim()) args.push(cur.trim());
    if (args.slice(0, 2).some((a) => element.test(a))) found.push(m[0] + args.slice(0, 2).join(", "));
  }
  return found;
}

test("no test hands a DOM element to assert.equal, whose failure exhausts the machine's memory", () => {
  // Positive control: the scan must see the shapes it exists to forbid, and pass
  // the safe ones, or a green run means it matched nothing.
  assert.equal(elementAssertions('assert.equal(dom.container.querySelector("a"), null, "m");').length, 1);
  assert.equal(elementAssertions("assert.deepEqual(dom.allByLabel('x'), []);").length, 1);
  assert.equal(elementAssertions("assert.equal(dom.window.document.activeElement, input);").length, 1);
  assert.equal(elementAssertions('assert.equal(control(ctx, "Start recording"), null);').length, 1);
  assert.equal(elementAssertions('assert.ok(control(ctx, "Start recording") === null);').length, 0);
  assert.equal(elementAssertions('assert.equal(dom.container.querySelector("a")?.textContent, "x");').length, 0);
  assert.equal(elementAssertions('assert.equal(dom.allByLabel("x").length, 0);').length, 0);
  assert.equal(elementAssertions('assert.ok(dom.container.querySelector("a") === null);').length, 0);

  const dir = new URL("./", import.meta.url);
  const offenders: string[] = [];
  for (const f of readdirSync(dir).filter((n) => n.endsWith(".ts") && n !== "sourceRules.test.ts")) {
    const code = readFileSync(new URL(f, dir), "utf8")
      .split("\n")
      .filter((line) => !/^\s*(\*|\/\/|\/\*)/.test(line))
      .join("\n");
    for (const hit of elementAssertions(code)) offenders.push(`${f}: ${hit}`);
  }
  assert.deepEqual(offenders, [], "compare a boolean or a length instead: assert.ok(el === null, msg)");
});

test("the Appearance pane decides whether a pasted file is a scheme in one place", () => {
  // Moved here from `scheme.test.ts`, whose other wiring assertions about the import
  // (that it decodes, merges, commits through the one history-recording path and
  // announces itself) are now held by mounted tests in `appearanceInteraction.test.ts`.
  // What is left is a rule about *shape* no click can observe: a second `JSON.parse`
  // in the import flow is a second implementation of "is this a scheme", and the thing
  // that decides that is precisely what `scheme.test.ts` holds.
  const source = readFileSync(
    new URL("../src/components/AppearancePane.tsx", import.meta.url),
    "utf8",
  );
  const parses = source.match(/JSON\.parse\(/g) ?? [];
  assert.equal(parses.length, 1, "there is more than one place that reads a scheme as JSON");
});
