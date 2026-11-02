/**
 * Settings → Workspace command jail, mounted against the engine routes the card uses.
 *
 * The mode and the host's bwrap availability are two different facts. The test that matters is
 * the combination: `auto` on a host without bubblewrap is not containment, and the card has to
 * say so before a command runs rather than only in the goal's event stream.
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

const settings = (mode = "auto", network = 0) => ({
  sandbox_isolation: { value: mode, max: 8 },
  sandbox_network: { value: network, min: 0, max: 1 },
});

const runtime = (available: boolean) =>
  json({ sandbox: { bwrap: available ? "/usr/bin/bwrap" : null, available } });

async function withCard(
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
    const { SandboxCard } = await import("../src/components/SandboxCard.tsx");
    await dom.render(h(SandboxCard));
    await dom.settle();
    await body(dom, calls);
  });
}

const puts = (calls: Call[]): Call[] =>
  calls.filter((call) => call.method === "PUT" && call.path === "/settings/engine");

const choose = async (dom: Dom, label: string): Promise<void> => {
  await dom.click(dom.byLabel("Workspace command isolation"));
  const row = [...dom.container.querySelectorAll("[role='option']")].find(
    (option) => option.textContent === label,
  );
  assert.ok(row, `the list has no "${label}" row`);
  await dom.click(row);
};

test("an engine without the setting shows no card", async () => {
  await withCard(
    {
      "GET /settings/engine": () => json({ parallel_width: { value: 4, min: 1, max: 16 } }),
      "GET /settings/runtime": () => runtime(true),
    },
    async (dom) => {
      assert.doesNotMatch(dom.text(), /Workspace command jail/);
    },
  );
});

test("auto on a host with bwrap names it, and choosing required saves the engine key", async () => {
  await withCard(
    {
      "GET /settings/engine": () => json(settings()),
      "GET /settings/runtime": () => runtime(true),
      "PUT /settings/engine": (body) => json({ saved: body }),
    },
    async (dom, calls) => {
      assert.match(dom.text(), /Workspace command jail/);
      assert.match(dom.byLabel("Workspace command isolation").textContent ?? "", /Auto/);
      assert.match(dom.text(), /\/usr\/bin\/bwrap/);

      await choose(dom, "Required — refuse commands without bubblewrap");
      await dom.settle();

      assert.deepEqual(
        puts(calls).map((call) => call.body),
        [{ sandbox_isolation: "required" }],
      );
      assert.match(dom.text(), /Saved — this applies to the next workspace command/);
    },
  );
});

test("auto without bwrap says it is unjailed before a command runs", async () => {
  await withCard(
    {
      "GET /settings/engine": () => json(settings()),
      "GET /settings/runtime": () => runtime(false),
    },
    async (dom) => {
      assert.match(dom.text(), /Bubblewrap is not installed on this host/);
      assert.match(dom.text(), /runs workspace commands unjailed/);
    },
  );
});

test("required without bwrap says every command will be refused", async () => {
  await withCard(
    {
      "GET /settings/engine": () => json(settings("required")),
      "GET /settings/runtime": () => runtime(false),
    },
    async (dom) => {
      assert.match(dom.text(), /required mode refuses every workspace command/);
    },
  );
});

test("the network switch is off by default and saves its own key", async () => {
  await withCard(
    {
      "GET /settings/engine": () => json(settings()),
      "GET /settings/runtime": () => runtime(true),
      "PUT /settings/engine": (body) => json({ saved: body }),
    },
    async (dom, calls) => {
      const toggle = dom.byButton("Allow test commands to reach the network");
      assert.equal(toggle.getAttribute("aria-checked"), "false");
      await dom.click(toggle);
      await dom.settle();

      assert.deepEqual(
        puts(calls).map((call) => call.body),
        [{ sandbox_network: true }],
      );
      assert.equal(toggle.getAttribute("aria-checked"), "true");
    },
  );
});

test("a refused save reverts the switch and shows the engine's sentence", async () => {
  await withCard(
    {
      "GET /settings/engine": () => json(settings()),
      "GET /settings/runtime": () => runtime(true),
      "PUT /settings/engine": () => json({ message: "settings unavailable" }, 503),
    },
    async (dom) => {
      const toggle = dom.byButton("Allow test commands to reach the network");
      await dom.click(toggle);
      await dom.settle();

      assert.equal(toggle.getAttribute("aria-checked"), "false", "a refused save left the switch on");
      assert.match(dom.text(), /settings unavailable/);
    },
  );
});
