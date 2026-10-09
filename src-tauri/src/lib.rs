mod browser;
mod engine_log;
mod engine_protocol;
mod machine;
mod terminal;
mod webview_bridge;

use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tauri::{AppHandle, Listener, Manager, State};
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
type SharedMachines = Arc<std::sync::Mutex<machine::Machines>>;

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
    machine::close_all(&app.state::<SharedMachines>());
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

// ── the render-starvation watchdog ──────────────────────────────────────────
//
// On a machine whose WebKitGTK composites in software (an Nvidia/Wayland
// session without a working dma-buf path), a full-window 30 FPS backdrop costs
// about a whole core *in this process* — and the cost lands on the GTK main
// loop, so the window stops answering input. The page cannot see the problem:
// its frames keep being delivered, its painter is cheap, and the burn is here.
// So the shell, the one party that pays, is the party that says so: it watches
// its own main-thread CPU, and when the app has been idle-and-burning long
// enough, it announces `codify:engine-render-starved` and the page stops the
// effects. See `ui/src/motionPreference.ts` for the other end.

/// The shell event the watchdog announces, and the payload it carries.
pub const RENDER_STARVED_EVENT: &str = "codify:engine-render-starved";

/** The thresholds, named once so a reader (and the freeze test) can judge them. */
pub mod render_watchdog {
    /// A sample is a fraction of one core spent by one thread in 1 s: the
    /// kernel counts 100 ticks per core-second (`CLK_TCK` on Linux).
    pub const SAMPLE_TICKS: u64 = 100;
    /// How much of a core, held across the window, is starvation: ~0.55. Below
    /// this a busy repaint is uncomfortable; above it the main loop is losing
    /// most of its time to the rasterizer and input is what dies first.
    pub const STARVE_NUMERATOR: u64 = 55;
    /// The burn must be sustained, but software rasterisation arrives in
    /// *bursts* — measured on the machine that froze, the main thread alternated
    /// ~3 s at ~90% of a core with ~3 s at ~34%, so a consecutive-streak rule
    /// missed the verdict by one second, twice. The rule is therefore windowed:
    /// this many hot samples out of the last [`HOT_WINDOW`] announce, which a
    /// page load (one spike) cannot reach and the burst pattern reaches on its
    /// second burst.
    pub const HOT_NEEDED: usize = 6;
    /// The window [`HOT_NEEDED`] is counted over, in 1 s samples.
    pub const HOT_WINDOW: usize = 10;

    /// One CPU sample of one thread, as ticks between two readings.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Sample {
        pub hot: bool,
        pub fraction_x100: u64,
    }

    /// Judge one interval: did this thread spend ≥ `STARVE_NUMERATOR`% of a
    /// core between `previous` and `now` ticks?
    pub fn sample(previous: u64, now: u64) -> Sample {
        let delta = now.saturating_sub(previous);
        let fraction_x100 = delta * 100 / SAMPLE_TICKS;
        Sample {
            hot: fraction_x100 >= STARVE_NUMERATOR,
            fraction_x100,
        }
    }

    /// The starvation verdict, from hot samples counted over a sliding window.
    ///
    /// A state machine rather than a counter in the loop, so the rule is a pure
    /// function a test can drive: [`HOT_NEEDED`] hot samples of the last
    /// [`HOT_WINDOW`] announce — exactly once, and then the machine stops for
    /// the rest of the boot.
    #[derive(Default)]
    pub struct Verdict {
        window: [bool; HOT_WINDOW],
        oldest: usize,
        filled: usize,
        pub announced: bool,
    }

    impl Verdict {
        pub fn observe(&mut self, hot: bool) -> bool {
            if self.announced {
                return false;
            }
            self.window[self.oldest] = hot;
            self.oldest = (self.oldest + 1) % HOT_WINDOW;
            self.filled = (self.filled + 1).min(HOT_WINDOW);
            if self.filled == HOT_WINDOW && self.window.iter().filter(|h| **h).count() >= HOT_NEEDED
            {
                self.announced = true;
                return true;
            }
            false
        }
    }
}

/// Watch this process's main thread and announce starvation, once, when found.
///
/// The verdict is deliberately *once per boot*, because false positives here
/// cost the user their backdrop for the whole session (the page keeps it), and
/// because announcing at most once is what makes the user's "animate anyway"
/// stick — a second announcement an hour later would override a choice the
/// user had already made.
///
/// This runs on the async runtime; reading `/proc` is a plain file read and the
/// emit is a one-way message, so nothing here needs the main thread it watches.
fn watch_render_starvation(app: tauri::AppHandle) {
    const WINDOW_SAMPLES: usize = 30;
    tauri::async_runtime::spawn(async move {
        let mut verdict = render_watchdog::Verdict::default();
        let mut previous: Option<u64> = None;
        // The first samples of a boot are the app starting — layout, the mount
        // storm, the engine handshake — and are not evidence of anything. The
        // verdict arms only after this long.
        const ARM_AFTER_MS: u64 = 5_000;
        let started = std::time::Instant::now();
        for _ in 0..WINDOW_SAMPLES {
            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
            let Some(ticks) = main_thread_cpu_ticks() else {
                continue;
            };
            let Some(prev) = previous.replace(ticks) else {
                continue; // the first reading establishes the baseline only
            };
            if started.elapsed().as_millis() < u128::from(ARM_AFTER_MS) {
                continue;
            }
            // A window nobody can see is not starving anybody: a minimised or
            // fully-occluded window still repaints, but there is no user under
            // the burn. `unwrap_or(true)`: if the answer cannot be had, count
            // the sample — a false positive needs the user to see it anyway.
            let visible = app
                .get_webview_window("main")
                .and_then(|w| w.is_visible().ok())
                .unwrap_or(true);
            let read = render_watchdog::sample(prev, ticks);
            let hot = visible && read.hot;
            if verdict.observe(hot) {
                println!(
                    "[Codify] render watchdog: this window is being rasterised in software \
                     (main thread hot on {} of the last {} sampled seconds) — asking \
                     the page to stop the animated backdrops",
                    render_watchdog::HOT_NEEDED,
                    render_watchdog::HOT_WINDOW,
                );
                use tauri::Emitter;
                let _ = app.emit(RENDER_STARVED_EVENT, read.fraction_x100);
                return; // once per boot
            }
        }
    });
}

/// The main thread's accumulated CPU ticks, from `/proc/self/stat` fields 14+15.
fn main_thread_cpu_ticks() -> Option<u64> {
    let stat = std::fs::read_to_string("/proc/self/stat").ok()?;
    // The comm field can contain spaces, so the parse starts after the final ')'.
    let after = stat.rsplit_once(")")?.1;
    let mut fields = after.split_whitespace();
    // Fields after the ')' run from field 3 (state is field 3, the first token
    // here), so utime — field 14 — is the 12th token, and stime follows it.
    let utime: u64 = fields.nth(11)?.parse().ok()?;
    let stime: u64 = fields.next()?.parse().ok()?;
    Some(utime + stime)
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
///
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

/// Open a machine on a registered workspace: a jailed shell the assistant may also type into.
///
/// Opening is the person's, and so is the network: `network` is chosen here and cannot be changed by
/// anything afterwards (`docs/09` §14). The workspace is resolved through the engine like a terminal's,
/// then refused if it would show a machine the person's secrets ([`machine::pin_workspace`]). There is
/// no fallback to an unjailed shell: if the jail cannot be made, this is an error with a reason.
#[tauri::command]
async fn codify_machine_open(
    engine: State<'_, SharedEngineState>,
    machines: State<'_, SharedMachines>,
    app: tauri::AppHandle,
    workspace_id: String,
    cols: u16,
    rows: u16,
    network: bool,
) -> Result<machine::OpenedMachine, String> {
    let root = workspace_root_for(&engine, &workspace_id).await?;
    machine::open(app, &machines, Some(&root), cols, rows, network)
}

/// Throw away what a machine has done and start it again from a clean project: the same project, the same
/// network, a new jail. It is the recovery for a machine that is wedged or was stopped for using too much,
/// and it can change nothing a person chose, because it remakes the machine from the recipe it was made
/// from (`docs/09` §14). The assistant reaches it through the window, as it reaches `codify_machine_write`.
#[tauri::command]
async fn codify_machine_reset(
    machines: State<'_, SharedMachines>,
    app: tauri::AppHandle,
    machine_id: String,
    cols: u16,
    rows: u16,
) -> Result<machine::OpenedMachine, String> {
    machine::reset(app, &machines, &machine_id, cols, rows)
}

#[tauri::command]
async fn codify_machine_write(
    machines: State<'_, SharedMachines>,
    machine_id: String,
    data: String,
) -> Result<(), String> {
    machine::write(&machines, &machine_id, &data)
}

#[tauri::command]
async fn codify_machine_resize(
    machines: State<'_, SharedMachines>,
    machine_id: String,
    cols: u16,
    rows: u16,
) -> Result<(), String> {
    machine::resize(&machines, &machine_id, cols, rows)
}

#[tauri::command]
async fn codify_machine_close(
    machines: State<'_, SharedMachines>,
    machine_id: String,
) -> Result<(), String> {
    machine::close(&machines, &machine_id)
}

/// Open (embed) a browser page for a tab, or navigate the one that exists.
///
/// The page runs with an empty capability set and behind the loopback URL
/// guard — both enforced and asserted in [`browser`], which owns the whole
/// boundary. The geometry arrives from the UI, which measures its own layout
/// in logical pixels; a child webview is placed where its pane says, not
/// where a guess about the window's chrome puts it. This command exists only
/// so the UI's webview can ask for a tab; a browser page itself cannot
/// invoke it, or anything else.
#[tauri::command]
async fn codify_browser_open(
    app: tauri::AppHandle,
    tab_id: String,
    url: String,
    bounds: browser::Bounds,
) -> Result<String, String> {
    browser::open(&app, &tab_id, &url, bounds)
}

/// Navigate an open browser page. Guarded twice: here for a refusal the UI
/// can show, and in `on_navigation` for enforcement no caller routes around.
#[tauri::command]
async fn codify_browser_navigate(
    app: tauri::AppHandle,
    tab_id: String,
    url: String,
) -> Result<String, String> {
    browser::navigate(&app, &tab_id, &url)
}

/// Show this browser tab's page and hide the others.
#[tauri::command]
async fn codify_browser_focus(app: tauri::AppHandle, tab_id: String) -> Result<String, String> {
    // Recorded before the focus, and recorded here because this is the only
    // moment the fact is known: `is_visible` is not on tauri 2.11's `Webview`
    // surface, so `browser::focus` cannot be asked which tab it just showed.
    // It is what makes the model's "the page you are looking at" mean the page
    // the user is actually looking at.
    webview_bridge::note_active(&tab_id);
    browser::focus(&app, &tab_id)
}

/// Resize every browser page to the UI's current content rectangle.
#[tauri::command]
async fn codify_browser_resize(
    app: tauri::AppHandle,
    bounds: browser::Bounds,
) -> Result<(), String> {
    browser::resize(&app, bounds)
}

/// Close a browser tab's page.
#[tauri::command]
async fn codify_browser_close(app: tauri::AppHandle, tab_id: String) -> Result<String, String> {
    browser::close(&app, &tab_id)
}

/// Open a browser page's DevTools inspector.
///
/// Shell-side surface over the webview, granted to the app's webview alone.
/// The crate builds with tauri's `devtools` feature, so this exists in every
/// build; remove that feature and this call stops compiling — the loud kind
/// of removal, which is the kind this crate prefers.
#[tauri::command]
async fn codify_browser_devtools_open(
    app: tauri::AppHandle,
    tab_id: String,
) -> Result<String, String> {
    browser::open_devtools(&app, &tab_id)
}

/// Close a browser page's DevTools inspector.
#[tauri::command]
async fn codify_browser_devtools_close(
    app: tauri::AppHandle,
    tab_id: String,
) -> Result<String, String> {
    browser::close_devtools(&app, &tab_id)
}

/// Is a browser page's DevTools inspector open?
#[tauri::command]
async fn codify_browser_devtools_state(
    app: tauri::AppHandle,
    tab_id: String,
) -> Result<bool, String> {
    browser::devtools_open(&app, &tab_id)
}

/// Does this build have an inspector at all?
///
/// Compile-time metadata, answered so the UI never hardcodes the build
/// shape: the pane asks, the shell answers, and a build that loses the
/// feature has the UI's back regardless of what the frontend assumed.
#[tauri::command]
async fn codify_browser_devtools_available() -> Result<bool, String> {
    Ok(browser::devtools_available())
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

    let project_root = engine_protocol::resolve_project_root(
        std::env::var("CODIFY_ROOT").ok().as_deref(),
        std::env::current_exe().ok().as_deref(),
        &std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from(".")),
    );

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

    // Which `python3` actually runs: the checkout's own `.venv` when it has one,
    // and the PATH otherwise. `make test` already uses the venv, and the two can
    // hold different installs — the Laya gate's SDK lives in the venv, and an
    // engine on `/usr/bin/python3` cannot see it, so it would keep paying an LLM
    // call per goal for an SDK that is installed in the same checkout. One
    // interpreter for the whole project; `engine_interpreter` is where the
    // fallback is decided and tested.
    let interpreter = engine_protocol::engine_interpreter(&project_root);
    eprintln!("[Codify] Engine interpreter: {}", interpreter.display());
    let mut engine = tokio::process::Command::new(&interpreter);
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

/// Leave a smoke run with a *real* process status.
///
/// `AppHandle::exit` only requests an exit: in tauri 2.11.6 it hands the
/// code to `RuntimeHandle::request_exit`, which raises
/// `RunEvent::ExitRequested`/`Exit` and unwinds the normal shutdown — the
/// code rides on the event and the process still ends 0. A first-paint
/// failure that exits 0 is a failure to anything reading the status, which
/// is most of what a smoke test is for, so the smoke exits for real instead.
///
/// Taking the process down with it is safe *here* and only here: smoke mode
/// never starts an engine (frozen by the browser module's
/// `the_smoke_mode_is_gated_reports_and_never_starts_the_engine`), so there
/// is no child to reap and no lease to break, and the harness that drives
/// this terminates the child on every path out anyway. Both streams are
/// flushed first, because the verdict is the last thing said.
fn smoke_exit(code: i32) -> ! {
    use std::io::Write;
    let _ = std::io::stdout().flush();
    let _ = std::io::stderr().flush();
    std::process::exit(code);
}

// ── the tabs smoke: one boot that proves tab restoration end to end ────────

/// The environment variable that turns a boot into the tabs smoke.
pub const TABS_SMOKE_ENV: &str = "CODEIFY_TABS_SMOKE";

/// The line prefix every verdict of this smoke carries; `scripts/tabs_smoke.py`
/// reads its run through these lines and nothing else.
pub const TABS_SMOKE_LINE: &str = "tabs-smoke: ";

/// The row the smoke seeds, and the one key in the whole run this window
/// **could not have minted**: `tabKey`'s output carries exactly two `_` — the
/// prefix's and the time/counter separator — and everything between them is
/// base36. A third `_` cannot be minted, only received, so a tab carrying this
/// key arrived through the engine round trip this smoke exists to measure. It
/// still satisfies the UI's own key law (`k_` prefix, bounded length), so a
/// restored tab may carry it.
pub const TABS_SMOKE_SEED_KEY: &str = "k_tabs_smoke_seed";

/// Where the seeded tab points: a real, refusable-by-nothing https address on
/// the embed smoke's own default host. The seat does not wait for the page to
/// paint, so the verdict does not depend on the network — a webview is seated
/// for the tab whether the page loads or not.
fn tabs_smoke_seed_url() -> &'static str {
    "https://example.com/tabs-smoke"
}

/// The whole run's deadline, from mode engagement. The engine's launch retries,
/// its migrations on a fresh `CODIFY_HOME`, the UI's boot, and the seat wait
/// all live inside it; a smoke that can outlive its own harness's patience is
/// a hang wearing a smoke's clothes.
pub const TABS_SMOKE_DEADLINE_SECS: u64 = 75;

/// Whether this boot is the tabs smoke. Presence is the switch, exactly as
/// `CODEIFY_EMBED_SMOKE` is: an empty value engages with the defaults, so a
/// harness never has to invent a value to say "run it".
fn tabs_smoke_requested(read: impl Fn(&str) -> Option<String>) -> bool {
    read(TABS_SMOKE_ENV).is_some()
}

/// The window's own webview — the UI, not one of the seated pages — for the
/// one channel diagnosis may use: asking the app what it sees about itself.
/// Reaches for `get_webview` first (a webview window whose children are the
/// seated pages resolves its *own* webview there) and falls back to scanning
/// for the label that is not a page's.
fn get_ui_webview(window: &tauri::WebviewWindow<tauri::Wry>) -> Option<tauri::Webview<tauri::Wry>> {
    if let Some(ui) = window.get_webview("main") {
        return Some(ui);
    }
    window
        .webviews()
        .into_values()
        .find(|page| !page.label().starts_with(crate::browser::LABEL_PREFIX))
}

/// The body the smoke seeds, as the engine's `PUT /shell/tabs` receives it.
///
/// A pure function so the freeze test can hold it: the payload is what the UI's
/// own decoder will refuse-or-restore, and the shape it must have — a browser
/// kind, an address, a history whose cursor names the address — is the mirror
/// format of `tabPersistence`, not anything the engine understands. The engine
/// will take any JSON; this smoke must send the JSON a real window writes.
fn tabs_smoke_seed_body() -> serde_json::Value {
    serde_json::json!({
        "key": TABS_SMOKE_SEED_KEY,
        "position": 0,
        "kind": "browser",
        "payload": serde_json::json!({
            "kind": "browser",
            "url": tabs_smoke_seed_url(),
            "history": {"entries": [tabs_smoke_seed_url()], "index": 0},
        })
        .to_string(),
    })
}

/// The tabs smoke's driver: start the engine as an ordinary boot does, seed the
/// engine's strip with one row this window has never seen, and wait for the
/// window to rehydrate it.
///
/// `CODEIFY_TABS_SMOKE` (any value) turns the app's own launch into the test —
/// the sibling of `browser::smoke_mode`, and different from it in the one way
/// that matters: this smoke's subject **is** the engine round trip, so the
/// engine starts and the UI boots exactly as a user's boot does. What it adds
/// is a seed and a verdict:
///
/// 1. wait for the boot handshake (`CODIFY_ENGINE token=… port=…`), failing
///    fast if the launcher recorded a problem — an engine that cannot start is
///    this smoke's subject failing, not an accident of the harness;
/// 2. seed `PUT /shell/tabs` with [`tabs_smoke_seed_body`] over the same
///    authenticated route a window writes through;
/// 3. focus the window — attention is what the UI's pull runs on (docs/09
///    §2.1's honest cost), and a smoke that never looks at the window would be
///    testing a pull that never happens;
/// 4. watch for a webview at the seed's address: the UI seats a webview only
///    for the active tab's address, and that address exists here only because
///    the engine-only row was adopted and restored — so the seat is the
///    end-to-end proof;
/// 5. read the strip back from the engine and print one
///    `tabs-smoke: restored <kind> <key>` line per row — the strip the window
///    is showing, as the engine holds it — then `tabs-smoke: PASS` and exit 0.
///    Every failure says which stage did not happen, and [`smoke_exit`] makes
///    the code real.
///
/// The UI is deliberately untouched: no smoke branch in the app, no init
/// script. Everything the verdict needs is already observable from the shell —
/// the seats and the engine's HTTP surface — so the production code path is the
/// only path there is.
fn tabs_smoke_mode(app: tauri::AppHandle, state: SharedEngineState) -> Result<(), String> {
    let Some(window) = app.get_webview_window("main") else {
        return Err("the main window does not exist".to_string());
    };
    tauri::async_runtime::spawn(async move {
        let deadline =
            tokio::time::Instant::now() + std::time::Duration::from_secs(TABS_SMOKE_DEADLINE_SECS);
        // 1. The handshake. The launcher owns the retries; this loop only
        //    watches for its answer, and takes the problem line as the verdict
        //    when the launcher has already given up.
        let (token, port) = loop {
            let engine = state.lock().await;
            if let (Some(token), Some(port)) = (&engine.token, engine.port) {
                break (token.clone(), port);
            }
            if let Some(problem) = &engine.problem {
                println!("{TABS_SMOKE_LINE}FAILED the engine could not launch: {problem}");
                smoke_exit(1);
            }
            drop(engine);
            if tokio::time::Instant::now() >= deadline {
                println!("{TABS_SMOKE_LINE}FAILED no engine handshake within {TABS_SMOKE_DEADLINE_SECS}s");
                smoke_exit(1);
            }
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        };
        println!("{TABS_SMOKE_LINE}engine is up on {port}");

        // 2. Seed, through the route a window writes. One attempt: a refusal
        //    is a finding — the smoke sends exactly what a real window sends.
        let client = reqwest::Client::new();
        let url = format!("http://127.0.0.1:{port}/shell/tabs");
        let sent = client
            .put(&url)
            .header("Authorization", format!("Bearer {token}"))
            .json(&tabs_smoke_seed_body())
            .send()
            .await;
        match sent {
            Ok(resp) if resp.status().is_success() => {
                println!("{TABS_SMOKE_LINE}seeded {}", TABS_SMOKE_SEED_KEY);
            }
            Ok(resp) => {
                println!(
                    "{TABS_SMOKE_LINE}FAILED the engine refused the seed (HTTP {}) — the smoke \
                     sends what a real window sends, so this is a finding, not a harness bug",
                    resp.status()
                );
                smoke_exit(1);
            }
            Err(e) => {
                println!("{TABS_SMOKE_LINE}FAILED could not reach the engine: {e}");
                smoke_exit(1);
            }
        }

        // 3. Attention, repeated. The pull runs when the window is next looked
        //    at — `visibilitychange` and `focus`, not a timer (docs/09 §2.1) —
        //    and a set_focus alone is a no-op when the window already *has*
        //    focus. So this performs the user action the contract is defined
        //    on: looking away, then back. It is done **in the seat loop, every
        //    ATTN_PULSE_SECS**, not once: the UI registers its pull listeners
        //    only when its own health probe flips `engineUp`, which can be up
        //    to one probe interval after the seed lands — a single pulse at
        //    seed time lands on a window that is not listening yet (measured:
        //    one pulse, then `tabs: 0` at the timeout with the seed still in
        //    the engine). A user glances back more than once; so does this.

        // 4. Wait for the seat. The UI seats webviews by *tab id* — the label
        //    names a local `id` this window minted at restore, never a key —
        //    so the label cannot be the fingerprint. The address is: the
        //    seed's URL is the one address in the whole run, a webview is
        //    seated only for the active tab's address, and that address exists
        //    in the UI only because the seeded row was adopted and restored.
        //    (Trailing-slash-insensitive, because URL normalisation is the
        //    loader's business and not the verdict's.) Before waiting: the
        //    UI's engine connection comes from `localStorage` when it holds
        //    one (WebKit's storage lives under the app identifier and is
        //    *not* scoped by CODIFY_HOME), and a stale pair from a previous
        //    session would point the window at an engine that exists but holds
        //    a different database — the pull would run, the seed would not be
        //    in what it read back, and the smoke would time out here. The
        //    stale-auth recovery the UI already has needs the answer to change
        //    first; answering with this boot's real pair up front removes the
        //    stale half of that loop.
        //    Only the port: the token is not written to the webview's storage at
        //    all any more (the page gets it from the shell over IPC and keeps it
        //    in memory), so there is no stale token to overwrite.
        let fresh_port = port;
        if let Err(e) = window.eval(format!(
            "localStorage.setItem('CODIFY_PORT', '{fresh_port}');",
        )) {
            println!("{TABS_SMOKE_LINE}FAILED could not reach the window's webview: {e}");
            smoke_exit(1);
        }
        let seed = tabs_smoke_seed_url().trim_end_matches('/');
        let ui = get_ui_webview(&window);
        let iteration = std::sync::Arc::new(std::sync::atomic::AtomicU32::new(0));
        // Every child label this run has already URL-checked. Seats are
        // monotonic in practice (a page is seated once per restore), so the
        // expensive question is asked once per label and the loop's steady
        // state is the pure label scan.
        let mut seen_seats: std::collections::HashSet<String> = window
            .webviews()
            .into_values()
            .map(|page| page.label().to_string())
            .collect();
        loop {
            // Live probe, every fourth iteration (~6s): what the window's own
            // page says about itself, while it is happening rather than at
            // the timeout. `port` is which engine the page *thinks* it talks
            // to; `banner` is the shell's own error surface, whose text names
            // the port a failed request tried; `tabs` is the strip's DOM.
            if let Some(ui) = &ui {
                let n = iteration.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                // Not `n.is_multiple_of(4)`: that is newer than the `rust-version` Cargo.toml declares.
                #[allow(clippy::manual_is_multiple_of)]
                let every_fourth = n % 4 == 0;
                if every_fourth {
                    let (tx, rx) = tokio::sync::oneshot::channel::<String>();
                    let tx = std::sync::Mutex::new(Some(tx));
                    let script = "JSON.stringify({tabs: document.querySelectorAll('[role=\"tab\"]').length, \
                        port: localStorage.getItem('CODIFY_PORT'), \
                        vis: document.visibilityState, \
                        banner: (document.querySelector('[role=\"alert\"]')?.textContent || '').slice(0, 140)})";
                    if ui
                        .eval_with_callback(script, move |answer| {
                            if let Ok(mut slot) = tx.lock() {
                                if let Some(sender) = slot.take() {
                                    let _ = sender.send(answer);
                                }
                            }
                        })
                        .is_ok()
                    {
                        if let Ok(Ok(answer)) =
                            tokio::time::timeout(std::time::Duration::from_secs(3), rx).await
                        {
                            println!("{TABS_SMOKE_LINE}probe {answer}");
                        }
                    }
                }
            }
            // The attention pulse, from inside the loop: see the comment
            // above. Driven as the **event the contract names**, not as a
            // window-manager gesture: a WM minimise/restore cycle was tried
            // first and killed the whole app on this desktop (frameless
            // Wayland window, `decorations: false` — GTK's minimise took the
            // window down with it, measured: seed, then a clean exit two
            // seconds later with no verdict). The pull listens for `focus` on
            // the app's window object, so the pulse is that event, dispatched
            // into the app's own webview; `visibilityState` is already
            // "visible" (the timeout diagnosis prints it), so the guard the
            // pull checks passes and the read runs. Same input signal a user's
            // alt-tab produces, minus the window manager that ate it.
            let _ = get_ui_webview(&window)
                .map(|ui| ui.eval("window.dispatchEvent(new Event('focus'));"));
            tokio::time::sleep(std::time::Duration::from_millis(1_500)).await;
            // The seat check: two stages, and the first one has no dispatcher
            // call in it. `Webview::url()` crosses to the web process, and the
            // webview being asked is the one whose page is allowed to be
            // failing to load — measured: a run whose example.com page sat in
            // "Load failed" took 189s to reach a verdict that should have
            // taken ten, with neither the shell's 75s deadline nor the
            // harness's timer firing, because the `url()` call stalled this
            // task and sleeps elsewhere kept the process alive. So: labels
            // first (a pure in-process scan; the label names the tab id, and
            // tab ids are `tab-<n>`, minted at restore in strip order), and
            // only when a *new* label appears does the URL question get
            // asked, once, bounded.
            let labels: Vec<String> = window
                .webviews()
                .into_values()
                .map(|page| page.label().to_string())
                .collect();
            let mut seated = false;
            for label in &labels {
                if seen_seats.contains(label) {
                    continue;
                }
                seen_seats.insert(label.clone());
                if let Some((_, page)) = window
                    .webviews()
                    .into_iter()
                    .find(|(_, p)| p.label() == label.as_str())
                {
                    let url = tokio::time::timeout(
                        std::time::Duration::from_secs(2),
                        tauri::async_runtime::spawn_blocking(move || {
                            page.url().map(|u| u.as_str().to_string())
                        }),
                    )
                    .await;
                    let matches = match url {
                        Ok(Ok(Ok(text))) => text.trim_end_matches('/') == seed,
                        _ => false,
                    };
                    if matches {
                        seated = true;
                        break;
                    }
                }
            }
            if seated {
                // 5. The strip, from the engine's side: what the window is
                //    showing, one line per row. The engine's answer is the
                //    shared truth; the window adopted it, and the seat above
                //    is the proof of the adopting half.
                match client
                    .get(&url)
                    .header("Authorization", format!("Bearer {token}"))
                    .send()
                    .await
                {
                    Ok(resp) => match resp.json::<serde_json::Value>().await {
                        Ok(rows) => {
                            if let Some(list) = rows.as_array() {
                                for row in list {
                                    let key =
                                        row.get("key").and_then(|k| k.as_str()).unwrap_or("?");
                                    let kind =
                                        row.get("kind").and_then(|k| k.as_str()).unwrap_or("?");
                                    println!("{TABS_SMOKE_LINE}restored {kind} {key}");
                                }
                            }
                        }
                        Err(e) => {
                            println!("{TABS_SMOKE_LINE}note: the strip read did not decode: {e}")
                        }
                    },
                    Err(e) => println!("{TABS_SMOKE_LINE}note: the strip read did not answer: {e}"),
                }
                println!("{TABS_SMOKE_LINE}PASS");
                smoke_exit(0);
            }
            if tokio::time::Instant::now() >= deadline {
                // The strip, as the engine holds it, then the window's own
                // account of itself. The two together separate every half of
                // the chain: "the engine lost the seed", "the engine has it
                // and the window never came to look", "the window came and
                // the merge refused it", "the tab is in the DOM and the seat
                // never ran". The eval is diagnosis only — its answer names
                // the stage in the FAILED line's footnotes and never passes
                // the run.
                match client
                    .get(&url)
                    .header("Authorization", format!("Bearer {token}"))
                    .send()
                    .await
                {
                    Ok(resp) => match resp.json::<serde_json::Value>().await {
                        Ok(rows) => {
                            let keys: Vec<String> = rows
                                .as_array()
                                .map(|list| {
                                    list.iter()
                                        .filter_map(|row| {
                                            row.get("key")?.as_str().map(String::from)
                                        })
                                        .collect()
                                })
                                .unwrap_or_default();
                            if keys.is_empty() {
                                println!("{TABS_SMOKE_LINE}strip at timeout: empty");
                            } else {
                                println!("{TABS_SMOKE_LINE}strip at timeout: {}", keys.join(", "));
                            }
                        }
                        Err(e) => println!("{TABS_SMOKE_LINE}strip at timeout: undecodable ({e})"),
                    },
                    Err(e) => println!("{TABS_SMOKE_LINE}strip at timeout: unreachable ({e})"),
                }
                let ui = get_ui_webview(&window);
                if let Some(ui) = ui {
                    let (tx, rx) = tokio::sync::oneshot::channel::<String>();
                    let tx = std::sync::Mutex::new(Some(tx));
                    let asked = ui.eval_with_callback(
                        "JSON.stringify({vis: document.visibilityState, \
                         tabs: document.querySelectorAll('[role=\"tab\"]').length, \
                         seed: location.href.includes('tabs-smoke'), \
                         port: localStorage.getItem('CODIFY_PORT')})",
                        move |answer| {
                            if let Ok(mut slot) = tx.lock() {
                                if let Some(sender) = slot.take() {
                                    let _ = sender.send(answer);
                                }
                            }
                        },
                    );
                    match asked {
                        Ok(()) => match tokio::time::timeout(std::time::Duration::from_secs(3), rx)
                            .await
                        {
                            Ok(Ok(answer)) => {
                                println!("{TABS_SMOKE_LINE}window at timeout: {answer}");
                            }
                            _ => println!("{TABS_SMOKE_LINE}window at timeout: no answer"),
                        },
                        Err(e) => {
                            println!("{TABS_SMOKE_LINE}window at timeout: eval refused ({e})")
                        }
                    }
                }
                println!(
                    "{TABS_SMOKE_LINE}FAILED the seeded tab was never seated within \
                     {TABS_SMOKE_DEADLINE_SECS}s — the pull, the adopt, the restore or the \
                     seat did not happen; the strip and window lines above say which half held",
                );
                smoke_exit(1);
            }
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        }
    });
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    apply_display_backend();
    // The first-paint facts, on the launch log: the session bus the
    // single-instance guard claims, the WebKit sandbox's ability to start a
    // web process, the DMABUF rendering path, the display backend. Diagnosis
    // only — nothing here changes behaviour (see the browser module's rationale).
    browser::log_environment_diagnostics();
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
            // `get_window`, not `get_webview_window`: the latter answers `None`
            // as soon as the main window has a child webview — which is the
            // moment anybody is using this app — so a second launch would
            // reveal nothing at all and say nothing about having failed.
            if let Some(window) = app.get_window("main") {
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
        .manage(SharedMachines::new(std::sync::Mutex::new(
            machine::Machines::default(),
        )))
        // The one channel a `browser-*` webview may speak on. A page needs no
        // capability to reach it — which is the point: it is not a Tauri
        // command, it carries text only, and `webview_bridge::deliver` drops
        // anything whose id this process did not issue.
        .register_uri_scheme_protocol(webview_bridge::BRIDGE_SCHEME, |context, request| {
            webview_bridge::scheme_handler(context, request)
        })
        .setup(move |app| {
            // The embed smoke test, when asked: seat the page, watch for the
            // paint, report, exit. Runs INSTEAD of the engine launch so the
            // test is about the embed path and nothing else — see
            // browser::smoke_mode for what it claims and scripts/embed_smoke.py
            // for the harness that runs it.
            if let Ok(url) = std::env::var("CODEIFY_EMBED_SMOKE") {
                let url = if url.trim().is_empty() {
                    "https://example.com/".to_string()
                } else {
                    url
                };
                println!("embed-smoke: mode engaged ({url})");
                browser::smoke_mode(&url, app.handle()).map_err(std::io::Error::other)?;
                // `Listener` comes from the Manager/AppHandle side, not the
                // Window: the event is announced by browser::smoke_mode from
                // the shell's own title hook, and the app is the listener.
                // Nothing the page says is trusted here — the page cannot
                // reach this event at all, it can only set a title.
                let listener_sink = app.handle().clone();
                // The smoke now has **three** deliverables — a paint, the
                // page's report of what it is showing, and the same page read
                // back through the bridge the AI uses — and the run ends when
                // all are in. Exiting on paint alone was correct when paint
                // was all there was, and wrong the moment the probe landed: the
                // first report is scheduled at 800ms because it must let the
                // paint markers clear, so a run that ended on the marker at
                // ~400ms ended before the first report and printed
                // "no-report" about a page it had never asked. Three flags, one
                // exit, and the timeout below is the backstop for any of them.
                //
                // The third leg is opt-out rather than opt-in, because a smoke
                // that quietly stops measuring the bridge is how a bridge that
                // cannot reach a page becomes a passing build. `--no-bridge`
                // is the escape hatch, and it is a harness flag so the reason
                // is on the command line that produced the run.
                let bridge_leg = webview_bridge::smoke_bridge_enabled();
                let flags = std::sync::Arc::new(std::sync::Mutex::new([false; 3]));
                let paint_flags = flags.clone();
                let report_flags = flags.clone();
                let bridge_flags = flags.clone();
                listener_sink.listen_any(browser::SMOKE_PAINTED_EVENT, move |_event| {
                    println!("embed-smoke: painted");
                    if let Ok(mut seen) = paint_flags.lock() {
                        seen[0] = true;
                        if seen.iter().all(|leg| *leg) {
                            smoke_exit(0);
                        }
                    }
                });
                listener_sink.listen_any(browser::SMOKE_REPORT_EVENT, move |_event| {
                    if let Ok(mut seen) = report_flags.lock() {
                        seen[1] = true;
                        if seen.iter().all(|leg| *leg) {
                            smoke_exit(0);
                        }
                    }
                });
                if bridge_leg {
                    listener_sink.listen_any(webview_bridge::SMOKE_BRIDGE_EVENT, move |_event| {
                        if let Ok(mut seen) = bridge_flags.lock() {
                            seen[2] = true;
                            if seen.iter().all(|leg| *leg) {
                                smoke_exit(0);
                            }
                        }
                    });
                    // Started here rather than from a page event: the question
                    // is the shell's own, and a page that cannot be scripted
                    // would otherwise be able to skip the leg that proves the
                    // AI can reach it.
                    webview_bridge::smoke_probe(app.handle().clone(), webview_bridge::SMOKE_TAB);
                } else {
                    println!(
                        "embed-smoke: bridge leg skipped ({})",
                        webview_bridge::SMOKE_BRIDGE_SKIP_ENV
                    );
                }
                // The timeout, from the same thread: a page that never paints,
                // never reports, or never answers the bridge must fail the
                // smoke rather than hang it.
                tauri::async_runtime::spawn(async move {
                    tokio::time::sleep(std::time::Duration::from_secs(20)).await;
                    println!(
                        "embed-smoke: FAILED no first paint, page report or page \
                         read within 20s"
                    );
                    smoke_exit(1);
                });
                // The engine never starts in smoke mode: this run is about
                // the embed path, and an engine would be a second variable.
                return Ok(());
            }
            // The tabs smoke, when asked: run the ordinary boot — engine and all
            // — then seed the strip and watch the window rehydrate it. Its
            // verdict is printed and the process exits for real; see
            // `tabs_smoke_mode` for what it claims and scripts/tabs_smoke.py
            // for the harness that runs it.
            if tabs_smoke_requested(|name| std::env::var(name).ok()) {
                println!("tabs-smoke: mode engaged");
                if let Err(reason) = tabs_smoke_mode(app.handle().clone(), state_clone.clone()) {
                    println!("{TABS_SMOKE_LINE}FAILED {reason}");
                    smoke_exit(1);
                }
                // The engine launch below still runs: this smoke's subject is
                // the round trip through it, and `tabs_smoke_mode` exits the
                // process on its own when the verdict is in.
            }
            // Ending deliberately is the shell's own job, whichever way it is asked:
            // a closed window reaches the exit handler below, a signal reaches this.
            watch_shutdown_signals(app.handle().clone());
            // The render watchdog: the one party that pays for a software-rasterised
            // backdrop is this process, so this is the party that notices and says
            // so. See the block comment above `watch_render_starvation`.
            watch_render_starvation(app.handle().clone());
            // Launch engine asynchronously so the window appears immediately.
            let shared = state_clone.clone();
            tauri::async_runtime::spawn(async move {
                launch_engine(shared).await;
            });
            // The bridge's other end: poll the engine for page questions and
            // put them to the webview the user is looking at. Started here, not
            // inside `launch_engine`, because it must survive an engine
            // restart — the base URL and token are re-read on every poll for
            // exactly that reason.
            webview_bridge::start(app.handle().clone(), state_clone.clone());
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
            codify_machine_open,
            codify_machine_write,
            codify_machine_resize,
            codify_machine_reset,
            codify_machine_close,
            codify_browser_open,
            codify_browser_navigate,
            codify_browser_focus,
            codify_browser_resize,
            codify_browser_close,
            codify_browser_devtools_open,
            codify_browser_devtools_close,
            codify_browser_devtools_state,
            codify_browser_devtools_available,
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
        tabs_smoke_requested, tabs_smoke_seed_body, tabs_smoke_seed_url, EngineState, EngineStop,
        SharedEngineState, DEFAULT_STOP_GRACE, ENGINE_STOP_POLL, STOP_GRACE_SLACK, TABS_SMOKE_ENV,
        TABS_SMOKE_LINE, TABS_SMOKE_SEED_KEY,
    };
    use super::{render_watchdog, RENDER_STARVED_EVENT};
    use std::sync::Arc;
    use std::time::Duration;
    use tokio::sync::Mutex;

    #[test]
    fn the_render_watchdog_announces_once_after_four_hot_seconds() {
        // The streak rule is the simplest shape of the verdict: a burn held for
        // the whole window. Six hot of ten must announce — exactly once, since
        // the page keeps the verdict for the session and a second announcement
        // would override the user's own "animate anyway".
        let mut verdict = render_watchdog::Verdict::default();
        for _ in 0..9 {
            assert!(!verdict.observe(true));
        }
        assert!(verdict.observe(true), "six hot of ten must announce");
        assert!(!verdict.observe(true), "and never announce again");
    }

    #[test]
    fn a_bursty_rasteriser_is_caught_but_a_single_spike_is_not() {
        // Measured on the machine that froze: the main thread alternates ~3 s
        // at ~90% of a core with ~3 s at ~34%. A consecutive-streak rule missed
        // that verdict by one second, twice — the honest reason this rule is
        // windowed. Three hot in a burst plus three in the next crosses the
        // six-of-ten line on the second burst; a one-off page load (two hot in
        // the window, however hot) never does.
        let mut verdict = render_watchdog::Verdict::default();
        let burst: Vec<bool> = [true, true, true, false, false, false]
            .into_iter()
            .collect();
        for hot in burst.iter().cycle().take(9) {
            assert!(
                !verdict.observe(*hot),
                "nine samples of a 3s-burst pattern must not announce"
            );
        }
        assert!(
            verdict.observe(true),
            "the second burst completes the window"
        );

        let mut verdict = render_watchdog::Verdict::default();
        let spike: Vec<bool> = [
            true, true, false, false, false, false, false, false, false, false,
        ]
        .into_iter()
        .collect();
        for hot in spike {
            assert!(!verdict.observe(hot));
        }
        assert!(
            !verdict.announced,
            "a single spike must stay a single spike"
        );
    }

    #[test]
    fn a_cool_second_resets_the_streak() {
        // Cool samples age out of the window, so a near-miss from minutes ago
        // cannot count toward the next spike: only the last ten seconds vote.
        let mut verdict = render_watchdog::Verdict::default();
        for _ in 0..5 {
            verdict.observe(true);
        }
        for _ in 0..render_watchdog::HOT_WINDOW {
            verdict.observe(false);
        }
        for _ in 0..5 {
            assert!(!verdict.observe(true));
        }
        assert!(
            !verdict.announced,
            "five old hot samples must have aged out of the window"
        );
        assert!(
            verdict.observe(true),
            "and the window must still be able to announce"
        );
    }

    #[test]
    fn the_hot_threshold_is_just_over_half_a_core() {
        // Named numbers, so the calibration is visible in a test rather than
        // buried in a loop: 54 ticks in a second is busy-but-below, 55 is the
        // verdict, and a clock (100 ticks) is exactly the edge.
        assert!(!render_watchdog::sample(0, 54).hot);
        assert!(render_watchdog::sample(0, 55).hot);
        assert!(render_watchdog::sample(0, 100).hot);
        assert_eq!(render_watchdog::sample(0, 100).fraction_x100, 100);
        // And a quiet second is nowhere near it, including after wrap-free
        // subtraction of a larger previous reading (saturating, never negative).
        assert!(!render_watchdog::sample(900, 950).hot);
        assert_eq!(render_watchdog::sample(950, 900).fraction_x100, 0);
    }

    #[test]
    fn the_main_thread_reading_parses_this_kernel_layout() {
        // Field 1 is pid, field 2 is comm *in parens*, field 3 is state, then
        // eight more fields to utime at field 14 — so the parse starts after
        // the LAST `)` (a thread named `codify (tty)` would otherwise shift
        // every field) and skips 11 tokens: state is index 0, utime index 11,
        // stime index 12.
        let stat = "600786 (codify-desktop) R 4113 0 0 0 0 0 0 0 0 0 1234 567 0 0";
        let after = stat.rsplit_once(")").unwrap().1;
        let mut fields = after.split_whitespace();
        let utime: u64 = fields.nth(11).unwrap().parse().unwrap();
        let stime: u64 = fields.next().unwrap().parse().unwrap();
        assert_eq!(utime + stime, 1234 + 567);
        // A comm field containing a `)` must not shift the parse.
        let tricky = "42 (codify (tty)) R 7 0 0 0 0 0 0 0 0 0 5 6 0 0";
        let after = tricky.rsplit_once(")").unwrap().1;
        let mut fields = after.split_whitespace();
        let utime: u64 = fields.nth(11).unwrap().parse().unwrap();
        let stime: u64 = fields.next().unwrap().parse().unwrap();
        assert_eq!(utime + stime, 5 + 6);
    }

    #[test]
    fn the_watchdog_is_wired_into_setup_and_names_the_page_event() {
        // A monitor that nothing calls monitors nothing. The setup block must
        // start it, and the event it emits must be the one `shellEvents.ts`
        // and `motionPreference.ts` listen for — nothing in the two type
        // systems spans the gap, so this test is the bridge.
        let source = include_str!("lib.rs");
        assert!(
            source.contains("watch_render_starvation(app.handle().clone());"),
            "setup never starts the render watchdog"
        );
        assert_eq!(
            RENDER_STARVED_EVENT, "codify:engine-render-starved",
            "the event name drifted from the UI's listener"
        );
        let ui = include_str!("../../ui/src/motionPreference.ts");
        assert!(
            ui.contains("codify:engine-render-starved"),
            "the UI store no longer listens for the shell's verdict"
        );
    }

    #[test]
    fn the_tabs_smoke_is_gated_on_its_env_var_alone() {
        // The same switch rule as the embed smoke: presence engages, absence
        // does not, and the value is nobody's business — a harness never has
        // to invent a value to say "run it".
        assert!(!tabs_smoke_requested(|_| None));
        assert!(tabs_smoke_requested(|_| Some(String::new())));
        assert!(tabs_smoke_requested(|_| Some("1".to_string())));
    }

    #[test]
    fn the_seed_is_a_row_a_real_window_would_restore() {
        // The engine takes any JSON; this smoke must send the JSON a window
        // writes. The body's four fields, the browser kind, and a history
        // whose cursor names its only entry — the mirror format of
        // tabPersistence, which is what the UI's decoder restores.
        let body = tabs_smoke_seed_body();
        assert_eq!(body["key"], TABS_SMOKE_SEED_KEY);
        assert_eq!(body["position"], 0);
        assert_eq!(body["kind"], "browser");
        let payload: serde_json::Value =
            serde_json::from_str(body["payload"].as_str().expect("payload is a string"))
                .expect("the payload is valid JSON, or PUT /shell/tabs would refuse it");
        assert_eq!(payload["kind"], "browser");
        assert_eq!(
            payload["url"], "https://example.com/tabs-smoke",
            "https on a non-loopback host — the one shape the shell's own guard allows"
        );
        assert_eq!(payload["history"]["entries"][0], payload["url"]);
        assert_eq!(
            payload["history"]["index"], 0,
            "the cursor names the address"
        );
        // And the address the seed carries is one the shell's own navigation
        // guard would allow: a seed the guard refuses is a seed the UI's
        // decoder drops on arrival, and the smoke would spend its whole
        // deadline waiting for a seat that can never happen.
        let target = crate::browser::parse_navigation(tabs_smoke_seed_url());
        assert!(
            target.is_ok(),
            "the seed URL must pass the shell's own guard"
        );
    }

    #[test]
    fn the_seed_key_is_one_this_window_could_not_have_minted() {
        // The proof rests on this: the UI's own key law (layoutSync's
        // `isTabKey`, mirrored here) accepts the seed — so a restored tab may
        // carry it — while the key factory cannot produce it. `tabKey` is
        // `k_` + base36 time + `_` + base36 counter: exactly two separators,
        // and nothing but base36 between them. A third `_` can only arrive
        // from somewhere that is not this process's clock.
        assert!(TABS_SMOKE_SEED_KEY.starts_with("k_"));
        assert!(TABS_SMOKE_SEED_KEY.len() <= 64);
        let rest = TABS_SMOKE_SEED_KEY
            .strip_prefix("k_")
            .expect("every key starts k_");
        let mut parts = rest.split('_');
        let (time, counter) = (
            parts.next().unwrap_or_default(),
            parts.next().unwrap_or_default(),
        );
        assert!(
            parts.next().is_some(),
            "the seed has a third separator, which tabKey cannot emit"
        );
        let base36 = |s: &str| {
            !s.is_empty()
                && s.chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        };
        assert!(base36(time) && base36(counter));
    }

    #[test]
    fn the_tabs_smoke_lines_name_the_reader_that_waits_for_them() {
        // The verdict channel is stdout, and the reader is a Python script
        // this crate cannot import; the contract is the prefix itself. The
        // script reads the run through these lines and nothing else, so a
        // rename here is a rename there, and the freeze says so.
        let reader = include_str!("../../scripts/tabs_smoke.py");
        assert!(reader.contains(TABS_SMOKE_LINE));
        assert!(reader.contains(TABS_SMOKE_ENV));
        assert!(
            reader.contains("--rebuild"),
            "the harness builds the shell it runs"
        );
        assert!(
            reader.contains("Popen"),
            "the harness launches the built binary"
        );
        assert!(
            reader.contains("XDG_DATA_HOME") && reader.contains("XDG_CACHE_HOME"),
            "the harness stopped isolating the webview's own storage. \
             `CODIFY_HOME` moves the engine's database and nothing else, while \
             the cached engine address and the layout mirror live in the shared \
             WebKit profile — which is how this smoke wrote its own tab into \
             the developer's real strip, one row per run, 137 of them"
        );
    }

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
