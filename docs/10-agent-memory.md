# Codify — Agent Memory: the Hindsight audit, and what this build does about it

Normative for `engine/recall.py`, `engine/metrics.py`'s `recovered_steps`,
`GoalService.recall_events` / `thread_recall`, and the conductor tools `recall`
and `recall_threads`. Roles/tools: `01`. Events and SQL: `04`. Security: `03`.
Workspace/conversation model: `09`.

Upstream: [vectorize-io/hindsight](https://github.com/vectorize-io/hindsight).
This document was written from Hindsight's **README alone** — no third-party
source was read or committed (see §7, and `docs/08` for the policy).

## 1. Why this audit exists

A turn used to start from nothing. Codify already records every stage outcome,
every failure classification, every retry and every recovery in `events` — and
then used them for exactly one thing: the statistics screen. `engine/metrics.py`
turned them into dashboards and `engine/stats_history.py` froze a day's
document, and **no model ever saw a byte of it**. `list_conversations` was never
offered to a model either. A turn therefore began from the user's prompt and the
files on disk, every single time, having learned nothing from the last four
hundred runs in the same repository.

Hindsight is an agent-memory system whose pitch is that this is the wrong shape.
Reading its README against this repository produced a concrete comparison — and
the comparison, not the transplant, is what this document records.

## 2. Hindsight's model, in the terms this codebase uses

Hindsight exposes three verbs over one store. Restated against Codify's
vocabulary:

| Hindsight verb | What it means here |
|---|---|
| **retain** | Something already writes history: every conductor move publishes `events` (docs/04 §1.4). The store exists; only the read was missing. |
| **recall** | Nothing read history for a model. This is the gap §4 closed. |
| **reflect** | Nothing distills history. Repeated identical failures are stored as separate rows forever; no consolidation step turns them into a durable belief with a proof count. Still open — §6. |

Its retrieval side is four parallel strategies — semantic vector search, BM25
keyword search, graph traversal, and temporal — whose results are fused by
reciprocal rank fusion and then reranked. Its consolidation side refines rather
than appends: repeated evidence strengthens an observation's proof count
instead of adding a near-duplicate row.

## 3. The gaps, verified before anything was built

Every line below was checked with a grep against the tree at audit time, not
inferred from the README:

- **12-turn same-conversation history** — `TURN_HISTORY_TURNS` in
  `engine/executor.py` served the *current* thread only. A new conversation
  learned nothing from its neighbours.
- **Workspace knowledge** — a `CODIFY.md` prior reached the librarian
  (`docs/04` §4.9). That is the user's written memory, not the engine's.
- **Zero semantic/vector/BM25/rerank** — the only retrieval was literal
  substring (`LibraryService.search`), anchored to single lines.
- **`events` written and never read by a model** — the statistics screen's
  queries are the only cross-event reads in `engine/`.
- **`list_conversations` never offered to a model** — the thread list existed
  for the UI and for nobody else.

## 4. What was built, and what each piece takes from the audit

Nothing in Hindsight's storage, server, or provider layers was ported. What was
taken is the *shape of the questions* — "has this specific thing happened
before, and did we ever get past it?" — and the honesty rules for answering
them. Three features landed; each maps to a gap in §3.

### 4.1 `recall` — the event grain (closes: events written and never read)

`engine/recall.py` + `GoalService.recall_events` + the conductor tool
`recall`. Contract detail lives in `docs/04` §1.4.1 and `docs/03` §1.8; what
matters for the comparison:

- **It answers Hindsight's recall question at event grain**: past step
  outcomes, failures, retries — with recovery marked, inherited from
  `metrics.recovered_steps` so there is one definition of "recovered", shared
  with the statistics screen.
- **Containment is an allow-list, not a filter.** `RECALLABLE` names the event
  types and, per type, the payload fields a model may see. `diff` and
  `library_evidence` are excluded on purpose: stored third-party text must not
  reach a model labelled "my own past history". The risk being managed is
  *salience*, not access — recall grants nothing `read_file` does not.
- **Bounded at every joint**: `MAX_SCAN_EVENTS` (2 000), `MAX_MATCHES` (20),
  `MAX_FIELD_CHARS` (300), `MIN_QUERY_CHARS` (3); log rows gated to
  `warn`/`error`; workspace scope in the JOIN.
- **The label is the security property.** `format_recall` says the result is
  *recorded outcomes, not evidence about the current code*, and an empty
  result is stated as an absence, not a proof it never happened.

### 4.2 `recall_threads` — the thread grain (closes: thread history unread)

`GoalService.thread_recall` + `recall.search_threads` + the conductor tool
`recall_threads`. A new conversation asks what this workspace's **earlier
conversations** were about and how their runs ended — thread names, the asks
(goal descriptions, clipped in the query), and per-thread
`completed`/`failed`/`cancelled` counts — instead of starting from nothing.

The two grains are deliberately separate tools, not one tool with a flag:
`recall` answers "what happened inside a run"; `recall_threads` answers "what
has this workspace been asked for before, and how did those runs end". Asks are
labelled as what was *asked for*, never as proof anything was done, and the
spec tells the model to pair the two.

### 4.3 Keyword retrieval in `search_code` (closes: zero BM25)

`LibraryService.search(mode="keyword")`: a second strategy beside substring —
the same walk under the same caps, indexed into an **in-memory** FTS5 table (one
row per file) and ranked by explicit `ORDER BY bm25()`, for multi-word
questions no single line answers (`docs/04` §4.0). The fallback is real, never
a flag: a probe establishes availability, and when FTS5 is absent — or the
connection fails, or FTS5 refuses the query — the literal substring answer is
computed and labelled `strategy: "substring_fallback"`. FTS5 is a SQLite
compile-time option, so availability is asked per interpreter, per call.

### 4.4 The two Hindsight strategies not built, and why

- **Semantic vector search is not here, and the reason is §7-adjacent: a
  vector row would need an embedding model in the build, and this repository
  has no catalog of models in it (`docs/06`). Models come from live provider
  discovery; an engine-side embedding requirement would hardcode one.
- **Graph and temporal strategies are not here** because the event store is
  already temporal (every row carries `timestamp`, and `recall`'s window is a
  `timestamp >= cutoff`) and relational (recovery pairing *is* the graph walk,
  delegated to `metrics` rather than reimplemented). A second spine would be a
  second answer to questions the schema already answers.

Reciprocal rank fusion and cross-encoder reranking are absent for the same
reason: with one retrievable corpus per question, fusion has nothing to fuse.
If `reflect` (§6) ever produces a second corpus — observations beside events —
fusion becomes the honest next step, and this section is where that decision
should be revisited.

## 5. What was explicitly rejected

| Hindsight piece | Why it does not fit |
|---|---|
| Postgres (+ pgvector) backing store | Violates docs/00 §6.7 — one SQLite file, no second database. The memory store is `events` and `goals` in `~/.codify/codify.db`. |
| LiteLLM wrapper and the 25-provider catalog | Violates `docs/06` — there is no catalog in the build; models come from live provider discovery. |
| Cross-encoder reranking | A model dependency for a corpus that does not need it yet (§4.4). |
| MCP server surface | The engine's tools are conductor moves with an authority story (`docs/01` §5.1); an external tool protocol is a second door with none. |
| Persisted vector/keyword indexes | A second artifact to go stale beside the workspace; keyword search re-walks under the standard caps instead. |

## 6. Reflect: built — formation, brief, and the durable store

Hindsight's reflect distills retained history into **observations** —
deduplicated beliefs with supporting evidence, exact quotes, and a proof count,
refined rather than overwritten. Codify now has the *formation* half of that:
`recall.distill_observations` groups one scan of `recall_events` rows into
observations by subject — a failure code first, a normalised message when the
code is missing — each carrying its proof count and one capped example. It is
**mechanical on purpose**: subjects come from what the engine itself
classified, never from model prose, because a summary of prose inherits its
trustworthiness. The positive lesson — *a retry got past this failure* — is
granted only by `metrics.recovered_steps` over the same scan, the one
definition of recovery the statistics screen already uses; a `goal_status
COMPLETED` from a different goal proves nothing about this subject's code, and
the grouping deliberately refuses to invent that link. A retry-pairing bug the
brief tests caught is worth recording: `fix_retry` reads `step_id` from the
column, not the payload, and a pass that never reaches the pairing reports
every known failure as unrecovered — the same shape as the allow-list bug §4.1
closed.

Observations reach a model two ways. The `brief` (§6.1) injects the most recent
`MAX_BRIEF_OBSERVATIONS` (5), `step:`-subjects filtered out — they are pairing
plumbing, not prose a model can act on. `format_observations` labels every
proof count with its bound: *proof: N event(s) in the most recent 2 000
scanned*.

Still **open**, deliberately: nothing here persists. Every answer is recomputed
from the rows that still exist — which cannot go stale, and cannot outlive the
`MAX_SCAN_EVENTS` window. The durable store is §6.2.

### 6.2 The durable store: `observations`, refined by the engine after each run

The reflect half is now closed end to end. A fourth table in the same SQLite
file (§5's constraint — one database, no second store):

```sql
observations (id, workspace_id → workspaces, subject, lesson, example,
              proof, evidence, created_at, refined_at, UNIQUE(workspace_id, subject))
```

**One row per workspace and subject — refined, not appended**, the shape §2
takes from Hindsight. The engine's consolidation pass refines it: when a run
reaches a terminal status (`ExecutorService._set_status` → `_consolidate`), it
re-scans `recall_events` **with event ids**, distills, and calls
`GoalService.record_observations`. A subject seen again gets its proof count
*added* and its evidence ids merged (newest first, deduplicated) — not a second
row. The lesson and example take the newest scan's wording, so a recovery
flipping a lesson positive survives the next scan. `step:`-subjects are never
stored: one retry is pairing plumbing, not a durable belief.

Three properties are load-bearing and each is asserted:

- **The engine writes it, and nothing else.** docs/00 §6.9's reading is why
  the hook is the terminal status transition rather than any model reply:
  memory is what a future turn *trusts*, and a conductor does not get to write
  what the next turn will be told as fact. `record_observations` is the only
  write path, and it is called from `_set_status`'s best-effort consolidation —
  a consolidation failure logs a `warn` and never fails a finished run.
- **The scope is the store's too.** Recording from one workspace's scan
  refines only that workspace's rows; another workspace's failures stay out
  (tested against two workspaces, like every other scope claim here).
- **The pairing is fed its own order.** The one bug the store tests caught is
  worth recording: `_recovery_pairs` assumes *chronological* rows (its
  dashboard caller orders on timestamp then sequence), and `recall_events`
  returns newest-first — feeding the scan straight in made the pass arrive
  before the retry and scored every recovered step unrecovered.
  `form_observations` now sorts oldest-first on the same key before pairing.

`GoalService.observation_rows` reads the store; `recall.search_observations`
caps (`MAX_OBSERVATIONS = 8`) and filters it; `format_observations_result`
labels it — *distilled from recorded runs, not evidence about the current
code*, with the proof count stated as what makes it believable rather than
what makes it unnecessary to check. `recall` searches events;
`search_observations` searches the beliefs; neither is the other.

### 6.1 The brief: memory injected, not requested

`recall.build_brief(thread_rows, event_rows, current_thread_id=…)` composes
what a new turn should know into one bounded string — the most recent threads
(other than the current one) with their run outcomes and first ask, and the
distilled observations. `ExecutorService._memory_brief` feeds it the same two
reads the tools serve (`thread_recall` + `recall_events`) and `_conduct`
splices the result between the user's prompt and the gate's intent brief, so
the conductor starts already knowing the workspace's history without calling
anything. Two properties are asserted, not intended: the current thread is
**excluded by id, not by position** (a goal created before its conversation
breaks "the newest row is mine", and the test constructs exactly that case);
and an empty workspace gains **no section at all** — the brief returns `""
rather than a header over an absence. A brief that cannot be computed is a
`warn` and an unchanged prompt, because memory is an upgrade, never a
prerequisite.

### 6.3 Old lessons fade

An observation never aged. A one-off failure from last year, long since fixed, carried the same weight and the
same words as one from yesterday, and sat in the brief until five newer ones pushed it out: a stale lesson is
worse than none, because the model is told it is what this workspace's history has *taught*. Hindsight's
temporal strategy is the reference; the version here is the smallest one that stops the lie.

* **Strength is the proof count halved every 30 days** since the failure was last seen
  (`recall.observation_strength`, `OBSERVATION_HALF_LIFE_DAYS`). The brief leaves out an observation whose
  strength has fallen *under* `BRIEF_FLOOR` (0.5), so a failure seen once is in it for a month, eight proofs last
  about three months, and what stays says how long ago: *proof: 3 event(s) in the most recent 2 000 scanned,
  last seen 12 days ago*.
* **At read time only.** From rows and events that already exist: no column, no migration, nothing rewritten.
  `recall` (the tool over the raw events) still finds the old failure when someone asks for it. Only the
  *unprompted* brief forgets.
* **"Last seen" is the newest event behind the lesson, when the scan can see one.** The consolidation pass
  (§6.2) refines every subject still in its scan window after every run, so `refined_at` of a quiet workspace's
  year-old failure says *yesterday*, and trusting it would make the decay a no-op exactly where it is needed.
  The row's `refined_at` is used only for a lesson the scan cannot see (its events have left the window). A
  lesson with no recorded time at all is kept and not dated rather than treated as ancient.
* **A faded lesson is not brought back as a new one.** The scan's own derived observations are aged by their
  events the same way, and a subject the store holds is never re-derived over it.

Proven by `tests/test_recall_decay.py`, with an injected clock so each boundary (the floor itself, a day, a
half-life) is exact.

## 7. Provenance rules for anything memory-adjacent

- **No third-party source is committed here.** This document was written from
  Hindsight's README, not its source; `benchmarks/vendor.py` exists if a
  SHA-pinned, licence-recorded snapshot is ever genuinely needed (docs/08).
- **No model catalog in the build.** Any memory feature that needs an embedding
  model is a `docs/06` decision before it is a storage decision.
- **One SQLite file.** Any new memory store is a table in `~/.codify/codify.db`
  or it does not ship (docs/00 §6.7).
- **The allow-list is the boundary.** Anything new that puts stored content in
  front of a model extends `RECALLABLE`'s pattern — named types, named fields,
  clipped — and updates `docs/03` §1.8 in the same change.
- **An absence is not a proof.** Every memory result is bounded and therefore
  partial; the wording must keep saying so.
