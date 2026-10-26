/**
 * The panel header's git line (`docs/16` item 3): the branch, whether it is dirty, and how far it has drifted.
 *
 * The service is tested against real repositories in `tests/test_workspace_git.py`; this file is about what the
 * person sees and what the window asks for. The three states with no branch — not a repository, no commits,
 * detached HEAD — must leave the header quiet rather than turn a fact into a warning banner.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withApp, waitFor } = await import("./appHarness.ts");
import type { AppContext } from "./appHarness.ts";

const { Sidebar } = await import("../src/components/Sidebar.tsx");
import type { Conversation, WorkspaceGit } from "../src/types.ts";

const noop = (): void => {};

const conversation = (): Conversation => ({
  id: "c1",
  workspace_id: "w1",
  title: "A thread",
  archived: false,
  created_at: 0,
  updated_at: 0,
  parent_id: null,
  parent_title: null,
});

const render = (git?: WorkspaceGit | null): string =>
  renderToStaticMarkup(
    React.createElement(Sidebar, {
      conversations: [conversation()],
      selectedWorkspaceId: "w1",
      workspaces: [{ id: "w1", name: "Codify", root_path: "/tmp/codify", design_contract_path: "", created_at: 0 }],
      onNewThread: noop,
      onNewProject: noop,
      onSelect: noop,
      onRename: noop,
      onArchive: noop,
      ...(git === undefined ? {} : { git }),
    }),
  );

const text = (markup: string): string => markup.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

const git = (over: Partial<WorkspaceGit> = {}): WorkspaceGit => ({
  branch: "main",
  detached: false,
  dirty: false,
  ahead: 0,
  behind: 0,
  reason: null,
  ...over,
});

const header = (ctx: AppContext): string =>
  (ctx.dom.container.querySelector('div[role="heading"][aria-level="2"]')?.textContent ?? "")
    .replace(/\s+/g, " ")
    .trim();

test("the header shows the branch, the dirty mark and the drift counts", () => {
  const markup = render(git({ dirty: true, ahead: 2, behind: 1 }));
  const shown = text(markup);

  assert.match(shown, /main/);
  assert.match(markup, /aria-label="Uncommitted changes"/, "the dirty marker has no accessible name");
  assert.match(markup, /aria-label="2 ahead"/);
  assert.match(markup, /aria-label="1 behind"/);
  assert.match(shown, /↑2/);
  assert.match(shown, /↓1/);
});

test("a clean branch shows its name and no count or dirty mark", () => {
  const markup = render(git());

  assert.match(text(markup), /main/);
  assert.doesNotMatch(markup, /Uncommitted changes/);
  assert.doesNotMatch(text(markup), /[↑↓]/);
});

test("no branch and nothing to report leaves the header as it was", () => {
  for (const quiet of [null, git({ branch: null, reason: "This workspace is not a git repository." }), undefined]) {
    const markup = render(quiet);
    assert.doesNotMatch(markup, /Uncommitted changes/);
    assert.doesNotMatch(text(markup), /[↑↓]/);
    assert.doesNotMatch(text(markup), /repository/, "a quiet state became an error sentence in the header");
  }
});

test("the app asks the engine for the selected workspace and puts the branch in the panel header", async () => {
  await withApp(
    { git: { "ws-a": git({ branch: "release/2.0", dirty: true, ahead: 3 }) } },
    async (ctx) => {
      await waitFor(ctx, "the git header line", () => /release\/2\.0/.test(header(ctx)));
      assert.match(header(ctx), /●/);
      assert.match(header(ctx), /↑3/);
      assert.ok(
        ctx.dom.container.querySelector('[role="heading"] [aria-label="Uncommitted changes"]'),
        "the dirty mark has no accessible name in the mounted header",
      );
      assert.ok(
        ctx.engine.some((call) => call.method === "GET" && call.path === "/workspaces/ws-a/git"),
        `the app never asked for the workspace's git state: ${ctx.engine.map((c) => c.path).join(", ")}`,
      );
    },
  );
});

test("a workspace whose git cannot be read shows no branch and no banner", async () => {
  // The harness answers `branch: null` and a reason for every workspace it was not given a state for, which is
  // exactly the engine's answer for a folder that is not a repository.
  await withApp({}, async (ctx) => {
    await waitFor(ctx, "the git request", () =>
      ctx.engine.some((call) => call.method === "GET" && call.path === "/workspaces/ws-a/git"),
    );
    assert.doesNotMatch(header(ctx), /repository/, "a missing repository became a header sentence");
    assert.doesNotMatch(header(ctx), /main/);
  });
});
