# Codify — The Xerg audit: finding wasted model calls, in tokens, from what the engine already records

Xerg ([`xergai/skills`](https://github.com/xergai/skills)) publishes one agent skill: a `SKILL.md` and a README that tell an agent how to
drive Xerg's CLI (`@xerg/cli`, closed source, installed from npm) to audit AI-agent spend in dollars. The request was to see how that
skill could be done in this project, and the answer chosen was a **native audit inside Codify, in tokens**. It follows the shape of
`docs/10` to `docs/14`: what was read, what is taken and refused, what was built, what the engine can and cannot measure, what this does
not claim, and what reopens a row.

## 1. What was read, and what was not

Read: the repository's README, `skills/xerg/README.md` and `skills/xerg/SKILL.md` (the whole skill), and the file layout. Not read, because
it is not published: the CLI itself, its detectors, its pricing catalogue, or its hosted service. **Nothing was copied.** What follows is
about the *method the skill describes*, and every claim about Xerg here is a statement about that text, not a test of its software.

## 2. The table

| What the skill does | Here | Why |
|---|---|---|
| Three evidence-strict findings: retry waste, tool-loop waste, cache churn | **Retry waste taken** (re-ask and fix loop). **Tool-loop waste and cache churn reported as unavailable** | The engine records what retry waste needs and does not record what the other two need (section 3) |
| Seven neutral signals | **Five built in the same spirit** (context outliers and growth, deep loops, re-ask rate, token concentration). Not built: cache-read and Max Mode concentration (no such data here), fixed cadence (not attempted), and "premium model on routine work" (refused) | The last needs a list of which models are premium, and this project has no hardcoded model lists (`CLAUDE.md`). The others need data the engine does not record |
| Dollars, from a reviewed local pricing catalogue | **Refused** | `engine/metrics.py` and `docs/04` §4.7 deliberately keep no price table: a price that silently stops matching the provider is worse than none. Everything here is tokens |
| Coverage before totals; zero findings is never "no waste"; unavailable detectors named | **Taken, as code and as skill text** | The strongest part of the skill, and the part `docs/13` learned the hard way (a report whose tail was cut) |
| Waste only when a failed chain ends in a successful attempt | **Taken** | A re-ask that did not recover is spend, not waste; one with no outcome yet is left out of every rate (`engine/metrics.py` rule 1) |
| Look at the evidence before recommending a fix (`xerg explain`) | **Taken** as `recall` | The conductor already has a read-only tool for past events |
| `--compare` before/after | **Not taken** | The window argument and the saved daily snapshots already let two periods be compared by eye; a stored baseline is a second state to keep honest |
| A CLI run through `npx`, hosted push, hosted MCP, Linear, CI gates, telemetry | **Refused** | A skill here is data and cannot widen `validate_argv`; `npx` is refused by it. Nothing leaves the machine. No second gate mechanism (`CLAUDE.md`) |
| Install and approval staging, version pinning | **Not applicable** | There is nothing to install and nothing external to pin. The skill text keeps the part that matters: it never changes settings or files |
| The skill's file format (nested `metadata:`, about 35 KB) | **Not loadable here** | Skill headers accept only `name`, `description` and `moves`, and bodies are capped at 32,768 characters (`engine/skills.py`) |

## 3. What the engine records, and so what can be measured

Read from the code (`engine/agent_orchestrator.py`, `engine/conductor.py`, `engine/providers.py`, `engine/services.py`), not assumed:

* **Retry waste: measurable.** A same-model re-ask is `usage` (the call that returned unusable text), then `agent_call_failed` with
  `retrying: true`, then `usage` for the repair. The first call's tokens are the candidate waste. There is no attempt number or id linking them,
  so the pairing is by order within one goal, step and role. What happened to the re-ask is not recorded either: if the repair also fails, the
  call falls through to a fallback target or raises. So the outcome is read from what follows for that step: a normal `stage_result`
  (recovered), a `provider_fallback`, an `error` or a stage that did not do its job (unrecovered), or nothing yet (unknown). The fix loop is
  read the same way as the statistics screens read it (`metrics.recovered_steps`), counting only the attempts *before* the last retry.
* **Tool-loop waste: not measurable.** A tool call is recorded only as a clipped log line (first and last 100 characters of its arguments); its
  result is never stored; refused calls are not logged at all. An identical repeated call with an identical result cannot be shown.
  Enabling it would mean recording a hash of each call's arguments and result (section 6).
* **Cache churn: not measurable, and probably not applicable.** No provider adapter parses cache token counts and no request asks for caching,
  so there is no cache lifecycle to price.
* **What no detector can see.** HTTP-level retries inside one call are invisible (they only lengthen its duration). A failed provider call or a
  stream cut mid-body books no tokens, so the tokens it spent are in no total; the audit counts those calls so the gap is stated.

## 4. What was built

* `engine/usage_audit.py`: pure functions over event dicts, no reads, writes or spawns. `audit()` and `format_audit()`.
* `GoalService.usage_audit_events()`: the query. Workspace scope is in the SQL join, the event types are an allow-list, the newest 20,000 are
  read and returned oldest first, and a row whose payload no longer parses is skipped. It is not `_METRIC_EVENT_TYPES` widened, because that
  tuple sizes the statistics screens' budget.
* The conductor tool `audit_usage(days?)` (`engine/conductor.py`, `engine/conductor_tools.py`): read-only, one optional argument, no route
  and no setting. It writes one `log` line, as `recall` does.
* The built-in skill `engine/builtin_skills/usage-audit.md`: the order of steps and the rules above, as instructions. It declares `moves:
  audit_usage, recall`, never names the writing move, and tells the model that agent settings are the person's to change.
* One clause in the conductor prompt, and its ceiling raised to 3,420 characters (`tests/test_conductor_prompt.py` records why).

**The report.** It opens with the coverage block, then the totals, then the findings, then the signals, then one line on what to do next, and
it is built to arrive whole: the conductor cuts any tool result over 12,000 characters, so the audit is bounded to 11,000 and signals beyond
twelve are counted rather than dropped (the lesson of `docs/14` §8, the `scan_code` report).

* **Findings** (evidence-strict): `retry-waste` (recovered / unrecovered / unknown, each with its tokens) and `fix-loop-retry-waste`.
* **Signals** (descriptive, never waste, never a recommendation): `context-outlier` (a role's p90 prompt far above its median),
  `context-growth` (a conductor loop whose prompt tripled), `deep-conductor-loop` (a loop that used most of the real turn cap),
  `reask-rate` (per role and model, with its counts) and `token-concentration`. Their thresholds are constants in the module; a shown signal
  always carries the numbers it came from.

## 5. What this does not claim

* **It is not a bill.** Tokens are what the provider reported; nothing here is a price, a saving or a forecast.
* **Zero findings is not "no waste".** Two kinds of waste are not measured at all, and a third is measured only for the two retry shapes the
  engine can see.
* **A finding is a pattern in recorded events, not a diagnosis.** A high re-ask rate on one model is a reason to read that model's replies.
* **It covers one workspace**, the one the goal belongs to, and the newest 20,000 events of the types it reads. Reaching that cap is reported.
* **The thresholds are judgements,** pinned by tests and open to change when a real install says otherwise.
* **Nothing of Xerg's software was run or tested.** This document makes no claim that its detectors would agree with these.

## 6. If a row is reopened

* **Tool-loop waste** needs the call's arguments and result *hashed* at the point they are known (`Conductor._run_tool`) and recorded as a
  structured field, plus whether the call failed. That changes the event contract (`engine/models.py` `EventType`, `docs/04`'s event table,
  `ui/src/types.ts`, `tests/test_event_type_contract.py`), so it is its own change, with its own decision about what a hash may reveal.
* **Cache churn** needs the adapters to read cache read and write tokens (`normalize_usage`) and, for Anthropic, a request that actually
  asks for caching; until then there is nothing to churn.
* **Dollars** would reverse `docs/04` §4.7. The only form that fits its reasoning is a rate file the person supplies and owns, with every
  figure labelled an estimate from their rates; nothing bundled and nothing fetched.
* **A results pane** needs a read-only route in `docs/04` that takes the boot token, a section in the statistics drawer, and mounted tests
  (not source-reading ones, `ui/tests/sourceTextTestBudget.test.ts`). The precedent is `docs/13` §6.
* **Install-wide audit** (all workspaces) is a different scope with a different privacy question; it is not what `recall` does either.
