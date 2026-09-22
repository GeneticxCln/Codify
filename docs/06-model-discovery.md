# Codify — Model Discovery

Normative for `GET /models`, the model pickers, and `engine/model_catalog.py`. Roles/providers: `01`.
Provider wire formats: `04`.

## 1. There is no model catalog in the build

Codify ships **no model list**. Every model the UI offers is fetched from the provider that serves it,
with the credential the user stored, at the moment it is asked for. A list compiled into the binary is
wrong within weeks: it shows models that have been retired and hides every model released after the
release — which is precisely the set a user is most likely to want.

Two design consequences follow:

* **Adding a key is enough.** A provider does not have to be assigned to an agent role before its
  models appear. `_targets()` unions (a) providers referenced by any role config, (b) every built-in
  whose key resolves from the OS keyring or the environment, and (c) providers that need no key
  (local servers).
* **An empty catalog is a valid answer.** If nothing is configured and nothing is reachable, `/models`
  returns `models: []` plus a reason per provider. It never substitutes a plausible-looking default.

## 2. Per-provider discovery

| Protocol | Endpoint | Credential | Notes |
|---|---|---|---|
| `ollama` | `GET {base}/api/tags` | none | reports size/parameter count; **no capability metadata** |
| `openai_compat` | `GET {base}/models` | `Authorization: Bearer <key>` | OpenAI, DeepSeek, Groq, OpenRouter, any compatible endpoint |
| `anthropic` | `GET {base}/v1/models?limit=…` | `x-api-key` + `anthropic-version` | uses `display_name`; the default page size is small, so `limit` is set explicitly |
| `google` | `GET {base}/models?key=…` | query key | follows `nextPageToken` (≤5 pages) and **strips the `models/` resource prefix** |

Rules that hold for every provider:

1. **Report, don't invent.** Fields are copied from the provider's response. Where the provider says
   nothing (Ollama has no capability flag; `openai_compat` has no display name), the field stays
   `null`/derived-from-id rather than being guessed. `supports_chat` is only set when the provider
   actually stated it.
2. **Never filter by name.** Heuristics like "skip ids containing `embed`" are a hardcoded list wearing
   a disguise — they hide models the user may legitimately want. Capability flags reported by the
   provider (`supportedGenerationMethods`) *are* carried, and the UI marks such entries as
   "not a chat model" instead of hiding them.
3. **Normalise at the boundary.** Google's `models/gemini-…` resource name is stripped to
   `gemini-…`, because passing it through produces `…/models/models/gemini-…:generateContent`.
4. **Isolate failures.** One provider's 401 or unreachable host must not empty the picker. Each
   provider returns its own `ok`/`count`/`error`; 401/403 is reported as "check the API key".
5. **Bound the work.** 8 s per provider, 500 models per provider, all providers queried concurrently.

## 3. Ordering

Models are ordered **newest first** when the provider dates them (`created` / `created_at` /
`modified_at`), then alphabetically. This is what puts a model released this morning at the top of
the picker instead of wherever the API's alphabetical response happened to place it.

## 3.1 Menu ordering and badges (`ui/src/modelSignals.ts`)

Discovery order is what a provider chose; it is not what you are looking for. Both pickers (the
command-bar menu and the per-role **Model Identifier** field) lead with the ids the app already has
reason to care about, using signals the engine already records — no new source of truth, and nothing
hardcoded:

| Signal | Read from | Effect |
|---|---|---|
| in use by roles | `GET /settings/agents` | "In use by roles" leads the menu; within it, the model **that actually answered most recently** leads, then chat-capable ids, then the model shared by the most roles |
| ran recently | `GET /models/recent` | a "late" section for models that answered but that no role currently points at; newest first, each badged `last run` |
| chat support | the provider's own `supports_chat` | chat ids before ones the provider reports as non-chat; the rest stay listed and badged `not a chat model` |

`GET /models/recent` exists because the obvious shortcut — the newest goal's `provider`/`model` — is
what the command bar *asked for*, not what ran. Since per-role configs became authoritative
(`01` §2.2) a goal can name one model and be executed by four others, so that shortcut would badge a
model the engine never called. It reads `agent_assigned` events instead, ordered by the event's own
timestamp, deduplicated to each model's newest run, and it skips the empty model id a role with
nothing chosen announces.

Both signals refresh without a reload: role configs when the settings screen closes, recency when a
goal reports a terminal status (`App.tsx`). Neither is persisted client-side — a second copy in local
storage would be a thinner version of a fact the engine already stores.

Ordering never *filters*: every discovered model is present, and non-chat ones are pushed down rather
than removed. The current selection is hoisted to the top of its own group in every case.

## 4. Caching and refresh

```
GET /models            → cached for 60 s (fast for repeated menu renders)
GET /models?refresh=true → always re-queries every provider
```

The UI passes `refresh=true`:

* when the app opens,
* when the engine's connection info changes (a fresh token may mean a different engine),
* when the settings dialog opens,
* when the user presses **Refresh** in the model picker,
* and after saving a key or an agent config.

`POST /settings/keys` and `PUT /settings/agents/{role}` call `ModelCatalogService.invalidate()` so a
newly saved key cannot be hidden behind a cached pre-key answer. A model the provider starts serving
is therefore visible in the already-open app, with no restart and no reinstall.

## 5. Response shape

```json
{
  "models": [
    {"id": "just-released-model", "name": "just-released-model", "provider": "deepseek",
     "protocol": "openai_compat", "description": "deepseek · owned by fake",
     "created": 1900000000.0, "supports_chat": null}
  ],
  "providers": [
    {"provider": "deepseek", "protocol": "openai_compat", "ok": true, "count": 2, "error": null},
    {"provider": "openai", "protocol": "openai_compat", "ok": false, "count": 0,
     "error": "no API key configured for this provider"}
  ],
  "fetched_at": 1774000000.0,
  "cached": false
}
```

An id is not unique across providers (the same model can be served locally and by an API), so the UI
keys selections on the `(provider, id)` pair.

## 6. UI contract

* The command-bar menu leads with "In use by roles" and "Ran recently", then groups what is left by
  provider with each provider's count; providers that answered nothing are listed **with their
  reason**, because a silently absent provider reads as a Codify bug rather than a configuration gap.
  Ordering and badges follow §3.1.
* The per-role **Model Identifier** field stays free text (a provider may serve a model its own list
  does not mention yet — a brand-new release, a private fine-tune, an alias) but autocompletes from
  that provider's discovered models. It only offers ids for *its own* provider, since another
  provider's ids cannot be served by this endpoint.
* Sending with no model selected is refused up front with a pointer to Settings, rather than failing
  later inside a provider call.

## 7. Stale role models are warned about, not just stored

A stored role config survives the model it names. Providers retire and rename ids on their own
schedule, so a role can keep pointing at `retired-model:latest` long after it stopped being callable —
and nothing about the saved row says so. The failure then lands mid-goal, as a 404 from whichever stage
happened to run first.

Settings therefore flags it: each affected card shows an amber warning and an amber outline, and the
panel carries a summary above the list (`N of 6 roles point at a model their provider no longer
reports`) so the affected card does not have to be scrolled to. `findStaleModel()` in
`ui/src/staleModel.ts` is the single rule behind both, so the two can never disagree.

A role's **fallback target rots the same way, and more quietly**: it is only used once something else has
already gone wrong, so a fallback pointing at a retired model is discovered at the worst possible
moment. The card warns about it too, from the same rules (`findStaleFallback`), and the repair action in
Settings treats a role whose *fallback* works as a role that works — repointing its primary would
overwrite the very choice keeping the goal alive.

The rule is deliberately narrow, because a wrong warning here is worse than none — it would tell a user
their model was retired when the truth is that a key is missing.

1. **A failed discovery proves nothing.** The warning requires `ok: true` for that role's provider.
   If discovery failed (no key, unreachable endpoint), the model's status is *unknown*, so the card
   says nothing rather than guessing — the adjacent Model Identifier note already explains *why*
   nothing was discovered.
2. **Only the saved config.** The verdict reads `model_name` as persisted, not the draft, so typing a
   new id does not flash a warning on every keystroke that is not yet a real model.
3. **A successful discovery with zero models is still a warning**, worded differently
   ("reported no models at all … could not be verified"), because a provider answering with an empty
   list while a role depends on it is a real configuration problem.

Two more consequences worth stating: the warning is **advisory, never blocking** — a provider may
legitimately serve an id its own list omits (new release, private fine-tune, alias), which is exactly
why the field stays free text — and the verdict disappears as soon as the catalog or the config
changes, since both the cards and the summary read one shared config store (one fetch per Settings
open, not one per card).

## 8. Roles are seeded with no model at all

The same rule that forbids a model list in the API applies to the defaults. Every role is seeded on the
**local, keyless** provider (`ollama`) with `model_name = ""` — nothing else. A seeded id would be a
hardcoded list in the one place it is least visible, and it would also choose a provider on the user's
behalf: the previous seed pointed five roles at three remote providers whose keys did not exist yet, so
a fresh install failed on the first prompt with no explanation of what was missing.

An unconfigured role is refused **before** any provider call. The goal fails with
`agent_not_configured`:

```
Error [agent_not_configured]: no model is configured for the planner role.
Open Settings → Agent Roles and pick one of the models ollama currently serves.
```

That message is the whole reason the guard exists: without it the same situation surfaced as a protocol
error from an endpoint asked to run an empty model id, reported as `agent_output_invalid`, which sends
the user looking for a malformed model reply that was never produced.

**Getting out of that state is one action.** Settings → Agent Roles carries a *Use one model for every
role* control, whose options are the discovered catalog — so a fresh install is configured from models
the providers actually serve, in one click, with no id typed by hand. The `laya` gate degrades cleanly
while unconfigured (`skipped: … no model configured for the laya role`) and never blocks a goal.

Related: `04-engine-data-and-runtime.md` holds the provider-switch rules that keep `protocol` and
`base_url` consistent when a role changes provider.
