/// Everything this module ships, as one string: `mod.rs`, `page_layer.rs` and `smoke.rs`, and
/// never `tests.rs`. Several tests below read the shell's own source as the contract. They used
/// to do it with `include_str!("browser.rs")` and a cut at `#[cfg(test)]`, so that a test could
/// not match the very string it asserts is absent; with the tests in a file of their own that
/// promise holds by construction, because this function does not read it.
///
/// Every test that asserts an *absence* over this string passes vacuously if a file stops
/// contributing to it, which is what a split invites, so each file is checked for one thing only
/// it holds before the string is handed out.
fn production_source() -> String {
    let files = [
        (
            "mod.rs",
            include_str!("mod.rs"),
            "pub fn navigation_allowed(",
        ),
        (
            "page_layer.rs",
            include_str!("page_layer.rs"),
            "gtk::Overlay::new()",
        ),
        ("smoke.rs", include_str!("smoke.rs"), "pub fn smoke_mode("),
    ];
    for (name, text, anchor) in files {
        assert!(
            text.contains(anchor),
            "browser/{name} no longer contains `{anchor}`: a source-reading test over this \
                 module would now pass without looking at it"
        );
    }
    files.map(|(_, text, _)| text).concat()
}

/// The page layer's source: what `mod page_layer` used to be, before it was a file.
fn page_layer_source() -> &'static str {
    include_str!("page_layer.rs")
}
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
            let webviews = cap["webviews"].as_array();
            assert!(
                webviews.is_some_and(|ws| list_reaches(ws, &label)),
                "{path} must cover webview label {label:?} — the committed declaration \
                     no longer describes the webviews that exist"
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
    for entry in
        std::fs::read_dir(dir).unwrap_or_else(|e| panic!("{} must be readable: {e}", dir.display()))
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
                Some("json") => serde_json::from_str(&text)
                    .unwrap_or_else(|e| panic!("{} is not a permission file: {e}", path.display())),
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
            CapabilityFile::List(caps) | CapabilityFile::NamedList { capabilities: caps } => caps,
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
    let source = include_str!("../lib.rs");
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

/// A background tab's page must not become visible by being seated.
///
/// Measured, not stylistic: the old seat was an unconditional `show()`, so
/// restoring a strip of N browser tabs seated N stacked, all-visible
/// webviews — and with the dmabuf path disabled (this machine's Nvidia
/// workaround) each visible page's frames are software `pixman` blits on
/// the GTK main thread. Four pages stacked up to a main loop that had no
/// frame budget left for input, which is the freeze that was measured:
/// 96% of a core on the main thread with four pages, idle with none.
///
/// The UI's own rule is "exactly one page visible — the active tab's",
/// enforced through `focus` on every active-tab change. The seat is the one
/// path that could break the invariant, because it happens before any focus
/// and used to decide visibility on its own. `seat_visibility` closes that
/// path by consulting the bridge's record of the active tab.
#[test]
fn a_seated_background_page_is_hidden_not_shown() {
    // No tab has been focused: the seat cannot know better than to show,
    // which is the boot-time case before the UI's first focus lands.
    crate::webview_bridge::note_active("");
    assert_eq!(
        seat_visibility("browser-sometab"),
        SeatVisibility::Shown,
        "a page whose tab was never focused must keep the boot-time show, or a \
             restored page could miss its first paint behind a hidden window"
    );

    // A different tab is active: this page is background and must be seated
    // hidden, so restoring a strip of pages seats exactly one visible one.
    crate::webview_bridge::note_active("othertab");
    assert_eq!(
        seat_visibility("browser-sometab"),
        SeatVisibility::Hidden,
        "seating a background page visibly is the boot freeze: every hidden \
             page's frames are still produced and land on the main loop"
    );

    // The active tab's own page: shown, as focus() would leave it.
    assert_eq!(
        seat_visibility("browser-othertab"),
        SeatVisibility::Shown,
        "the active tab's page must not be hidden by its own seat"
    );

    // Leave the shared bridge state as it was found: this is a process-wide
    // static, and another test reading it must not inherit this one's write.
    crate::webview_bridge::note_active("");
}

/// The seat must consult the bridge's *recorded* active tab, and the
/// recording must be the focus command's job — so the wiring is pinned
/// from both sides: `codify_browser_focus` writes, `seat_visibility`
/// reads.
#[test]
fn the_seat_reads_the_tab_focus_recorded() {
    let source = include_str!("../lib.rs");
    let focus_command = source
        .split_once("async fn codify_browser_focus")
        .expect("lib.rs declares the focus command")
        .1
        .split_once("fn codify_browser")
        .map(|(body, _)| body)
        .unwrap_or(source);
    assert!(
        focus_command.contains("webview_bridge::note_active"),
        "the focus command must record the active tab before focusing — the \
             seat's visibility decision is only as honest as that record"
    );
    let this_module = production_source();
    let main_module = &this_module;
    assert!(
        main_module.contains("webview_bridge::active()"),
        "seat_visibility must read the bridge's active-tab record — a seat \
             that guesses visibility outside the focus record is how every page \
             ends up visible at once"
    );
}

/// The §7.2 prose of `docs/09-workspace-shell.md` — the paragraph that
/// lists this layer's commands.    ///
/// Scoped to the section rather than to the file on purpose: §7.3 talks
/// about the *absence* of the removed system-browser hand-off, and a check
/// that read the whole document would flag that honest sentence as a
/// command that exists.
fn docs_browser_command_section() -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent")
        .join("docs/09-workspace-shell.md");
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("{} must exist: {e}", path.display()));
    let after = text
        .split_once("### 7.2 Browser")
        .expect("docs/09 has a §7.2 for the browser's shell layer")
        .1;
    after
        .split_once("### 7.3")
        .expect("§7.2 is followed by §7.3")
        .0
        .to_string()
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

/// `docs/09` §7.2 lists this layer's commands, and the list is checked
/// rather than trusted.
///
/// This exists because the list *was* wrong, twice, in the same paragraph:
/// it named `codify_browser_open_external` for as long as the command
/// existed and then for a while after it did not, and it never mentioned
/// `codify_browser_devtools_available` at all. A reader deciding whether
/// the browser is inside the app was reading a section that both claimed a
/// way out of the app and could not tell them what the shell answers.
///
/// Both directions are checked, and the parse is lexical, so the weakest
/// form of this test is the vacuous one: a §7.2 that stops naming any
/// command must fail, not pass. Hence the non-empty assertion first.
#[test]
fn the_docs_name_the_browser_commands_that_exist() {
    let section = docs_browser_command_section();
    let documented: BTreeSet<String> = section
        .split(|c: char| !c.is_ascii_alphanumeric() && c != '_')
        .filter(|token| token.starts_with("codify_browser_"))
        .map(str::to_string)
        .collect();
    let defined: BTreeSet<String> = handler_commands()
        .into_iter()
        .filter(|name| name.starts_with("codify_browser_"))
        .collect();
    assert!(
        !documented.is_empty(),
        "docs/09 §7.2 names no browser command — this test is no longer reading \
             the list it was written for"
    );
    assert!(
        !defined.is_empty(),
        "parsed no browser command out of generate_handler![] — this test is not \
             looking at the handler any more"
    );
    assert_eq!(
        documented,
        defined,
        "docs/09 §7.2 and invoke_handler![] have drifted.\n  in the handler but \
             undocumented: {:?}\n  documented but not in the handler: {:?}\nThe second \
             kind is the one that lies: a command named here after it was deleted \
             sends the next reader looking for code that is not there, and a page \
             that 'cannot' be opened outside the app reads as an app with a way out.",
        defined.difference(&documented).collect::<Vec<_>>(),
        documented.difference(&defined).collect::<Vec<_>>(),
    );
}

/// The grant reaches the main window and nothing else — which is the
/// part that survives an untrusted page.
///
/// `reaches` is Tauri's own rule, copied from `RuntimeAuthority::resolve_access`:
/// a resolved command counts only if the origin matches its context *and*
/// a `webviews` pattern matches the webview label or a `windows` pattern
/// matches the window label. The app's UI and every browser page now share
/// one **window**, so the grant is matched on `webviews` alone: the UI's
/// webview label is `main`, a browser page's is `browser-<tab>`, and a
/// `windows` pattern reaching `main` would hand every child seated in the
/// window the whole grant.
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

/// The grant is matched on `webviews`, never on `windows`.
///
/// The one shape the test above cannot see: a capability that reaches
/// `main` through a **`windows`** pattern. With pages embedded in the
/// main window, a window-matched grant is inherited by every child
/// webview seated in it — the exact bypass the re-point exists to close.
/// This is the test that keeps it closed even when the pattern happens
/// to resolve correctly today.
#[test]
fn no_capability_grants_through_a_window_pattern() {
    for (path, cap) in capability_files() {
        let permissions = cap
            .get("permissions")
            .and_then(|v| v.as_array())
            .map_or(0, Vec::len);
        if permissions == 0 {
            continue; // the empty browser declaration matches on purpose
        }
        assert!(
            cap.get("windows").is_none(),
            "{path} grants through `windows` — an embedded child webview reports its \
                 parent's window label and would inherit the whole grant"
        );
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
/// wiring fails here instead of silently — the same "freeze the decision
/// site" shape as `tests/test_no_unguarded_spawns.py`, for the lines a
/// predicate test cannot protect.
#[test]
fn open_still_registers_the_embed_wiring() {
    let source = production_source();
    let after_open = source
        .split_once("pub fn open(")
        .expect("this file defines `open`")
        .1;
    let body = after_open
        .split_once("pub fn navigate(")
        .expect("`open` is followed by `navigate`")
        .0;
    for needed in [
        "on_navigation",
        "on_new_window",
        "NewWindowResponse::Deny",
        "add_child",
    ] {
        assert!(
            body.contains(needed),
            "`open` no longer mentions {needed} — the navigation guard, the popup \
                 refusal and the embed are all wired in there, and nothing else registers \
                 them"
        );
    }
    // And the old separate-window shape must not come back through the
    // side door: this module builds child webviews and nothing else. The
    // scan is over the shipped files only (`production_source`), because this
    // assertion names the builder it refuses.
    assert!(
        !source.contains("WebviewWindowBuilder"),
        "browser.rs builds a WebviewWindow again — the browser is embedded; a separate \
             OS window is the shape this module exists to not have"
    );
}

/// Popups are refused and announced, and the announcement carries what
/// the user needs to open the target themselves.
#[test]
fn a_popup_is_denied_and_announced_with_its_target() {
    let payload = BrowserPopupRequested {
        tab_id: "tab-1".to_string(),
        url: "https://accounts.example/signin".to_string(),
    };
    let wire = serde_json::to_value(&payload).unwrap();
    assert_eq!(
        wire,
        serde_json::json!({
            "tab_id": "tab-1",
            "url": "https://accounts.example/signin"
        })
    );
    // The refusal itself is structural: the handler returns Deny on every
    // path (asserted at the call site by the source freeze above), and
    // this payload is the *only* thing a page learns happened — nothing.
    assert_eq!(POPUP_REQUESTED_EVENT, "browser-popup-requested");
}

/// The focus announcement is a name and a tab id, and nothing a page chose.
///
/// The tab id is what the UI acts on (it moves the split's focus to that pane),
/// so the wire is exactly that: no url, no title, nothing read from the page.
#[test]
fn a_page_focus_announcement_carries_only_the_tab() {
    assert_eq!(PAGE_FOCUSED_EVENT, "browser-page-focused");
    let wire = serde_json::to_value(BrowserPageFocused {
        tab_id: "tab-1".to_string(),
    })
    .unwrap();
    assert_eq!(wire, serde_json::json!({ "tab_id": "tab-1" }));
}

/// The two user escapes are wired, and the module still spawns nothing
/// The inspector is the one escape left, and it stays a shell-side surface
/// rather than a process. The system-browser hand-off this file used to
/// carry is **gone**, and the assertion is the inverse of the old one on
/// purpose: a removal nobody can see is a removal that comes back, so the
/// name, the `open` crate and the OS handler are each pinned absent.
#[test]
fn the_only_escape_is_the_inspector_and_the_module_spawns_nothing() {
    let source = production_source();
    let main_module = &source;
    assert!(
        main_module.contains("pub fn open_devtools"),
        "the DevTools command is gone — the pane's inspector control would dead-end"
    );
    for gone in [
        "pub fn open_external",
        "open::that_detached",
        "that_detached",
    ] {
        assert!(
                !main_module.contains(gone),
                "browser.rs has grown {gone:?} back: a page's URL must never reach              the operating system's own browser. Every website this app can open              is a tab in this app, and the way to keep it that way is for there              to be no code here that could hand one out"
            );
    }
    // And no direct process start anywhere in this module: the spawn
    // freeze tests/test_no_unguarded_spawns.py would fail anyway, but this
    // fails in the same breath as the feature it guards.
    assert!(
            !main_module.contains("Command::new"),
            "browser.rs starts a process with Command::new — a browser tab that can              launch something is a browser tab with a shell, whatever the command              is called"
        );
}

/// The `open` crate is not a dependency, and nothing in the crate reaches for it
///
/// The test above pins the hand-off out of *this* file, which was never the
/// same claim. A command can be gone from every source file while the crate
/// that served it sits in `Cargo.toml` pulling `windows-sys` and `is-wsl`
/// into the graph — which is exactly what had happened: `open` outlived its
/// last caller, and nothing failed, because an unused dependency is not an
/// error to any tool in the gate.
///
/// Two halves, and the second is the one that does the work. `cargo check`
/// *is* the proof that nothing reaches for it — an unresolved crate path
/// does not compile — but it only proves it on the day it runs and says
/// nothing about *why*. This states the reason next to the failure, which
/// is what stops the re-add: someone reaching for a hand-off finds a test
/// explaining that there is not one, rather than a missing dependency and
/// the easy fix of running `cargo add open`.
#[test]
fn the_open_crate_is_not_a_dependency_and_nothing_reaches_for_it() {
    // A comment naming the removed hand-off is not a hand-off. This file's
    // own module docs mention `open_external` to explain that it is gone,
    // and a scanner that failed on that would be pushing the next reader
    // towards rewording the explanation rather than towards leaving the
    // behaviour alone — which is the wrong pressure to apply.
    //
    // A hand-written line scan, and a deliberately narrow claim: it knows
    // about `//` comments and `"` strings, which is every case in this
    // crate, and it says so rather than pretending to be a parser. A block
    // comment holding a reference would slip past — harmless, because a
    // comment cannot hand a URL to anything.
    fn code_only(line: &str) -> &str {
        let bytes = line.as_bytes();
        let mut in_string = false;
        let mut i = 0;
        while i < bytes.len() {
            match bytes[i] {
                b'\\' if in_string => i += 1,
                b'"' => in_string = !in_string,
                b'/' if !in_string && bytes.get(i + 1) == Some(&b'/') => {
                    return &line[..i];
                }
                _ => {}
            }
            i += 1;
        }
        line
    }

    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let manifest = std::fs::read_to_string(manifest_dir.join("Cargo.toml"))
        .unwrap_or_else(|e| panic!("src-tauri/Cargo.toml must be readable: {e}"));
    // Hand-parsed rather than with a toml crate this test does not have:
    // the only shape that matters is a line whose key is `open`, and a
    // mention inside a comment is a mention inside a comment. A stricter
    // read would reject this manifest's own note, which is why it is
    // stripped before the key is compared.
    for (number, line) in manifest.lines().enumerate() {
        let code = line.split('#').next().unwrap_or_default().trim();
        let is_open = code
            .split_once('=')
            .map(|(key, _)| key.trim() == "open")
            .unwrap_or(false);
        assert!(
            !is_open,
            "src-tauri/Cargo.toml:{} re-declares the `open` crate. It exists to hand a URL to \
                 the operating system's own browser, and every website Codify can open is a tab in \
                 Codify. If a hand-off is genuinely wanted, the decision belongs in the browser \
                 module's doc comment — not in a manifest.",
            number + 1
        );
    }

    // Crate-wide, not just this file: a hand-off could be written in lib.rs
    // or terminal.rs tomorrow and a check pointed at browser.rs would not
    // see it. Each file is scanned up to its `#[cfg(test)]` because the
    // tests *name* these strings in order to assert they are gone — a
    // whole-file scan would match its own assertions and fail forever,
    // which is how a check like this quietly stops being one.
    let src = manifest_dir.join("src");
    // Recursive, and it skips `tests.rs`: a module that is a directory (this one is) would
    // otherwise drop out of the scan without a word, which is the failure the scan exists to
    // prevent. `tests.rs` is skipped for the reason given above — its tests name these strings.
    fn rust_sources(dir: &std::path::Path, found: &mut Vec<PathBuf>) {
        let entries = std::fs::read_dir(dir)
            .unwrap_or_else(|e| panic!("{} must be readable: {e}", dir.display()));
        for entry in entries {
            let path = entry.expect("a readable directory entry").path();
            if path.is_dir() {
                rust_sources(&path, found);
            } else if path.extension().and_then(|e| e.to_str()) == Some("rs")
                && path.file_name().and_then(|n| n.to_str()) != Some("tests.rs")
            {
                found.push(path);
            }
        }
    }
    let mut sources = Vec::new();
    rust_sources(&src, &mut sources);
    for path in &sources {
        let text = std::fs::read_to_string(path)
            .unwrap_or_else(|e| panic!("{} must be readable: {e}", path.display()));
        let live = text
            .split_once("#[cfg(test)]")
            .map_or(text.as_str(), |(head, _)| head)
            .lines()
            .map(code_only)
            .collect::<Vec<_>>()
            .join("\n");
        for gone in ["open::", "open_external"] {
            assert!(
                !live.contains(gone),
                "{} has grown {gone:?} back. A page's URL must never reach the operating \
                     system's own browser — every website this app can open is a tab in this app, \
                     and the way to keep it that way is for there to be no code that could hand \
                     one out.",
                path.display()
            );
        }
    }
    // The scan is the whole argument when it is complete, so its own
    // coverage is part of the claim: a read_dir that found one file would
    // pass every assertion above while proving nothing, and one that did not
    // descend would lose the browser module altogether.
    assert!(
        sources.len() >= 5,
        "the scan only reached {} Rust files — it is not looking at the crate it claims to",
        sources.len()
    );
    for needed in [
        "browser/mod.rs",
        "browser/page_layer.rs",
        "browser/smoke.rs",
    ] {
        assert!(
            sources.iter().any(|p| p.ends_with(needed)),
            "the scan never reached src/{needed}: the browser module is outside the check that no \
                 page's URL can be handed to the operating system"
        );
    }
}

/// The UI knows the commands these tests pin. The event file is shared
/// surface; this is the same cross-language read, pointed at the api
/// module this time.
#[test]
fn the_ui_knows_the_escape_commands() {
    let ui = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent")
        .join("ui/src/api.ts");
    let text =
        std::fs::read_to_string(&ui).unwrap_or_else(|e| panic!("{} must exist: {e}", ui.display()));
    for command in [
        "codify_browser_devtools_open",
        "codify_browser_devtools_close",
        "codify_browser_devtools_state",
    ] {
        assert!(
                text.contains(&format!("\"{command}\"")),
                "ui/src/api.ts does not name {command:?} — the shell registers it                  and the UI would never call it"
            );
    }
    // …and the hand-off is absent on both sides at once, so a half-removal
    // (command gone, button left) cannot leave a control that dead-ends.
    assert!(
            !text.contains("codify_browser_open_external") && !text.contains("openBrowserExternal"),
            "ui/src/api.ts still names the system-browser hand-off — the shell                  no longer registers it, so the control would refuse at the user"
        );
}

/// The build carries the feature the inspector commands are compiled
/// against, through this crate's own forwarding `devtools` feature
/// (Cargo.toml). Losing it fails here in the same breath as the feature
/// it guards — the pane's control is unconditional because this is.
#[test]
fn the_build_has_the_devtools_feature() {
    assert!(
        devtools_available(),
        "the crate no longer builds with the devtools feature — the pane's \
             inspector control would offer a command the shell does not have"
    );
}

/// The page-life payload: two fields on the wire for load events, three
/// for the title, and the tab id always first-class.
#[test]
fn the_page_state_payload_names_the_tab_and_rides_the_url() {
    let loading = serde_json::to_value(BrowserPageState {
        tab_id: "tab-1".to_string(),
        url: "https://example.com/".to_string(),
        title: None,
    })
    .unwrap();
    assert_eq!(
        loading,
        serde_json::json!({ "tab_id": "tab-1", "url": "https://example.com/" }),
        "a load event carries no title field — the shape the UI reads"
    );
    let titled = serde_json::to_value(BrowserPageState {
        tab_id: "tab-1".to_string(),
        url: "https://example.com/".to_string(),
        title: Some("Example Domain".to_string()),
    })
    .unwrap();
    assert_eq!(titled["title"], "Example Domain");
}

/// The wiring is the part no predicate can reach: the hooks are attached
/// where the page is built, and nothing else registers them.
#[test]
fn open_still_reports_the_page_life() {
    let source = production_source();
    let main_module = &source;
    let after_open = main_module
        .split_once("pub fn open(")
        .expect("this file defines `open`")
        .1;
    let body = after_open
        .split_once("pub fn navigate(")
        .expect("`open` is followed by `navigate`")
        .0;
    for needed in [
        "on_page_load",
        "PageLoadEvent::Started",
        "PageLoadEvent::Finished",
        "on_document_title_changed",
        "PAGE_LOADING_EVENT",
        "PAGE_LOADED_EVENT",
        "PAGE_TITLED_EVENT",
    ] {
        assert!(
            body.contains(needed),
            "`open` no longer mentions {needed} — the page-life hooks are wired \
                 there, and nothing else registers them"
        );
    }
    // And the static popup sink must stay gone: a boot-order invariant for
    // nothing is exactly the kind of thing this module does not re-grow.
    // `static POPUP_SINK` by name: the doc comment above the retired sink
    // mentions OnceLock as prose, and a lexical check that flags prose is
    // a scanner nobody trusts. The declaration is the decision site.
    assert!(
        !main_module.contains("static POPUP_SINK"),
        "browser.rs grew a static sink again — the handlers capture their own \
             AppHandle clones in open()"
    );
}

/// The event names are a contract between two languages. This is the same
/// move as the capability test above — read the committed file rather
/// than assume the two ends agree.
#[test]
fn the_ui_listens_for_the_events_this_module_emits() {
    let ui = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent")
        .join("ui/src/shellEvents.ts");
    let text =
        std::fs::read_to_string(&ui).unwrap_or_else(|e| panic!("{} must exist: {e}", ui.display()));
    for event in [
        POPUP_REQUESTED_EVENT,
        PAGE_LOADING_EVENT,
        PAGE_LOADED_EVENT,
        PAGE_TITLED_EVENT,
        PAGE_FOCUSED_EVENT,
    ] {
        assert!(
            text.contains(&format!("\"{event}\"")),
            "ui/src/shellEvents.ts does not name {event:?} — the shell \
                 emits it and the UI would never hear it"
        );
    }
    assert!(
        text.contains("tab_id"),
        "ui/src/shellEvents.ts does not read the tab_id the payloads carry"
    );
    // The separate-window build's event must not linger on either side:
    // an embedded page has no window to destroy, and a listener for an
    // event nobody emits is code pretending to handle something.
    assert!(
        !text.contains("browser-window-closed"),
        "ui/src/shellEvents.ts still listens for browser-window-closed — that event \
             announced a separate window, and the browser no longer opens one"
    );
}

/// `ui/src/browserDispatch.ts` mirrors the navigation guard so a refusal
/// is shown before the round trip instead of after it. A mirror that
/// disagreed with the original would refuse pages the shell allows (a
/// dead address bar) or seat pages the shell refuses — so this test
/// feeds the mirror's own refused/allowed tables through the *real*
/// guard, in both directions.
#[test]
fn the_ui_dispatch_mirror_agrees_with_the_navigation_guard() {
    let ui = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent")
        .join("ui/tests/browserDispatch.test.ts");
    let text =
        std::fs::read_to_string(&ui).unwrap_or_else(|e| panic!("{} must exist: {e}", ui.display()));

    let list = |name: &str| -> Vec<String> {
        let at = text
            .split_once(&format!("const {name}"))
            .unwrap_or_else(|| panic!("the UI mirror test no longer defines {name}"))
            .1;
        let end = at
            .split_once("];")
            .unwrap_or_else(|| panic!("{name} must be closed with ];"))
            .0;
        end.lines()
            .filter_map(|line| {
                let line = line.trim().trim_end_matches(',');
                let raw = line.strip_prefix('"')?.strip_suffix('"')?;
                Some(raw.replace("\\\"", "\"").replace("\\\\", "\\"))
            })
            .collect()
    };
    let refused = list("REFUSED_IN_UI");
    let allowed = list("ALLOWED_IN_UI");
    assert!(
        !refused.is_empty() && !allowed.is_empty(),
        "the UI mirror's tables are empty — this test is not reading them any more"
    );

    // What the UI module actually does with a typed address, in the
    // order it does it: keep a typed `http(s)://`, refuse any *other*
    // typed scheme before parsing (the URL parser would rescue its
    // authority into a host), prefix everything else with `https://`.
    // The mirror feeds the *result* of that pipeline to the guard, so a
    // divergence in the pipeline itself shows up here too.
    let as_the_ui_sends = |raw: &str| -> Option<String> {
        let trimmed = raw.trim();
        if trimmed.starts_with("http://") || trimmed.starts_with("https://") {
            return Some(trimmed.to_string());
        }
        let typed_scheme = trimmed.len() >= 3
            && trimmed
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_alphabetic())
            && trimmed[1..].split_once("://").is_some_and(|(scheme, _)| {
                !scheme.is_empty()
                    && scheme
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
            });
        if typed_scheme {
            return None; // refused before any parsing
        }
        Some(format!("https://{trimmed}"))
    };

    for raw in &refused {
        // The UI refuses *before* the shell; the guard is the truth, so
        // everything the UI refuses, the guard must refuse too — the
        // typed-scheme refusals included, which is what `None` encodes:
        // the shell refuses unparseable input, so both ends say no.
        let sent = as_the_ui_sends(raw);
        let refused_by_guard = match sent {
            None => true,
            Some(url) => Url::parse(&url)
                .map(|u| !navigation_allowed(&u))
                .unwrap_or(true),
        };
        assert!(
            refused_by_guard,
            "the UI refuses {raw:?} but the shell would allow what the pane sends — \
                 the mirror is stricter than the guard, and would dead-end addresses \
                 that work"
        );
    }
    for raw in &allowed {
        let sent = as_the_ui_sends(raw)
            .unwrap_or_else(|| panic!("the UI test allows {raw:?} but the pane would refuse it"));
        let url = parse(&sent);
        assert!(
            navigation_allowed(&url),
            "the UI allows {raw:?} but the shell refuses {sent:?} — the mirror is \
                 looser than the guard, and would seat a page the shell immediately \
                 rejects"
        );
    }
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

#[test]
fn bounds_are_finite_positive_rectangles() {
    // Serialise the shape the UI sends, so a rename here fails the wire
    // test instead of arriving as a silent zero.
    let wire = serde_json::to_value(Bounds {
        x: 0.0,
        y: 48.0,
        width: 1280.0,
        height: 700.0,
    })
    .unwrap();
    assert_eq!(
        wire,
        serde_json::json!({"x": 0.0, "y": 48.0, "width": 1280.0, "height": 700.0}),
        "the Bounds field names are the IPC contract with ui/src/api.ts"
    );
}

// ------------------------------------------------------------------
// The first-paint story: diagnostics, the zero-bounds refusal, and the
// smoke mode.
//
// The three mechanisms that can seat a page and never paint it (the
// session-bus exit in `build()`, WebKit's sandbox refusing to start the
// web process, the DMABUF renderer path) are all outside this crate's
// reach at runtime — they are diagnosed, not worked around. What the
// tests below can hold are the decisions: that the diagnostics run at
// the top of `run()` and nowhere later, that a zero-size pane refuses
// with the sentence that names the recovery, and that smoke mode is
// gated, reports, and never starts an engine.
// ------------------------------------------------------------------

/// The environment diagnostics are the shell's first word, printed
/// before anything else can fail. The call site is a decision in
/// `run()` — read lib.rs the same way `open`'s wiring is read above.
#[test]
fn run_prints_the_environment_diagnostics_first() {
    let lib = include_str!("../lib.rs");
    let main_module = lib
        .split_once("#[cfg(test)]")
        .expect("lib.rs has a test module")
        .0;
    let (before_diagnostics, after_diagnostics) = main_module
        .split_once("browser::log_environment_diagnostics()")
        .expect(
            "run() no longer prints the browser environment diagnostics — the \
                 four lines that say which first-paint fact bit are gone",
        );
    // First word, not a footnote: the line has to come before the
    // builder is assembled, so a boot that dies in `build()` — the
    // single-instance exit — has already said which fact it died on.
    assert!(
            before_diagnostics.contains("pub fn run()"),
            "the diagnostics call site is no longer inside run() — it must run in            the app's own process, not a helper's"
        );
    assert!(
            !after_diagnostics.contains("browser::log_environment_diagnostics()"),
            "lib.rs prints the browser environment diagnostics more than once —            one line per fact is the whole shape"
        );
    // And the function covers the four facts the first-paint question is
    // actually about. The labels are the read-out's contract; the smoke
    // script documents them verbatim.
    let source = production_source();
    let main_source = &source;
    for label in [
        "session bus",
        "webkit sandbox",
        "dmabuf rendering",
        "display backend",
    ] {
        assert!(
                main_source.contains(&format!("\"{label}\",")),
                "log_environment_diagnostics no longer prints {label:?} — one of the            four first-paint facts went dark"
            );
    }
}

/// The zero-bounds refusal is the message the pane shows the user, so
/// its wording is load-bearing: it must name the condition (no size
/// yet) and the recovery (it opens when the pane is measured). This is
/// a freeze of the sentence, in the same shape as the embed-wiring
/// freeze above — a wording change is a decision, not a typo.
#[test]
fn the_zero_bounds_refusal_names_the_recovery() {
    let source = production_source();
    let body = source
        .split_once("pub fn open(")
        .expect("this file defines `open`")
        .1
        .split_once("pub fn navigate(")
        .expect("`open` is followed by `navigate`")
        .0;
    assert!(
            body.contains("browser pane has no size yet"),
            "`open` no longer names the zero-bounds condition — the pane would            show a bare error with no story"
        );
    assert!(
            body.contains("the page will open the moment the pane is measured"),
            "`open`'s refusal no longer names the recovery — the user would know            nothing opened yet but not why"
        );
}

/// The smoke mode is a decision site in lib.rs's setup closure, which no
/// predicate test can reach. This freeze holds four claims: it is gated
/// on the environment variable (default URL, not the real one), it
/// reports and exits through the AppHandle, it has a timeout that fails
/// rather than hangs, and it returns before the engine launch — the
/// smoke run is about the embed path and nothing else.
#[test]
fn the_smoke_mode_is_gated_reports_and_never_starts_the_engine() {
    let lib = include_str!("../lib.rs");
    let main_module = lib
        .split_once("#[cfg(test)]")
        .expect("lib.rs has a test module")
        .0;
    let setup = main_module
        .split_once(".setup(move |app| {")
        .expect("lib.rs still configures the builder with a setup closure")
        .1
        .split_once(".invoke_handler(")
        .expect("the setup closure ends at the invoke handler")
        .0;
    // Gated, and gated on nothing else: the mode engages only when the
    // variable is set, and an empty value still has a default page so a
    // bare `CODEIFY_EMBED_SMOKE=1` is a working invocation.
    assert!(
        setup.contains("CODEIFY_EMBED_SMOKE"),
        "the smoke mode lost its environment gate — it would run on every            normal launch"
    );
    assert!(
            setup.contains("https://example.com/"),
            "the smoke mode lost its default URL — a bare engagement should            still have a page to seat"
        );
    // It reports and exits through the AppHandle: painted → 0, timeout
    // → 1, both said out loud on stdout where the harness reads them.
    assert!(
            setup.contains("listen_any(browser::SMOKE_PAINTED_EVENT"),
            "the smoke listener is no longer on the AppHandle — the paint            announcement would have nowhere to land"
        );
    assert!(
            setup.contains("embed-smoke: painted") && setup.contains("smoke_exit(0)"),
            "the smoke no longer reports the paint and exits zero — the harness            would wait out its timeout on a success"
        );
    // Three deliverables now — a paint, the page's report, and the page
    // read back through the bridge — so the timeout sentence names all
    // three. The assertion is on the shape rather than the full line,
    // because a line that grows a fourth deliverable should not fail here
    // for the wrong reason.
    assert!(
            setup.contains("embed-smoke: FAILED no first paint, page report or page")
                && setup.contains("read within 20s")
                && setup.contains("smoke_exit(1)"),
            "the smoke lost its timeout failure — a page that never paints,            never reports what it is showing, or never answers the bridge,            would hang the run instead of failing it"
        );
    // And both paths exit for real. `AppHandle::exit` only *requests* an
    // exit in tauri 2.11.6 (it raises RunEvent::ExitRequested/Exit and
    // unwinds), so the requested code never becomes the process status —
    // a failed first paint would exit 0 and read as a pass to anything
    // looking at the status. This is the assertion that holds the
    // difference, and it is the reason smoke_exit exists.
    let helper = main_module
        .split_once("fn smoke_exit(")
        .expect("lib.rs defines `smoke_exit`")
        .1
        .split_once("\n}\n")
        .expect("`smoke_exit` is a plain function")
        .0;
    for needed in ["std::process::exit", "flush()"] {
        assert!(
                helper.contains(needed),
                "smoke_exit no longer mentions {needed} — the smoke would end with a            status of 0 whether the page painted or not"
            );
    }
    // And the early return sits between the smoke wiring and the engine
    // launch: the smoke path never starts an engine, and a reorder that
    // lets it would be a second variable in the run.
    let (smoke_path, engine_path) = setup
            .split_once("return Ok(());")
            .expect(
                "the smoke block no longer returns early from setup — it would fall            through into the engine launch",
            );
    assert!(
        smoke_path.contains("CODEIFY_EMBED_SMOKE"),
        "the smoke block's early return moved above the smoke wiring itself"
    );
    assert!(
            engine_path.contains("launch_engine"),
            "the setup closure no longer reaches the engine launch after the            smoke early return — the smoke path is no longer a prefix of it"
        );
}

/// And the smoke page's paint detector is the one string both the shell
/// and its test agree on: two animation frames (the second proves the
/// compositor consumed a frame), then the mark. If the marker drifts
/// from the event `lib.rs` listens for, the smoke fails at 20s with
/// nothing to diagnose — so the two ends are frozen together here.
#[test]
fn the_paint_script_announces_the_event_the_lib_listens_for() {
    let lib = include_str!("../lib.rs");
    assert!(
        lib.contains("listen_any(browser::SMOKE_PAINTED_EVENT"),
        "lib.rs no longer listens for the paint event by name"
    );
    assert_eq!(
        SMOKE_PAINT_MARKER, SMOKE_PAINTED_EVENT,
        "the title the page sets and the event the app listens for are one \
             string on purpose — two spellings are two things to drift"
    );
    assert!(
        SMOKE_PAINT_SCRIPT.contains(SMOKE_PAINT_MARKER),
        "SMOKE_PAINT_SCRIPT no longer sets the marker lib.rs's listener \
             announces — the smoke would time out at 20s with nothing to diagnose"
    );
    assert!(
        SMOKE_PAINT_SCRIPT.contains("requestAnimationFrame"),
        "SMOKE_PAINT_SCRIPT no longer asks for animation frames — 'document \
             loaded' does not prove a frame was composited, which is the whole \
             reason the script exists"
    );
    // And the page may not speak Tauri. This is the assertion that
    // holds the correction: the first version of this script had the
    // page invoke `plugin:event|emit_to` itself, reasoning that a
    // one-way event is harmless. It is ACL-governed like any other
    // command, the `browser-*` capability set is empty by design, and
    // the invoke was refused — so the smoke could only ever fail, and
    // read as "first paint is broken" on a machine where it was fine.
    // A page that needs no permission (its own title) is the only
    // channel this may use.
    assert!(
        !SMOKE_PAINT_SCRIPT.contains("invoke(")
            && !SMOKE_PAINT_SCRIPT.contains("__TAURI_INTERNALS__"),
        "SMOKE_PAINT_SCRIPT speaks Tauri again — the page holds the empty \
             browser-* capability set, so an invoke is refused and the paint \
             announcement never leaves the page. Use the title mark instead."
    );
    // The shell side of that mark: smoke_mode watches the title and
    // re-announces, which is the one place in the path that talks Tauri.
    let source = production_source();
    let smoke = source
        .split_once("pub fn smoke_mode(")
        .expect("this file defines `smoke_mode`")
        .1
        .split_once("/// The script the smoke page runs")
        .expect("`SMOKE_PAINT_SCRIPT` follows `smoke_mode`")
        .0;
    for needed in [
        "on_document_title_changed",
        "SMOKE_PAINT_MARKER",
        "SMOKE_PAINTED_EVENT",
        "SMOKE_PROBE_SCRIPT",
        "SMOKE_PROBE_PREFIX",
        "SMOKE_PROBE_PAINTED_MARKER",
        "SMOKE_REPORT_EVENT",
    ] {
        assert!(
            smoke.contains(needed),
            "smoke_mode no longer mentions {needed} — the page's mark is \
                 watched nowhere, so a paint would go unannounced"
        );
    }
}

/// The run ends when the page has **both** painted and reported.
///
/// Two facts here and one shape. The paint event and the report event are
/// separate, because the two arrive on their own schedules and the first
/// report deliberately waits 800ms to let the paint markers clear. And
/// `lib.rs` waits for both rather than for the first: a run that ended when
/// the paint marker landed (~400ms) ended before the first report was even
/// due, and printed "no-report" about a page it had not asked — a verdict
/// about the harness's own timing, dressed as one about the site. That is
/// the failure this harness exists to prevent, and it is worth a test of
/// its own because the alternative looks fine in every other run.
///
/// The third deliverable is the bridge's: the same page read back through
/// the channel the AI uses. It is required by default for the same
/// reason — a run that quietly stopped measuring the bridge is how a
/// bridge that cannot reach a page becomes a passing build.
#[test]
fn the_smoke_run_waits_for_a_paint_and_a_report() {
    let lib = include_str!("../lib.rs");
    for needed in [
        "browser::SMOKE_PAINTED_EVENT",
        "browser::SMOKE_REPORT_EVENT",
        "webview_bridge::SMOKE_BRIDGE_EVENT",
    ] {
        assert!(
            lib.contains(needed),
            "lib.rs no longer listens for {needed} by name — the run would \
                 end on one deliverable and never hear the other"
        );
    }
    // All three flags, and the exit is gated on all of them. One flag and
    // the run goes on whichever lands first, which is the bug.
    let start = lib
        .find("browser::SMOKE_PAINTED_EVENT")
        .expect("the paint listener is in setup");
    let block = &lib[start..];
    let end = block
        .find("tokio::time::sleep")
        .expect("the timeout follows the listeners");
    let listeners = &block[..end];
    for leg in 0..3 {
        assert!(
            listeners.contains(&format!("seen[{leg}] = true")),
            "the smoke no longer tracks deliverable {leg} — it tracks fewer, \
                 and the last one is what gets dropped"
        );
    }
    // `all` rather than a pairwise `&&`: with three legs the pairwise form
    // is three nearly-identical conditions and one of them is always the
    // wrong one to have forgotten.
    assert!(
        listeners.matches("seen.iter().all(|leg| *leg)").count() >= 3,
        "the smoke exits on one deliverable rather than on all of them: a run \
             that ends on the paint marker ends before the first report is due, \
             and reports a page it never asked about"
    );
}

/// The probe asks the page what it is showing, and still says nothing to
/// the shell itself.
///
/// Three properties, each of which was learned the hard way by the paint
/// script beside it. The page holds the empty `browser-*` capability set,
/// so an `invoke` is refused and a probe built on one could only fail; the
/// channel is the page's own title, which needs no permission. The report
/// has to carry the *facts* that separate a working site from a refusal
/// page, because a paint smoke passes all three of them. And it is
/// smoke-only: it overwrites the document title to carry its payload, which
/// is destructive to a tab's name — the same channel the tab strip's live
/// title uses — so it belongs on the throwaway webview and nowhere else.
#[test]
fn the_probe_script_asks_the_page_and_says_nothing_to_the_shell() {
    assert!(
        SMOKE_PROBE_SCRIPT.contains(SMOKE_PROBE_PREFIX.trim()),
        "SMOKE_PROBE_SCRIPT no longer announces behind SMOKE_PROBE_PREFIX — \
             the shell would not recognise a report, or would recognise a \
             paint mark as one"
    );
    assert!(
        !SMOKE_PROBE_SCRIPT.contains("invoke(")
            && !SMOKE_PROBE_SCRIPT.contains("__TAURI_INTERNALS__"),
        "SMOKE_PROBE_SCRIPT speaks Tauri — the page holds the empty \
             browser-* capability set, so an invoke is refused and the report \
             never leaves the page. Use the title, as the paint script does."
    );
    // The facts, not decoration: each one is a way a real site differs in
    // an embedded view, and dropping one silently weakens every verdict
    // built on it.
    for needed in [
        "canPlayType", // the codec story, which is a fact to report
        "querySelectorAll(\"video\")",
        "querySelectorAll(\"audio\")",
        "querySelectorAll(\"iframe\")", // where this probe's reach ends
        "innerText",                    // a page that rendered nothing says so here
        "may not be secure",            // Google's embedded-OAuth refusal, verbatim
        "setTimeout(announce",          // late content, and a page that repaints us
        SMOKE_PROBE_PAINTED_MARKER,     // the second witness of paint
        "requestAnimationFrame",        // two frames, as the paint script does
    ] {
        assert!(
            SMOKE_PROBE_SCRIPT.contains(needed),
            "SMOKE_PROBE_SCRIPT no longer asks for {needed:?} — the report \
                 it produces cannot tell a working site from a broken one"
        );
    }

    // ── the mechanism, for the times the page is blank ──
    //
    // A YouTube watch page measured at zero characters, no title and no
    // media, and every fact above it was equally true of a page that
    // worked. These five are what make the finding actionable: which
    // subresources were tried (`rp`), which failed (`x`), what the page
    // threw (`j`), what it logged (`c9`), and who it thinks it is (`ua`) —
    // the last because "the embed sends a user agent no site wants" was one
    // of the standing hypotheses and this is the fact that settles it.
    for needed in [
        "addEventListener(\"error\"",     // failed subresources *and* throws
        "unhandledrejection",             // a modern site fails here, silently
        "console[level]",                 // warn/error, before any page code
        "getEntriesByType(\"resource\")", // attempted, not merely failed
        "navigator.userAgent",
    ] {
        assert!(
            SMOKE_PROBE_SCRIPT.contains(needed),
            "SMOKE_PROBE_SCRIPT no longer captures {needed:?} — a page that \
                 renders nothing comes back with a symptom and no mechanism, and \
                 a symptom sends the reader to fix the wrong layer"
        );
    }
    // `useCapture` is the difference between hearing about a script that
    // 404'd and hearing nothing: a resource error fires on the *element*,
    // and only the capture phase sees it.
    assert!(
        SMOKE_PROBE_SCRIPT.contains("}, true);"),
        "the page's error listener has lost capture — a failed <script> or \
             <img> fires on the element, so a bubbling listener on window hears \
             nothing and every blank page looks unexplained"
    );
    // The race that cost a run, frozen: the probe writes the same title
    // channel the paint script writes, so a report announced *at load* can
    // overwrite the paint marker before the shell is told. The first report
    // has to wait the markers out, and there has to be a second witness.
    assert!(
        !SMOKE_PROBE_SCRIPT.contains("load\", announce")
            && !SMOKE_PROBE_SCRIPT.contains("complete\") announce"),
        "the probe announces synchronously again — it will overwrite the \
             paint marker before the shell sees it, and a page that painted \
             will be reported as never having painted"
    );
    // The channel is lossy during load — a second run measured that, and
    // the evidence is in the doc above. A witness that announces once, in
    // the frame it was earned, is a witness the shell will usually not
    // hear, and the failure reads as "first paint is broken" on a machine
    // where painting is fine: the exact mistake the paint script's own
    // history records. It must be re-asserted after load, several times.
    let witness_repeats = SMOKE_PROBE_SCRIPT
        .matches(SMOKE_PROBE_PAINTED_MARKER)
        .count();
    assert!(
        witness_repeats >= 1 && SMOKE_PROBE_SCRIPT.contains("[0, 400, 1200, 3000]"),
        "the paint witness announces once again — title changes made during \
             page load are not delivered on this WebKit, so the marker is set \
             and never heard, and every run reports a page that plainly painted \
             as never having painted at all"
    );
    // And the report must describe the page, not the probe: the title is
    // read once, before the probe takes it over, because a live read
    // reports the previous payload back as the page's own title.
    assert!(
        SMOKE_PROBE_SCRIPT.contains("let pageTitle")
            && SMOKE_PROBE_SCRIPT.contains("t: String(pageTitle || \"\")"),
        "the probe reports its own payload as the page's title — a fact \
             about the harness presented as a fact about the site"
    );
    // And the reader agrees on the one spelling, across the language
    // boundary: a prefix that drifts turns every report into a line the
    // smoke does not recognise, and the run looks like a page that said
    // nothing.
    let reader = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent")
        .join("scripts/embed_smoke.py");
    let text = std::fs::read_to_string(&reader)
        .unwrap_or_else(|e| panic!("{} must exist: {e}", reader.display()));
    assert!(
        text.contains(SMOKE_REPORT_LINE),
        "scripts/embed_smoke.py does not name the line this module prints — \
             every report would be printed and never read"
    );
}

/// The probe names the request that has not come back.
///
/// Every other field in the report counts something that **happened**:
/// resources completed, scripts present, errors caught. None of them can
/// see a request still in flight, and that is the field a stuck page most
/// needs — measured on youtube.com, a page at 10s with 92 completed
/// subresources, still `loading`, and a report that could say nothing about
/// what it was waiting for. Resource timing cannot close the gap, twice
/// over: an entry exists only once a response has *finished*, and a
/// cross-origin response with no `Timing-Allow-Origin` produces no entry at
/// all. So this test holds three things: the two request channels that can
/// be observed at the call site, the media element that neither of them
/// sees, and the rule that a request with no measured start reports no age
/// rather than a zero.
#[test]
fn the_probe_names_the_request_that_has_not_come_back() {
    for needed in [
        "window.fetch",       // the fetch channel, patched at the call
        "XMLHttpRequest",     // …and the one before it
        "loadstart",          // a <video> that is waiting
        "networkState === 2", // …or one already loading when asked
        "q: outstanding()",   // the field, in the report
        "le: loadEventEnd()", // the document's own load timestamp
    ] {
        assert!(
            SMOKE_PROBE_SCRIPT.contains(needed),
            "SMOKE_PROBE_SCRIPT no longer contains {needed:?} — the report \
                 counts what arrived and is blind to what has not, so a page \
                 waiting on one request reads exactly like a page waiting on \
                 none"
        );
    }
    // A zero age would be a lie told in the most plausible-looking place:
    // "started 0ms ago" for a request that was found in flight and has
    // been waiting since before the probe looked. `-1` means the page
    // cannot say, which is the truth.
    assert!(
        SMOKE_PROBE_SCRIPT.contains("entry.at === null ? -1"),
        "the probe reports a made-up age for a request it did not see \
             leaving — a page is then described as 'waiting 0ms' for something \
             it has been waiting about for seconds"
    );
    // …and a `blob:` URL keeps its scheme, because dropping it turns a
    // Media Source handle into a page that has been asked to fetch a URL.
    // Measured: youtube.com, whose player names one of these.
    assert!(
        SMOKE_PROBE_SCRIPT.contains("blob:") && SMOKE_PROBE_SCRIPT.contains("MEDIASOURCE"),
        "the probe shortens a non-http URL to host+path — a blob: Media \
             Source URL then reads as a youtube.com path, which names a request \
             that was never made"
    );
    // The channel is finite, and this row width is the measured cost of
    // fitting in it. A report rides in a document title as percent-encoded
    // JSON, and past roughly a kilobyte it comes back cut off mid-string —
    // which the reader cannot tell from a page that said nothing. Measured:
    // a youtube.com report carrying three outstanding `videoplayback`
    // requests, with their signed query strings, lost its *whole* report to
    // a URL the page had already been told not to print. So no query, and
    // a row cap: the host and path name the request, and the cap is three.
    assert!(
        !SMOKE_PROBE_SCRIPT.contains("parsed.search.slice(")
            && SMOKE_PROBE_SCRIPT.contains("PENDING_CAP = 3"),
        "the report grew again — the outstanding rows carry query strings \
             they do not need, and a payload past a document title's width is \
             read as a page that said nothing at all"
    );
    // And the harness's own traffic must never appear in the page's
    // findings. Measured: a run answered "waiting on
    // `codify-bridge://reply/smoke0001/0/0/…`" — the page replying to the
    // smoke's own question, named as youtube.com's outstanding request. The
    // two ends of that scheme are pinned together here, because a renamed
    // bridge would put the harness back in the middle of the measurement
    // with nothing failing.
    assert!(
        SMOKE_PROBE_SCRIPT.contains("codify-bridge:"),
        "the probe records the page's own bridge replies as outstanding \
             requests — a run then names its own measurement as the site"
    );
    assert_eq!(
        format!("{}:", crate::webview_bridge::BRIDGE_SCHEME),
        "codify-bridge:",
        "the probe excludes one scheme and the bridge serves another, so \
             the harness's own replies come back into the findings"
    );
    // And the one field a later report may drop. The user agent is set on
    // the builder before the page exists, so every announcement repeats a
    // string that cannot change — 133 encoded characters of a channel
    // measured at about 980, on a page heavy enough to overflow it. The
    // reader keeps the first one; what it must *not* do is extend the same
    // licence to a field about now.
    assert!(
        SMOKE_PROBE_SCRIPT.contains("delete payload.ua"),
        "the probe is sending its user agent on every announcement again — \
             a heavy page overflows the title channel and loses its whole \
             report, stall data included, to a string the run already printed"
    );
    // …and it has to *measure* rather than hope. No per-field cap predicts
    // the total for a page nobody has run before, and the consequence of
    // getting it wrong is a report read as silence, so the payload checks
    // its own encoded size and gives up the least useful field it still
    // has. The order is the claim: `q` is dropped after `c9`, `x` and `j`,
    // because a console warning is not a verdict and an outstanding
    // request is.
    assert!(
        SMOKE_PROBE_SCRIPT.contains("CHANNEL_LIMIT = 940")
            && SMOKE_PROBE_SCRIPT
                .contains(r#"const SPENDABLE = ["c9", "x", "j", "lz", "q", "ua"];"#)
            && SMOKE_PROBE_SCRIPT.contains("payload.df = dropped.join"),
        "the report no longer measures itself against the channel's width — \
             an overflowing report is read as a page that said nothing, and the \
             reader cannot tell those apart"
    );
    // The patches are observational: the page's own call is what returns,
    // and a probe that swallowed a response would turn a measurement into
    // a fault it caused.
    assert!(
        SMOKE_PROBE_SCRIPT.contains("originalFetch.call(this, input, init)"),
        "the fetch patch no longer returns the page's own promise — a probe \
             that changed what the page receives is a fault of the harness, and \
             it would be reported as a fault of the site"
    );
    // And the reader has to know the field exists, or it is carried in a
    // title nobody reads a key of.
    let reader = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri has a parent")
        .join("scripts/embed_smoke.py");
    let text = std::fs::read_to_string(&reader)
        .unwrap_or_else(|e| panic!("{} must exist: {e}", reader.display()));
    assert!(
        text.contains("classify_stall"),
        "scripts/embed_smoke.py has no stall verdict — the page names the \
             outstanding request and the run has no word for it, so the fact is \
             in the transcript and absent from the verdict"
    );
}

/// The user agent a page is given, and what is *not* in it.
///
/// The two are one test because the second is the point: a string can be
/// made honest by accident and go back to lying the next time somebody
/// edits it, and "honest" here has a precise shape — no Safari release,
/// no `Version/` token, and the product named as what it is.
#[test]
fn a_page_advertises_codify_and_not_a_2022_release_of_safari() {
    let ua = page_user_agent();
    assert!(
        ua.contains(&format!("Codify/{}", env!("CARGO_PKG_VERSION"))),
        "the page must name the product and its real version: {ua}"
    );
    assert!(
        ua.contains("AppleWebKit/") && ua.contains("KHTML, like Gecko"),
        "the engine token stays — it is genuinely WebKit, and every \
             parser already looks for it: {ua}"
    );
    // The lie itself. wry's default claims Safari 15.4 (2022) and Apple's
    // build number for it, on an engine that is WebKitGTK.
    assert!(
        !ua.contains("Safari"),
        "the page must not claim to be Safari: {ua}"
    );
    assert!(
        !ua.contains("Version/"),
        "the page must not carry a browser release token it does not \
             have: {ua}"
    );
    assert!(
        ua.starts_with("Mozilla/5.0"),
        "sites parse this far before anything else: {ua}"
    );
}

/// The override exists to be measured with, and it obeys the two rules
/// that make an override safe: it is exactly what it says, and nothing
/// else about the default moves.
#[test]
fn an_override_replaces_the_whole_string_and_nothing_else() {
    let honest = page_user_agent_from(None);
    assert!(honest.contains("Codify/"), "the default names the product");
    // Three presets, exactly as `make smoke-embed --user-agent` sends them.
    for pretend in [
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/60.5 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        ] {
            assert_eq!(
                page_user_agent_from(Some(pretend)),
                pretend,
                "an override must reach the page unchanged — a harness that \
                 could not reproduce the exact string it is comparing against \
                 would not be measuring anything"
            );
        }
    // Whitespace is trimmed and emptiness falls back, so a harness that
    // passes an unset variable gets the honest string rather than a blank
    // one that every site would treat as a bot.
    assert_eq!(page_user_agent_from(Some("  ")), honest);
    assert_eq!(page_user_agent_from(Some("")), honest);
    assert_eq!(page_user_agent_from(Some("   honest-ish  ")), "honest-ish");
}

#[test]
fn the_override_is_reachable_from_the_environment() {
    // The plumbing, not the behaviour: `page_user_agent` is what every page
    // builder calls, and this is the only thing that makes the env var
    // mean anything.
    let source = production_source();
    let code: String = source
        .lines()
        .map(|line| match line.find("//") {
            Some(at) => &line[..at],
            None => line,
        })
        .collect::<Vec<_>>()
        .join("\n");
    assert!(code.contains("std::env::var(UA_ENV)"));
    assert!(
        code.contains(".user_agent(&page_user_agent())"),
        "both page builders must go through the resolver, not one of them \
             pinning the string"
    );
}

/// Every page is given it — the two places a page is built, and nowhere
/// else, because the app's own UI is not a website and is not pretending
/// to be one.
#[test]
fn every_browser_page_is_given_the_user_agent() {
    let source = production_source();
    assert_eq!(
        source.matches(".user_agent(&page_user_agent())").count(),
        2,
        "a page can be built from two places — `open` and the smoke \
             probe — and both must carry the same honest string"
    );
    assert!(
        !source.contains("user_agent_override"),
        "an app-wide override would put the string on the app's own UI \
             too, which is not a website"
    );
}

/// The page layer is the one container that can hold a page *inside* a
/// pane, and it exists because of a platform fact rather than a taste.
///
/// Tauri builds **every** webview into the window's default `gtk::Box`
/// (`tauri-runtime-wry`'s `WebviewKind::WindowChild => build_gtk(default_vbox())`),
/// and wry's box branch packs a child expand-and-fill **without reading the
/// bounds it was handed**. So a page takes a share of the window — half of
/// Codify, then all of it — while the UI reports the right rectangle and the
/// toolkit discards it. A `gtk::Overlay` is the standard container that
/// overlaps its children, which is what a pane needs. This holds the three
/// things that make the fix work: the overlay, the fixed inside it, and the
/// **ordering**, because an overlay has exactly one main child and a page
/// added before the layer would be adopted into a slot that is taken.
#[test]
fn the_page_layer_is_the_container_that_can_overlap() {
    let source = production_source();
    let layer = page_layer_source();
    for needed in [
        "gtk::Overlay::new()",
        "gtk::Fixed::new()",
        "overlay.add_overlay(&fixed)",
        "pack_start(&overlay, true, true, 0)",
    ] {
        assert!(
            layer.contains(needed),
            "the page layer no longer builds {needed:?} — a page packed into \
                 the window's GtkBox takes a share of the window instead of the \
                 pane's rectangle, which is the bug a user found"
        );
    }
    let open = source
        .split_once("pub fn open(")
        .expect("this file defines `open`")
        .1
        .split_once("/// The `User-Agent` a browser page is given.")
        .expect("`open` is followed by the user agent")
        .0;
    let prepared = open
        .find("page_layer::prepare(&window)")
        .expect("`open` must ready the layer before creating the page");
    let created = open
        .find(".add_child(")
        .expect("`open` still creates the page with add_child");
    let adopted = open
        .find("page_layer::adopt(")
        .expect("`open` must move the new page into the layer");
    assert!(
        prepared < created,
        "the page layer is built after the page again — the overlay would \
             have to adopt two children, the second is dropped with a warning, \
             and a page dropped that way has no parent to be placed in"
    );
    assert!(
        created < adopted,
        "the layer adopts a page that has not been created yet — there is \
             nothing to move"
    );
}

/// The one test that builds real GTK widgets, and the reason there is only one.
///
/// GTK may be initialised from a single thread per process and `cargo test` gives
/// every test a thread of its own, so a second test calling `gtk::init` panics
/// ("Attempted to initialize GTK from two different threads"). The real-widget
/// checks are functions, and this runs them in order: a failure names which. Needs
/// a display, and says so rather than passing without one.
#[test]
fn the_page_layer_holds_up_on_real_widgets() {
    gtk::init().expect("this test builds real GTK widgets and needs a display");
    the_layer_lets_input_through_to_the_app_beneath_it();
    a_page_taking_the_focus_is_reported_and_nothing_else_is();
}

/// The layer must not take the app's input.
///
/// The tests around it read this file's source, which is how a layer that
/// painted perfectly and answered nothing shipped: every string they look
/// for was there. This builds the real widgets and asks GDK — the thing that
/// routes a click — whether the layer's input window lets it through.
fn the_layer_lets_input_through_to_the_app_beneath_it() {
    use gtk::prelude::*;
    let vbox = gtk::Box::new(gtk::Orientation::Vertical, 0);
    vbox.pack_start(&gtk::Button::with_label("the app"), true, true, 0);
    let (overlay, fixed) = page_layer::assemble(&vbox);
    // A page, so the fixed has something in it to keep receiving input.
    let page = gtk::Button::with_label("a page");
    page.set_size_request(120, 80);
    fixed.put(&page, 40, 40);

    let window = gtk::OffscreenWindow::new();
    window.set_default_size(400, 300);
    window.add(&vbox);
    window.show_all();
    while gtk::events_pending() {
        gtk::main_iteration();
    }

    assert!(
        overlay.is_overlay_pass_through(&fixed),
        "the page layer is not pass-through, so its input window covers the \
             whole app and the window stops answering the moment a page exists"
    );
    let gdk_window = overlay.window().expect("the overlay is realised");
    let covering: Vec<_> = gdk_window
        .children()
        .into_iter()
        .filter(|w| w.width() >= 400 && w.height() >= 300 && !w.is_pass_through())
        .collect();
    // The overlay's own main child is one full-size window that must take
    // input (it is the app). The layer's is the other, and must not.
    assert_eq!(
        covering.len(),
        1,
        "exactly one full-size window may take input — the app's; a second \
             is the layer sitting over it"
    );
}

/// The window is told when a page takes the keyboard, and only then.
///
/// Real widgets and a real window, no app: [`page_layer::watch_focus`] takes the
/// sink as a closure for exactly this. The page's widget is renamed to its label
/// by `adopt` and the toolkit's focus is always a leaf, so both shapes are here —
/// the focus *is* the named widget, and the focus is something inside it.
fn a_page_taking_the_focus_is_reported_and_nothing_else_is() {
    use gtk::prelude::*;
    use std::cell::RefCell;
    use std::rc::Rc;

    let app = gtk::Entry::new();
    // A page whose widget is a wrapper, with the focusable thing inside it.
    let wrapper = gtk::Box::new(gtk::Orientation::Vertical, 0);
    wrapper.set_widget_name(&format!("{LABEL_PREFIX}tab-7"));
    let inside = gtk::Entry::new();
    wrapper.pack_start(&inside, true, true, 0);
    // A page whose widget is itself what takes the focus.
    let direct = gtk::Entry::new();
    direct.set_widget_name(&format!("{LABEL_PREFIX}tab-8"));
    // Something that is neither the app nor a page, with a name that merely
    // contains the prefix: only a *prefix* is a page.
    let lookalike = gtk::Entry::new();
    lookalike.set_widget_name(&format!("not-a-{LABEL_PREFIX}tab-9"));

    let vbox = gtk::Box::new(gtk::Orientation::Vertical, 0);
    for child in [
        app.upcast_ref::<gtk::Widget>(),
        wrapper.upcast_ref(),
        direct.upcast_ref(),
        lookalike.upcast_ref(),
    ] {
        vbox.pack_start(child, true, true, 0);
    }
    let window = gtk::OffscreenWindow::new();
    window.set_default_size(400, 300);
    window.add(&vbox);
    let told: Rc<RefCell<Vec<String>>> = Rc::default();
    let sink = told.clone();
    page_layer::watch_focus(&window, move |tab_id| sink.borrow_mut().push(tab_id));
    window.show_all();
    while gtk::events_pending() {
        gtk::main_iteration();
    }

    app.grab_focus();
    assert!(
        told.borrow().is_empty(),
        "the app's own widget taking the focus was announced as a page: {:?}",
        told.borrow()
    );
    inside.grab_focus();
    assert_eq!(
        *told.borrow(),
        ["tab-7"],
        "a widget inside a page took the focus and the page was not named — the \
         toolkit's focus is a leaf, so the label has to be looked for in its ancestry"
    );
    app.grab_focus();
    assert_eq!(
        told.borrow().len(),
        1,
        "the focus leaving a page was announced — that fact belongs to the app's \
         own events, and a second voice about it is a second opinion"
    );
    direct.grab_focus();
    assert_eq!(
        *told.borrow(),
        ["tab-7", "tab-8"],
        "the page's own widget took the focus and was not named"
    );
    lookalike.grab_focus();
    assert_eq!(
        told.borrow().len(),
        2,
        "a widget whose name only *contains* the page prefix was taken for a page"
    );
}

/// The layer is not conditional. Codify targets Linux only, and a `cfg` here would
/// bring back the second definition — a stub that places pages with a `set_bounds`
/// this toolkit discards — for a platform nothing builds for.
///
/// `gtk` is a plain dependency for the same reason: gated to a list of targets it
/// would be a dependency the code cannot compile without, hidden behind a condition
/// no build ever fails.
#[test]
fn the_layer_and_its_gtk_dependency_are_unconditional() {
    let source = production_source();
    let production = &source;
    assert!(
        !production.contains("target_os"),
        "browser.rs gates something on the operating system again — Codify is \
             Linux-only, and a second definition of the page layer is a second \
             thing to keep right on a platform nothing builds for"
    );
    assert!(
        !production.contains("#[cfg(not(any("),
        "the page layer has a second definition again"
    );
    let cargo =
        std::fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml"))
            .expect("the crate's Cargo.toml is readable");
    let dependencies = cargo
        .split_once("\n[dependencies]\n")
        .expect("Cargo.toml has a [dependencies] table")
        .1
        .split_once("\n[")
        .expect("another table follows [dependencies]")
        .0;
    assert!(
        dependencies.contains("\ngtk = "),
        "`gtk` is no longer a plain dependency — the page layer uses it on every build"
    );
    assert!(
        !cargo.contains("target_os"),
        "Cargo.toml gates something on the operating system again"
    );
}

/// Placement is this module's job, not the toolkit's. Both at once is a fight.
#[test]
fn placement_is_driven_by_this_module_and_never_by_the_toolkit() {
    let layer = page_layer_source();
    assert!(
        !layer.contains("set_bounds"),
        "the layer calls `set_bounds`, which wry honours only for a \
             webview it created in a GtkFixed — ours is created in the window's \
             box, so the call is a silent no-op and the page stays wherever the \
             box put it"
    );
    assert!(
        layer.contains("fixed.move_(&widget") && layer.contains("set_size_request"),
        "the layer no longer moves and sizes the page's widget itself \
             — nothing else will"
    );
}

/// A page that fills the window passes every other leg of this smoke.
///
/// That is the whole reason the smoke now asks for a rectangle that a
/// **discarded** request cannot imitate: it used to ask for the window's
/// own size at the origin, which is exactly what "the toolkit ignored the
/// bounds and packed the page" produces. Green either way, for as long as
/// the page was covering the app.
#[test]
fn the_smoke_seats_its_page_somewhere_a_discarded_request_cannot_imitate() {
    // A constant, read at run time on purpose: this is a test that fails by name, not a build that stops.
    let page = std::hint::black_box(SMOKE_PAGE);
    assert!(
        !(page.x == 0.0 && page.y == 0.0),
        "the smoke seats its page at the origin again — a toolkit that \
             discards the request and fills the window produces exactly that, \
             which is how this stayed green while a user watched it happen"
    );
    assert!(
        page.width < 1280.0 && page.height < 800.0,
        "the smoke asks for a rectangle as large as the window, which a \
             discarded request imitates perfectly"
    );
    let source = production_source();
    assert!(
        source.contains("page_layer::geometry(&geometry_sink, SMOKE_PAGE_LABEL)"),
        "the smoke no longer checks where its page actually ended up — every \
             other leg measures the page's *content*, and a page covering the \
             whole window satisfies all of them"
    );
    let smoke = source
        .split_once("pub fn smoke_mode(")
        .expect("this file defines `smoke_mode`")
        .1
        .split_once("/// The script the smoke page runs")
        .expect("`smoke_mode` is followed by the paint script")
        .0;
    assert!(
        !smoke.contains("run_on_main_thread"),
        "the smoke asks for the main thread itself again before it reads its \
             page's geometry — a measurement taken there measures the code that \
             already worked, and this smoke is the leg that has to notice a \
             widget call arriving on a tokio worker"
    );
}

/// GTK talks to one thread, and the commands that build a page do not run
/// on it.
///
/// This is not a hypothetical: `codify_browser_open` is an `async` command,
/// so Tauri runs it on a tokio worker, and the first click on a browser tab
/// in a real window ended on GTK's own assert — "GTK may only be used from
/// the main thread" — before the page existed. The smoke missed it because
/// the smoke seats its page from `setup`, on the main thread, and a user's
/// click does not. So the freeze is here: every door into the layer asks
/// GTK's question before it touches a widget, and the question is the one
/// the toolkit's own assert asks rather than a guess about thread ids.
#[test]
fn the_layer_touches_gtk_only_from_the_thread_gtk_allows() {
    let linux_half = page_layer_source();
    assert!(
        linux_half.contains("gtk::glib::MainContext::default().is_owner()"),
        "the layer no longer asks whether this is the thread GTK will answer \
             to. tao initialises GTK on the main thread and `gtk::init` acquires \
             and leaks the default main context, so its owner is the only thread \
             whose widget calls are legal — and `open` runs on a tokio worker"
    );
    // Each door, taken from its own `fn` to the next one, so a door that
    // stopped hopping is named rather than averaged away.
    for door in [
        "prepare",
        "adopt",
        "place",
        "release",
        "geometry",
        "take_focus",
    ] {
        let rest = linux_half
            .split_once(&format!("pub fn {door}("))
            .unwrap_or_else(|| panic!("the layer still has a `{door}` door"))
            .1;
        let body = match rest.split_once("\n    pub fn ") {
            Some((body, _)) => body,
            None => rest,
        };
        assert!(
            body.contains("on_main("),
            "the layer's `{door}` no longer goes through `on_main` — called \
                 from `codify_browser_{door}`-shaped code it runs on a tokio \
                 worker, where GTK panics and the page never exists"
        );
    }
}

/// The announcement has to be wired, and the keyboard has to stay the person's.
///
/// `watch_focus` working on real widgets is proved above; what that cannot see
/// is a layer that never connects it, and a shell that starts taking the focus
/// for itself — which would pull the keyboard out of the composer every time a
/// page was shown.
#[test]
fn the_layer_reports_a_page_taking_the_keyboard_and_never_takes_it() {
    let layer = page_layer_source();
    let built = layer
        .split_once("let built = assemble(&vbox);")
        .expect("`layer` builds the layer with `assemble`")
        .1
        .split_once("Ok(built)")
        .expect("`layer` returns what it built")
        .0;
    for needed in ["watch_focus(", "PAGE_FOCUSED_EVENT", "BrowserPageFocused"] {
        assert!(
            built.contains(needed),
            "`layer` no longer wires {needed:?} when it builds the layer — a click inside \
             a page would again leave the split's focus marker where it was"
        );
    }
    // The one place a page is handed the focus is the smoke, which has no
    // person to click. `grab_focus` anywhere else is the app taking the
    // keyboard from whoever has it.
    for (name, text) in [
        ("mod.rs", include_str!("mod.rs")),
        ("../lib.rs", include_str!("../lib.rs")),
    ] {
        assert!(
            !text.contains("take_focus") && !text.contains("grab_focus"),
            "{name} gives a page the keyboard — only the smoke does that, because it has \
             nobody to click, and the app taking the focus would pull it out of the composer"
        );
    }
    let smoke = include_str!("smoke.rs");
    assert!(
        smoke.contains("focus_leg(&geometry_sink)")
            && smoke.contains("embed-smoke: FAILED a page was given the keyboard"),
        "the smoke no longer gives its page the focus and fails when the window is not told"
    );
}

/// A page that is not in the layer is a fault, and a smoke that read it as "nothing to
/// check here" would report a placement failure as green.
#[test]
fn a_page_missing_from_the_layer_is_a_fault() {
    let layer = page_layer_source();
    assert!(
        layer.contains("is not in the page layer"),
        "a page missing from the layer is no longer an error — a smoke \
             would report a placement failure as 'nothing to check here', which \
             is the shape of green that hid the original bug"
    );
}
