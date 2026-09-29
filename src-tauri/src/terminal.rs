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
use std::sync::{Arc, Mutex};

/// What one open terminal holds. The three things a session needs and no two of
/// which can substitute for each other: somewhere to write, somewhere to read
/// from, and the child itself — kept whole rather than as a bare signaller, so
/// close can reap it and escalate past a SIGHUP the shell chose to ignore.
pub struct Session {
    writer: Box<dyn Write + Send>,
    master: Box<dyn MasterPty + Send>,
    child: Box<dyn Child + Send + Sync>,
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
/// falls back to `/bin/sh` where none is set, and to `cmd.exe` on Windows.
fn shell_command(cwd: PathBuf) -> CommandBuilder {
    let program = if cfg!(windows) {
        std::env::var("COMSPEC").unwrap_or_else(|_| "cmd.exe".to_string())
    } else {
        std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string())
    };
    let mut cmd = CommandBuilder::new(program);
    cmd.cwd(cwd);
    // A login-adjacent interactive shell is what a user expects from a terminal
    // pane; `portable-pty`'s default env is the parent's, which is right here —
    // the pane is the user's environment and not a sanitised one.
    if !cfg!(windows) {
        cmd.env("TERM", "xterm-256color");
    }
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

/// Read a PTY to its end, handing each piece of decoded text to `on_text`.
///
/// This is the whole of what the reader thread does, taken out of [`open`] so a
/// test can drive it with a real PTY and a closure instead of an `AppHandle`:
/// before, the path from a shell's first byte to the `terminal-output` event
/// could only be exercised by launching the app. It returns when the child's end
/// closes (EOF, or the EIO a Linux PTY reports once the shell has gone).
pub(crate) fn pump_output<R: Read>(mut reader: R, mut on_text: impl FnMut(String)) {
    let mut chunker = Utf8Chunker::default();
    let mut buf = [0u8; 4096];
    loop {
        match reader.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                let text = chunker.push(&buf[..n]);
                if !text.is_empty() {
                    on_text(text);
                }
            }
        }
    }
    let tail = chunker.finish();
    if !tail.is_empty() {
        on_text(tail);
    }
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

    let pty_system = native_pty_system();
    let pair = pty_system
        .openpty(PtySize {
            rows,
            cols,
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|e| format!("failed to open a pty: {e}"))?;

    let cmd = shell_command(cwd);
    let child = pair
        .slave
        .spawn_command(cmd)
        .map_err(|e| format!("failed to start the shell: {e}"))?;
    // The slave is the child's end; the parent keeps the master. Dropping the
    // slave here is what makes the PTY behave like a terminal rather than a pipe.
    drop(pair.slave);

    let id = {
        let mut guard = sessions.lock().map_err(|_| "terminal state poisoned")?;
        guard.seq += 1;
        let id = format!("term-{}", guard.seq);
        let reader = pair
            .master
            .try_clone_reader()
            .map_err(|e| format!("failed to read the pty: {e}"))?;
        guard.sessions.insert(
            id.clone(),
            Session {
                writer: pair
                    .master
                    .take_writer()
                    .map_err(|e| format!("no pty writer: {e}"))?,
                master: pair.master,
                child,
            },
        );

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
    session
        .writer
        .write_all(data.as_bytes())
        .map_err(|e| format!("write failed: {e}"))?;
    session
        .writer
        .flush()
        .map_err(|e| format!("flush failed: {e}"))?;
    Ok(())
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
    session
        .master
        .resize(PtySize {
            rows,
            cols,
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|e| format!("resize failed: {e}"))
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
fn kill_and_reap(mut child: Box<dyn Child + Send + Sync>) {
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

    #[cfg(target_os = "linux")]
    #[test]
    fn close_kills_a_shell_that_ignores_sighup_and_leaves_no_zombie() {
        // The regression this pins: close used to keep only a bare signaller,
        // whose kill is SIGHUP alone, so a shell with SIGHUP at SIG_IGN outlived
        // its pane and nothing waited on it. Both halves are asserted: the
        // process is gone (the escalation worked) and its /proc entry is gone
        // (something reaped it — a zombie would still have one). Linux only,
        // because the zombie check reads /proc.
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
                .spawn_command(CommandBuilder::new(if cfg!(windows) {
                    "cmd.exe"
                } else {
                    "true"
                }))
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

    /// Open a real PTY running `program args`, and return what a session holds.
    #[cfg(unix)]
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

    #[cfg(unix)]
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

    #[cfg(unix)]
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
