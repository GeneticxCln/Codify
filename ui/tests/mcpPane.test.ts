/**
 * Settings → MCP: admit a remote server, probe it, and remove it.
 *
 * The pane is deliberately an admission screen, not a tool menu. These tests
 * press the controls and inspect the requests: a card that renders without
 * asking the engine, or a probe button that sends nothing, is the failure this
 * surface cannot have.
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

const server = {
  id: "mcp-1",
  name: "Issues",
  url: "https://93.184.216.34/mcp",
  auth_kind: "none",
  auth_header: "",
  has_secret: false,
  protocol_version: "2025-06-18",
  server_name: "test-mcp",
  enabled: true,
  last_probe_at: 1,
  last_probe_error: null,
  tools: [
    {
      name: "search_issues",
      description: "Search issues in a repository.",
      input_schema: { type: "object" },
      authority: null,
      enabled: false,
      state: "new",
    },
  ],
  created_at: 1,
  updated_at: 1,
};

async function withPane(
  body: (dom: Dom, calls: Call[]) => Promise<void>,
  options: { createFailsAfterStoring?: boolean } = {},
): Promise<void> {
  await withDom(async (dom) => {
    let stored = false;
    const calls: Call[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const method = (init?.method ?? "GET").toUpperCase();
      const payload = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      calls.push({ method, path: url.pathname, body: payload });
      if (method === "GET" && url.pathname === "/settings/mcp") {
        // A row whose probe failed carries the failure, which is how the pane shows it after the re-read.
        const row = options.createFailsAfterStoring
          ? { ...server, last_probe_error: "93.184.216.34 answered 503" }
          : server;
        return json(stored ? [row] : []);
      }
      if (method === "POST" && url.pathname === "/settings/mcp") {
        stored = true;
        if (options.createFailsAfterStoring) {
          // The engine stores the row, then the probe fails: a 502 with the server already saved.
          return json({ code: "mcp_http_error", message: "93.184.216.34 answered 503" }, 502);
        }
        return json(server, 201);
      }
      if (method === "POST" && url.pathname === "/settings/mcp/mcp-1/probe") {
        return json(server);
      }
      if (method === "PUT" && url.pathname === "/settings/mcp/mcp-1") {
        return json({ ...server, enabled: (payload as { enabled?: boolean }).enabled ?? true });
      }
      if (method === "PUT" && url.pathname === "/settings/mcp/mcp-1/tools/search_issues") {
        const change = payload as { authority: "read" | null; enabled: boolean };
        return json({ ...server.tools[0], ...change });
      }
      if (method === "DELETE" && url.pathname === "/settings/mcp/mcp-1") {
        stored = false;
        return json({ ok: true, id: "mcp-1" });
      }
      throw new Error(`no answer for ${method} ${url.pathname}`);
    }) as typeof fetch;

    const { McpPane } = await import("../src/components/McpPane.tsx");
    await dom.render(h(McpPane));
    await dom.settle();
    await body(dom, calls);
  });
}

test("the empty pane explains Read and Act and offers an add form", async () => {
  await withPane(async (dom) => {
    assert.match(dom.text(), /Remote MCP servers/);
    assert.match(dom.text(), /Act tools only while a goal is running/i);
    assert.match(dom.text(), /No MCP servers yet/);
  });
});

test("saving posts the address and re-reads the list", async () => {
  await withPane(async (dom, calls) => {
    await dom.click(dom.byButton("Add server"));
    await dom.fill(dom.byField("Server name") as HTMLInputElement, "Issues");
    await dom.fill(dom.byField("Server URL") as HTMLInputElement, "https://93.184.216.34/mcp");
    await dom.click(dom.byButton("Save and probe"));
    await dom.settle();

    const posted = calls.find((call) => call.method === "POST" && call.path === "/settings/mcp");
    assert.ok(posted, "Save must POST to the engine");
    assert.deepEqual(posted.body, {
      name: "Issues",
      url: "https://93.184.216.34/mcp",
      auth_kind: "none",
      auth_header: "",
      secret: "",
    });
    assert.match(dom.text(), /Issues/);
    assert.match(dom.text(), /search_issues/);
    assert.match(dom.text(), /Read tools\s+are callable/);
    assert.match(dom.text(), /Input schema/);
  });
});

test("admitting a tool as Read sends the authority, and Enabled sends its own switch", async () => {
  await withPane(async (dom, calls) => {
    await dom.click(dom.byButton("Add server"));
    await dom.fill(dom.byField("Server name") as HTMLInputElement, "Issues");
    await dom.fill(dom.byField("Server URL") as HTMLInputElement, "https://93.184.216.34/mcp");
    await dom.click(dom.byButton("Save and probe"));
    await dom.settle();

    await dom.click(dom.byLabel("Authority for search_issues"));
    await dom.settle();
    await dom.click(dom.byButton("Read"));
    await dom.settle();

    const admitted = calls.find(
      (call) => call.method === "PUT" && call.path === "/settings/mcp/mcp-1/tools/search_issues",
    );
    assert.ok(admitted, "choosing Read must PUT the tool's authority");
    assert.deepEqual(admitted.body, { authority: "read", enabled: true });

    // Act is admitted, and the engine gates it on the goal's stored status.
    await dom.click(dom.byLabel("Authority for search_issues"));
    await dom.settle();
    await dom.click(dom.byButton("Act"));
    await dom.settle();
    const act = calls
      .filter((call) => call.method === "PUT" && call.path === "/settings/mcp/mcp-1/tools/search_issues")
      .at(-1);
    assert.ok(act, "choosing Act must PUT the tool's authority");
    assert.deepEqual(act.body, { authority: "act", enabled: true });

    // The server's Enabled switch is its own request, not a tool authority.
    await dom.click(dom.byButton("Enabled"));
    await dom.settle();
    const toggled = calls.find(
      (call) => call.method === "PUT" && call.path === "/settings/mcp/mcp-1",
    );
    assert.ok(toggled, "the server switch must PUT the server");
    assert.deepEqual(toggled.body, { enabled: false });
  });
});

test("Probe now and Remove address that one server", async () => {
  await withPane(async (dom, calls) => {
    await dom.click(dom.byButton("Add server"));
    await dom.fill(dom.byField("Server name") as HTMLInputElement, "Issues");
    await dom.fill(dom.byField("Server URL") as HTMLInputElement, "https://93.184.216.34/mcp");
    await dom.click(dom.byButton("Save and probe"));
    await dom.settle();

    await dom.click(dom.byButton("Probe now"));
    await dom.settle();
    assert.ok(
      calls.some((call) => call.method === "POST" && call.path === "/settings/mcp/mcp-1/probe"),
      "Probe must ask the engine to run initialize + tools/list",
    );

    await dom.click(dom.byLabel("Remove Issues"));
    await dom.settle();
    assert.ok(
      calls.some((call) => call.method === "DELETE" && call.path === "/settings/mcp/mcp-1"),
      "Remove must delete that server",
    );
    assert.match(dom.text(), /No MCP servers yet/);
  });
});

test("a probe that fails after the row was stored closes the form, so Save cannot add a second copy", async () => {
  await withPane(async (dom, calls) => {
    await dom.click(dom.byButton("Add server"));
    await dom.fill(dom.byField("Server name") as HTMLInputElement, "Issues");
    await dom.fill(dom.byField("Server URL") as HTMLInputElement, "https://93.184.216.34/mcp");
    await dom.click(dom.byButton("Save and probe"));
    await dom.settle();

    assert.match(dom.text(), /answered 503/, "the probe's failure must be shown");
    assert.match(dom.text(), /Issues/, "the stored server must be listed");
    assert.throws(() => dom.byField("Server name"), "the add form must close: its draft is now a saved server");
    assert.equal(calls.filter((call) => call.method === "POST" && call.path === "/settings/mcp").length, 1);
  }, { createFailsAfterStoring: true });
});
