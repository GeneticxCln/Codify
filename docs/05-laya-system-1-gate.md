# Codify — Laya System-1 Pre-Flight Gate

Normative for the `laya` role and the pre-flight gate. Roles/pipeline: `01`. Data: `04`. Security: `03`.

Upstream: [NandhaKishorM/laya](https://github.com/NandhaKishorM/laya).

## 1. What Laya actually is

Laya is not another chat model, so it is *not* wired in as a pipeline stage at all. It is a
**non-autoregressive, multilingual decision engine**: you hand it a state and a set of *typed
questions*, and it answers them in one forward pass (upstream reports ~33 ms), with a Router that
selects the checkpoint per request and reports which one it used.

| Primitive | Meaning | Codify's use |
|---|---|---|
| `choice` | pick one label from declared criteria | `intent` |
| `score` | grade the state on an ordered scale | `risk` |
| `noul` | calibrated probability in `[0,1]` | `prompt_injection`, `needs_clarification` |

Two properties make it a good fit here:

1. **Nothing to parse, nothing to hallucinate.** There is no free text to coax into JSON, so the
   failure mode that plagues every JSON-returning LLM stage (invalid output → failed goal) does not
   exist. `AgentOutputInvalid` can never be raised by the gate.
2. **Calibrated probabilities.** The upstream models are trained against strictly proper scoring
   rules, so `noul: 0.97` means something. That is what makes a *blocking threshold* defensible
   instead of a vibe — and the threshold that fired is always reported back to the user.

## 2. Placement: a gate, not a stage

```
goal created
    │
    ▼
┌─────────────────────────────┐
│ Laya gate (System-1)        │  typed intent / risk / injection / ambiguity
│ engine/laya.py              │  one forward pass, no tokens, no files
└───────┬─────────────────────┘
        │ blocked (injection ≥ 0.85) → goal FAILED, code `laya_blocked`, planner never runs
        ▼
librarian → planner → fixer → verifier → critic → scribe   (System-2, unchanged)
```

The gate runs in `ExecutorService.run_planning` **before** the first LLM call. Rationale: the
expensive, non-deterministic part of the system is the six-LLM pipeline, and the cheapest way to
protect it is to reject hostile or underspecified requests before any of it runs. A blocked goal
therefore has **zero** `plan_steps` and **zero** provider calls — verifiable in the event stream.

## 3. Engines, and honest provenance

The gate reports which engine answered in `laya_decision.payload.engine`:

| `engine` | When | Blocks? |
|---|---|---|
| `sdk` | `pip install laya` + local weights, run in-process via `Router(preload=True)` | yes |
| `llm-fallback` | the `laya` role's configured provider answers the same typed contract | yes |
| `skipped` | neither available (no SDK, no reachable provider) | **never** |

A gate that cannot run is a **skipped** gate, never a broken pipeline: `LayaService.decide`
catches everything and the pipeline proceeds exactly as it did before Laya existed. Skipping is
logged (`log` event, level `info`) and surfaced in Settings, so a silent no-op can't hide.

`Router(preload=True)` is deliberate: without preload, traffic that alternates languages rebuilds
a checkpoint per request (upstream measured 7–10 s per switch).

Set `CODIFY_LAYA_SDK=0` to force the fallback path (useful for tests and for judging the
LLM-based contract on its own).

**Context limits.** Laya's checkpoints have 512–1024 token contexts, so an arbitrary user prompt
cannot fit whole. `build_state` clips over-long requests to 4000 characters **keeping both ends**
(`…[N chars elided]…`), because an injected instruction is as likely to be appended at the end of a
request as stated up front. Never widen this without checking the checkpoint's context, and never
switch to head-only clipping.

**Why not the built-in presets?** Upstream ships `laya.guard_questions()` (jailbreaks, injections,
leaks) and `laya.router_questions()`. Those are better-tuned for the SDK path, but using them here
would mean the SDK and the fallback answer *different* questions — and the fallback exists precisely
so an install without weights behaves the same as one with them. `LAYA_QUESTIONS` is therefore one
shared schema; revisit this if the fallback is ever removed.

## 4. Policy (normative)

| Signal | Threshold | Action |
|---|---|---|
| `prompt_injection` (noul) | `≥ 0.85` | **block** — goal `FAILED`, error code `laya_blocked` |
| `prompt_injection` (noul) | `≥ 0.50` | warn (`log` level `warn`) |
| `risk` (score) | `≥ 1.5` of 2.0 | warn |
| `needs_clarification` (noul) | `≥ 0.80` | warn |
| `intent == "ops_command"` | — | warn |

Only injection can block, and only at high confidence. Warnings never gate execution: the human
sees them in the chat and decides. Thresholds live in `engine/laya.py` and are echoed in the
payload (`payload.policy`) so the UI never has to hardcode them.

## 5. Wire format

`laya_decision` event payload:

```json
{
  "engine": "sdk",
  "answers": {
    "intent": {"choice": "code_change", "confidence": 0.9},
    "risk": {"score": 0.0},
    "prompt_injection": {"noul": 0.02},
    "needs_clarification": {"noul": 0.05}
  },
  "routing": {"model": "laya-noul-en", "repo": "NandhaKishorM/laya"},
  "blocked": false,
  "block_reason": null,
  "warnings": [],
  "skipped_reason": null,
  "provider": "laya",
  "model": "laya-noul-en",
  "policy": {"injection_block_threshold": 0.85, "risk_warn_level": 1.5, "clarify_warn_threshold": 0.8}
}
```

Answers are read tolerantly (nested `{"choice": …}` or flat), because both shapes appear in the
wild. `GET /settings/laya` returns the capability report: `sdk_installed`, `sdk_disabled`,
`sdk_error`, the question set, and the policy. It loads no weights.

## 6. Role slot

`laya` is a real seventh slot in the registry (`ROLES`), seeded at first migration, so its
provider/model/prompt are Settings-only like every other role. It is hidden from the pipeline
ordering in the UI (it renders first, labelled "Laya — System-1 Gate") and it never receives a
`PlanStep`.
