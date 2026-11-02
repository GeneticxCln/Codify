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

**What it does not see.** The state it scores is the request (`build_state`): the title, the
description, the mode and the workspace's folder name. It never sees what a run reads afterwards —
files, `read_page`, `fetch_page`, command output — which is where text written to steer a model
usually arrives. So it is a first filter on what the person sent, not a boundary against injected
instructions. The boundaries are the ones that read no text at all: the write gate (`00` §6.9), the
approval before the repository's own code runs (`00` §6.6), and `fetch_page`, which a person can narrow or
switch off (`12`). That is also why a gate that cannot run is skipped (§3) rather than
failing the goal: failing closed would refuse the person's own requests whenever the gate's model is
down, and would protect nothing the gate can see.

## 3. Engines, and honest provenance

The gate reports which engine answered in `laya_decision.payload.engine`:

| `engine` | When | Blocks? |
|---|---|---|
| `sdk` | `make setup-laya` (or `pip install laya` into the engine's interpreter) + the checkpoints it downloads on first use, run in-process on the CPU by default (`Router(device="cpu")`, the two routable checkpoints preloaded) | yes |
| `llm-fallback` | the `laya` role's configured provider answers the same typed contract | yes |
| `skipped` | neither available (no SDK, no reachable provider) | **never** |

A gate that cannot run is a **skipped** gate, never a broken pipeline: `LayaService.decide`
catches everything and the pipeline proceeds exactly as it did before Laya existed. Skipping is
logged (`log` event, level `info`) and surfaced in Settings, so a silent no-op can't hide.

The one exception is the engine-wide `laya_required` setting (0 by default). When it is 1, a gate
that is `unavailable` — configured, but its call failed or returned nothing usable — refuses the
turn or goal with the `laya_required` error instead of running unguarded. A gate nobody configured
is still a deliberate `skipped` gate (`LayaNotConfigured`, `unavailable=False`) and is not refused,
so a fresh install is not locked out by the setting. This is the only mode in which the gate fails
closed; the default stays fail-open because the gate is an optional accelerator, and a missing gate
must not make the engine unusable.

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

**The checkpoints are loaded at engine start, not on the first message.** `LayaService.start_warming()`
runs the load on a daemon thread from the app's lifespan (it returns in milliseconds, so the
engine's readiness and the desktop shell's handshake never wait on it). Measured on the real SDK:
warm-up finishes ~7 s later, and the first decision after that takes ~1.4 s instead of paying the
load itself. A decision that arrives *during* the load waits for it (up to the same timeout)
rather than being skipped, so the first message is neither unguarded nor charged a second,
fallback model call. A turn's gate with no model of its own borrows the model the person picked for
that turn (`docs/09` §10.20); a call that fails is still skipped rather than fatal, but a chat turn
now says so in the gate's card instead of staying silent.

It is a daemon thread on purpose: `asyncio.to_thread` workers are joined at
interpreter exit, so an engine asked to stop mid-load would wait for the load. The price is ~4 GB
of RAM held from launch instead of from the first message; `CODIFY_LAYA_WARM=0` opts out. A load
that fails records why in `sdk_error` and the first decision falls back exactly as before.

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
shell's PATH otherwise, so `make test` and the app share one environment. Installing
the SDK into a different interpreter than the one the app spawns is the failure mode worth
naming: nothing errors, `sdk_available()` is simply false, and every goal quietly pays
the LLM call anyway.

**Installing it is one target, and `make setup` deliberately does not run it.**
`make setup-laya` installs the extra into this checkout's `.venv`; `make doctor` says whether that
interpreter can import it and names the target when it cannot. It is not a runtime dependency and
not part of the default setup because it brings torch and the checkpoints are hundreds of
megabytes — an install that never runs the gate should pay for neither. A packaged app is the same
decision at the other end: it ships without the SDK and gates through the fallback model (`18`
§2), which is why the Settings card names both paths.

**There is no training step to run.** The checkpoints are pretrained and `Router` downloads the
ones it needs on first use (~808 MB for English, ~647 MB for multilingual, upstream's own
numbers) into the Hugging Face cache; `start_warming` pays that at engine start rather than on
the first message. They answer Codify's four questions zero-shot. Fine-tuning is an accuracy
improvement for a domain's own decisions, not a prerequisite: on upstream's typed-decisions
benchmark the base English checkpoint scored 0.362 and the fine-tuned one 0.766. If the gate's
verdicts on this schema ever need that, upstream's fine-tuning notebook is the path; this gate
loads the upstream checkpoint names today and has no setting for a custom checkpoint, and a
fine-tuned model's calibration would have to be checked against the thresholds in §4 first.

**A fine-tuned checkpoint is a setting, not a code change.** Settings → the gate card takes a
Hugging Face repo id in `laya_checkpoint` (`PUT /settings/engine`, the same person-only door as the
conductor's model). A value replaces the built-in Router: `laya.load(repo)` loads the checkpoint at
the repo root, every request is answered by that one model (so there is no language routing), and it
runs on the CPU by default like the Router. Empty is the built-in `english` + `multilingual` pair.
The repo must be in Laya's export format — `rl_agent_config.json`, `model.safetensors`, the
tokenizer and encoder — which is what upstream's fine-tuning notebook produces. The value is read
when the SDK loads, and saving it makes the engine reload the checkpoint in the background; a repo
that cannot be loaded is reported in the gate card and the gate falls back exactly as it does for
any other SDK failure. A fine-tune's calibration is its own, so re-check the thresholds in §4
against it before trusting a block.

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
