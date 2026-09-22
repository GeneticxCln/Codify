# Codify — Settings App Spec (Sub-Agent Configuration)

## 1. Principle

Agent configuration is edited only at **Settings → Agents**. Enforced at three layers:

1. **API** — only `settings_api.py` exposes `PUT /settings/agents/{role}` (`01` §7). Goal payloads `extra=forbid`.
2. **Tauri** — only `SettingsPanel.tsx` / `AgentConfigCard.tsx` call `codify_update_agent_config`.
3. **Display** — `StepStatus` / `GoalDetail` render `agent_assigned` as text. Click MAY deep-link to Settings → Agents → `{role}`.

## 2. Layout files

```
ui/src/
  components/
    SettingsPanel.tsx
    AgentConfigCard.tsx
    ProviderSelect.tsx         # builtins + free-text slug
    ProtocolSelect.tsx         # anthropic | openai_compat | ollama (custom slugs)
    ModelSelect.tsx            # free-text model id
    ApiKeyField.tsx            # hidden when protocol===ollama and no key needed
    BaseUrlField.tsx           # shown for ollama OR any custom slug
    TestConnectionButton.tsx
    PromptOverrideEditor.tsx
  hooks/
    useAgentConfigs.ts        # ONLY hook that reads/writes agent config
```

`AgentConfigCard` MUST call `useAgentConfigs()` (plural) and select `configs.find(c => c.role === role)`. There is no `useAgentConfig` hook.

## 3. Screen

Seven cards, fixed order as in `ROLES`: laya, librarian, planner, fixer, verifier, critic, scribe. No
add/remove. Each card shows what the slot is for and when it runs, read from `GET /settings/roles` —
the ability text lives in the engine (`models.ROLE_JOB` / `ROLE_TIMING`), so the screen cannot describe
a grant the engine no longer makes.

Each card: Provider (builtin or custom slug), Protocol (when custom), Model, API Key (if needs_key), Base URL (ollama/custom), Temperature, Max tokens, prompt override, Save, Test Connection.

General / Commands / About tabs MAY exist; they MUST NOT call `codify_update_agent_config`.

## 4. Components

### 4.1 `AgentConfigCard.tsx`

```tsx
export function AgentConfigCard({ role }: { role: AgentRole }) {
  const { configs, update, testConnection } = useAgentConfigs();
  const config = configs.find(c => c.role === role);
  const [draft, setDraft] = useState<AgentConfig | undefined>(config);
  // sync draft when config arrives / updates from server

  const onSave = async () => {
    if (!draft) return;
    await update(role, {
      provider: draft.provider,
      model_name: draft.model_name,
      api_key: draft.pendingApiKey,
      base_url: draft.base_url,
      protocol: draft.protocol,
      temperature: draft.temperature,
      max_tokens: draft.max_tokens,
      system_prompt_override: draft.systemPromptOverride,
    });
    setDraft(d => d ? { ...d, pendingApiKey: undefined } : d);
  };

  return (
    <Card title={config?.display_name ?? role}>
      <ProviderSelect value={draft.provider} onChange={p => setDraft({ ...draft, provider: p })} />
      <ModelSelect provider={draft.provider} value={draft.model_name}
                   onChange={m => setDraft({ ...draft, model_name: m })} />
      {draft.provider !== "local" && (
        <ApiKeyField onChange={k => setDraft({ ...draft, pendingApiKey: k })} />
      )}
      {(draft.provider === "ollama" || !BUILTIN.has(draft.provider)) && (
        <BaseUrlField value={draft.base_url ?? ""}
                      localOnly={draft.protocol === "ollama"}
                      onChange={u => setDraft({ ...draft, base_url: u })} />
      )}
      <NumberField label="Temperature" value={draft.temperature} min={0} max={2} step={0.05}
                   onChange={t => setDraft({ ...draft, temperature: t })} />
      <NumberField label="Max tokens" value={draft.max_tokens}
                   onChange={m => setDraft({ ...draft, max_tokens: m })} />
      <PromptOverrideEditor value={draft.systemPromptOverride} maxLength={32768}
                             onChange={p => setDraft({ ...draft, systemPromptOverride: p })} />
      <Button onClick={onSave}>Save</Button>
      <TestConnectionButton onClick={() => testConnection(role)} />
    </Card>
  );
}
```

### 4.2 `useAgentConfigs.ts`

```ts
export function useAgentConfigs() {
  const [configs, setConfigs] = useState<AgentConfig[]>([]);
  useEffect(() => { invoke<AgentConfig[]>("codify_list_agent_configs").then(setConfigs); }, []);
  const update = async (role: AgentRole, patch: AgentConfigPatch) => {
    const updated = await invoke<AgentConfig>("codify_update_agent_config", { role, patch });
    setConfigs(cs => cs.map(c => (c.role === role ? updated : c)));
  };
  const testConnection = (role: AgentRole) =>
    invoke<{ ok: boolean; message: string }>("codify_test_agent_connection", { role });
  return { configs, update, testConnection };
}
```

Imported only by Settings components. Goal UI never imports this hook.

`PromptOverrideEditor`: free-text, `maxLength={32768}`. Blank/whitespace on Save → `null` inherit. No `max_calls_per_goal` field.

`BaseUrlField` client-side: hostname `127.0.0.1` or `localhost`, scheme `http`. Engine re-validates.

## 5. Tauri

Commands: `codify_list_agent_configs`, `codify_update_agent_config`, `codify_test_agent_connection` as previously specified.

`AgentConfigPatch` includes `api_key` and `base_url`. `api_key` never written to Tauri store or logs.

`BackendClient` attaches boot Bearer (`04` §6) on every call.

## 6. Read-only elsewhere

```tsx
<Badge>{payload.role} · {payload.provider}/{payload.model}</Badge>
```

No edit. Optional deep-link to Settings.
