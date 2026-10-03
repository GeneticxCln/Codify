//! A user-driven terminal, and the boundary it sits behind.
//!
//! **Who owns this, and why it is not the engine.** `tests/test_no_unguarded_spawns.py`
//! freezes every process start in `engine/`, `benchmarks/` and `scripts/`. This
//! file is in `src-tauri/` deliberately: the desktop shell already owns process
//! lifecycle — it spawns `python3 -m engine` and kills it on exit — and a PTY is
//! the same kind of responsibility. The engine must not gain a way to run a
//! shell, because its one command runner, `SandboxService.run_command`, is the
//! agent's privileged path: docs/00 §6.6 says only verifier-proposed argv reaches
//! it, and a user typing at a prompt is a different authority from an agent
//! proposing a command. Mixing the two would weaken the one boundary that keeps a
//! model's output from becoming a shell.
//!
//! So this is a terminal in the ordinary sense: the user's own shell, in the
//! workspace directory, typed at by hand. Nothing in this module reads an argv
//! from a request.
//!
//! ## The boundary that matters
//!
//! A terminal is a shell, so the only real question is *where* it starts. A
//! client that could choose the working directory could point a user's shell at
//! anywhere on the machine. [`pin_cwd`] answers that: the directory comes from
//! the engine's own workspace record — resolved server-side by
//! [`crate::workspace_root_for`] — and is refused unless it exists and is
//! absolute. The client supplies a workspace id, never a path.

use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty, PtySize};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::PathBuf;
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// What one open terminal holds. The three things a session needs and no two of
/// which can substitute for each other: somewhere to write, somewhere to read
/// from, and the child itself — kept whole rather than as a bare signaller, so
/// close can reap it and escalate past a SIGHUP the shell chose to ignore.
pub struct Session {
    pub(crate) writer: Box<dyn Write + Send>,
    pub(crate) master: Box<dyn MasterPty + Send>,
    pub(crate) child: Box<dyn Child + Send + Sync>,
}

impl Session {
    /// Send bytes to the process on the PTY's slave end.
    pub(crate) fn send(&mut self, data: &str) -> Result<(), String> {
        self.writer
            .write_all(data.as_bytes())
            .map_err(|e| format!("write failed: {e}"))?;
        self.writer
            .flush()
            .map_err(|e| format!("flush failed: {e}"))
    }

    /// Resize the PTY, which is what makes `ls` wrap at the pane's width.
    pub(crate) fn resize(&self, cols: u16, rows: u16) -> Result<(), String> {
        self.master
            .resize(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| format!("resize failed: {e}"))
    }
}

/// Open a PTY and start `cmd` on its slave end: the one place a pane's process is
/// spawned, for the user's terminal and for a machine alike.
///
/// It is shared **plumbing**, not shared authority. What differs between the two
/// callers is the `CommandBuilder` they hand in — the user's own shell here in
/// [`open`], a `bwrap` jail in `machine.rs` — and each keeps its own registry and
/// its own ids, so a terminal id can never be written through a machine's commands
/// or the reverse. Keeping `native_pty_system()` and `spawn_command()` in this one
/// function is what keeps the spawn freeze's table short and honest.
pub(crate) fn spawn_pty(
    cmd: CommandBuilder,
    cols: u16,
    rows: u16,
) -> Result<(Session, Box<dyn Read + Send>), String> {
    let pty_system = native_pty_system();
    let pair = pty_system
        .openpty(PtySize {
            rows,
            cols,
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|e| format!("failed to open a pty: {e}"))?;
    let child = pair
        .slave
        .spawn_command(cmd)
        .map_err(|e| format!("failed to start the process: {e}"))?;
    // The slave is the child's end; the parent keeps the master. Dropping the
    // slave here is what makes the PTY behave like a terminal rather than a pipe.
    drop(pair.slave);
    let reader = pair
        .master
        .try_clone_reader()
        .map_err(|e| format!("failed to read the pty: {e}"))?;
    let writer = pair
        .master
        .take_writer()
        .map_err(|e| format!("no pty writer: {e}"))?;
    Ok((
        Session {
            writer,
            master: pair.master,
            child,
        },
        reader,
    ))
}

/// The terminal backend the shell hands around.
#[derive(Default)]
pub struct Terminals {
    sessions: HashMap<String, Session>,
    seq: u64,
}

/// Sent on `terminal-output` as a terminal receives it.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TerminalOutput {
    pub id: String,
    pub data: String,
}

/// Sent on `terminal-exit` when a shell finishes, so the pane can say so.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TerminalExit {
    pub id: String,
}

/// The one decision this module exists to settle: where a terminal starts.
///
/// The path comes from the engine's workspace record, so this is not the user's
/// to choose — but it is still validated here, because a record is data and data
/// can be wrong. Three refusals, each of which would otherwise hand a shell
/// somewhere it should not be:
///
/// * no root recorded at all;
/// * a relative path, which would resolve against whatever the shell happened to
///   start from;
/// * a path that does not exist or is not a directory.
///
/// It returns the path it will use rather than using it, so the value is
/// testable without spawning anything.
pub fn pin_cwd(root_path: Option<&str>) -> Result<PathBuf, String> {
    let raw = root_path
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            "workspace has no root path — a terminal has nowhere to start".to_string()
        })?;
    let path = PathBuf::from(raw);
    if !path.is_absolute() {
        return Err(format!(
            "workspace root is not absolute: {raw:?} — a terminal's directory must not depend on where the shell was launched"
        ));
    }
    if !path.is_dir() {
        return Err(format!(
            "workspace root is not a directory: {raw:?} — refusing to start a shell there"
        ));
    }
    Ok(path)
}

/// The user's own shell, with the working directory already decided.
///
/// `$SHELL` rather than a hardcoded interpreter: this terminal is the user's, and
/// choosing their shell for them is the one thing a terminal must not do. It
/// falls back to `/bin/sh` where none is set.
fn shell_command(cwd: PathBuf) -> CommandBuilder {
    let program = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());
    let mut cmd = CommandBuilder::new(program);
    cmd.cwd(cwd);
    // A login-adjacent interactive shell is what a user expects from a terminal
    // pane; `portable-pty`'s default env is the parent's, which is right here —
    // the pane is the user's environment and not a sanitised one.
    cmd.env("TERM", "xterm-256color");
    cmd
}

/// Turns a stream of byte chunks into text without cutting a character in half.
///
/// A PTY is read in fixed-size chunks and a chunk ends wherever the read did, so
/// a three-byte `€` or a four-byte emoji can arrive as two reads. Decoding each
/// chunk on its own turns both halves into U+FFFD, which is what a box-drawing
/// TUI or `cat` of a large UTF-8 file looked like. The trailing bytes of an
/// *incomplete* sequence are held back and prefixed to the next chunk; bytes
/// that are genuinely invalid are replaced at once, so bad input can never stall
/// the stream waiting for a completion that will not come.
#[derive(Default)]
pub(crate) struct Utf8Chunker {
    pending: Vec<u8>,
}

impl Utf8Chunker {
    /// The text this chunk completes. May be empty when the chunk ends mid-character.
    pub(crate) fn push(&mut self, bytes: &[u8]) -> String {
        self.pending.extend_from_slice(bytes);
        let mut out = String::new();
        let mut start = 0;
        while start < self.pending.len() {
            match std::str::from_utf8(&self.pending[start..]) {
                Ok(valid) => {
                    out.push_str(valid);
                    start = self.pending.len();
                }
                Err(err) => {
                    let end = start + err.valid_up_to();
                    // The prefix is valid by construction, so this is lossless.
                    out.push_str(&String::from_utf8_lossy(&self.pending[start..end]));
                    start = end;
                    match err.error_len() {
                        Some(bad) => {
                            out.push('\u{FFFD}');
                            start += bad;
                        }
                        // An unfinished sequence at the very end: wait for the rest.
                        None => break,
                    }
                }
            }
        }
        self.pending.drain(..start);
        out
    }

    /// What is left when the stream ends: an unfinished character can never be
    /// completed now, so it is reported as the replacement it has become.
    pub(crate) fn finish(&mut self) -> String {
        if self.pending.is_empty() {
            String::new()
        } else {
            self.pending.clear();
            "\u{FFFD}".to_string()
        }
    }
}

/// How a PTY's output is grouped on its way to the window.
///
/// The kernel hands a PTY reader at most about 4 KiB per `read`, so a program that repaints the
/// whole screen (`cmatrix`, a progress bar, `top`) used to become one event per read: measured on a
/// full-screen colour repaint, 135 events a second at 80x24, 770 at 200x50 and 30 frames a second,
/// 1,400 at 60, and 2,400 with the producer unthrottled. Each event is a serialised message into the
/// webview, a listener call, a history append and an xterm write, all on the one thread that also
/// draws the page and handles keys. So the reader groups what it reads:
///
/// * a gap of [`BATCH_QUIET`] with nothing new ends a batch, so a prompt, an echoed key or the last
///   line of a command goes out within a couple of milliseconds and is never held for a frame;
/// * a batch is built for at most [`BATCH_WINDOW`] or [`BATCH_MAX_BYTES`], and two batches are at
///   least [`BATCH_WINDOW`] apart, which is what bounds a flood to about 125 events a second however
///   fast the program writes;
/// * the reader hands text over through a channel that holds [`BACKLOG_PIECES`], so a window that
///   cannot keep up makes the *shell* wait (the PTY fills and the program blocks on its write)
///   instead of the app growing without limit.
const READ_BUF_BYTES: usize = 16 * 1024;
const BATCH_QUIET: Duration = Duration::from_millis(2);
const BATCH_WINDOW: Duration = Duration::from_millis(8);
const BATCH_MAX_BYTES: usize = 128 * 1024;
const BACKLOG_PIECES: usize = 64;

/// Read a PTY to its end, decoding it, and hand the decoded text to `out` a piece at a time.
///
/// Runs on a thread of its own so that waiting for the shell never waits on the window, and the
/// other way round. Stops early when `out`'s receiver is gone: nobody is listening any more.
fn read_decoded<R: Read>(mut reader: R, out: mpsc::SyncSender<String>) {
    let mut chunker = Utf8Chunker::default();
    let mut buf = vec![0u8; READ_BUF_BYTES];
    loop {
        match reader.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                let text = chunker.push(&buf[..n]);
                if !text.is_empty() && out.send(text).is_err() {
                    return;
                }
            }
        }
    }
    let tail = chunker.finish();
    if !tail.is_empty() {
        let _ = out.send(tail);
    }
}

fn sleep_until(when: Instant) {
    if let Some(wait) = when.checked_duration_since(Instant::now()) {
        std::thread::sleep(wait);
    }
}

/// Group what `rx` delivers into batches (see [`BATCH_QUIET`]) and give each to `on_text`, in order.
fn batch_output(rx: &mpsc::Receiver<String>, on_text: &mut impl FnMut(String)) {
    // The first batch is not made to wait for an interval that has not happened.
    let mut last_emit = Instant::now()
        .checked_sub(BATCH_WINDOW)
        .unwrap_or_else(Instant::now);
    while let Ok(first) = rx.recv() {
        let mut batch = first;
        let started = Instant::now();
        let mut last_piece = started;
        let mut gone = false;
        loop {
            let earliest = last_emit + BATCH_WINDOW;
            if batch.len() >= BATCH_MAX_BYTES {
                // Full: take nothing more (the channel fills, the reader stops, the shell waits)
                // and go as soon as the interval since the last batch allows.
                sleep_until(earliest);
                break;
            }
            // A quiet gap ends the batch and the window caps it, and neither is allowed to fire
            // before the minimum interval since the last one: that is the rate limit.
            let due = (last_piece + BATCH_QUIET)
                .min(started + BATCH_WINDOW)
                .max(earliest);
            let now = Instant::now();
            if now >= due {
                break;
            }
            match rx.recv_timeout(due - now) {
                Ok(more) => {
                    batch.push_str(&more);
                    last_piece = Instant::now();
                }
                Err(RecvTimeoutError::Timeout) => break,
                Err(RecvTimeoutError::Disconnected) => {
                    gone = true;
                    break;
                }
            }
        }
        last_emit = Instant::now();
        on_text(batch);
        if gone {
            return;
        }
    }
}

/// Read a PTY to its end, handing its decoded text to `on_text` in batches.
///
/// This is the whole of what the reader does, taken out of [`open`] so a test can drive it with a
/// real PTY and a closure instead of an `AppHandle`. It returns when the child's end closes (EOF,
/// or the EIO a Linux PTY reports once the shell has gone), after the last of the output has been
/// handed over: the `terminal-exit` that follows is never ahead of the text it ends. The text is
/// the same as it always was and in the same order; only the size of the pieces changed.
pub(crate) fn pump_output<R: Read + Send + 'static>(reader: R, mut on_text: impl FnMut(String)) {
    let (tx, rx) = mpsc::sync_channel::<String>(BACKLOG_PIECES);
    let reader_thread = std::thread::spawn(move || read_decoded(reader, tx));
    batch_output(&rx, &mut on_text);
    drop(rx);
    let _ = reader_thread.join();
}

/// Open a terminal in `root_path`, and start feeding its output to the app.
///
/// Returns the session id. The reading half runs on its own thread: a shell that
/// blocks the UI is worse than no terminal, and the reader is exactly the
/// long-lived task a thread is for.
///
/// Everything a session needs is created here and nowhere else, so "a terminal
/// exists" and "a PTY was opened" are the same statement.
pub fn open(
    app: tauri::AppHandle,
    sessions: &Arc<Mutex<Terminals>>,
    root_path: Option<&str>,
    cols: u16,
    rows: u16,
) -> Result<String, String> {
    let cwd = pin_cwd(root_path)?;
    let (session, reader) = spawn_pty(shell_command(cwd), cols, rows)?;

    let id = {
        let mut guard = sessions.lock().map_err(|_| "terminal state poisoned")?;
        guard.seq += 1;
        let id = format!("term-{}", guard.seq);
        guard.sessions.insert(id.clone(), session);

        // The reader thread holds no lock and no session reference: it owns the
        // reader and reports by id, so a terminal can be closed while its thread
        // is still draining without a deadlock or a use-after-free.
        let sink = app.clone();
        let for_thread = id.clone();
        std::thread::spawn(move || {
            use tauri::Emitter;
            pump_output(reader, |text| {
                let _ = sink.emit(
                    "terminal-output",
                    TerminalOutput {
                        id: for_thread.clone(),
                        data: text,
                    },
                );
            });
            let _ = sink.emit("terminal-exit", TerminalExit { id: for_thread });
        });

        id
    };

    Ok(id)
}

/// Send keystrokes to a terminal. Unknown ids are an error, not a no-op: a pane
/// that silently accepts typing into a closed terminal is a pane that looks
/// alive.
pub fn write(sessions: &Arc<Mutex<Terminals>>, id: &str, data: &str) -> Result<(), String> {
    let mut guard = sessions.lock().map_err(|_| "terminal state poisoned")?;
    let session = guard
        .sessions
        .get_mut(id)
        .ok_or_else(|| format!("no such terminal: {id}"))?;
    session.send(data)
}

/// Resize the PTY, which is what makes `ls` wrap at the pane's width.
pub fn resize(
    sessions: &Arc<Mutex<Terminals>>,
    id: &str,
    cols: u16,
    rows: u16,
) -> Result<(), String> {
    let mut guard = sessions.lock().map_err(|_| "terminal state poisoned")?;
    let session = guard
        .sessions
        .get_mut(id)
        .ok_or_else(|| format!("no such terminal: {id}"))?;
    session.resize(cols, rows)
}

/// Close a terminal and reap the shell behind it.
///
/// The kill is not optional. A terminal left running after its pane closes is a
/// stray process holding the workspace directory — the same defect the engine's
/// exit handler exists to prevent (`python3 -m engine` leaking a port and the
/// DB). The session is removed first so a second close is a clean no-op, and
/// [`kill_and_reap`] collects the exit status rather than leaving a zombie.
pub fn close(sessions: &Arc<Mutex<Terminals>>, id: &str) -> Result<(), String> {
    // The reap happens outside the lock: kill_and_reap can block for up to a
    // second against an unkillable shell, and a write or resize that arrived
    // meanwhile should wait on that only if it really has to.
    let session = {
        let mut guard = sessions.lock().map_err(|_| "terminal state poisoned")?;
        guard.sessions.remove(id)
    };
    if let Some(session) = session {
        kill_and_reap(session.child);
    }
    Ok(())
}

/// Kill the shell behind a session and collect its exit status.
///
/// [`portable_pty::ChildKiller::kill`] on the child stored in a [`Session`] is portable-pty's
/// escalating kill for a `std::process::Child`: SIGHUP first, then a grace
/// period whose polls reap a shell that honoured the signal, then a hard kill.
/// A shell that *ignores* SIGHUP therefore still dies. The escalation is the
/// whole reason the [`Session`] keeps the child instead of a signaller: a bare
/// `clone_killer()` sends SIGHUP and nothing else, so a shell with SIGHUP set
/// to SIG_IGN — a deliberately detached process, the exact disposition docs/09
/// §5.4 keeps the engine from overriding — used to outlive its pane, and
/// nothing ever waited on it.
///
/// The reap is bounded. Normally the kill's own grace poll has already
/// collected the status and the first `try_wait` below sees it. A process that
/// survives even the hard kill plus this window — stopped under a debugger,
/// stuck in uninterruptible IO — is handed to a detached thread that waits for
/// as long as it takes, so the entry is reaped eventually and close never
/// hangs the window that asked for it.
pub(crate) fn kill_and_reap(mut child: Box<dyn Child + Send + Sync>) {
    // An error here is ESRCH — the shell is already gone — which is success,
    // the same reading `SandboxService._signal_alone` gives a vanished pid.
    let _ = child.kill();
    for _ in 0..20 {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => std::thread::sleep(std::time::Duration::from_millis(50)),
            Err(_) => return,
        }
    }
    std::thread::spawn(move || {
        let _ = child.wait();
    });
}

/// Close every terminal. Called when the app exits, so a quit does not leave a
/// shell holding the workspace.
pub fn close_all(sessions: &Arc<Mutex<Terminals>>) {
    let children: Vec<Box<dyn Child + Send + Sync>> = if let Ok(mut guard) = sessions.lock() {
        guard
            .sessions
            .drain()
            .map(|(_, session)| session.child)
            .collect()
    } else {
        return;
    };
    for child in children {
        kill_and_reap(child);
    }
}

/// How many terminals are open. For the window's status line and the tests.
#[cfg(test)]
fn open_count(sessions: &Arc<Mutex<Terminals>>) -> usize {
    sessions.lock().map(|g| g.sessions.len()).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn a_terminal_refuses_a_workspace_with_no_root() {
        // A shell with no directory would start wherever the app was launched,
        // which is not the workspace and not anywhere a user expects.
        assert!(pin_cwd(None).is_err());
        assert!(pin_cwd(Some("")).is_err());
        assert!(pin_cwd(Some("   ")).is_err());
    }

    #[test]
    fn a_terminal_refuses_a_relative_directory() {
        // A relative path would resolve against the shell's own launch
        // directory — the one thing a pinned cwd is there to prevent.
        let err = pin_cwd(Some("relative/path")).unwrap_err();
        assert!(err.contains("not absolute"), "{err}");
    }

    #[test]
    fn a_terminal_refuses_a_directory_that_is_not_there() {
        // Refused rather than created: a terminal that made its own workspace
        // would be writing to a path nobody registered.
        let err = pin_cwd(Some("/nonexistent/codify/workspace/root")).unwrap_err();
        assert!(err.contains("not a directory"), "{err}");
    }

    #[test]
    fn a_terminal_refuses_a_path_that_is_a_file() {
        let dir = std::env::temp_dir().join("codify-terminal-pin-cwd-file");
        fs::write(&dir, b"not a directory").unwrap();
        let err = pin_cwd(dir.to_str()).unwrap_err();
        assert!(err.contains("not a directory"), "{err}");
        let _ = fs::remove_file(&dir);
    }

    #[test]
    fn a_registered_directory_is_accepted_as_it_stands() {
        // The returned path is the one that will be used, so the decision is
        // testable without ever spawning a shell.
        let dir = std::env::temp_dir().join("codify-terminal-pin-cwd-ok");
        fs::create_dir_all(&dir).unwrap();
        let pinned = pin_cwd(dir.to_str()).unwrap();
        assert_eq!(pinned, dir);
        let _ = fs::remove_dir(&dir);
    }

    #[test]
    fn closing_a_terminal_that_is_already_gone_is_a_clean_no_op() {
        // A second close must not be an error: a pane and the app's exit handler
        // both close, and the second one to arrive should not surface a failure.
        let sessions: Arc<Mutex<Terminals>> = Arc::new(Mutex::new(Terminals::default()));
        close(&sessions, "term-1").unwrap();
        assert_eq!(open_count(&sessions), 0);
    }

    #[test]
    fn close_kills_a_shell_that_ignores_sighup_and_leaves_no_zombie() {
        // The regression this pins: close used to keep only a bare signaller,
        // whose kill is SIGHUP alone, so a shell with SIGHUP at SIG_IGN outlived
        // its pane and nothing waited on it. Both halves are asserted: the
        // process is gone (the escalation worked) and its /proc entry is gone
        // (something reaped it — a zombie would still have one). The zombie
        // check reads /proc.
        use std::path::Path;
        use std::time::{Duration, Instant};

        let pair = portable_pty::native_pty_system()
            .openpty(PtySize {
                rows: 24,
                cols: 80,
                pixel_width: 0,
                pixel_height: 0,
            })
            .unwrap();
        let mut cmd = CommandBuilder::new("python3");
        cmd.arg("-c");
        cmd.arg(
            "import signal, time; signal.signal(signal.SIGHUP, signal.SIG_IGN); \
             print('ready', flush=True); time.sleep(60)",
        );
        let child = pair.slave.spawn_command(cmd).unwrap();
        let pid = child.process_id().expect("a unix child has a pid");
        drop(pair.slave);

        // The handshake is what makes the test honest: `ready` is printed only
        // once SIG_IGN is installed, so the SIGHUP that close sends is genuinely
        // ignored rather than racing the shell's default disposition.
        let mut reader = pair.master.try_clone_reader().unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut seen = String::new();
        let mut buf = [0u8; 64];
        while !seen.contains("ready") {
            assert!(Instant::now() < deadline, "the shell never signalled ready");
            let n = reader.read(&mut buf).expect("pty read failed");
            assert!(n > 0, "the pty closed before the shell signalled ready");
            seen.push_str(&String::from_utf8_lossy(&buf[..n]));
        }
        let proc_entry = Path::new("/proc").join(pid.to_string());
        assert!(
            proc_entry.exists(),
            "the shell should be alive before close"
        );

        let sessions: Arc<Mutex<Terminals>> = Arc::new(Mutex::new(Terminals::default()));
        {
            let mut guard = sessions.lock().unwrap();
            guard.seq += 1;
            let id = format!("term-{}", guard.seq);
            guard.sessions.insert(
                id.clone(),
                Session {
                    writer: pair.master.take_writer().unwrap(),
                    master: pair.master,
                    child,
                },
            );
        }
        close(&sessions, "term-1").unwrap();

        let deadline = Instant::now() + Duration::from_secs(3);
        while proc_entry.exists() {
            assert!(
                Instant::now() < deadline,
                "the SIGHUP-ignoring shell survived close, or was left a zombie"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    #[test]
    fn writing_to_a_terminal_that_is_not_open_is_an_error_not_a_no_op() {
        // A pane that silently accepted typing into a closed terminal would look
        // alive while dropping everything typed at it.
        let sessions: Arc<Mutex<Terminals>> = Arc::new(Mutex::new(Terminals::default()));
        let err = write(&sessions, "term-1", "ls\n").unwrap_err();
        assert!(err.contains("no such terminal"), "{err}");
    }

    #[test]
    fn a_closed_terminal_stops_reporting_itself_as_open() {
        let sessions: Arc<Mutex<Terminals>> = Arc::new(Mutex::new(Terminals::default()));
        {
            let mut guard = sessions.lock().unwrap();
            guard.seq += 1;
            let id = format!("term-{}", guard.seq);
            let pair = portable_pty::native_pty_system()
                .openpty(PtySize {
                    rows: 24,
                    cols: 80,
                    pixel_width: 0,
                    pixel_height: 0,
                })
                .unwrap();
            let child = pair
                .slave
                .spawn_command(CommandBuilder::new("true"))
                .unwrap();
            guard.sessions.insert(
                id.clone(),
                Session {
                    writer: pair.master.take_writer().unwrap(),
                    master: pair.master,
                    child,
                },
            );
        }
        // `true` has exited on its own by now, so this close exercises the path
        // where the shell is already gone: the kill reads ESRCH as success and
        // the reap still collects the status it left behind.
        std::thread::sleep(std::time::Duration::from_millis(100));
        assert_eq!(open_count(&sessions), 1);
        close(&sessions, "term-1").unwrap();
        assert_eq!(open_count(&sessions), 0);
    }

    #[test]
    fn a_character_split_across_reads_is_not_cut_in_half() {
        // The defect this pins: each 4096-byte read was decoded on its own with
        // `from_utf8_lossy`, so a multi-byte character whose bytes straddled two
        // reads came out as two U+FFFD. Every split point of a string with 1-,
        // 2-, 3- and 4-byte characters is tried, because the boundary can land
        // anywhere.
        let text = "a€é😀b";
        let bytes = text.as_bytes();
        for cut in 0..=bytes.len() {
            let mut chunker = Utf8Chunker::default();
            let mut got = chunker.push(&bytes[..cut]);
            got.push_str(&chunker.push(&bytes[cut..]));
            got.push_str(&chunker.finish());
            assert_eq!(got, text, "split at byte {cut}");
        }
    }

    #[test]
    fn invalid_bytes_are_replaced_at_once_and_an_unfinished_tail_at_the_end() {
        // Garbage must never stall the stream waiting for a completion that will
        // not come; and a stream that ends mid-character reports what it lost.
        let mut chunker = Utf8Chunker::default();
        assert_eq!(chunker.push(b"a\xffb"), "a\u{FFFD}b");
        assert_eq!(chunker.push(&[b'c', 0xe2, 0x82]), "c");
        assert_eq!(chunker.finish(), "\u{FFFD}");
        assert_eq!(chunker.finish(), "", "finish is once-only");

        let mut seen = Vec::new();
        pump_output(std::io::Cursor::new(vec![b'x', 0xe2, 0x82]), |t| {
            seen.push(t)
        });
        assert_eq!(seen.concat(), "x\u{FFFD}");
    }

    /// A reader that hands out what it is told to, a piece per `read`, and then waits or ends.
    struct Script {
        steps: std::collections::VecDeque<(Duration, Vec<u8>)>,
    }

    impl Read for Script {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            let Some((wait, bytes)) = self.steps.pop_front() else {
                return Ok(0);
            };
            std::thread::sleep(wait);
            assert!(
                bytes.len() <= buf.len(),
                "a test piece larger than the buffer"
            );
            buf[..bytes.len()].copy_from_slice(&bytes);
            Ok(bytes.len())
        }
    }

    /// Run `reader` through `pump_output`, and say when each piece of text was handed over.
    fn pump_timed(reader: impl Read + Send + 'static) -> (Vec<(Instant, String)>, Instant) {
        let began = Instant::now();
        let mut seen = Vec::new();
        pump_output(reader, |t| seen.push((Instant::now(), t)));
        (seen, began)
    }

    #[test]
    fn a_flood_arrives_whole_and_in_order_in_far_fewer_events_than_reads() {
        // The defect this pins: one event per 4 KiB read. `cmatrix` repainting a screen was
        // hundreds of events a second, each a message into the webview, a listener call, a
        // history append and an xterm write on the one thread that also draws the page.
        // The text is numbered so a lost, repeated or reordered piece shows.
        const READS: usize = 2000;
        let mut all = String::new();
        let mut steps = std::collections::VecDeque::new();
        for i in 0..READS {
            let piece = format!("{i:04} {}\n", "x".repeat(4000));
            all.push_str(&piece);
            steps.push_back((Duration::ZERO, piece.into_bytes()));
        }
        let (seen, began) = pump_timed(Script { steps });
        let took = began.elapsed();

        assert_eq!(
            seen.iter().map(|(_, t)| t.as_str()).collect::<String>(),
            all,
            "the flood was changed on the way"
        );
        assert!(
            seen.len() <= READS / 10,
            "{} events for {READS} reads: nothing was grouped",
            seen.len()
        );
        // The rate limit itself, which does not depend on how fast this machine is: two events are at
        // least a window apart. The last one is the exception on purpose: it is what the reader had
        // when the stream ended, and it goes at once so that `terminal-exit` is not made to wait.
        let apart = seen[..seen.len() - 1]
            .windows(2)
            .map(|w| w[1].0.duration_since(w[0].0))
            .min()
            .unwrap_or(BATCH_WINDOW);
        assert!(
            apart + Duration::from_millis(2) >= BATCH_WINDOW,
            "two events {apart:?} apart, under the {BATCH_WINDOW:?} floor (flood took {took:?})"
        );
    }

    #[test]
    fn a_trickle_just_slower_than_the_quiet_gap_is_still_held_to_the_rate_limit() {
        // A piece every 3 ms is a gap longer than `BATCH_QUIET`, so each one would end its own batch
        // and become its own event, about 330 a second: a spinner, or `cmatrix` on a slow terminal.
        // The floor between two events is what turns that into one per window. A flood never shows
        // this, because it fills a batch and waits for the floor on the way out.
        let steps = (0..40)
            .map(|i| (Duration::from_millis(3), format!("{i:02}\r\n").into_bytes()))
            .collect();
        let (seen, _) = pump_timed(Script { steps });
        let joined: String = seen.iter().map(|(_, t)| t.as_str()).collect();
        assert_eq!(joined.matches("\r\n").count(), 40, "a line was lost");
        let apart = seen[..seen.len() - 1]
            .windows(2)
            .map(|w| w[1].0.duration_since(w[0].0))
            .min()
            .expect("a 120 ms trickle came out as a single event");
        assert!(
            apart + Duration::from_millis(2) >= BATCH_WINDOW,
            "two events {apart:?} apart, under the {BATCH_WINDOW:?} floor"
        );
    }

    #[test]
    fn no_event_is_larger_than_the_cap_by_more_than_one_read() {
        // One piece of memory handed to the webview at a time has to stay bounded however much the
        // program writes: a batch stops growing at the cap, and the most it can overshoot by is the
        // single piece that carried it over.
        let piece = "y".repeat(READ_BUF_BYTES);
        let steps = (0..400)
            .map(|_| (Duration::ZERO, piece.clone().into_bytes()))
            .collect();
        let (seen, _) = pump_timed(Script { steps });
        let biggest = seen.iter().map(|(_, t)| t.len()).max().unwrap();
        assert!(
            biggest < BATCH_MAX_BYTES + READ_BUF_BYTES,
            "an event of {biggest} bytes"
        );
        assert_eq!(
            seen.iter().map(|(_, t)| t.len()).sum::<usize>(),
            400 * READ_BUF_BYTES
        );
    }

    #[test]
    fn a_prompt_is_not_held_for_the_output_that_has_not_come() {
        // What batching must not cost: a prompt, an echoed key, the last line of a command. The
        // reader below stalls for half a second after the prompt, as a shell does; the prompt has to
        // be in the window long before that, and alone, not waiting to be grouped with a next piece.
        let steps = [
            (Duration::ZERO, b"user@host:~$ ".to_vec()),
            (Duration::from_millis(500), b"ls\r\n".to_vec()),
        ]
        .into();
        let (seen, began) = pump_timed(Script { steps });
        assert_eq!(seen.len(), 2, "{seen:?}");
        assert_eq!(seen[0].1, "user@host:~$ ");
        let waited = seen[0].0.duration_since(began);
        assert!(
            waited < Duration::from_millis(250),
            "the prompt was held for {waited:?}"
        );
    }

    #[test]
    fn what_was_read_is_handed_over_before_pump_output_returns() {
        // `terminal-exit` is sent when this returns, and an exit that overtook the last line would
        // leave the pane saying the shell had ended with its output still on the way. The text here
        // ends mid-character, so the replacement for it is part of what must arrive too.
        let steps = [
            (Duration::ZERO, b"last line\r\n".to_vec()),
            (Duration::ZERO, vec![0xe2, 0x82]),
        ]
        .into();
        let (seen, _) = pump_timed(Script { steps });
        assert_eq!(
            seen.iter().map(|(_, t)| t.as_str()).collect::<String>(),
            "last line\r\n\u{FFFD}"
        );
    }

    #[test]
    fn a_window_that_cannot_keep_up_makes_the_shell_wait_and_the_app_does_not_grow() {
        // The reader hands text to the batcher through a bounded channel. While the listener is stuck
        // (a webview that is busy), the reader must stop reading: the PTY then fills and the program
        // blocks in its write, which is the right place for a flood to wait. With an unbounded channel
        // this reader would have run on to the end of the test, and every piece would be held in memory.
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

        struct Endless {
            reads: Arc<AtomicUsize>,
            stop: Arc<AtomicBool>,
        }
        impl Read for Endless {
            fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
                if self.stop.load(Ordering::SeqCst) {
                    return Ok(0);
                }
                self.reads.fetch_add(1, Ordering::SeqCst);
                buf.fill(b'z');
                Ok(buf.len())
            }
        }

        let reads = Arc::new(AtomicUsize::new(0));
        let stop = Arc::new(AtomicBool::new(false));
        let reader = Endless {
            reads: reads.clone(),
            stop: stop.clone(),
        };
        let mut stuck_at = None;
        let mut first = true;
        pump_output(reader, |_| {
            if first {
                first = false;
                std::thread::sleep(Duration::from_millis(400));
                stuck_at = Some(reads.load(Ordering::SeqCst));
                stop.store(true, Ordering::SeqCst);
            }
        });
        let stuck_at = stuck_at.expect("the listener was never called");
        // What can be in flight: the channel, the piece the reader is blocked sending, the batch the
        // listener is holding (it stops growing at the cap), and a little slack for a piece in transit.
        let limit = BACKLOG_PIECES + BATCH_MAX_BYTES / READ_BUF_BYTES + 4;
        assert!(
            stuck_at <= limit,
            "{stuck_at} reads while the listener was stuck; the most that can be in flight is {limit}"
        );
    }

    #[test]
    fn a_real_pty_flood_is_grouped_too_and_loses_nothing() {
        // The same property against the kernel's own PTY, which is where the 4 KiB reads come from:
        // two million bytes through a real terminal, counted at the far end.
        let (master, mut child) =
            real_pty("sh", &["-c", "head -c 2000000 /dev/zero | tr '\\000' x"]);
        let reader = master.try_clone_reader().unwrap();
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let (seen, began) = pump_timed(reader);
            let _ = tx.send((seen, began.elapsed()));
        });
        let (seen, took) = rx
            .recv_timeout(Duration::from_secs(60))
            .expect("the reader never saw the shell finish");
        let bytes: usize = seen.iter().map(|(_, t)| t.len()).sum();
        assert_eq!(bytes, 2_000_000, "bytes were lost or invented");
        assert!(seen.iter().all(|(_, t)| t.chars().all(|c| c == 'x')));
        // 2,000,000 bytes is about 500 reads of 4 KiB. Unbatched, that is about 500 events; batched, no
        // more than one per window plus the first and the last.
        let allowed = (took.as_millis() / BATCH_WINDOW.as_millis()) as usize + 3;
        assert!(
            seen.len() <= allowed,
            "{} events in {took:?}; the rate limit allows {allowed}",
            seen.len()
        );
        let _ = child.wait();
        drop(master);
    }

    /// Open a real PTY running `program args`, and return what a session holds.
    fn real_pty(
        program: &str,
        args: &[&str],
    ) -> (Box<dyn MasterPty + Send>, Box<dyn Child + Send + Sync>) {
        let pair = portable_pty::native_pty_system()
            .openpty(PtySize {
                rows: 24,
                cols: 80,
                pixel_width: 0,
                pixel_height: 0,
            })
            .unwrap();
        let mut cmd = CommandBuilder::new(program);
        for a in args {
            cmd.arg(a);
        }
        let child = pair.slave.spawn_command(cmd).unwrap();
        drop(pair.slave);
        (pair.master, child)
    }

    #[test]
    fn a_real_shell_prints_non_ascii_text_through_the_reader() {
        // The path from a shell's first byte to the text the app is handed, with
        // nothing stubbed but the app itself: a real PTY, a real child, the same
        // `pump_output` the reader thread runs. Ends when the shell exits, which
        // is also what the `terminal-exit` event is sent on.
        use std::sync::mpsc;
        use std::time::Duration;

        let (master, mut child) =
            real_pty("sh", &["-c", "printf 'h\\303\\251llo \\342\\202\\254\\n'"]);
        let reader = master.try_clone_reader().unwrap();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let mut all = String::new();
            pump_output(reader, |t| all.push_str(&t));
            let _ = tx.send(all);
        });
        let all = rx
            .recv_timeout(Duration::from_secs(10))
            .expect("the reader never saw the shell finish");
        assert!(all.contains("h\u{e9}llo \u{20ac}"), "got {all:?}");
        assert!(
            !all.contains('\u{FFFD}'),
            "a character was corrupted: {all:?}"
        );
        let _ = child.wait();
        drop(master);
    }

    #[test]
    fn a_line_written_to_a_real_pty_is_echoed_back_and_the_shell_is_reaped() {
        // The other half of the round trip: what `write` sends reaches the child,
        // and what the child prints comes back through the reader. `cat` on a
        // terminal echoes the line twice (the tty's echo, then cat's own), and
        // both are non-ASCII here so the write path is UTF-8 clean as well.
        use std::sync::mpsc;
        use std::time::{Duration, Instant};

        let (master, child) = real_pty("cat", &[]);
        let mut writer = master.take_writer().unwrap();
        let reader = master.try_clone_reader().unwrap();
        let (tx, rx) = mpsc::channel::<String>();
        std::thread::spawn(move || {
            pump_output(reader, |t| {
                let _ = tx.send(t);
            })
        });

        writer
            .write_all("h\u{e9}llo \u{20ac}\n".as_bytes())
            .unwrap();
        writer.flush().unwrap();

        let deadline = Instant::now() + Duration::from_secs(10);
        let mut all = String::new();
        while !all.contains("h\u{e9}llo \u{20ac}") {
            let left = deadline
                .checked_duration_since(Instant::now())
                .expect("the line was never echoed back");
            all.push_str(
                &rx.recv_timeout(left)
                    .expect("the line was never echoed back"),
            );
        }

        let sessions: Arc<Mutex<Terminals>> = Arc::new(Mutex::new(Terminals::default()));
        sessions.lock().unwrap().sessions.insert(
            "term-1".to_string(),
            Session {
                writer,
                master,
                child,
            },
        );
        close(&sessions, "term-1").unwrap();
        assert_eq!(open_count(&sessions), 0);
    }
}
