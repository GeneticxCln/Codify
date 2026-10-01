/**
 * A link in an answer, through the whole App: the click reaches the shell as a browser tab.
 *
 * `markdownRender.test.ts` proves a link is a button that calls `onOpenLink` with an address the
 * policy accepted. What it cannot show is that `App.tsx` does something with it, and that the something
 * is the guarded path: a real tab, a `codify_browser_open` to the shell, no window of the page's own,
 * and never the app navigating itself away. This mounts the app with a finished turn whose reply has
 * links in it, opens the thread and presses them.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { AppContext } from "./appHarness.ts";

const { withApp, conversation } = await import("./appHarness.ts");

const REPLY = [
  "## The fix",
  "",
  "See [the guide](https://example.com/guide) and <https://other.example/p>.",
  "",
  "Do not open [a script](javascript:alert(1)) or [this machine](http://localhost:8080/).",
  "",
  "```sh",
  "make test",
  "```",
].join("\n");

async function withAnswer(
  body: (ctx: AppContext) => Promise<void>,
  extra: { shellFails?: Record<string, string> } = {},
): Promise<void> {
  await withApp(
    {
      ...extra,
      viewport: { width: 1100, height: 700 },
      conversations: [conversation({ id: "c1", title: "Thread" })],
      goals: [{ id: "g1", conversation_id: "c1", title: "How do I fix it?", status: "COMPLETED", mode: "chat" }],
      goalEvents: {
        g1: [
          {
            id: "e1",
            goal_id: "g1",
            step_id: null,
            type: "log",
            payload: { turn: true, message: REPLY },
            timestamp: 2,
            sequence: 1,
          },
        ],
      },
    },
    async (ctx) => {
      const row = ctx.dom.container.querySelector('[data-thread-row="true"]');
      assert.ok(row, "the panel did not list the thread");
      await ctx.dom.click(row);
      await ctx.settle();
      await body(ctx);
    },
  );
}

const opens = (ctx: AppContext) =>
  ctx.shell.calls.flatMap((c, i) => (c === "codify_browser_open" ? [ctx.shell.args[i]!] : []));
const answerButtons = (ctx: AppContext): HTMLElement[] =>
  [...ctx.dom.container.querySelectorAll("button")].filter(
    (b) => b.closest("h3, p") !== null && b.getAttribute("title")?.startsWith("Open "),
  ) as HTMLElement[];

test("the answer is drawn as Markdown in the transcript", async () => {
  await withAnswer(async ({ dom }) => {
    // `##` is level 2, drawn two steps down (h4) so an answer's headings stay below the app's own.
    assert.equal(dom.container.querySelector("h4")?.textContent, "The fix", "the answer's heading was not drawn as one");
    assert.equal(dom.container.querySelector("pre code")?.textContent, "make test");
    assert.ok(!dom.text().includes("## The fix"), "the heading marker is on screen");
    assert.ok(!dom.text().includes("```"), "the fence is on screen");
  });
});

test("only the two web links in the answer are buttons, and the script and loopback ones are words", async () => {
  await withAnswer(async (ctx) => {
    const buttons = answerButtons(ctx);
    assert.deepEqual(
      buttons.map((b) => b.textContent),
      ["the guide", "https://other.example/p"],
    );
    assert.ok(ctx.dom.text().includes("a script") && ctx.dom.text().includes("this machine"), "a refused link lost its words");
    assert.equal(ctx.dom.container.querySelectorAll("main a").length, 0, "the answer contains an anchor that would navigate the app away");
  });
});

test("clicking a link opens the page in a new browser tab through the shell, as a popup request does", async () => {
  await withAnswer(async (ctx) => {
    const tabsBefore = ctx.tabs().length;
    const opened: string[] = [];
    (ctx.dom.window as unknown as { open: (u?: string) => null }).open = (u) => {
      opened.push(String(u));
      return null;
    };
    await ctx.dom.click(answerButtons(ctx)[0]!);
    await ctx.settle();

    assert.equal(opens(ctx).length, 1, "the click did not reach the shell");
    assert.equal(opens(ctx)[0]!.url, "https://example.com/guide");
    assert.equal(ctx.tabs().length, tabsBefore + 1, "the click did not open exactly one new tab");
    assert.deepEqual(opened, [], "the page was opened as a window of its own");
  });
});

test("a second link opens a second tab, and the first is left alone", async () => {
  await withAnswer(async (ctx) => {
    const tabsBefore = ctx.tabs().length;
    await ctx.dom.click(answerButtons(ctx)[0]!);
    await ctx.settle();
    // The transcript is behind the new tab now, so the second link is reached by going back to it.
    const thread = ctx.dom.container.querySelector('[data-thread-row="true"]');
    await ctx.dom.click(thread as Element);
    await ctx.settle();
    await ctx.dom.click(answerButtons(ctx)[1]!);
    await ctx.settle();
    assert.deepEqual(opens(ctx).map((o) => o.url), ["https://example.com/guide", "https://other.example/p"]);
    assert.equal(ctx.tabs().length, tabsBefore + 2);
  });
});

test("a page the shell refuses is reported where the person is looking, and the transcript survives", async () => {
  const refusal = "refusing to navigate to the address: browser webviews load http(s) on non-loopback hosts only";
  await withAnswer(
    async (ctx) => {
      await ctx.dom.click(answerButtons(ctx)[0]!);
      await ctx.settle();
      assert.ok(ctx.dom.text().includes(refusal), "the shell's refusal was swallowed instead of shown");
      // Back to the thread: the answer is still there and its links still work.
      await ctx.dom.click(ctx.dom.container.querySelector('[data-thread-row="true"]') as Element);
      await ctx.settle();
      assert.equal(ctx.dom.container.querySelector("h4")?.textContent, "The fix");
    },
    { shellFails: { codify_browser_open: refusal } },
  );
});
