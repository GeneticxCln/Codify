---
description: Checklist for changing what a role may do, what it must return, or when it runs. Use before editing ROLES, ROLE_JOB, ROLE_TIMING in engine/models.py or DEFAULT_PROMPTS in engine/default_prompts.py.
argument-hint: "[optional: a role name — e.g. verifier, critic, scribe]"
allowed-tools: Read, Grep, Glob, Edit
---

# /role-change

`engine/models.py` holds **three parallel tables keyed by the same eight role names**,
and `engine/default_prompts.py` a fourth. A change to a role's abilities that updates
one and not the others is the normal way this drifts, and the in-file warning says so:

> The engine is the only place this is defined: a second copy in the UI is how the
> screen and the enforcement drift apart.

$ARGUMENTS, if given, names the role; otherwise treat every role as in scope.

## 1. Know which tables you are changing

| Table | What it drives | Who reads it |
|---|---|---|
| `ROLES` | the valid role set — **exactly eight, never added to or removed from** | validation, `docs/00` §6.1 |
| `ROLE_JOB` | one line per role, for the settings screen | the UI's Agent Roles screen |
| `ROLE_TIMING` | when the role runs, for the settings screen | the UI's Agent Roles screen |
| `DEFAULT_PROMPTS` | the system prompt sent to the model | every role invocation |

Adding a ninth role is out of scope: `docs/00` §6.1 is a non-negotiable invariant, and
slot creation is not a feature this checklist unlocks.

## 2. If the role's abilities changed

Abilities are enforced by the engine, not requested of the prompt. So a prompt edit
alone is not a behaviour change, and a behaviour change is never *only* a prompt edit.

Check, in this order:

1. **Is the new ability already enforced somewhere?** A role that must not write is
   kept from writing by the engine refusing the request, not by the prompt asking
   nicely. If a new ability needs enforcing, the change is in `engine/sandbox.py`,
   `engine/git.py`, `engine/fs.py` or the executor's tool routing — and each of those
   has an invariant and a test attached.
2. **Does the prompt still describe the enforced behaviour?** If the engine refuses
   something the prompt now invites, the prompt is wasting a round trip. If the prompt
   forbids something the engine allows, the model is being told a lie about its own
   tools.
3. **Does the return contract change?** Most prompts specify JSON only, with a named
   shape. If the shape changes, the parser changes, and the test that proves an
   unparseable reply is handled gracefully must change with it. The fallback path
   (`docs/04` §4.6) only tries the second provider for specific failures — a new parse
   failure is a decision about whether that list grows.
4. **Every table above, in the same change.** A role whose `ROLE_JOB` text still says
   "never writes" after it was taught to write is the drift this command exists to
   stop.

## 3. If the role's timing changed

`ROLE_TIMING` and the executor must agree. "Once per goal" and "once per step" are
different code paths — `engine/executor.py` decides which — and changing one without
the other produces a role that runs when the goal did not expect, or not at all.

The stage sequence appears in more than one place: the pipeline in `docs/00` §4, the
role table in `README.md`, and the `stages` check in `benchmarks/manifest.json`, which
asserts the exact order. A reorder breaks the benchmark, and the benchmark is the thing
that would have told you.

## 4. Do not drift the UI

`engine/models.py` is the only place roles are defined, and the UI is a reader. If the
change alters a role's name, its abilities or its timing, grep `ui/src` for a second
copy — a hardcoded role list, a display map, an ordering array. The settings screen
showing a role the engine no longer has is the visible half of the same bug.

## 5. The test, or it did not happen

An engine change needs a test that fails without it. For a role change that means, at
minimum, a test asserting the new ability is enforced (or newly refused) — not a test
asserting the prompt text contains a new phrase. The suite exists to catch what this
project has actually gotten wrong; a test that reads a string back cannot.

Then run the gate: `make lint typecheck test`, plus `make test-streams` if the change
touches streaming, and `/gate` to route the rest.

## Report

Which of the four tables changed, what enforces the behaviour, the test that fails
without the change, and any doc now untrue (`docs/01` for orchestration, `docs/00` for
the role set, `docs/04` for the return contract).
