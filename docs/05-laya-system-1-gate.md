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
librarian → design → planner → fixer → verifier → critic → scribe   (System-2, unchanged)
```

The gate runs in `ExecutorService.run_planning` **before** the first LLM call. Rationale: the
expensive, non-deterministic part of the system is the seven-LLM pipeline, and the cheapest way to
protect it is to reject hostile or underspecified requests before any of it runs. A blocked goal
therefore has **zero** `plan_steps` and **zero** provider calls — verifiable in the event stream.

## 3. Engines, and honest provenance

The gate reports which engine answered in `laya_decision.payload.engine`:

| `engine` | When | Blocks? |
|---|---|---|
| `sdk` | `pip install laya` + local weights, run in-process on the CPU by default (`Router(device="cpu")`, the two routable checkpoints preloaded) | yes |
| `llm-fallback` | the `laya` role's configured provider answers the same typed contract | yes |
| `skipped` | neither available (no SDK, no reachable provider) | **never** |

A gate that cannot run is a **skipped** gate, never a broken pipeline: `LayaService.decide`
catches everything and the pipeline proceeds exactly as it did before Laya existed. Skipping is
logged (`log` event, level `info`) and surfaced in Settings, so a silent no-op can't hide.

The routable checkpoints are preloaded on purpose: without it, traffic that alternates languages
rebuilds a checkpoint per request (upstream measured 7–10 s per switch). Only `english` and
`multilingual` are preloaded, not `Router(preload=True)`'s all three: the third, `typed-decisions`,
is reachable only through `model=`, `task=` or `auto_task_detection`, none of which the gate uses.

Set `CODIFY_LAYA_SDK=0` to force the fallback path, and **the test suite sets it on every
run** (`tests/hermetic.py`, beside the parent-pid variables it also always clears). The
SDK is optional by design, and that is exactly what made it a hazard in the suite: a
developer who followed the install advice got a `make test` that downloaded checkpoints
and preloaded them on the first gate decision, and five tests silently changed what they
proved — the fallback tests saw `engine == "sdk"`, the skip tests saw a verdict. A test
that wants the SDK path asks for it by name: it installs a fake `laya` module and passes
`disabled=False`. `test_hermetic_run_pins_the_sdk_off` fails if the pin is ever dropped.

Outside a test run, leaving the variable unset is what you want: the whole point of
installing the SDK is that `sdk_available()` is true and the gate answers in-process.
Measured here, `Router(preload=True)` on a free GPU cost ~9.7 s on the first decision in a
process (checkpoint load) and ~31 ms on every one after, against ~20.8 s and ~1.4k tokens for the
LLM fallback it replaces. **On the CPU** (the default, below), with the GPU hidden and the process
capped at four cores: loading `english` + `multilingual` takes ~8 s and peaks at ~4.4 GB of RAM
(all three checkpoints: ~35 s and ~6.1 GB), and a decision takes **~1.2 s**.

**The gate runs on the CPU unless `CODIFY_LAYA_DEVICE` says otherwise.** The machine this runs on
is usually one where Ollama already fills the GPU with the agent's own model and the desktop needs
some too. Loading ~6 GB of checkpoints onto it does not only crawl through CUDA out-of-memory
retries (90 s for a "hi"); it races the display server for the last VRAM. One such run logged 3,389
`nvidia-drm: Failed to allocate NVKMS memory` errors in a single minute, which is the compositor
failing to allocate its buffers: the whole desktop froze. A pre-flight gate must not be able to do
that, and ~1.2 s a decision costs nothing against the 20+ s model call it guards. Set
`CODIFY_LAYA_DEVICE=cuda` (or `cuda:1`) to opt back in on a GPU with room to spare.

**The SDK never runs on the event loop.** `decide` is `async`, but the checkpoint load and
`predict` are blocking, so they run on a worker thread (`asyncio.to_thread`). They used to run
inline, and a slow one froze the *whole engine*: on a machine whose GPU Ollama already filled,
the first decision for "hi" took 90 s of CUDA out-of-memory retries, and for those 90 s the
engine could not answer the UI's `POST /turns` (so the composer kept the text and the button had
no goal id to stop), the health probe, or any other stream. Three rules keep it that way:

- **A timeout.** `CODIFY_LAYA_TIMEOUT_S` (default 30 s, three cold loads). Past it the gate is
  reported `unavailable` — the same "a gate that cannot run is a skipped gate" policy as any
  other failure, so the pipeline goes on — and the worker keeps running in the background.
- **One call at a time, never a queue.** A request that arrives while an earlier one is still
  inside the model is skipped at once (`the laya SDK is still busy…`) instead of stacking worker
  threads behind a model that is already stuck. When the stalled call finishes, the next request
  uses the SDK again; a timeout is a verdict on one request, not a switch-off, and it is kept
  out of `sdk_error` so Settings does not show a stale failure.
- **`CODIFY_LAYA_DEVICE`** is passed to `Router(device=…)`; unset or blank means `cpu`, as above.

**The SDK has to be in the interpreter that runs the engine.** The desktop shell spawns
the checkout's own `.venv/bin/python3` when it has one and `python3` from the login
shell's PATH otherwise, so `make test` and the app share one environment. Installing the
SDK into a different interpreter than the one the app spawns is the failure mode worth
naming: nothing errors, `sdk_available()` is simply false, and every goal quietly pays
the LLM call anyway.

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

`GET /settings/runtime` is the other half of that answer. This route says *which engine is
gating*; that one says *which interpreter is gating*, and whether it can import the SDK at all.
The failure it exists for is invisible from the gate's own report: the SDK installed into
`.venv`, the shell spawning `/usr/bin/python3`, and every goal quietly paying the fallback
model's latency for a package sitting in the same checkout. `sdk_installed` is `false` in that
state, which is true and useless — the report now names both interpreters and says which one is
running. The same facts are on stderr at boot (`engine/capabilities.py`), so they are in
`codify.log` before anyone thinks to open Settings.

## 6. Role slot

`laya` is a real eighth slot in the registry (`ROLES`), seeded at first migration, so its
provider/model/prompt are Settings-only like every other role. It is hidden from the pipeline
ordering in the UI (it renders first, labelled "Laya — System-1 Gate") and it never receives a
`PlanStep`.
