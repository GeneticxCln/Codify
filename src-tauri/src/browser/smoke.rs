use super::*;

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
    .on_navigation(navigation_allowed)
    // The paint detector, injected at document start: two animation frames
    // (the second proves the compositor consumed one) and then the mark. The
    // probe rides in the same script slot because it is the same kind of
    // measurement on the same throwaway page, and concatenating keeps both
    // constants readable and testable on their own.
    .initialization_script(format!("{SMOKE_PAINT_SCRIPT}\n{SMOKE_PROBE_SCRIPT}"))
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
                println!("embed-smoke: page geometry {found:?} — the page is where the pane asked");
                focus_leg(&geometry_sink).await;
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

/// The page taking the keyboard is heard, with nobody at the display to click.
///
/// A press inside a page is the toolkit's business, and the window learns of
/// it only through [`PAGE_FOCUSED_EVENT`]. This subscribes the way the window
/// does, gives the page the focus the way a press does
/// ([`page_layer::take_focus`]), and fails the run if the window is not told:
/// a coloured edge that cannot follow the keyboard is a feature the shell
/// cannot deliver, not an environment fact.
///
/// What it does **not** prove is that a *person's* click makes WebKit take the
/// focus — that is the toolkit's behaviour and this leg stands in for it. It
/// was checked once by hand, with the event injected through XTest into an
/// Xvfb display (a press inside the page was reported; no press, and a press
/// outside it, were not), and that check is not part of this run.
async fn focus_leg(app: &tauri::AppHandle) {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use tauri::Listener;
    let heard = Arc::new(AtomicBool::new(false));
    let seen = heard.clone();
    // The tab id is what the UI acts on, so it is what is checked: the label
    // is `browser-` and the id, and the smoke's page is `browser-smoke`.
    let wanted = SMOKE_PAGE_LABEL
        .trim_start_matches(LABEL_PREFIX)
        .to_string();
    let listener = app.listen(PAGE_FOCUSED_EVENT, move |event| {
        if let Ok(fact) = serde_json::from_str::<BrowserPageFocused>(event.payload()) {
            if fact.tab_id == wanted {
                seen.store(true, Ordering::SeqCst);
            }
        }
    });
    let asked = app
        .get_window("main")
        .ok_or_else(|| "the main window does not exist".to_string())
        .and_then(|window| page_layer::take_focus(&window, SMOKE_PAGE_LABEL));
    if let Err(why) = asked {
        println!("embed-smoke: FAILED the page could not be given the focus ({why})");
        std::process::exit(1);
    }
    // The signal is synchronous and the event is queued behind it: a second
    // is generous, and a run that needs more is a run that should say so.
    for _ in 0..20 {
        if heard.load(Ordering::SeqCst) {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    app.unlisten(listener);
    if heard.load(Ordering::SeqCst) {
        println!(
            "embed-smoke: page focus reported — the window is told which page took the keyboard"
        );
    } else {
        println!(
            "embed-smoke: FAILED a page was given the keyboard and the window was not told \
             ({PAGE_FOCUSED_EVENT}) — the split's focus marker would stay where it was"
        );
        std::process::exit(1);
    }
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
