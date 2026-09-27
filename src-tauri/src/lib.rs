// Prevents additional console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod browser;
mod engine_protocol;
mod terminal;

use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tauri::{Manager, State};
use tokio::sync::Mutex;

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
#[derive(Default)]
pub struct EngineState {
    pub token: Option<String>,
    pub port: Option<u16>,
    pub child: Option<tokio::process::Child>,
    pub problem: Option<String>,
}

type SharedEngineState = Arc<Mutex<EngineState>>;

/// The open terminals. A `std` mutex rather than a `tokio` one: every use here is
/// a short synchronous section and none of it is held across an await, so a
/// `tokio` lock would only add a scheduler dependency.
type SharedTerminals = Arc<std::sync::Mutex<terminal::Terminals>>;

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
    use tokio::io::{AsyncBufReadExt, BufReader};

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
        .stderr(Stdio::inherit())
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
    {
        let mut s = shared.lock().await;
        s.child = Some(child);
    }
    let mut lines = BufReader::new(stdout).lines();

    let mut handshake_done = false;
    while let Ok(Some(line)) = lines.next_line().await {
        if !handshake_done {
            if let Some((token, port)) = engine_protocol::parse_handshake(&line) {
                let mut s = shared.lock().await;
                s.token = Some(token);
                s.port = Some(port);
                // Reached the handshake, so any earlier doubt is resolved. Harmless
                // today (one launcher, one attempt) and load-bearing the moment
                // anything retries the launch.
                s.problem = None;
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
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
            eprintln!("[Codify] Second launch from {cwd} ({argv:?}) — revealing the running window");
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
        .setup(move |_app| {
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
            if let tauri::RunEvent::Exit = event {
                // The terminals go first, and unconditionally. They are the
                // user's own shells, and one left running after the window goes
                // is a stray process holding the workspace directory — the same
                // defect the engine kill below exists to prevent. It runs before
                // the lock dance on purpose: the engine lock may never be
                // acquired, and a shell leak must not depend on that.
                {
                    let terms = app.state::<SharedTerminals>();
                    terminal::close_all(&terms);
                }
                let shared = app.state::<SharedEngineState>();
                // This kill is not optional — a silently-skipped kill leaks a
                // stray engine holding the port and DB. Every lock holder holds
                // it across a short synchronous section only, so a bounded wait
                // always lands; the old `try_lock → return` skipped the kill
                // whenever another task happened to hold the lock at exit.
                let mut guard = None;
                for _ in 0..500 {
                    if let Ok(g) = shared.try_lock() {
                        guard = Some(g);
                        break;
                    }
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
                let Some(mut guard) = guard else {
                    eprintln!("[Codify] engine state lock still held at exit — engine process may be leaked");
                    return;
                };
                if let Some(child) = guard.child.as_mut() {
                    let _ = child.start_kill();
                    println!("[Codify] Engine process stopped");
                }
            }
        });
}
