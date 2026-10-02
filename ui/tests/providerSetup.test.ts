/**
 * The provider row's arithmetic: what the search finds, and what "apply" writes.
 *
 * A provider row is the first place in this app where a model is chosen next to
 * a credential rather than on a role card, and it is the first place a single
 * click reaches more than one role. Both halves of that are worth pinning
 * away from a DOM: the search decides whether a model is findable at all, and
 * the apply plan decides how many roles a button is about to repoint.
 *
 * The search cases are the ones that decide whether the picker is usable. Model
 * ids are long and rarely what somebody remembers — `meta-llama/llama-3.3-70b-instruct`
 * arrives as "llama", or as "70b", or as "instruct" — and a provider serving two
 * hundred models is a scroll box, not a list you can read.
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  applyHint,
  applyLabel,
  changedRoles,
  countLabel,
  checkedLabel,
  discoveredFooter,
  needsOwnRefresh,
  noModelsMessage,
  planApply,
  planSummary,
  rolesOnProvider,
  searchModels,
  type RoleProvider,
} from "../src/providerSetup.ts";
import type { ModelOption } from "../src/types.ts";

const model = (over: Partial<ModelOption> = {}): ModelOption => ({
  id: "model-a",
  name: "Model A",
  provider: "openai",
  description: "",
  ...over,
});

const LLAMA = model({
  id: "meta-llama/llama-3.3-70b-instruct",
  name: "Llama 3.3 70B Instruct",
  description: "Meta's open-weight flagship",
});
const MISTRAL = model({
  id: "mistralai/mistral-large-2407",
  name: "Mistral Large",
  description: "European open weights",
});
const EMBED = model({
  id: "text-embedding-3-small",
  name: "Embedding v3",
  description: "vectors only",
  supports_chat: false,
});
const CATALOG = [LLAMA, MISTRAL, EMBED];

// ── the search ───────────────────────────────────────────────────────────

test("an empty query shows every discovered model, not none of them", () => {
  // Opening the menu on purpose is browsing. A filter that returns nothing for
  // an empty query makes the provider look like it serves nothing.
  assert.equal(searchModels(CATALOG, "").length, 3);
  assert.equal(searchModels(CATALOG, "   ").length, 3);
});

test("the search matches anywhere in the id, not just the prefix", () => {
  // This is the whole reason the picker is not a datalist. Every id in a real
  // catalogue starts with an account or an org ("meta-llama/", "hf.co/…"), so
  // nobody guesses the prefix and a prefix-only search finds nothing at all.
  const hits = searchModels(CATALOG, "70b");
  assert.deepEqual(
    hits.map((m) => m.id),
    [LLAMA.id],
    "the fragment nobody can guess is the one that has to work",
  );
});

test("the search is case-insensitive", () => {
  assert.equal(searchModels(CATALOG, "LLAMA")[0].id, LLAMA.id);
  assert.equal(searchModels(CATALOG, "Meta")[0].id, LLAMA.id);
});

test("the search also reads the display name and the description", () => {
  // A provider's list carries a name and a blurb the id does not. Searching one
  // field loses models for no gain.
  assert.equal(searchModels(CATALOG, "open-weight")[0].id, LLAMA.id);
  assert.equal(searchModels(CATALOG, "European")[0].id, MISTRAL.id);
});

test("two words narrow, rather than widening", () => {
  // All-of, not any-of: someone who remembers two fragments of one id types two
  // fragments and expects one list. Any-of would return the union and look like
  // the search ignored half of what they typed.
  assert.equal(searchModels(CATALOG, "llama 70b").length, 1);
  assert.equal(
    searchModels(CATALOG, "llama mistral").length,
    0,
    "two ids that do not coexist are not a match",
  );
});

test("extra whitespace between terms does not become a term", () => {
  assert.equal(searchModels(CATALOG, "  llama   70b  ").length, 1);
});

test("a search that matches nothing returns nothing rather than everything", () => {
  // The failure this guards is a filter that returns the full list on a
  // miss, which is indistinguishable from the search not running at all.
  assert.deepEqual(searchModels(CATALOG, "zzz-not-a-model"), []);
});

test("the search does not mutate or alias the catalogue", () => {
  const result = searchModels(CATALOG, "llama");
  result.push(EMBED);
  assert.equal(CATALOG.length, 3, "the caller grew the list it was handed");
});

// ── the header's count ───────────────────────────────────────────────────

test("the count says how many are shown out of how many exist", () => {
  // A scroll box with no number reads as "these twelve are all of them", which
  // is how a provider serving two hundred models looks like it serves twelve.
  assert.equal(countLabel(0, 0), "No models discovered");
  assert.equal(countLabel(12, 12), "12 discovered");
  assert.equal(countLabel(3, 200), "3 of 200");
});

// ── the footer's summary ─────────────────────────────────────────────────

test("the footer says who answered, and pluralizes", () => {
  // Discovery asks one provider at a time, so "0 models · 1 provider answered"
  // and "0 models · 0 providers answered" are different facts. The old wording
  // — "0 models discovered across 0 providers" — cannot tell them apart, and
  // "across 1 providers" is a number a person stops trusting.
  assert.equal(discoveredFooter(16, 1), "16 models · 1 provider answered");
  assert.equal(discoveredFooter(16, 3), "16 models · 3 providers answered");
  assert.equal(discoveredFooter(0, 1), "No models · 1 provider answered");
  assert.equal(discoveredFooter(0, 0), "No models · 0 providers answered");
  assert.equal(discoveredFooter(1, 1), "1 model · 1 provider answered");
});

// ── who refreshes the catalogue ───────────────────────────────────────────

test("no screen polls while the engine is pushing", () => {
  // The reason the engine watches at all. One sweep serves every open window, so
  // a timer per screen would ask the same eight providers once per screen to
  // learn what they were just told.
  assert.equal(needsOwnRefresh({ live: true, isOpen: true }), false);
});

test("a screen falls back to asking when the push channel is down", () => {
  // The trade when the socket drops: a stale list, or one extra discovery. The
  // list is the worse of the two, and the cost falls to one open panel rather
  // than the whole app.
  assert.equal(needsOwnRefresh({ live: false, isOpen: true }), true);
});

test("a closed panel never refreshes, by either route", () => {
  // Nothing on screen to be stale, and a closed panel asking providers is an app
  // that spends a rate limit on nobody.
  assert.equal(needsOwnRefresh({ live: false, isOpen: false }), false);
  assert.equal(needsOwnRefresh({ live: true, isOpen: false }), false);
});

// ── whose model a provider row would write ───────────────────────────────

const CONFIGS: RoleProvider[] = [
  { role: "librarian", provider: "anthropic", model_name: "claude-x" },
  { role: "design", provider: "openai", model_name: "gpt-x" },
  { role: "planner", provider: "openai", model_name: "gpt-x" },
  { role: "fixer", provider: "openai", model_name: "gpt-x" },
  { role: "scribe", provider: "ollama", model_name: "llama3" },
];

test("a provider row targets only the roles already on it", () => {
  // Widening this to every role would be a different button with a different
  // name. "Point everything at one provider" is a decision, and the settings
  // panel's bulk assign already makes it on purpose.
  assert.deepEqual(rolesOnProvider(CONFIGS, "openai"), ["design", "planner", "fixer"]);
  assert.deepEqual(rolesOnProvider(CONFIGS, "anthropic"), ["librarian"]);
});

test("a provider nobody is on has no roles to write to", () => {
  assert.deepEqual(rolesOnProvider(CONFIGS, "nvidia"), []);
  assert.deepEqual(planApply(CONFIGS, "nvidia", "nvidia/llama-3.1"), []);
});

test("the plan names the model each role moves to and away from", () => {
  const plan = planApply(CONFIGS, "openai", "gpt-5");
  assert.equal(plan.length, 3);
  assert.deepEqual(plan[0], { role: "design", from: "gpt-x", to: "gpt-5", changes: true });
  assert.ok(
    plan.every((s) => s.changes),
    "all three are on gpt-x, so all three are about to change",
  );
});

test("a role already on the chosen model is included but not counted a change", () => {
  // Included so the count means "roles this touches" and the reader can see
  // the one that needs nothing; dropped would make a total silently exclude it.
  const plan = planApply(CONFIGS, "openai", "gpt-x");
  assert.equal(plan.length, 3);
  assert.deepEqual(changedRoles(plan), []);
});

test("only the roles that would really change are written", () => {
  const configs: RoleProvider[] = [
    { role: "librarian", provider: "openai", model_name: "claude-x" },
    { role: "design", provider: "openai", model_name: "gpt-x" },
  ];
  const changes = changedRoles(planApply(configs, "openai", "gpt-x"));
  assert.deepEqual(
    changes.map((s) => s.role),
    ["librarian"],
    "writing to a role that already matches is a wasted request that can fail",
  );
});

test("an empty model plans nothing at all", () => {
  // The picker's field is free text, so an empty string is a reachable state
  // and not a hypothetical one.
  assert.deepEqual(planApply(CONFIGS, "openai", ""), []);
  assert.deepEqual(planApply(CONFIGS, "openai", "   "), []);
});

test("a model is compared whole, not by prefix", () => {
  const configs: RoleProvider[] = [
    { role: "fixer", provider: "openai", model_name: "gpt-4" },
  ];
  assert.equal(changedRoles(planApply(configs, "openai", "gpt-4o")).length, 1);
  assert.equal(changedRoles(planApply(configs, "openai", "gpt-4")).length, 0);
});

test("a role with no model yet is a change, and says so without a 'from'", () => {
  // `from` empty is a real state: a provider was just added and nobody has
  // chosen for it. Rendering "→ gpt-5" beats rendering "undefined → gpt-5".
  const plan = planApply([{ role: "fixer", provider: "nvidia" }], "nvidia", "nvidia/llama-3.1");
  assert.deepEqual(plan, [
    { role: "fixer", from: "", to: "nvidia/llama-3.1", changes: true },
  ]);
  assert.equal(planSummary(plan), "fixer set to nvidia/llama-3.1");
});

// ── the blast radius, in words ───────────────────────────────────────────

test("the summary names the first role and counts the rest", () => {
  // "Apply to 3 roles" says how many. It does not say which, and a bulk edit
  // that names none is one nobody can check against what they meant.
  const summary = planSummary(planApply(CONFIGS, "openai", "gpt-5"));
  assert.match(summary, /^design gpt-x → gpt-5, and 2 more$/);
});

test("a single change is not dressed up as a list", () => {
  const plan = planApply([{ role: "fixer", provider: "openai", model_name: "old" }], "openai", "new");
  assert.equal(planSummary(plan), "fixer old → new");
});

test("a plan that changes nothing has no summary to show", () => {
  // Otherwise the row prints an empty sentence next to a disabled button.
  assert.equal(planSummary(planApply(CONFIGS, "openai", "gpt-x")), "");
  assert.equal(planSummary([]), "");
});

// ── the apply button's own label ─────────────────────────────────────────

test("the apply button asks for a choice before it reports anything", () => {
  // A static render can only ever reach this state, because staging a model
  // needs a keystroke. Everything else in this section is asserted against the
  // function rather than against markup that cannot reach it.
  assert.equal(applyLabel({ staged: "", changes: 0, onProvider: 3, applied: false }), "Choose a model");
});

test("the button never claims a state nobody is in", () => {
  // With nothing staged there is no plan, so "3 roles already on it" is a
  // statement about a state that does not exist — on a control that looks like
  // it has already run.
  const label = applyLabel({ staged: "  ", changes: 0, onProvider: 3, applied: false });
  assert.doesNotMatch(label, /already on it/);
});

test("the button names how many roles it will write to", () => {
  assert.equal(
    applyLabel({ staged: "gpt-5", changes: 1, onProvider: 1, applied: false }),
    "Apply to 1 role",
  );
  assert.equal(
    applyLabel({ staged: "gpt-5", changes: 3, onProvider: 5, applied: false }),
    "Apply to 3 roles",
  );
});

test("a fresh provider with a model picked is told nobody is on it", () => {
  // Reachable only after a keystroke, which is why it is here and not in the
  // markup suite: the alternative is a branch nothing tests.
  assert.equal(
    applyLabel({ staged: "nvidia/llama-3.1", changes: 0, onProvider: 0, applied: false }),
    "No roles on this provider",
  );
});

test("a model every role already runs is reported as such, not as an offer", () => {
  assert.equal(
    applyLabel({ staged: "gpt-x", changes: 0, onProvider: 3, applied: false }),
    "3 roles already on it",
  );
});

test("the success label counts the roles it wrote, and needs a staged model", () => {
  assert.equal(
    applyLabel({ staged: "gpt-5", changes: 3, onProvider: 3, applied: true }),
    "Applied to 3 roles",
  );
  // `applied` with nothing staged is not a state; the label must fall through
  // rather than render "Applied to 0 roles" off the back of it.
  assert.equal(
    applyLabel({ staged: "", changes: 0, onProvider: 0, applied: true }),
    "Choose a model",
  );
});

test("the tooltip says why the button is dead, per reason", () => {
  assert.match(applyHint({ staged: "", changes: 0, onProvider: 3 }), /Choose a model first/);
  assert.match(applyHint({ staged: "gpt-5", changes: 0, onProvider: 0 }), /No role is on this/);
  assert.match(
    applyHint({ staged: "gpt-x", changes: 0, onProvider: 3 }),
    /already runs this model/,
  );
  assert.match(applyHint({ staged: "gpt-5", changes: 2, onProvider: 3 }), /one at a time/);
});

// ── why the list is empty ────────────────────────────────────────────────

test("an unreachable provider says it could not be reached, and why", () => {
  const msg = noModelsMessage("nvidia", { needsKey: true, hasKey: true, discoveryError: "401" });
  assert.match(msg, /Could not reach NVIDIA/);
  assert.match(msg, /401/);
});

test("a keyed provider with no key is sent to the key, not to the logs", () => {
  const msg = noModelsMessage("nvidia", { needsKey: true, hasKey: false });
  assert.match(msg, /API key/);
});

test("a local provider is never told to add a key", () => {
  // "Add its API key" is worse than useless for a server that needs none: it
  // sends the reader to a setting that cannot fix anything. This is the bug the
  // old wording shipped, for every provider with no list.
  const msg = noModelsMessage("ollama", { needsKey: false, hasKey: false });
  assert.doesNotMatch(msg, /API key/i);
  assert.match(msg, /server is running/);
});

test("a configured provider that answered with nothing says exactly that", () => {
  // "Reported no models" is a different fact from "was never asked", and
  // collapsing them sends people to refresh a discovery that already ran.
  const msg = noModelsMessage("groq", { needsKey: true, hasKey: true });
  assert.match(msg, /configured but reported no models/);
});

// ── when the list was last checked ────────────────────────────────────────

test("the footer says how long ago the providers answered", () => {
  // The panel re-discovers on its own now, so the list can change while somebody
  // is reading it. A list that changes by itself and says nothing reads as a
  // model quietly disappearing — so the age is always on screen, and the wording
  // is coarse on purpose: nobody needs seconds of precision here, and "checked
  // 3s ago" would be a value that visibly races.
  const at = 1_700_000_000;
  assert.equal(checkedLabel(at, at, false), "checked just now");
  assert.equal(checkedLabel(at, at + 30, false), "checked 30s ago");
  assert.equal(checkedLabel(at, at + 59, false), "checked 59s ago");
  assert.equal(checkedLabel(at, at + 60, false), "checked 1 min ago");
  assert.equal(checkedLabel(at, at + 600, false), "checked 10 min ago");
  assert.match(checkedLabel(at, at + 7200, false), /^checked at \d{2}:\d{2}$/);
});

test("a request in flight is announced as one, not as a stale age", () => {
  // While a refresh is running, "checked 12s ago" is answering a question
  // nobody asked: the honest thing to say is that something is being asked now.
  assert.equal(checkedLabel(1_700_000_000, 1_700_000_012, true), "checking providers…");
  // Including when there is no previous answer at all, which is the first open.
  assert.equal(checkedLabel(null, 1_700_000_000, true), "checking providers…");
});

test("before the first answer the footer says nothing about age", () => {
  // An empty string, not "checked never": there was no check to report on, and
  // a line of text that only appears once a request has finished is a line that
  // is missing for the first few seconds of the panel being open.
  assert.equal(checkedLabel(null, 1_700_000_000, false), "");
});

test("a clock that disagrees with itself does not render a negative age", () => {
  // NTP correction, a laptop waking from sleep, a machine whose clock was set
  // backwards. "checked -4s ago" is what the arithmetic would otherwise produce,
  // and it is worse than useless: it reads as a bug in the app.
  const at = 1_700_000_000;
  assert.equal(checkedLabel(at, at - 120, false), "checked just now");
  assert.equal(checkedLabel(at + 120, at, false), "checked just now");
});
