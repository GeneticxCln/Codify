//! The embedded browser, and the isolation it starts from.
//!
//! This is the first surface in Codify that renders **untrusted content**:
//! every other pixel in the app is engine-owned or shell-owned. The isolation
//! has five layers; this module owns the four the shell adds, and the tests at
//! the bottom assert them rather than trusting a comment.
//!
//! 1. **An empty capability set, matched on the webview label.** Browser
//!    webviews get the label `browser-<tab>` ([`webview_label`]) and no
//!    capability that grants anything matches it:
//!    `src-tauri/capabilities/browser.json` covers `browser-*` — on
//!    `webviews`, not `windows` — with an empty `permissions` list, and no
//!    other capability's globs reach it. The key matters: a browser page now
//!    lives **inside the main window** as a child webview, and a child
//!    reports its parent's window label. Tauri resolves a capability with an
//!    *or* across `windows` and `webviews` patterns, so `default.json` is
//!    granted on `webviews: ["main"]` — the app's own webview label — and
//!    never through its window. A page seated in that window matches on
//!    neither and resolves nothing. [`the_browser_capability_set_is_empty`]
//!    and [`the_grant_reaches_main_and_no_browser_label`] hold both halves.
//!
//! 2. **A loopback URL guard on every navigation.** [`navigation_allowed`]
//!    refuses anything that is not http(s) and refuses loopback or
//!    unspecified hosts: `localhost` and `*.localhost` (RFC 6761), all of
//!    127/8, `::1`, `0.0.0.0`, `::`, their IPv4-mapped IPv6 forms, and the
//!    canonicalised integer/hex/octal spellings the WHATWG URL parser turns
//!    into them — `http://2130706433/` *is* 127.0.0.1. It is wired into
//!    [`WebviewBuilder::on_navigation`], so it is asked about every
//!    navigation the page attempts, including redirects — not just the first
//!    URL — and the commands check it once more before anything exists.
//!
//! 3. **The page reports itself: loading, loaded, titled.** Two page-load
//!    hooks and the title hook turn the page's own life into events the
//!    strip can show: `PageLoadEvent::Started` becomes
//!    [`PAGE_LOADING_EVENT`], `Finished` becomes [`PAGE_LOADED_EVENT`], and
//!    `on_document_title_changed` becomes [`PAGE_TITLED_EVENT`] — the tab
//!    shows the page's title (or its host until one arrives) and a bounded
//!    loading marker. Bounded because the runtime has no failed-load event
//!    (wry 0.55.1's `PageLoadEvent` is `Started | Finished` and nothing
//!    else, checked against the vendored source): "loading" the UI shows
//!    means "no Finished yet", and a page that dies into WebKit's
//!    interstitial would spin forever on that alone — so the UI expires the
//!    marker on a timer and the interstitial is the story, not the spinner.
//!    A popup's page reports through the same hooks: the announcement goes
//!    out, and the tab it opens loads like any other.
//!
//! 4. **Popups never become windows; they become tabs.** A page that calls
//!    `window.open` or points a link at `_blank` reaches
//!    [`WebviewBuilder::on_new_window`]. The handler refuses — returns
//!    [`NewWindowResponse::Deny`] — and announces the target on
//!    [`POPUP_REQUESTED_EVENT`] instead. The main window opens a real,
//!    user-visible browser tab for it through the same guarded command every
//!    other tab uses. Nothing in this module ever builds a `WebviewWindow`:
//!    a separate OS window is the one shape this browser does not have.
//!    Deny is the *only* return; the announcement is a courtesy to the user,
//!    not a permission the page holds.
//!
//! 5. **A voice at the command layer, and a page that cannot lie silently
//!    about its window.** A refused address is refused *before* the round
//!    trip (the UI's mirror in `ui/src/browserDispatch.ts`, pinned against
//!    [`navigation_allowed`] from both directions) or refused by the command
//!    with a sentence the tab renders verbatim — no address bar that
//!    swallows. A *load* failure — the TLS/certificate class — has no
//!    shell-side event to catch: wry 0.55.1's `PageLoadEvent` is
//!    `Started | Finished` and nothing else (checked against the vendored
//!    source), so the shell is never told. What actually happens is better
//!    than a wording of ours: WebKitGTK paints its own interstitial **inside
//!    the embedded page**, where the user is looking, and offers no bypass
//!    to widen. In the separate-window build that interstitial appeared in
//!    a window of its own; embedded, it is in the tab. The certificate
//!    error the user reported is therefore fixed by the embedding itself
//!    plus the trust store, and never by a flag on this app.
//!
//! 6. **One escape that never widens the page, and no way out of the app.**
//!    **DevTools** ([`open_devtools`]) inspects the page without granting it
//!    anything: the inspector is a shell-side surface over the webview, not
//!    a capability the page holds, and it exists only because the crate is
//!    built with tauri's `devtools` feature — a release build without it
//!    compiles the command away, and the UI hides its control when
//!    a build without it says so.
//!
//!    There used to be a second one. **Open in system browser**
//!    ([`open_external`], now gone) handed the page's current URL to the
//!    operating system's own opener, on the honest argument that some page
//!    needs the profile, extensions and certificate trust a real browser
//!    carries. It is gone because the answer it gave was the wrong shape:
//!    a page that could be opened outside Codify is a page whose session,
//!    cookies and credentials leave with it, and the fix for "this site
//!    renders badly in an embedded view" is to make the embedded view
//!    handle the site, not to send the user somewhere else. Every site
//!    this app can open is a tab in this app, a popup is a tab in this
//!    app, and there is deliberately no code in this module that could
//!    hand a URL to anything else.
//!    [`the_only_escape_is_the_inspector_and_the_module_spawns_nothing`]
//!    holds that, by pinning the name, the `open` crate and the OS
//!    handler absent rather than present.
//!
//! 7. **An app ACL manifest, so layer 1 has something to deny with.** Layers
//!    1–4 only bite because Tauri *has* an ACL to enforce. Until this
//!    change the app defined none, and Tauri skips the check entirely for an
//!    application command invoked from a local origin — the main window's own
//!    origin, and any origin a page reached it at. `src-tauri/permissions/shell.json`
//!    is now that manifest: one `allow-` permission per `codify_*` command,
//!    collected into a `shell` set that `capabilities/default.json` — and
//!    only that file — references. Every `codify_*` invoke is resolved
//!    against the calling webview's label whatever the origin is, and a
//!    `browser-*` label resolves to nothing.
//!    [`the_app_acl_manifest_closes_the_local_origin_bypass`],
//!    [`the_grant_is_exactly_the_commands_the_handler_defines`] and
//!    [`the_grant_reaches_main_and_no_browser_label`] hold the first two
//!    claims; the third is the one that keeps layer 5 and `lib.rs` in step,
//!    because a command defined without a grant is stripped from the handler
//!    at compile time and fails at runtime as "command not found".
//!
//! ## Where the pages live
//!
//! Every browser page is a **child webview of the main window**, created with
//! [`Window::add_child`] (the `unstable` multi-webview feature) and then — on
//! the platforms whose toolkit cannot place a child webview — moved by
//! [`page_layer`] into a container this module owns, at the rectangle the pane
//! measured. Only one is visible at a time: [`focus`] shows the active tab's
//! page and hides the rest — a hidden webview paints nothing and takes no
//! input, so visibility *is* the stacking order and no z-order bookkeeping
//! exists. [`resize`] places them all together when the window's layout
//! changes, reported by the UI in logical pixels — the same units CSS uses, so
//! a HiDPI display needs no scaling here.
//!
//! **A page is only where the pane asked if the toolkit agreed**, and this is
//! the paragraph that used to be wrong about it. Tauri builds every webview
//! into the window's default `gtk::Box` on Linux, and wry's box branch packs a
//! child expand-and-fill without reading the bounds it was given: the page took
//! a *share of the window*. That is a page over half the app, then over all of
//! it, while the UI reported the right rectangle and this module handed it to a
//! toolkit that discarded it — and `resize` was a silent no-op for the same
//! reason, because wry only honours `set_bounds` for a webview it created in a
//! `gtk::Fixed`. The `page_layer` module below is the fix, and `make smoke-embed`
//! now asserts the page's real allocation against the rectangle it asked for,
//! which is the measurement that would have caught it years earlier.
//!
//! **And that work has to happen on GTK's own thread.** `codify_browser_open`
//! and its siblings are `async` commands, so Tauri runs them on a tokio
//! worker — and GTK's own assert, "GTK may only be used from the main
//! thread", ended the page there, before it existed. Every widget call in the
//! layer now goes through one door, which asks the toolkit that question
//! first. The smoke's geometry read comes from a spawned task rather than from
//! `setup`, for the same reason: a measurement taken on the main thread would
//! have agreed with the code that was broken.
//!
//! An embedded webview cannot close itself: a child has no window events,
//! and `WebviewEvent` (checked against tauri 2.11.6) carries drag-drop and
//! nothing else. The tab strip is therefore the only closer, which is the
//! model the UI already had — `browser-window-closed` from the
//! separate-window build is gone with the window it announced. What *can*
//! end a page without the strip asking is a load failure, which is layer 4,
//! and a WebKit web-process crash, which nothing observable reports and the
//! docs say so rather than pretending otherwise.
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
//! - The commands are a *creation* policy, not a download manager, a
//!   permission requester, or a certificate exception store. Downloads are
//!   refused (the builder's default), permission requests get WebKit's
//!   default no, and no failed certificate is ever overridden.
//! - The app ACL manifest gates *Tauri's* commands. It says nothing about
//!   what the shell itself can do: the engine subprocess, the PTYs and the
//!   webviews are all created from Rust, and no capability reaches them.
//!
//! ## Where things live
//!
//! This was one 4,196-line file, of which 2,063 lines were code and the rest tests. It is a
//! directory now, along the seams the code already had:
//!
//! - `mod.rs` (this file): labels and seat policy, the navigation guard, `open`, the user agent,
//!   and the page operations (navigate, focus, resize, close, devtools) and page events.
//! - `page_layer.rs`: the GTK container work, which is the only place that knows how the toolkit
//!   wants to hear about a page's position.
//! - `smoke.rs`: the embed smoke test's driver and the JavaScript it injects. Re-exported here, so
//!   `browser::smoke_mode` and the `SMOKE_*` names keep the paths `lib.rs` and the docs use.
//! - `tests.rs`: every test. Several read the shell's own source as their contract, through
//!   `production_source()`, which concatenates the three files above and never itself, so a test
//!   cannot match the string it asserts is absent.

use serde::{Deserialize, Serialize};
use tauri::webview::NewWindowResponse;
use tauri::{
    AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, Url, WebviewBuilder, WebviewUrl,
};

use crate::webview_bridge;

/// Every browser webview's label begins with this.
///
/// The capability file matches `browser-*`; the coupling between the two is
/// asserted by [`the_browser_capability_set_is_empty`], which builds a real
/// label through [`webview_label`] and requires the committed capability to
/// match it. Renaming one without the other fails the test instead of
/// silently producing webviews outside their own declaration.
pub const LABEL_PREFIX: &str = "browser-";

/// Whether a page being seated into the window should be visible.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum SeatVisibility {
    Shown,
    Hidden,
}

/// The visibility a seated page gets: visible only if the user is looking at
/// its tab.
///
/// Measured, not stylistic. The old seat was an unconditional `show()`, which
/// made the act of *restoring* a strip the act of making every browser page
/// visible at once — boot restored a strip with four browser tabs and seated
/// four stacked, all-visible webviews, and the window began burning CPU on
/// entry, before any user gesture. A webview that is visible but fully covered
/// by the page above it is not free: WebKitGTK keeps producing frames for the
/// pages the user cannot see (an animated tab most of all), and every frame
/// lands on the same GTK main loop every other pixel in the app is waiting
/// for. With the GPU path disabled — the dmabuf workaround this machine
/// needs — those frames are software `pixman` blits on the main thread, and
/// four pages stack up to a window that cannot draw a frame fast enough to
/// take a click.
///
/// The rule the UI already implements is "exactly one page visible — the
/// active tab's"; this moves the invariant into the shell so a seat can no
/// longer violate it. The bridge's [`webview_bridge::active`] records the tab
/// the user last looked at (written by the focus command, and nothing else);
/// a page whose tab is *not* that one is seated hidden, and the UI's focus
/// effect — which fires on every active-tab change and calls `focus` with the
/// empty id when no browser tab is showing — is what reveals it when its turn
/// comes. A page whose tab *is* active keeps the old behaviour, which is also
/// the boot-time behaviour when nothing has been focused yet.
pub fn seat_visibility(label: &str) -> SeatVisibility {
    let active = webview_bridge::active();
    match active {
        // The user has named a tab. Only that tab's page is shown; every other
        // page arrives hidden. The record holds a *tab id* while `label` is a
        // *webview label* (`browser-<tab id>`, the same shape [`webview_label`]
        // builds), so the comparison is made after re-deriving the label —
        // the two namespaces must never be compared raw.
        Some(active_tab) if format!("{LABEL_PREFIX}{active_tab}") != label => {
            SeatVisibility::Hidden
        }
        // Either this is the active tab's page, or nothing has been focused
        // yet and the seat cannot know better — boot restores pages before the
        // UI's first focus lands, and a page seated hidden then might sit
        // behind its own first-paint window (see `open`'s bounds note).
        _ => SeatVisibility::Shown,
    }
}

/// The webview label for a browser tab.
///
/// Prefixed so a tab id can never collide with the main window's own webview
/// label — the one label that *does* hold capabilities — and validated so no
/// glob metacharacter, whitespace, or separator can ride into a label that
/// the ACL then matches as a string. Refuses rather than sanitises: a caller
/// whose id needs cleaning does not know what it is doing.
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

/// One line per fact the first-paint question is actually about, printed to
/// stdout where a launch log already is.
///
/// The three mechanisms that can seat a page and never paint it are all
/// outside this crate's reach at runtime — the session-bus claim that makes
/// a *second* launch exit inside `build()` (a "blocked first paint" that is
/// really an exit), WebKit's bubblewrap sandbox refusing to start the web
/// process (a webview that loads nothing, not a webview that hangs), and
/// the driver/GPU path DMABUF rendering rides on. They are diagnosed here
/// rather than worked around: nothing in this function changes behaviour,
/// because a heuristic that "fixes" the sandbox by disabling it would be
/// weakening the isolation for everyone to unblock one machine. The
/// smoke test drives the embed end-to-end; these lines say which fact bit
/// when it does not paint.
pub fn log_environment_diagnostics() {
    let line = |label: &str, fact: String| {
        println!("[Codify] browser env: {label}: {fact}");
    };
    line(
        "session bus",
        match std::env::var("DBUS_SESSION_BUS_ADDRESS") {
            Ok(addr) if !addr.trim().is_empty() => {
                "present (the single-instance guard can claim its name; a second \
                 launch exits instead of painting)"
                    .to_string()
            }
            _ => "ABSENT (the guard degrades to no-guard; two instances may run)".to_string(),
        },
    );
    line(
        "webkit sandbox",
        match (std::path::Path::new("/usr/bin/bwrap").exists(), std::fs::read_to_string("/proc/sys/kernel/unprivileged_userns_clone").map(|v| v.trim().to_string())) {
            (true, Ok(v)) if v == "1" => "bubblewrap present, unprivileged userns enabled (the expected shape)".to_string(),
            (true, Ok(v)) => format!("bubblewrap present, unprivileged userns = {v} (a disabled value makes WebKit fail to start its web process; pages then paint nothing)"),
            (true, Err(_)) => "bubblewrap present, userns sysctl unreadable (non-Linux or restricted /proc)".to_string(),
            (false, _) => "bubblewrap NOT found at /usr/bin/bwrap (distro-dependent path; if WebKit cannot start its web process, this is the first thing to check)".to_string(),
        },
    );
    line(
        "dmabuf rendering",
        match std::env::var("WEBKIT_DISABLE_DMABUF_RENDERER") {
            Ok(v) if v == "1" => "disabled by environment (set by the user or another app; expect software rendering)".to_string(),
            _ => "enabled (default; on Nvidia-proprietary or compositor quirks, first paint may hang — the known workaround is WEBKIT_DISABLE_DMABUF_RENDERER=1, which this app never sets itself)".to_string(),
        },
    );
    line(
        "display backend",
        format!(
            "GDK_BACKEND={:?}, WAYLAND_DISPLAY={:?} ({} session)",
            std::env::var("GDK_BACKEND").unwrap_or_default(),
            std::env::var("WAYLAND_DISPLAY").unwrap_or_default(),
            std::env::var("XDG_SESSION_TYPE").unwrap_or_else(|_| "unknown".to_string()),
        ),
    );
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

/// The container the browser's pages live in.
///
/// `open`, `resize` and `close` say *what* to do with a page; this module is
/// the only place that knows *how* the toolkit wants to hear it.
///
/// ## Why it exists
///
/// Tauri builds **every** webview into the window's default `gtk::Box` —
/// `tauri-runtime-wry`'s `WebviewKind::WindowChild => build_gtk(default_vbox())`
/// — and wry's box branch packs a child with
/// `pack_start(webview, true, true, 0)`, expand and fill, **without reading the
/// bounds it was handed**. A `GtkBox` lays its children out in a line; it
/// cannot overlap them and it cannot place one at a rectangle. So a page in one
/// takes a *share of the window* rather than the pane's rectangle, and that is
/// what a user saw: the browser opening over half of Codify and then over all
/// of it, while the UI reported the right rectangle and the shell handed it to
/// a toolkit that discarded it.
///
/// wry's own way out is a `GtkFixed` parent — it records
/// `is_in_fixed_parent` at *creation* and only then honours `set_bounds` — but
/// Tauri exposes no way to create a webview into a container of our choosing
/// (its builder has no `build_gtk`, its `Webview` no `gtk_widget`). So this
/// module does the container work itself, from `Window::default_vbox()`: the
/// app's own webview becomes the main child of a [`gtk::Overlay`], every page
/// lives in a [`gtk::Fixed`] layered above it, and a page's position and size
/// are then exactly what this module sets. Placement is driven here, by hand,
/// on every `open` and every `resize`; nothing calls wry's `set_bounds`, which
/// would stay a no-op on this platform however the tree is arranged.
mod page_layer;

/// Open a browser page inside the main window, or navigate the one that
/// exists.
///
/// The label comes from [`webview_label`] and the URL from
/// [`parse_navigation`], so both refusals happen before anything exists.
/// Re-opening an existing tab navigates it — the guard runs again through
/// `on_navigation` on the way, so the double check cannot disagree.
///
/// The geometry is handed in by the UI, which measures its own layout:
/// `x`/`y`/`width`/`height` in **logical** pixels, the units CSS speaks, so
/// the page sits exactly under the pane's toolbar and above nothing else.
pub fn open(
    app: &AppHandle,
    tab_id: &str,
    raw_url: &str,
    bounds: Bounds,
) -> Result<String, String> {
    let label = webview_label(tab_id)?;
    let target = parse_navigation(raw_url)?;
    let window = app
        .get_window("main")
        .ok_or_else(|| "the main window does not exist".to_string())?;

    // An existing page navigates in place; only a fresh one is created.
    let existing = window.webviews().into_iter().find(|w| w.label() == label);
    if let Some(page) = existing {
        page.navigate(target)
            .map_err(|e| format!("could not navigate browser tab {tab_id:?}: {e}"))?;
        return Ok(label);
    }

    // The popup handler outlives this call — it fires for the life of the
    // page — so it owns its own copy of the tab id rather than borrowing the
    // parameter a caller's borrow cannot cover.
    // A page seated at zero size paints nothing and — on WebKitGTK — a
    // child created hidden-or-tiny can miss its first damage event even
    // after a later resize, which is the "first paint never came" class of
    // bug. The bounds arrive from the UI, and a pane that has not mounted
    // yet reports nothing: seating an invisible page and hoping the resize
    // fixes it is the bug, so this refuses *before* the webview exists and
    // the UI re-asks the moment its ResizeObserver fires. The address is
    // remembered by the tab either way; nothing is lost but a frame.
    if !(bounds.width.is_finite() && bounds.height.is_finite())
        || bounds.width < 1.0
        || bounds.height < 1.0
    {
        return Err(format!(
            "browser pane has no size yet ({}x{}) — the page will open the moment the pane is measured",
            bounds.width, bounds.height
        ));
    }

    let for_popup = tab_id.to_string();
    let page_sink = app.clone();
    let for_load = tab_id.to_string();
    let for_title = app.clone();
    let for_titled = tab_id.to_string();
    let popup_sink = app.clone();
    let builder = WebviewBuilder::new(label.clone(), WebviewUrl::External(target))
        // Honest about what is rendering the page. See `page_user_agent`; the
        // default is a Safari 15.4 string on a WebKitGTK build that has
        // nothing to do with Safari.
        .user_agent(&page_user_agent())
        // The guard at the only place it can be enforced: every navigation
        // request the page makes is asked first, including ones it
        // initiates itself and server redirects. Returning false cancels.
        .on_navigation(navigation_allowed)
        // The page's life, announced: a load starts (the tab may show a
        // loading marker), a load finishes (the marker ends, and the
        // address the page actually arrived at — redirects included — is
        // what the address bar should say). The two events carry the URL so
        // one subscription keeps the strip and the address bar in step.
        .on_page_load(move |_page, payload| {
            let (event_name, url) = match payload.event() {
                tauri::webview::PageLoadEvent::Started => {
                    (PAGE_LOADING_EVENT, payload.url().to_string())
                }
                tauri::webview::PageLoadEvent::Finished => {
                    (PAGE_LOADED_EVENT, payload.url().to_string())
                }
            };
            let _ = page_sink.emit(
                event_name,
                BrowserPageState {
                    tab_id: for_load.clone(),
                    url,
                    title: None,
                },
            );
        })
        // The document's title, as the page names itself — the strip shows
        // this instead of a host the moment the page has a better answer.
        .on_document_title_changed(move |page, title| {
            let url = page.url().map(|u| u.to_string()).unwrap_or_default();
            let _ = for_title.emit(
                PAGE_TITLED_EVENT,
                BrowserPageState {
                    tab_id: for_titled.clone(),
                    url,
                    title: Some(title),
                },
            );
        })
        // Popups never open windows — see the module docs. Deny and announce.
        .on_new_window(move |url, _features| {
            if navigation_allowed(&url) {
                let _ = popup_sink.emit(
                    POPUP_REQUESTED_EVENT,
                    BrowserPopupRequested {
                        tab_id: for_popup.clone(),
                        url: url.to_string(),
                    },
                );
            }
            NewWindowResponse::Deny
        })
        .accept_first_mouse(true);
    // The position is the toolbar's bottom edge; the size is what the UI
    // measured for the pane's content area. Both logical, both from the UI,
    // because the UI is where the layout lives.
    // **Before the page exists**, and that ordering is load-bearing: the layer
    // adopts whatever the window already holds as its main child on the way
    // up, and a `gtk::Overlay` has exactly one of those. Built after the page,
    // it would try to adopt both and drop one with a warning.
    page_layer::prepare(&window)?;

    let page = window
        .add_child(
            builder,
            LogicalPosition::new(bounds.x, bounds.y),
            LogicalSize::new(bounds.width, bounds.height),
        )
        .map_err(|e| format!("could not open browser page: {e}"))?;
    // Where the page is *actually* placed on Linux: `add_child` packed it into
    // the window's box, and a box would give it a share of the window instead
    // of the pane's rectangle. See the module docs.
    page_layer::adopt(&window, &page, &bounds)?;

    // The new page is on top by construction (latest created paints last), and
    // it is visible only if its tab is the one the user is looking at —
    // `seat_visibility` decided that inside `adopt`. `focus` re-asserts
    // visibility on every active-tab change, so a page seated hidden is shown
    // the moment its tab is the active one.
    Ok(label)
}

/// The `User-Agent` a browser page is given.
///
/// Measured, not assumed: wry's WebKitGTK default is
/// `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko)
/// Version/60.5 Safari/605.1.15` — read out of a real page by
/// `SMOKE_PROBE_SCRIPT`'s `navigator.userAgent` and printed in every smoke
/// verdict. Two claims in it are false here. `Version/60.5` is Safari 15.4,
/// released in 2022, and `605.1.15` is Apple's build number for that release;
/// the engine actually rendering the page is WebKitGTK 2.52.6. A site that
/// branches on that string is branching on a fact about us that is wrong, and
/// the branches it takes are the ones that serve the *2022* web.
///
/// What is kept is the `AppleWebKit/<n> (KHTML, like Gecko)` token, and this
/// is a deliberate compromise rather than an oversight: the engine really is
/// WebKit, and it is the one token every browser-side parser already looks
/// for. Dropping it would make Codify read as an unknown client to more sites
/// than the lie ever did, which would make this change look like a regression
/// for a reason that has nothing to do with honesty. So: the engine family
/// stays, the Safari release and the Apple build number go, and the product
/// this actually is takes their place.
///
/// The version is the crate's, read at compile time, so it cannot drift into
/// a second version of the truth.
/// The environment variable that replaces the user agent for every page.
///
/// It exists so that "does this site behave differently for us?" is a
/// *measurement* rather than an argument: `make smoke-embed --user-agent` sets
/// it, the smoke runs the same URL under each string, and the verdicts are
/// compared. There is no other way to find out, because the alternative is
/// reading a site's JavaScript and guessing which branch the version string
/// takes — which is precisely the guess that keeps being wrong.
///
/// It is an env var rather than a setting for two reasons: it cannot be
/// persisted by accident, and it is visible in the process listing, so "why
/// does my browser look like Chrome" has a one-line answer. Empty or
/// whitespace is not an override — it falls back to the honest default, so a
/// harness that forgets to pass one gets the real thing rather than a blank.
pub const UA_ENV: &str = "CODIFY_PAGE_USER_AGENT";

pub fn page_user_agent() -> String {
    page_user_agent_from(std::env::var(UA_ENV).ok().as_deref())
}

/// The string itself, with the override resolved.
///
/// Split from [`page_user_agent`] so it can be tested without a process-wide
/// environment mutation: setting an env var in one test thread while another
/// asserts the default is a race that produces a failure nobody can
/// reproduce. Every caller passes the override in; only this one reads the
/// process.
fn page_user_agent_from(override_value: Option<&str>) -> String {
    if let Some(custom) = override_value.map(str::trim).filter(|v| !v.is_empty()) {
        return custom.to_string();
    }
    format!(
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) \
         Codify/{}",
        env!("CARGO_PKG_VERSION")
    )
}

/// The geometry a browser page occupies inside the main window.
///
/// Logical pixels — the units the UI measures in and CSS renders in. A
/// physical-pixel form here would need the display's scale factor on both
/// sides of the IPC to agree, which is exactly the kind of second source of
/// truth that drifts on a HiDPI monitor.
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct Bounds {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

/// Navigate an already-open browser page. The guard runs twice on purpose:
/// once here (a clear refusal the UI can show) and once inside
/// `on_navigation` (the enforcement point no caller routes around).
pub fn navigate(app: &AppHandle, tab_id: &str, raw_url: &str) -> Result<String, String> {
    let label = webview_label(tab_id)?;
    let target = parse_navigation(raw_url)?;
    let window = app
        .get_window("main")
        .ok_or_else(|| "the main window does not exist".to_string())?;
    let page = window
        .webviews()
        .into_iter()
        .find(|w| w.label() == label)
        .ok_or_else(|| format!("no browser tab {tab_id:?} is open"))?;
    page.navigate(target)
        .map_err(|e| format!("could not navigate browser tab {tab_id:?}: {e}"))?;
    Ok(label)
}

/// The page for a tab id, or the refusal every caller words the same way.
///
/// One lookup, four callers: navigate, focus, close, and the two escapes
/// below all mean "the page this tab owns" and all refuse identically when
/// there is none — which is what keeps "no browser tab is open" a sentence
/// with one owner.
fn page_for(app: &AppHandle, tab_id: &str, label: &str) -> Result<tauri::Webview, String> {
    let window = app
        .get_window("main")
        .ok_or_else(|| "the main window does not exist".to_string())?;
    window
        .webviews()
        .into_iter()
        .find(|w| w.label() == label)
        .ok_or_else(|| format!("no browser tab {tab_id:?} is open"))
}

/// Open the page's DevTools inspector.
///
/// The inspector is a debugging surface the *shell* opens over the webview —
/// it grants the page nothing it did not already have. The crate builds with
/// tauri's `devtools` feature, so the inspector exists in every build; remove
/// the feature and this call stops compiling rather than disappearing.
pub fn open_devtools(app: &AppHandle, tab_id: &str) -> Result<String, String> {
    let label = webview_label(tab_id)?;
    let page = page_for(app, tab_id, &label)?;
    page.open_devtools();
    Ok(label)
}

/// Close the page's DevTools inspector.
///
/// The open/close pair rather than a toggle, so a control's state and the
/// inspector's are the same fact: `is_open` reports, the commands act, and
/// no caller guesses.
pub fn close_devtools(app: &AppHandle, tab_id: &str) -> Result<String, String> {
    let label = webview_label(tab_id)?;
    let page = page_for(app, tab_id, &label)?;
    page.close_devtools();
    Ok(label)
}

/// Is this tab's inspector open?
///
/// So a caller can ask rather than guess, and a control can show the
/// inspector's real state instead of its own opinion of it.
pub fn devtools_open(app: &AppHandle, tab_id: &str) -> Result<bool, String> {
    let label = webview_label(tab_id)?;
    let page = page_for(app, tab_id, &label)?;
    Ok(page.is_devtools_open())
}

/// Does this build have an inspector to open?
///
/// Compile-time, not runtime: the same predicate tauri puts on
/// `open_devtools` itself. The crate enables the feature, so this is `true`
/// today and the type system says so the day it is not. Pinned by the test
/// below rather than carried as a dead code path.
pub fn devtools_available() -> bool {
    cfg!(feature = "devtools")
}

// The embed smoke test's driver and the page-side scripts it injects. Re-exported whole so
// `browser::smoke_mode` and the `SMOKE_*` names keep the paths `lib.rs` and the docs use.
mod smoke;
pub use smoke::*;

/// Show the named tab's page and hide every other.
///
/// Visibility *is* the stacking order for embedded webviews: a hidden
/// webview paints nothing and takes no input, so making the active tab the
/// only visible one needs no z-order bookkeeping and survives the
/// create-order lottery that raw stacking would be. An empty `tab_id` means
/// no browser tab is showing — every page is hidden, which is what a switch
/// to a chat or terminal tab asks for.
pub fn focus(app: &AppHandle, tab_id: &str) -> Result<String, String> {
    let active = if tab_id.is_empty() {
        None
    } else {
        Some(webview_label(tab_id)?)
    };
    let window = app
        .get_window("main")
        .ok_or_else(|| "the main window does not exist".to_string())?;
    for page in window.webviews() {
        if !page.label().starts_with(LABEL_PREFIX) {
            continue;
        }
        // Unconditional rather than read-then-decide: `is_visible` is not
        // part of tauri 2.11.6's Webview surface, and show on an already
        // visible view is the toolkit's no-op — the cheap case, not a bug.
        if active.as_deref() == Some(page.label()) {
            page.show()
                .map_err(|e| format!("could not show browser tab {tab_id:?}: {e}"))?;
        } else {
            page.hide()
                .map_err(|e| format!("could not hide browser tab {tab_id:?}: {e}"))?;
        }
    }
    Ok(tab_id.to_string())
}

/// Resize every browser page to the UI's current content area.
///
/// One command for all of them, not one per tab: the pages share the same
/// rectangle by construction, and a resize that arrived while the UI held
/// five browser tabs would otherwise be five calls and five chances to leave
/// one behind. Pages that do not exist yet are simply absent when their
/// `open` arrives with the fresh geometry.
pub fn resize(app: &AppHandle, bounds: Bounds) -> Result<(), String> {
    if !(bounds.width.is_finite() && bounds.height.is_finite()) {
        return Err("browser bounds must be finite".to_string());
    }
    if bounds.width < 1.0 || bounds.height < 1.0 {
        // A zero-sized pane is a real moment (the tab strip mid-animation);
        // it is not an error, there is just nothing to size yet.
        return Ok(());
    }
    let window = app
        .get_window("main")
        .ok_or_else(|| "the main window does not exist".to_string())?;
    for page in window.webviews() {
        if !page.label().starts_with(LABEL_PREFIX) {
            continue;
        }
        page_layer::place(&window, &page, &bounds)?;
    }
    Ok(())
}

/// Close a browser tab's page.
///
/// `close` rather than destroy: a child webview holds no window to destroy,
/// and `Webview::close` is the supported teardown. A page that is already
/// gone is a refused call — the UI only closes tabs it knows have a page —
/// so the refusal stays loud rather than being mistaken for success.
pub fn close(app: &AppHandle, tab_id: &str) -> Result<String, String> {
    let label = webview_label(tab_id)?;
    let window = app
        .get_window("main")
        .ok_or_else(|| "the main window does not exist".to_string())?;
    let page = window
        .webviews()
        .into_iter()
        .find(|w| w.label() == label)
        .ok_or_else(|| format!("no browser tab {tab_id:?} is open"))?;
    // Out of the layer first: the widget is about to be destroyed, and a
    // `gtk::Fixed` that is still holding it would keep the last geometry
    // around for the next page that reuses this label — which every reopened
    // tab does.
    page_layer::release(&window, &label);
    page.close()
        .map_err(|e| format!("could not close browser tab {tab_id:?}: {e}"))?;
    Ok(label)
}

/// The event a page's load start is announced on.
///
/// `PageLoadEvent::Started`, named for the fact rather than the hook: the
/// strip shows a loading marker from here until [`PAGE_LOADED_EVENT`] (or
/// the UI's bounded expiry — there is no failed-load event to end it, see
/// the module docs).
pub const PAGE_LOADING_EVENT: &str = "browser-page-loading";

/// The event a page's load completion is announced on.
pub const PAGE_LOADED_EVENT: &str = "browser-page-loaded";

/// The event a page's document title changing is announced on.
pub const PAGE_TITLED_EVENT: &str = "browser-page-titled";

/// The payload the page-life events carry.
///
/// `url` rides along on every one of them so a single subscription can keep
/// the address bar, the history and the title in step without a second
/// round trip: the load events carry the address being loaded (which is how
/// a redirect reaches the strip), and the title event carries the address
/// it titles.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserPageState {
    /// The tab whose page reported.
    pub tab_id: String,
    /// The address the fact is about.
    pub url: String,
    /// The document title, for [`PAGE_TITLED_EVENT`] only. `None` elsewhere;
    /// serde skips it so the load events' wire shape stays two fields.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
}

/// The event a refused popup is announced on.
///
/// The page asked for a window; the shell says no and says why not by
/// announcing the target so the *user* can open it as a tab instead. The
/// page itself learns nothing — `window.open` simply returns null, the same
/// answer a popup blocker gives.
pub const POPUP_REQUESTED_EVENT: &str = "browser-popup-requested";

/// Sent on [`POPUP_REQUESTED_EVENT`] when a page tried to open a window.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserPopupRequested {
    /// The tab that tried to open the popup.
    pub tab_id: String,
    /// The address it asked for, already through [`navigation_allowed`].
    pub url: String,
}

/// The event a page taking the keyboard is announced on.
///
/// A page is a native view, and the DOM of the app never sees a press inside
/// it: nothing the UI listens to moves when a person clicks into a page that
/// sits beside a chat. Typing goes where the toolkit's focus is, so what was
/// wrong was only the coloured edge saying where — and the UI's rule is that
/// the edge follows the keyboard. This is the toolkit's fact, said once per
/// change of focus; it is *not* a request, and the UI is free to ignore it
/// for a tab it is not drawing.
pub const PAGE_FOCUSED_EVENT: &str = "browser-page-focused";

/// Sent on [`PAGE_FOCUSED_EVENT`] when the keyboard went into a page.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserPageFocused {
    /// The tab whose page now holds the toolkit's focus.
    pub tab_id: String,
}

// Popup announcements need no static sink: `open` holds the `AppHandle`,
// and every handler captures its own clone. A module-level `OnceLock` was
// the shape the first pass reached for, and it was a second channel where a
// local would do — plus a boot-order invariant nothing needed.

#[cfg(test)]
mod tests;
