/**
 * A deliverable's body: rendered by default, and the exact bytes one click away.
 *
 * `knowledgeCard.test.ts` and `designCard.test.ts` check what each card puts on screen; this owns the
 * toggle they share. The property that matters is that neither view can show a different document:
 * Source is the string, untouched, so the user can see precisely what the next run will read as fact.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;

const BODY = [
  "# Brand contract",
  "",
  "Use **ink** `#0d1117` on `paper`.",
  "",
  "- tokens in [the guide](https://example.com/guide)",
  "- <script>alert(1)</script>",
  "",
  "```css",
  ":root { --ink: #0d1117; }",
  "```",
  "",
].join("\n");

async function mount(dom: Dom, onOpenLink?: (url: string) => void): Promise<void> {
  const { DeliverableText } = await import("../src/components/DeliverableText.tsx");
  await dom.render(h(DeliverableText, { body: BODY, boxClassName: "max-h-64", onOpenLink }));
}

const pressed = (dom: Dom, name: string): string | null => dom.byButton(name).getAttribute("aria-pressed");

test("it opens rendered, with Rendered pressed and Source not", async () => {
  await withDom(async (dom) => {
    await mount(dom);
    assert.equal(pressed(dom, "Rendered"), "true");
    assert.equal(pressed(dom, "Source"), "false");
    assert.equal(dom.container.querySelector("h3")?.textContent, "Brand contract");
    assert.equal(dom.container.querySelector("strong")?.textContent, "ink");
    // The only <pre> is the fenced block's own; the source view is not showing.
    assert.equal(dom.container.querySelectorAll("pre").length, 1);
    assert.ok(!dom.text().includes("**ink**"));
  });
});

test("Source is the string exactly, in one preformatted block, with nothing interpreted", async () => {
  await withDom(async (dom) => {
    await mount(dom);
    await dom.click(dom.byButton("Source"));
    assert.equal(pressed(dom, "Source"), "true");
    assert.equal(pressed(dom, "Rendered"), "false");
    const pres = dom.container.querySelectorAll("pre");
    assert.equal(pres.length, 1);
    assert.equal(pres[0]!.textContent, BODY, "the source view is not the body byte for byte");
    assert.equal(dom.container.querySelectorAll("h3, strong, code, ul, button[title]").length, 0, "the source view interpreted something");
  });
});

test("the toggle goes both ways and keeps showing the same document", async () => {
  await withDom(async (dom) => {
    await mount(dom);
    await dom.click(dom.byButton("Source"));
    await dom.click(dom.byButton("Rendered"));
    assert.equal(dom.container.querySelector("h3")?.textContent, "Brand contract");
    assert.equal(pressed(dom, "Rendered"), "true");
  });
});

test("a hostile body is inert in both views", async () => {
  await withDom(async (dom) => {
    await mount(dom);
    for (const view of ["Rendered", "Source", "Rendered"]) {
      await dom.click(dom.byButton(view));
      assert.equal(dom.container.querySelectorAll("script, img, a, iframe").length, 0, `${view}: a live element appeared`);
      assert.ok(dom.text().includes("<script>alert(1)</script>"), `${view}: the script text is not shown literally`);
    }
  });
});

test("a link in the rendered body opens a browser tab through the transcript's handler", async () => {
  await withDom(async (dom) => {
    const opened: string[] = [];
    await mount(dom, (url) => opened.push(url));
    await dom.click(dom.byButton("the guide"));
    assert.deepEqual(opened, ["https://example.com/guide"]);
  });
});

test("the view control is a labelled group, so it is announced as one", async () => {
  await withDom(async (dom) => {
    await mount(dom);
    const group = dom.container.querySelector('[role="group"]');
    assert.equal(group?.getAttribute("aria-label"), "View the file as");
    assert.equal(group?.querySelectorAll("button").length, 2);
  });
});
