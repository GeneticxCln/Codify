# Graph Report - Codify  (2026-09-20)

## Corpus Check
- 15 files · ~8,457 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 214 nodes · 551 edges · 12 communities
- Extraction: 84% EXTRACTED · 16% INFERRED · 0% AMBIGUOUS · INFERRED: 89 edges (avg confidence: 0.51)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Codify — Sub-Agent Orchestration Spec
- Codify — Settings App Spec (Sub-Agent Configuration)
- 1. Security additions for the multi-agent/multi-provider system
- Codify — Architecture Overview (v2)
- 3. Updated phased roadmap
- 3. Provider abstraction
- 4. Agent JSON contracts
- 1. Domain models
- 2. Data model
- db.py
- ExecutorService

## God Nodes (most connected - your core abstractions)
1. `ExecutorService` - 32 edges
2. `ApiError` - 29 edges
3. `GoalService` - 28 edges
4. `AgentRegistryService` - 26 edges
5. `AgentOutputInvalid` - 24 edges
6. `ProviderError` - 23 edges
7. `WorkspaceService` - 23 edges
8. `AgentConfig` - 20 edges
9. `PlanStep` - 20 edges
10. `FileSystemService` - 18 edges

## Surprising Connections (you probably didn't know these)
- `api_error()` --references--> `ApiError`  [EXTRACTED]
  engine/app.py → engine/services.py
- `AgentOutputInvalid` --uses--> `FileSystemService`  [INFERRED]
  engine/executor.py → engine/fs.py
- `AgentOutputInvalid` --uses--> `PathEscapeError`  [INFERRED]
  engine/executor.py → engine/fs.py
- `AgentOutputInvalid` --uses--> `Event`  [INFERRED]
  engine/executor.py → engine/models.py
- `AgentOutputInvalid` --uses--> `ProviderError`  [INFERRED]
  engine/executor.py → engine/providers.py

## Import Cycles
- None detected.

## Communities (12 total, 0 thin omitted)

### Community 0 - "Codify — Sub-Agent Orchestration Spec"
Cohesion: 0.20
Nodes (9): 1. The 5 fixed agent roles, 2.1 Provider is agnostic, 2.2 `AgentConfig`, 2.3 Store, 2.4 `DEFAULT_PROMPTS`, 2. Data model, 3. Adapters (by protocol, not slug), 4. Registry / Orchestrator / API (+1 more)

### Community 1 - "Codify — Settings App Spec (Sub-Agent Configuration)"
Cohesion: 0.20
Nodes (9): 1. Principle, 2. Layout files, 3. Screen, 4.1 `AgentConfigCard.tsx`, 4.2 `useAgentConfigs.ts`, 4. Components, 5. Tauri, 6. Read-only elsewhere (+1 more)

### Community 2 - "1. Security additions for the multi-agent/multi-provider system"
Cohesion: 0.14
Nodes (23): BaseModel, lifespan(), dumps(), Any, row_to_dict(), AgentConfig, AgentConfigUpdate, ErrorBody (+15 more)

### Community 3 - "Codify — Architecture Overview (v2)"
Cohesion: 0.25
Nodes (7): 1. What changed from the original OCDify/Codify draft, 2. Gaps found in the original plan, 3. Refined component list, 4. High-level flow (updated), 5. Document set, 6. Invariants (non-negotiable), Codify — Architecture Overview (v2)

### Community 4 - "3. Updated phased roadmap"
Cohesion: 0.14
Nodes (13): 1.1 API key storage, 1.2 Local-provider SSRF guard, 1.3 Engine–Desktop auth token, 1.4 Retained from v1, 1. Security additions for the multi-agent/multi-provider system, 2. Persistence layer, 3. Updated phased roadmap, 4. Settled defaults (+5 more)

### Community 5 - "3. Provider abstraction"
Cohesion: 0.11
Nodes (17): 1.1 Workspace, 1.2 Goal, 1.3 PlanStep, 1.4 Event, 1. Domain models, 2. SQL (`~/.codify/codify.db`), 3. HTTP (Engine), 4.1 Planner (+9 more)

### Community 6 - "4. Agent JSON contracts"
Cohesion: 0.17
Nodes (8): ABC, AnthropicProvider, BaseProvider, OllamaProvider, OpenAICompatProvider, ProviderError, Exception, validate_local_base_url()

### Community 7 - "1. Domain models"
Cohesion: 0.13
Nodes (26): api_error(), auth(), cancel_goal(), create_goal(), create_ws(), get_agent(), get_goal(), get_ws() (+18 more)

### Community 8 - "2. Data model"
Cohesion: 0.16
Nodes (12): AgentOrchestrator, FileSystemService, PathEscapeError, Exception, Path, CommandNotAllowed, _is_flag(), _is_workspace_path() (+4 more)

### Community 9 - "db.py"
Cohesion: 0.60
Nodes (5): Connection, connect(), default_db_path(), Path, seed_agents()

### Community 11 - "ExecutorService"
Cohesion: 0.26
Nodes (6): AgentOutputInvalid, ExecutorService, AgentRole, Any, Exception, PlanStep

## Knowledge Gaps
- **44 isolated node(s):** `1. What changed from the original OCDify/Codify draft`, `2. Gaps found in the original plan`, `3. Refined component list`, `4. High-level flow (updated)`, `5. Document set` (+39 more)
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ExecutorService` connect `ExecutorService` to `2. Data model`, `1. Security additions for the multi-agent/multi-provider system`, `4. Agent JSON contracts`, `1. Domain models`?**
  _High betweenness centrality (0.082) - this node is a cross-community bridge._
- **Why does `ProviderError` connect `4. Agent JSON contracts` to `2. Data model`, `1. Security additions for the multi-agent/multi-provider system`, `ExecutorService`, `1. Domain models`?**
  _High betweenness centrality (0.068) - this node is a cross-community bridge._
- **Why does `AgentRegistryService` connect `1. Security additions for the multi-agent/multi-provider system` to `2. Data model`, `ExecutorService`, `4. Agent JSON contracts`, `1. Domain models`?**
  _High betweenness centrality (0.048) - this node is a cross-community bridge._
- **Are the 10 inferred relationships involving `ExecutorService` (e.g. with `FileSystemService` and `PathEscapeError`) actually correct?**
  _`ExecutorService` has 10 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `ApiError` (e.g. with `AgentConfig` and `AgentConfigUpdate`) actually correct?**
  _`ApiError` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 14 inferred relationships involving `GoalService` (e.g. with `AgentOrchestrator` and `AgentOutputInvalid`) actually correct?**
  _`GoalService` has 14 INFERRED edges - model-reasoned connections that need verification._
- **Are the 14 inferred relationships involving `AgentRegistryService` (e.g. with `AgentOrchestrator` and `AgentOutputInvalid`) actually correct?**
  _`AgentRegistryService` has 14 INFERRED edges - model-reasoned connections that need verification._