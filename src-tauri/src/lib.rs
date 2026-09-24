// Prevents additional console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

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
#[derive(Default)]
pub struct EngineState {
    pub token: Option<String>,
    pub port: Option<u16>,
    pub child: Option<tokio::process::Child>,
}

type SharedEngineState = Arc<Mutex<EngineState>>;

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

/// The role name goes into a URL path; a frontend bug (or a compromise) must
/// not be able to traverse into other engine endpoints via `..%2f`-style
/// sequences. Roles are lowercase slugs — anything else is refused here.
fn valid_role(role: &str) -> Result<(), String> {
    let ok = !role.is_empty()
        && role.len() <= 64
        && role
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-');
    if ok {
        Ok(())
    } else {
        Err("invalid role identifier".to_string())
    }
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

/// List all seven agent configs from the engine.
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
    valid_role(&role)?;
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
    valid_role(&role)?;
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

/// Spawn the Python engine and parse its boot handshake:
/// `CODIFY_ENGINE token=<hex> port=<int>`
async fn launch_engine(shared: SharedEngineState) {
    use std::process::Stdio;
    use tokio::io::{AsyncBufReadExt, BufReader};

    let project_root = std::env::current_dir()
        .map(|p| {
            if p.ends_with("src-tauri") {
                p.parent().unwrap_or(&p).to_path_buf()
            } else {
                p
            }
        })
        .unwrap_or_else(|_| std::path::PathBuf::from("."));

    // Locate `python3` on PATH — fall back gracefully.
    let mut child = match tokio::process::Command::new("python3")
        .args(["-m", "engine"])
        .current_dir(&project_root)
        .env("PYTHONPATH", &project_root)
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        // Belt: if this handle is ever dropped unexpectedly, the engine dies
        // with it instead of surviving as a stray holding the port and DB.
        .kill_on_drop(true)
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[Codify] Failed to launch engine: {e}");
            return;
        }
    };

    // The pipe was requested above (`Stdio::piped`), so `take()` can only fail if
    // the handle was already taken — either way there is no handshake to read.
    let Some(stdout) = child.stdout.take() else {
        eprintln!("[Codify] Engine spawned without a readable stdout pipe — cannot read handshake; engine state not parked");
        return;
    };
    // Suspenders: park the handle where the exit handler can reach it.
    {
        let mut s = shared.lock().await;
        s.child = Some(child);
    }
    let mut lines = BufReader::new(stdout).lines();

    let mut handshake_done = false;
    while let Ok(Some(line)) = lines.next_line().await {
        // Parse: CODIFY_ENGINE token=<hex> port=<int>
        if !handshake_done && line.starts_with("CODIFY_ENGINE") {
            let mut token = None;
            let mut port: Option<u16> = None;

            for part in line.split_whitespace().skip(1) {
                if let Some(v) = part.strip_prefix("token=") {
                    token = Some(v.to_string());
                } else if let Some(v) = part.strip_prefix("port=") {
                    port = v.parse().ok();
                }
            }

            if let (Some(t), Some(p)) = (token, port) {
                let mut s = shared.lock().await;
                s.token = Some(t);
                s.port = Some(p);
                println!("[Codify] Engine ready on port {p}");
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
        }
        s.token = None;
        s.port = None;
    }

    // No `child.wait()` here: the handle lives in shared state and tokio's
    // reaper collects the process in the background. Waiting on a child we no
    // longer own would just pin this task forever.
}

// ── Entry point ────────────────────────────────────────────────────────────

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let engine_state: SharedEngineState = Arc::new(Mutex::new(EngineState::default()));
    let state_clone = engine_state.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(engine_state)
        // One shared HTTP client for every engine-bridge command: `Client` is
        // cheaply cloneable over an internal connection pool, while
        // `Client::new()` per call re-resolves and re-handshakes every time.
        .manage(reqwest::Client::new())
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
            codify_list_agent_configs,
            codify_update_agent_config,
            codify_repair_agent_configs,
            codify_test_agent_connection,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Codify application")
        .run(|app, event| {
            // The engine is our child process: when the app goes, it goes.
            // Without this, closing Codify left a stray `python3 -m engine`
            // holding the port, the DB, and any writes it was mid-way through.
            if let tauri::RunEvent::Exit = event {
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
