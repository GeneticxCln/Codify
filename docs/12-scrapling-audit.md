# Codify — The Scrapling audit: what was taken, what was refused, and what the web is allowed to be

Scrapling (`D4Vinci/Scrapling`, BSD-3-Clause) is a Python web-scraping framework: a fast HTML parser, several
HTTP and browser fetchers, a spider framework, and an MCP server. The question put to this repository was how to
"integrate" it. The answer is one tool, `fetch_page`, built on Scrapling's **parser** and on nothing else of it;
the rest is refused, and the reasons are the invariants in `docs/00` §6 and the decisions in `docs/03` and
`docs/07`, not taste. This document follows the shape of `docs/10` and `docs/11`: what was read, what was
measured, what each idea became, and what was not looked at, so the table cannot be mistaken for more than it is.

## 1. What was read, and what was not

* **Read:** the README of the repository; `pyproject.toml` (0.4.15: Python ≥ 3.10, six core dependencies, a
  `fetchers` extra of nine more); the installed package's source where a claim below depends on it (the parser's
  `get_all_text`, the HTTP engine's defaults, `scrapling.fetchers`' imports, the adaptive storage's path).
* **Measured here, on Scrapling 0.4.15, Python 3.10.20 and 3.11.15, 2026-10-03:** the base install (six small
  packages, `py.typed`, passes this repository's mypy flags on 3.10); the `[fetchers]` install (about 370 MB of
  site-packages, Playwright and Patchright among them, no browser downloaded); a plain HTTP fetch of a loopback
  address (it succeeds: the first address is not checked); a redirect to loopback (refused, by its default
  `follow_redirects="safe"`); a 5 MB response (held whole in memory, no cap); its default `Referer` (a spoofed
  `https://www.google.com/`); retries (three attempts with a sleep, on failure); that `import scrapling.fetchers`
  fails without Playwright installed, so even its HTTP client needs the `fetchers` extra; that its logging goes to
  stderr and not stdout (the boot token travels on stdout, `docs/03`).
* **Not read, not run:** the source of the stealth and browser fetchers, the spider framework, the MCP server and
  the RAG extras. No browser was started. Nothing about how they behave is claimed here beyond what their README
  says they are for.
* **Every Scrapling statement below is that README's or a measurement above.** Its benchmark numbers (parser
  speed against other libraries) are its own and untested here; nothing in this repository depends on them.

## 2. The table

| Scrapling | Here | Why |
|---|---|---|
| `scrapling.parser.Selector`: CSS and XPath, text extraction that skips script, style and comments | **Have.** `engine/web_fetch.py` `extract`. | Six small, typed dependencies; works on the 3.10 floor. It is the part that is good at what it does and carries no authority. |
| `Fetcher` / `FetcherSession` (HTTP, TLS impersonation) | **Reject.** The fetch is `httpx`, which the engine already depends on, under rules in `web_fetch.py`. | It does not check the first address (measured), holds a response whole (measured), spoofs `Referer` by default (measured), and needs Playwright to import (measured). Each is a rule this tool has to own; two of them are the attacks the rules answer (§4). |
| `StealthyFetcher`, `solve_cloudflare`, header and TLS impersonation | **Reject.** | `docs/03` §1.5 says what this project does about identity: it announces itself, and measured that pretending changed nothing. The tool sends `Codify-fetch_page/<version> (an AI coding assistant reading a page for its user)`. A site that blocks that is a site the person opens in their own tab. |
| `DynamicFetcher` and the browser engines | **Reject.** | Playwright starts Chromium on its own, outside `spawn_guard` (`docs/07` §1), so "no process outlives the engine" would be false and `tests/test_no_unguarded_spawns.py`'s static scan would not see it. The person's own tab already renders script-built pages, and `read_page` reads it. |
| `Spider`, `CrawlSpider`, checkpoints, concurrent crawls | **Reject.** | `docs/11` §3: nothing starts without a person's turn or Start, there is no daemon, and a crawler is a background worker. A turn may fetch a few pages (`MAX_FETCHES_PER_RUN`); it may not crawl a site. |
| `adaptive=True` / `auto_save` (relocate elements after a site changes) | **Reject, and pinned.** | It opens a SQLite file inside the package directory, a second database (`docs/00` §6.7). It is off by default and the engine never turns it on; `tests/test_web_fetch.py` fails if the name appears in the module and checks the file is untouched by an extraction. |
| `scrapling mcp` (the `ai` extra) | **Reject.** | `docs/11` §3: an MCP surface admits tools the engine does not define (invariants 6, 9) and is a way around the boot token (invariants 2, 3). The call is an engine-defined tool, `fetch_page`, and not a client of someone else's. |
| `page.markdown()`, the RAG extras | **Not taken.** | They pull `markdownify`, and the text the parser gives is already what a model needs. Revisit if tables turn out to matter. |
| The agent skill that teaches a coding agent Scrapling's API | **Not applicable.** | It is for an agent that writes scrapers. This engine does not. |

## 3. What was built

One conductor tool, **`fetch_page(url, selector?, max_chars?)`**, in `engine/web_fetch.py`, offered through
`engine/conductor.py` and run by `engine/conductor_tools.py`; and two engine settings that a person alone writes.

* **`web_fetch`** (0 / 1 / 2, default 2): off, only the sites in `web_fetch_hosts`, any public site.
  **`web_fetch_hosts`**: site names, each covering its subdomains. `PUT /settings/engine` is the only writer
  (`docs/00` §6.2); `tests/test_invariants_at_their_boundary.py` sweeps every writing route with both keys in a
  hostile body and fails if any other route moves them. A list entry that is not a site name (an address, a URL, a
  bare word) is a `422` naming the entries, never a quiet repair. Settings → Web pages is the card (`docs/02`).
* **Off means off, and unreadable means off.** A store that cannot answer, a value outside 1 and 2, or a list
  that parses to nothing is off, whatever a fresh install's default is. The tool is **not on the menu** while it cannot act: a model choosing
  from the tools it is shown should not spend a call learning one only refuses. A list entry in another script is
  stored as its ASCII form, and the list's 200-character limit is applied to the stored form (a list too long once
  written out is refused, never cut).
* **Every attempt is announced before it is made** (`conductor is fetching <address>`), and an address the rules
  then refuse is announced too: the person sees what was tried. One run may make at most `MAX_FETCHES_PER_RUN` (8);
  the ninth is told so and is not announced, because it never reaches the fetch.
* **The request, and what answers each attack:**

  | Rule | Answers |
  |---|---|
  | GET only; no body, header, cookie, credential or proxy the model can name, and none the engine adds (no `Referer`, no cookie jar, `trust_env=False`) | Data carried in a request any way but the address; a proxy that would resolve names and turn the next rule into a check of nothing |
  | `http`/`https`, no user-info, ports 80/443/8080/8443, ≤ 2,000 characters, no whitespace or control characters | `file:`, `ftp:`, `gopher:`, credentials in a URL, the HTTP-to-SMTP/SSH/Redis cross-protocol trick, a long URL as a data channel |
  | The name is resolved here and **every** answer must be a public address (`is_public`: loopback, private, link-local and so the cloud metadata service, CGNAT, multicast, reserved, and IPv6 that wraps one of those, NAT64 included) | `localhost`, `169.254.169.254`, a LAN host, the engine's own port, whatever name points there; a name with one private answer among public ones |
  | The connection is made **to one of the addresses that were checked** (the name is resolved once per request; no other address is ever used); the name travels in `Host` and, for HTTPS, as the name the certificate must match (`sni_hostname`). If connecting to an address fails (`ConnectError`, which includes a failed TLS handshake, or `ConnectTimeout`, so no request was sent), the next checked address is tried, in the resolver's order, each host once however its address is spelled, at most `MAX_ADDRESS_TRIES` (4) per hop (each redirect is a new request that resolves afresh) and inside the same 20 s budget, which ends a run of slow connect timeouts before the cap does (the sentence is then "took longer than 20 seconds"); any other failure ends the fetch | DNS rebinding: a name that answers differently the second time is never asked again. (The fallback is for a dual-stack host whose first answer, often IPv6, cannot be reached from this machine; it connects to no address the check did not pass, because every address it can try was checked.) |
  | Redirects are followed by hand, at most 5, and each hop passes every rule again, the list included | An open redirect on an allowed site walking somewhere the first hop could not go |
  | One 20 s budget over the whole fetch; the body is read to 1 MB and the stream stops there (a compressed body is capped after it is decompressed); the content type must be text | A slow or endless response; a large one held in memory; a binary one |
  | Text is capped, stripped of zero-width and bidirectional controls and of the Unicode tag block, and returned labelled as a website's words | Instructions hidden in text a person cannot see; a page that talks like a command (`format_fetch` says what it is in the same breath as the text) |

* **No process is started.** `web_fetch.py` adds nothing to `GUARDED_SPAWN_SITES` (`docs/07`), and imports
  `scrapling.parser` only: `tests/test_web_fetch.py::TestScraplingIsOnlyTheParser` reads the engine's source and
  fails on any other Scrapling import, on the fetchers, spiders or adaptive names in code, and on a declared
  dependency that asks for an extra.
* **`scrapling>=0.4.15,<0.5`** is a runtime dependency in both `pyproject.toml` and `engine/requirements.txt`
  (the parity test holds them equal). Capped below 0.5 because it is 0.x and its API moves between minors.

## 4. What this does not do, and cannot

* **The address is the payload.** A model that has read `.env` can put it in the query string of a URL on an
  allowed site, and the site receives it. No rule here can tell a destination from a destination used to carry
  data; `docs/03` §1.6 says the same of `navigate_page`, and this tool is the same limit made *invisible* (no tab
  moves) and *engine-originated* (no shell guard in the way). What was done is what can be done: it is off until a
  person turns it on, a list is the default recommendation and the card says why, every attempt is in the
  transcript before it happens, a run is bounded, and the tool's description and the conductor's prompt both say
  not to put the workspace in an address. A model that ignores that, with the setting on, is a way out of the
  machine for what the turn has read.
* **With the list, the destination is a site a person named. With "any public site" it is anywhere.** The card
  says so in those words; the second state is a decision, not a convenience.
* **It is on the menu while an approved plan runs, too.** The tool is offered whenever the setting allows it, and
  an approved step runs with nobody necessarily watching the transcript as it happens. The announcement is still
  made before each request and is there to read afterwards, but it is not a prompt. A person who wants no fetching
  without them present should leave the setting off while a plan runs.
* **A proxy is not used.** `trust_env=False` makes the address check the only check. A person who can reach the
  web only through a proxy cannot use this tool, and that is the cost of the check meaning something.
* **Hidden text is still text.** `display:none` and off-screen text are returned, because the parser reads the
  document and not the rendering. Only invisible *characters* are stripped. The label on the page is what stands
  between that text and the model, and it is the same label `read_page` relies on.
* **Script-built pages return nothing useful.** There is no JavaScript here. The person's own tab renders them,
  and `read_page` reads it.
* **A name lookup has no timeout of its own.** It runs in a worker thread (`getaddrinfo`); the 20 s budget stops
  the *fetch*, and a lookup that never returns leaves its thread until the resolver gives up.
* **The HTTPS pin was verified once by hand and is not in the suite.** On 2026-10-03 a local TLS server with a
  throwaway certificate for `fetch.test` was reached by `https://fetch.test` while the connection went to
  `127.0.0.1`: the request succeeded, and a certificate for a different name failed closed. A test that mints a
  certificate needs a package the 3.10 floor leg does not install, so what the suite pins is the request's
  *construction* (the address in the URL, the name in `Host`, `sni_hostname` set) and a real socket test of the
  `Host` header. A change to httpcore's handling of `sni_hostname` would not be seen by the suite. What it does
  pin on the fallback path, over real sockets, is that a failed handshake moves on to the next checked address and
  that no request reaches either.
* **Rule mutations.** Twenty-five deliberate breaks of these rules were tried (sixteen in `web_fetch.py`, nine in
  the wiring; the first version of one, the redirect re-check, was weaker than intended and was redone) and each
  was checked against the tests. One real gap in the tests was found and closed (the
  redirect limit was asserted by the sentence it produces and not by the number of requests made). One survives
  as equivalent on the interpreters this supports: `is_public`'s own unwrapping of an IPv4-mapped IPv6 address,
  which `ipaddress`'s `is_global` already does on 3.10.20 and 3.11.15; it stays as a guard for an interpreter
  that does not. NAT64 is the one wrapped form that `is_global` gets wrong (`64:ff9b::7f00:1` is "global" on both),
  which is why it is named in `_NEVER` and in the tests. That shows the breaks someone thought of are noticed, not
  that no other gap exists. The count predates the address fallback (`MAX_ADDRESS_TRIES`), which was broken on
  purpose separately: thirteen mutants, of which four survived the first tests (a hard-coded fallback address for a
  host with one answer, a sorted order, a cache across same-host redirects, a bare timeout treated as retryable)
  and each now has a test that kills it.

## 5. If a rejected row is ever reopened

Each rejection above is a statement about an invariant or a decision, so reopening one starts at `docs/00` §6 or
`docs/03` §1.5, not at the feature. Two have a shorter road:

* **Browser rendering** needs a spawn site: a worker process under `guarded_argv`, killed with its group at a hard
  limit, with a dynamic test in the style of `tests/test_sandbox_orphans_e2e.py` proving it dies with the engine.
  `engine/regex_worker.py` is the pattern. Without that it widens the engine.
* **Scrapling's HTTP fetcher** would be worth having only for the TLS and header impersonation `docs/03` §1.5
  declines. That is a decision about what this product claims to be, and it is the person's, not an engineering
  one.

Nothing here should be read as a verdict on Scrapling's quality. It is a different product with a different job,
and this document is only about what this one may take from it.
