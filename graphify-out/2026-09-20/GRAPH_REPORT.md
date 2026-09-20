# Graph Report - Codify  (2026-09-20)

## Corpus Check
- 12 files · ~6,642 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 160 nodes · 350 edges · 11 communities
- Extraction: 85% EXTRACTED · 15% INFERRED · 0% AMBIGUOUS · INFERRED: 52 edges (avg confidence: 0.51)
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

## God Nodes (most connected - your core abstractions)
1. `ApiError` - 28 edges
2. `GoalService` - 22 edges
3. `AgentConfig` - 20 edges
4. `AgentRegistryService` - 20 edges
5. `WorkspaceService` - 18 edges
6. `ProviderError` - 17 edges
7. `Keychain` - 13 edges
8. `ProviderFactory` - 12 edges
9. `Goal` - 11 edges
10. `row_to_dict()` - 10 edges

## Surprising Connections (you probably didn't know these)
- `Keychain` --uses--> `AgentConfig`  [INFERRED]
  engine/providers.py → engine/models.py
- `ProviderFactory` --uses--> `AgentConfig`  [INFERRED]
  engine/providers.py → engine/models.py
- `AgentRegistryService` --uses--> `AgentConfig`  [INFERRED]
  engine/services.py → engine/models.py
- `ApiError` --uses--> `AgentConfig`  [INFERRED]
  engine/services.py → engine/models.py
- `GoalService` --uses--> `AgentConfig`  [INFERRED]
  engine/services.py → engine/models.py

## Import Cycles
- None detected.

## Communities (11 total, 0 thin omitted)

### Community 0 - "Codify — Sub-Agent Orchestration Spec"
Cohesion: 0.20
Nodes (9): 1. The 5 fixed agent roles, 2.1 Provider is agnostic, 2.2 `AgentConfig`, 2.3 Store, 2.4 `DEFAULT_PROMPTS`, 2. Data model, 3. Adapters (by protocol, not slug), 4. Registry / Orchestrator / API (+1 more)

### Community 1 - "Codify — Settings App Spec (Sub-Agent Configuration)"
Cohesion: 0.20
Nodes (9): 1. Principle, 2. Layout files, 3. Screen, 4.1 `AgentConfigCard.tsx`, 4.2 `useAgentConfigs.ts`, 4. Components, 5. Tauri, 6. Read-only elsewhere (+1 more)

### Community 2 - "1. Security additions for the multi-agent/multi-provider system"
Cohesion: 0.16
Nodes (18): BaseModel, api_error(), dumps(), Any, row_to_dict(), AgentConfigUpdate, ErrorBody, Event (+10 more)

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
Nodes (9): ABC, AgentConfig, AnthropicProvider, BaseProvider, OllamaProvider, OpenAICompatProvider, ProviderError, Exception (+1 more)

### Community 7 - "1. Domain models"
Cohesion: 0.17
Nodes (20): auth(), cancel_goal(), create_goal(), create_ws(), get_agent(), get_goal(), get_ws(), goal_events() (+12 more)

### Community 8 - "2. Data model"
Cohesion: 0.24
Nodes (7): AgentRole, lifespan(), Keychain, ProviderFactory, AgentRegistryService, Any, FastAPI

### Community 9 - "db.py"
Cohesion: 0.60
Nodes (5): Connection, connect(), default_db_path(), seed_agents(), Path

## Knowledge Gaps
- **44 isolated node(s):** `1. What changed from the original OCDify/Codify draft`, `2. Gaps found in the original plan`, `3. Refined component list`, `4. High-level flow (updated)`, `5. Document set` (+39 more)
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ProviderError` connect `4. Agent JSON contracts` to `2. Data model`, `1. Security additions for the multi-agent/multi-provider system`, `1. Domain models`?**
  _High betweenness centrality (0.053) - this node is a cross-community bridge._
- **Why does `ApiError` connect `1. Security additions for the multi-agent/multi-provider system` to `2. Data model`, `4. Agent JSON contracts`, `1. Domain models`?**
  _High betweenness centrality (0.051) - this node is a cross-community bridge._
- **Why does `AgentConfig` connect `4. Agent JSON contracts` to `2. Data model`, `1. Security additions for the multi-agent/multi-provider system`, `1. Domain models`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._
- **Are the 11 inferred relationships involving `ApiError` (e.g. with `AgentConfig` and `AgentConfigUpdate`) actually correct?**
  _`ApiError` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `GoalService` (e.g. with `AgentConfig` and `AgentConfigUpdate`) actually correct?**
  _`GoalService` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `AgentConfig` (e.g. with `AnthropicProvider` and `BaseProvider`) actually correct?**
  _`AgentConfig` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `AgentRegistryService` (e.g. with `AgentConfig` and `AgentConfigUpdate`) actually correct?**
  _`AgentRegistryService` has 11 INFERRED edges - model-reasoned connections that need verification._