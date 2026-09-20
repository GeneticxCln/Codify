# Codify — Sub-Agent Orchestration Spec

Normative for agent slots, providers, registry, and `/settings/agents`. Persistence: `04`. Security: `03`.

## 1. The 5 fixed agent roles

Exactly 5 slots. Users cannot add or remove **roles**. Provider/model/key/`base_url` per slot are Settings-only.

| Slot (`role` id) | Job | Output schema |
|---|---|---|
| `planner` | Ordered `PlanStep[]` | `04` §4.1 |
| `coder` | File edits | `04` §4.2 |
| `tester` | Argv + verdict | `04` §4.3 |
| `reviewer` | approve / request-changes | `04` §4.4 |
| `summarizer` | summary + commit | `04` §4.5 |

## 2. Data model

### 2.1 Provider is agnostic

`provider` is a **slug string**, not a closed enum. Engine ships four **built-in** slugs. Settings MAY save any other slug if `protocol` + `base_url` are set.

```python
AgentRole = Literal["planner", "coder", "tester", "reviewer", "summarizer"]
ProviderProtocol = Literal["anthropic", "openai_compat", "ollama"]
SYSTEM_PROMPT_OVERRIDE_MAX = 32768

BUILTIN_PROVIDERS: dict[str, dict] = {
    "anthropic": {"protocol": "anthropic", "base_url": "https://api.anthropic.com", "needs_key": True, "local_only": False},
    "openai":    {"protocol": "openai_compat", "base_url": "https://api.openai.com/v1", "needs_key": True, "local_only": False},
    "deepseek":  {"protocol": "openai_compat", "base_url": "https://api.deepseek.com", "needs_key": True, "local_only": False},
    "ollama":    {"protocol": "ollama", "base_url": "http://127.0.0.1:11434", "needs_key": False, "local_only": True},
}
```

| Slug | Protocol | Default `base_url` | Key |
|---|---|---|---|
| `anthropic` | Messages API | `https://api.anthropic.com` | yes |
| `openai` | Chat Completions | `https://api.openai.com/v1` | yes |
| `deepseek` | Chat Completions (OpenAI-compatible) | `https://api.deepseek.com` | yes |
| `ollama` | Ollama `/api/generate` | `http://127.0.0.1:11434` | no |

Custom slug (e.g. `openrouter`, `groq`): `protocol` MUST be `openai_compat` or `anthropic` or `ollama`. `base_url` REQUIRED. `ollama` / `local_only` → `validate_local_base_url`. Remote custom URLs are allowed (single-user); still no query-token, still Bearer.

`GET /settings/providers` → built-in catalog + any extra slugs already stored on the five agent rows.

### 2.2 `AgentConfig`

```python
class AgentConfig(BaseModel):
    role: AgentRole
    display_name: str = Field(..., min_length=1, max_length=80)
    provider: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    protocol: ProviderProtocol
    model_name: str = Field(..., min_length=1, max_length=128)
    api_key_ref: Optional[str] = None
    base_url: Optional[str] = None
    system_prompt_override: Optional[str] = Field(None, max_length=SYSTEM_PROMPT_OVERRIDE_MAX)
    temperature: float = Field(0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(4096, gt=0, le=200000)
    updated_at: float

class AgentConfigUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    display_name: Optional[str] = None
    provider: Optional[str] = Field(None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    protocol: Optional[ProviderProtocol] = None
    model_name: Optional[str] = None
    api_key: Optional[str] = Field(None, min_length=1, max_length=4096)
    base_url: Optional[str] = None
    system_prompt_override: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
```

`set_config` merge rules:

1. Prompt override: strip; empty → `NULL`; `>32768` → `400` `prompt_too_long`.
2. If `provider` is built-in and `protocol` omitted: fill from catalog. If `base_url` omitted: fill catalog default.
3. If `provider` is **not** built-in: `protocol` and `base_url` REQUIRED after merge.
4. `protocol==ollama` OR catalog `local_only`: `validate_local_base_url`.
5. `api_key` present → keyring `codify` / `codify/agents/{role}`; store `api_key_ref` only.
6. Built-in `needs_key=False`: key optional.
7. `updated_at = time.time()`.

### 2.3 Store

`~/.codify/codify.db` table `agent_configs`. **No `agents.db`.** Column `protocol` TEXT NOT NULL.

```python
DEFAULT_AGENTS = [
    AgentConfig(role="planner", display_name="Planner Agent", provider="anthropic", protocol="anthropic", model_name="claude-opus-5", temperature=0.3, max_tokens=4096, updated_at=0),
    AgentConfig(role="coder", display_name="Coder Agent", provider="anthropic", protocol="anthropic", model_name="claude-sonnet-4-6", temperature=0.1, max_tokens=8192, updated_at=0),
    AgentConfig(role="tester", display_name="Tester Agent", provider="openai", protocol="openai_compat", model_name="gpt-4.1-mini", temperature=0.0, max_tokens=2048, updated_at=0),
    AgentConfig(role="reviewer", display_name="Reviewer Agent", provider="anthropic", protocol="anthropic", model_name="claude-sonnet-4-6", temperature=0.2, max_tokens=4096, updated_at=0),
    AgentConfig(role="summarizer", display_name="Summarizer Agent", provider="deepseek", protocol="openai_compat", model_name="deepseek-chat", temperature=0.4, max_tokens=1024, updated_at=0),
]
```

SQL: `04` §2 (includes `protocol`).

### 2.4 `DEFAULT_PROMPTS`

Unchanged role JSON contracts (`04` §4). Invalid JSON → `agent_output_invalid`, step `FAILED`.

## 3. Adapters (by protocol, not slug)

`ProviderFactory.build(config)` switches on `config.protocol`:

- `anthropic` → Anthropic Messages (`anthropic` SDK or HTTP).
- `openai_compat` → `AsyncOpenAI(api_key=..., base_url=config.base_url)` — covers OpenAI, DeepSeek, Groq, OpenRouter, user slugs.
- `ollama` → POST `{base_url}/api/generate` after SSRF check.

Adding a **harness** later = one catalog row, not a new class. Adding a new **wire format** = one protocol class.

`test_connection`: `max_tokens=8`, prompt `ping`, 15s. Never echo keys.

## 4. Registry / Orchestrator / API

`AgentRegistryService` only mutator. `list_configs` = 5 rows, fixed role order.

`GET /settings/providers` → `{builtins: [...], custom: [slugs on rows not in builtins]}`.

`PUT /settings/agents/{role}` patch as `AgentConfigUpdate`. Extra keys `422`. No raw key in responses.

`POST /goals*` : `extra=forbid`, no agent fields.

Reviewer rejection: no auto-Coder. Retry `POST /goals/{id}/steps/{step_id}/retry`.
