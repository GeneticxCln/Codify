// Prevents additional console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod browser;
mod engine_log;
mod engine_protocol;
mod terminal;

use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tauri::{AppHandle, Manager, State};
use tokio::sync::Mutex;
// The one wait in the exit path is a future it awaits, not a thread it blocks:
// see `stop_engine` for what a blocking version cost.
use std::future::Future;
use std::pin::Pin;

// ── Engine process state ────────────────────────────────────────────────────

/// Shared state: engine token + port discovered from stdout.
///
/// The `Child` lives here too: the exit handler has to be able to kill the
/// engine when the app closes, and it can only do that through a handle that
/// outlives the task that spawned it.
///
/// `problem` is the third thing the window needs and the two above cannot express:
/// "not ready yet" and "never going to be ready" looked identical from outside, so
/// the launcher's own diagnosis had nowhere to land and the UI showed a red dot
/// forever. It is `Some` exactly when a failure has been recorded.
///
/// `log` is the fourth: the engine's own stderr, tailed. `problem` can only say
/// what the launcher knows; when the engine said it itself — a traceback, and
/// above all the bounded-shutdown backstop announcing that it gave up waiting
/// and exited anyway — that is the explanation, and it used to go to a terminal
/// the window user may never see. An `Arc` rather than a value because the reader
/// task feeds it while the window reads it (`engine_log::EngineLog`).
///
/// `hard_exit_s` is the engine's own bound on a stop request, announced on the
/// handshake. It is cleared with the token and port, because it describes that
/// process: an engine that has exited cannot be waited for.
#[derive(Default)]
pub struct EngineState {
    pub token: Option<String>,
    pub port: Option<u16>,
    pub child: Option<tokio::process::Child>,
    pub problem: Option<String>,
    pub log: Arc<engine_log::EngineLog>,
    pub hard_exit_s: Option<f64>,
}

type SharedEngineState = Arc<Mutex<EngineState>>;

/// The open terminals. A `std` mutex rather than a `tokio` one: every use here is
/// a short synchronous section and none of it is held across an await, so a
/// `tokio` lock would only add a scheduler dependency.
type SharedTerminals = Arc<std::sync::Mutex<terminal::Terminals>>;

// ── Ending the app, whichever way it is asked to ─────────────────────────────

/// The exit path's bound on waiting for the engine state lock: 500 x 10ms.
const ENGINE_LOCK_TRIES: u32 = 500;
const ENGINE_LOCK_WAIT: std::time::Duration = std::time::Duration::from_millis(10);

/// How often the stop path looks to see whether the engine has gone. Small because
/// this is the whole cost of the grace in the normal case: the engine is gone
/// within a few hundred milliseconds and the loop only notices on its next look.
const ENGINE_STOP_POLL: std::time::Duration = std::time::Duration::from_millis(50);

/// Slack added to the engine's announced bound before this shell escalates.
///
/// The announcement is what the engine *intends*, not a guarantee it reaches: it is
/// a timer thread racing a shutdown that may be one scheduling hiccup behind, and a
/// final `os._exit` that has to flush two streams first. Without slack the shell
/// would kill an engine a moment before its own deadline, which is precisely the
/// interrupted turn this path exists to record.
const STOP_GRACE_SLACK: std::time::Duration = std::time::Duration::from_millis(1500);

/// The bound to use when the engine announced none — an older build, or a handshake
/// line whose `hard_exit_s` was nonsense. The engine's own default is 6s, so this is
/// that plus the same slack: long enough to let a bounded shutdown finish, short
/// enough that a wedged process is not waited on indefinitely.
const DEFAULT_STOP_GRACE: std::time::Duration = std::time::Duration::from_millis(7_500);

/// Claimed once, by whichever exit path gets there first.
static SHUTDOWN_CLAIMED: AtomicBool = AtomicBool::new(false);

/// What stopping the engine actually did, so the log can say which it was.
#[derive(Debug, PartialEq, Eq)]
pub enum EngineStop {
    /// Asked with `SIGTERM`, and it was gone inside its own bound.
    Graceful,
    /// Asked, and still there when its bound expired. Killed.
    Killed,
    /// No child to stop: the engine never started, or it had already exited.
    NothingToStop,
    /// The state lock stayed held for the whole bound. The engine's own bound is
    /// what covers this case now — see `docs/04` §6.1.
    LockUnavailable,
}

/// The first caller does the work; every later one is told it is already done.
///
/// Two paths reach `release_children` and they can both fire for one exit: the
/// signal path cleans up and then asks Tauri to quit, and that quit delivers the
/// `RunEvent::Exit` a closed window would have produced. Running it twice is
/// harmless in effect and noisy in fact — a second bounded wait for a lock, a second
/// "Engine process stopped" for an engine that is already gone — so the second
/// caller leaves.
fn claim_shutdown(claimed: &AtomicBool) -> bool {
    !claimed.swap(true, Ordering::SeqCst)
}

/// Close the terminals and stop the engine, once, whoever asked.
///
/// Async because the wait has to keep the runtime turning — see [`stop_engine`].
async fn release_children(app: &AppHandle) {
    if !claim_shutdown(&SHUTDOWN_CLAIMED) {
        return;
    }
    // The terminals go first, and unconditionally. They are the user's own shells,
    // and one left running after the window goes is a stray process holding the
    // workspace directory — the same defect the engine kill below exists to prevent.
    // It runs before the lock dance on purpose: the engine lock may never be
    // acquired, and a shell leak must not depend on that.
    terminal::close_all(&app.state::<SharedTerminals>());
    match stop_engine(&app.state::<SharedEngineState>()).await {
        EngineStop::Graceful => println!("[Codify] Engine stopped inside its own bound"),
        EngineStop::Killed => println!(
            "[Codify] Engine outlived its shutdown bound — killed, so whatever it had \
             not recorded is lost"
        ),
        EngineStop::NothingToStop => {}
        EngineStop::LockUnavailable => eprintln!(
            "[Codify] engine state lock still held at exit — the engine will have to \
             notice this process is gone on its own (docs/04 §6.1)"
        ),
    }
}

/// Stop the engine: ask first, wait out its own bound, kill only if it outlasts it.
///
/// `SIGKILL` on the way out used to be the whole of this, on the grounds that a
/// closing window should not wait. That was right about the *wait* and wrong about
/// the *kill*: a killed engine writes nothing on its way out, so a goal that was
/// RUNNING stays RUNNING in the database with no event saying why, and the only
/// repair is the *next* boot's rescue — a post-mortem rather than a record, with a
/// window of lying state in between. Asking first costs a wait the engine bounds
/// itself, and buys the record at the moment it happens: a real engine asked this
/// way was gone in 0.19s with the interrupted run already marked FAILED.
///
/// **Async, and it must stay that way.** The first version of this slept with
/// `std::thread::sleep`, and the live run of it was a self-inflicted wound: on the
/// signal path this runs as a tokio task, so a sleeping worker starved the very
/// tasks the wait depends on — the reaper that collects the child and the reader
/// that sees its pipe close. The engine exited in a fifth of a second, sat
/// unreaped as a zombie for the whole bound, and answered `kill(pid, 0)` the entire
/// time, so the shell waited 7.5s and then killed a process that had been gone since
/// before the first nap. Yielding between looks is not politeness here; it is the
/// mechanism.
///
/// Three things make the wait honest rather than a new way to hang:
///
/// * the bound is the engine's own, read from its boot handshake
///   (`hard_exit_s`), not a number written down twice;
/// * the shell watches for the *exit*, not for silence, and stops as soon as the
///   process is gone — normally a few hundred milliseconds, not the bound;
/// * the escalation is unconditional afterwards, so a process that ignores
///   `SIGTERM` is still killed on a deadline, which is what `docs/04` §6.1 promises.
///
/// The state lock is released before any of that: nothing about signalling another
/// process needs the app's state, and holding the lock for the grace would stall
/// every in-flight IPC call for as long as the engine takes to leave.
async fn stop_engine(shared: &SharedEngineState) -> EngineStop {
    let Some(guard) = bounded_lock(shared).await else {
        return EngineStop::LockUnavailable;
    };
    let Some(pid) = guard.child.as_ref().and_then(|child| child.id()) else {
        return EngineStop::NothingToStop;
    };
    let grace = stop_grace(guard.hard_exit_s);
    drop(guard);

    if !signal_process(pid, libc::SIGTERM) {
        // No such process: it exited between the id being read and the signal being
        // sent, which is the ordinary state of an engine that has already crashed.
        return EngineStop::Graceful;
    }
    println!(
        "[Codify] Asked the engine to stop (SIGTERM) — up to {:.1}s before SIGKILL",
        grace.as_secs_f64()
    );
    let state = shared.clone();
    await_exit(
        grace,
        || !engine_has_gone(process_alive(pid), engine_reported_gone(&state)),
        |nap| Box::pin(tokio::time::sleep(nap)),
        || {
            let _ = signal_process(pid, libc::SIGKILL);
        },
    )
    .await
}

/// Whether the engine is gone, by either of the two things that know.
///
/// `alive` is `kill(pid, 0)`; `reported_gone` is the stdout reader having seen the
/// pipe close. Gone is *either* of them — and getting that direction wrong is not a
/// subtle bug, it is the difference between a quit that takes a moment and a quit
/// that waits out the whole bound and then kills a corpse. The first live run of
/// this path combined them the other way round ("alive or reported gone", a
/// predicate that is almost always true) and duly waited 7.5s for an engine that
/// had left 0.15s after the signal, SIGKILLed its unreaped zombie, and reported
/// `Killed` for a shutdown that had actually been graceful.
///
/// The second signal is not redundant: a dead child that nobody has reaped yet
/// still answers signal 0, and only the reader knows the difference between a
/// process and the absence of one.
fn engine_has_gone(alive: bool, reported_gone: bool) -> bool {
    !alive || reported_gone
}

/// The engine's state lock, or `None` if it stayed held for the whole bound.
///
/// Bounded rather than skipped on purpose: every holder takes the lock across a
/// short synchronous section, so a bounded wait lands, and the old `try_lock →
/// return` gave up — leaking a stray engine holding the port and the database —
/// whenever another task happened to hold the lock at that instant.
async fn bounded_lock(
    shared: &SharedEngineState,
) -> Option<tokio::sync::MutexGuard<'_, EngineState>> {
    for _ in 0..ENGINE_LOCK_TRIES {
        if let Ok(guard) = shared.try_lock() {
            return Some(guard);
        }
        tokio::time::sleep(ENGINE_LOCK_WAIT).await;
    }
    None
}

/// How long to let the engine take, from what it announced on the handshake.
///
/// `announced` is already filtered by the parser (finite, positive), which is what
/// makes `from_secs_f64` safe here: it panics on a negative or non-finite value, and
/// the one place that could feed it garbage is the one place that cannot.
fn stop_grace(announced: Option<f64>) -> std::time::Duration {
    match announced {
        Some(seconds) => std::time::Duration::from_secs_f64(seconds) + STOP_GRACE_SLACK,
        None => DEFAULT_STOP_GRACE,
    }
}

/// Wait out the grace, escalating only if the engine is still there at the end.
///
/// `still_there`, `wait` and `force` are injected so this is a function a test can
/// drive exactly, and `wait` is a *future* rather than a sleep: the loop has to
/// yield to the runtime between looks, because the reaper that tells us the child
/// is gone runs on that runtime. `await_exit` is the one place the wait exists, so
/// there is no second, blocking version to drift back into.
///
/// The loop checks before it waits, which is what makes the boundary count as
/// graceful: an engine that finished on the last nap is found gone by the look that
/// follows it, and killing it would credit the kill with a shutdown that had
/// already happened.
async fn await_exit(
    grace: std::time::Duration,
    mut still_there: impl FnMut() -> bool,
    mut wait: impl FnMut(std::time::Duration) -> Pin<Box<dyn Future<Output = ()> + Send>>,
    mut force: impl FnMut(),
) -> EngineStop {
    let mut waited = std::time::Duration::ZERO;
    loop {
        if !still_there() {
            return EngineStop::Graceful;
        }
        if waited >= grace {
            force();
            return EngineStop::Killed;
        }
        wait(ENGINE_STOP_POLL).await;
        waited += ENGINE_STOP_POLL;
    }
}

/// Whether the stdout reader has already seen the engine's pipe close.
///
/// The reader watches stdout for the life of the process and clears the connection
/// info at EOF, so "no token, no port" means the process is gone — the second of
/// the two answers [`engine_has_gone`] combines. It is a `try_lock` because this is
/// called from the wait loop: it must never be the thing that makes the wait slow,
/// and a lock it cannot take is a signal it does not have rather than one to wait
/// for.
fn engine_reported_gone(shared: &SharedEngineState) -> bool {
    shared
        .try_lock()
        .is_ok_and(|state| state.token.is_none() && state.port.is_none())
}

/// Send a signal, reporting whether there was a process there to receive it.
///
/// `false` means `ESRCH`: no such process. That is a normal answer on this path —
/// the engine may have exited a moment ago — and not an error worth logging.
fn signal_process(pid: u32, signal: i32) -> bool {
    // SAFETY: `kill` is handed a pid and a signal and reads neither of ours; no
    // pointer into this process is involved, and every argument is a plain integer
    // this function has already range-checked by type.
    unsafe { libc::kill(pid as libc::pid_t, signal) == 0 }
}

/// Whether a pid still names a process — a zombie included, deliberately.
///
/// Signal 0 asks the question without sending anything, and it is the only liveness
/// question available without owning the child's reaping. The zombie case is why
/// [`engine_reported_gone`] exists and why this is never the only answer.
fn process_alive(pid: u32) -> bool {
    signal_process(pid, 0)
}

/// Turn a termination signal into the same deliberate exit a closed window takes.
///
/// A signal never runs Tauri's exit path. SIGTERM's default disposition is to end the
/// process where it stands, so on a keybind the window went, the terminals were left
/// to whatever their PTY happened to do, and the engine was left to notice that its
/// parent was gone. That last part is a backstop working as designed (`docs/04`
/// §6.1); it is not the shell keeping its own promise, and it takes up to a second
/// and a half longer than doing it here.
///
/// tokio's signal streams rather than `libc::signal`: what tokio installs writes to a
/// pipe, and the work happens on a runtime thread, so this may touch the app's state
/// and call into Tauri at all — none of which a signal handler may do.
///
/// SIGHUP is deliberately absent. A terminal that launched the app may have been
/// started with SIGHUP ignored, and a handler installed over an inherited `SIG_IGN`
/// would make closing that terminal kill an app that was deliberately detached.
/// SIGTERM and SIGINT are requests to stop, with no such second reading.
fn watch_shutdown_signals(app: AppHandle) {
    use tokio::signal::unix::{signal, SignalKind};
    for (name, kind) in [
        ("SIGTERM", SignalKind::terminate()),
        ("SIGINT", SignalKind::interrupt()),
    ] {
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            let Ok(mut stream) = signal(kind) else {
                return;
            };
            // `recv` resolves only when the signal arrives, which is the point: until
            // something asks the app to stop, this task does nothing at all.
            if stream.recv().await.is_none() {
                return;
            }
            eprintln!("[Codify] {name} — closing the terminals and stopping the engine");
            release_children(&app).await;
            // Then the ordinary exit: `AppHandle::exit` emits the same
            // `RunEvent::Exit` a closed window does, so the window, the webview and
            // the compositor's idea of this surface all come down the normal path. And
            // if the runtime cannot honour that, it exits the process itself rather
            // than leaving a shell with no engine behind it.
            app.exit(0);
        });
    }
}

// ── Serde types for Tauri commands ─────────────────────────────────────────

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct AgentConfig {
    pub role: String,
    pub display_name: String,
    pub provider: String,
    pub protocol: String,
    pub model_name: String,
    pub api_key_ref: Option<String>,
    pub base_url: Option<String>,
    pub system_prompt_override: Option<String>,
    pub temperature: f64,
    pub max_tokens: u32,
    // The role's second target. These have to be here, not just in the engine's
    // schema: this struct is what deserializes the frontend's patch, so a field
    // missing from it is dropped silently — the card would say "Saved" about a
    // fallback the engine never received.
    pub fallback_provider: Option<String>,
    pub fallback_model_name: String,
    pub fallback_protocol: Option<String>,
    pub fallback_base_url: Option<String>,
    pub updated_at: f64,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct AgentConfigPatch {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub protocol: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub api_key: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub base_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub system_prompt_override: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u32>,
    // Removing a fallback sends an empty provider rather than null: through this
    // struct a `null` and an absent field are the same thing, and "no fallback"
    // must be distinguishable from "leave it alone". The engine reads an empty
    // slug as "clear the fallback", null as "no change".
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fallback_provider: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fallback_model_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fallback_protocol: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fallback_base_url: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct EngineInfo {
    pub port: u16,
    pub token: String,
}

/// What the shell knows about the engine's lifecycle. `error` is the launcher's
/// diagnosis, or `None` while it is still starting or has succeeded.
#[derive(Debug, Serialize, Deserialize)]
pub struct EngineStatus {
    pub running: bool,
    pub port: Option<u16>,
    pub error: Option<String>,
}

// ── Helper: build engine base URL ──────────────────────────────────────────

async fn engine_url(state: &SharedEngineState) -> Result<(String, String), String> {
    let s = state.lock().await;
    match (&s.token, &s.port) {
        (Some(token), Some(port)) => Ok((format!("http://127.0.0.1:{}", port), token.clone())),
        _ => Err("Engine not ready".to_string()),
    }
}

// ── Tauri commands ──────────────────────────────────────────────────────────

/// Surface engine errors as engine errors. Without this, a 401/404/409/5xx body
/// dies inside `resp.json()` as "error decoding response body" — the UI then
/// shows a decode bug where the engine cleanly reported, say, a version
/// conflict. Pass every response through here before decoding.
async fn check_engine(resp: reqwest::Response) -> Result<reqwest::Response, String> {
    let status = resp.status();
    if status.is_success() {
        return Ok(resp);
    }
    let body = resp.text().await.unwrap_or_default();
    Err(format!("engine returned {status}: {body}"))
}

/// A registered workspace's root directory, asked for rather than supplied.
///
/// This is the whole security shape of the terminal. The client names a
/// workspace *id*; the shell asks the engine what directory that workspace is,
/// and `terminal::pin_cwd` refuses anything that is not an existing absolute
/// directory. A caller that could send a path could point a user's shell at
/// anywhere on the machine, and the engine's own record is the only authority on
/// what the workspace is. An unknown id is an error here rather than a shell in
/// the app's own launch directory.
async fn workspace_root_for(
    engine: &SharedEngineState,
    workspace_id: &str,
) -> Result<String, String> {
    let (base, token) = engine_url(engine).await?;
    let resp = reqwest::Client::new()
        .get(format!("{base}/workspaces/{workspace_id}"))
        .header("Authorization", format!("Bearer {token}"))
        .send()
        .await
        .map_err(|e| format!("engine unreachable: {e}"))?;
    let checked = check_engine(resp).await?;
    let body: serde_json::Value = checked
        .json()
        .await
        .map_err(|e| format!("bad workspace response: {e}"))?;
    body.get("root_path")
        .and_then(|v| v.as_str())
        .map(str::to_string)
        .ok_or_else(|| "workspace has no root_path recorded".to_string())
}

/// Open a terminal in a registered workspace.
///
/// The workspace is resolved through the engine (see [`workspace_root_for`]) and
/// the directory is pinned by [`terminal::pin_cwd`]. This is the user's own
/// shell: it does not go through the engine's `SandboxService`, which is the
/// agent's privileged path and must stay that way (docs/00 §6.6).
#[tauri::command]
async fn codify_terminal_open(
    engine: State<'_, SharedEngineState>,
    terminals: State<'_, SharedTerminals>,
    app: tauri::AppHandle,
    workspace_id: String,
    cols: u16,
    rows: u16,
) -> Result<String, String> {
    let root = workspace_root_for(&engine, &workspace_id).await?;
    terminal::open(app, &terminals, Some(&root), cols, rows)
}

#[tauri::command]
async fn codify_terminal_write(
    terminals: State<'_, SharedTerminals>,
    terminal_id: String,
    data: String,
) -> Result<(), String> {
    terminal::write(&terminals, &terminal_id, &data)
}

#[tauri::command]
async fn codify_terminal_resize(
    terminals: State<'_, SharedTerminals>,
    terminal_id: String,
    cols: u16,
    rows: u16,
) -> Result<(), String> {
    terminal::resize(&terminals, &terminal_id, cols, rows)
}

#[tauri::command]
async fn codify_terminal_close(
    terminals: State<'_, SharedTerminals>,
    terminal_id: String,
) -> Result<(), String> {
    terminal::close(&terminals, &terminal_id)
}

/// Open a browser webview for a tab (or bring the existing one forward).
///
/// The webview runs the page with an empty capability set and a loopback URL
/// guard — both enforced and asserted in [`browser`], which owns the whole
/// boundary. This command exists only so the main window can ask for a tab;
/// the browser webview itself cannot invoke it, or anything else.
#[tauri::command]
async fn codify_browser_open(
    app: tauri::AppHandle,
    tab_id: String,
    url: String,
) -> Result<String, String> {
    browser::open(&app, &tab_id, &url)
}

/// Navigate an open browser tab. Guarded twice: here for a refusal the UI can
/// show, and in `on_navigation` for enforcement no caller routes around.
#[tauri::command]
async fn codify_browser_navigate(
    app: tauri::AppHandle,
    tab_id: String,
    url: String,
) -> Result<String, String> {
    browser::navigate(&app, &tab_id, &url)
}

/// Close a browser tab's webview.
#[tauri::command]
async fn codify_browser_close(app: tauri::AppHandle, tab_id: String) -> Result<String, String> {
    browser::close(&app, &tab_id)
}

/// Return the current engine connection info so the UI can build its HTTP client.
#[tauri::command]
async fn codify_get_engine_info(state: State<'_, SharedEngineState>) -> Result<EngineInfo, String> {
    let s = state.lock().await;
    match (&s.token, &s.port) {
        (Some(token), Some(port)) => Ok(EngineInfo {
            port: *port,
            token: token.clone(),
        }),
        _ => Err("Engine not yet started".to_string()),
    }
}

/// Why the engine is not running, in the shell's own words.
///
/// `codify_get_engine_info` can only ever say "not yet started" — which is exactly
/// what the window already concluded from its retries, so the whole failure showed
/// up as an unexplained red pill. This hands over the launcher's recorded reason
/// (no checkout under the working directory, no readable stdout pipe, a process
/// that exited before its handshake) so the banner can name the fix instead.
#[tauri::command]
async fn codify_engine_status(state: State<'_, SharedEngineState>) -> Result<EngineStatus, String> {
    let s = state.lock().await;
    Ok(EngineStatus {
        running: s.token.is_some() && s.port.is_some(),
        port: s.port,
        error: s.problem.clone(),
    })
}

/// The engine's own last words: the tail of its stderr, oldest first.
///
/// The launcher can say *that* an engine failed — no checkout, no handshake, a
/// port it never reported — but not *why one that had been running stopped*, and
/// the why is the part worth reading. A turn cancelled mid-flight leaves a
/// websocket open, uvicorn waits for it, and the bounded-shutdown backstop ends
/// the process with one line on stderr: `shutdown unfinished after 6s — exiting
/// anyway`. That line used to go wherever this shell's stderr was pointed, which
/// for someone using the app is nowhere they will look.
///
/// Read on demand rather than pushed: the interesting moment is the engine
/// stopping, and the reader task already keeps the tail up to that point. So the
/// window asks once, when it notices, and pays nothing while the engine is
/// healthy.
///
/// The state lock is released before the log's own lock is taken, so no lock is
/// ever held across an await and the reader is never blocked by a read.
#[tauri::command]
async fn codify_engine_log(
    state: State<'_, SharedEngineState>,
    limit: Option<u16>,
) -> Result<Vec<String>, String> {
    const DEFAULT_LINES: usize = 40;
    // `Result` is the shape Tauri requires of an async command that takes
    // references, not a claim that reading a buffer can fail.
    let log = { state.lock().await.log.clone() };
    Ok(log.tail(limit.map_or(DEFAULT_LINES, |l| l as usize)))
}

/// List all eight agent configs from the engine.
#[tauri::command]
async fn codify_list_agent_configs(
    state: State<'_, SharedEngineState>,
    http: State<'_, reqwest::Client>,
) -> Result<Vec<AgentConfig>, String> {
    let (base, token) = engine_url(&state).await?;
    let resp = http
        .get(format!("{}/settings/agents", base))
        .bearer_auth(&token)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    check_engine(resp)
        .await?
        .json::<Vec<AgentConfig>>()
        .await
        .map_err(|e| e.to_string())
}

/// Update a single agent config by role.
#[tauri::command]
async fn codify_update_agent_config(
    role: String,
    patch: AgentConfigPatch,
    state: State<'_, SharedEngineState>,
    http: State<'_, reqwest::Client>,
) -> Result<AgentConfig, String> {
    let (base, token) = engine_url(&state).await?;
    engine_protocol::valid_role(&role)?;
    let resp = http
        .put(format!("{}/settings/agents/{}", base, role))
        .bearer_auth(&token)
        .json(&patch)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    check_engine(resp)
        .await?
        .json::<AgentConfig>()
        .await
        .map_err(|e| e.to_string())
}

/// Point every role that cannot run at a model the engine has discovered.
///
/// The decision is the engine's (`engine/role_repair.py`): this command only
/// forwards it, so the desktop app and the standalone browser build cannot
/// disagree about which roles count as broken.
#[tauri::command]
async fn codify_repair_agent_configs(
    state: State<'_, SharedEngineState>,
    http: State<'_, reqwest::Client>,
) -> Result<serde_json::Value, String> {
    let (base, token) = engine_url(&state).await?;
    let resp = http
        .post(format!("{}/settings/agents/repair", base))
        .bearer_auth(&token)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    check_engine(resp)
        .await?
        .json::<serde_json::Value>()
        .await
        .map_err(|e| e.to_string())
}

/// Test connectivity to an agent's provider endpoint.
#[tauri::command]
async fn codify_test_agent_connection(
    role: String,
    state: State<'_, SharedEngineState>,
    http: State<'_, reqwest::Client>,
) -> Result<serde_json::Value, String> {
    let (base, token) = engine_url(&state).await?;
    engine_protocol::valid_role(&role)?;
    let resp = http
        // BUG-03 fix: correct endpoint is /test-connection not /test
        .post(format!("{}/settings/agents/{}/test-connection", base, role))
        .bearer_auth(&token)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    check_engine(resp)
        .await?
        .json::<serde_json::Value>()
        .await
        .map_err(|e| e.to_string())
}

// ── Engine subprocess launcher ─────────────────────────────────────────────

/// How long the login shell gets to report its PATH. An rc file that starts a version
/// manager costs a second or so; anything beyond this is not worth holding the engine
/// back for — and if the interpreter then cannot be found, the window says so
/// (`codify_engine_status`) rather than the app hanging on a shell prompt.
const LOGIN_SHELL_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(5);

/// Ask the user's login shell what PATH it has — `(shell, path)`.
///
/// A window launched from a `.desktop` file (or the Finder) inherits the session's
/// environment, not the one the user's shell builds: `~/.local/bin`, a version
/// manager's shims, a Homebrew prefix are put on the PATH by the shell's rc files and
/// are simply absent here. The engine is spawned as a bare `python3 -m engine`, and
/// its sandbox and git helper resolve their commands through the same PATH it
/// inherits — so a PATH without `/usr/bin` is not a degraded engine, it is no engine
/// at all. Asking once, here, is what makes the interpreter, `git` and the verifier's
/// commands agree with the terminal.
///
/// `-i` as well as `-l`: PATH edits live in `.zshrc`/`.bashrc` at least as often as in
/// the login-only files, and it is the terminal's environment being reproduced here.
/// `None` when there is no `$SHELL` to ask, the shell cannot be started, or it does
/// not answer in time — the caller then keeps the PATH it already has.
async fn login_shell_path() -> Option<(String, String)> {
    use std::process::Stdio;

    // POSIX-only: there is no login-shell convention to ask on Windows, and its PATH
    // separator is `;`, which neither the probe nor `merge_path` speaks. Compiled
    // rather than `cfg`-ed out so the two halves of this feature stay checked on
    // every platform.
    if cfg!(not(unix)) {
        return None;
    }

    let shell = std::env::var("SHELL")
        .ok()
        .filter(|s| !s.trim().is_empty())?;
    let probe = engine_protocol::login_path_probe();
    // `-ilc` is not portable (`dash` has no `-l`), so a shell that prints no answer to
    // the first form is asked the plain interactive way instead of being written off.
    for flags in [["-ilc", probe.as_str()], ["-ic", probe.as_str()]] {
        let mut command = tokio::process::Command::new(&shell);
        command
            .args(flags)
            // An interactive shell must not be able to read from a terminal it does
            // not have. stdout is the answer; stderr is rc-file chatter, dropped
            // rather than mistaken for a report.
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            // An rc file that waits forever takes the shell down with the timeout
            // instead of leaking it.
            .kill_on_drop(true);
        match tokio::time::timeout(LOGIN_SHELL_TIMEOUT, command.output()).await {
            Ok(Ok(output)) => {
                let stdout = String::from_utf8_lossy(&output.stdout);
                if let Some(path) = engine_protocol::parse_login_path(&stdout) {
                    return Some((shell, path));
                }
            }
            // The shell itself could not be started: no other flag spelling will fix
            // that, and the inherited PATH is still there to try.
            Ok(Err(error)) => {
                eprintln!(
                    "[Codify] Could not ask {shell} for its PATH ({error}); keeping the inherited PATH"
                );
                return None;
            }
            Err(_) => {
                eprintln!(
                    "[Codify] {shell} did not report its PATH within {}s; keeping the inherited PATH",
                    LOGIN_SHELL_TIMEOUT.as_secs()
                );
                return None;
            }
        }
    }
    None
}

/// How many times to try before giving up, and how long to wait between tries.
///
/// Three, a second apart. The failure worth retrying is a *transient* one — the
/// port band exhausted by another engine, an interpreter that had not finished
/// importing, a machine still coming out of suspend — and all of those clear in
/// seconds. A fourth attempt is not optimism, it is a hang.
const ENGINE_LAUNCH_ATTEMPTS: u32 = 3;
const ENGINE_LAUNCH_RETRY_MS: u64 = 1_000;

/// What one launch attempt managed. Only one of these is worth trying again.
#[derive(PartialEq)]
enum LaunchOutcome {
    /// The engine handed over a port, and this call returned when it later exited.
    Started,
    /// It ran and died without a handshake: transient, so try again.
    NoHandshake,
    /// It was never going to start — no checkout, no `python3`. The reason is
    /// already recorded, and retrying it would only bury that reason in noise.
    Unlaunchable,
}

/// Keep trying until the engine is up, or the attempts run out.
///
/// This used to be a single attempt in `setup`, which made one transient failure
/// permanent: `codify_get_engine_info` answers `Err("Engine not yet started")`
/// forever after, the window retries its IPC ten times, gives up, and shows a
/// red pill for the rest of the session — with the port free again thirty
/// seconds later. A launcher that cannot retry is a launcher that turns a blip
/// into a restart of the app.
///
/// Not retried: an engine that ran and then exited. That is a crash, and
/// restarting the app is a decision the user should make, not one this makes for
/// them in a loop.
async fn launch_engine(shared: SharedEngineState) {
    for attempt in 1..=ENGINE_LAUNCH_ATTEMPTS {
        match launch_engine_once(shared.clone()).await {
            LaunchOutcome::Started | LaunchOutcome::Unlaunchable => return,
            LaunchOutcome::NoHandshake => {
                if attempt < ENGINE_LAUNCH_ATTEMPTS {
                    eprintln!(
                        "[Codify] Engine launch attempt {attempt}/{} never completed its \
                         handshake — retrying in {ENGINE_LAUNCH_RETRY_MS}ms",
                        ENGINE_LAUNCH_ATTEMPTS
                    );
                    tokio::time::sleep(std::time::Duration::from_millis(ENGINE_LAUNCH_RETRY_MS))
                        .await;
                }
            }
        }
    }
    eprintln!("[Codify] Engine did not come up after {ENGINE_LAUNCH_ATTEMPTS} attempts");
}

/// Spawn the Python engine and parse its boot handshake:
/// `CODIFY_ENGINE token=<hex> port=<int>`. One attempt; `launch_engine` decides
/// whether to make another.
async fn launch_engine_once(shared: SharedEngineState) -> LaunchOutcome {
    use std::process::Stdio;
    use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};

    let project_root = std::env::current_dir()
        .map(|cwd| engine_protocol::project_root_from(&cwd))
        .unwrap_or_else(|_| std::path::PathBuf::from("."));

    // Started outside the checkout, `python3 -m engine` cannot import anything: it
    // would come up only to print a traceback on a stderr no window user sees and
    // exit. Record the reason and skip the doomed spawn — a guaranteed-failing child
    // adds nothing but noise, and the diagnosis is what the window needs.
    if let Some(problem) = engine_protocol::launch_problem(&project_root) {
        eprintln!("[Codify] {problem}");
        shared.lock().await.problem = Some(problem);
        return LaunchOutcome::Unlaunchable;
    }

    // Resolve the interpreter through the login shell's PATH, not this process's —
    // see `login_shell_path`. This is the first thing that needs it: a GUI launch can
    // reach this point with no `python3` on PATH at all.
    let engine_path = match login_shell_path().await {
        Some((shell, login_path)) => {
            let inherited = std::env::var("PATH").unwrap_or_default();
            let (merged, added) = engine_protocol::merge_path(&inherited, &login_path);
            // One line either way. "0 added" is an answer — the shell was asked and
            // had nothing to contribute — and it is a different story from the shell
            // that never answered (the two branches above, which say so outright).
            eprintln!(
                "[Codify] Login shell {shell}: {added} PATH entr{} added ({} total)",
                if added == 1 { "y" } else { "ies" },
                merged.split(':').count()
            );
            Some(merged)
        }
        None => None,
    };

    // Locate `python3` on PATH — fall back gracefully.
    let mut engine = tokio::process::Command::new("python3");
    engine
        .args(["-m", "engine"])
        .current_dir(&project_root)
        .env("PYTHONPATH", &project_root)
        // The parent-death contract: the engine watches this pid and exits with it.
        // The exit handler below cannot cover every way this process ends — a signal
        // never runs it, and `kill_on_drop` needs Rust to drop the handle — so the
        // orphan left holding the port and the database has to be able to notice on
        // its own (see `engine/watchdog.py`).
        .env("CODIFY_PARENT_PID", std::process::id().to_string())
        .stdout(Stdio::piped())
        // Piped, not inherited — see the reader spawned below. Inheriting threw
        // away the one diagnostic that explains an engine that exits: its own
        // last words, which for a bounded shutdown is the backstop saying it
        // stopped waiting and left anyway.
        .stderr(Stdio::piped())
        // Belt: if this handle is ever dropped unexpectedly, the engine dies
        // with it instead of surviving as a stray holding the port and DB.
        .kill_on_drop(true);
    if let Some(path) = &engine_path {
        engine.env("PATH", path);
    }
    let mut child = match engine.spawn() {
        Ok(c) => c,
        Err(e) => {
            // The OS error is the whole story here (usually: no `python3` on PATH),
            // and it is more specific than anything this shell could add to it.
            let problem = format!("Could not start the engine: {e}");
            eprintln!("[Codify] {problem}");
            shared.lock().await.problem = Some(problem);
            return LaunchOutcome::NoHandshake;
        }
    };

    // The pipe was requested above (`Stdio::piped`), so `take()` can only fail if
    // the handle was already taken — either way there is no handshake to read.
    let Some(stdout) = child.stdout.take() else {
        let problem = "The engine started but exposed no stdout, so its handshake could \
                       never be read — the app has nothing to connect to."
            .to_string();
        eprintln!("[Codify] {problem}");
        shared.lock().await.problem = Some(problem);
        return LaunchOutcome::NoHandshake;
    };
    // Suspenders: park the handle where the exit handler can reach it.
    //
    // The engine's stderr is tailed before the child is parked, because the
    // reader needs the pipe and the pipe only exists while we still own the
    // child. It reads to EOF — the whole life of the process, not just the boot
    // — so the lines a crash leaves behind are still there when the window asks.
    let stderr_log = {
        let s = shared.lock().await;
        // This attempt's stderr replaces the previous run's: a banner quoting
        // the last words of an engine that is no longer this one would be
        // confidently wrong.
        s.log.clear();
        s.log.clone()
    };
    if let Some(stderr) = child.stderr.take() {
        tauri::async_runtime::spawn(async move {
            let mut reader = BufReader::new(stderr);
            let mut chunk = [0u8; 4096];
            loop {
                match reader.read(&mut chunk).await {
                    // EOF: the engine is gone, and whatever it managed to say is
                    // now the whole diagnosis. A read error ends the reader too —
                    // continuing would spin on a pipe that is broken — and says so
                    // rather than leaving a truncated tail looking complete.
                    Ok(0) => break,
                    Err(e) => {
                        eprintln!("[Codify] engine stderr reader stopped: {e}");
                        break;
                    }
                    Ok(n) => {
                        for line in stderr_log.feed(&chunk[..n]) {
                            // Exactly the bytes `Stdio::inherit` printed, so a
                            // person watching a terminal loses nothing.
                            eprintln!("{line}");
                        }
                    }
                }
            }
        });
    } else {
        eprintln!("[Codify] engine exposed no stderr — its last words will not reach the app");
    }
    {
        let mut s = shared.lock().await;
        s.child = Some(child);
    }
    let mut lines = BufReader::new(stdout).lines();

    let mut handshake_done = false;
    while let Ok(Some(line)) = lines.next_line().await {
        if !handshake_done {
            if let Some(handshake) = engine_protocol::parse_handshake(&line) {
                let mut s = shared.lock().await;
                s.token = Some(handshake.token);
                s.port = Some(handshake.port);
                // The engine's own bound on a stop request, kept so the exit path
                // can wait exactly as long as it says and no less.
                s.hard_exit_s = handshake.hard_exit_s;
                // Reached the handshake, so any earlier doubt is resolved. Harmless
                // today (one launcher, one attempt) and load-bearing the moment
                // anything retries the launch.
                s.problem = None;
                let port = handshake.port;
                println!("[Codify] Engine ready on port {port}");
                handshake_done = true;
                // No `break` here. Breaking drops this reader end of the pipe,
                // and a later stdout write from the engine then dies with EPIPE
                // (SIGPIPE kills a Python process outright). Keep draining —
                // the lines are simply no longer parsed.
            }
        }
    }

    // The loop above ends only when the pipe closes — i.e. the engine process
    // exited. The connection info it handed out is now a lie, so clear it:
    // without this, the UI keeps a stale port/token and reports confusing
    // connection errors instead of a clean "engine is down".
    {
        let mut s = shared.lock().await;
        if s.token.is_some() || s.port.is_some() {
            println!("[Codify] Engine process exited — clearing connection info");
        } else {
            // Ended its life without ever handing over a port. This is the failure
            // that used to be indistinguishable from "still starting": record it, so
            // the window can name the command that reproduces the engine's output
            // instead of leaving a red pill standing as the whole explanation.
            let problem = engine_protocol::engine_exited_problem(&project_root);
            eprintln!("[Codify] {problem}");
            s.problem = Some(problem);
        }
        s.token = None;
        s.port = None;
        s.hard_exit_s = None;
    }

    // No `child.wait()` here: the handle lives in shared state and tokio's
    // reaper collects the process in the background. Waiting on a child we no
    // longer own would just pin this task forever.
    if handshake_done {
        LaunchOutcome::Started
    } else {
        LaunchOutcome::NoHandshake
    }
}

// ── Entry point ────────────────────────────────────────────────────────────

/// Which display backend this launch should use, given the environment.
///
/// `None` means "say nothing", and it is the answer in two different cases that
/// must not be conflated: an X11 session, where GTK's own default is correct, and
/// a `GDK_BACKEND` the user set themselves, which is a decision this shell has no
/// business overruling.
///
/// The case that matters is the third one. GTK chooses its backend from the
/// environment, and under a Wayland compositor with Xwayland running — which is
/// every niri, sway or GNOME session with an X client on it — that choice is a
/// coin flip decided inside GTK's build and version. Landing on X11 makes this
/// window an X client inside a Wayland session: Xwayland draws a title bar for
/// it, and a tiling compositor tiles a window whose decorations the app is not
/// going to draw. Naming the backend removes the coin flip; the session being
/// Wayland is the only condition, so an X11 session is left exactly as it was.
fn display_backend(wayland_display: Option<&str>, requested: Option<&str>) -> Option<&'static str> {
    if requested.is_some() {
        return None;
    }
    if wayland_display.is_some() {
        return Some("wayland");
    }
    None
}

/// Apply [`display_backend`] to this process, before GTK is initialised.
///
/// Called first thing in [`run`], because GTK reads `GDK_BACKEND` when it
/// initialises — on the main thread, inside `Builder::build()` — and a variable
/// set after that is a variable nothing reads. It has to be this process: the
/// window is created here, and a child (the engine, a terminal) inherits the
/// variable and is welcome to ignore it.
fn apply_display_backend() {
    let requested = std::env::var("GDK_BACKEND").ok();
    let wayland = std::env::var("WAYLAND_DISPLAY").ok();
    match display_backend(wayland.as_deref(), requested.as_deref()) {
        Some(backend) => {
            std::env::set_var("GDK_BACKEND", backend);
            println!(
                "[Codify] Display backend {backend} (chosen here: a Wayland compositor is \
                 running and nothing set GDK_BACKEND)"
            );
        }
        // Named even when nothing was decided, because "which backend am I on"
        // is not answerable from inside the window: an X client and a Wayland
        // client look identical once the compositor has drawn them, and "the
        // title bar is back" is otherwise a bug with no cause on the launch.
        // An X11 session stays silent — GTK's default there is not news.
        None => {
            if let Some(backend) = requested.filter(|b| !b.trim().is_empty()) {
                println!("[Codify] Display backend {backend} (from GDK_BACKEND)");
            }
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    apply_display_backend();
    let engine_state: SharedEngineState = Arc::new(Mutex::new(EngineState::default()));
    let state_clone = engine_state.clone();

    tauri::Builder::default()
        // Registered first, and it has to be: the plugin claims the app's session-bus
        // name while `Builder::build()` runs, and the app's own `setup` — the place
        // below that starts the engine — only runs later, on `RunEvent::Ready`. A
        // second launch therefore exits inside `build()`, before it can spawn a rival
        // engine on the next free port that would then fight the first one over the
        // same database. The callback runs in the instance that *keeps* running, so
        // all it has to do is reveal the window the user was asking for.
        //
        // On Linux the guard is a D-Bus name: without a session bus there is nothing
        // to claim against, and the plugin degrades to "no guard" rather than
        // refusing to start (its own match swallows the connection error).
        .plugin(tauri_plugin_single_instance::init(|app, argv, cwd| {
            // Said out loud, because "nothing happened" is this path's whole failure
            // mode: a second launch that does not raise the window is
            // indistinguishable from a second launch that was ignored. `argv`/`cwd`
            // are what the launch carried (a file to open, the directory it came
            // from) — nothing consumes them yet, and the line is where you would see
            // them if something did.
            eprintln!(
                "[Codify] Second launch from {cwd} ({argv:?}) — revealing the running window"
            );
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .manage(engine_state)
        // One shared HTTP client for every engine-bridge command: `Client` is
        // cheaply cloneable over an internal connection pool, while
        // `Client::new()` per call re-resolves and re-handshakes every time.
        .manage(reqwest::Client::new())
        .manage(SharedTerminals::new(std::sync::Mutex::new(
            terminal::Terminals::default(),
        )))
        .setup(move |app| {
            // Ending deliberately is the shell's own job, whichever way it is asked:
            // a closed window reaches the exit handler below, a signal reaches this.
            watch_shutdown_signals(app.handle().clone());
            // Launch engine asynchronously so the window appears immediately.
            let shared = state_clone.clone();
            tauri::async_runtime::spawn(async move {
                launch_engine(shared).await;
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            codify_get_engine_info,
            codify_engine_status,
            codify_engine_log,
            codify_list_agent_configs,
            codify_update_agent_config,
            codify_repair_agent_configs,
            codify_test_agent_connection,
            codify_terminal_open,
            codify_terminal_write,
            codify_terminal_resize,
            codify_terminal_close,
            codify_browser_open,
            codify_browser_navigate,
            codify_browser_close,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Codify application")
        .run(|app, event| {
            // The engine is our child process: when the app goes, it goes.
            // Without this, closing Codify left a stray `python3 -m engine`
            // holding the port, the DB, and any writes it was mid-way through.
            // A signal gets here too, through `watch_shutdown_signals` — so a quit
            // that is not a closed window is the same deliberate exit, not a
            // different accident.
            if let tauri::RunEvent::Exit = event {
                // `block_on`, because this handler is not a future and the wait is:
                // the engine's exit is noticed by tasks on the runtime, so the future
                // has to be driven to completion before the process goes. The main
                // thread is not a runtime worker, so driving it here costs the event
                // loop — which is already tearing down — and nothing else. On the
                // signal path the same work runs as a task and simply awaits.
                tauri::async_runtime::block_on(release_children(app));
            }
        });
}

#[cfg(test)]
mod tests {
    use super::{
        await_exit, claim_shutdown, display_backend, engine_has_gone, stop_engine, stop_grace,
        EngineState, EngineStop, SharedEngineState, DEFAULT_STOP_GRACE, ENGINE_STOP_POLL,
        STOP_GRACE_SLACK,
    };
    use std::sync::Arc;
    use std::time::Duration;
    use tokio::sync::Mutex;

    #[test]
    fn a_wayland_session_gets_the_wayland_backend() {
        assert_eq!(display_backend(Some("wayland-1"), None), Some("wayland"));
    }

    #[test]
    fn an_x11_session_is_left_alone() {
        // No compositor, no opinion. This is the case that must not regress:
        // forcing Wayland onto a machine that has none is a window that never
        // appears, which is worse than the title bar.
        assert_eq!(display_backend(None, None), None);
    }

    #[test]
    fn an_explicit_gdk_backend_wins() {
        // The user's environment is a decision, not a default to improve on —
        // and it is also how the choice is undone without a rebuild.
        assert_eq!(display_backend(Some("wayland-1"), Some("x11")), None);
    }

    #[test]
    fn the_cleanup_runs_once_however_many_paths_reach_it() {
        // A closed window and a signal can both arrive for one exit, and the second
        // has to be a no-op rather than a second bounded wait for a lock that nothing
        // is holding and a second "stopped" line for an engine already gone.
        let claimed = std::sync::atomic::AtomicBool::new(false);
        assert!(claim_shutdown(&claimed), "the first caller does the work");
        assert!(!claim_shutdown(&claimed), "the second is told it is done");
    }

    #[test]
    fn an_engine_that_never_started_is_not_a_reason_to_wait() {
        // A signal during boot: the launcher may not have spawned anything yet, and
        // the exit path still has to come back promptly rather than burn its whole
        // bound finding nothing to kill. One second against a five-second bound.
        let shared: SharedEngineState = Arc::new(Mutex::new(EngineState::default()));
        let started = std::time::Instant::now();
        let outcome = tauri::async_runtime::block_on(stop_engine(&shared));
        assert_eq!(EngineStop::NothingToStop, outcome);
        assert!(
            started.elapsed() < std::time::Duration::from_secs(1),
            "an uncontended lock must land on the first try, not after the bound"
        );
    }

    /// Run the stop policy against a scripted engine, and report what it did.
    ///
    /// `polls_before_exit` is how many looks answer "still here" before the process
    /// is gone; `None` means it never goes. The naps are *real* ones — the same
    /// `tokio::time::sleep` the exit path uses — so these tests also prove the wait
    /// yields rather than blocking, which is the half that was once wrong.
    fn scripted_stop(
        grace: Duration,
        polls_before_exit: Option<usize>,
    ) -> (EngineStop, usize, usize) {
        let mut looks = 0usize;
        let mut naps = 0usize;
        let mut kills = 0usize;
        let outcome = tauri::async_runtime::block_on(await_exit(
            grace,
            || {
                looks += 1;
                match polls_before_exit {
                    Some(after) => looks <= after,
                    None => true,
                }
            },
            |nap| {
                naps += 1;
                Box::pin(tokio::time::sleep(nap))
            },
            || kills += 1,
        ));
        (outcome, naps, kills)
    }

    #[test]
    fn an_engine_that_leaves_when_asked_is_never_killed() {
        // The whole point of the change: SIGTERM, and the engine records what it
        // was doing on the way out. One kill here would be a turn cut off mid-write.
        let (outcome, naps, kills) = scripted_stop(Duration::from_secs(7), Some(2));
        assert_eq!(EngineStop::Graceful, outcome);
        assert_eq!(2, naps, "the wait ends as soon as the engine is gone");
        assert_eq!(0, kills, "an engine that stopped politely is not killed");
    }

    #[test]
    fn an_engine_that_ignores_the_request_is_killed_after_the_grace() {
        let (outcome, naps, kills) = scripted_stop(Duration::from_millis(200), None);
        assert_eq!(EngineStop::Killed, outcome);
        assert_eq!(4, naps, "the whole grace is waited before the escalation");
        assert_eq!(1, kills, "escalation happens exactly once");
    }

    #[test]
    fn an_engine_that_has_already_gone_is_not_waited_for_at_all() {
        // The ordinary case when closing the app after a crash: the process is
        // gone, so the grace has nothing to measure.
        let (outcome, naps, kills) = scripted_stop(Duration::from_secs(7), Some(0));
        assert_eq!(EngineStop::Graceful, outcome);
        assert_eq!(
            0, naps,
            "a dead engine must not cost the closing window a nap"
        );
        assert_eq!(0, kills);
    }

    #[test]
    fn an_engine_that_exits_on_the_boundary_counts_as_having_stopped() {
        // It finished inside the bound it announced, so it is a graceful stop even
        // though the last look happened after the last nap. The alternative is
        // crediting a kill with a shutdown that had already happened.
        let (outcome, _naps, kills) = scripted_stop(ENGINE_STOP_POLL * 2, Some(2));
        assert_eq!(EngineStop::Graceful, outcome);
        assert_eq!(0, kills);
    }

    #[test]
    fn a_grace_of_nothing_skips_straight_to_the_kill() {
        // Degenerate, and the reason the escalation cannot be forgotten: with no
        // room to wait, the only correct outcome is the one that ends the process.
        let (outcome, naps, kills) = scripted_stop(Duration::ZERO, None);
        assert_eq!(EngineStop::Killed, outcome);
        assert_eq!(0, naps);
        assert_eq!(1, kills);
    }
    #[test]
    fn an_engine_the_reader_saw_leave_is_gone_even_while_its_pid_answers() {
        // The regression, in the shape it happened. The shell combined its two
        // liveness signals the wrong way round, so an engine that had left — and
        // whose unreaped pid still answered signal 0 — read as alive for the whole
        // bound, and the shell waited 7.5s and killed a zombie.
        assert!(
            engine_has_gone(false, false),
            "no process and no report: gone"
        );
        assert!(
            engine_has_gone(true, true),
            "the reader saw the pipe close; an unreaped pid is not a reason to wait"
        );
        assert!(engine_has_gone(false, true));
        assert!(
            !engine_has_gone(true, false),
            "a live engine nobody has reported gone is still here"
        );
    }

    #[test]
    fn an_engine_that_is_gone_by_either_signal_is_never_killed() {
        // The end-to-end shape of the same regression, through the real wait: dead
        // from the third look on, as a reaped process and a closed pipe would be.
        let mut looks = 0usize;
        let mut kills = 0usize;
        let outcome = tauri::async_runtime::block_on(await_exit(
            Duration::from_secs(7),
            || {
                looks += 1;
                !engine_has_gone(looks > 2, looks > 2)
            },
            |nap| Box::pin(tokio::time::sleep(nap)),
            || kills += 1,
        ));
        assert_eq!(EngineStop::Graceful, outcome, "it left; the wait must end");
        assert_eq!(0, kills, "and it must not be killed for leaving");
    }

    #[test]
    fn the_wait_yields_instead_of_blocking_the_thread_it_runs_on() {
        // The bug the first live run of this path found, pinned in the shape it
        // took. The wait used to be `std::thread::sleep` on a tokio worker, so the
        // reaper that collects the child and the reader that sees its pipe close
        // never ran: the engine left in a fifth of a second, sat unreaped for the
        // whole bound answering `kill(pid, 0)`, and the shell waited 7.5s before
        // SIGKILLing a process that had been gone since the first nap.
        //
        // A current-thread runtime, deliberately. A multi-thread one can hide a
        // blocking wait behind a spare worker, which is how this survived a code
        // review and only showed up when a person quit the app.
        use std::sync::atomic::{AtomicUsize, Ordering};
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .expect("a current-thread runtime with a timer");
        runtime.block_on(async {
            let ticks = Arc::new(AtomicUsize::new(0));
            let counter = ticks.clone();
            let ticker = tokio::spawn(async move {
                loop {
                    tokio::time::sleep(Duration::from_millis(5)).await;
                    counter.fetch_add(1, Ordering::SeqCst);
                }
            });
            let outcome = await_exit(
                Duration::from_millis(300),
                || true,
                |nap| Box::pin(tokio::time::sleep(nap)),
                || {},
            )
            .await;
            ticker.abort();
            assert_eq!(EngineStop::Killed, outcome);
            assert!(
                ticks.load(Ordering::SeqCst) > 0,
                "nothing else ran during the wait, so the wait is blocking the thread \
                 it is on — which starves the tasks that notice the engine is gone"
            );
        });
    }

    #[test]
    fn the_engines_own_bound_decides_how_long_the_shell_waits() {
        assert_eq!(
            Duration::from_secs(6) + STOP_GRACE_SLACK,
            stop_grace(Some(6.0)),
            "the announced bound plus slack, so a shutdown one hiccup behind still finishes"
        );
        assert_eq!(
            DEFAULT_STOP_GRACE,
            stop_grace(None),
            "an engine that announced nothing gets the engine's own default, plus slack"
        );
    }
}
