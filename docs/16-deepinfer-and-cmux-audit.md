# Codify — The DeepInfer and cmux audit: two upstreams, and the little that is actually portable

Two repositories, read in the same week and asked the same question: what here belongs in Codify? They are not
competitors — [DeepInfer](https://github.com/deepseek-ai/DeepInfer) is the layer this project is a *client* of, and
[cmux](https://github.com/manaflow-ai/cmux) is the layer this project already *has*. That is the finding, and it is
why this document is short on adopted code and long on refusals: the parts worth having are small, and the parts that
look impressive are on the other side of a boundary.

It follows `docs/11`–`15`: what was read, what is taken and refused (and by which invariant), what was built or is
proposed, what this does not claim, and what reopens a row.

**Nothing was copied.** cmux is `GPL-3.0-or-later` (its server components `BUSL-1.1`) and this repository is MIT, so
copying its Swift would relicense this tree; DeepInfer is Apache-2.0 and would be legally easier and is still not
copied, for the reason in `docs/08` and `CONTRIBUTING.md`. What follows is about *ideas*, expressed in this project's
own code and its own words.

## 1. What was read, and what was not

**DeepInfer.** The repository's landing page (features, supported hardware, what a complete service still needs, the
benchmark provenance, the licence) and `doc/inference_deployment_qa.md` in full as served (the Q&A is truncated at the
kernel-library section). Not read: the kernels, `build.sh`, `run_sweep.sh`, `eval/`, `examples/`, or any source.
Nothing was built or run.

**cmux.** The repository's landing page in full: the feature list, the author's own account of why it exists, the
shortcut tables, session restore and the rules around resume commands, the FAQ, and the licence. Not read: the Swift
sources, the CLI reference, `cmux.json`'s schema, the browser-automation and skills docs, `docs/local-tmux.md`. Nothing
was installed or run — it is a macOS application and this project is Linux.

Every claim below about either project is a claim about those texts.

## 2. What each one is, in this project's terms

**DeepInfer is a serving framework**: kernels, expert and data parallelism, an asynchronous batch scheduler, a KV cache
with three days of retention, speculative decoding, structured output, and deterministic sampling, on SM100 GPUs and
Ascend NPUs. Its Q&A opens with the sentence that frames the difference: *a model defines the boundary of what is
possible; serving those capabilities to a massive user base at a reasonable cost is the core problem an inference
framework has to solve.* Codify never loads a weight. It sends a prompt to an endpoint somebody else serves, so a
serving optimisation is portable **only** if it changes what is sent, or what is recorded about what was sent.

**cmux is a terminal for other people's agents**: a Ghostty-based macOS app whose additions are vertical tabs, split
panes, an in-app browser, an inbox fed by terminal escape sequences and agent hooks, and a CLI plus socket API over all
of it. Codify's shell already has the tabs, the splits, a real PTY, a browser the model may read and move, an editor, a
jailed machine, and an inbox (`docs/09` §10.18). So the overlap is large and the honest gap was one thing: **the terminal
pane was deaf.** A program running in it could ask for attention in the standard way and nothing here heard it. It is
built now (§6 item 1).

## 3. DeepInfer: the table

| What it does | Here | Why |
|---|---|---|
| Optimised kernels (FlashMLA, DeepGEMM, DeepEP, TileLang) | **Refused** | Codify never loads weights and never runs a forward pass |
| DP/CP attention, EP + EPLB for MoE, E/P/D disaggregation | **Refused** | Cluster scheduling for a model this project does not serve |
| DSpark / confidence-scheduled verification (speculative decoding) | **Refused** | Decoding happens on the other side of the wire |
| Asynchronous batch scheduler | **Refused** | There is no batch here: one role call is one request, and the provider batches it or does not |
| Batch-invariant inference | **Refused, and the claim it protects is already policy** | No client can hold a hosted model to it; `docs/08` §6 already says a number from a hosted model is a sample and repeats are the only honest answer |
| **SWA bounded replay** | **Taken as an idea, and built as `bound_replay`** — §6 item 4 | Replaying a *bounded suffix* rather than everything is the right shape for the conductor's growing transcript; `bound_replay` in `engine/conductor.py` drops the oldest tool results first and never a person's or the model's own words |
| **Deterministic sampling** (fixed `seq_seed`) | **Refused** | A seed is a request, not a guarantee: a hosted provider may ignore it, and on a local server batching can change the result anyway — which is why DeepInfer ships *batch-invariant inference* as a separate feature. `docs/08` §6 already answers a number that moves: repeat the run |
| **Structured outputs** (regex / JSON schema / EBNF) | **JSON schema taken, and built** — §6 item 5; regex and EBNF refused | Every role already answers JSON by contract, and `providers.py` asks for the role's own schema where a model advertises `json_schema`, one rung down at `json_object` where it does not. A grammar is a second language to maintain for replies that are already objects |
| **KV-cache retention measured in days** (deployment Q&A) | **Taken, and built** — items 6–7 | The client-side analogue is prompt caching: a stable prefix is cheaper and faster when the provider reports a hit. This reopened `docs/15`'s cache row: the adapters had to read cache token counts (item 6) before a prefix could be repaid (item 7) |
| "Required for a complete inference service (not included in this release)" | **Already the house style** | `docs/12` §4, `13` §5, `14` §6 and `15` §5 all state what is missing rather than only what works |
| Benchmark provenance: the GPU count, one scale-up domain, and the script that reproduces it | **Already the house style** | `docs/08` §6 and §8 — what a number may claim, and the setup that lets someone else rebuild it |
| KV storage on 3FS, cross-datacenter P/D, autoscaling, FP4 formats | **Refused** | Operator concerns of a cluster that is not this application |

One reframing worth keeping: DeepInfer optimises *tokens per GPU-hour*. Codify's scarce resource is the model's
attention inside one turn, and the nearest honest analogue of throughput is **a change that verifies without a retry** —
which is what `docs/15`'s `audit_usage` already measures, in tokens, from events the engine records.

## 4. cmux: the table

| What it does | Here | Why |
|---|---|---|
| **OSC 9 / 99 / 777 terminal notifications**, plus `cmux notify` and agent hooks | **Taken, and built** — §6 item 1 | The standard escape sequences are data a program emits, not a capability it gains. Before this, the bytes in `terminal.rs` went to xterm unchanged; `OscScan` now reads one out as an inbox entry and still passes it through |
| **Notification panel, unread badges, jump to the most recent unread** | **Taken, and built** — §6 item 2 | The inbox, its unread count and the per-tab unread dot already existed (`docs/09` §10.18); this added the palette's `Needs you` row, which opens the newest unread, and the latest line beside the project name |
| Notification *rings* around panes | **Refused** | Decoration on state the tab strip and the inbox already show |
| Sidebar metadata: git branch, PR status/number, cwd, listening ports, latest notification | **Partly taken, and the branch half is built** — §6 items 2 and 3 | Branch, dirty count and ahead/behind are a read of the workspace's own git, through the existing read-only table, and are shown on the panel header's project line. PR status needs a GitHub token and a request that leaves the machine; listening ports need the process table outside the workspace |
| In-app browser beside the terminal, with a scriptable API | **Already built** | `docs/09` §7.2–7.5, and `navigate_page`/`click_page`/`type_page` behind a setting |
| Snapshot the accessibility tree, click by element ref, read console and network | **Not now** — item 8, as a decision | Clicking by role and name beats guessing selectors; it also needs a rule about what a snapshot may contain (never a field's value) that this repository would have to own |
| Vertical tabs, horizontal/vertical splits | **Already built** | `docs/09`, and §12 for the paired pane |
| SSH workspaces, cloud VMs, an iOS client, mobile attach | **Refused** | The engine binds `127.0.0.1` and no pane here is a remote session; nothing in this app leaves the machine except `fetch_page` under `docs/12`'s rules |
| Claude Code Teams spawn as native splits; subagents as panes | **Refused** | Codify's eight roles are not processes and the conductor is not a shell. Drawing them as panes would say they are |
| Session restore: layout, cwd, terminal scrollback, browser history | **Already built, except scrollback** | Layout and browser history are restored (`tabPersistence.ts`). Terminal scrollback is deliberately not persisted: `terminalHistory.ts` keeps it in memory for the session, and `docs/09` §2.2 records that decision (not yet). Item 9 was the narrow question, not a copy |
| Agent resume via hooks, with trusted command prefixes and secrets dropped before storing | **Refused** | A stored command that runs on restore is argv from state, and no stored state becomes argv here (`docs/00` §6.9). That cmux drops tokens and passwords *before* writing a resume binding is the right instinct; the difference is that this project does not need the feature enough to open that door |
| `cmux.json` project commands launched from the palette | **Refused** | A cloned repository would name argv. `.codify/skills/` is the sanctioned analogue, and a skill is instructions, never a capability |
| CLI + Unix socket: create workspaces, split panes, send keystrokes, drive the browser | **Refused** | That is a second door into a window that already has one, and the existing one needs the boot token. Keystrokes are the person's (`docs/09` §14); a socket any local process can write is not a boot token |
| Desktop notifications and sound | **Refused** | In-app only is a decision with its reason written down (`docs/09` §10.18) |
| Browser import: cookies, history and sessions from 20+ browsers | **Refused** | Reading another application's credential store. `docs/03` §1.5–1.6 is where the browser's answer to "what may the page have" lives |
| Ghostty config compatibility, libghostty rendering, Swift/AppKit | **Not applicable** | This shell is WebKitGTK and xterm (`docs/09` §7.1, `ui/src/terminalRenderer.ts`) |

## 5. What is already here, so the plan is not a rebuild

Tabs, splits, a real PTY with batched output, an xterm pane with saved scrollback for the session, a browser pane the
model may read and move, an editor the model may read and edit but not save, a jailed machine, a command palette, a
shortcut table, a notification inbox with unread counts and a persisted list, and layout/browser-history restore. Each
is in `docs/09`. Any plan that starts by rebuilding one of them has read cmux and not read this repository.

## 6. The plan: what is worth taking

Seven one-PR items and two decisions, selected from the tables above; everything not here is refused there. Ships 1 and
2 do not depend on each other, and within a ship the only dependencies are the ones stated. Each item names the test
that has to fail without it, because that is the rule here (`CONTRIBUTING.md`).

### Ship 1 — the shell earns its keep

**1. A terminal can ask for attention.** *Small. `src-tauri/src/terminal.rs`, `ui/src/shellEvents.ts`,
`ui/src/notifications.ts`, `App.tsx`; `docs/09` §7.1 and §10.18.*
- **Read the sequences where the bytes are.** An `OscScan` beside `Utf8Chunker` in `read_decoded` recognises
  `ESC ] 9 ; text` (iTerm2/Growl), `ESC ] 777 ; notify ; title ; body` (rxvt) and `ESC ] 99 ; …` (kitty), terminated
  by BEL or ST, and carries an unterminated tail between reads the way the chunker carries a partial character. Fields
  past the ones a form defines are ignored; title and body are clipped to the inbox's own 120 and 300.
- **Deliver them in order with the text.** `read_decoded`'s channel carries text or a notification (or `pump_output`
  grows an `on_notify`), and `batch_output` hands both over in order, so a notification never arrives ahead of the
  output that made it. `open` emits `terminal-notification`; `machine::run_reader` drops them for now — a machine's
  shell asking for attention is its own decision, not a side effect of a terminal feature.
- **The inbox learns one kind and one target.** `NotificationKind` gains `"terminal"` (tone `info`), the id is
  `terminal:<termId>:<seq>` with a sequence the shell assigns per notification, so two prints are two entries and a
  repeated delivery is one. `NotificationTarget` gains `{kind: "tab", tabId}`; `validTarget` accepts it in memory and
  **refuses it on load**, because `term-3` from a previous run is not this run's tab, and a stored jump that lands
  somewhere unrelated is worse than no jump.
- **Rules, each a test.** At most one entry per terminal per 3 s; none while that terminal's tab is the active one; a
  non-string id or title is not a notification; the bytes still reach xterm unchanged; a closed tab's notification
  names nothing. No invariant is touched — a notification is state for the person, not authority for the model — and
  that belongs in the module docs.
- **Built** in this change: `OscScan` in `terminal.rs` with the tests above, `terminal-notification` on the wire,
  the `terminal` inbox kind and its live-only tab target (a terminal entry is not restored at all — its id and
  its shell belong to one run), the window's rules in `notifications.ts` and `App.tsx`, and the drawer's sentence. **One limit, stated:** OSC 99 is read one sequence at a time — kitty's chunked and
  base64 forms are refused rather than half-decoded — so one of its notifications can arrive title-only or
  body-only. OSC 9 and 777 are read whole.
- **Accepted when** `printf '\033]9;done\007'` in a Codify terminal makes one entry, a build script looping it
  makes at most 20 a minute, and the pane in front makes none. Gate: `make check` + `make check-tauri`.

**2. The palette takes you to what needs you.** *Small. `notifications.ts`, `commandPalette.ts`, `App.tsx`,
`Sidebar.tsx`.*
- One `PaletteAction` `{type: "focus-newest-unread"}` (data, like every palette item), listed first while anything is
  unread, wired in `App.tsx`'s action switch, and one pure `newestUnread(list)` in `notifications.ts`.
- The newest unread's title as a quiet second line under the project name in the panel header, and nothing when the
  inbox is empty.
- **No new chord, and that is a decision.** Every `Ctrl`+letter is a control character to a focused terminal (`Ctrl+U`
  is kill-line), and `shortcuts.ts` exists on the rule that it does not steal the shell's keys. `Ctrl+K` then Enter is
  the shortest honest path; `SHORTCUT_HELP` does not change.
- Tests: `newestUnread` order and emptiness; the palette lists the action only when something is unread; a mounted test
  that selecting it opens the goal, the settings tab or the terminal tab.
- **Built** in this change: `newestUnread` in `notifications.ts`, `{type: "focus-newest-unread"}` in the palette's
  data with the `Needs you` row first, the action wired through the drawer's own `handleOpenNotification`, and the
  title under the project name in the panel header. **No chord was added**; the row is reached through Ctrl+K.
  `commandPalette.test.ts` and `notifications.test.ts` cover the order and the absence when everything is read, and
  three tests in `notificationsApp.test.ts` mount the window and press Ctrl+K for each destination — goal, settings
  page and terminal tab.

**3. The panel header says which branch, and whether it is dirty.** *Small–medium. `engine/app.py`, `engine/git.py`,
`engine/git_readonly.py`, `engine/models.py`, `ui/src/api.ts`, `App.tsx`, `Sidebar.tsx`; `docs/04` §6.*
- `GET /workspaces/{id}/git` (boot token like every route) answering `{branch, detached, dirty, ahead, behind}` from
  `git status --porcelain=v2 --branch`, through the existing read-only table and its `GIT_OPTIONAL_LOCKS=0` runner —
  **no new spawn site**.
- A directory that is not a repository, a repository with no commits, and a detached head are three quiet answers
  (`branch: null`, a sentence in `reason`), never an error banner; the header shows nothing when there is nothing true
  to show.
- Refetch on workspace change, on window focus, and when a step finishes (`step_status` on the goal stream); a 5 s
  cache and no timer.
- Tests: `test_workspace_git.py` against a real temporary repository (clean, dirty, detached, unborn, not a repo); the
  route is picked up by `test_every_route_is_authenticated.py`; a static render that the branch and count are in the
  header and absent when null.
- **Built** in this change: `GitService.status` reads `--porcelain=v2 --branch` through the read-only table's own
  runner (`GIT_OPTIONAL_LOCKS=0`, no new spawn site), `GET /workspaces/{id}/git` answers `WorkspaceGit`, and the
  panel header draws the branch, a dirty dot and the ahead/behind counts with a 5 s cache refetched on a workspace
  switch, window focus and `step_status`. The three no-branch states are quiet 200s with a `reason`; the header
  draws nothing for them. `test_workspace_git.py` uses real temporary repositories, and `workspaceGit.test.ts`
  renders the header clean, dirty and empty. `docs/04` §3 owns the route.

### Ship 2 — the engine wastes fewer tokens

**4. Bound the replay.** *Medium. `engine/conductor.py`; `docs/01` §5.3; `docs/15`.*
`Conductor.run` appends every tool result for the whole turn, each clipped at 12,000 characters
(`MAX_TOOL_RESULT_CHARS`), so a 14-call loop can carry well over a hundred thousand characters into a local model's
32,768-token window.
- Two constants (`MAX_CONTEXT_CHARS`, `KEEP_WHOLE_RESULTS`) and one `bound_replay(messages)` called before each
  `_call`.
- Walk the tool messages oldest-first and replace content with a stub that names the tool, its original size and that it
  was dropped (`"[dropped: read_file returned 8,412 characters; call it again if you need it]"`), until the whole list
  is under the budget. Never touch the newest *N* results, and never touch a user or assistant message: the person's
  words and the model's own replies are the conversation, not evidence.
- A single newest result over the budget is kept whole (clipping already bounds it), and the tool message keeps its
  `tool_call_id`, so the protocol pairing is untouched.
- Tests: thirty results never leave a request over budget; the newest four are intact; every stub names its tool and
  size; a re-read of a dropped file still returns its text. The effect is measured, afterwards, by `audit_usage`'s
  `context-growth` on the same trace.
- **Built** in this change: `bound_replay` in `engine/conductor.py`, called at the top of every loop iteration, with
  `tests/test_conductor_replay.py` driving a ten-read turn to its last request and pinning the direct function's
  order, idempotence and small-result rule. `docs/01` §5.3 and `docs/15` §4 say what it guarantees and how to see it.

**5. Ask for the reply shape, not just for JSON.** *Medium. `engine/replies.py`, `engine/providers.py`, the orchestrator
call sites; `docs/01` §3, `docs/06` §5.*
Every role's reply is one JSON object (`coerce_object` in `replies.py`), and each role's parser reads a fixed set of keys.
- A `REPLY_SCHEMAS` table beside `coerce_object`, one per role, `additionalProperties: false` and every property
  required (what strict mode needs).
- `complete(..., json_schema=None)` added to the base contract with a default on **every** adapter — the same pattern
  `num_ctx` and `keep_alive` already follow — and only the OpenAI-compatible adapter reads it.
- `_supports_json_mode` becomes a per-model capability (`"schema" | "object" | None`), cached by endpoint and model,
  because the advertisement is per model. The payload sends the schema when it says `schema`, `json_object` when it
  says `object`, and nothing when it says `None`; a 400 that names the field downgrades that model one rung and retries,
  exactly as today's `_json_mode` does.
- Tests, in `test_provider_json_mode.py`: a server advertising `json_schema` receives the role's schema and the body
  validates against it; one advertising only `json_object` receives `json_object`; a 400 downgrades twice and the third
  call is plain prose. Plus the pair test: every key a parser reads is required by its schema.
- Done when the fake-server tests pass and `audit_usage`'s retry waste on the same task is lower — measured, not
  asserted.
- **Built** in this change: `REPLY_SCHEMAS` beside `coerce_object` (`replies.py`), `json_schema` on `complete` for
  every adapter (defaulted, read only by the OpenAI-compatible one), and a per-model `"schema" | "object" | None`
  read from the same `/models` body — a `capabilities` object, or OpenRouter's `supported_parameters`
  (`structured_outputs` / `response_format`) — cached per endpoint and model. The second shape was added after this
  document was first written: reading only a `capabilities` object resolved every hosted OpenRouter model to `None`, so
  the schema rung was dead on the provider it was meant to help. The payload sends the role's schema when the
  model advertises `schema` and a caller supplied one, `json_object` one rung down, and nothing at `None`; a 400
  steps that model down and retries, remembering the lower rung. `tests/test_reply_schemas.py` proves every key a
  parser reads is a required property and every nested object is strict, and `test_provider_json_mode.py` drives
  the three rungs and the downgrade against a fake server. **One limit, stated:** what a provider *does* with the
  schema is its own guarantee — the parser and its repair still decide whether a reply is usable. Retry waste on
  a real task has not been measured here.

**6. Record what the cache did.** *Small. `engine/providers.py` (`normalize_usage`), `engine/usage_audit.py`;
`docs/04` §4.7, `docs/15` §3.*
- `usage` gains `cache_read_tokens` and `cache_write_tokens` where a provider reports them (Anthropic's
  `cache_read_input_tokens`/`cache_creation_input_tokens`, OpenAI's `prompt_tokens_details.cached_tokens`); totals stay
  input + output tokens.
- `audit_usage` reports cache reads as a **signal**, never a saving, and its "cache churn: not measurable" line becomes
  a measurement.
- Tests: `normalize_usage` over both provider shapes, a missing field as zero, and the audit's signal arithmetic.
- This is a payload field, not a new event type, so `test_event_type_contract.py` is untouched; `docs/04` §4.7 and
  `docs/15` move with it.
- **Built** in this change: `normalize_usage` reads both shapes and always emits both counts (zero where a provider has
  none), and Anthropic's counts — which its `input_tokens` excludes — are added into `input_tokens` so a cached call is
  never booked as a smaller prompt; `audit_usage` totals them, reports a `cache-reads` signal with its share and the
  written count, and replaces the cache-churn unavailability line with a coverage sentence. **One limit, stated:** the
  provider's counts do not say which write a later read repaid, so reads and writes are measured and a churn rate is not.

**7. Cache the prefix that is repaid.** *Medium, and only after item 6. `engine/providers.py`, the conductor's call
path; `docs/01` §5.3.*
- The conductor re-sends one stable system prompt up to 14 times in a turn: that is the one prefix where a cache write
  is read back. Mark it cacheable where the protocol has a field (Anthropic's `cache_control` on the system block) and
  nowhere else. **Never a one-shot role call**, where the write premium is paid and never read.
- No threshold is invented: below the provider's minimum nothing is cached and the counts come back zero, and item 6's
  audit says whether it ever hit. A 400 or a refusal falls back exactly as json mode does.
- Tests: the conductor's request body carries the mark on the system block only; a role call's does not; a rejected
  request is retried without it.
- Done when a multi-call turn reports cache reads and a one-shot role call reports zero, on a real provider.
- **Built** in this change: `complete_with_tools` takes `cache_prefix` on every provider (default `False`, ignored by
  all but Anthropic), the conductor passes `True` on every call, and Anthropic sends `system` as one text block marked
  `{"type": "ephemeral"}`. Only that block is marked — the tool schemas and transcript are not a stable prefix — and
  a role call's `complete` body is untouched. A refusal is retried once without the mark and turns it off for that
  provider, exactly as a rejected `response_format` falls back; a second refusal surfaces rather than looping.
  `tests/test_prompt_cache.py` covers the conductor's body, an unmarked role call, both refusals and a remembered
  refusal, and the signature guard now names `cache_prefix` too. **Not verified against a live Anthropic key here:**
  what is proven is the request body and its fallback, not that a real endpoint reports a hit.

### Ship 3 — decisions, not code

**8. A page snapshot with element references.** *Decided: taken under the rules in `docs/09` §7.6; not built.*
Only while `browser_actions` is on, because it exists to aim a click or a type. A snapshot carries roles, names and refs
and **never a field's contents**, is bounded in size, and its refs are valid only for the snapshot they came from; it
adds no verb beyond the clicks and types that already exist.

**9. Does a terminal's output ever reach the disk?** *Decision first.*
cmux restores scrollback across restarts. This project deliberately does not, and `terminalHistory.ts` says why: shell
output is whatever was on screen, including `cat .env`. The recommendation is **not yet**. If it is ever taken it is
opt-in, read-only (a record, labelled as one, that never accepts a keystroke), written through the clipboard history's
secret-skipping rules, and its size bounded — and `docs/09` §2.2 now records the decision: not yet.

**Status.** Items 1–7 are built; items 8 and 9 are decisions, not code, and both are recorded (`docs/09` §7.6 and §2.2). Two limits are stated rather than
papered over: item 5's retry-waste reduction has not been measured on a real task, and item 7's live-provider
confirmation — a multi-call turn reporting cache reads — has not been run here; its request body and fallback are
tested. Item 5 also proves the schema it sends, not that a provider honours it. Item 1's OSC 99 is read one
sequence at a time (kitty's chunked and base64 forms are refused rather than half-decoded).

## 7. What we need

- **No new dependency, no vendored code, no service, no key.** DeepInfer needs nothing from this side: a DeepSeek model
  served by it is reached through the OpenAI-compatible adapter that already exists. cmux has nothing to install on
  Linux.
- **One structural risk, named:** item 1's scanner sits in the PTY reader, so a sequence split across two reads must be
  reassembled. `Utf8Chunker` is the existing precedent for exactly that, and the Rust test that splits one across three
  reads is the proof.
- **One contract change, named:** item 6 adds two fields to the `usage` payload. It is not a new event type, so
  `test_event_type_contract.py` is untouched, but `docs/04` §4.7 and `docs/15` §3 own that payload and move in the same
  PR.
- **One adapter signature change, named:** item 5 gives `complete()` a keyword on every adapter, defaulted, the same way
  `num_ctx` and `keep_alive` already are. An adapter that ignores it must still accept it, and a test walks the adapters
  so one cannot be forgotten.
- **One maintenance pair, named:** item 5 puts a schema next to each parser in `replies.py`. The test that a schema and
  its parser agree about required fields is what keeps it from drifting; without that test the schema is a second
  description of the contract with nothing checking it.
- **One route, already swept:** item 3 adds `GET /workspaces/{id}/git`, and `test_every_route_is_authenticated.py` is
  the test that must see it.
- **Two decisions, now written:** item 8 (`docs/09` §7.6: taken under its rules, not built) and item 9 (`docs/09`
  §2.2: not yet). Neither blocks Ships 1 and 2.
- **The order to build them in:** 1 (the visible one), 4 (the cheapest engine win), 6, 5, 2, 3, 7. Items 2 and 3 do not
  depend on 1; 7 depends on 6.
- **The gate is the usual one:** `make check` / `make ci`, plus `make check-tauri` for item 1. Nothing here needs a new
  gate leg.

## 8. What this does not claim

- **Nothing of either project was run, tested or compared for speed.** No benchmark number of DeepInfer's was verified,
  and none is relevant: its numbers are cluster-wide, on hardware this project will never have.
- **No code was taken**, and none may be: cmux is GPL-3.0-or-later against this repository's MIT, and this project ships
  no third-party source anyway.
- **cmux is macOS-only**; half of what it does well (native rendering, Ghostty config, macOS notifications) has no
  counterpart on WebKitGTK, and this document does not pretend otherwise.
- **The borrowed ideas are ordinary.** Notifications from escape sequences, bounded transcript replay, JSON-schema
  replies and cache accounting are not inventions of either project; they are the ideas those two put in front of us
  in a form worth copying into our own words.
- **The plan was a proposal, and items 1–7 are built.** Each one has its own doc update and test; items 8 and 9 are
  recorded decisions, not code. The limits listed in §6's status stand, including the two that were not verified
  against a live provider here.

## 9. If a row is reopened

- **CLI / socket control** would need an authenticated client and a fixed operation table — which is the surfaces
  bridge (`engine/surfaces.py`) and already exists. A new door is not the fix; a new op in that table is.
- **Agent resume** would need a stored command with a trust and signature story. The answer today is that no stored
  state becomes argv. That changes only with an invariant, not with a feature.
- **PR status** needs a token and a request that leaves the machine. `docs/12` is where such a request would be argued,
  and it is about pages the model asked for, not about this project's repository.
- **Desktop notifications** are reversible on request: they are a system surface, not a capability, and the inbox's rules
  would carry over unchanged.
- **Scrollback on disk** is `docs/09` §2.2's decision (not yet), and would arrive with the clipboard history's secret
  rules, not before them.
