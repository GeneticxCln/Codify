/** Settings → the fail-closed gate switch, mounted against the engine settings route. */
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

async function withToggle(
  routes: Record<string, Route>,
  body: (dom: Dom, calls: Call[]) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    const calls: Call[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const method = (init?.method ?? "GET").toUpperCase();
      const call: Call = {
        method,
        path: url.pathname,
        body: typeof init?.body === "string" ? JSON.parse(init.body) : null,
      };
      calls.push(call);
      const route = routes[`${method} ${url.pathname}`];
      if (!route) throw new Error(`no answer for ${method} ${url.pathname}`);
      return route(call.body);
    }) as typeof fetch;
    const { LayaRequiredToggle } = await import("../src/components/LayaRequiredToggle.tsx");
    await dom.render(h(LayaRequiredToggle));
    await dom.settle();
    await body(dom, calls);
  });
}

const control = (dom: Dom): HTMLElement =>
  dom.byButton("Require the pre-flight gate — refuse a turn it cannot check");

test("an engine without the setting shows no switch", async () => {
  await withToggle(
    { "GET /settings/engine": () => json({ parallel_width: { value: 4, min: 1, max: 16 } }) },
    async (dom) => {
      assert.doesNotMatch(dom.text(), /Require the pre-flight gate/);
    },
  );
});

test("the setting is off by default and the switch saves true", async () => {
  await withToggle(
    {
      "GET /settings/engine": () => json({ laya_required: { value: 0, min: 0, max: 1 } }),
      "PUT /settings/engine": (body) => json({ saved: body }),
    },
    async (dom, calls) => {
      assert.equal(control(dom).getAttribute("aria-checked"), "false");
      await dom.click(control(dom));
      await dom.settle();

      assert.deepEqual(
        calls.filter((call) => call.method === "PUT").map((call) => call.body),
        [{ laya_required: true }],
      );
      assert.equal(control(dom).getAttribute("aria-checked"), "true");
    },
  );
});

test("a refused save reverts the switch and shows the reason", async () => {
  await withToggle(
    {
      "GET /settings/engine": () => json({ laya_required: { value: 0, min: 0, max: 1 } }),
      "PUT /settings/engine": () => json({ message: "settings unavailable" }, 503),
    },
    async (dom) => {
      await dom.click(control(dom));
      await dom.settle();

      assert.equal(control(dom).getAttribute("aria-checked"), "false");
      assert.match(dom.text(), /settings unavailable/);
    },
  );
});
