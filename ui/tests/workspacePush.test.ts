/**
 * The header's push and checks line (`docs/04` §6): the branch, a button that pushes it, GitHub's
 * pull-request form as a link the app's own browser opens, and the CI state the window is watching.
 *
 * The engine routes are tested against real repositories in `tests/test_workspace_push.py`; this file is
 * about what the panel draws. A state that could not be read must say so — it must never look like a pass.
 */
import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { Sidebar } = await import("../src/components/Sidebar.tsx");
import type { Conversation, WorkspaceChecks, WorkspaceGit, WorkspacePush } from "../src/types.ts";

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

const git = (over: Partial<WorkspaceGit> = {}): WorkspaceGit => ({
  branch: "main",
  detached: false,
  dirty: false,
  ahead: 0,
  behind: 0,
  reason: null,
  ...over,
});

const push = (over: Partial<WorkspacePush> = {}): WorkspacePush => ({
  ok: true,
  branch: "main",
  remote: "https://github.com/acme/widget.git",
  sha: "a".repeat(40),
  pr_url: "https://github.com/acme/widget/compare/main?expand=1",
  reason: null,
  ...over,
});

const checks = (over: Partial<WorkspaceChecks> = {}): WorkspaceChecks => ({
  state: "pending",
  sha: "a".repeat(40),
  url: "https://github.com/acme/widget/commit/" + "a".repeat(40) + "/checks",
  checks: [],
  reason: null,
  ...over,
});

const render = (props: {
  git?: WorkspaceGit | null;
  onPush?: () => void;
  pushResult?: WorkspacePush | null;
  checks?: WorkspaceChecks | null;
}): string =>
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
      onOpenLink: noop,
      ...props,
    }),
  );

const shown = (markup: string): string =>
  markup
    .replace(/<[^>]+>/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/\s+/g, " ")
    .trim();

test("a branch with a handler offers the push button; a repository with no branch does not", () => {
  assert.match(shown(render({ git: git(), onPush: noop })), /Push & PR/);
  assert.doesNotMatch(shown(render({ git: git({ branch: null }), onPush: noop })), /Push & PR/);
});

test("a refused push shows git's sentence, not a success and not a silent no-op", () => {
  const refused = push({ ok: false, branch: null, remote: null, sha: null, pr_url: null, reason: "origin does not appear to be a git repository" });

  const markup = render({ git: git(), onPush: noop, pushResult: refused });

  assert.match(shown(markup), /origin does not appear to be a git repository/);
  assert.doesNotMatch(shown(markup), /Open PR page/);
});

test("a pushed branch offers GitHub's pull-request form as a link", () => {
  const markup = render({ git: git(), onPush: noop, pushResult: push() });

  assert.match(shown(markup), /Open PR page/);
  assert.match(shown(markup), /Push & PR/);
});

test("the CI line says running, passed and failed, and unknown is not a pass", () => {
  assert.match(shown(render({ checks: checks({ state: "pending" }) })), /CI running/);
  assert.match(shown(render({ checks: checks({ state: "success" }) })), /CI passed/);
  assert.match(shown(render({ checks: checks({ state: "failure" }) })), /CI failed/);

  const unknown = render({
    checks: checks({ state: "unknown", url: null, reason: "GitHub refused the checks request (403)" }),
  });
  assert.match(shown(unknown), /CI unknown/);
  assert.doesNotMatch(shown(unknown), /CI passed|CI failed/);
});
