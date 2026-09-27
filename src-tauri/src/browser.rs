//! The embedded browser, and the isolation it starts from.
//!
//! This is the first surface in Codify that renders **untrusted content**:
//! every other pixel in the app is engine-owned or shell-owned. The isolation
//! has four layers; this module owns the three the shell adds, and the tests at
//! the bottom assert them rather than trusting a comment.
//!
//! 1. **An empty capability set.** Browser webviews get the label
//!    `browser-<tab>` ([`webview_label`]) and no capability that grants
//!    anything matches that label: `src-tauri/capabilities/browser.json`
//!    covers `browser-*` with an empty `permissions` list, and no other
//!    capability's window/webview globs reach it. Tauri v2 resolves an
//!    `invoke` against the calling webview's label *and* its origin; with no
//!    matching capability no command — plugin or core, not even an event
//!    listener — resolves. [`the_browser_capability_set_is_empty`] parses the
//!    committed capability files (and `tauri.conf.json`'s inline capability
//!    slot) and fails if any permission ever appears for a browser label,
//!    including through a widened `*` elsewhere.
//!
//! 2. **A loopback URL guard on every navigation.** [`navigation_allowed`]
//!    refuses anything that is not http(s) and refuses loopback or
//!    unspecified hosts: `localhost` and `*.localhost` (RFC 6761), all of
//!    127/8, `::1`, `0.0.0.0`, `::`, their IPv4-mapped IPv6 forms, and the
//!    canonicalised integer/hex/octal spellings the WHATWG URL parser turns
//!    into them — `http://2130706433/` *is* 127.0.0.1. It is wired into
//!    `WebviewWindowBuilder::on_navigation`, so it is asked about every
//!    navigation the page attempts, including redirects — not just the first
//!    URL — and [`open`] checks it once more before the window exists at all.
//!
//! 3. **A close signal, because a webview can die without permission.** A
//!    browser tab's webview is a real OS window: the user can close it with the
//!    window's own close button, and nothing about that passes through the tab
//!    bar. [`open`] registers [`reports_closed`] on the window and emits
//!    [`CLOSED_EVENT`] carrying the tab id, which the main window turns into a
//!    tab close — otherwise the strip keeps a tab whose window is gone.
//!    Emitted on `Destroyed`, not on `CloseRequested`, which fires when a close
//!    is *asked for* and can still be prevented. The reverse order (tab bar
//!    closes the tab, shell destroys the window) emits the same event and the
//!    UI's handler is a no-op for it, so both directions are safe to repeat.
//!
//! 4. **An app ACL manifest, so layer 1 has something to deny with.** Layers 1
//!    and 2 only bite because Tauri *has* an ACL to enforce. Until this
//!    change the app defined none, and Tauri skips the check entirely for an
//!    application command invoked from a local origin — the main window's own
//!    origin, and any origin a page reached it at. A browser webview was kept
//!    out by its origin alone. `src-tauri/permissions/shell.json` is now that
//!    manifest: one `allow-` permission per `codify_*` command, collected into
//!    a `shell` set that `capabilities/default.json` — and only that file, whose
//!    `windows` is `["main"]` — references. Every `codify_*` invoke is now
//!    resolved against the calling label whatever the origin is, and a
//!    `browser-*` label resolves to nothing.
//!    [`the_app_acl_manifest_closes_the_local_origin_bypass`],
//!    [`the_grant_reaches_main_and_no_browser_label`] and
//!    [`the_grant_is_exactly_the_commands_the_handler_defines`] hold the first
//!    two claims; the third is the one that keeps layer 4 and `lib.rs` in
//!    step, because a command defined without a grant is stripped from the
//!    handler at compile time and fails at runtime as "command not found".
//!
//! ## Why loopback is still refused, now that it is not the only line
//!
//! The guarantee "untrusted content can never invoke a `codify_*` command" is
//! a **label** guarantee: the grant lives in `capabilities/default.json` with
//! `windows: ["main"]`, and `browser-<tab>` matches nothing. That holds for a
//! local origin, a remote one, and an origin that has not been classified
//! yet, because Tauri resolves the ACL by label and origin together and
//! either half failing is enough.
//!
//! The origin half used to be the load-bearing one. Tauri treats an origin
//! relative to the `devUrl` or the app's own assets (`tauri://localhost`,
//! `http://tauri.localhost` on Windows) as **local**, and with no app ACL
//! manifest defined it allows an application command from a local origin
//! with no capability check at all. Layer 4 is what removes that pass, which
//! makes the loopback guard defence in depth rather than the whole boundary.
//! It is still worth having, for the reason it was worth having: the two
//! fail differently. A page that reaches a local origin gains nothing (layer
//! 4) but has still seated untrusted content where the app's own scripts
//! run — `http://localhost:5173` is the dev UI — and a guard that keeps it
//! out has no dependency on the ACL staying correct.
//! See docs/03 §1.5, docs/09 §7.2.
//!
//! ## What this module does not claim
//!
//! - The guard is a *navigation* policy, not a network filter. Subresource
//!   requests (images, iframes, `fetch`) to loopback are not intercepted
//!   here. They do not need to be for safety — the engine requires the bearer
//!   token on every route, and the token reaches a webview only through a
//!   `codify_get_engine_info` invoke its label cannot make — but a claim this
//!   honest must not imply packet-level blocking.
//! - A hostname that *resolves* to loopback through DNS (`127.0.0.1.nip.io`)
//!   passes a lexical guard. That is beyond any synchronous host check; the
//!   capability set is the boundary that does not care what the host resolves
//!   to, and the app ACL manifest is what makes that set mean anything.
//! - The guard is top-level navigation policy. This module starts no process
//!   (see `tests/test_no_unguarded_spawns.py` for why a webview is not one)
//!   and holds no state: webviews live in the Tauri manager under their
//!   labels, so there is no table here that could drift from what exists.
//! - The app ACL manifest gates *Tauri's* commands. It says nothing about
//!   what the shell itself can do: the engine subprocess, the PTYs and the
//!   webview windows are all created from Rust, and no capability reaches
//!   them. The manifest is the boundary for the `invoke` surface, and only
//!   for that.
//! - The close event is also emitted when the app tears every window down at
//!   exit. The main window is going with them, so the emit lands on nothing —
//!   harmless, and cheaper than a shutdown flag that would be a second piece of
//!   state to keep true.

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent};

/// Every browser webview's label begins with this.
///
/// The capability file matches `browser-*`; the coupling between the two is
/// asserted by [`the_browser_capability_set_is_empty`], which builds a real
/// label through [`webview_label`] and requires the committed capability to
/// match it. Renaming one without the other fails the test instead of
/// silently producing webviews outside their own declaration.
pub const LABEL_PREFIX: &str = "browser-";

/// The webview label for a browser tab.
///
/// Prefixed so a tab id can never collide with the main window's label — the
/// one window that *does* hold capabilities — and validated so no glob
/// metacharacter, whitespace, or separator can ride into a label that the ACL
/// then matches as a string. Refuses rather than sanitises: a caller whose id
/// needs cleaning does not know what it is doing.
pub fn webview_label(tab_id: &str) -> Result<String, String> {
    if tab_id.is_empty() {
        return Err("browser tab id is empty".to_string());
    }
    if tab_id.len() > 48 {
        return Err(format!("browser tab id is too long: {tab_id:?}"));
    }
    if !tab_id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return Err(format!(
            "browser tab id {tab_id:?} may only contain ASCII letters, digits, '-' and '_'"
        ));
    }
    Ok(format!("{LABEL_PREFIX}{tab_id}"))
}

/// The one decision this module exists to settle: may the browser be here?
///
/// `http`/`https` only, and never a loopback or unspecified host — see the
/// module docs for why the host half is load-bearing. Takes the parsed form
/// so `on_navigation` (which hands over a [`Url`]) and the commands (which
/// parse strings through [`parse_navigation`]) run the *same* check.
pub fn navigation_allowed(url: &Url) -> bool {
    if !matches!(url.scheme(), "http" | "https") {
        return false;
    }
    match url.host() {
        // No host at all — `about:blank`-class or opaque. Not a web origin
        // this guard can reason about, so it is not allowed.
        None => false,
        // RFC 6761 reserves `localhost.` and everything under `.localhost`;
        // the trailing dot is the same host and the label case never survives
        // URL parsing but is cheap to normalise anyway.
        Some(url::Host::Domain(domain)) => {
            let host = domain.trim_end_matches('.').to_ascii_lowercase();
            host != "localhost" && !host.ends_with(".localhost")
        }
        // All of 127/8 is loopback; 0.0.0.0 is unspecified (listening-any,
        // and reachable as "this machine" on many stacks).
        Some(url::Host::Ipv4(ip)) => !(ip.is_loopback() || ip.is_unspecified()),
        // ::1 and ::, plus IPv4-mapped spellings like [::ffff:127.0.0.1],
        // which would otherwise sidestep the IPv6 checks entirely.
        Some(url::Host::Ipv6(ip)) => match ip.to_ipv4_mapped() {
            Some(v4) => !(v4.is_loopback() || v4.is_unspecified()),
            None => !(ip.is_loopback() || ip.is_unspecified()),
        },
    }
}

/// Parse a navigation target and refuse it if the guard says no.
///
/// A [`Url`] rather than a bool on refusal, so the caller gets both the
/// decision and the reason in the error the UI can show.
pub fn parse_navigation(raw: &str) -> Result<Url, String> {
    let url = Url::parse(raw.trim()).map_err(|e| format!("not a navigable URL: {raw:?} ({e})"))?;
    if navigation_allowed(&url) {
        Ok(url)
    } else {
        Err(format!(
            "refusing to navigate to {raw:?}: browser webviews load http(s) on non-loopback hosts only (docs/03 §1.5)"
        ))
    }
}

/// Open a browser webview for a tab, or bring the existing one forward.
///
/// The label comes from [`webview_label`] and the URL from
/// [`parse_navigation`], so both refusals happen before anything exists.
/// Re-opening an existing tab navigates it — the guard runs again through
/// `on_navigation` on the way, so the double check cannot disagree.
pub fn open(app: &AppHandle, tab_id: &str, raw_url: &str) -> Result<String, String> {
    let label = webview_label(tab_id)?;
    let target = parse_navigation(raw_url)?;
    if let Some(existing) = app.get_webview_window(&label) {
        existing
            .navigate(target)
            .map_err(|e| format!("could not navigate browser tab {tab_id:?}: {e}"))?;
        let _ = existing.set_focus();
        return Ok(label);
    }
    let window = WebviewWindowBuilder::new(app, label.clone(), WebviewUrl::External(target))
        .title(format!("Codify — {tab_id}"))
        .inner_size(1100.0, 760.0)
        // The guard at the only place it can be enforced: every navigation
        // request the webview makes is asked first, including ones the page
        // initiates itself and server redirects. Returning false cancels.
        .on_navigation(|url| navigation_allowed(url))
        .build()
        .map_err(|e| format!("could not open browser webview: {e}"))?;
    // A browser tab's webview is a real OS window, so it can be closed without
    // the tab bar's permission — the user clicking that window's own close
    // button. Nothing about it reaches the strip, so announce it: the tab id,
    // emitted to the app, turned into a tab close by the main window.
    //
    // Registered on the built window because that is where Tauri offers it
    // (`Window::on_window_event`), and exactly once per window — re-opening an
    // existing tab returned above, so its handler is already registered.
    let sink = app.clone();
    let for_tab = tab_id.to_string();
    window.on_window_event(move |event| {
        if !reports_closed(event) {
            return;
        }
        // Broadcast, like the terminal's events, and only the main window can
        // receive it: a browser webview holds an empty capability set, so it
        // could not have registered a listener in the first place.
        let _ = sink.emit(
            CLOSED_EVENT,
            BrowserWindowClosed {
                tab_id: for_tab.clone(),
            },
        );
    });
    Ok(label)
}

/// Navigate an already-open browser tab. The guard runs twice on purpose:
/// once here (a clear refusal the UI can show) and once inside
/// `on_navigation` (the enforcement point no caller routes around).
pub fn navigate(app: &AppHandle, tab_id: &str, raw_url: &str) -> Result<String, String> {
    let label = webview_label(tab_id)?;
    let target = parse_navigation(raw_url)?;
    let window = app
        .get_webview_window(&label)
        .ok_or_else(|| format!("no browser tab {tab_id:?} is open"))?;
    window
        .navigate(target)
        .map_err(|e| format!("could not navigate browser tab {tab_id:?}: {e}"))?;
    Ok(label)
}

/// Close a browser tab's webview. `destroy` rather than `close`: a webview
/// holds no child process to reap (see module docs), but a close that a page
/// could veto would leave a window no caller can address by label again.
pub fn close(app: &AppHandle, tab_id: &str) -> Result<String, String> {
    let label = webview_label(tab_id)?;
    let window = app
        .get_webview_window(&label)
        .ok_or_else(|| format!("no browser tab {tab_id:?} is open"))?;
    window
        .destroy()
        .map_err(|e| format!("could not close browser tab {tab_id:?}: {e}"))?;
    Ok(label)
}

/// The event the main window listens for. Named rather than assumed: a test
/// below reads the UI module that subscribes to it and fails if the two drift,
/// because nothing in the type system spans a Rust constant and a TypeScript
/// string.
pub const CLOSED_EVENT: &str = "browser-window-closed";

/// Sent on [`CLOSED_EVENT`] when a browser webview's window is destroyed.
///
/// The **tab id**, not the webview label. The label is this module's business
/// (`browser-<tab>`); the tab id is the UI's, and naming it directly means the
/// other end need not know how webviews are labelled to close the right tab.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserWindowClosed {
    pub tab_id: String,
}

/// Is this window event the one that means "the window is gone"?
///
/// `Destroyed`, and not `CloseRequested`. Close-requested fires when a close is
/// *asked for* and can still be prevented — `prevent_close` exists, and a
/// handler or a page-driven flow could use it. Announcing a tab closed for a
/// window that is still open is the wrong direction to be wrong in: the strip
/// would drop the tab and the page would carry on running with nothing pointing
/// at it.
///
/// The wildcard arm is deliberate. `WindowEvent` is `#[non_exhaustive]`, and a
/// variant added later must not start reporting a close by accident.
pub fn reports_closed(event: &WindowEvent) -> bool {
    matches!(event, WindowEvent::Destroyed)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn parse(raw: &str) -> Url {
        Url::parse(raw).unwrap_or_else(|e| panic!("{raw:?} should parse: {e}"))
    }

    #[test]
    fn loopback_and_non_web_schemes_are_refused() {
        for raw in [
            // The name forms, on any port, in any case, with or without the
            // RFC 6761 trailing dot, and everything RFC 6761 reserves.
            "http://localhost/",
            "https://LOCALHOST:8443/login",
            "http://localhost./",
            "http://api.localhost/",
            "http://tauri.localhost/",
            // 127/8: every address, every port.
            "http://127.0.0.1/",
            "http://127.0.0.1:8080/engine/health",
            "http://127.9.9.9:1/",
            // Canonicalised spellings the WHATWG parser reduces to loopback:
            // decimal, hex, and octal all *are* 127.0.0.1.
            "http://2130706433/",
            "http://0x7f000001/",
            "http://0177.0.0.1/",
            // IPv6 loopback, long and short form, any port, and the
            // IPv4-mapped shape that would dodge a naive ::1 check.
            "http://[::1]/",
            "http://[::1]:3000/",
            "http://[0:0:0:0:0:0:0:1]/",
            "http://[::ffff:127.0.0.1]/",
            // Unspecified: 0.0.0.0 and its aliases.
            "http://0.0.0.0:9/",
            "http://0/",
            "http://[::]/",
            // Non-web schemes: the app's own origin, local files, script,
            // opaque documents. `tauri://localhost` and the Windows
            // `http://tauri.localhost` are the *local* origins whose invoke
            // path bypasses the ACL — none of them may be navigated to.
            "tauri://localhost/",
            "file:///etc/passwd",
            "javascript:alert(document.cookie)",
            "data:text/html,<h1>hi</h1>",
            "about:blank",
            "devtools://devtools/bins/devtools",
        ] {
            assert!(
                !navigation_allowed(&parse(raw)),
                "{raw:?} must be refused by the navigation guard"
            );
        }
    }

    #[test]
    fn ordinary_sites_are_allowed() {
        for raw in [
            "https://example.com/",
            "http://example.com:8080/path?q=1#frag",
            "https://docs.rs/tauri/latest/",
            // Only bare `localhost` and the RFC 6761 `.localhost` tree are
            // reserved loopback names; this is an ordinary external domain.
            "http://localhost.evil.example/",
            // A loopback-*looking* label on an external domain. It passes
            // because the guard is lexical, deliberately: DNS answers are not
            // knowable synchronously here, and the capability set — not the
            // hostname — is the boundary that stops this page invoking
            // anything. Pinned so nobody later "fixes" this into a DNS lookup
            // inside the navigation callback.
            "https://127.0.0.1.nip.io/",
        ] {
            assert!(
                navigation_allowed(&parse(raw)),
                "{raw:?} must be allowed by the navigation guard"
            );
        }
    }

    #[test]
    fn labels_are_prefixed_and_scoped() {
        assert_eq!(webview_label("tab-1").unwrap(), "browser-tab-1");
        // The prefix is what keeps a tab id out of the main window's
        // label no matter what the id says.
        assert_ne!(webview_label("main").unwrap(), "main");
        for bad in [
            "",
            "has space",
            "has/slash",
            "has.dot",
            "glob*star",
            "glob?mark",
            "[bracket]",
            &"z".repeat(49),
        ] {
            assert!(webview_label(bad).is_err(), "{bad:?} must be refused");
        }
    }

    /// All committed capability files, parsed the way Tauri reads them: when
    /// `app.security.capabilities` is unset (as it is here) every file in
    /// `capabilities/` is loaded.
    fn capability_files() -> Vec<(String, serde_json::Value)> {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("capabilities");
        let mut out = Vec::new();
        for entry in std::fs::read_dir(&dir).expect("src-tauri/capabilities must exist") {
            let path = entry.expect("directory entry").path();
            if path.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            let text = std::fs::read_to_string(&path).expect("capability file readable");
            let value: serde_json::Value = serde_json::from_str(&text)
                .unwrap_or_else(|e| panic!("{} is not valid JSON: {e}", path.display()));
            out.push((path.display().to_string(), value));
        }
        assert!(
            !out.is_empty(),
            "no capability files found — the ACL moved; this test must parse the real ones"
        );
        out
    }

    /// Does this capability's window/webview list reach the given label? The
    /// same glob semantics Tauri's ACL uses (`glob::Pattern`), so the
    /// assertion and the runtime agree on what "matches" means.
    fn list_reaches(list: &[serde_json::Value], label: &str) -> bool {
        list.iter().any(|entry| {
            let pattern = entry.as_str().unwrap_or_else(|| {
                panic!("capability window/webview pattern is not a string: {entry}")
            });
            glob::Pattern::new(pattern)
                .unwrap_or_else(|e| panic!("invalid glob pattern {pattern:?}: {e}"))
                .matches(label)
        })
    }

    /// The core assertion of this module: nothing grants anything to a
    /// browser webview, and the deny-by-default posture is committed in the
    /// open rather than implied by absence.
    #[test]
    fn the_browser_capability_set_is_empty() {
        let label = webview_label("tab-1").unwrap();
        let mut saw_browser_capability = false;
        for (path, cap) in capability_files() {
            let identifier = cap["identifier"].as_str().unwrap_or("<missing identifier>");
            if identifier == "browser" {
                saw_browser_capability = true;
                let windows = cap["windows"].as_array();
                assert!(
                    windows.is_some_and(|ws| list_reaches(ws, &label)),
                    "{path} must cover label {label:?} — the committed declaration no \
                     longer describes the webviews that exist"
                );
            }
            // Both keys, because Tauri matches either.
            for key in ["windows", "webviews"] {
                if let Some(list) = cap.get(key).and_then(|v| v.as_array()) {
                    if list_reaches(list, &label) {
                        let permissions = cap
                            .get("permissions")
                            .and_then(|v| v.as_array())
                            .map_or(0, Vec::len);
                        assert_eq!(
                            0, permissions,
                            "{path} ({identifier}) reaches browser label {label:?} with \
                             {permissions} permissions — a browser webview must hold an \
                             empty capability set"
                        );
                    }
                }
            }
        }
        assert!(
            saw_browser_capability,
            "capabilities/browser.json is missing — the browser's empty capability set \
             must be declared and committed, not merely true by nobody having said otherwise"
        );
    }

    // ------------------------------------------------------------------
    // The app ACL manifest.
    //
    // Everything above reads the capability files as committed text and
    // re-implements Tauri's glob semantics to do it. The tests below ask
    // Tauri to resolve the same files instead, through the same
    // `tauri_utils` types `generate_context!` uses, so "is a local-origin
    // invoke gated?" is answered by the code that answers it at runtime
    // rather than by a second opinion written here.
    // ------------------------------------------------------------------

    use std::{
        collections::{BTreeMap, BTreeSet},
        path::Path,
    };
    use tauri::utils::{
        acl::{
            capability::{Capability, CapabilityFile},
            manifest::Manifest,
            resolved::Resolved,
            ExecutionContext, APP_ACL_KEY,
        },
        platform::Target,
    };

    /// Every `.json`/`.toml` permission file under a directory, recursively.
    /// The extension filter is tauri-build's, so a README or a generated
    /// schema dropped in here is skipped rather than failing the parse.
    fn permission_files_under(dir: &Path, out: &mut Vec<PathBuf>) {
        for entry in std::fs::read_dir(dir)
            .unwrap_or_else(|e| panic!("{} must be readable: {e}", dir.display()))
        {
            let path = entry.expect("directory entry").path();
            if path.is_dir() {
                permission_files_under(&path, out);
                continue;
            }
            if matches!(
                path.extension().and_then(|e| e.to_str()),
                Some("json") | Some("toml")
            ) {
                out.push(path);
            }
        }
    }

    /// `src-tauri/permissions/` read the way `tauri_build::acl::build` reads
    /// it, parsed into the same [`PermissionFile`] type it parses into.
    fn app_manifest() -> Manifest {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("permissions");
        let mut paths = Vec::new();
        permission_files_under(&dir, &mut paths);
        assert!(
            !paths.is_empty(),
            "src-tauri/permissions/ is empty or missing — that directory *is* the app ACL \
             manifest, and without it Tauri allows any local-origin codify_* invoke"
        );
        let files = paths
            .iter()
            .map(|path| {
                let text = std::fs::read_to_string(path)
                    .unwrap_or_else(|e| panic!("{} must be readable: {e}", path.display()));
                match path.extension().and_then(|e| e.to_str()) {
                    Some("json") => serde_json::from_str(&text).unwrap_or_else(|e| {
                        panic!("{} is not a permission file: {e}", path.display())
                    }),
                    // This test parses JSON rather than growing a TOML
                    // dependency for the suite. Say so instead of failing
                    // with a type error.
                    _ => panic!(
                        "{} is a .toml permission file; express it as .json so the test leg \
                         can read it",
                        path.display()
                    ),
                }
            })
            .collect();
        Manifest::new(files, None)
    }

    /// The committed capability files as Tauri parses them, keyed by
    /// identifier — the map `tauri_build` hands to `Resolved::resolve`.
    fn parsed_capabilities() -> BTreeMap<String, Capability> {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("capabilities");
        let mut map = BTreeMap::new();
        for entry in std::fs::read_dir(&dir).expect("src-tauri/capabilities must exist") {
            let path = entry.expect("directory entry").path();
            if path.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            let capabilities = match CapabilityFile::load(&path)
                .unwrap_or_else(|e| panic!("{} is not a capability file: {e}", path.display()))
            {
                CapabilityFile::Capability(cap) => vec![cap],
                CapabilityFile::List(caps) | CapabilityFile::NamedList { capabilities: caps } => {
                    caps
                }
            };
            for cap in capabilities {
                assert!(
                    cap.platforms.is_none(),
                    "{} ({}) restricts itself to {:?}. These tests resolve against a single \
                     fixed target because platform gating is not what they are about; extend \
                     them rather than letting the resolution quietly skip the capability",
                    path.display(),
                    cap.identifier,
                    cap.platforms
                );
                assert!(
                    map.insert(cap.identifier.clone(), cap).is_none(),
                    "{}: duplicate capability identifier",
                    path.display()
                );
            }
        }
        assert!(
            !map.is_empty(),
            "no capability files parsed — the ACL moved; this must read the real ones"
        );
        map
    }

    /// Tauri resolving this app's half of the ACL.
    ///
    /// Prefixed entries (`core:default`, anything `plugin:`-scoped) are
    /// dropped first, and that is safe for the same reason the app's own
    /// commands cannot be reached through one: a prefixed entry resolves
    /// against another crate's manifest, and `Resolved::resolve` keys such a
    /// command as `plugin:<name>|<command>` — a name no `codify_*` invoke
    /// can send. `the_app_manifest_is_referenced_by_exactly_one_capability`
    /// is what keeps the set of unprefixed references, and therefore the
    /// set of things dropped here, knowable.
    fn resolved_app_acl() -> Resolved {
        let mut capabilities = parsed_capabilities();
        for cap in capabilities.values_mut() {
            cap.permissions
                .retain(|entry| entry.identifier().get_prefix().is_none());
        }
        let acl = BTreeMap::from([(APP_ACL_KEY.to_string(), app_manifest())]);
        Resolved::resolve(&acl, capabilities, Target::Linux)
            .expect("the committed app ACL must resolve")
    }

    /// The command names inside `generate_handler![...]` in lib.rs — the
    /// set the app actually answers.
    fn handler_commands() -> BTreeSet<String> {
        let source = include_str!("lib.rs");
        let body = source
            .split_once("tauri::generate_handler![")
            .expect("lib.rs invokes tauri::generate_handler!")
            .1
            .split_once(']')
            .expect("the generate_handler! body is closed")
            .0;
        body.split(',')
            .map(str::trim)
            .filter(|name| !name.is_empty())
            .map(str::to_string)
            .collect()
    }

    /// The bypass itself. Tauri allows a local-origin invoke of an app
    /// command with no capability check at all unless the app has an ACL
    /// manifest, and `has_app_acl` is true only when one was built. This is
    /// the flag `on_message` reads before it decides to skip the check.
    #[test]
    fn the_app_acl_manifest_closes_the_local_origin_bypass() {
        let manifest = app_manifest();
        // Verbatim from tauri-build's `acl::build`: a manifest is inserted
        // under `__app-acl__`, and only then, if it defines something. An
        // empty permissions/ directory leaves `has_app_acl` false and puts
        // the bypass straight back.
        assert!(
            manifest.default_permission.is_some()
                || !manifest.permission_sets.is_empty()
                || !manifest.permissions.is_empty(),
            "src-tauri/permissions/ defines no permission, so tauri-build emits no app ACL \
             manifest and a local-origin codify_* invoke is allowed unchecked again"
        );
        let resolved = resolved_app_acl();
        assert!(
            resolved.has_app_acl,
            "Tauri resolved has_app_acl = false — the local-origin bypass is open"
        );
    }

    /// Every command the handler defines is granted, and nothing else is.
    ///
    /// The failure this is here for is silent in both directions. Add a
    /// command to `invoke_handler!` without a permission and
    /// `filter_unused_commands` removes it from the handler at compile time
    /// (under `tauri build`): the UI's invoke fails with "command not
    /// found" and no compile error names the cause. Grant a command nothing
    /// defines and the app carries a permission no one can reach.
    #[test]
    fn the_grant_is_exactly_the_commands_the_handler_defines() {
        let granted: BTreeSet<String> = resolved_app_acl()
            .allowed_commands
            .keys()
            .cloned()
            .collect();
        let defined = handler_commands();
        assert!(
            !defined.is_empty(),
            "parsed no commands out of generate_handler![] — this test is not looking at \
             the handler any more"
        );
        assert_eq!(
            granted,
            defined,
            "the ACL grant and invoke_handler![] have drifted.\n  defined but not granted: \
             {:?}\n  granted but not defined: {:?}\nA command that is defined but not \
             granted is stripped from the handler by filter_unused_commands and fails at \
             runtime with 'command not found'.",
            defined.difference(&granted).collect::<Vec<_>>(),
            granted.difference(&defined).collect::<Vec<_>>(),
        );
    }

    /// The grant reaches the main window and nothing else — which is the
    /// part that survives an untrusted page.
    ///
    /// `reaches` is Tauri's own rule, copied from `RuntimeAuthority::resolve_access`:
    /// a resolved command counts only if the origin matches its context *and*
    /// a `webviews` pattern matches the webview label or a `windows` pattern
    /// matches the window label. [`WebviewWindowBuilder::new`] gives a webview
    /// window the same label for both, so a browser tab presents
    /// `browser-<tab>` on both sides and `main` matches neither.
    #[test]
    fn the_grant_reaches_main_and_no_browser_label() {
        let label = webview_label("tab-1").unwrap();
        let resolved = resolved_app_acl();
        for (command, entries) in &resolved.allowed_commands {
            for entry in entries {
                let reaches = |target: &str| {
                    entry
                        .windows
                        .iter()
                        .chain(entry.webviews.iter())
                        .any(|pattern| pattern.matches(target))
                };
                assert_eq!(
                    entry.context,
                    ExecutionContext::Local,
                    "{command} is granted in a non-local context; this app configures no \
                     remote URL patterns and must not start"
                );
                assert!(
                    reaches("main"),
                    "{command} is granted to nobody the main window matches — the UI would \
                     call a command the ACL refuses"
                );
                assert!(
                    !reaches(&label),
                    "{command} resolves for browser label {label:?} — a remote page that \
                     reached a local origin could invoke it"
                );
            }
        }
    }

    /// One reference, in one capability.
    ///
    /// Every unprefixed permission reference in `capabilities/` resolves
    /// against the app manifest, so each one is somewhere the grant can
    /// widen. `the_grant_reaches_main_and_no_browser_label` catches a
    /// widened *reach*; this catches a widened *set* that reaches only main
    /// and would otherwise pass unnoticed.
    #[test]
    fn the_app_manifest_is_referenced_by_exactly_one_capability() {
        let mut references: Vec<(String, String)> = Vec::new();
        for (identifier, cap) in parsed_capabilities() {
            for entry in &cap.permissions {
                if entry.identifier().get_prefix().is_none() {
                    references.push((identifier.clone(), entry.identifier().get().to_string()));
                }
            }
        }
        assert_eq!(
            references,
            vec![("default".to_string(), "shell".to_string())],
            "the app ACL manifest is referenced by {references:?}. Every unprefixed \
             reference is a place the grant can widen, and the tests above can only \
             vouch for a set they know the whole of."
        );
    }

    /// The registration in `open` is the one link the tests above cannot
    /// reach: it needs a live window and a display. This is a read of this
    /// file's own source, crude on purpose, and it exists so *deleting* the
    /// listener fails here instead of silently — the same "freeze the decision
    /// site" shape as `tests/test_no_unguarded_spawns.py`, for the one line
    /// that a predicate test cannot protect.
    #[test]
    fn open_still_registers_the_close_listener() {
        let source = include_str!("browser.rs");
        let after_open = source
            .split_once("pub fn open(")
            .expect("this file defines `open`")
            .1;
        let body = after_open
            .split_once("pub fn navigate(")
            .expect("`open` is followed by `navigate`")
            .0;
        for needed in [
            "on_window_event",
            "reports_closed",
            "CLOSED_EVENT",
            "BrowserWindowClosed",
        ] {
            assert!(
                body.contains(needed),
                "`open` no longer mentions {needed} — the close signal is wired in \
                 there, and nothing else registers it"
            );
        }
    }

    /// A close is reported when the window is *gone* and for nothing else: a
    /// tab must not be dropped because a close was asked for, or because the
    /// window was resized or lost focus.
    #[test]
    fn a_close_is_reported_on_destroyed_and_on_nothing_else() {
        assert!(reports_closed(&WindowEvent::Destroyed));
        assert!(!reports_closed(&WindowEvent::Focused(true)));
        assert!(!reports_closed(&WindowEvent::Focused(false)));
        assert!(!reports_closed(&WindowEvent::Resized(
            tauri::PhysicalSize::new(800, 600)
        )));
    }

    /// The wire shape the other end reads. `tab_id` is the field the UI's
    /// `readBrowserWindowClosed` looks for, and serde would drop a rename on
    /// this struct to a silent null there.
    #[test]
    fn the_closed_payload_names_the_tab() {
        let payload = BrowserWindowClosed {
            tab_id: "tab-1".to_string(),
        };
        assert_eq!(
            serde_json::to_value(&payload).unwrap(),
            serde_json::json!({ "tab_id": "tab-1" })
        );
    }

    /// The event name is a contract between two languages. This is the same
    /// move as the capability test above — read the committed file rather than
    /// assume the two ends agree.
    #[test]
    fn the_ui_listens_for_the_event_this_module_emits() {
        let ui = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("src-tauri has a parent")
            .join("ui/src/shellEvents.ts");
        let text = std::fs::read_to_string(&ui)
            .unwrap_or_else(|e| panic!("{} must exist: {e}", ui.display()));
        assert!(
            text.contains(&format!("\"{CLOSED_EVENT}\"")),
            "ui/src/shellEvents.ts does not name {CLOSED_EVENT:?} — the shell emits it \
             and the UI would never hear it"
        );
        assert!(
            text.contains("tab_id"),
            "ui/src/shellEvents.ts does not read the tab_id the payload carries"
        );
    }

    /// `app.security.capabilities` can inline a capability (or reference a
    /// file by identifier) instead of loading the whole directory. The dir
    /// scan above cannot see an inline grant, so this closes that hole.
    #[test]
    fn tauri_conf_inlines_nothing_for_browser_webviews() {
        let label = webview_label("tab-1").unwrap();
        let conf_path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tauri.conf.json");
        let text = std::fs::read_to_string(&conf_path).expect("tauri.conf.json readable");
        let conf: serde_json::Value = serde_json::from_str(&text).expect("tauri.conf.json is JSON");
        // Unset means "every file in capabilities/", already scanned above.
        let Some(entries) = conf
            .pointer("/app/security/capabilities")
            .and_then(|v| v.as_array())
        else {
            return;
        };
        for entry in entries {
            // A bare string is an identifier reference to a file in
            // capabilities/, which the directory scan covers.
            let Some(cap) = entry.as_object() else {
                continue;
            };
            let reaches = ["windows", "webviews"].iter().any(|key| {
                cap.get(*key)
                    .and_then(|v| v.as_array())
                    .is_some_and(|list| list_reaches(list, &label))
            });
            if reaches {
                let permissions = cap
                    .get("permissions")
                    .and_then(|v| v.as_array())
                    .map_or(0, Vec::len);
                assert_eq!(
                    0, permissions,
                    "inline capability in tauri.conf.json reaches browser label \
                     {label:?} with {permissions} permissions"
                );
            }
        }
    }
}
