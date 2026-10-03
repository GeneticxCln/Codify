/**
 * Settings → Web pages, mounted against an engine that answers from a table.
 *
 * What is asserted is what the card *did*: which keys it saved, what it said was stored, and which of the
 * engine's own sentences it put in front of the person. Which site names are valid is the engine's rule
 * (`tests/test_api.py`), so the refusal here is the engine's, served by the table, and the test is that the card
 * shows it and keeps the draft so the person can fix the entry.
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

const held = (mode: number, hosts: string) => ({
  parallel_width: { value: 4, min: 1, max: 16 },
  web_fetch: { value: mode, min: 0, max: 2 },
  web_fetch_hosts: { value: hosts, max: 200 },
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
    const { WebAccessCard } = await import("../src/components/WebAccessCard.tsx");
    await dom.render(h(WebAccessCard));
    await dom.settle();
    await body(dom, calls);
  });
}

const choose = async (dom: Dom, mode: number): Promise<void> => {
  const select = dom.byField("The assistant may fetch") as HTMLSelectElement;
  await React.act(async () => {
    select.value = String(mode);
    select.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  });
};

const puts = (calls: Call[]): Call[] => calls.filter((c) => c.method === "PUT" && c.path === "/settings/engine");

test("an engine without the setting shows no card, rather than a card whose save would be refused", async () => {
  await withCard({ "GET /settings/engine": () => json({ parallel_width: { value: 4, min: 1, max: 16 } }) }, async (dom) => {
    assert.doesNotMatch(dom.text(), /Web pages/);
  });
});

test("a fresh install shows it off, says so, and offers nothing to save", async () => {
  await withCard({ "GET /settings/engine": () => json(held(0, "")) }, async (dom) => {
    assert.match(dom.text(), /Web pages/);
    assert.equal((dom.byField("The assistant may fetch") as HTMLSelectElement).value, "0");
    assert.match(dom.text(), /cannot fetch web pages/);
    assert.ok((dom.byButton("Save web access") as HTMLButtonElement).disabled, "Save was offered for no change");
    assert.throws(() => dom.byField("Sites"), "the site list is shown while the setting is off");
  });
});

test("choosing the list shows the field for it, and an empty list says it allows nothing", async () => {
  await withCard({ "GET /settings/engine": () => json(held(0, "")) }, async (dom) => {
    await choose(dom, 1);
    assert.ok(dom.byField("Sites"));
    assert.match(dom.text(), /list is empty/);
    assert.match(dom.text(), /nothing can be fetched/);
  });
});

test("saving sends the mode and the list as typed, and shows what the engine kept", async () => {
  await withCard(
    {
      "GET /settings/engine": () => json(held(0, "")),
      // The engine normalises the list: lower-cased, one spelling, each site once.
      "PUT /settings/engine": () => json({ saved: { web_fetch: 1, web_fetch_hosts: "docs.python.org, example.com" } }),
    },
    async (dom, calls) => {
      await choose(dom, 1);
      await dom.fill(dom.byField("Sites") as HTMLInputElement, "Docs.Python.org, *.example.com");
      assert.match(dom.text(), /2 sites you listed/);
      await dom.click(dom.byButton("Save web access"));
      await dom.settle();
      assert.deepEqual(puts(calls).map((c) => c.body), [{ web_fetch: 1, web_fetch_hosts: "Docs.Python.org, *.example.com" }]);
      assert.equal((dom.byField("Sites") as HTMLInputElement).value, "docs.python.org, example.com", "the draft was not replaced by the echo");
      assert.match(dom.text(), /Saved — the next run uses it/);
      assert.ok((dom.byButton("Save web access") as HTMLButtonElement).disabled, "a saved change still offered Save");
    },
  );
});

test("any public site is said as plainly as the engine says it, and is what is sent", async () => {
  await withCard(
    {
      "GET /settings/engine": () => json(held(0, "")),
      "PUT /settings/engine": (body) => json({ saved: body }),
    },
    async (dom, calls) => {
      await choose(dom, 2);
      assert.match(dom.text(), /sent to that site/);
      assert.match(dom.text(), /anything the assistant has read in your files/);
      await dom.click(dom.byButton("Save web access"));
      await dom.settle();
      assert.deepEqual(puts(calls).map((c) => (c.body as { web_fetch: number }).web_fetch), [2]);
    },
  );
});

test("the engine's refusal of a site list is shown as its sentence, and the draft is kept", async () => {
  const refusal = {
    code: "invalid_value",
    message: "web_fetch_hosts takes site names such as docs.python.org, not addresses, URLs or bare words: http://10.0.0.1/",
  };
  await withCard(
    {
      "GET /settings/engine": () => json(held(1, "docs.python.org")),
      "PUT /settings/engine": () => json(refusal, 422),
    },
    async (dom) => {
      await dom.fill(dom.byField("Sites") as HTMLInputElement, "http://10.0.0.1/");
      await dom.click(dom.byButton("Save web access"));
      await dom.settle();
      assert.match(dom.text(), /takes site names such as docs\.python\.org/);
      assert.doesNotMatch(dom.text(), /"code":/, "the engine's JSON was shown as it arrived");
      assert.equal((dom.byField("Sites") as HTMLInputElement).value, "http://10.0.0.1/", "the person's draft was thrown away");
    },
  );
});
