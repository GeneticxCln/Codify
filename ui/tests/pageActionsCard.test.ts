/**
 * Settings → Browser tab, mounted against an engine that answers from a table.
 *
 * What is asserted is what the card *did*: whether it showed up at all, what it said each state lets through,
 * which key it saved, and that what it shows afterwards is what the engine stored. Whether a stored value
 * actually gates the three verbs is the engine's claim (`tests/test_page_actions.py`).
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";

const { withDom } = await import("./dom.ts");
const React = (await import("react")).default;
const h = React.createElement;

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });

interface Call {
  method: string;
  path: string;
  body: unknown;
}

type Route = (body: unknown) => Response;

const LABEL = "The assistant may open addresses, click and type in the browser tab";

const held = (allowed: number) => ({
  parallel_width: { value: 4, min: 1, max: 16 },
  page_actions: { value: allowed, min: 0, max: 1 },
});

async function withCard(
  routes: Record<string, Route>,
  body: (dom: Dom, calls: Call[]) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    const calls: Call[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ method, path: url.pathname, body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
      const route = routes[`${method} ${url.pathname}`];
      if (!route) throw new Error(`no answer for ${method} ${url.pathname}`);
      return route(calls[calls.length - 1].body);
    }) as typeof fetch;
    const { PageActionsCard } = await import("../src/components/PageActionsCard.tsx");
    await dom.render(h(PageActionsCard));
    await dom.settle();
    await body(dom, calls);
  });
}

const puts = (calls: Call[]): Call[] => calls.filter((c) => c.method === "PUT" && c.path === "/settings/engine");

test("an engine without the setting shows no card, rather than a switch whose save would be refused", async () => {
  await withCard({ "GET /settings/engine": () => json({ parallel_width: { value: 4, min: 1, max: 16 } }) }, async (dom) => {
    assert.doesNotMatch(dom.text(), /Browser tab/);
  });
});

test("a fresh install shows it off, and says reading still works", async () => {
  await withCard({ "GET /settings/engine": () => json(held(0)) }, async (dom) => {
    assert.match(dom.text(), /Browser tab/);
    assert.ok((dom.byField(LABEL) as HTMLInputElement).checked === false, "a fresh install showed the switch on");
    assert.match(dom.text(), /can read the page you have open, but cannot open an address, click or type/);
  });
});

test("turning it on saves the switch, shows what the engine kept, and says what it lets out", async () => {
  await withCard(
    {
      "GET /settings/engine": () => json(held(0)),
      "PUT /settings/engine": () => json({ saved: { page_actions: 1 } }),
    },
    async (dom, calls) => {
      await dom.click(dom.byField(LABEL));
      await dom.settle();
      assert.deepEqual(puts(calls).map((c) => c.body), [{ page_actions: true }]);
      assert.ok((dom.byField(LABEL) as HTMLInputElement).checked === true, "the switch did not show what was stored");
      assert.match(dom.text(), /sent to that site/);
      assert.match(dom.text(), /anything it has read in your files/);
      assert.match(dom.text(), /shown in the transcript before it is taken/);
    },
  );
});

test("the switch shows what the engine stored, not what was clicked", async () => {
  await withCard(
    {
      "GET /settings/engine": () => json(held(0)),
      // An engine that kept it off (an older clamp, a lost race): the card believes the echo.
      "PUT /settings/engine": () => json({ saved: { page_actions: 0 } }),
    },
    async (dom) => {
      await dom.click(dom.byField(LABEL));
      await dom.settle();
      assert.ok((dom.byField(LABEL) as HTMLInputElement).checked === false, "the click was shown instead of the echo");
    },
  );
});

test("a refused save says so, and the switch stays where the engine has it", async () => {
  await withCard(
    {
      "GET /settings/engine": () => json(held(1)),
      "PUT /settings/engine": () => json({ code: "invalid_value", message: "page_actions must be an integer" }, 422),
    },
    async (dom) => {
      await dom.click(dom.byField(LABEL));
      await dom.settle();
      assert.ok((dom.byField(LABEL) as HTMLInputElement).checked === true, "a refused save moved the switch");
      assert.match(dom.text(), /page_actions must be an integer/, "the engine's own sentence was not shown");
    },
  );
});
