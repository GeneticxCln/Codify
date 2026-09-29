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
/// **Two definitions, one `cfg`.** `open`, `resize` and `close` say *what* to
/// do with a page; this module is the only place that knows *how* the local
/// toolkit wants to hear it, and the call sites are platform-free.
///
/// ## Why the Linux half exists
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
///
/// Every other platform gets real child-webview bounds from wry, so its
/// definition is five decisions that do nothing.
#[cfg(any(
    target_os = "linux",
    target_os = "dragonfly",
    target_os = "freebsd",
    target_os = "openbsd",
    target_os = "netbsd"
))]
mod page_layer {
    use super::*;
    use gtk::prelude::*;

    /// The widget name the layer's `gtk::Overlay` carries.
    ///
    /// Found by **name**, not by position: a child's index in a parent is a
    /// fact about the order things happened in, and that is precisely the kind
    /// of fact that stops being true the next time somebody adds a widget above
    /// it.
    const LAYER_NAME: &str = "codify-page-layer";

    /// The widget name the `gtk::Fixed` inside that overlay carries.
    const FIXED_NAME: &str = "codify-page-fixed";

    /// Run widget work on the one thread GTK allows it from.
    ///
    /// GTK asserts this on every call into a widget, and the assert reads
    /// "GTK may only be used from the main thread". A user's click reached it:
    /// `codify_browser_open` is an `async` command, Tauri runs those on a tokio
    /// worker, and the layer was built there — the page never existed. The
    /// embed smoke agreed with the broken code because the smoke seats its page
    /// from `setup`, on the main thread, and the click a user makes does not.
    ///
    /// The question below is the toolkit's own. `tao` initialises GTK on the
    /// main thread, and `gtk::init` **acquires and leaks** the default main
    /// context (gtk-rs#186), so for the life of the process that context has an
    /// owner and that owner is the only thread whose widget calls GTK accepts.
    /// When this already is that thread the work runs inline: hopping from the
    /// main thread would queue a closure behind the call that is waiting for it.
    ///
    /// The hop is a blocking `recv` on the calling thread, which is the shape
    /// Tauri itself uses for `add_child`: the worker waits, the toolkit's thread
    /// does the work, and the answer comes back to the call that asked. Nothing
    /// here is reentrant — the work is widget bookkeeping, not a callback.
    fn on_main<T>(
        window: &tauri::Window,
        work: impl FnOnce() -> Result<T, String> + Send + 'static,
    ) -> Result<T, String>
    where
        T: Send + 'static,
    {
        if gtk::glib::MainContext::default().is_owner() {
            return work();
        }
        let (done, answer) = std::sync::mpsc::channel();
        window
            .run_on_main_thread(move || {
                // A closed receiver means the caller gave up on the answer; the
                // work has already run and this is not the toolkit's problem.
                let _ = done.send(work());
            })
            .map_err(|e| format!("the page layer could not reach the main thread: {e}"))?;
        answer
            .recv()
            .map_err(|_| "the page layer's main-thread step ended without an answer".to_string())?
    }

    /// The layer, looked up and **never built**.
    ///
    /// Every path that must not restructure a window as a side effect uses
    /// this: closing a page, asking where a page is. A close that arrived
    /// before any page existed has nothing to do and must not leave an overlay
    /// behind as a souvenir.
    fn find(vbox: &gtk::Box) -> Option<(gtk::Overlay, gtk::Fixed)> {
        for child in vbox.children() {
            let Ok(overlay) = child.downcast::<gtk::Overlay>() else {
                continue;
            };
            if overlay.widget_name().as_str() != LAYER_NAME {
                continue;
            }
            for inner in overlay.children() {
                if let Ok(fixed) = inner.downcast::<gtk::Fixed>() {
                    if fixed.widget_name().as_str() == FIXED_NAME {
                        return Some((overlay, fixed));
                    }
                }
            }
        }
        None
    }

    /// The layer, built on first use.
    ///
    /// Lazy so that a user who never opens a browser tab has a window that was
    /// never restructured — the whole of this exists for pages, and revoking it
    /// for people who do not use them is the polite version.
    fn layer(window: &tauri::Window) -> Result<(gtk::Overlay, gtk::Fixed), String> {
        let vbox = window
            .default_vbox()
            .map_err(|e| format!("the main window has no GTK container: {e}"))?;
        if let Some(found) = find(&vbox) {
            return Ok(found);
        }
        Ok(assemble(&vbox))
    }

    /// Restructure `vbox` into the layer: what it holds becomes the overlay's
    /// main child, and a fixed sits above it for the pages.
    ///
    /// Split from [`layer`] so it can be built and inspected without a window;
    /// `the_page_layer_lets_input_through_to_the_app_beneath_it` does exactly that.
    pub(super) fn assemble(vbox: &gtk::Box) -> (gtk::Overlay, gtk::Fixed) {
        let overlay = gtk::Overlay::new();
        overlay.set_widget_name(LAYER_NAME);
        let fixed = gtk::Fixed::new();
        fixed.set_widget_name(FIXED_NAME);
        // Adopt what the window already holds — the app's own webview, the
        // whole UI — as the overlay's **main** child. A `gtk::Overlay` has
        // exactly one of those, which is why this has to run *before* the first
        // page is created: a second `add` is dropped with a warning rather than
        // layered, and a page dropped that way has no parent to be placed in.
        for child in vbox.children() {
            vbox.remove(&child);
            overlay.add(&child);
            child.show();
        }
        overlay.add_overlay(&fixed);
        // **Without this the app stops answering the moment a page exists.** An
        // overlay child that is not pass-through gets an input window over its
        // whole allocation, and a `Fixed` is allocated the entire overlay — so
        // the layer's window sat over the app's own webview and took every
        // click, key and scroll that was aimed at the tab strip, the side panel
        // and the composer. The window kept painting (nothing was wrong with
        // rendering), it just did not respond. Pass-through makes the *layer's*
        // window transparent to input; the pages inside it are child windows of
        // it and keep receiving theirs, which is what `gdk_window_set_pass_through`
        // documents ("the child windows of window are unaffected").
        overlay.set_overlay_pass_through(&fixed, true);
        vbox.pack_start(&overlay, true, true, 0);
        overlay.show();
        fixed.show();
        (overlay, fixed)
    }

    /// The widget a page's label names inside the layer's fixed.
    fn widget(fixed: &gtk::Fixed, label: &str) -> Option<gtk::Widget> {
        fixed
            .children()
            .into_iter()
            .find(|child| child.widget_name().as_str() == label)
    }

    /// Ready the container for a page that is about to exist.
    pub fn prepare(window: &tauri::Window) -> Result<(), String> {
        let here = window.clone();
        on_main(window, move || layer(&here).map(|_| ()))
    }

    /// Move a just-created page into the layer, at its rectangle.
    ///
    /// The page's widget is *the child that appeared in the window's box when
    /// this module asked for a page* — identified that way because Tauri gives
    /// out no handle to it. It is then renamed to the page's **label**, which
    /// is how every later lookup finds it: the same string the commands take,
    /// rather than an entry in a side table that could outlive the widget it
    /// describes.
    ///
    /// Whether the page is **shown** is decided by [`seat_visibility`], not by
    /// this function: a background tab's page is seated into the layer hidden,
    /// and the UI's focus effect is what makes it visible when its turn comes.
    pub fn adopt(
        window: &tauri::Window,
        page: &tauri::Webview,
        bounds: &Bounds,
    ) -> Result<(), String> {
        let here = window.clone();
        // The label and the rectangle travel instead of the handles: the
        // closure has to be `Send` and `'static` to cross threads, and the
        // widget is found by the label anyway.
        let label = page.label().to_string();
        let size = *bounds;
        let visibility = seat_visibility(&label);
        on_main(window, move || {
            let (_overlay, fixed) = layer(&here)?;
            let vbox = here
                .default_vbox()
                .map_err(|e| format!("the main window has no GTK container: {e}"))?;
            let widget = vbox
                .children()
                .into_iter()
                .find(|child| child.widget_name().as_str() != LAYER_NAME)
                .ok_or_else(|| "the new browser page has no widget to place".to_string())?;
            widget.set_widget_name(&label);
            vbox.remove(&widget);
            fixed.put(&widget, size.x.round() as i32, size.y.round() as i32);
            widget.set_size_request(size.width.round() as i32, size.height.round() as i32);
            match visibility {
                SeatVisibility::Shown => widget.show(),
                // `gtk_widget_hide` on an already-hidden child is a no-op, so
                // the call is unconditional: every page reaches the layer
                // through one branch, and no caller has to know which.
                SeatVisibility::Hidden => widget.hide(),
            }
            Ok(())
        })
    }

    /// Put one page at `bounds`, in the layer, by hand.
    pub fn place(
        window: &tauri::Window,
        page: &tauri::Webview,
        bounds: &Bounds,
    ) -> Result<(), String> {
        let here = window.clone();
        let label = page.label().to_string();
        let size = *bounds;
        on_main(window, move || {
            let (_overlay, fixed) = layer(&here)?;
            let widget = widget(&fixed, &label)
                .ok_or_else(|| format!("browser page {label:?} is not in the page layer"))?;
            fixed.move_(&widget, size.x.round() as i32, size.y.round() as i32);
            widget.set_size_request(size.width.round() as i32, size.height.round() as i32);
            Ok(())
        })
    }

    /// Take a page's widget out of the layer, before the page is closed.
    ///
    /// A label is reusable — reopening a closed tab asks for the same one — and
    /// a `gtk::Fixed` still holding the destroyed widget would hand the next
    /// page the last page's geometry.
    pub fn release(window: &tauri::Window, label: &str) {
        let here = window.clone();
        let label = label.to_string();
        // The outcome is dropped on purpose: a widget that could not be taken
        // out of the layer is one stale entry against a page that is being
        // destroyed, and refusing to close a tab over it is the worse trade.
        let _: Result<(), String> = on_main(window, move || {
            let Ok(vbox) = here.default_vbox() else {
                return Ok(());
            };
            if let Some((_overlay, fixed)) = find(&vbox) {
                if let Some(widget) = widget(&fixed, &label) {
                    fixed.remove(&widget);
                }
            }
            Ok(())
        });
    }

    /// Where a page's widget **actually is**, in the window's own coordinates.
    ///
    /// The measurement this pane was missing. The UI reports the rectangle it
    /// measured and the shell reports what it asked the toolkit for; neither is
    /// evidence that the toolkit complied, and on this platform for the whole
    /// life of the pane it did not. A page that is not in the layer is an
    /// `Err` and not a `None`, because those are different answers: `None`
    /// means "this platform does not report geometry", and a missing page is a
    /// fault worth failing a smoke over.
    pub fn geometry(app: &AppHandle, label: &str) -> Result<Option<(i32, i32, i32, i32)>, String> {
        let window = app
            .get_window("main")
            .ok_or_else(|| "the main window does not exist".to_string())?;
        let here = window.clone();
        let label = label.to_string();
        on_main(&window, move || {
            let vbox = here
                .default_vbox()
                .map_err(|e| format!("the main window has no GTK container: {e}"))?;
            let Some((_overlay, fixed)) = find(&vbox) else {
                return Err("no page layer exists, so no page can be in it".to_string());
            };
            let widget = widget(&fixed, &label)
                .ok_or_else(|| format!("browser page {label:?} is not in the page layer"))?;
            // Deprecated in GTK 3 in favour of size plus margins, and the right
            // call anyway: this reads the box the toolkit gave the widget,
            // which is the only thing that answers the question being asked. A
            // `width()` would report our own request back to us — the mistake
            // this whole check exists to stop.
            #[allow(deprecated)]
            let allocation = widget.allocation();
            Ok(Some((
                allocation.x(),
                allocation.y(),
                allocation.width(),
                allocation.height(),
            )))
        })
    }
}

#[cfg(not(any(
    target_os = "linux",
    target_os = "dragonfly",
    target_os = "freebsd",
    target_os = "openbsd",
    target_os = "netbsd"
)))]
mod page_layer {
    use super::*;

    /// Nothing to prepare: wry places a child webview where it is told.
    pub fn prepare(_window: &tauri::Window) -> Result<(), String> {
        Ok(())
    }

    /// Nothing to adopt: `add_child` already placed the page at these bounds.
    pub fn adopt(
        _window: &tauri::Window,
        _page: &tauri::Webview,
        _bounds: &Bounds,
    ) -> Result<(), String> {
        Ok(())
    }

    /// The platform's own call, one rectangle at a time.
    ///
    /// `set_bounds` and not `set_position` plus `set_size`: one rectangle, and
    /// no window of time where a page has the new size and the old origin.
    pub fn place(
        _window: &tauri::Window,
        page: &tauri::Webview,
        bounds: &Bounds,
    ) -> Result<(), String> {
        page.set_bounds(tauri::Rect {
            position: LogicalPosition::new(bounds.x, bounds.y).into(),
            size: LogicalSize::new(bounds.width, bounds.height).into(),
        })
        .map_err(|e| format!("could not place browser page: {e}"))
    }

    /// Nothing to release: closing the page is the whole teardown.
    pub fn release(_window: &tauri::Window, _label: &str) {}

    /// Not reported, and said so rather than reported as zero: the smoke asks
    /// this question to prove *this module's* placement, and on a platform
    /// where the toolkit does the placing there is nothing here to prove.
    pub fn geometry(
        _app: &AppHandle,
        _label: &str,
    ) -> Result<Option<(i32, i32, i32, i32)>, String> {
        Ok(None)
    }
}

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
        .on_navigation(|url| navigation_allowed(url))
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

/// Hand the page's current URL to the operating system's opener.
///
/// The user's escape hatch: the same URL, in the browser they have already
/// configured, with the profile, extensions and certificate trust that
/// browser carries — the honest answer to "this page needs what my browser
/// has and this embedded one does not". The URL comes from the webview
/// itself (`Webview::url`), which is the page's *live* address rather than
/// the tab's last recorded one, and it has passed the navigation guard by
/// being loaded at all; it is checked once more here anyway, because a
/// refusal costs nothing and the guard is the only rule this module keeps.
///
/// The embed smoke test's driver: seat a page on a fixed rectangle, wait for
/// its first paint, report one line, exit.
///
/// `CODEIFY_EMBED_SMOKE=<url>` (default `https://example.com/`) turns the
/// app's own launch into the test: no engine, no UI expectations — the
/// builder's `setup` seats the page exactly as `codify_browser_open` does
/// (same guard, same label, same `add_child`), the process announces
/// `embed-smoke: painted` on the first compositor frame, `embed-smoke: report
/// <…>` each time the page describes what it is showing
/// ([`SMOKE_PROBE_SCRIPT`]), and exits when it has **both** — or
/// `embed-smoke: FAILED <why>` at 20s. Exit 0 means the embed path painted a
/// real page *and* that page could describe itself; it says nothing about
/// whether the site worked, which is what the report is for and what
/// `scripts/embed_smoke.py` reads. It exists because every unit test above
/// stops one step short of the display: a webview that resolves its guard,
/// loads its HTML and *paints* is the one claim only a real run can make.
///
/// The wait is a frame-detection script injected into the page: it asks
/// `requestAnimationFrame` twice (the second callback proves the compositor
/// consumed a frame, which "document loaded" does not) and then marks the
/// paint in the document title. The mark is picked up by this module's own
/// `on_document_title_changed` hook and re-announced as a Tauri event, which
/// is the only part of the path that speaks Tauri — and it is the shell
/// speaking, not the page. The page itself needs no capability, which
/// matters: this is an untrusted page in a `browser-*` webview, exactly like
/// a user's tab, and it holds the empty capability set like one. See
/// [`SMOKE_PAINT_SCRIPT`] for the measurement that took the page's own
/// `emit_to` out of this path.
pub fn smoke_mode(url: &str, app: &tauri::AppHandle) -> Result<(), String> {
    let target = parse_navigation(url)?;
    println!("embed-smoke: seating {target} in the main window");
    let window = app
        .get_window("main")
        .ok_or_else(|| "the main window does not exist".to_string())?;
    let announce_sink = app.clone();
    let report_sink = app.clone();
    let builder = WebviewBuilder::new(
        SMOKE_PAGE_LABEL.to_string(),
        WebviewUrl::External(target.clone()),
    )
    .user_agent(&page_user_agent())
    .on_navigation(|url| navigation_allowed(url))
    // The paint detector, injected at document start: two animation frames
    // (the second proves the compositor consumed one) and then the mark. The
    // probe rides in the same script slot because it is the same kind of
    // measurement on the same throwaway page, and concatenating keeps both
    // constants readable and testable on their own.
    .initialization_script(&format!("{SMOKE_PAINT_SCRIPT}\n{SMOKE_PROBE_SCRIPT}"))
    // The page's only channel out, and it is one the ACL never governs: a
    // page may set its own title, and this is a builder hook on the shell's
    // side. See SMOKE_PAINT_MARKER for why the page does not get to speak
    // Tauri.
    .on_document_title_changed(move |_page, title| {
        if title == SMOKE_PAINT_MARKER || title == SMOKE_PROBE_PAINTED_MARKER {
            let _ = announce_sink.emit(SMOKE_PAINTED_EVENT, ());
        } else if let Some(report) = title.strip_prefix(SMOKE_PROBE_PREFIX) {
            // Printed, not parsed. The payload is percent-encoded JSON and the
            // reader is `scripts/embed_smoke.py`, which is where the report
            // becomes a sentence a person reads; keeping the decode on that
            // side means this module needs no base64, no percent-decoder and
            // no JSON in a hot path, and a malformed report degrades to a line
            // of noise rather than a failed smoke.
            println!("{SMOKE_REPORT_LINE}{report}");
            // Announced like the paint mark, so the run's end can wait for
            // both: the report is only half of what this smoke now claims, and
            // a run that ended on paint alone ended before the first report
            // was due.
            let _ = report_sink.emit(SMOKE_REPORT_EVENT, ());
        }
    });
    page_layer::prepare(&window)?;
    let page = window
        .add_child(
            builder,
            LogicalPosition::new(SMOKE_PAGE.x, SMOKE_PAGE.y),
            LogicalSize::new(SMOKE_PAGE.width, SMOKE_PAGE.height),
        )
        .map_err(|e| format!("could not seat the smoke page: {e}"))?;
    page_layer::adopt(&window, &page, &SMOKE_PAGE)?;
    println!(
        "embed-smoke: page seated at {},{} {}x{}, waiting for first paint",
        SMOKE_PAGE.x, SMOKE_PAGE.y, SMOKE_PAGE.width, SMOKE_PAGE.height
    );

    // **The measurement this smoke was missing.** Everything else here measures
    // the *page* — that it painted, what it shows, whether the AI can read it.
    // Nothing measured whether the page is where the pane asked for it, and on
    // Linux it never was: Tauri builds every webview into the window's
    // `gtk::Box`, which gives a child a *share of the window* instead of the
    // rectangle it was handed. A page that fills the window paints, reports
    // itself and answers the bridge perfectly — so this smoke ran green for as
    // long as a user watched the browser cover the whole app, which is the
    // worst kind of green there is.
    //
    // The requested rectangle is deliberately **not** the origin and not the
    // window's size, so "the toolkit used the whole window" cannot pass by
    // coincidence, and it is small enough to fit inside the window's client
    // area whatever the decorations cost.
    let geometry_sink = app.clone();
    tauri::async_runtime::spawn(async move {
        // One layout pass after the request is when the toolkit has an
        // allocation to report; reading it in the same breath as the request
        // would read the old one and call it a fix.
        tokio::time::sleep(std::time::Duration::from_millis(700)).await;
        // **From this thread, and not from the main one.** A task on the async
        // runtime is where `codify_browser_open` runs — the thread this layer
        // was built on when GTK refused the call and the page never existed. A
        // check taken on the main thread agreed with that broken code, which is
        // how this smoke stayed green while a user watched the browser cover
        // the app.
        //
        // Three of the layer's doors, then: `prepare` and `place` are idempotent
        // on a page that is already seated, so rehearsing them costs nothing and
        // proves the hop, and `geometry` is the verdict.
        let asked = (
            SMOKE_PAGE.x.round() as i32,
            SMOKE_PAGE.y.round() as i32,
            SMOKE_PAGE.width.round() as i32,
            SMOKE_PAGE.height.round() as i32,
        );
        let rehearsal = std::panic::catch_unwind(std::panic::AssertUnwindSafe(
            || -> Result<Option<(i32, i32, i32, i32)>, String> {
                let window = geometry_sink
                    .get_window("main")
                    .ok_or_else(|| "the main window does not exist".to_string())?;
                page_layer::prepare(&window)?;
                let page = window
                    .webviews()
                    .into_iter()
                    .find(|page| page.label() == SMOKE_PAGE_LABEL)
                    .ok_or_else(|| format!("no {:?} page is open", SMOKE_PAGE_LABEL))?;
                page_layer::place(&window, &page, &SMOKE_PAGE)?;
                page_layer::geometry(&geometry_sink, SMOKE_PAGE_LABEL)
            },
        ));
        let found = match rehearsal {
            Ok(Ok(found)) => found,
            Ok(Err(why)) => {
                println!(
                    "embed-smoke: FAILED the page's geometry could not be read ({why}), so \
                     nothing here proves the pane's rectangle was honoured"
                );
                std::process::exit(1);
            }
            // GTK's own assert, seen from a thread it will not talk to. Caught
            // rather than left to unwind so the run ends *naming* it: a panic in
            // a spawned task would end nothing, and this smoke would go on to
            // report a green that says nothing about where the page is.
            Err(_) => {
                println!(
                    "embed-smoke: FAILED the page layer panicked on a thread that is not the \
                     toolkit's — every widget call belongs on the main thread, and the click \
                     that opens a tab does not arrive there"
                );
                std::process::exit(1);
            }
        };
        match found {
            Some(found) if found == asked => {
                println!("embed-smoke: page geometry {found:?} — the page is where the pane asked")
            }
            Some(found) => {
                // A failed run, immediately and in words: this is a
                // *feature* the shell cannot deliver, not an environment
                // fact, so it must not be mistaken for a flaky paint.
                println!(
                    "embed-smoke: FAILED the page is at {found:?} but the pane asked for \
                     {asked:?} — the toolkit discarded the pane's rectangle, which is the \
                     bug that hid a page covering the whole app"
                );
                std::process::exit(1);
            }
            None => println!(
                "embed-smoke: this platform places pages through its own toolkit, so there \
                 is no container of ours to check the geometry of"
            ),
        }
    });
    Ok(())
}

/// The script the smoke page runs: two animation frames, then the mark.
///
/// Two frames and not one because the second callback is the one that
/// proves the *compositor* consumed a frame — a first callback only proves
/// the page's own script loop is running, which a hidden or zero-sized
/// webview also achieves. "The document loaded" is weaker again, and the
/// whole point of this detector is to be stronger than that.
///
/// The mark is a document title, not a Tauri invoke, and that is a
/// measured correction rather than a preference. The first version of this
/// script had the page call
/// `__TAURI_INTERNALS__.invoke("plugin:event|emit_to", …)`, reasoning that
/// the event is one-way out and therefore harmless. It is not: `emit_to` is
/// an ACL-governed Tauri command like any other, and this page holds the
/// empty `browser-*` capability set on purpose (see the module docs), so
/// the invoke was refused and the announcement never left the page. A smoke
/// built on it could only ever fail, and its failure would have read as
/// "first paint is broken" on a machine where painting was fine — measured
/// on a Wayland session whose four environment diagnostics all read
/// healthy. The title hook is the one channel a page needs no permission to
/// use, and it is the same mechanism the tab strip's live title already
/// depends on. [`the_paint_script_announces_the_event_the_lib_listens_for`]
/// holds the choice: the script may not grow an `invoke` back.
pub const SMOKE_PAINT_SCRIPT: &str = r#"
window.__codifySmoke = 0;
const step = () => {
  window.__codifySmoke += 1;
  if (window.__codifySmoke >= 2) {
    try {
      document.title = "codify-embed-smoke-painted";
    } catch (error) {
      console.log("embed-smoke: painted but could not mark it", error);
    }
    return;
  }
  requestAnimationFrame(step);
};
requestAnimationFrame(step);
"#;

/// The script that asks the smoke page **what it is showing**, not merely that
/// it painted.
///
/// `smoke_mode`'s paint verdict answers "did the compositor consume a frame".
/// That is the right question for a broken session and the wrong one for a
/// *site*: a YouTube shell, a Google sign-in **refusal** and a GitHub issue all
/// composite their first frame in about a second, so all three pass a paint
/// smoke while one of them is showing the user an error. Measured on this
/// checkout: `www.youtube.com`, `accounts.google.com` and a GitHub issue each
/// reported `painted` in 1.2–3.6s, and the verdict said nothing at all about
/// whether any of them worked. This script is what turns that pass into a fact.
///
/// It asks for the small set of things that differ between a working site and a
/// broken one in an embedded view: how much text the page rendered, whether it
/// put `<video>`/`<audio>` on the page, whether this WebKit says it can decode
/// H.264 and AAC (it usually cannot — that is the codec story, and it is a
/// fact to report rather than a mystery), and whether the rendered text
/// contains one of the sentences a site shows when it has decided to refuse an
/// embedded browser. The refusal list is the important half: "This browser or
/// app may not be secure" is Google's embedded-OAuth policy, it is not a
/// rendering bug, and an app that cannot tell the two apart either sends the
/// user somewhere else (which this module no longer does — see point 6) or
/// leaves them staring at a page that looks broken.
///
/// **Smoke-only, and it overwrites the title to say so.** The channel is the
/// same one [`SMOKE_PAINT_SCRIPT`] uses — a page may set its own title, and a
/// `browser-*` webview may not invoke anything — so the report rides in the
/// title, percent-encoded, behind [`SMOKE_PROBE_PREFIX`]. That is destructive
/// to the tab's name, which is exactly why it belongs to the smoke's throwaway
/// webview and must never be wired into a user's tab: the tab strip's live
/// title is the same channel, and a probe there would rename the user's tabs.
/// [`the_probe_script_asks_the_page_and_says_nothing_to_the_shell`] holds that,
/// including that the script still grows no `invoke`.
///
/// Re-announced a few times rather than once, because a page that manages its
/// own title will overwrite ours — YouTube rewrites its title long after load —
/// and a single early probe would report an empty body for a page that is
/// working perfectly. Each announcement is a whole report, so the last one the
/// shell sees is the most complete.
pub const SMOKE_PROBE_SCRIPT: &str = r##"
(() => {
  // ── Why, captured from the first script and before any page code runs ──
  //
  // "The page rendered nothing" names a symptom. These name the mechanism, and
  // they are the difference between a site that does not work in an embedded
  // view and a *network* that never delivered the page: a watch page measured
  // at zero characters, no title and no media, with its script tags served but
  // none of them running. Nothing above this block could tell those apart —
  // every one of them paints.
  const CAP = 5;          // entries per list
  const CLIP = 80;        // characters per entry
  // Outstanding requests get a tighter clip than the error lists, and the
  // reason is measured rather than guessed: the report rides in a document
  // title, percent-encoded, and a payload past **roughly a thousand
  // characters of line** comes back cut off mid-string. Measured on this
  // checkout, a 907-character payload arrived whole and a 981-character one
  // came back cut in the middle of the `ua` field — a loss the reader cannot
  // tell from a page that said nothing. So: no query strings, a 64-character
  // address, three rows, and the empty lists left out.
  const URL_CLIP = 64;
  const PENDING_CAP = 3;
  const keep = (list, entry) => {
    if (list.length >= CAP) return;
    list.push(String(entry).slice(0, CLIP));
  };
  const failedResources = [];
  const pageErrors = [];
  const consoleErrors = [];
  // `useCapture` is the whole point: a resource that fails to load fires
  // `error` on the *element*, and only the capture phase sees it — a bubbling
  // listener on window never hears "this <script> 404'd".
  window.addEventListener("error", (event) => {
    const el = event.target;
    if (el && el !== window && (el.src || el.href)) {
      const tag = (el.tagName || "?").toLowerCase();
      keep(failedResources, tag + " " + (el.src || el.href));
    } else {
      keep(pageErrors, event.message || "uncaught error");
    }
  }, true);
  // A rejected promise is how a modern site fails: no uncaught error, no
  // failed resource, a blank page and one line in a console nobody reads.
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event && event.reason;
    keep(pageErrors, "unhandled rejection: " + (reason && reason.message ? reason.message : reason));
  });
  // Console, patched before the page runs. warn/error only: `log` on a heavy
  // SPA is thousands of lines, and the two levels that matter for a blank page
  // are the two that mean someone gave up.
  ["warn", "error"].forEach((level) => {
    const original = console[level];
    console[level] = function () {
      try {
        keep(consoleErrors, level + ": " + Array.prototype.map.call(arguments, String).join(" "));
      } catch (e) { /* a page that breaks String() is not the story */ }
      try { original.apply(console, arguments); } catch (e) { /* keep the patch quiet */ }
    };
  });

  // ── Which request has not come back ──
  //
  // Resource timing is the wrong instrument for this and the reason is the
  // whole point of this block: **an entry only exists once a response has
  // finished**, and a cross-origin response with no `Timing-Allow-Origin`
  // produces no entry at all — in flight or complete. So `rp` counts what
  // arrived and is blind to what has not, which is the half that matters when
  // a page is stuck: measured on youtube.com at 10s, 92 completed subresources
  // and a page still `loading`, and nothing in the report could say what it
  // was waiting for. Its media lives on googlevideo.com, which is precisely
  // the origin resource timing cannot see.
  //
  // The outstanding half is counted where the request is *made* instead:
  // `fetch` and `XMLHttpRequest` are patched observationally — the page's own
  // call is what returns, and the patch only adds a bookkeeping entry — while
  // a media element that is fetching *right now* is read off the element
  // itself, because that is the one request a video site never finishes and
  // never hands back. Every patch is inside its own try: a page that defeats
  // this is a limit of the measurement, never a crash in the page.
  const pendingRequests = new Map();
  let requestSerial = 0;
  // Set once the user agent has been sent; see the report for why it is sent
  // at all. Declared here so the report can reach it.
  let userAgentSent = false;
  const now = () => {
    try {
      return performance.now();
    } catch (error) {
      return 0;
    }
  };
  const shorten = (url) => {
    try {
      const parsed = new URL(String(url), location.href);
      // **No query.** It is the wrong half to spend the channel on: a media
      // URL's query is kilobytes of signed parameters, none of which name the
      // request, and the channel is a document title with a measured width
      // (see the row cap below). A `?` survives, so a reader can tell an
      // addressed request from a bare path.
      //
      // A non-http scheme keeps its scheme, because dropping it is a lie:
      // `blob:https://www.youtube.com/6f3a…` read as `youtube.com/6f3a…` is a
      // page that has been asked to fetch a URL, and a Media Source object
      // URL is a handle on a stream rather than anything on the network.
      const named = parsed.protocol === "http:" || parsed.protocol === "https:"
        ? parsed.host + parsed.pathname + (parsed.search ? "?" : "")
        : parsed.protocol + (parsed.host ? "//" + parsed.host : "") + parsed.pathname;
      return named.slice(0, URL_CLIP);
    } catch (error) {
      return String(url).slice(0, URL_CLIP);
    }
  };
  const isMediaSource = (url) => {
    try {
      const protocol = new URL(String(url), location.href).protocol;
      return protocol === "blob:" || protocol === "data:" || protocol === "mediasource:";
    } catch (error) {
      return false;
    }
  };
  const startRequest = (url, method, kind, watched) => {
    // **The harness's own traffic is not the site's outstanding request.**
    // Measured: a run whose "what is the page waiting for" answer was
    // `GET codify-bridge://reply/smoke0001/0/0/%7B%22url%22…` twice over —
    // the page answering the bridge question the smoke asked it, reported as
    // youtube.com waiting on something. A measurement that appears in its own
    // findings is worse than one that is blind, because it is believed. The
    // scheme is a string rather than an import because this script is injected
    // as text; `the_probe_ignores_its_own_channel` holds the two ends together.
    const bridgeScheme = "codify-bridge:";
    if (String(url).slice(0, bridgeScheme.length).toLowerCase() === bridgeScheme) {
      return "";
    }
    requestSerial += 1;
    const id = "q" + requestSerial;
    pendingRequests.set(id, {
      k: kind,
      u: shorten(url),
      m: String(method || "GET").toUpperCase(),
      // `null` and not zero: `watched === false` means this request was
      // *found* in flight rather than seen leaving, so the page can say what
      // it is but not how long it has been waiting.
      at: watched === false ? null : now(),
    });
    return id;
  };
  const settleRequest = (id) => {
    if (id) pendingRequests.delete(id);
  };
  const hasMediaRequest = () => {
    for (const entry of pendingRequests.values()) {
      if (entry.k === "media") return true;
    }
    return false;
  };
  // One media request per address. A player that restarts its stream leaves
  // two rows naming the same URL, and a reader cannot tell a duplicate from a
  // retry — so the second sighting updates the first rather than adding to it.
  const noteMedia = (url) => {
    const address = shorten(url);
    for (const entry of pendingRequests.values()) {
      if (entry.k === "media" && entry.u === address) {
        // …and if the report-time scan got there first, the start is known now,
        // so an undated row becomes a dated one instead of staying "-1" for
        // the rest of the run.
        if (entry.at === null) entry.at = now();
        return;
      }
    }
    startRequest(url, isMediaSource(url) ? "MEDIASOURCE" : "MEDIA", "media");
  };
  // A media element already loading when the report looked, with no start to
  // measure from. Adding a timestamp here would invent one: the page did not
  // see this request leave, and "waiting 0ms" for a stream that has been
  // running for two seconds is the most plausible-looking lie available here.
  const noteLoadingMedia = (url) => {
    const address = shorten(url);
    for (const entry of pendingRequests.values()) {
      if (entry.k === "media" && entry.u === address) return;
    }
    startRequest(url, isMediaSource(url) ? "MEDIASOURCE" : "MEDIA", "media", false);
  };
  try {
    const originalFetch = window.fetch;
    if (typeof originalFetch === "function") {
      window.fetch = function (input, init) {
        let url = "";
        let method = "GET";
        try {
          url = input && input.url ? input.url : String(input);
          method = (init && init.method) || (input && input.method) || "GET";
        } catch (error) {
          // A request whose address cannot be read is still a request; the
          // unreadable half is not worth losing the entry over.
        }
        const id = startRequest(url, method, "fetch");
        let settled = false;
        const done = () => {
          if (!settled) {
            settled = true;
            settleRequest(id);
          }
        };
        try {
          return originalFetch.call(this, input, init).then(
            (value) => {
              done();
              return value;
            },
            (error) => {
              done();
              throw error;
            }
          );
        } catch (error) {
          done();
          throw error;
        }
      };
    }
  } catch (error) {
    // No fetch here. XMLHttpRequest below may still be the site's channel.
  }
  try {
    const proto = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
    if (proto) {
      const open = proto.open;
      const send = proto.send;
      proto.open = function (method, url) {
        try {
          this.__codifyProbe = { m: method, u: url };
        } catch (error) {
          // A frozen request object; the send below still runs.
        }
        return open.apply(this, arguments);
      };
      proto.send = function () {
        const asked = this.__codifyProbe || {};
        const id = startRequest(asked.u, asked.m, "xhr");
        // `loadend` fires for every outcome — finished, failed, aborted — so
        // one listener covers all of them and none of them can leave a request
        // pending that is not.
        this.addEventListener("loadend", () => settleRequest(id), { once: true });
        return send.apply(this, arguments);
      };
    }
  } catch (error) {
    // No XHR here either; a site using neither is a site with nothing pending.
  }
  // Media is neither `fetch` nor XHR: it is the element, and a `<video>` that
  // has not buffered is a request the other two patches would never see.
  // `loadstart` does not bubble, so this is a capture-phase listener on window
  // for the same reason the resource-error listener above is one.
  try {
    window.addEventListener("loadstart", (event) => {
      const el = event.target;
      const tag = el && el.tagName ? String(el.tagName).toLowerCase() : "";
      if (tag === "video" || tag === "audio") {
        noteMedia(el.currentSrc || el.src);
      }
    }, true);
    // Anything that means the media is no longer waiting. `error` is
    // deliberately in this list and not only `canplay`: a video that failed is
    // not a request still outstanding, and claiming otherwise would turn a
    // broken asset into a network stall.
    ["canplay", "playing", "error", "abort", "emptied"].forEach((name) => {
      window.addEventListener(name, () => {
        pendingRequests.forEach((entry, id) => {
          if (entry.k === "media") pendingRequests.delete(id);
        });
      }, true);
    });
  } catch (error) {
    // Keep going; the report will simply have no media half.
  }

  // The sentences a site shows when it has decided this embedded browser is
  // not acceptable. Matched case-insensitively against the rendered text, and
  // reported as the strings themselves rather than as indices into this list,
  // so the reading side needs no copy of the table and cannot drift from it.
  const REFUSALS = [
    "may not be secure",
    "not secure",
    "unsupported browser",
    "browser is not supported",
    "this browser or app",
    "sign in to view",
    "enable javascript",
    "access denied",
    "403 forbidden",
    "verify you are human",
    "unsupported",
  ];
  const report = () => {
    let text = "";
    try {
      text = document.body ? (document.body.innerText || "") : "";
    } catch (error) {
      text = "";
    }
    const lower = text.toLowerCase();
    const probe = document.createElement("video");
    const canPlay = (type) => {
      try {
        return probe.canPlayType(type) || "";
      } catch (error) {
        return "";
      }
    };
    const found = REFUSALS.filter((needle) => lower.includes(needle));
    const resourceCount = () => {
      try {
        return performance.getEntriesByType("resource").length;
      } catch (error) {
        return -1; // no Performance API: "unknown", not "none"
      }
    };
    const elapsedMs = () => {
      try {
        return Math.round(performance.now());
      } catch (error) {
        return -1;
      }
    };
    // What is outstanding **right now**, oldest first, and `-1` for an age
    // there is no honest way to give. A request the probe watched into
    // existence has a start time; one it found already in flight does not,
    // and a zero there would read as "just started" rather than "unknown".
    const outstanding = () => {
      const rows = [];
      try {
        // A media element loading right now, even if its `loadstart` was never
        // seen: `networkState` 2 is NETWORK_LOADING — fetching, no data yet —
        // and `currentSrc` is then the URL it is waiting on.
        if (!hasMediaRequest()) {
          const players = document.querySelectorAll("video,audio");
          for (let i = 0; i < players.length; i += 1) {
            try {
              if (players[i].networkState === 2 && players[i].currentSrc) {
                noteLoadingMedia(players[i].currentSrc);
                break;
              }
            } catch (error) {
              // An element that refuses to be read is not the story.
            }
          }
        }
        pendingRequests.forEach((entry) => {
          if (rows.length >= PENDING_CAP) return;
          rows.push({
            k: entry.k,
            u: entry.u,
            m: entry.m,
            ms: entry.at === null ? -1 : Math.max(0, Math.round(now() - entry.at)),
          });
        });
      } catch (error) {
        return [];
      }
      // Oldest first, and an entry with no age sorts last rather than
      // claiming to be the longest wait on the page.
      rows.sort((a, b) => b.ms - a.ms);
      return rows;
    };
    // The document's own load timestamp. `0` beside a `loading` readyState is
    // the document saying its load event never fired, which is the same fact
    // from the other side; `-1` is "this page has no navigation timing",
    // which is a different thing and must not read as zero.
    const loadEventEnd = () => {
      try {
        const nav = performance.getEntriesByType("navigation");
        if (nav && nav.length && typeof nav[0].loadEventEnd === "number") {
          return Math.round(nav[0].loadEventEnd);
        }
      } catch (error) {
        // No Performance API: unknown, not zero.
      }
      return -1;
    };
    // Where progress **stopped**, from the same resource timing entries. The
    // other two fields answer "what is it waiting for" and "has the document
    // finished"; this one answers "what was the last thing that arrived", and
    // on a stuck page the pair is the whole story — a site whose last arrival
    // was 1.2s ago and whose next request is a video segment is a network
    // saying something out loud, where either half alone only says it stopped.
    const lastArrival = () => {
      try {
        const entries = performance.getEntriesByType("resource");
        let latest = null;
        for (let i = 0; i < entries.length; i += 1) {
          if (!latest || entries[i].responseEnd > latest.responseEnd) latest = entries[i];
        }
        if (latest && latest.name) {
          return shorten(latest.name) + " at " + Math.round(latest.responseEnd) + "ms";
        }
      } catch (error) {
        // No timing to summarise.
      }
      return "";
    };
    const payload = {
      u: String(location.href || "").slice(0, 140),
      // The page's **own** title, remembered before the first announcement.
      // Reporting `document.title` live is reporting ourselves: the probe sets
      // the title to carry its payload, so a later report quotes the probe's
      // previous payload back as if it were the page's title — which is what
      // the first measured run did, in full view, and a fact about the probe
      // is not a fact about the site.
      t: String(pageTitle || "").slice(0, 200),
      n: text.length,
      v: document.querySelectorAll("video").length,
      d: document.querySelectorAll("audio").length,
      // Frames, and they are a *limit on this report*, not a fact about the
      // site: `innerText` reaches the top document only, so a page that puts
      // its content in a cross-origin frame renders perfectly and reads here
      // as nearly empty. Google���s own sign-in does exactly that — measured at
      // 135 characters of visible text and one frame, on a page that was
      // working. Counting them is what lets the reader say "this probe cannot
      // see that page" instead of "that page is broken", which is the
      // difference between a measurement and a false accusation.
      f: document.querySelectorAll("iframe").length,
      h: canPlay('video/mp4; codecs="avc1.42E01E"'),
      c: canPlay('audio/mp4; codecs="mp4a.40.2"'),
      e: found,
      // ── the mechanism, for the times the page is blank ──
      // What the page asked for, and what came back. `rp` counts the
      // subresources the browser *tried*; `x` lists the ones that *failed*; `j`
      // is what the page itself threw. A page with scripts, resources attempted
      // and nothing failed is a page that loaded and chose to render nothing; a
      // page with no resources attempted never got past the document. Those are
      // different bugs with different fixes, and "rendered nothing" cannot
      // tell them apart.
      rs: String(document.readyState || ""),
      sc: document.querySelectorAll("script").length,
      lk: document.querySelectorAll('link[rel="stylesheet"]').length,
      // **Completed** subresources, not attempted: a resource timing entry
      // only exists once the response finished, so this number counts what
      // arrived. That is the more useful half anyway — a page whose document
      // is still `loading` with a full set of completed subresources is
      // telling you the stall is after the fetch, in its own script.
      rp: resourceCount(),
      // How long the page has had, in milliseconds. "Still loading" and
      // "still loading after 10s" are different findings, and the difference is
      // this field.
      ms: elapsedMs(),
      // ── which request is outstanding ──
      //
      // The one field the earlier probe was missing. `rp` says how much
      // *arrived*; this says what the page is still waiting for, by name and
      // with an age where the page can give one. Without it, a page that is
      // stuck and a page that is idle both report the same completed count,
      // and the reader is left guessing which layer to look in.
      q: outstanding(),
      le: loadEventEnd(),
      lz: lastArrival(),
      ua: String(navigator.userAgent || "").slice(0, 200),
      x: failedResources.slice(),
      j: pageErrors.slice(),
      c9: consoleErrors.slice(),
    };
    // The user agent is sent **once**. It cannot change — the shell sets it on
    // the builder, before the page exists — and it is the single largest field
    // that does not vary between announcements, at 133 encoded characters of a
    // channel measured at about 980. The first report carries it and the rest
    // do not, and the reader keeps the first one: the alternative is a heavy
    // page losing its *whole* report, stall data included, to a string the run
    // already printed at launch. Nothing else is ever dropped this way, because
    // every other field is a fact about *now* — a page that failed a script at
    // 800ms and stopped failing at 2s must not go on reporting the failure.
    if (userAgentSent) {
      delete payload.ua;
    } else {
      userAgentSent = true;
    }
    // An empty list is the page saying nothing, and an absent key says it in
    // four characters instead of nineteen — four lists, 77 characters of a
    // channel measured at about a thousand. The reader treats a missing key
    // and an empty one identically, so nothing is lost and the room goes to
    // the fields that have something to say.
    ["e", "x", "j", "c9"].forEach((key) => {
      if (Array.isArray(payload[key]) && payload[key].length === 0) delete payload[key];
    });
    // …and then the page **measures** what it is about to send, because no
    // per-field cap predicts the total for a page nobody has run before.
    // Measured on this checkout: the channel delivers about 980 encoded
    // characters — 907 arrived whole, 981 came back cut off in the middle of a
    // field, twice at the same character — and a report that overflows it is
    // read as a page that said nothing at all.
    //
    // So the payload gives up its least useful field, in a fixed order, until
    // it fits, and says which ones in `df`. The order is the argument, and it
    // runs from least to most load-bearing: console noise, then the failed
    // subresources and the page's own throws, then the corroborating "last
    // arrival", then the outstanding requests — the headline — and last the
    // user agent, which cannot change and is usually not even here twice. What
    // no verdict is built on is never in the list: what the page shows, its
    // frame, codec and refusal state, and its readyState.
    const CHANNEL_LIMIT = 940;   // 981 is where it gets cut; 16 spare for `df`
    const SPENDABLE = ["c9", "x", "j", "lz", "q", "ua"];
    const encodedSize = () => {
      try {
        return encodeURIComponent(JSON.stringify(payload)).length;
      } catch (error) {
        return 0;
      }
    };
    const dropped = [];
    for (let i = 0; i < SPENDABLE.length && encodedSize() > CHANNEL_LIMIT; i += 1) {
      if (SPENDABLE[i] in payload) {
        dropped.push(SPENDABLE[i]);
        delete payload[SPENDABLE[i]];
      }
    }
    if (dropped.length) payload.df = dropped.join(",");
    return payload;
  };
  const announce = () => {
    try {
      let payload;
      try {
        payload = encodeURIComponent(JSON.stringify(report()));
      } catch (error) {
        payload = encodeURIComponent(JSON.stringify({ u: String(location.href || ""), e: ["probe-failed"] }));
      }
      document.title = "codify-embed-probe " + payload;
    } catch (error) {
      // Nothing to do: the smoke's own 20s timeout is the backstop.
    }
  };
  // The page's own title, taken before anything here overwrites it.
  let pageTitle = "";
  try {
    pageTitle = document.title || "";
  } catch (error) {
    pageTitle = "";
  }
  const noteTitle = () => {
    // A site that sets its title after load is the normal case, and this is
    // the only moment it is safe to read: the probe is about to take the title
    // over for its payload and will not give it back.
    try {
      if (document.title && !String(document.title).startsWith("codify-embed-")) {
        pageTitle = document.title;
      }
    } catch (error) {
      // Keep whatever was there.
    }
  };
  window.addEventListener("load", noteTitle, { once: true });
  setTimeout(noteTitle, 300);
  // The same two-frame rule the paint script uses, reported as a second
  // witness: the shell accepts this marker or the paint script's, so a race
  // between two scripts writing one title can cost a *report* but not the
  // verdict. (See SMOKE_PROBE_PAINTED_MARKER for the run that taught it.)
  let frames = 0;
  const witness = () => {
    frames += 1;
    if (frames < 2) {
      requestAnimationFrame(witness);
      return;
    }
    // **Re-asserted after load, not only in the frame it was earned.** The
    // first version set it once, at the second animation frame, and the shell
    // never saw it: on this WebKit the title callback does not deliver changes
    // made while the page is still loading, and every run since has reported
    // "no first paint within 20s" on pages that had plainly painted — the
    // probe's own report, arriving at 800ms, quoted the marker as the current
    // title, which is the proof that the page had it and the shell did not.
    // The condition is still two frames; only the announcement is repeated,
    // because the channel is lossy during load and this is the channel.
    [0, 400, 1200, 3000].forEach((ms) => setTimeout(() => {
      try {
        document.title = "codify-embed-probe-painted";
      } catch (error) {
        // The paint script's own marker is the other witness.
      }
    }, ms));
  };
  requestAnimationFrame(witness);
  // **Not** on load. This script and the paint script above it share one
  // channel — the document title — and they raced: a first version announced
  // on `load`, overwrote the paint marker within the same second. The first
  // report waits out the markers by a wide margin.
  [800, 2000, 5000, 10000].forEach((ms) => setTimeout(announce, ms));
})();
"##;

/// The prefix a probe report carries in the document title.
///
/// A prefix and not an equality check because the title is the whole channel
/// and the report is variable-length: the page can only say "here is a report"
/// and the shell can only recognise the shape. Public so the smoke's reader
/// (`scripts/embed_smoke.py`) and this module agree on the one string.
pub const SMOKE_PROBE_PREFIX: &str = "codify-embed-probe ";

/// The line the shell prints when the page hands over a report.
///
/// The actual cross-language boundary, and the one worth pinning: the page's
/// title prefix ([`SMOKE_PROBE_PREFIX`]) never leaves this module, and the
/// reader (`scripts/embed_smoke.py`) never sees a title. Public so the reader's
/// `REPORT_LINE` and this module cannot drift — a prefix that drifts prints
/// every report and reads none, which looks exactly like a page that said
/// nothing.
pub const SMOKE_REPORT_LINE: &str = "embed-smoke: report ";

/// The event the shell announces on when a page report arrives.
///
/// The paint event has a twin because the run has two deliverables now: the
/// paint verdict is not a verdict on its own any more. Public so `lib.rs`'s
/// listener and this module agree on the one string.
pub const SMOKE_REPORT_EVENT: &str = "codify-embed-smoke-report";

/// What the probe announces when **it** has seen two animation frames.
///
/// A second, independent witness of the same fact, on the same channel, for a
/// reason that cost a run to learn: two scripts writing one title can race, and
/// when the probe's first version did, the paint marker was overwritten before
/// the shell saw it and the run reported "no first paint" on a page that had
/// painted. Two witnesses where either suffices is the shape that survives a
/// race — the paint script keeps its own marker, this is a separate one, and
/// the shell accepts either.
pub const SMOKE_PROBE_PAINTED_MARKER: &str = "codify-embed-probe-painted";

/// The title a painted smoke page sets, and the event that title becomes.
///
/// One string for both, on purpose: the page can only say the marker, the
/// shell can only listen for it, and two spellings would be two things to
/// drift. It is a title rather than a Tauri event precisely because a page
/// holding the empty `browser-*` capability set cannot emit one — see
/// [`SMOKE_PAINT_SCRIPT`].
pub const SMOKE_PAINT_MARKER: &str = "codify-embed-smoke-painted";

/// The event the shell announces on when the smoke page's mark arrives.
///
/// Public so `lib.rs`'s listener and this module agree on the one string.
pub const SMOKE_PAINTED_EVENT: &str = SMOKE_PAINT_MARKER;

/// The label the smoke's page is created with.
///
/// A constant because two places need the *same* string now: the builder that
/// creates the page, and the geometry check that asks where the page ended up.
/// A drifted second spelling would make that check read `None` and report a
/// placement failure on a machine where placement works.
pub const SMOKE_PAGE_LABEL: &str = "browser-smoke";

/// Where the smoke seats its page — and **not** the origin, on purpose.
///
/// The smoke's job is to be the measurement the unit tests cannot be, and the
/// one thing it never measured was whether the page is where it was asked to
/// go. It asked for the whole window, which is also what a toolkit that
/// ignores the request produces, so the two were indistinguishable for as long
/// as the page was covering the app. Asking for a rectangle that is *not* the
/// window — inset, and smaller than any plausible client area at this window's
/// 1280×800 — is what makes the answers differ.
pub const SMOKE_PAGE: Bounds = Bounds {
    x: 24.0,
    y: 24.0,
    width: 560.0,
    height: 420.0,
};

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

/// Popup announcements need no static sink: `open` holds the `AppHandle`,
/// and every handler captures its own clone. A module-level `OnceLock` was
/// the shape the first pass reached for, and it was a second channel where a
/// local would do — plus a boot-order invariant nothing needed.
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
        let source = include_str!("lib.rs");
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
        let this_module = include_str!("browser.rs");
        let main_module = this_module
            .split_once("#[cfg(test)]")
            .expect("this file has a test module")
            .0;
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
        // scan stops at the test module — `include_str!` reads this file
        // whole, and this assertion names the builder it refuses.
        let main_module = source
            .split_once("#[cfg(test)]")
            .expect("this file has a test module")
            .0;
        assert!(
            !main_module.contains("WebviewWindowBuilder"),
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

    /// The two user escapes are wired, and the module still spawns nothing
    /// The inspector is the one escape left, and it stays a shell-side surface
    /// rather than a process. The system-browser hand-off this file used to
    /// carry is **gone**, and the assertion is the inverse of the old one on
    /// purpose: a removal nobody can see is a removal that comes back, so the
    /// name, the `open` crate and the OS handler are each pinned absent.
    #[test]
    fn the_only_escape_is_the_inspector_and_the_module_spawns_nothing() {
        let source = include_str!("browser.rs");
        let main_module = source
            .split_once("#[cfg(test)]")
            .expect("this file has a test module")
            .0;
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
        let mut scanned = 0usize;
        for entry in std::fs::read_dir(&src).expect("src-tauri/src must exist") {
            let path = entry.expect("a readable directory entry").path();
            if path.extension().and_then(|e| e.to_str()) != Some("rs") {
                continue;
            }
            scanned += 1;
            let text = std::fs::read_to_string(&path)
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
        // pass every assertion above while proving nothing.
        assert!(
            scanned >= 5,
            "the scan only reached {scanned} Rust files — it is not looking at the crate it claims to"
        );
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
        let text = std::fs::read_to_string(&ui)
            .unwrap_or_else(|e| panic!("{} must exist: {e}", ui.display()));
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
        let source = include_str!("browser.rs");
        let main_module = source
            .split_once("#[cfg(test)]")
            .expect("this file has a test module")
            .0;
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
        let text = std::fs::read_to_string(&ui)
            .unwrap_or_else(|e| panic!("{} must exist: {e}", ui.display()));
        for event in [
            POPUP_REQUESTED_EVENT,
            PAGE_LOADING_EVENT,
            PAGE_LOADED_EVENT,
            PAGE_TITLED_EVENT,
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
        let text = std::fs::read_to_string(&ui)
            .unwrap_or_else(|e| panic!("{} must exist: {e}", ui.display()));

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
            let sent = as_the_ui_sends(raw).unwrap_or_else(|| {
                panic!("the UI test allows {raw:?} but the pane would refuse it")
            });
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
        let lib = include_str!("lib.rs");
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
        let source = include_str!("browser.rs");
        let main_source = source
            .split_once("#[cfg(test)]")
            .expect("this file has a test module")
            .0;
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
        let source = include_str!("browser.rs");
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
        let lib = include_str!("lib.rs");
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
        let lib = include_str!("lib.rs");
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
        let source = include_str!("browser.rs");
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
        let lib = include_str!("lib.rs");
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
        let source = include_str!("browser.rs")
            .split("#[cfg(test)]")
            .next()
            .unwrap_or_default();
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
            code.contains(&format!(".user_agent(&page_user_agent())")),
            "both page builders must go through the resolver, not one of them \
             pinning the string"
        );
    }

    /// Every page is given it — the two places a page is built, and nowhere
    /// else, because the app's own UI is not a website and is not pretending
    /// to be one.
    #[test]
    fn every_browser_page_is_given_the_user_agent() {
        let source = include_str!("browser.rs")
            .split("#[cfg(test)]")
            .next()
            .unwrap_or_default();
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
        let source = include_str!("browser.rs");
        let layer = source
            .split_once("mod page_layer {")
            .expect("this file defines the page layer")
            .1
            .split_once("#[cfg(not(any(")
            .expect("the layer has a second definition")
            .0;
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

    /// The layer must not take the app's input.
    ///
    /// The tests around it read this file's source, which is how a layer that
    /// painted perfectly and answered nothing shipped: every string they look
    /// for was there. This builds the real widgets and asks GDK — the thing that
    /// routes a click — whether the layer's input window lets it through.
    /// Needs a display, and says so rather than passing without one.
    #[cfg(any(
        target_os = "linux",
        target_os = "dragonfly",
        target_os = "freebsd",
        target_os = "openbsd",
        target_os = "netbsd"
    ))]
    #[test]
    fn the_page_layer_lets_input_through_to_the_app_beneath_it() {
        use gtk::prelude::*;
        gtk::init().expect("this test builds real GTK widgets and needs a display");
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

    /// Every platform that cannot place a page gets the layer, and the list is
    /// the toolkit's own rather than this crate's opinion.
    ///
    /// A platform missing from it is not a compiler error and not a test
    /// failure anywhere else: it is a page that silently covers the app, which
    /// is the failure mode this whole change exists to end.
    #[test]
    fn the_layer_covers_exactly_the_platforms_that_need_it() {
        let source = include_str!("browser.rs");
        let cargo =
            std::fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml"))
                .expect("the crate's Cargo.toml is readable");
        for os in ["linux", "dragonfly", "freebsd", "openbsd", "netbsd"] {
            assert!(
                source.contains(&format!("target_os = \"{os}\"")),
                "browser.rs no longer names {os} in the layer's gate — every \
                 toolkit-backed webview puts its children in a GtkBox, and the \
                 one left out gets the silent no-op that hid this"
            );
            assert!(
                cargo.contains(&format!("target_os = \"{os}\"")),
                "Cargo.toml no longer gates `gtk` to {os} — either the \
                 dependency is compiled on a platform with no code for it or \
                 the code has no dependency to compile against"
            );
        }
    }

    /// Placement is this module's job where the toolkit will not do it, and the
    /// toolkit's everywhere else. Both at once is a fight; neither is the bug.
    #[test]
    fn placement_is_driven_by_this_module_or_by_the_toolkit_but_not_both() {
        let source = include_str!("browser.rs");
        let linux_half = source
            .split_once("mod page_layer {")
            .expect("the page layer exists")
            .1
            .split_once("#[cfg(not(any(")
            .expect("the layer has a second definition")
            .0;
        assert!(
            !linux_half.contains("set_bounds"),
            "the Linux half calls `set_bounds`, which wry honours only for a \
             webview it created in a GtkFixed — ours is created in the window's \
             box, so the call is a silent no-op and the page stays wherever the \
             box put it"
        );
        assert!(
            linux_half.contains("fixed.move_(&widget") && linux_half.contains("set_size_request"),
            "the Linux half no longer moves and sizes the page's widget itself \
             — on this platform nothing else will"
        );
        let other_half = source
            .split_once("#[cfg(not(any(")
            .expect("the layer has a second definition")
            .1;
        assert!(
            other_half.contains("page.set_bounds(tauri::Rect"),
            "the other platforms lost their `set_bounds` — they have no page \
             layer to be placed in, so the page would simply never be positioned"
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
        assert!(
            !(SMOKE_PAGE.x == 0.0 && SMOKE_PAGE.y == 0.0),
            "the smoke seats its page at the origin again — a toolkit that \
             discards the request and fills the window produces exactly that, \
             which is how this stayed green while a user watched it happen"
        );
        assert!(
            SMOKE_PAGE.width < 1280.0 && SMOKE_PAGE.height < 800.0,
            "the smoke asks for a rectangle as large as the window, which a \
             discarded request imitates perfectly"
        );
        let source = include_str!("browser.rs");
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
        let source = include_str!("browser.rs");
        let linux_half = source
            .split_once("mod page_layer {")
            .expect("the page layer exists")
            .1
            .split_once("#[cfg(not(any(")
            .expect("the layer has a second definition")
            .0;
        assert!(
            linux_half.contains("gtk::glib::MainContext::default().is_owner()"),
            "the layer no longer asks whether this is the thread GTK will answer \
             to. tao initialises GTK on the main thread and `gtk::init` acquires \
             and leaks the default main context, so its owner is the only thread \
             whose widget calls are legal — and `open` runs on a tokio worker"
        );
        // Each door, taken from its own `fn` to the next one, so a door that
        // stopped hopping is named rather than averaged away.
        for door in ["prepare", "adopt", "place", "release", "geometry"] {
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

    /// "This platform does not report geometry" and "this page is not in the
    /// layer" are different answers, and a smoke that failed on both would fail
    /// on macOS for being macOS.
    #[test]
    fn a_missing_page_is_a_fault_and_a_missing_platform_is_a_platform() {
        let source = include_str!("browser.rs");
        let linux_half = source
            .split_once("mod page_layer {")
            .expect("the page layer exists")
            .1
            .split_once("#[cfg(not(any(")
            .expect("the layer has a second definition")
            .0;
        assert!(
            linux_half.contains("is not in the page layer"),
            "a page missing from the layer is no longer an error — a smoke \
             would report a placement failure as 'nothing to check here', which \
             is the shape of green that hid the original bug"
        );
        let other_half = source
            .split_once("#[cfg(not(any(")
            .expect("the layer has a second definition")
            .1;
        assert!(
            other_half.contains("Ok(None)"),
            "the platforms that place pages themselves no longer say so — there \
             the absence of a layer is not a fault and must not be read as one"
        );
    }
}
