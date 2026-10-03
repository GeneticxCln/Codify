//! A machine: a jailed Linux shell the assistant may type into, and the boundary around it.
//!
//! **What this is.** A disposable userland in a `bubblewrap` jail — its own PID, mount, network, IPC,
//! UTS and user namespaces, a read-only view of the system, a read-only view of the workspace at
//! `/work`, and a scratch home that is a size-capped tmpfs thrown away with the machine. The window
//! shows it in the same xterm pane a terminal uses, and the assistant gets eyes and hands on it through
//! the surface bridge (`docs/09` §14). It shares the host's **kernel**: it is not a separate machine,
//! and nothing here says otherwise.
//!
//! **Why this is not [`crate::terminal`].** A terminal is the person's own shell, "deliberately NOT
//! routed through the engine's `SandboxService`" (`tests/test_no_unguarded_spawns.py`): a user typing at
//! a prompt is a different authority from an agent proposing a command. A machine is the opposite
//! case, on purpose — the one place an assistant's keystrokes are allowed to run commands without an
//! allowlist (`docs/00` §6.6) — and the only thing that makes that acceptable is the jail. So the two
//! share the PTY plumbing ([`crate::terminal::spawn_pty`]) and nothing else: separate registry,
//! separate ids (`mach-N`), separate commands, separate events (`machine-output`, so the terminal
//! scrollback recorder never files a machine's output), and a different thing at the end of the
//! `CommandBuilder`. A terminal id cannot be written through a machine's commands or the reverse.
//!
//! ## The rules this module holds, each asserted by a test and not only said here
//!
//! * **There is no path that starts the shell without the jail.** [`open`] builds exactly one
//!   `CommandBuilder`, for `bwrap`, and nothing here names the user's shell. If `bwrap` is missing, or
//!   unprivileged user namespaces are switched off, opening fails with a sentence that says which.
//!   Falling back to a bare shell "so it still works" would hand an assistant the person's real
//!   account, which is the one outcome this exists to rule out.
//! * **The environment is built, not inherited.** `--clearenv`, then a short fixed list. No token, no key,
//!   no `CODIFY_*`, nothing the shell was started with.
//! * **The workspace is read-only, and so is everything else that is bound.** There is no `--bind`
//!   (read-write) anywhere in [`jail_argv`]. The writable places are two tmpfs mounts that die with the
//!   machine. Nothing leaves the jail except as text on the screen: invariant 9 is not amended.
//! * **No capabilities, and not root.** `--cap-drop ALL`; when the shell runs as uid 0 the jail maps to
//!   uid 1000 instead, so "root in the jail" is never host root.
//! * **A workspace that would expose the person's secrets is refused.** `/`, `$HOME` or an ancestor of
//!   it, and the credential directories (`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`) and Codify's own
//!   state, the same list the engine applies to a workspace root (`docs/03` §1.4) and for the same
//!   reason, with more at stake here: the reader is an assistant that can run anything.
//!
//! ## What it does not do, said here so it is a choice
//!
//! * **Network on shares the host's network namespace.** The machine can then reach services on
//!   `127.0.0.1` and the LAN. The engine needs the boot token, which the jail never sees; other local
//!   services do not. That is why the network is the person's choice at open time and cannot change
//!   afterwards: a running jail cannot be moved between namespaces, so "switch it on" means a new
//!   machine.
//! * **No seccomp filter.** The namespaces and the empty capability set are real; the host kernel's
//!   whole syscall surface is still reachable from inside. A filter is the next hardening step.
//! * **No memory limit.** `ulimit` bounds processes and core files; a cgroup needs systemd or root.
//!   The tmpfs sizes bound what the machine can keep, not what it can run.
//! * **No `--new-session`.** It would detach the jail from the PTY this module owns, and with it job
//!   control and Ctrl-C. The terminal-injection it guards against (`TIOCSTI`) reaches only this PTY's
//!   own input queue, which belongs to the jail already.

use crate::terminal::{self, Session};
use portable_pty::CommandBuilder;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

/// Sent on [`MACHINE_OUTPUT_EVENT`] as a machine prints.
pub const MACHINE_OUTPUT_EVENT: &str = "machine-output";
/// Sent on [`MACHINE_EXIT_EVENT`] when the shell in a machine finishes.
pub const MACHINE_EXIT_EVENT: &str = "machine-exit";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MachineOutput {
    pub id: String,
    pub data: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MachineExit {
    pub id: String,
}

/// How many machines may be open at once. A jail is cheap, and still a process tree the person did
/// not start by hand; four is more than a split can show.
pub const MAX_MACHINES: usize = 4;

/// The most a single write may carry. Typing is a handful of bytes and a paste a few thousand; an
/// unbounded write is a way to wedge the PTY's input queue from a caller that is not a person.
pub const MAX_WRITE_BYTES: usize = 64 * 1024;

/// Where the workspace appears inside the jail, and where the shell starts.
pub const WORK: &str = "/work";
/// The scratch home inside the jail.
pub const HOME: &str = "/home/machine";

const TMP_BYTES: u64 = 256 * 1024 * 1024;
const HOME_BYTES: u64 = 512 * 1024 * 1024;

/// The process cap for the shell and everything it starts. On a kernel from 5.14 this counts inside the
/// jail's own user namespace; on an older one it counts every process of the host user, which is why it
/// is generous rather than tight. It bounds a fork bomb; it is not a resource policy.
const NPROC: u32 = 1024;

/// The uid and gid a jail runs as when the shell itself is root.
const UNPRIVILEGED_ID: u32 = 1000;

/// The environment the jail's shell receives, and the only one. Ordered: `--clearenv` is emitted
/// before every `--setenv`, so what is listed here is *all* there is.
const JAIL_ENV: [(&str, &str); 8] = [
    (
        "PATH",
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    ),
    ("HOME", HOME),
    ("USER", "machine"),
    ("LOGNAME", "machine"),
    ("TERM", "xterm-256color"),
    ("LANG", "C.UTF-8"),
    // A prompt the assistant can recognise on the screen: where a command ends and the next can begin.
    ("PS1", "[machine] \\w \\$ "),
    // The workspace is read-only, so a bytecode write would only fail quietly.
    ("PYTHONDONTWRITEBYTECODE", "1"),
];

/// Files under `/etc` a jail may see, each only if the host has it. Enough for the dynamic linker, TLS
/// roots, time zones, terminfo and name lookups, and nothing that holds a secret (`shadow`, `ssh/`,
/// `sudoers`, `machine-id` are all absent).
const ETC_READ_ONLY: [&str; 13] = [
    "/etc/alternatives",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/localtime",
    "/etc/terminfo",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/etc/os-release",
    "/etc/profile.d",
];

/// What a jail is built from, once the person's choices and the workspace are settled.
#[derive(Debug, Clone)]
pub(crate) struct JailSpec {
    /// The workspace, absolute and already resolved. It is bound read-only at [`WORK`].
    pub workspace: PathBuf,
    /// Whether the machine shares the host's network. Off unless the person opened it with network.
    pub network: bool,
    /// The effective uid of the shell, to decide whether the jail may map to it.
    pub uid: u32,
}

/// One top-level system directory, as the host has it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum TopLevel {
    /// A real directory, bound read-only at the same path.
    Dir(String),
    /// A symlink (`/bin -> usr/bin` on a merged-`/usr` system), recreated as one.
    Link { at: String, target: String },
}

/// The parts of the host the jail copies the shape of. Detecting is the only impure step, so
/// [`jail_argv`] stays a function of its arguments and a test can hand it any host it likes.
#[derive(Debug, Clone, Default)]
pub(crate) struct HostLayout {
    pub top: Vec<TopLevel>,
    /// The entries of [`ETC_READ_ONLY`] this host has.
    pub etc: Vec<String>,
    /// The resolved target of `/etc/resolv.conf`, which is a symlink into `/run` on most systems and
    /// would dangle in the jail if bound as it stands.
    pub resolv: Option<PathBuf>,
}

impl HostLayout {
    pub(crate) fn detect() -> HostLayout {
        let mut top = Vec::new();
        for at in ["/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32"] {
            let Ok(meta) = std::fs::symlink_metadata(at) else {
                continue;
            };
            if meta.file_type().is_symlink() {
                if let Ok(target) = std::fs::read_link(at) {
                    top.push(TopLevel::Link {
                        at: at.to_string(),
                        target: target.to_string_lossy().into_owned(),
                    });
                }
            } else if meta.is_dir() {
                top.push(TopLevel::Dir(at.to_string()));
            }
        }
        let etc = ETC_READ_ONLY
            .iter()
            .filter(|p| Path::new(p).exists())
            .map(|p| p.to_string())
            .collect();
        let resolv = std::fs::canonicalize("/etc/resolv.conf").ok();
        HostLayout { top, etc, resolv }
    }
}

/// The arguments to `bwrap`, in full. A pure function: no spawning, no filesystem, no environment.
///
/// Read it as the jail's whole definition. There is no `--bind` (read-write) in it, no bind of
/// `$HOME`, the keyring, the display, D-Bus or Codify's state, and `--share-net` appears only for a
/// machine the person opened with network.
pub(crate) fn jail_argv(spec: &JailSpec, host: &HostLayout) -> Vec<String> {
    let mut a: Vec<String> = Vec::new();
    let mut push = |items: &[&str]| a.extend(items.iter().map(|s| s.to_string()));

    push(&["--unshare-all"]);
    if spec.network {
        // Retaining the network namespace is the one thing `--unshare-all` is told not to do.
        push(&["--share-net"]);
    }
    push(&["--die-with-parent", "--cap-drop", "ALL"]);
    if spec.uid == 0 {
        // Root in a user namespace that maps to host root is still host root for any file it can reach.
        let id = UNPRIVILEGED_ID.to_string();
        push(&["--uid", &id, "--gid", &id]);
    }
    push(&["--hostname", "machine", "--clearenv"]);
    for (key, value) in JAIL_ENV {
        push(&["--setenv", key, value]);
    }

    push(&["--ro-bind", "/usr", "/usr"]);
    for entry in &host.top {
        match entry {
            TopLevel::Dir(path) => push(&["--ro-bind", path, path]),
            TopLevel::Link { at, target } => push(&["--symlink", target, at]),
        }
    }
    for path in &host.etc {
        push(&["--ro-bind", path, path]);
    }
    if spec.network {
        if let Some(real) = &host.resolv {
            push(&["--ro-bind", &real.to_string_lossy(), "/etc/resolv.conf"]);
        }
    }
    push(&["--proc", "/proc", "--dev", "/dev"]);
    let tmp = TMP_BYTES.to_string();
    let home = HOME_BYTES.to_string();
    push(&["--size", &tmp, "--tmpfs", "/tmp"]);
    push(&["--size", &home, "--tmpfs", HOME]);
    push(&["--ro-bind", &spec.workspace.to_string_lossy(), WORK]);
    push(&["--chdir", WORK]);

    // The shell, behind the two limits `bwrap` has no flag for. `bash` where the host has it (its
    // readline is what a person expects), `sh` where it does not.
    let wrapper = format!(
        "ulimit -c 0 2>/dev/null; ulimit -u {NPROC} 2>/dev/null; \
         b=$(command -v bash) && exec \"$b\"; exec sh"
    );
    push(&["/bin/sh", "-c", &wrapper]);
    a
}

/// Why this host cannot make a jail, when it can be told without trying. `read` is a sysctl reader so a
/// test can be any host it likes.
///
/// Two refusals only, both unambiguous. Ubuntu's AppArmor restriction
/// (`kernel.apparmor_restrict_unprivileged_userns`) is deliberately *not* one: it blocks some programs
/// and allows others by profile, so reading it cannot say whether `bwrap` is among them. That case
/// reaches the person as `bwrap`'s own message in the pane, which is a sentence and not a fallback.
pub(crate) fn userns_refusal(
    is_root: bool,
    read: impl Fn(&str) -> Option<String>,
) -> Option<String> {
    let value = |path: &str| read(path).map(|v| v.trim().to_string());
    if value("/proc/sys/user/max_user_namespaces").as_deref() == Some("0") {
        return Some(
            "user namespaces are disabled on this system (user.max_user_namespaces is 0), \
             and a machine cannot be isolated without them"
                .to_string(),
        );
    }
    if !is_root && value("/proc/sys/kernel/unprivileged_userns_clone").as_deref() == Some("0") {
        return Some(
            "unprivileged user namespaces are disabled on this system \
             (kernel.unprivileged_userns_clone is 0), and a machine cannot be isolated without them"
                .to_string(),
        );
    }
    None
}

/// `bwrap` on `path_var`, if it is there and executable.
pub(crate) fn find_bwrap(path_var: &str) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    path_var
        .split(':')
        .filter(|dir| !dir.is_empty())
        .map(|dir| Path::new(dir).join("bwrap"))
        .find(|candidate| {
            std::fs::metadata(candidate)
                .map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
                .unwrap_or(false)
        })
}

/// Why this directory may not be shown to a machine, if it may not. The engine refuses the same list
/// when a workspace is *created* (`fs.protected_root_reason`); this asks again because a record is data
/// and can predate the rule, and because the reader here is an assistant that can run anything.
pub(crate) fn protected_reason(
    path: &Path,
    home: Option<&Path>,
    state: &[PathBuf],
) -> Option<String> {
    if path == Path::new("/") {
        return Some("the filesystem root".to_string());
    }
    if let Some(home) = home {
        if home.starts_with(path) {
            return Some("the home directory, or a directory that contains it".to_string());
        }
        for secret in [".ssh", ".gnupg", ".aws", ".kube"] {
            if path.starts_with(home.join(secret)) {
                return Some(format!("inside ~/{secret}"));
            }
        }
    }
    for dir in state {
        if path.starts_with(dir) {
            return Some("Codify's own state directory".to_string());
        }
    }
    None
}

/// The one decision about *what* a machine may see: the workspace, resolved, and refused if it would
/// hand an assistant the person's secrets. Returns the path it will bind rather than binding it.
pub(crate) fn pin_workspace(root_path: Option<&str>) -> Result<PathBuf, String> {
    let raw = root_path
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "workspace has no root path — a machine has nothing to show".to_string())?;
    let path = PathBuf::from(raw);
    if !path.is_absolute() {
        return Err(format!(
            "workspace root is not absolute: {raw:?} — a machine's view must not depend on where the shell was launched"
        ));
    }
    let resolved = std::fs::canonicalize(&path).map_err(|e| {
        format!(
            "workspace root cannot be resolved: {raw:?} ({e}) — refusing to open a machine on it"
        )
    })?;
    if !resolved.is_dir() {
        return Err(format!(
            "workspace root is not a directory: {raw:?} — refusing to open a machine on it"
        ));
    }
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .and_then(|h| std::fs::canonicalize(h).ok());
    let mut state = Vec::new();
    if let Some(home) = &home {
        state.push(home.join(".codify"));
    }
    if let Some(dir) = std::env::var_os("CODIFY_HOME").map(PathBuf::from) {
        state.push(std::fs::canonicalize(&dir).unwrap_or(dir));
    }
    if let Some(why) = protected_reason(&resolved, home.as_deref(), &state) {
        return Err(format!(
            "refusing to show {raw:?} to a machine: it is {why}, and an assistant can read whatever a machine can see"
        ));
    }
    Ok(resolved)
}

/// The machines the shell holds. Its own type, so Tauri's state lookup cannot hand a terminal's
/// registry to a machine's command.
#[derive(Default)]
pub struct Machines {
    sessions: HashMap<String, Session>,
    seq: u64,
}

fn missing_bwrap() -> String {
    "bubblewrap (bwrap) is not installed, and a machine cannot be isolated without it — \
     Arch/CachyOS: bubblewrap, Debian/Ubuntu: bubblewrap, Fedora: bubblewrap. \
     It is also what WebKitGTK's own sandbox uses"
        .to_string()
}

fn read_sysctl(path: &str) -> Option<String> {
    std::fs::read_to_string(path).ok()
}

/// Everything about a machine that is settled before a process exists: the program, the jail and what
/// refuses it. Split from [`open`] so a test can drive the real jail with a closure instead of an
/// `AppHandle`, the way `terminal::pump_output` is driven.
pub(crate) fn prepare(
    root_path: Option<&str>,
    network: bool,
    path_var: &str,
    uid: Option<u32>,
) -> Result<CommandBuilder, String> {
    let workspace = pin_workspace(root_path)?;
    let bwrap = find_bwrap(path_var).ok_or_else(missing_bwrap)?;
    // `uid` is a parameter so a test can be any user; production passes `None` and asks the OS.
    // SAFETY: `geteuid` takes no arguments, cannot fail and touches no memory.
    let uid = uid.unwrap_or_else(|| unsafe { libc::geteuid() });
    if let Some(why) = userns_refusal(uid == 0, read_sysctl) {
        return Err(why);
    }
    let spec = JailSpec {
        workspace,
        network,
        uid,
    };
    let argv = jail_argv(&spec, &HostLayout::detect());
    // The one `CommandBuilder` in this module, and it is `bwrap`'s. The shell it starts is named in the
    // jail's own argv, inside the namespaces; nothing here can start one outside them.
    let mut cmd = CommandBuilder::new(bwrap);
    cmd.args(argv);
    // `bwrap` itself needs nothing from the shell's environment; the jailed process gets its own.
    cmd.env_clear();
    cmd.env("PATH", path_var);
    Ok(cmd)
}

/// A machine, started and registered; its output goes to `on_text` and its end to `on_exit`.
pub(crate) fn open_with_sink(
    machines: &Arc<Mutex<Machines>>,
    cmd: CommandBuilder,
    cols: u16,
    rows: u16,
    mut on_text: impl FnMut(&str, String) + Send + 'static,
    on_exit: impl FnOnce(String) + Send + 'static,
) -> Result<String, String> {
    {
        let guard = machines.lock().map_err(|_| "machine state poisoned")?;
        if guard.sessions.len() >= MAX_MACHINES {
            return Err(format!(
                "{MAX_MACHINES} machines are already open — close one first"
            ));
        }
    }
    let (session, reader) = terminal::spawn_pty(cmd, cols, rows)?;
    let id = {
        let mut guard = machines.lock().map_err(|_| "machine state poisoned")?;
        guard.seq += 1;
        let id = format!("mach-{}", guard.seq);
        guard.sessions.insert(id.clone(), session);
        id
    };
    let for_thread = id.clone();
    std::thread::spawn(move || {
        terminal::pump_output(reader, |text| on_text(&for_thread, text));
        on_exit(for_thread);
    });
    Ok(id)
}

/// Open a machine on a registered workspace, and start feeding its output to the app.
///
/// `network` is the person's choice, made when they opened it, and is not something any later call
/// can change.
pub fn open(
    app: tauri::AppHandle,
    machines: &Arc<Mutex<Machines>>,
    root_path: Option<&str>,
    cols: u16,
    rows: u16,
    network: bool,
) -> Result<String, String> {
    use tauri::Emitter;
    let cmd = prepare(
        root_path,
        network,
        &std::env::var("PATH").unwrap_or_default(),
        None,
    )?;
    let sink = app.clone();
    open_with_sink(
        machines,
        cmd,
        cols,
        rows,
        move |id, text| {
            let _ = sink.emit(
                MACHINE_OUTPUT_EVENT,
                MachineOutput {
                    id: id.to_string(),
                    data: text,
                },
            );
        },
        move |id| {
            let _ = app.emit(MACHINE_EXIT_EVENT, MachineExit { id });
        },
    )
}

/// Send keystrokes to a machine. An unknown id is an error, and so is a write too large to be typing.
pub fn write(machines: &Arc<Mutex<Machines>>, id: &str, data: &str) -> Result<(), String> {
    if data.len() > MAX_WRITE_BYTES {
        return Err(format!(
            "that is {} bytes and a machine takes at most {MAX_WRITE_BYTES} at a time",
            data.len()
        ));
    }
    let mut guard = machines.lock().map_err(|_| "machine state poisoned")?;
    let session = guard
        .sessions
        .get_mut(id)
        .ok_or_else(|| format!("no such machine: {id}"))?;
    session.send(data)
}

/// Resize the machine's PTY, so a command wraps at the pane's width.
pub fn resize(
    machines: &Arc<Mutex<Machines>>,
    id: &str,
    cols: u16,
    rows: u16,
) -> Result<(), String> {
    let guard = machines.lock().map_err(|_| "machine state poisoned")?;
    let session = guard
        .sessions
        .get(id)
        .ok_or_else(|| format!("no such machine: {id}"))?;
    session.resize(cols, rows)
}

/// Close a machine and reap what runs in it. A second close is a clean no-op.
pub fn close(machines: &Arc<Mutex<Machines>>, id: &str) -> Result<(), String> {
    let session = {
        let mut guard = machines.lock().map_err(|_| "machine state poisoned")?;
        guard.sessions.remove(id)
    };
    if let Some(session) = session {
        terminal::kill_and_reap(session.child);
    }
    Ok(())
}

/// Close every machine. Called when the app exits, so a quit does not leave a jail running.
pub fn close_all(machines: &Arc<Mutex<Machines>>) {
    let children: Vec<_> = if let Ok(mut guard) = machines.lock() {
        guard.sessions.drain().map(|(_, s)| s.child).collect()
    } else {
        return;
    };
    for child in children {
        terminal::kill_and_reap(child);
    }
}

#[cfg(test)]
mod tests;
