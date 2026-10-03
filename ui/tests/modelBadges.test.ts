/**
 * What a model row says about the model, as badges.
 *
 * Rendered, not read as source. The three facts used to be bare coloured words: the roles in the
 * knowledge hue (reserved for the CODIFY.md deliverable), "last run" in blue, and "not a chat model"
 * in amber with no shape beside it, so the one warning in the row was told apart by colour alone.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const React = (await import("react")).default;
const { ModelBadges } = await import("../src/components/ModelBadges.tsx");
const { modelBadges, buildModelSignals } = await import("../src/modelSignals.ts");

type Badges = ReturnType<typeof modelBadges>;
const NONE: Badges = { roles: "", rolesTitle: "", lastRun: false, lastRunTitle: "", notChat: false };
const render = (b: Partial<Badges>): string =>
  renderToStaticMarkup(React.createElement(ModelBadges, { badges: { ...NONE, ...b } }));
const textOf = (markup: string): string => markup.replace(/<[^>]*>/g, "");

test("a row with nothing to say renders nothing at all", () => {
  assert.equal(render({}), "");
});

test("each fact is a pill, in the order roles, last run, not a chat model", () => {
  const markup = render({
    roles: "planner, critic",
    rolesTitle: "Configured on: planner, critic",
    lastRun: true,
    lastRunTitle: "Last run by the planner",
    notChat: true,
  });
  assert.equal((markup.match(/rounded-full/g) ?? []).length, 3, "the three facts are not three pills");
  const text = textOf(markup);
  assert.ok(text.indexOf("planner, critic") < text.indexOf("last run"), "last run came before the roles");
  assert.ok(text.indexOf("last run") < text.indexOf("not a chat model"), "the warning came before last run");
});

test("the roles are a neutral tag: not the knowledge hue, and no glyph", () => {
  const markup = render({ roles: "planner", rolesTitle: "Configured on: planner" });
  assert.match(markup, /bg-codify-raised/, "roles are not the resting neutral pill");
  assert.doesNotMatch(markup, /codify-knowledge/, "roles are drawn in the hue reserved for the CODIFY.md deliverable");
  assert.doesNotMatch(markup, /<svg/, "a tag carries a glyph");
  assert.match(markup, /title="Configured on: planner"/, "the roles' explanation is gone");
});

test("last run is news, in the info tone, with no glyph", () => {
  const markup = render({ lastRun: true, lastRunTitle: "Last run by the critic" });
  assert.match(markup, /bg-codify-info\/40/);
  assert.doesNotMatch(markup, /<svg/);
  assert.match(markup, /title="Last run by the critic"/);
});

test("not a chat model is a warning with its triangle, so it is not told apart by colour alone", () => {
  const markup = render({ notChat: true });
  assert.match(markup, /bg-codify-warning\/40/);
  assert.match(markup, /<svg[^>]*aria-hidden="true"/, "the warning has no glyph");
  assert.equal(textOf(markup), "not a chat model", "the glyph added text, so a screen reader would read it out");
  assert.match(markup, /title="[^"]+"/, "the warning does not say what it means");
});

test("a long list of roles is cut inside its pill and still says all of them on hover", () => {
  const markup = render({ roles: "planner, critic, scribe +2", rolesTitle: "Configured on: planner, critic, scribe, fixer, verifier" });
  assert.match(markup, /<span class="truncate">planner, critic, scribe \+2<\/span>/, "the roles are not cut inside the pill");
  assert.match(markup, /title="Configured on: planner, critic, scribe, fixer, verifier"/);
});

test("a model that is configured and has run gets its roles and its last run from the signals the menus build", () => {
  const signals = buildModelSignals(
    [{ role: "planner", provider: "local", model_name: "llama3.1:8b" } as never],
    [{ provider: "local", model: "llama3.1:8b", role: "planner", ran_at: 5 }],
  );
  const markup = render(modelBadges({ id: "llama3.1:8b", name: "llama3.1:8b", provider: "local" } as never, signals));
  assert.equal(textOf(markup), "plannerlast run");
});
