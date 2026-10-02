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
    SettingsModal.tsx         # the tabs: providers, agent roles, audio, appearance, about
    SettingsPanel.tsx
    AgentConfigCard.tsx
    ProviderRow.tsx           # one provider: key field, model picker, apply button
    ModelPicker.tsx           # searchable + scrollable model dropdown
    ProviderSelect.tsx        # builtins + free-text slug
    ProtocolSelect.tsx        # anthropic | openai_compat | ollama (custom slugs)
    ModelSelect.tsx            # free-text model id, role-scoped
    ApiKeyField.tsx            # hidden when protocol===ollama and no key needed
    BaseUrlField.tsx           # shown for ollama OR any custom slug
    TestConnectionButton.tsx
    PromptOverrideEditor.tsx
    ConductorSettingsCard.tsx  # the conductor's own model + budgets, not a role
    AudioPane.tsx              # microphone, dictation, read-aloud — engine settings only
    UiScalePanel.tsx           # Appearance → UI scale: how big the whole window is
    AboutPane.tsx              # About: version, engine, data folder, shortcuts — read-only
  providerSetup.ts            # search, apply plan, and the wording beside them
  conductorSettings.ts        # what a conductor pair means, and when to refuse it
  modelMenu.ts                # menu placement, shared by both pickers
  modelFreshness.ts           # which models are new since the last visit
  speech.ts                   # what an answer sounds like, where dictation lands, the one player
  uiScale.ts                  # the window's size: one percentage on <html>, stored and followed
  hooks/
    useAgentConfigs.ts        # ONLY hook that reads/writes agent config
```

`AgentConfigCard` MUST call `useAgentConfigs()` (plural) and select `configs.find(c => c.role === role)`. There is no `useAgentConfig` hook.

### 2.1 The provider tab: key and model, in one row

`ProviderRow` is the first place a model is chosen *next to* a credential rather
than on a role card, and the two sit side by side on purpose: the key field and
the model picker used to be on different tabs, so a person was asked to trust a
provider before they could see what it served, and then asked to choose a model
after they had left that provider's row. Side by side is the order the two
decisions are actually made in.

* **The key is written immediately.** `POST /settings/keys` is its own route, there
  is no "apply" for a credential, and a save re-runs discovery so the picker beside
  it fills without a second visit.
* **The model is staged, never applied by picking.** Only
  `PUT /settings/agents/{role}` may mutate agent config (§1), so a provider row
  has no provider-wide default to set. It stages a choice and offers a button that
  calls that one route, once per role, for **the roles already on this provider**.
* **The button names its blast radius.** It says how many roles it will write
  (`Apply to 3 roles`), the summary names the first role and its move
  (`planner old-model → new-model, and 2 more`), and it is disabled with a
  tooltip naming the reason. A bulk edit whose reach is hidden is the kind that
  repoints eight roles on a stray click.
* **With nothing staged it says "Choose a model."** It MUST NOT report
  `3 roles already on it` — there is no plan, and that label claims a state
  nobody is in. (`applyLabel` in `providerSetup.ts` is where all four states
  live, because two of them need a keystroke and are unreachable from markup.)
* **Widening the apply to every role is a different button, in the roles tab.**
  The roles tab's bulk assign already does that across providers.
* **No key for a local provider.** `needs_key` false renders the base URL and
  "no key to paste", not an empty password field. A key typed into a
  `type="text"` field is a key on screen, so a keyed provider's field MUST be
  `type="password"`.
* **`ModelPicker` and `ModelSelect` share one menu geometry** (`modelMenu.ts`) and
  one search (`searchModels`). Two pickers computing a menu's shape is how they
  end up disagreeing about it.
* **A provider's new releases are counted in the row's header** (`3 new`) and
  floated to the top of its picker, badged `new` — the baseline is the last visit,
  per provider, in `modelFreshness.ts`. The marker's rules are docs/06 §6; the
  row owns the count because a badge that only exists after opening a dropdown is
  a badge nobody opens a dropdown for.
* **The tab keeps its own list current, and admits it.** The engine pushes every
  sweep (docs/06 §6), so while that channel is up this tab owns no timer at all —
  one sweep serves every open screen. When the socket is down the tab falls back
  to re-discovering every 60 s and on window focus, because a stale list is worse
  than one extra discovery. Either way the footer says when the providers last
  answered (`checked 40s ago`) or that a check is running, because a list that
  updates itself while you read it and says nothing is indistinguishable from a
  model disappearing.

## 3. Screen

Eight cards, fixed order as in `ROLES`: laya, librarian, design, planner, fixer, verifier, critic,
scribe. No
add/remove. Each card shows what the slot is for and when it runs, read from `GET /settings/roles` —
the ability text lives in the engine (`models.ROLE_JOB` / `ROLE_TIMING`), so the screen cannot describe
a grant the engine no longer makes.

Each card: Provider (builtin or custom slug), Protocol (when custom), Model, API Key (if needs_key), Base URL (ollama/custom), Temperature, Max tokens, prompt override, Save, Test Connection.

The **Providers** tab sits beside this one and is a row per provider from
`GET /settings/providers` — see §2.1. It MUST render the providers it is handed
rather than keeping a list of its own: a copy went stale once, and when it did,
`openrouter` and `groq` were treated as custom endpoints, so the screen asked for
a protocol and a base URL the engine already knew.

### 3.0 The engine runtime card

One card, above the gate card, reading `GET /settings/runtime`: which interpreter the engine is
actually running under, and whether *that interpreter* can import the gate's SDK. It exists
because a capability installed into the wrong environment is otherwise invisible — the SDK in
`.venv` makes `make test` fast while a shell-spawned engine silently pays the fallback model's
latency, with no error anywhere.

It is a separate card from the gate status rather than part of it, because the two answer
different questions: *which engine is gating goals* versus *what this engine is*. A gate can be
off because it was told to be or because the package is not there, and the fixes are deleting a
line of config and running `pip install`.

The wording lives in `ui/src/engineRuntime.ts`, not the component: the suite runs with no DOM, and
`api.ts` reads `localStorage` at import. The one decision worth a test is which interpreter
paths get shown — inside a venv `sys.executable` is `…/.venv/bin/python` while the shell's choice
is `…/.venv/bin/python3`, so the card compares *prefixes* and shows the "checkout's interpreter"
line only when it is genuinely a different environment. A card that reports a difference which
is not there trains people to skim past the one that is.

### 3.1 The Conductor card

One card, above the eight, for the model that decides which sub-agent runs. It
is **not** a role card: the conductor is a loop rather than a ninth `AgentRole`
(docs/01 §5), so its provider and model are `engine_settings` keys written
through `PUT /settings/engine` and nothing else. It sits among the role cards
because that is where a user looks when a turn reaches for the wrong sub-agent.

It carries two model fields, a second pair for the fallback, the two budgets
(`conductor_max_turns`, `conductor_max_moves`) and
`conductor_drives_execution` as a checkbox. Every key is optional on the way in:
an engine that predates the setting answers without it, and a hidden card beats a
card whose save 400s.

The fallback pair follows the same rules as the primary — a half pair is refused,
clearing is a separate button — and the card MUST state which of the two sources
is in force. A conductor still borrowing the scribe's row takes the *scribe's*
fallback and never reads the pair below it; one with a model of its own takes
the pair below and never reads the scribe's. The two are not merged, because a
chain with two fallbacks is a shape nothing else in the product has. Saying so
is the whole reason the fields can be filled in without a user watching them do
nothing.

It MUST NOT offer to save a provider without a model. The engine stores that
half-pair — clearing has to be one call — and then ignores it, so a Save that
accepted it would be a setting the user can change and watch nothing happen.
Clearing is a separate, explicit button, offered only for a *complete* stored
pair the draft has begun to empty. When nothing is configured it states which
model the conductor is borrowing, and when the scribe has no model either it
says there is no conductor on this install rather than naming a model that does
not exist.

### 3.2 The Audio tab

Settings → Audio (`AudioPane.tsx`) is where voice is set up. It has three sections:

* **Microphone:** the input, read from `GET /audio/inputs`.
* **Dictation:** provider, model and an optional language.
* **Read aloud:** provider, model, a voice name, and the "read each answer aloud as it arrives"
  switch, which is off by default. Which answers that switch reads is docs/09 §9.1.

Like the Conductor card, it is **not** agent config. Every field is an `engine_settings` key
(`stt_*`, `tts_*`, `audio_input`, `tts_auto_read`), written through `PUT /settings/engine` and
nothing else. The engine is what records and what calls the speech providers (docs/04 §3.0.2), so
the pane holds no audio state of its own.

* **The status lines are the engine's.** Under each section, `GET /audio/status` says whether that
  feature can run and, if it cannot, why, in the engine's words. The pane reads it again after every
  save. A pane that worked out "ready" for itself could say so about a provider the engine refuses
  (a non-OpenAI protocol, a key that may not go to that address).
* **Trials use what is saved.** "Try dictation" and "Play sample" go through the same routes the
  composer and the answers use, so they are disabled while the draft differs from what is saved.
  Testing an unsaved draft would test something other than what Save would keep.
* **PipeWire missing is a reason, not a list.** Without `pw-dump` the Microphone section shows the
  engine's reason in place of the input select. The engine records with PipeWire's tools, and
  `make doctor` notes their absence without failing (docs/04 §3.0.2).
* **An engine without the keys is told so.** An engine that predates voice answers without them.
  The pane then says the engine needs updating and offers nothing to save, because a save it
  offered would be refused.
* **No model or voice lists of its own.** The model fields browse the same discovered models as a
  role card, with free text allowed. The voice is free text, because the provider owns its voice
  names.
* **A custom provider gets a Server address field; a built-in one does not.** This is how a local
  speech server (speaches, LocalAI, on `127.0.0.1`) is set up without any agent role naming it. The
  engine ignores an address beside a built-in provider, and a field that does nothing is a trap,
  so it is not shown.

### 3.3 UI scale

Settings → Appearance opens with a **UI scale** panel (`UiScalePanel.tsx`): 100, 112.5, 125, 150
or 175%, **125% by default**. It is client-side display state, like the theme: it is stored in
`localStorage` under `codify.uiScale`, never sent to the engine, and not agent config.

* **It is a percentage on `<html>`, and everything written in rem follows.** The type ramp, the radii
  and Tailwind's spacing are rem (`tailwind.config.js`, `DESIGN.md` §3), so text, icons and padding
  grow together. It is not CSS `zoom` and not the webview's own zoom, because the browser pane's
  native webview is placed from `getBoundingClientRect()` in logical pixels (docs/09 §7.3), and
  either of those would make that number disagree with the pixels the shell places it in.
* **It is applied before the first render** (`startUiScale` in `main.tsx`), so the window never paints
  at 100% and then jumps. `startUiScale` also follows another window's change (the `storage` event
  fires only in other windows).
* **Only the steps are accepted.** A stored value that is not exactly one of them is refused and the
  default is used, so a hand-edited `3` or `900` cannot produce a window nobody can read or reach
  this panel in to repair. Steps, not a slider, because layout is only checked at these sizes.
* **Applied at once, remembered, and the same store as the keyboard.** The preview line in the panel
  is the window itself at that size. Ctrl +, Ctrl - and Ctrl 0 (docs/09 §8) write the same store, so
  the panel and the keys cannot disagree.
* **Layout is checked at these sizes, in the real build.** Rendered in Chromium at 1280×800 and at the
  900×600 minimum window, at 100, 125 and 175%, with the Stats and History drawers open and the left
  panel shown and hidden, across the Settings tabs. That found three things a unit test cannot see, all
  fixed: icon buttons drew their icon at 8px instead of 20px (`IconButton`'s `p-0` was outranked by
  `Button`'s `px-*`), the Settings tab bar clipped its last tabs and scrolled the whole modal sideways
  at 175% (it now wraps), and the left panel took nearly half of a 900px window at 175% (it is capped
  at 35% of the row). At 175% in a 600px-tall window the Settings body is short and scrolls: that is
  the extreme corner, and 125% is the default.
* **A drawer and the left panel do not both fit at 125%, so the panel gives way.** Rendering the real
  build with Stats open found the transcript squeezed to about 400px, a step's status pill broken a
  letter to a line while its title kept its width, the "TOKEN USAGE" label broken while the totals kept
  theirs, and the composer's placeholder cut mid-word. The panel now yields to a drawer when the row
  cannot hold both (`docs/09` §8.1); the step header lets its title shrink and its status not, the usage
  header wraps its totals under the label, and the placeholder is short with the long sentence in its
  `title`. `ui/tests/layoutWrapping.test.ts` pins which side may shrink.
* **Notifications are a third drawer, in the same slot.** A **Notifications** button (a bell, with the
  unread count as a badge that is absent at zero) sits between Stats and History in the header. The three
  drawers are one state, so at most one is open and each button is armed only for its own. It is a flex
  sibling of the centre column and not a popover, for the reason the others are (`docs/09` §8.1: a native
  browser view paints above any overlay), and it takes the same width as History, so the left panel's
  yield rule (`ui/src/drawers.ts`) treats it the same. What it collects, and what it refuses to, is
  `docs/09` §10.18. The button's title does not begin with Browser, Terminal, Keys or Settings, which is
  how the panel's own buttons are found.
* **Providers are read by name, not by slug.** The Provider Keys row printed the slug through CSS
  `capitalize` ("Openai", "Nvidia") and the protocol as the raw tag `openai_compat`, and the provider
  select printed every slug in capitals. `ui/src/providerLabels.ts` supplies the words (OpenAI,
  OpenRouter, DeepSeek, NVIDIA, Groq, Google, Anthropic, Ollama; any other slug title-cased) and the
  protocol's name ("OpenAI-compatible"; the choice list keeps its endpoint). The slug stays the
  identifier everywhere: it is the name's tooltip and what every request carries. The table is a
  spelling guide for eight names and is not a catalogue (`docs/06` §1): which providers exist, and every
  model, still come from the engine and from live discovery.
* **What does not follow a root font size is handled where it lives.** The xterm terminal draws its own
  canvas, so `TerminalPane` sets its font size from the scale and re-fits when it changes, and the
  prompt box's 180px height cap is multiplied by the scale. A new text size is `text-xs` and friends,
  **never `text-[Npx]`**, which would stay small while the window grew around it.

### 3.4 The About tab

Settings → About (`AboutPane.tsx`, last tab) says what this app is and what it is running on. It is
**read-only**, so it can never be a second writer of agent config (§1), and it prints only what the app
can truthfully know:

* **Version.** The desktop shell's own name, version and Tauri version, read through Tauri's app API
  (`getAppFacts` in `api.ts`; `core:app` is already granted, so no capability of its own). Under the
  shell, a version that comes back blank or not a string is shown as "unavailable", never as an empty
  fact. In the standalone browser preview there is no shell, and the pane says so instead of a version.
* **The engine.** Health (`running`, `not answering`, or `running, but this window's token is stale`),
  the port the window is connected to, and the existing engine runtime card (§3.0: the interpreter and
  what it can import). The engine has no version of its own, so none is invented: it ships in the same
  checkout as the app, and the interpreter line is what tells two installs apart. **The boot token is
  never shown.**
* **Where things live.** `~/.codify` (or `$CODIFY_HOME`), as text: no route exposes the data folder, and
  adding one only to print a path was not worth a new engine contract.
* **Keyboard shortcuts**, from `SHORTCUT_HELP` in `shortcuts.ts`. Each line carries a real key event
  that `shortcuts.test.ts` resolves through `resolveShortcut`, and a second assertion pins the set of
  actions, so a binding that changes, or a shortcut added without a line, fails a test instead of
  leaving this tab describing keys that no longer do that.
* **Project facts** (repository, licence, where the specification is) as plain text rather than links:
  the page is not a browser, and an external link opened from here would be a navigation the app has no
  reason to offer.

The palette (Ctrl+K) lists it as `About Codify`, and Appearance as `Appearance, themes & UI scale` so
that searching "scale" finds the UI scale.

General / Commands tabs MAY exist; they MUST NOT call `codify_update_agent_config`.

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
      num_ctx: active.num_ctx ?? null,
      keep_alive: active.keep_alive?.trim() || null,
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
      {draft.protocol === "ollama" && (
        <NumberField label="Context window" value={active.num_ctx} placeholder="4096"
                     onChange={n => setDraft({ ...draft, num_ctx: n })} />
      )}
      {draft.protocol === "ollama" && (
        <TextField label="Keep alive" value={active.keep_alive} placeholder="5m"
                   onChange={k => setDraft({ ...draft, keep_alive: k })} />
      )}
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

**The card also states how the role has been doing**, from the engine's measured stage results (`04` §4.7): `did its job <rate> over N runs`, with the outcome histogram as the tooltip and `runs`/`success_rate` optional in the type so an engine that does not measure stages still renders. Three rules the view inherits and must not re-invent: a role that never ran shows no rate at all rather than 0%, the rate is about the *role* (a verifier that reported `fail` worked), and the histogram travels with the rate so the percentage can be checked rather than trusted. This is on the card because that is where someone decides whether to re-prompt a role.

`BaseUrlField` client-side: hostname `127.0.0.1` or `localhost`, scheme `http`. Engine re-validates.

## 5. Tauri

Commands: `codify_list_agent_configs`, `codify_update_agent_config`, `codify_test_agent_connection` as previously specified.

`AgentConfigPatch` includes `api_key` and `base_url`. `api_key` never written to Tauri store or logs.

Every engine call the UI makes (`ui/src/api.ts`) attaches the boot Bearer token (`04` §6).

## 6. Read-only elsewhere

```tsx
<Badge>{payload.role} · {payload.provider}/{payload.model}</Badge>
```

No edit. Optional deep-link to Settings.
