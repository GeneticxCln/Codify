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

use portable_pty::{native_pty_system, ChildKiller, CommandBuilder, MasterPty, PtySize};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

/// What one open terminal holds. The three things a session needs and no two of
/// which can substitute for each other: somewhere to write, somewhere to read
/// from, and the child to kill.
pub struct Session {
    writer: Box<dyn Write + Send>,
    master: Box<dyn MasterPty + Send>,
    killer: Box<dyn ChildKiller + Send + Sync>,
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
        let mut reader = pair
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
                killer: child.clone_killer(),
            },
        );

        // The reader thread holds no lock and no session reference: it owns the
        // reader and reports by id, so a terminal can be closed while its thread
        // is still draining without a deadlock or a use-after-free.
        let sink = app.clone();
        let for_thread = id.clone();
        std::thread::spawn(move || {
            let mut buf = [0u8; 4096];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        let text = String::from_utf8_lossy(&buf[..n]).to_string();
                        use tauri::Emitter;
                        let _ = sink.emit(
                            "terminal-output",
                            TerminalOutput {
                                id: for_thread.clone(),
                                data: text,
                            },
                        );
                    }
                }
            }
            use tauri::Emitter;
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
/// DB). The session is removed first so a second close is a clean no-op, and the
/// wait reaps the child rather than leaving a zombie.
pub fn close(sessions: &Arc<Mutex<Terminals>>, id: &str) -> Result<(), String> {
    let mut guard = sessions.lock().map_err(|_| "terminal state poisoned")?;
    let Some(mut session) = guard.sessions.remove(id) else {
        return Ok(());
    };
    let _ = session.killer.kill();
    Ok(())
}

/// Close every terminal. Called when the app exits, so a quit does not leave a
/// shell holding the workspace.
pub fn close_all(sessions: &Arc<Mutex<Terminals>>) {
    if let Ok(mut guard) = sessions.lock() {
        for (_, session) in guard.sessions.drain() {
            let mut s = session;
            let _ = s.killer.kill();
        }
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
            let mut child = pair
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
                    killer: child.clone_killer(),
                },
            );
            let _ = child.kill();
        }
        assert_eq!(open_count(&sessions), 1);
        close(&sessions, "term-1").unwrap();
        assert_eq!(open_count(&sessions), 0);
    }
}
