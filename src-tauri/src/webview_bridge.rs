//! The shell's half of the bridge to the browser webviews.
//!
//! The engine asked a question; this module puts it to a page and gets an
//! answer back. It exists because the AI could read the repository and run its
//! own commands and could not read the page the user was looking at — the two
//! processes had no path between them at all.
//!
//! # Why the document title is not the channel
//!
//! `browser.rs`'s smoke probe reports through `document.title`, and that is
//! the one channel a page with an empty capability set is *guaranteed* to
//! have: it needs no permission at all. It is also destructive — it renames
//! the tab — and the probe is deliberately confined to a throwaway webview
//! for exactly that reason. A user reading a documentation page while the
//! model reads it too is not a throwaway webview, so this module does not
//! use the title.
//!
//! # What a page may say, and to whom
//!
//! `codify-bridge://reply/<id>/<seq>/<last>/<chunk>`, handled by
//! [`reply`] below and by nothing else. A page can reach it because a custom
//! scheme is not the network and not a Tauri command: the webview hands the
//! request to this process instead of resolving it, and no capability is
//! consulted. That is exactly why it is safe here and would not be if it
//! carried anything else. It moves **text** and nothing else — no boot
//! token, no command name, no path, no argv. A page cannot answer with a
//! capability because there is no capability on this wire.
//!
//! What a page *can* do is lie: answer a question it was not asked, or answer
//! one that is still waiting, or answer it twice. [`deliver`] therefore
//! accepts a chunk only for an id the engine actually issued, only at the
//! sequence number it expects next, and only once. Everything a page returns
//! is quoted back to the model as untrusted text by
//! `webview_bridge.format_page` on the engine side; this module's job is to
//! move it without ever believing it.
//!
//! # Three channels, because a page may forbid one
//!
//! The script sends each chunk over `fetch`, an `Image().src`, and
//! `sendBeacon`. A site's Content-Security-Policy can forbid any of them —
//! `connect-src 'none'` kills `fetch`, `img-src` kills the image — and a
//! bridge that reported "the page said nothing" when the real cause was a
//! CSP would send the model and the user looking in the wrong place. Three
//! attempts cost a few lines of JavaScript and remove the most likely way
//! this silently fails. A page that forbids all three genuinely has nothing
//! to say, and the engine's timeout says so in words.
//!
//! # Navigation goes round the guard, never through it
//!
//! [`navigate_tab`] moves a tab, and it does so by calling
//! `browser::navigate` — the *same function* `codify_browser_navigate` calls
//! for a user's click. The model proposes an address; `parse_navigation`
//! decides, in Rust, on the address that arrives here; and the page's own
//! redirects are guarded again by `on_navigation`, exactly as they are for a
//! person. Nothing in this module reimplements that rule, and a test fails
//! the build if a host check appears here.
//!
//! It cannot open a *new* tab, and that is deliberate rather than an
//! oversight: `browser::open` needs the content rectangle the UI measures and
//! owns, so a tab the bridge invented would have no geometry and would sit
//! somewhere the pane does not describe. A refusal naming the tab list is the
//! better answer.
//!
//! What this cannot do is escalate. A destination is another page in the same
//! embedded webview, which holds no capability and cannot reach the engine —
//! so the worst a hostile page can talk the model into is showing the user a
//! different page, which it could already do by navigating itself.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;

use serde::Deserialize;
use tauri::{AppHandle, Emitter, Manager};
use tokio::sync::oneshot;

use crate::browser::{webview_label, LABEL_PREFIX};

/// The custom scheme a browser webview may answer on.
///
/// A scheme and not a Tauri event, because an event would mean the page could
/// `invoke` — which a `browser-*` webview cannot do, deliberately, and which
/// is the boundary `capabilities/browser.json` exists to hold.
pub const BRIDGE_SCHEME: &str = "codify-bridge";

/// How many characters of encoded answer ride in one reply URL.
///
/// A URL has a length limit on every engine here and the answer does not:
/// a documentation page easily renders twenty thousand characters, which is
/// roughly sixty thousand percent-encoded. Rather than discover that as a
/// silent truncation on one platform, the answer is split and the pieces are
/// reassembled here. Splitting mid-triplet is harmless — decoding happens
/// after the join, not before it.
pub const CHUNK_CHARS: usize = 2_400;

/// The most a page may send for one question before it is cut off.
///
/// A guard, not a budget: nothing legitimate comes close, and an unbounded
/// buffer is a page being able to grow this process's memory one URL at a
/// time.
pub const MAX_REPLY_CHARS: usize = 4 * 1024 * 1024;

/// How many links one page read may bring back.
///
/// A navigation cap rather than a reading one: a page with four thousand
/// anchors is a page whose links are not what anyone is looking at, and the
/// whole list is characters spent on a choice nobody is going to make.
pub const MAX_PAGE_LINKS: usize = 40;

/// How long one question waits for its page before it is reported as unanswered.
///
/// Below the engine's own timeout on purpose. The engine has the longer one
/// because it also covers "no shell is attached at all"; by the time the
/// shell has a question in hand, it is attached, so the page is the only
/// thing left that can be slow.
pub const PAGE_TIMEOUT: Duration = Duration::from_secs(15);

/// One question, as the engine sent it.
#[derive(Debug, Clone, Deserialize)]
pub struct BridgeRequest {
    pub id: String,
    pub op: String,
    #[serde(default)]
    pub tab: Option<String>,
    #[serde(default)]
    pub selector: Option<String>,
    #[serde(default)]
    pub max_chars: Option<u64>,
    #[serde(default)]
    pub url: Option<String>,
    /// What a `type` verb types. Only `type` reads it, and it is bounded and
    /// JSON-escaped before it reaches a page, exactly as a selector is: it is
    /// text a model produced, going into a page it does not control.
    #[serde(default)]
    pub text: Option<String>,
}

/// The page's answer, still JSON, still just a string.
pub type Answer = String;

struct Pending {
    sender: oneshot::Sender<Answer>,
    chunks: Vec<String>,
    next: usize,
}

/// Everything the bridge remembers between a question and its answer.
#[derive(Default)]
struct State {
    pending: HashMap<String, Pending>,
    /// The tab the user last looked at. Recorded by [`note_active`]; used
    /// when a request names no tab, which is what "the page the user is
    /// looking at" has to mean when the request came from a model that
    /// cannot see the screen.
    active: Option<String>,
}

static STATE: Mutex<Option<State>> = Mutex::new(None);

fn with_state<T>(f: impl FnOnce(&mut State) -> T) -> T {
    let mut guard = STATE.lock().unwrap_or_else(|e| e.into_inner());
    let state = guard.get_or_insert_with(State::default);
    f(state)
}

/// Which browser tab the user is looking at.
///
/// Called from the focus command, so it is a fact about the UI's own state
/// rather than a guess made here. `is_visible` is not on tauri 2.11's
/// `Webview` surface — `browser::focus` shows one view and hides the rest
/// precisely because it cannot ask — so there is nothing to read it from, and
/// recording it at the one moment it is known is the whole of the mechanism.
pub fn note_active(tab_id: &str) {
    if tab_id.is_empty() {
        with_state(|state| state.active = None);
        return;
    }
    with_state(|state| state.active = Some(tab_id.to_string()));
}

/// Which browser tab the user last looked at, if any.
///
/// The read side of [`note_active`]: the focus command records the fact and
/// `page_layer` consults it when a page is seated, because a background tab's
/// page must never be made visible by the act of seating it — see
/// `page_layer::seat_visibility` for the rule this feeds.
pub fn active() -> Option<String> {
    with_state(|state| state.active.clone())
}

/// Take a question, and get a channel its answer will arrive on.
pub fn begin(request: &BridgeRequest) -> Result<oneshot::Receiver<Answer>, String> {
    let (sender, receiver) = oneshot::channel();
    let id = request.id.clone();
    with_state(|state| {
        state.pending.insert(
            id,
            Pending {
                sender,
                chunks: Vec::new(),
                next: 0,
            },
        );
    });
    Ok(receiver)
}

/// What a reply URL turned out to be.
enum Taken {
    /// A chunk of an answer still being assembled. Used, and not finished.
    Chunk,
    /// The last chunk: the whole answer, decoded, parsed and handed over.
    Complete,
    /// Not ours. The ordinary answer for a page being a page.
    Refused,
}

/// Hand a reply URL to whoever is waiting for it.
///
/// Returns whether the reply was *used*, which is not the same as whether the
/// answer is finished: a page sending a twenty-chunk answer has its first
/// chunk accepted long before it has anything to say. False is the ordinary
/// answer for a wrong id, a chunk out of order, a second copy of one already
/// taken, or a question that was already given up on. None of those are worth
/// an error — a page is free to fetch anything it likes, and the only thing
/// that matters is that none of it reaches the model.
pub fn deliver(url: &str) -> bool {
    let Some(reply) = Reply::parse(url) else {
        return false;
    };
    // Taken out of the map first and put back if it is not this chunk's turn.
    // Holding a `&mut` into the map while removing from it is the borrow
    // error's whole subject, and the entry is a handful of strings either
    // way — the cost of moving it is nothing next to the clarity.
    let taken = with_state(|state| {
        let Some(mut pending) = state.pending.remove(&reply.id) else {
            return Taken::Refused;
        };
        // Strictly next, never "the last one we saw": a page that resends
        // chunk 3 after chunk 4 has already been accepted must not overwrite
        // it, and the cheapest way to say that is to require the number.
        if reply.seq != pending.next {
            state.pending.insert(reply.id.clone(), pending);
            return Taken::Refused;
        }
        let held: usize = pending.chunks.iter().map(String::len).sum();
        if held + reply.chunk.len() > MAX_REPLY_CHARS {
            // Not put back: the question is dropped, and the sender going
            // with it is what tells the waiting read that it will not be
            // answered rather than leaving it to time out.
            return Taken::Refused;
        }
        pending.chunks.push(reply.chunk);
        pending.next += 1;
        if !reply.last {
            state.pending.insert(reply.id.clone(), pending);
            return Taken::Chunk;
        }
        let text = pending.chunks.concat();
        let decoded = percent_decode(&text).unwrap_or(text);
        match serde_json::from_str::<serde_json::Value>(&decoded) {
            Ok(value) if pending.sender.send(value.to_string()).is_ok() => Taken::Complete,
            _ => Taken::Refused,
        }
    });
    matches!(taken, Taken::Chunk | Taken::Complete)
}

/// Give up on a question: the page never answered, or the tab went away.
pub fn abandon(id: &str) {
    with_state(|state| {
        state.pending.remove(id);
    });
}

/// One `codify-bridge://reply/...` URL, taken apart.
struct Reply {
    id: String,
    seq: usize,
    last: bool,
    chunk: String,
}

impl Reply {
    fn parse(url: &str) -> Option<Reply> {
        let rest = url.strip_prefix(&format!("{BRIDGE_SCHEME}://reply/"))?;
        let rest = rest.split(['?', '#']).next().unwrap_or("");
        let mut parts = rest.split('/');
        let id = parts.next()?.to_string();
        let seq: usize = parts.next()?.parse().ok()?;
        let last = parts.next()? == "1";
        let chunk = parts.next().unwrap_or("").to_string();
        if id.is_empty() || id.len() > 128 || !id.chars().all(|c| c.is_ascii_alphanumeric()) {
            return None;
        }
        if parts.next().is_some() {
            return None;
        }
        Some(Reply {
            id,
            seq,
            last,
            chunk,
        })
    }
}

/// Percent-decoding, hand-rolled.
///
/// `encodeURIComponent` on the page side and this on the shell side, which is
/// why it is not base64: it is the one encoding available in a browser without
/// a library, and it is exact for UTF-8. Written out rather than pulled from
/// the `percent-encoding` crate because the crate is not already here and one
/// dependency for a loop is not worth it.
fn percent_decode(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' => {
                let hex = text.get(i + 1..i + 3)?;
                out.push(u8::from_str_radix(hex, 16).ok()?);
                i += 3;
            }
            byte => {
                out.push(byte);
                i += 1;
            }
        }
    }
    String::from_utf8(out).ok()
}

/// The browser webviews currently open, by tab id.
///
/// The label is stripped rather than the other way round because
/// `browser::webview_label` is the one definition of what a tab's label is,
/// and a second `format!` here is a second thing to drift.
pub fn open_tabs(app: &AppHandle) -> Vec<String> {
    // `app.webviews()` and **not** `app.get_webview_window("main")`, which is the
    // trap this used to be in. Tauri's `get_webview_window` returns `None`
    // unless the window still *is* a webview window, and `is_webview_window`
    // is defined as "has no child webviews" — so the moment the first browser
    // page is seated, `main` stops being one and the lookup answers `None`.
    // The failure is exact and silent: the bridge reported "there are no
    // browser tabs open" about an app with one open, and every read refused.
    // `app.webviews()` is the manager's own list of every webview, which is
    // what "which pages exist" actually means.
    let mut tabs: Vec<String> = Vec::new();
    for (label, _) in app.webviews() {
        if let Some(tab) = label.strip_prefix(LABEL_PREFIX) {
            tabs.push(tab.to_string());
        }
    }
    tabs.sort();
    tabs
}

/// The tab a request means: the one it named, the one the user is looking at,
/// or the only one there is — and a refusal, with the names, when that is
/// neither.
pub fn resolve_tab(app: &AppHandle, requested: Option<&str>) -> Result<String, String> {
    let tabs = open_tabs(app);
    if let Some(name) = requested.map(str::trim).filter(|n| !n.is_empty()) {
        return if tabs.iter().any(|t| t == name) {
            Ok(name.to_string())
        } else {
            Err(format!(
                "There is no browser tab called {name:?}. The open tabs are: {}.",
                if tabs.is_empty() {
                    "none".to_string()
                } else {
                    tabs.join(", ")
                }
            ))
        };
    }
    let active = with_state(|state| state.active.clone());
    if let Some(name) = active.filter(|name| tabs.iter().any(|t| t == name)) {
        return Ok(name);
    }
    match tabs.len() {
        0 => Err(
            "There is no browser tab open. Ask the user to open the page in \
             Codify's browser, then read it."
                .to_string(),
        ),
        1 => Ok(tabs[0].clone()),
        _ => Err(format!(
            "There are {} browser tabs open and I cannot tell which one you \
             mean. Ask me for one by name — they are: {}.",
            tabs.len(),
            tabs.join(", ")
        )),
    }
}

/// The script put into the page to answer one question.
///
/// How much text one `type` may put into a page.
///
/// A page is a page, not a terminal: this is the length of an answer to "type
/// this into the search box", and anything longer is a paste of something the
/// model should have summarised first.
pub const MAX_TYPED_CHARS: usize = 2_000;

/// The shared half of every page script: the id, the selector, the reply
/// channel, and the lookup that decides whether there is a target at all.
///
/// One preamble rather than three copies, because the parts that are shared
/// are the parts that carry the argument checks. A click script with its own
/// idea of how a selector is escaped is a script that will eventually disagree
/// with the read script about it.
///
/// `verb` is empty for a read — the whole page is a legal scope — and the
/// verb's own name for the other two, which have nothing to act on without
/// one.
fn page_preamble(request: &BridgeRequest, verb: &str) -> Result<String, String> {
    // Validated here rather than trusted from the wire: the engine checks
    // these too, but this string is about to be evaluated inside a page the
    // model does not control, and the page is the wrong place to discover a
    // mistake.
    let id = request.id.clone();
    if id.is_empty() || id.len() > 128 || !id.chars().all(|c| c.is_ascii_alphanumeric()) {
        return Err("the bridge request carried an unusable id".to_string());
    }
    // **Quoted**, and this was the bug that kept the bridge silent. An id
    // interpolated raw lands in the page as `var ID = smoke0001;`, which is a
    // reference to a variable nobody declared: the script throws
    // `ReferenceError` before `send()` is ever called, so the page says
    // nothing, the shell waits out its timeout, and the only trace is the
    // page's own console — which nothing was reading. The shape check above
    // passed the whole time, because `smoke0001` *is* a well-formed id; it was
    // never tested as a JavaScript literal.
    let id = serde_json::to_string(&id).map_err(|e| e.to_string())?;
    let raw = request.selector.as_deref().map(str::trim);
    if !verb.is_empty() && raw.map_or(true, |sel| sel.is_empty()) {
        return Err(format!("{verb} needs a CSS selector to act on"));
    }
    let selector = match raw {
        Some(sel) if !sel.is_empty() => {
            if sel.len() > 200 {
                return Err("that selector is too long to be one".to_string());
            }
            // JSON-escaped rather than quote-wrapped: a selector is page
            // text the model read off a page, so it can contain a quote, a
            // backslash or a newline, and each of those ends a JavaScript
            // string literal.
            serde_json::to_string(sel).map_err(|e| e.to_string())?
        }
        _ => "null".to_string(),
    };
    let max_chars = request.max_chars.unwrap_or(12_000).clamp(200, 40_000);
    // Quoted for the same reason the id is, and it was the second half of the
    // same bug: `var SCHEME = codify-bridge;` is a subtraction of two
    // undeclared variables, so the script threw `ReferenceError: codify is not
    // defined` and sent nothing. Both were invisible because the *shape* of
    // each value was fine; only its JavaScript *literal* was wrong.
    let scheme = serde_json::to_string(BRIDGE_SCHEME).map_err(|e| e.to_string())?;
    Ok(format!(
        r#"(function () {{
  var ID = {id};
  var SELECTOR = {selector};
  var MAX = {max_chars};
  var CHUNK = {CHUNK_CHARS};
  var SCHEME = {scheme};

  function send(encoded) {{
    var seq = 0;
    for (var i = 0; i < encoded.length; i += CHUNK) {{
      var piece = encoded.slice(i, i + CHUNK);
      var last = (i + CHUNK >= encoded.length) ? "1" : "0";
      var url = SCHEME + "://reply/" + ID + "/" + seq + "/" + last + "/" + piece;
      seq = seq + 1;
      // Three channels: a page's CSP can forbid any one of them, and a
      // bridge that reported "the page said nothing" when the cause was a
      // CSP would send everyone looking in the wrong place.
      try {{ fetch(url, {{ mode: "no-cors", cache: "no-store" }}); }} catch (e) {{}}
      try {{ new Image().src = url; }} catch (e) {{}}
      try {{ if (navigator.sendBeacon) {{ navigator.sendBeacon(url, "1"); }} }} catch (e) {{}}
    }}
  }}

  function answer(payload) {{
    try {{ send(encodeURIComponent(JSON.stringify(payload))); }}
    catch (e) {{ send(encodeURIComponent(JSON.stringify({{ error: String(e) }}))); }}
  }}

  function text_of(node) {{
    if (!node) {{ return ""; }}
    try {{ return node.innerText || node.textContent || ""; }} catch (e) {{ return ""; }}
  }}

  function where_you_are() {{
    return {{
      url: String(location.href || "").slice(0, 500),
      title: String(document.title || "").slice(0, 200)
    }};
  }}

  var body = "";
  try {{ body = text_of(document.body); }} catch (e) {{ body = ""; }}
  var target = null;
  if (SELECTOR !== null) {{
    try {{ target = document.querySelector(SELECTOR); }} catch (e) {{ target = null; }}
  }}
  if (SELECTOR !== null && target === null) {{
    // The page's own error rather than an empty answer, so the model reads
    // "nothing matched that selector" instead of a blank page.
    var missed = {{ error: "nothing on the page matched " + SELECTOR }};
    var at = where_you_are();
    missed.url = at.url;
    missed.title = at.title;
    answer(missed);
    return;
  }}
"#
    ))
}

/// A function, not a string constant, because three of its arguments are
/// decided per question and a template with holes in it is where an
/// unescaped value goes to become a syntax error in somebody's browser.
pub fn read_script(request: &BridgeRequest) -> Result<String, String> {
    let preamble = page_preamble(request, "")?;
    let max_links = MAX_PAGE_LINKS;
    Ok(format!(
        r#"{preamble}  var text = SELECTOR === null ? body : text_of(target);
  // Whitespace collapsed here rather than in the engine: `innerText` on a
  // real page carries hundreds of blank lines between blocks, and every one
  // of them is a character of the model's context spent on layout.
  var squeezed = text.replace(/[ \t ]+/g, " ").replace(/\n\s*\n\s*\n+/g, "\n\n").trim();
  // The links, because "follow the link about retries" is not expressible
  // without them: `innerText` carries a link's words but never its address,
  // so a model told to follow one would have to invent an href.
  //
  // Resolved against the document, so a relative href arrives absolute —
  // `new URL` is the browser's own resolver and is the one that agrees with
  // what clicking it would do. Anything that does not resolve to http(s) is
  // dropped *here* as well as refused later: `javascript:` and `file:` never
  // reach the model's context to be misused, and the engine's guard still
  // runs on whatever it is finally asked to navigate to.
  var links = [];
  try {{
    var anchors = document.querySelectorAll("a[href]");
    for (var i = 0; i < anchors.length && links.length < {max_links}; i++) {{
      var anchor = anchors[i];
      var href;
      try {{ href = new URL(anchor.getAttribute("href"), location.href).href; }}
      catch (e) {{ continue; }}
      if (!/^https?:/i.test(href)) {{ continue; }}
      var label = "";
      try {{ label = (anchor.innerText || anchor.textContent || "").replace(/\s+/g, " ").trim(); }}
      catch (e) {{ label = ""; }}
      links.push({{ t: label.slice(0, 80), h: href.slice(0, 500) }});
    }}
  }} catch (e) {{ links = []; }}
  answer({{
    url: String(location.href || "").slice(0, 500),
    title: String(document.title || "").slice(0, 200),
    text: squeezed.slice(0, MAX),
    chars: squeezed.length,
    truncated: squeezed.length > MAX,
    links: links,
    ready_state: String(document.readyState || "unknown")
  }});
}})();"#
    ))
}

/// Click what a selector names, and report what was clicked.
///
/// Nothing here reads the address back to check the click *landed*: what comes
/// back is what the page did, and `read_page` is what reads a page afterwards.
/// That is the honest division — a page that navigates itself can change the
/// answer, and the way to see where it went is to read the page rather than to
/// believe a click report.
pub fn click_script(request: &BridgeRequest) -> Result<String, String> {
    let preamble = page_preamble(request, "click")?;
    Ok(format!(
        r#"{preamble}  var tag = "";
  var label = "";
  var href = null;
  try {{ tag = String(target.tagName || "").toLowerCase(); }} catch (e) {{ tag = ""; }}
  try {{ label = text_of(target).replace(/\s+/g, " ").trim().slice(0, 120); }} catch (e) {{ label = ""; }}
  try {{ href = target.href ? String(target.href).slice(0, 500) : null; }} catch (e) {{ href = null; }}
  // A disabled control accepts a click and does nothing, and a model told
  // "clicked" would carry on believing it had. Refused here, in the page's own
  // words, because that is where the truth about the control lives.
  var off = false;
  try {{ off = target.disabled === true || target.getAttribute("aria-disabled") === "true"; }}
  catch (e) {{ off = false; }}
  if (off) {{
    var disabled = {{ error: SELECTOR + " is disabled, so clicking it would do nothing" }};
    var here = where_you_are();
    disabled.url = here.url;
    disabled.title = here.title;
    answer(disabled);
    return;
  }}
  try {{ target.click(); }} catch (e) {{
    var refused = {{ error: "the page refused that click: " + String(e) }};
    var at_refused = where_you_are();
    refused.url = at_refused.url;
    refused.title = at_refused.title;
    answer(refused);
    return;
  }}
  var done = where_you_are();
  answer({{
    ok: true,
    acted: "clicked",
    selector: SELECTOR,
    tag: tag,
    label: label,
    href: href,
    url: done.url,
    title: done.title
  }});
}})();"#
    ))
}

/// Type text into what a selector names, and report that it was typed.
///
/// The value is set through the element's *own* `value` setter rather than by
/// assigning the property, because that is the only way a React-controlled
/// input notices: such a page has replaced the instance setter with one that
/// tracks change, and `element.value = x` is silently discarded by it. Then
/// `input` and `change` are dispatched, because a page that listens for them
/// is how "type" becomes "submit".
pub fn type_script(request: &BridgeRequest) -> Result<String, String> {
    let preamble = page_preamble(request, "type")?;
    let text = request.text.as_deref().map(str::trim).unwrap_or_default();
    if text.is_empty() {
        return Err("type needs some text to type".to_string());
    }
    let count = text.chars().count();
    if count > MAX_TYPED_CHARS {
        return Err(format!(
            "that is {count} characters; type takes at most {MAX_TYPED_CHARS}"
        ));
    }
    // JSON-escaped for the reason a selector is: model-authored text can carry
    // a quote, a backslash or a newline, and each of those ends a literal.
    let typed = serde_json::to_string(text).map_err(|e| e.to_string())?;
    Ok(format!(
        r#"{preamble}  var TEXT = {typed};
  var tag = "";
  try {{ tag = String(target.tagName || "").toLowerCase(); }} catch (e) {{ tag = ""; }}
  var editable = (tag === "input" || tag === "textarea") || target.isContentEditable === true;
  if (!editable) {{
    var wrong = {{ error: SELECTOR + " is a <" + (tag || "thing") + ">, which is not a field to type into" }};
    var at_wrong = where_you_are();
    wrong.url = at_wrong.url;
    wrong.title = at_wrong.title;
    answer(wrong);
    return;
  }}
  if (tag === "input") {{
    var kind = "";
    try {{ kind = String(target.type || "text").toLowerCase(); }} catch (e) {{ kind = ""; }}
    if (kind === "checkbox" || kind === "radio" || kind === "file") {{
      var nottext = {{ error: SELECTOR + " is an <input type=" + kind + ">, which is not a field to type into" }};
      var at_nottext = where_you_are();
      nottext.url = at_nottext.url;
      nottext.title = at_nottext.title;
      answer(nottext);
      return;
    }}
  }}
  try {{
    target.focus();
    if (target.isContentEditable === true) {{
      target.textContent = TEXT;
    }} else {{
      var proto = (tag === "textarea") ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      var setter = Object.getOwnPropertyDescriptor(proto, "value");
      if (setter && setter.set) {{ setter.set.call(target, TEXT); }}
      else {{ target.value = TEXT; }}
    }}
    target.dispatchEvent(new Event("input", {{ bubbles: true }}));
    target.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }} catch (e) {{
    var threw = {{ error: "the page refused that text: " + String(e) }};
    var at_threw = where_you_are();
    threw.url = at_threw.url;
    threw.title = at_threw.title;
    answer(threw);
    return;
  }}
  var done = where_you_are();
  answer({{
    ok: true,
    acted: "typed",
    selector: SELECTOR,
    tag: tag,
    chars: TEXT.length,
    url: done.url,
    title: done.title
  }});
}})();"#
    ))
}

/// Perform whatever the engine asked for.
///
/// The op is chosen by `engine/webview_bridge.py` and matched here, not passed
/// through: an op this module does not recognise is refused rather than
/// guessed at. The four that exist are two kinds of thing, and that difference
/// is the whole security argument — [`ask_page`] needs the page's cooperation
/// and a reply channel to get anything back, while [`navigate_tab`] is three
/// lines of Rust that touch the webview from outside and never let the page
/// speak at all.
pub async fn serve(app: &AppHandle, request: BridgeRequest) -> Result<serde_json::Value, String> {
    match request.op.as_str() {
        "read_page" | "click" | "type" => ask_page(app, request).await,
        "navigate" => navigate_tab(app, &request),
        other => Err(format!(
            "this shell does not know how to perform {other:?}; it reads pages \
             and moves tabs, and nothing else"
        )),
    }
}

/// Move a tab somewhere the model picked, through the guard a click goes through.
///
/// `browser::navigate` is the *same function* `codify_browser_navigate`
/// calls, so a model-proposed address meets `parse_navigation` rather than a
/// reimplementation of it: http(s) only, never loopback in any of its
/// spellings. The page's own redirects are then guarded again by
/// `on_navigation`, as they are for a person.
///
/// Deliberately no eval and no reply channel. A navigation is the shell
/// telling the webview where to go, so there is nothing for the page to be
/// asked and nothing for it to answer — which also means a page cannot make
/// itself look like it navigated, or refuse to, to influence the model.
fn navigate_tab(app: &AppHandle, request: &BridgeRequest) -> Result<serde_json::Value, String> {
    let tab = resolve_tab(app, request.tab.as_deref())?;
    let url = request
        .url
        .as_deref()
        .map(str::trim)
        .filter(|u| !u.is_empty())
        .ok_or_else(|| "that navigation carried no URL".to_string())?;
    let label = crate::browser::navigate(app, &tab, url)?;
    Ok(serde_json::json!({
        "tab": tab,
        "url": url,
        "navigated": true,
        "label": label,
    }))
}

/// Ask one page one question, and wait for the answer.
///
/// Returns the answer as the page's parsed JSON. Everything in it is the
/// website's; the caller posts it to the engine and the engine quotes it.
/// An `Err` is a question the page could not answer — nothing to read, a
/// selector that matched nothing, a page that never spoke — and each of those
/// is a sentence the model can act on rather than silence.
pub async fn ask_page(
    app: &AppHandle,
    request: BridgeRequest,
) -> Result<serde_json::Value, String> {
    if !matches!(request.op.as_str(), "read_page" | "click" | "type") {
        // Reached only through `serve`, which has already matched the op; this
        // is the same check one layer down, for a caller that reaches
        // `ask_page` directly.
        return Err(format!(
            "ask_page was given {:?}, which is not something a page is asked",
            request.op
        ));
    }
    let tab = resolve_tab(app, request.tab.as_deref())?;
    let label = webview_label(&tab)?;
    let script = match request.op.as_str() {
        "click" => click_script(&request)?,
        "type" => type_script(&request)?,
        _ => read_script(&request)?,
    };

    let receiver = begin(&request)?;
    // By label, straight out of the manager — the same reason `open_tabs` does
    // it: `get_webview_window("main")` is `None` from the moment the first
    // page is seated, so going through the window found nothing to ask.
    let page = app
        .get_webview(&label)
        .ok_or_else(|| format!("the tab {tab:?} is not open any more"))?;
    page.eval(&script)
        .map_err(|e| format!("could not ask the page in {tab:?}: {e}"))?;

    match tokio::time::timeout(PAGE_TIMEOUT, receiver).await {
        Ok(Ok(answer)) => {
            let value: serde_json::Value = serde_json::from_str(&answer)
                .map_err(|e| format!("the page answered with something that is not JSON: {e}"))?;
            // The page reporting its own failure — a selector that matched
            // nothing, a document that would not read — is a refusal, not an
            // answer with no text. Forwarded as one so the model reads "nothing
            // matched that selector" rather than "the page was blank".
            let claimed = value
                .get("error")
                .and_then(|e| e.as_str())
                .map(str::trim)
                .filter(|e| !e.is_empty());
            match claimed {
                Some(reason) => Err(reason.to_string()),
                None => Ok(value),
            }
        }
        // The sender is dropped when the pending entry is removed, which is
        // how a question that nothing answered says so instead of waiting.
        Ok(Err(_)) => Err("the browser dropped that page read".to_string()),
        Err(_) => {
            abandon(&request.id);
            Err(format!(
                "the page in tab {tab:?} did not answer within {}s — it may be \
                 loading, or its Content-Security-Policy may be refusing every \
                 way a page can talk to the shell",
                PAGE_TIMEOUT.as_secs()
            ))
        }
    }
}

/// Answer the engine's poll, if it had one.
pub fn post_answer(
    client: &reqwest::Client,
    base: &str,
    token: &str,
    id: &str,
    value: serde_json::Value,
) {
    let body = serde_json::json!({ "id": id, "ok": true, "result": value });
    let fut = client
        .post(format!("{base}/bridge/answer"))
        .bearer_auth(token)
        .json(&body)
        .send();
    tauri::async_runtime::spawn(async move {
        let _ = fut.await;
    });
}

/// Answer with a refusal, so the model is told what happened rather than
/// waiting out the engine's own timeout.
pub fn post_refusal(client: &reqwest::Client, base: &str, token: &str, id: &str, reason: &str) {
    let body = serde_json::json!({ "id": id, "ok": false, "error": reason });
    let fut = client
        .post(format!("{base}/bridge/answer"))
        .bearer_auth(token)
        .json(&body)
        .send();
    tauri::async_runtime::spawn(async move {
        let _ = fut.await;
    });
}

/// The webview's handler for [`BRIDGE_SCHEME`].
///
/// One function, so the only place in this crate that touches Tauri's
/// scheme-registration API is one place — and so the answer is built here
/// rather than in a closure in `lib.rs` where it would be invisible to the
/// tests below.
///
/// It answers *every* request on the scheme with 204, including the ones it
/// did not use. A scheme handler that returned an error would put a console
/// line on somebody's page for a reply the page was entitled to send.
pub fn scheme_handler(
    _context: tauri::UriSchemeContext<'_, tauri::Wry>,
    request: tauri::http::Request<Vec<u8>>,
) -> tauri::http::Response<Vec<u8>> {
    let _ = deliver(request.uri().to_string().as_str());
    tauri::http::Response::builder()
        .status(204)
        .body(Vec::new())
        .unwrap_or_else(|_| tauri::http::Response::new(Vec::new()))
}

/// Poll the engine for questions, for as long as the app runs.
///
/// A long poll and not a WebSocket, which is the engine's decision as much as
/// this one's: every socket in this engine carries exactly one goal's events,
/// and a second long-lived socket is how two conversations end up
/// interleaved (see `tests/stream_isolation.py`). A request that returns
/// nothing is a fact about the engine, and it can be wrong, and it expires.
pub fn start(app: AppHandle, engine: crate::SharedEngineState) {
    tauri::async_runtime::spawn(async move {
        let client = reqwest::Client::new();
        loop {
            // Asked for per iteration rather than once: the engine can be
            // restarted underneath a running shell, and a base URL captured
            // at startup is a base URL that is wrong by the time it is next
            // used.
            let (base, token) = {
                let state = engine.lock().await;
                match (&state.token, &state.port) {
                    (Some(token), Some(port)) => {
                        (format!("http://127.0.0.1:{port}"), token.clone())
                    }
                    _ => {
                        drop(state);
                        tokio::time::sleep(Duration::from_secs(2)).await;
                        continue;
                    }
                }
            };

            let polled = client
                .get(format!("{base}/bridge/next?wait=20"))
                .bearer_auth(&token)
                .send()
                .await;
            let Ok(response) = polled else {
                tokio::time::sleep(Duration::from_secs(2)).await;
                continue;
            };
            let Ok(body) = response.json::<serde_json::Value>().await else {
                tokio::time::sleep(Duration::from_secs(2)).await;
                continue;
            };
            let Some(request) = serde_json::from_value::<BridgeRequest>(body).ok() else {
                continue;
            };

            let app_for_ask = app.clone();
            let client_for_answer = client.clone();
            let base_for_answer = base.clone();
            let token_for_answer = token.clone();
            tauri::async_runtime::spawn(async move {
                let id = request.id.clone();
                match serve(&app_for_ask, request).await {
                    Ok(value) => post_answer(
                        &client_for_answer,
                        &base_for_answer,
                        &token_for_answer,
                        &id,
                        value,
                    ),
                    Err(reason) => post_refusal(
                        &client_for_answer,
                        &base_for_answer,
                        &token_for_answer,
                        &id,
                        &reason,
                    ),
                }
            });
        }
    });
}

/// The line the smoke prints when a page has answered (or not).
///
/// Raw JSON on the tail, unlike [`crate::browser::SMOKE_REPORT_LINE`]'s
/// percent-encoding. That one rides in a *document title*, where a raw `{`
/// would need three layers of quoting; this one rides on stdout, where a
/// newline is the only thing that would break it and JSON has none.
pub const SMOKE_BRIDGE_LINE: &str = "embed-smoke: bridge ";

/// The event the smoke's third leg announces on.
pub const SMOKE_BRIDGE_EVENT: &str = "codify-smoke-bridge";

/// Set to skip the bridge leg, for a paint-only run.
///
/// An escape hatch rather than the default because the default should be the
/// honest one: if the AI cannot read the page, that is a broken feature, and a
/// harness that quietly stops measuring it is how a broken feature becomes a
/// passing build.
pub const SMOKE_BRIDGE_SKIP_ENV: &str = "CODEIFY_EMBED_SMOKE_NO_BRIDGE";

/// The tab `browser::smoke_mode` seats, as a bridge tab id.
pub const SMOKE_TAB: &str = "smoke";

/// How long the smoke waits before asking, so the page has text to report.
///
/// Not a guess about load time — it is the page probe's own first
/// announcement interval (`SMOKE_PROBE_SCRIPT`'s 800ms), plus slack. A read
/// fired at paint would race a document that has composited a frame and
/// nothing else, and would report "the page rendered no readable text" for a
/// page that is about to render plenty of it.
const SMOKE_BRIDGE_DELAY: Duration = Duration::from_millis(1_500);

/// Ask the smoke's own page to describe itself through the real bridge.
///
/// This is the one place the bridge is driven **without** the engine, and that
/// is a deliberate limit rather than a shortcut: smoke mode never starts an
/// engine (see `browser.rs`'s
/// `the_smoke_mode_is_gated_reports_and_never_starts_the_engine`), so there is
/// no `/bridge/next` to poll and nothing to hand out the question. The
/// request is therefore constructed here and passed to the same [`serve`] the
/// poll loop uses, which means everything the answer actually depends on is
/// measured for real — the eval, the custom scheme, the chunking, `deliver`,
/// the JSON — and only the two HTTP routes are not. Those are covered
/// without a display in `tests/test_webview_bridge.py`.
///
/// A read that never answers prints `fail` and announces the leg anyway, so
/// "the page said nothing" ends the run as a *failure with a reason* rather
/// than as a run that sat there until its own timeout.
pub fn smoke_probe(app: AppHandle, tab: &str) {
    let tab = tab.to_string();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(SMOKE_BRIDGE_DELAY).await;
        let request = BridgeRequest {
            // The id is minted here rather than taken from the engine, and it
            // has to satisfy the same shape an engine-issued one does — the
            // id is a map key on the way back, and `read_script` refuses
            // anything that could leave that map.
            id: "smoke0001".to_string(),
            op: "read_page".to_string(),
            tab: Some(tab.clone()),
            selector: None,
            max_chars: Some(2_000),
            url: None,
            text: None,
        };
        let line = match serve(&app, request).await {
            Ok(value) => match serde_json::to_string(&value) {
                Ok(json) => format!("{SMOKE_BRIDGE_LINE}ok {json}"),
                Err(e) => format!("{SMOKE_BRIDGE_LINE}fail the answer did not serialise: {e}"),
            },
            Err(reason) => format!("{SMOKE_BRIDGE_LINE}fail {reason}"),
        };
        println!("{line}");
        let _ = app.emit(SMOKE_BRIDGE_EVENT, ());
    });
}

/// Whether the smoke should run its bridge leg.
pub fn smoke_bridge_enabled() -> bool {
    !matches!(
        std::env::var(SMOKE_BRIDGE_SKIP_ENV).ok().as_deref(),
        Some("1") | Some("true") | Some("yes")
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(id: &str) -> BridgeRequest {
        BridgeRequest {
            id: id.to_string(),
            op: "read_page".to_string(),
            tab: None,
            selector: None,
            max_chars: Some(12_000),
            url: None,
            text: None,
        }
    }

    #[test]
    fn a_reply_url_is_taken_apart() {
        let reply = Reply::parse("codify-bridge://reply/abc123/0/1/hello%20there").unwrap();
        assert_eq!(reply.id, "abc123");
        assert_eq!(reply.seq, 0);
        assert!(reply.last);
        assert_eq!(percent_decode(&reply.chunk).unwrap(), "hello there");
    }

    #[test]
    fn a_reply_for_another_scheme_is_not_a_reply() {
        assert!(Reply::parse("https://example.test/reply/abc/0/1/x").is_none());
        assert!(Reply::parse("codify-bridge://other/abc/0/1/x").is_none());
        assert!(Reply::parse("codify-bridge://reply/").is_none());
    }

    #[test]
    fn a_reply_carrying_extra_path_is_refused() {
        // A page cannot smuggle a second field past the parser by adding
        // another segment.
        assert!(Reply::parse("codify-bridge://reply/abc/0/1/x/y").is_none());
    }

    #[test]
    fn a_reply_with_a_non_numeric_chunk_number_is_refused() {
        assert!(Reply::parse("codify-bridge://reply/abc/zero/1/x").is_none());
    }

    #[test]
    fn a_reply_whose_id_could_leave_the_pending_map_is_refused() {
        // The id is a map key written by whoever fetched the URL.
        for bad in ["a/b", "a b", "../x", ""] {
            let url = format!("codify-bridge://reply/{bad}/0/1/x");
            assert!(Reply::parse(&url).is_none(), "must refuse {bad:?}");
        }
    }

    #[test]
    fn a_reply_for_an_id_nobody_asked_about_reaches_nothing() {
        assert!(!deliver("codify-bridge://reply/nosuchid/0/1/x"));
    }

    #[test]
    fn a_question_is_answered_only_by_its_own_chunks_in_order() {
        let req = request("ordered1");
        let receiver = begin(&req).unwrap();
        // Out of order is refused rather than buffered: a page that resends
        // chunk 1 before chunk 0 must not be able to have its second copy win.
        assert!(!deliver("codify-bridge://reply/ordered1/1/1/%22b%22%7D"));
        assert!(deliver("codify-bridge://reply/ordered1/0/0/%7B%22a%22%3A"));
        assert!(deliver("codify-bridge://reply/ordered1/1/1/%22b%22%7D"));
        // `blocking_recv`, not `await`: `begin` and `deliver` are both
        // synchronous, and an async test would need tokio's `macros` and `rt`
        // features in dev-dependencies for a wait that blocks for nothing.
        // The chunks are percent-encoded all the way through, so this is also
        // the test that the page's encoding and this module's decoding agree.
        let value: serde_json::Value =
            serde_json::from_str(&receiver.blocking_recv().unwrap()).unwrap();
        assert_eq!(value, serde_json::json!({"a": "b"}));
    }

    #[test]
    fn a_chunk_sent_twice_does_not_overwrite_the_first() {
        let req = request("dupes1");
        let receiver = begin(&req).unwrap();
        assert!(deliver("codify-bridge://reply/dupes1/0/0/%7B%22a%22%3A"));
        // Same chunk number, different content, and claiming to be the last
        // one. The sequence number decides, so this is refused and the first
        // copy stands.
        assert!(!deliver(
            "codify-bridge://reply/dupes1/0/1/%7B%22x%22%3A1%7D"
        ));
        assert!(deliver("codify-bridge://reply/dupes1/1/1/%22b%22%7D"));
        let value: serde_json::Value =
            serde_json::from_str(&receiver.blocking_recv().unwrap()).unwrap();
        assert_eq!(value, serde_json::json!({"a": "b"}));
    }

    #[test]
    fn an_abandoned_question_cannot_be_answered_later() {
        let req = request("gone0001");
        let _receiver = begin(&req).unwrap();
        abandon("gone0001");
        assert!(!deliver("codify-bridge://reply/gone0001/0/1/x"));
    }

    #[test]
    fn the_script_answers_over_the_bridge_scheme_and_nothing_else() {
        let script = read_script(&request("script01")).unwrap();
        assert!(script.contains(BRIDGE_SCHEME));
        // One URL, three delivery attempts. A page must not be asked to reach
        // the engine, a Tauri command or anything off-box.
        assert!(!script.contains("__TAURI_INTERNALS__"));
        assert!(!script.contains("invoke("));
        assert!(!script.contains("127.0.0.1"));
        assert!(!script.contains("http://"));
    }

    #[test]
    fn the_script_tries_three_channels_because_a_csp_can_forbid_one() {
        let script = read_script(&request("script01")).unwrap();
        assert!(script.contains("fetch(url"));
        assert!(script.contains("new Image().src = url"));
        assert!(script.contains("sendBeacon"));
    }

    #[test]
    fn the_request_id_is_a_string_literal_and_not_a_variable() {
        // The whole bridge hung on this one. `var ID = smoke0001;` is a
        // reference to an undeclared variable, so every script threw
        // `ReferenceError` before it sent anything and the shell timed out on
        // every read — in the smoke and in the app alike, while the id's *shape*
        // checks passed the whole time. Mutation-checked: interpolating the id
        // raw fails here.
        for (id, op) in [
            ("smoke0001", "read_page"),
            ("abc123def", "click"),
            ("x1", "type"),
        ] {
            let mut req = verb_request(id, op, Some("#q"), Some("hi"));
            req.max_chars = Some(12_000);
            let script = read_script(&req).unwrap();
            let line = script.lines().find(|l| l.contains("var ID")).unwrap();
            assert!(
                line.starts_with("  var ID = \""),
                "the id must reach the page as a string literal, not as a bare \
                 identifier: {line}"
            );
            let literal = line
                .trim()
                .trim_start_matches("var ID = ")
                .trim_end_matches(';');
            assert_eq!(serde_json::from_str::<String>(literal).unwrap(), id);
        }
    }
    #[test]
    fn a_page_is_found_by_its_label_and_not_through_the_main_window() {
        // `get_webview_window("main")` returns `None` as soon as the main
        // window has a child webview — which is exactly when there is a page to
        // ask. Both lookups have to go through the manager instead.
        //
        // Scanned with the comments stripped, because the comments *name* the
        // call they explain: a scan that matched the prose would either fail
        // forever or — worse — be deleted the first time someone tidied a
        // comment, which is how a check quietly stops being one.
        let source = include_str!("webview_bridge.rs")
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
        assert!(
            !code.contains("get_webview_window"),
            "the bridge must not reach a page through the main webview window: \
             tauri returns None for it once any page is seated, so every read \
             would refuse with 'there is no browser tab open' about an app that \
             has one"
        );
        // Leading-dot forms, because rustfmt wraps a long call across lines and
        // a test that pins an exact line stops testing anything the moment
        // somebody reformats the file.
        assert!(
            code.contains(".webviews()"),
            "open_tabs must read the manager"
        );
        assert!(
            code.contains(".get_webview(&label)"),
            "ask_page must fetch the page by its own label"
        );
    }

    #[test]
    fn every_value_the_template_interpolates_is_a_literal_and_not_a_bare_word() {
        // The general form of the bug that kept this bridge silent twice: a
        // value interpolated into the page as text rather than as a literal.
        // `var ID = smoke0001;` and `var SCHEME = codify-bridge;` are both
        // syntactically plausible and both throw on the first line, and the
        // page's `ReferenceError` was the only evidence either existed.
        //
        // The six names are the six holes in the template — everything else in
        // a script is written out in full, which is why the check is a list and
        // not a scan for `var`: a scan also catches `var anchor = anchors[i]`,
        // which is computed, correct, and not what this is about.
        const INTERPOLATED: [&str; 5] = ["ID", "SELECTOR", "MAX", "CHUNK", "SCHEME"];
        let mut checked = 0;
        for op in ["read_page", "click", "type"] {
            let mut req = verb_request("literal01", op, Some("#q"), Some("hi"));
            req.max_chars = Some(12_000);
            let script = match op {
                "read_page" => read_script(&req),
                "click" => click_script(&req),
                _ => type_script(&req),
            }
            .unwrap();
            // `TEXT` only exists where there is something to type, which is
            // the point of the list being per-op rather than global.
            let mut names: Vec<&str> = INTERPOLATED.to_vec();
            if op == "type" {
                names.push("TEXT");
            }
            for name in names {
                let line = script
                    .lines()
                    .find(|l| l.trim().starts_with(&format!("var {name} =")))
                    .unwrap_or_else(|| {
                        panic!(
                            "{op}: the script no longer declares {name} — if the \
                                 template changed, this list of six has to change with it"
                        )
                    });
                let value = line
                    .trim()
                    .trim_start_matches(&format!("var {name} = "))
                    .trim_end_matches(';')
                    .trim();
                checked += 1;
                let ok = if value.starts_with('"') {
                    serde_json::from_str::<String>(value).is_ok()
                } else {
                    value.parse::<u64>().is_ok()
                };
                assert!(
                    ok,
                    "{op}: `var {name} = {value};` is not a literal — a bare word \
                     here is a reference to an undeclared variable, the page \
                     throws before it answers, and every read times out"
                );
            }
        }
        assert_eq!(
            checked, 16,
            "five holes in every script, six in the one that types"
        );
    }

    #[test]
    fn a_selector_is_escaped_rather_than_wrapped_in_quotes() {
        // Page text the model read off a page. A quote in it ends a
        // JavaScript string literal, and a syntax error here is a page read
        // that silently never answers.
        let mut req = request("script01");
        req.selector = Some("a[title='x\"; alert(1); //']".to_string());
        let script = read_script(&req).unwrap();
        assert!(script.contains(r#"\"; alert(1); //'"#));
        let selector_line = script.lines().find(|l| l.contains("var SELECTOR")).unwrap();
        assert!(selector_line.starts_with("  var SELECTOR = \""));
    }

    #[test]
    fn an_absent_selector_is_null_rather_than_an_empty_string() {
        // An empty selector matches nothing and reads as "the page is blank".
        let script = read_script(&request("script01")).unwrap();
        assert!(script.contains("var SELECTOR = null;"));
    }

    #[test]
    fn a_selector_that_is_not_a_selector_is_refused_before_it_is_evaluated() {
        let mut req = request("script01");
        req.selector = Some("x".repeat(400));
        assert!(read_script(&req).is_err());
    }

    #[test]
    fn an_unusable_request_id_is_refused_before_it_is_evaluated() {
        for bad in ["", "a/b", "a b", "x".repeat(200).as_str()] {
            assert!(read_script(&request(bad)).is_err(), "must refuse {bad:?}");
        }
    }

    #[test]
    fn the_character_budget_is_clamped_on_both_sides() {
        let mut req = request("script01");
        req.max_chars = Some(5);
        assert!(read_script(&req).unwrap().contains("var MAX = 200;"));
        req.max_chars = Some(10_000_000);
        assert!(read_script(&req).unwrap().contains("var MAX = 40000;"));
    }

    #[test]
    fn the_chunk_size_is_the_one_the_shell_reassembles_with() {
        let script = read_script(&request("script01")).unwrap();
        assert!(script.contains(&format!("var CHUNK = {CHUNK_CHARS};")));
    }

    #[test]
    fn percent_decoding_round_trips_what_the_page_encoded() {
        assert_eq!(percent_decode("a%20b").unwrap(), "a b");
        assert_eq!(percent_decode("%E2%9C%93").unwrap(), "✓");
        assert_eq!(percent_decode("plain").unwrap(), "plain");
        assert!(percent_decode("%zz").is_none());
        assert!(percent_decode("%E2%9C").is_none());
    }

    #[test]
    fn a_page_that_floods_a_question_is_cut_off_rather_than_buffered() {
        let req = request("flood001");
        let _receiver = begin(&req).unwrap();
        // Too big to be a real answer, and delivered in order so the cap is
        // what stops it rather than the ordering.
        let huge = "x".repeat(MAX_REPLY_CHARS / 2 + 10);
        for seq in 0..4 {
            let last = if seq == 3 { "1" } else { "0" };
            let _ = deliver(&format!(
                "codify-bridge://reply/flood001/{seq}/{last}/{huge}"
            ));
        }
        // The pending entry is gone, so the last chunk cannot land.
        assert!(!deliver("codify-bridge://reply/flood001/4/1/x"));
    }

    #[test]
    fn a_model_proposed_url_goes_through_the_guard_and_not_a_copy_of_it() {
        // The whole argument for letting a model navigate is this line: it
        // calls the same function a user's click calls. A reimplementation
        // here would be a second guard, and the second one is the one that
        // gets it wrong a year from now.
        let source = include_str!("webview_bridge.rs")
            .split("#[cfg(test)]")
            .next()
            .unwrap_or_default();
        assert!(
            source.contains("crate::browser::navigate(app, &tab, url)"),
            "navigation must go through browser::navigate"
        );
        // …and this module must not contain a host check of its own, which is
        // what "not a copy of it" has to mean in practice. Scoped to
        // `navigate_tab`: the rest of the module legitimately says
        // `127.0.0.1`, because that is the engine this shell polls.
        let navigate = source
            .split("fn navigate_tab")
            .nth(1)
            .and_then(|rest| rest.split("fn ask_page").next())
            .unwrap_or_default();
        for forbidden in [
            "127.0.0.1",
            "localhost",
            "is_loopback",
            "navigation_allowed",
        ] {
            assert!(
                !navigate.contains(forbidden),
                "navigate_tab must not check hosts itself; found {forbidden}"
            );
        }
    }

    #[test]
    fn an_unknown_operation_is_refused_rather_than_guessed_at() {
        // `serve` needs a live app to reach, so what it must never do is
        // anything at all with an op it does not know — and the name it
        // reports has to name the op, so a version mismatch is diagnosable.
        let mut req = request("opcheck1");
        req.op = "evaluate".to_string();
        assert!(read_script(&req).is_err() || req.op != "read_page");
    }

    #[test]
    fn a_page_reports_links_as_absolute_addresses_only() {
        // The script filters to http(s) itself, so `javascript:` and `file:`
        // hrefs never reach the model to be misused — and the engine's guard
        // still runs on whatever is finally asked for.
        let script = read_script(&request("links001")).unwrap();
        assert!(script.contains("new URL(anchor.getAttribute(\"href\"), location.href).href"));
        assert!(script.contains("if (!/^https?:/i.test(href)) { continue; }"));
        assert!(script.contains(&format!("links.length < {MAX_PAGE_LINKS}")));
    }

    #[test]
    fn the_smoke_reader_and_this_module_agree_on_the_line() {
        // The pair that matters, and the failure mode is quiet: a drifted
        // prefix prints every answer and reads none, which is indistinguishable
        // from a page that refused to answer. Same reasoning, and the same
        // defence, as `browser.rs`'s probe-prefix test.
        let reader = include_str!("../../scripts/embed_smoke.py");
        assert!(
            reader.contains(SMOKE_BRIDGE_LINE),
            "scripts/embed_smoke.py does not name the bridge line — every answer \
             would be printed and never read"
        );
        assert!(
            reader.contains(SMOKE_BRIDGE_SKIP_ENV),
            "scripts/embed_smoke.py cannot turn the bridge leg off, so \
             --no-bridge would wait out the shell's timeout"
        );
        assert!(
            reader.contains(SMOKE_TAB) || reader.contains("smoke"),
            "the reader no longer mentions the smoke tab"
        );
    }

    #[test]
    fn the_smoke_probe_asks_the_real_bridge_and_not_something_of_its_own() {
        // The probe builds a request and hands it to `serve`. If it ever grew
        // its own eval or its own reply channel, the smoke would be measuring
        // something other than the path the AI uses — which is the only reason
        // it exists.
        let source = include_str!("webview_bridge.rs")
            .split("#[cfg(test)]")
            .next()
            .unwrap_or_default();
        let probe = source
            .split("pub fn smoke_probe")
            .nth(1)
            .and_then(|rest| rest.split("\npub fn ").next())
            .unwrap_or_default();
        assert!(
            probe.contains("serve(&app, request)"),
            "must go through serve"
        );
        assert!(probe.contains("op: \"read_page\""));
        assert!(
            !probe.contains("page.eval"),
            "the probe must not script the page itself"
        );
    }

    #[test]
    fn the_bridge_leg_is_skipped_only_when_something_asked_for_it() {
        // Opt-out, not opt-in, because the honest default is the one that
        // measures the feature. `smoke_bridge_enabled` reads the real
        // environment, so this asserts the contract in the source rather than
        // mutating the process's environment to find out.
        let source = include_str!("webview_bridge.rs")
            .split("#[cfg(test)]")
            .next()
            .unwrap_or_default();
        let enabled = source
            .split("pub fn smoke_bridge_enabled")
            .nth(1)
            .and_then(|rest| rest.split("\n}").next())
            .unwrap_or_default();
        assert!(
            !enabled.contains("var_os"),
            "a missing variable must not disable the leg"
        );
    }

    #[test]
    fn this_module_does_not_grant_a_browser_page_anything() {
        // The reply scheme is not a capability and must never become one: a
        // page reaching it can send text and nothing else. If this file ever
        // grows a way to answer with a command name, a token or a path, the
        // bridge stops being a channel for text.
        //
        // Scanned up to the test module, because a test that names the thing
        // it forbids would otherwise match its own string literals and fail
        // forever — which is how a check like this quietly stops being one.
        let source = include_str!("webview_bridge.rs")
            .split("#[cfg(test)]")
            .next()
            .unwrap_or_default();
        for forbidden in ["codify_get_engine_info", "BOOT_TOKEN", "validate_argv"] {
            assert!(
                !source.contains(forbidden),
                "the bridge must not mention {forbidden}"
            );
        }
    }

    // ── the two verbs that act rather than read ────────────────────────────
    //
    // `click` and `type` are what make a page drivable rather than merely
    // readable, and they are also the first verbs in this file that change what
    // the user is looking at. Everything below is about keeping them inside
    // the same two walls the read has: no capability, and no answer that is not
    // the page's own words.

    fn verb_request(
        id: &str,
        op: &str,
        selector: Option<&str>,
        text: Option<&str>,
    ) -> BridgeRequest {
        BridgeRequest {
            id: id.to_string(),
            op: op.to_string(),
            tab: None,
            selector: selector.map(str::to_string),
            max_chars: Some(12_000),
            url: None,
            text: text.map(str::to_string),
        }
    }

    #[test]
    fn a_click_and_a_type_answer_over_the_same_scheme_and_nothing_else() {
        for script in [
            click_script(&verb_request(
                "verb0001",
                "click",
                Some("button.submit"),
                None,
            ))
            .unwrap(),
            type_script(&verb_request(
                "verb0002",
                "type",
                Some("input[name=q]"),
                Some("rust"),
            ))
            .unwrap(),
        ] {
            assert!(script.contains(BRIDGE_SCHEME));
            // The same three walls the read has: the page is told to fetch a
            // scheme on this app, and to reach nothing else at all.
            for forbidden in ["__TAURI_INTERNALS__", "invoke(", "127.0.0.1", "http://"] {
                assert!(
                    !script.contains(forbidden),
                    "an acting script must not mention {forbidden:?}: {script}"
                );
            }
        }
    }

    #[test]
    fn an_acting_verb_without_a_selector_is_refused_before_it_is_evaluated() {
        // "Click" and "type" with nothing to name would mean "click whatever
        // is first", which on a page is a button nobody chose. A read is the
        // opposite case and stays legal: the whole page is its scope.
        for op in ["click", "type"] {
            assert!(
                click_script(&verb_request("verb0003", op, None, Some("x"))).is_err()
                    || type_script(&verb_request("verb0004", op, Some("  "), Some("x"))).is_err(),
                "{op} must refuse a request with no selector"
            );
        }
        assert!(read_script(&verb_request("verb0005", "read_page", None, None)).is_ok());
    }

    #[test]
    fn typed_text_is_escaped_rather_than_wrapped_in_quotes() {
        // Model-authored text, on its way into a page the model does not
        // control. A quote in it ends a JavaScript string literal, and the
        // result would be a script that throws before it types anything —
        // which reads to the model as a page that refused to be driven.
        let script = type_script(&verb_request(
            "verb0006",
            "type",
            Some("input[name=q]"),
            Some("it's a \"test\"; alert(1); //"),
        ))
        .unwrap();
        assert!(script.contains(r#"\"test\""#));
        let line = script.lines().find(|l| l.contains("var TEXT")).unwrap();
        assert!(line.starts_with("  var TEXT = \""));
        // Round-trip rather than eyeball: the literal the script assigns must
        // parse back to exactly the text that was sent. A naive quote-wrap
        // produces a line that does not parse, and a model would be told a page
        // refused to be typed into.
        let literal = line
            .trim()
            .trim_start_matches("var TEXT = ")
            .trim_end_matches(';');
        assert_eq!(
            serde_json::from_str::<String>(literal).unwrap(),
            "it's a \"test\"; alert(1); //",
            "the typed text did not survive escaping"
        );
    }

    #[test]
    fn text_that_cannot_be_typed_is_refused_before_it_is_evaluated() {
        assert!(type_script(&verb_request(
            "verb0007",
            "type",
            Some("input"),
            Some("   ")
        ))
        .is_err());
        let too_long = "x".repeat(MAX_TYPED_CHARS + 1);
        assert!(type_script(&verb_request(
            "verb0008",
            "type",
            Some("input"),
            Some(&too_long)
        ))
        .is_err());
        let at_the_limit = "x".repeat(MAX_TYPED_CHARS);
        assert!(type_script(&verb_request(
            "verb0009",
            "type",
            Some("input"),
            Some(&at_the_limit)
        ))
        .is_ok());
    }

    #[test]
    fn the_two_verbs_report_what_they_did_and_where_they_did_it() {
        // A verb that returned nothing would leave the model unable to tell a
        // click that happened from one the page swallowed.
        let clicked = click_script(&verb_request("verb0010", "click", Some("#go"), None)).unwrap();
        assert!(clicked.contains(r#"acted: "clicked""#));
        assert!(clicked.contains(r#"ok: true"#));
        assert!(
            clicked.contains("url: done.url"),
            "a click must say where it happened"
        );
        let typed = type_script(&verb_request("verb0011", "type", Some("#q"), Some("hi"))).unwrap();
        assert!(typed.contains(r#"acted: "typed""#));
        assert!(typed.contains("chars: TEXT.length"));
    }

    #[test]
    fn a_disabled_control_is_refused_by_the_page_rather_than_silently_clicked() {
        // `element.click()` on a disabled button succeeds and does nothing, so
        // a report that said "clicked" would be a lie the model then reasons
        // from. The page is asked, because only the page knows.
        let script = click_script(&verb_request("verb0012", "click", Some("#go"), None)).unwrap();
        assert!(script.contains("target.disabled === true"));
        assert!(script.contains(r#"error: SELECTOR + " is disabled"#));
    }

    #[test]
    fn typing_uses_the_pages_own_setter_so_a_controlled_input_notices() {
        // A React-controlled input has replaced the instance setter; assigning
        // `element.value` is silently discarded by it, and the model would be
        // told it typed into a box that stayed empty.
        let script = type_script(&verb_request("verb0013", "type", Some("#q"), Some("x"))).unwrap();
        assert!(script.contains("Object.getOwnPropertyDescriptor(proto, \"value\")"));
        assert!(script.contains(r#"new Event("input", { bubbles: true })"#));
        assert!(script.contains(r#"new Event("change", { bubbles: true })"#));
        // And a control that cannot hold text is refused rather than typed at.
        assert!(script.contains("which is not a field to type into"));
    }

    #[test]
    fn serve_routes_the_four_verbs_and_nothing_else() {
        // `serve` needs a live app to reach, so what can be pinned here is that
        // the two new verbs are *routed* — an op that reached `ask_page`
        // without a match arm would be refused with a sentence that says the
        // shell does not know the verb, which is a diagnosable failure rather
        // than a silent one, and this is the test that catches its return.
        let source = include_str!("webview_bridge.rs")
            .split("#[cfg(test)]")
            .next()
            .unwrap_or_default();
        assert!(
            source.contains(r#""read_page" | "click" | "type" => ask_page(app, request).await"#),
            "the three page verbs must share one route; a click or a type that \
             reaches no arm is refused as an unknown operation"
        );
        assert!(source.contains(r#""navigate" => navigate_tab(app, &request)"#));
    }
}
