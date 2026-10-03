/**
 * Where a narrow centre column used to break text, and the structure that stops it.
 *
 * Rendered at the default 125% with a drawer open, the transcript was squeezed to about 400px and three
 * things were broken a letter to a line: a step's status pill (while its title kept its width), the
 * "TOKEN USAGE" label (while the totals beside it did), and the composer's placeholder, which does not
 * wrap at all and was cut mid-word. jsdom has no layout, so these pin the structure that makes each
 * right (which side is allowed to shrink, which is not) on the mounted app; the live render in real
 * Chromium is what showed them, and what confirms them.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext } from "./appHarness.ts";

const { withApp, conversation } = await import("./appHarness.ts");

const LONG_TITLE = "Split parse_line into a tokenizer and a parser with a very long title that cannot fit";

async function withRun(body: (ctx: AppContext) => Promise<void>): Promise<void> {
  await withApp(
    {
      conversations: [conversation({ id: "c1", title: "Thread" })],
      goals: [
        {
          id: "g1",
          conversation_id: "c1",
          title: "Refactor",
          status: "COMPLETED",
          mode: "normal",
          steps: [
            {
              id: "s1",
              goal_id: "g1",
              ordinal: 0,
              title: LONG_TITLE,
              description: "Do the split",
              suggested_paths: [],
              status: "COMPLETED",
              review_notes: null,
              last_agent_role: "fixer",
            },
          ],
        },
      ],
    },
    async (ctx) => {
      await ctx.dom.click(ctx.dom.container.querySelector('[data-thread-row="true"]') as Element);
      await ctx.settle();
      await ctx.settle();
      await body(ctx);
    },
  );
}

const classesOf = (el: Element | null): string[] => (el?.getAttribute("class") ?? "").split(/\s+/);
const spanWithText = (ctx: AppContext, text: string): HTMLElement | undefined =>
  [...ctx.dom.container.querySelectorAll<HTMLElement>("span, div")].find(
    (e) => e.children.length === 0 && (e.textContent ?? "").trim() === text,
  );

test("a step's title may shrink and wrap, and its status may not", async () => {
  await withRun(async (ctx) => {
    const title = [...ctx.dom.container.querySelectorAll<HTMLElement>("span")].find((e) => (e.textContent ?? "").includes(LONG_TITLE));
    assert.ok(title, "the step's title is not on screen");
    assert.ok(classesOf(title!).includes("min-w-0") && classesOf(title!).includes("break-words"), "the title cannot shrink or wrap");
    const titleBox = title!.parentElement!;
    assert.ok(classesOf(titleBox).includes("min-w-0") && classesOf(titleBox).includes("flex-1"), "the title's box does not take the spare room and give it back");

    const pill = spanWithText(ctx, "COMPLETED");
    assert.ok(pill, "the step's status is not on screen");
    assert.ok(classesOf(pill!).includes("whitespace-nowrap"), "the status pill can be broken a letter to a line");
    assert.ok(classesOf(pill!.parentElement).includes("shrink-0"), "the status cluster can be squeezed by the title");
  });
});

test("the token usage header wraps its totals instead of squeezing its label", async () => {
  await withRun(async (ctx) => {
    const label = [...ctx.dom.container.querySelectorAll<HTMLElement>("div")].find((e) => (e.textContent ?? "").trim() === "Token Usage");
    assert.ok(label, "the usage card is not on screen");
    assert.ok(classesOf(label!).includes("whitespace-nowrap"), "the label can be broken a letter to a line");
    const row = label!.parentElement;
    assert.ok(classesOf(row).includes("flex-wrap"), "the totals cannot drop to the next line when the column is narrow");
  });
});

test("the composer's placeholder is short, and the whole sentence is in its title", async () => {
  await withApp({}, async (ctx) => {
    const box = ctx.dom.byLabel("Chat prompt") as HTMLTextAreaElement;
    const placeholder = box.getAttribute("placeholder") ?? "";
    // A placeholder does not wrap, so it must fit a narrow composer on one line: about 46 characters is what the
    // 125% default leaves beside the mic and Send in a 900px window.
    assert.ok(placeholder.length > 0 && placeholder.length <= 46, `the placeholder is ${placeholder.length} characters: ${placeholder}`);
    assert.equal(box.getAttribute("title"), "Ask Codify to build, edit files, fix tests, or refactor code");
    assert.equal(box.getAttribute("aria-label"), "Chat prompt");
  });
});
