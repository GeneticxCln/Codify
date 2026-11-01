/**
 * Settings → Agent skills: one toggle per skill, saved to the engine.
 *
 * The pane is the screen side of a narrow contract: the engine lists every skill and
 * persists a disabled set per workspace, while the conductor only ever sees the enabled
 * names. These tests press the controls and inspect the requests, because a checkbox that
 * renders but sends nothing is exactly the failure this feature cannot have.
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
  query: string;
  body: unknown;
}

type Route = (body: unknown) => Response;

const skill = (
  name: string,
  description: string,
  source: "built-in" | "workspace",
  enabled: boolean,
  moves: string[] = [],
) => ({ name, description, source, moves, enabled });

const SKILLS = {
  workspace_id: "ws-1",
  skills: [
    skill(
      "blast-radius",
      "Find what a change could break beyond its own diff.",
      "built-in",
      true,
      ["search_code", "verify"],
    ),
    skill("prove", "Prove a change with the narrowest real check.", "built-in", false, ["write", "verify"]),
  ],
  disabled: ["prove"],
  problems: [],
  shadows: [],
};

/** Ten rows, enough for the search/filter row, with both sources and both states. */
const MANY_SKILLS = {
  workspace_id: "ws-1",
  skills: [
    skill("alpha-audit", "Audit the deploy manifest.", "built-in", true, ["read_file"]),
    skill("beta-build", "Build the release bundle.", "built-in", true),
    skill("gamma-check", "Check the deployment health.", "workspace", false),
    skill("delta-deploy", "Deploy the package locally.", "workspace", true, ["write", "verify"]),
    skill("epsilon-helper", "Epsilon helper.", "built-in", true),
    skill("zeta-helper", "Zeta helper.", "built-in", true),
    skill("eta-helper", "Eta helper.", "built-in", false),
    skill("theta-helper", "Theta helper.", "built-in", true),
    skill("iota-helper", "Iota helper.", "workspace", true),
    skill("kappa-helper", "Kappa helper.", "built-in", true),
  ],
  disabled: ["gamma-check", "eta-helper"],
  problems: [],
  shadows: [],
};

const DETAIL = {
  ...skill("delta-deploy", "Deploy the package locally.", "workspace", true, ["write", "verify"]),
  notes: ["declares a move that does not exist, ignored: nope"],
  body: "Step one: read the manifest.\nStep two: deploy the package.",
};

async function withPane(
  overrides: Record<string, Route>,
  body: (dom: Dom, calls: Call[]) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    const routes: Record<string, Route> = {
      "GET /settings/skills": () => json(SKILLS),
      "PUT /settings/skills/prove": (patch) =>
        json({ name: "prove", enabled: (patch as { enabled: boolean }).enabled, disabled: [] }),
      "PUT /settings/skills/blast-radius": (patch) =>
        json({ name: "blast-radius", enabled: (patch as { enabled: boolean }).enabled, disabled: ["blast-radius"] }),
      ...overrides,
    };
    const calls: Call[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const method = (init?.method ?? "GET").toUpperCase();
      const payload = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      calls.push({
        method,
        path: url.pathname,
        query: url.search,
        body: payload,
      });
      const route = routes[`${method} ${url.pathname}`];
      if (!route) throw new Error(`no answer for ${method} ${url.pathname}`);
      return route(payload);
    }) as typeof fetch;

    const { AgentSkillsPane } = await import("../src/components/AgentSkillsPane.tsx");
    await dom.render(h(AgentSkillsPane, { workspaceId: "ws-1" }));
    await dom.settle();
    await body(dom, calls);
  });
}

const on = (dom: Dom, name: string): boolean =>
  dom.byButton(name).getAttribute("aria-checked") === "true";

/** The skill rows on screen, in order: each checkbox's own name. */
const listed = (dom: Dom): string[] =>
  [...dom.container.querySelectorAll('[role="checkbox"]')].map((box) =>
    (box.textContent ?? "").trim(),
  );

test("without a workspace it asks for one instead of offering unscoped switches", async () => {
  await withDom(async (dom) => {
    const calls: Call[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      calls.push({ method: "GET", path: new URL(String(input)).pathname, query: "", body: null });
      return json(SKILLS);
    }) as typeof fetch;

    const { AgentSkillsPane } = await import("../src/components/AgentSkillsPane.tsx");
    await dom.render(h(AgentSkillsPane, {}));
    await dom.settle();

    assert.match(dom.text(), /Select a project first/);
    assert.equal(calls.length, 0, "there is no workspace menu to ask for");
  });
});

test("every skill is listed with its own on/off state", async () => {
  await withPane({}, async (dom) => {
    assert.equal(on(dom, "blast-radius"), true);
    assert.equal(on(dom, "prove"), false, "a disabled skill must not read as enabled");
    const text = dom.text().replace(/\s+/g, " ");
    assert.match(text, /use_skill/, "the pane must say bodies are fetched only when chosen");
    assert.match(text, /not offered at all/, "a disabled skill's consequence must be stated");
    assert.equal(dom.text().match(/Built-in/g)?.length, 2, "both skills must show their source");
    assert.ok(
      dom.container.querySelector('input[aria-label="Search skills"]') === null,
      "two rows do not need a search field above them",
    );
  });
});

test("turning a skill on saves that exact level for that skill and workspace", async () => {
  await withPane({}, async (dom, calls) => {
    await dom.click(dom.byButton("prove"));
    await dom.settle();

    assert.equal(on(dom, "prove"), true);
    const saved = calls.filter((call) => call.method === "PUT");
    assert.equal(saved.length, 1);
    assert.equal(saved[0].path, "/settings/skills/prove");
    assert.equal(saved[0].query, "?workspace_id=ws-1");
    assert.deepEqual(saved[0].body, { enabled: true });
    assert.match(dom.text(), /can choose it again/);
  });
});

test("a failed save rolls the toggle back and says why", async () => {
  await withPane(
    {
      "PUT /settings/skills/blast-radius": () =>
        json({ message: "the settings store is locked" }, 503),
    },
    async (dom) => {
      await dom.click(dom.byButton("blast-radius"));
      await dom.settle();

      assert.equal(on(dom, "blast-radius"), true, "a refused save left the skill off");
      assert.match(dom.text(), /settings store is locked/);
    },
  );
});

test("a large menu searches by name, description or declared move", async () => {
  await withPane({ "GET /settings/skills": () => json(MANY_SKILLS) }, async (dom) => {
    const input = dom.byField("Search skills") as HTMLInputElement;
    assert.equal(listed(dom).length, 10);

    await dom.fill(input, "deploy package");
    assert.deepEqual(listed(dom), ["delta-deploy"], "every token must match");
    assert.match(dom.text(), /1 of 10 skills/);

    await dom.fill(input, "read_file");
    assert.deepEqual(listed(dom), ["alpha-audit"], "a declared move is searchable");

    await dom.fill(input, "nothing matches this");
    assert.deepEqual(listed(dom), []);
    assert.match(dom.text(), /No skills match this search or filter/);

    await dom.click(dom.byButton("Clear search and filters"));
    assert.equal(listed(dom).length, 10);
  });
});

test("the filters separate enabled, disabled, built-in and workspace skills", async () => {
  await withPane({ "GET /settings/skills": () => json(MANY_SKILLS) }, async (dom) => {
    assert.equal(
      dom.byButton("All").getAttribute("aria-pressed"),
      "true",
      "the first view is the whole list",
    );

    await dom.click(dom.byButton("Disabled"));
    assert.deepEqual(listed(dom), ["gamma-check", "eta-helper"]);
    assert.match(dom.text(), /2 of 10 skills · Disabled/);

    await dom.click(dom.byButton("Workspace"));
    assert.deepEqual(
      listed(dom),
      ["gamma-check", "delta-deploy", "iota-helper"],
      "a source filter must keep both states",
    );

    await dom.click(dom.byButton("Enabled"));
    assert.equal(listed(dom).length, 8);

    await dom.fill(dom.byField("Search skills") as HTMLInputElement, "helper");
    await dom.click(dom.byButton("Disabled"));
    assert.deepEqual(listed(dom), ["eta-helper"], "search and filter are combined");
  });
});

test("refresh re-reads the menu, keeps the current filter, and says so", async () => {
  let reads = 0;
  await withPane(
    {
      "GET /settings/skills": () => {
        reads += 1;
        return json(MANY_SKILLS);
      },
    },
    async (dom, calls) => {
      await dom.fill(dom.byField("Search skills") as HTMLInputElement, "helper");
      assert.equal((dom.byField("Search skills") as HTMLInputElement).value, "helper");

      await dom.click(dom.byLabel("Refresh skills"));
      await dom.settle();

      assert.equal(reads, 2, "Refresh must ask the engine again, not reuse the first list");
      assert.equal(
        calls.filter((call) => call.method === "GET" && call.path === "/settings/skills").length,
        2,
      );
      assert.equal(
        (dom.byField("Search skills") as HTMLInputElement).value,
        "helper",
        "refresh must not erase the search the person was using",
      );
      assert.match(dom.text(), /Skills refreshed from this workspace/);
    },
  );
});

test("the creator posts the form and re-reads the list", async () => {
  const posted: unknown[] = [];
  await withPane(
    {
      "POST /settings/skills": (body) => {
        posted.push(body);
        const fields = body as { name: string; description: string; moves: string[] };
        return json(
          {
            workspace_id: "ws-1",
            name: fields.name,
            description: fields.description,
            path: `.codify/skills/${fields.name}.md`,
            source: "workspace",
            moves: fields.moves,
            shadows: false,
          },
          201,
        );
      },
    },
    async (dom, calls) => {
      await dom.click(dom.byButton("New skill"));
      assert.match(dom.text(), /New skill/);

      await dom.fill(dom.byLabel("New skill name") as HTMLInputElement, "release-check");
      await dom.fill(
        dom.byLabel("New skill description") as HTMLInputElement,
        "Checks a release before tagging.",
      );
      await dom.fill(
        dom.byLabel("New skill instructions") as HTMLTextAreaElement,
        "1. Read the manifest.\n2. Run the narrowest check.",
      );
      await dom.click(dom.byButton("Create skill"));
      await dom.settle();

      assert.equal(posted.length, 1);
      assert.deepEqual(posted[0], {
        name: "release-check",
        description: "Checks a release before tagging.",
        moves: [],
        instructions: "1. Read the manifest.\n2. Run the narrowest check.",
      });
      assert.match(dom.text(), /release-check was created at \.codify\/skills\/release-check\.md/);
      assert.ok(
        dom.container.querySelector('[aria-label="New skill"]') === null,
        "a successful create closes the form",
      );
      assert.equal(
        calls.filter((call) => call.method === "GET" && call.path === "/settings/skills").length,
        2,
        "the new skill must come from the engine's list, not be invented locally",
      );
    },
  );
});

test("an uploaded Markdown file is sent as text and the header names it", async () => {
  const uploaded: unknown[] = [];
  await withPane(
    {
      "POST /settings/skills/import": (body) => {
        uploaded.push(body);
        return json(
          {
            workspace_id: "ws-1",
            name: "uploaded-skill",
            description: "An uploaded recipe.",
            path: ".codify/skills/uploaded-skill.md",
            source: "workspace",
            moves: ["read_file"],
            shadows: false,
          },
          201,
        );
      },
    },
    async (dom) => {
      await dom.click(dom.byButton("New skill"));
      await dom.click(dom.byButton("Upload"));
      const input = dom.byLabel("Upload a skill file") as HTMLInputElement;
      await dom.selectFile(input, {
        name: "pick-me.md",
        text: "---\nname: uploaded-skill\ndescription: An uploaded recipe.\n---\nDo it.\n",
      });
      assert.match(dom.text(), /Ready to upload/);

      await dom.click(dom.byButton("Upload skill"));
      await dom.settle();

      assert.equal(uploaded.length, 1);
      assert.deepEqual(uploaded[0], {
        filename: "pick-me.md",
        content: "---\nname: uploaded-skill\ndescription: An uploaded recipe.\n---\nDo it.\n",
      });
      assert.match(dom.text(), /uploaded-skill was created/);
    },
  );
});

test("instructions are fetched only when previewed, and can be hidden again", async () => {
  await withPane(
    {
      "GET /settings/skills": () => json(MANY_SKILLS),
      "GET /settings/skills/delta-deploy": () => json(DETAIL),
    },
    async (dom, calls) => {
      assert.equal(dom.text().includes("Step one:"), false, "a list must not carry bodies");
      assert.equal(
        calls.filter((call) => call.path === "/settings/skills/delta-deploy").length,
        0,
        "opening the list read a body",
      );

      const button = () =>
        dom.container.querySelector(
          'button[aria-label="Preview delta-deploy instructions"], button[aria-label="Hide delta-deploy instructions"]',
        ) as HTMLButtonElement;
      await dom.click(button());
      await dom.settle();

      const call = calls.find((entry) => entry.path === "/settings/skills/delta-deploy");
      assert.equal(call?.method, "GET");
      assert.equal(call?.query, "?workspace_id=ws-1");
      assert.match(dom.text(), /Step one: read the manifest\./);
      assert.match(dom.text(), /Moves: write, verify/);
      assert.match(dom.text(), /declares a move that does not exist/);
      assert.equal(button().getAttribute("aria-expanded"), "true");

      await dom.click(dom.byButton("Hide instructions"));
      assert.equal(dom.text().includes("Step one:"), false);
      assert.equal(button().getAttribute("aria-expanded"), "false");
    },
  );
});
