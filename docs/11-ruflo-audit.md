# Codify — The Ruflo audit: which ideas were borrowed and which were refused

Ruflo (formerly Claude Flow, `ruvnet/ruflo`) is an agent harness for Claude Code and Codex. The question put to
this repository was how it could be "implemented" here. The answer is that most of it cannot, and the reasons are
the invariants in `docs/00` §6, not taste; a few of its ideas could be borrowed, and this document records which,
what each became, and what was **not** read, so the table cannot be mistaken for more than it is. It follows the
shape of `docs/10`'s Hindsight audit, which did the same job for memory.

No Ruflo code, Node dependency, MCP client or server came out of it, and none is planned.

## 1. What was read, and what was not

* **Read:** the README of the npm package `ruflo` 3.51.0 (MIT), as published on the npm registry on 2026-10-02:
  about 23 KB.
* **Not read:** its source, its `docs/`, the issues and ADRs it links, its benchmark gist, its user guide. Nothing
  was installed or run.
* **Every Ruflo statement below is the README's statement.** Its numbers (a claimed 89% routing accuracy, 314 MCP
  tools, 12 workers, 27 hooks, speedups against other frameworks) are its own and untested here, and the README's
  counts disagree with each other (98 agents in one table and 100+ in the next; "All 35 plugins" and "33 native
  plugins + 21 npm plugins"). Nothing in this repository depends on any of them.
* **Not in the README, and so relied on by no row:** an "Agent Booster", a tiered model router, a claims
  protocol. They were named in the descriptions this audit started from; the README says nothing about them
  (the words do not appear in it), so they are neither kept nor rejected here. ("Handoff" appears once, in the
  description of a team checklist document.)
* Third-party reports that parts of the project are stubs were seen along the way. They are unverified, are not
  relied on, and nothing below depends on whether they are right.

## 2. What Ruflo says it is

"**Agent = Model + Harness.** The model writes; the harness gives it tools, memory, loops, sandboxes, and
controls so it can actually work." Its stack, as drawn in the README: a CLI and MCP server, a router and 27
hooks, a swarm layer (a queen, a topology, consensus), 100+ specialised agents, memory and learning (a vector
database, learning patterns), and several model providers; plus plugins, background workers and federation
between machines.

The first sentence is this repository's own design rule from the other side: *judgement is the model's;
authority is the engine's* (`engine/conductor.py`, `docs/09` §10.6). The difference is everything after it. Ruflo
grows the harness by adding agents, topologies and automation around the model; Codify keeps eight roles, one
writer and one door to a command, and grows what the one conductor can see and say.

## 3. The table

| Ruflo, as its README states it | Here | Why |
|---|---|---|
| A coordinating "queen" over the agents | **Have.** The conductor: one loop decides which of the other components runs and in what order. | `docs/09` §10.6, §10.14. Sub-agents never coordinate each other. |
| 100+ specialised agents; agents spawned on demand (`agent_spawn`, `swarm_init` are named) | **Reject.** | Invariant 1: exactly eight roles, no create or delete of slots. The conductor chooses *moves*, not new agents. |
| Hierarchical, mesh and adaptive topologies | **Partial.** A hierarchy only. Independent steps of a `parallel` goal run together because the batcher can prove their paths are disjoint. | The conductor has no parallel move: it has no proof of disjointness to offer (`docs/09` §10.14). |
| Consensus among agents (Raft, Byzantine, Gossip) | **Reject.** | One writer (invariant 9). An opinion is checked by a different role (the critic) and then by a person pressing Start, not by a vote among models that read the same text. |
| "Intelligent" task routing; several providers with smart routing and failover | **Partial.** The gate classifies the request (`docs/05`); a role, and the conductor, fall back to a second target on a provider-class failure (`docs/01` §2.2.1, §5.0). | No routing table of models: models come from live discovery and are chosen per role by the person (`docs/06`; invariant 2; CLAUDE.md, "no hardcoded model lists"). |
| 27 hooks that route, learn and coordinate automatically | **Reject.** | A hook that runs a command around a tool call is a second door to a command (invariant 6) and, if it writes, a second writer (invariant 9). Nothing here runs unless it is a move. |
| HNSW vector memory; "self-learning" patterns | **Reject** the vectors; **partial** on learning. | `docs/10` §4.4: no embeddings and no second store (invariant 7). What exists is mechanical: observations with proof counts, now fading with age (`docs/10` §6.3). Nothing a model writes about itself feeds a later prompt, because that is third-party text (`docs/10` §7). |
| An MCP server (314 tools) and `claude mcp add` | **Reject**, in both directions. | An MCP *client* would admit tool surfaces the engine does not define (invariants 6, 9); an MCP *server* would be a way around the boot token and the settings routes (invariants 2, 3). Both also need a Node runtime. |
| 30 skills; a plugin marketplace | **Have** skills, **reject** installable plugins. | A skill here is text from the workspace and never a capability (`docs/09` §10.14); a plugin that runs code is a supply-chain door. |
| 12 background workers (audit, optimise, test gaps) | **Reject.** | Nothing starts without a person's turn or Start. There is no daemon, and a worker that writes would be a second writer. |
| Zero-trust federation between machines | **Reject.** | The engine binds `127.0.0.1` (invariant 3); this is a local-first product. |
| A prompt-injection and PII guard (AIDefence) | **Have** the injection half. | The Laya gate scores injection on every request (`docs/05`). PII detection is not built. |
| Token and cost tracking with budgets | **Have**, without prices. | `usage` events for every model call, the conductor's now included, and call and move budgets. No price table (`docs/08` §6). |
| Goals broken into plans, progress tracked; reusable workflow templates | **Have.** Plan steps; skills. | Plus a run-to-run note for the conductor (§4). |
| Claude, GPT, Gemini, Cohere, Ollama | **Have.** | `docs/01`, `docs/06`. |
| "Agent = Model + Harness" | **Agreed**, and already the design rule. | §2. |

## 4. What was borrowed, and what was not

Be exact about the origin, because "borrowed from Ruflo" is easy to say about work that was not.

* **From Ruflo's README, as an idea:** a note the coordinator keeps for itself so a long goal survives its own
  context (its goals and workflows plugins: "break big goals into plans and track progress"). Built as `todo`
  (`docs/09` §10.6): bounded, one line an item, shown to the next run as the model's *own notes and not
  instructions*, never shown to a sub-agent, not recallable, and read by nothing in the engine.
* **From its cost-tracking plugin, as an idea:** seeing what a run spent. The conductor's own model calls were
  never booked; they now are, like every role's (`docs/04` §1.4, `usage`).
* **From its plugin model, loosely:** a skill says what it provides. A skill's header may now name the moves it
  is written around (`moves:`); that is a hint checked against the moves that exist and never a grant
  (`docs/09` §10.14).
* **Not from Ruflo at all**, and said so plainly: the conductor owning every step to completion and pausing with
  a reason when it cannot (`docs/09` §10.14, §10.18a); honest moves and visible failures; real call and move
  budgets; schemas valid for every provider dialect; linters and type checkers on the command allowlist
  (`docs/03` §1.4); `ask_user` (`docs/09` §10.19); observations that fade (`docs/10` §6.3); the conductor's
  prompt naming every tool it has; the benchmark report saying `driver: recipe` (`docs/08` §6). These came from
  reading this repository's own conductor, which was in control of the pipeline only until it was inconvenient.

## 5. If a rejected row is ever reopened

Each rejection above is a statement about an invariant, so reopening one is a change to that invariant and
starts at `docs/00` §6, not at the feature. Two have a shorter road than the rest, and say what they would need:

* **Parallel conductor moves** need the conductor to be given what the batcher already has, a proof that two
  steps touch disjoint paths, so that "run these together" is something the engine checks and not something a
  model asserts.
* **An MCP surface** would need a design for admitting a tool to the menu that is not `validate_argv` and not a
  move, and an answer for what an admitted tool's results are (third-party text, quoted as such, like a page).
  Without both it widens the engine.

Nothing here should be read as a verdict on Ruflo's quality. It is a different product with a different
trust model, and this document is only about what this one may take from it.
