//! A machine: a jailed Linux shell the assistant may type into, and the boundary around it.
//!
//! **What this is.** A disposable userland in a `bubblewrap` jail — its own PID, mount, network, IPC,
//! UTS and user namespaces, a read-only view of the system, a *copy-on-write* view of the workspace at
//! `/work` (the person's files are the lower layer and are never written: [`layer`]), and a scratch home
//! that is a size-capped tmpfs thrown away with the machine. The window
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
//! * **The person's files are never written, and nothing else that is bound can be.** The system and the
//!   workspace are bound read-only. What the machine changes under `/work` goes to an overlay's upper layer,
//!   a size-capped tmpfs in the machine's own mount namespace ([`layer`]), so it is the *machine's* copy of
//!   the project that changes and it is gone when the machine is. The one writable bind in [`jail_argv`] is
//!   that overlay's merged directory, and the tests do not read that off the flags: they write through it in
//!   a real jail and then look at the host's files. Where this host cannot make the layer the workspace is
//!   bound read-only, as it was before, and the machine says so. Nothing leaves the jail except as text on the
//!   screen: invariant 9 is not amended.
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
//! * **Memory is watched, not capped by the kernel.** `ulimit` bounds processes, CPU per process, file size
//!   and core files, the tmpfs sizes bound what the machine can keep, and [`guard`] ends a machine whose
//!   processes add up to more than its share, by polling. A cgroup would be stronger and needs systemd or
//!   root. A kernel bug is not contained by any of this: the jail has no kernel of its own to fail.
//! * **No `--new-session`.** It would detach the jail from the PTY this module owns, and with it job
//!   control and Ctrl-C. The terminal-injection it guards against (`TIOCSTI`) reaches only this PTY's
//!   own input queue, which belongs to the jail already.

mod guard;
mod layer;

use crate::terminal::{self, Session};
use guard::Limits;
use layer::{ProjectNote, Stage};
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
    // Bytecode written next to the code would only fill the project layer, which is memory.
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
    /// The workspace, absolute and already resolved. It is what [`WORK`] shows, one way or the other.
    pub workspace: PathBuf,
    /// Whether the machine shares the host's network. Off unless the person opened it with network.
    pub network: bool,
    /// The effective uid of the shell, to decide whether the jail may map to it.
    pub uid: u32,
    /// How the workspace is shown at [`WORK`].
    pub project: ProjectView,
    /// What the shell inside is allowed to use. The kernel-enforced part goes into the jail's own argv.
    pub limits: Limits,
}

/// How the project appears at `/work`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ProjectView {
    /// The workspace itself, bound read-only.
    ReadOnly,
    /// The merged directory of an overlay the launcher mounted in front of the jail: the workspace is its
    /// read-only lower layer, so what is written lands in the machine's own layer ([`layer`]).
    CopyOnWrite { merged: PathBuf },
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
/// Read it as the jail's whole definition. The only `--bind` (read-write) in it is the project layer's
/// merged directory, when there is one, and it is bound at `/work` and nowhere else. There is no bind of
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
    let layered = matches!(spec.project, ProjectView::CopyOnWrite { .. });
    if spec.uid == 0 || layered {
        // Root in a user namespace that maps to host root is still host root for any file it can reach.
        // Behind the project layer the launcher's namespace maps the person to root, so the jail is told
        // what to be rather than left to inherit it.
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
    match &spec.project {
        ProjectView::ReadOnly => push(&["--ro-bind", &spec.workspace.to_string_lossy(), WORK]),
        ProjectView::CopyOnWrite { merged } => push(&["--bind", &merged.to_string_lossy(), WORK]),
    }
    push(&["--chdir", WORK]);

    // The shell, behind the limits `bwrap` has no flag for: no core files, no fork bomb, no one process
    // that burns CPU for ever, no single huge file. `ulimit -f` counts 1024-byte blocks in bash and
    // 512-byte blocks in dash, so the cap is a gigabyte or half of one, and either is the point.
    // `bash` where the host has it (its readline is what a person expects), `sh` where it does not. The two
    // get two prompts, because only bash reads `\w` and `\$` as escapes; POSIX `sh` expands parameters in
    // `PS1` and nothing else, so its prompt names the directory with `$PWD`. Either way it is
    // `[machine] <directory> $ `, the marker the assistant looks for. The bash prompt is assigned on bash's
    // branch only; the fallback sets its own, and neither adds a command the person did not type.
    let wrapper = format!(
        "ulimit -c 0 2>/dev/null; ulimit -u {NPROC} 2>/dev/null; \
         ulimit -t {cpu} 2>/dev/null; ulimit -f {blocks} 2>/dev/null; \
         b=$(command -v bash) && PS1='[machine] \\w \\\\$ ' && export PS1 && exec \"$b\"; \
         PS1='[machine] $PWD $ '; export PS1; exec sh",
        cpu = spec.limits.cpu_secs,
        blocks = spec.limits.file_bytes / 1024,
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

/// What was chosen before a machine existed, and so everything needed to make the same one again. A reset
/// is `prepare` over the same recipe: the same project, the same network, the same limits, a new jail.
#[derive(Debug, Clone)]
pub(crate) struct Recipe {
    pub root_path: Option<String>,
    pub network: bool,
    pub path_var: String,
    /// The effective uid, or `None` to ask the OS. A parameter so a test can be any user.
    pub uid: Option<u32>,
    pub limits: Limits,
    /// Where the project layer's empty mount point is made, or `None` for a read-only project by request.
    /// A host that cannot make the layer is read-only whatever this says.
    pub layer_base: Option<PathBuf>,
}

/// A machine that is running: its PTY, what made it, and which generation of it this is.
struct Entry {
    session: Session,
    recipe: Recipe,
    stage: Option<Stage>,
    /// Which start this is. A reset replaces an entry with the next generation, and the end of the old
    /// one's output is not the machine exiting.
    epoch: u64,
}

/// The machines the shell holds. Its own type, so Tauri's state lookup cannot hand a terminal's
/// registry to a machine's command.
#[derive(Default)]
pub struct Machines {
    entries: HashMap<String, Entry>,
    seq: u64,
    epoch: u64,
}

/// Where a machine's text goes, and who hears that it ended. Shared by the thread that reads the PTY and
/// the one that watches memory, so both can say something to the pane.
pub(crate) type TextSink = Arc<dyn Fn(&str, String) + Send + Sync>;
pub(crate) type ExitSink = Arc<dyn Fn(String) + Send + Sync>;

/// What a machine's reader thread does: pass the machine's output on, then say it ended.
///
/// `current` is the epoch the registry holds for this machine's id now (`None` once it is gone), and
/// `epoch` is the one this reader was started for. A reset puts its own line on the screen the moment
/// it has ended the old machine, and this reader still holds what it had read: a batch waiting for its
/// turn (`terminal::BATCH_WINDOW`), a backlog queued behind it, and whatever the PTY had buffered. Left
/// alone that text would land after the reset line, on a screen that had just been cleared, so a reader
/// whose machine has been **replaced** (an entry with a different epoch) drops what it still holds.
/// `current` takes the registry's lock, which a reset holds from the old machine's end to the new one's
/// start, so a chunk is either delivered before the reset line or dropped after it, never in between.
///
/// A machine that was reset did not exit: its replacement is already there. One that was *closed* has
/// no entry at all, and that has always been reported as an exit (and its last output delivered).
pub(crate) fn run_reader<R: std::io::Read + Send + 'static>(
    reader: R,
    epoch: u64,
    current: impl Fn() -> Option<u64>,
    mut text: impl FnMut(String),
    exit: impl FnOnce(),
) {
    let replaced = || matches!(current(), Some(now) if now != epoch);
    terminal::pump_output(reader, |chunk| {
        if !replaced() {
            text(chunk);
        }
    });
    if !replaced() {
        exit();
    }
}

/// What opening a machine tells the window: its id, and whether the project under `/work` is its own copy.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OpenedMachine {
    pub id: String,
    /// Changes under `/work` stay in this machine and are discarded with it.
    pub copy_on_write: bool,
    /// When they are not: why this host could not make the layer.
    pub note: Option<String>,
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

/// A machine, ready to start: the program, and the layer's mount point if there is one.
pub(crate) struct Launch {
    pub cmd: CommandBuilder,
    pub stage: Option<Stage>,
    pub project: ProjectNote,
}

/// The layer, or the reason there is none. Every step that can fail says what it was, because the person
/// is told: a read-only project is a decision the host made, and it should not be a mystery.
fn make_layer(base: &Path, workspace: &Path, path_var: &str) -> Result<(Stage, PathBuf), String> {
    layer::overlay_safe(workspace)
        .map_err(|why| format!("this project's {}", why.replacen("its ", "", 1)))?;
    let unshare = layer::find_on_path("unshare", path_var)
        .ok_or_else(|| "unshare (util-linux) is not installed".to_string())?;
    if !Path::new("/bin/sh").exists() {
        return Err("there is no /bin/sh to run the layer's mount step".to_string());
    }
    layer::probe(base, &unshare, path_var)?;
    let stage = Stage::create(base)?;
    Ok((stage, unshare))
}

/// Everything about a machine that is settled before a process exists: the program, the jail and what
/// refuses it. Split from [`open`] so a test can drive the real jail with a closure instead of an
/// `AppHandle`, the way `terminal::pump_output` is driven.
pub(crate) fn prepare(recipe: &Recipe) -> Result<Launch, String> {
    let workspace = pin_workspace(recipe.root_path.as_deref())?;
    let bwrap = find_bwrap(&recipe.path_var).ok_or_else(missing_bwrap)?;
    // SAFETY: `geteuid` takes no arguments, cannot fail and touches no memory.
    let uid = recipe.uid.unwrap_or_else(|| unsafe { libc::geteuid() });
    if let Some(why) = userns_refusal(uid == 0, read_sysctl) {
        return Err(why);
    }

    let (view, stage, project, unshare) = match &recipe.layer_base {
        None => (
            ProjectView::ReadOnly,
            None,
            ProjectNote::read_only("a read-only project was asked for"),
            None,
        ),
        Some(base) => match make_layer(base, &workspace, &recipe.path_var) {
            Ok((stage, unshare)) => (
                ProjectView::CopyOnWrite {
                    merged: stage.merged(),
                },
                Some(stage),
                ProjectNote::layered(),
                Some(unshare),
            ),
            Err(why) => (
                ProjectView::ReadOnly,
                None,
                ProjectNote::read_only(why),
                None,
            ),
        },
    };
    let spec = JailSpec {
        workspace: workspace.clone(),
        network: recipe.network,
        uid,
        project: view,
        limits: recipe.limits.clone(),
    };
    let argv = jail_argv(&spec, &HostLayout::detect());

    // The one `CommandBuilder` in this module. It starts `bwrap`, or, behind the project layer, `unshare`
    // running the fixed mount script that then `exec`s `bwrap` with this same argv. The shell it starts is
    // named in the jail's own argv, inside the namespaces; nothing here can start one outside them.
    let (program, args): (PathBuf, Vec<String>) = match (&stage, &unshare) {
        (Some(stage), Some(unshare)) => (
            unshare.clone(),
            layer::launch_args(
                stage.dir(),
                recipe.limits.layer_bytes,
                &workspace,
                &bwrap,
                &argv,
            ),
        ),
        _ => (bwrap, argv),
    };
    let mut cmd = CommandBuilder::new(program);
    cmd.args(args);
    // Neither program needs anything from the shell's environment; the jailed process gets its own.
    cmd.env_clear();
    cmd.env("PATH", &recipe.path_var);
    Ok(Launch {
        cmd,
        stage,
        project,
    })
}

/// Start a machine's process under `id` and the threads that serve it, with `guard` held. The one place a
/// machine's process is started, for a machine that is opening and for one that is being reset.
fn start(
    guard: &mut Machines,
    machines: &Arc<Mutex<Machines>>,
    id: &str,
    recipe: &Recipe,
    (cols, rows): (u16, u16),
    on_text: &TextSink,
    on_exit: &ExitSink,
) -> Result<ProjectNote, String> {
    let Launch {
        cmd,
        stage,
        project,
    } = prepare(recipe)?;
    let (session, reader) = match terminal::spawn_pty(cmd, cols, rows) {
        Ok(started) => started,
        Err(why) => {
            if let Some(stage) = stage {
                stage.release();
            }
            return Err(why);
        }
    };
    let pid = session.child.process_id();
    guard.epoch += 1;
    let epoch = guard.epoch;
    guard.entries.insert(
        id.to_string(),
        Entry {
            session,
            recipe: recipe.clone(),
            stage,
            epoch,
        },
    );

    let current = {
        let (machines, id) = (machines.clone(), id.to_string());
        move || {
            machines
                .lock()
                .map(|m| m.entries.get(&id).map(|e| e.epoch))
                .unwrap_or(None)
        }
    };
    let (text, exit) = (on_text.clone(), on_exit.clone());
    let (read_id, exit_id, read_current) = (id.to_string(), id.to_string(), current.clone());
    std::thread::spawn(move || {
        run_reader(
            reader,
            epoch,
            read_current,
            |chunk| text(&read_id, chunk),
            || exit(exit_id),
        );
    });
    if let Some(pid) = pid {
        let (watch_id, say) = (id.to_string(), on_text.clone());
        let limits = recipe.limits.clone();
        std::thread::spawn(move || {
            guard::watch(
                pid,
                &limits,
                || current() == Some(epoch),
                |line| say(&watch_id, line),
            );
        });
    }
    Ok(project)
}

/// A machine, started and registered; its output goes to `on_text` and its end to `on_exit`.
pub(crate) fn open_with_sink(
    machines: &Arc<Mutex<Machines>>,
    recipe: &Recipe,
    cols: u16,
    rows: u16,
    on_text: TextSink,
    on_exit: ExitSink,
) -> Result<OpenedMachine, String> {
    let mut guard = machines.lock().map_err(|_| "machine state poisoned")?;
    if guard.entries.len() >= MAX_MACHINES {
        return Err(format!(
            "{MAX_MACHINES} machines are already open — close one first"
        ));
    }
    guard.seq += 1;
    let id = format!("mach-{}", guard.seq);
    let project = start(
        &mut guard,
        machines,
        &id,
        recipe,
        (cols, rows),
        &on_text,
        &on_exit,
    )?;
    Ok(OpenedMachine {
        id,
        copy_on_write: project.copy_on_write,
        note: project.reason,
    })
}

/// Throw away everything a machine has done and start it again from a clean project: the same project, network and limits, a new jail and a new layer.
///
/// This is what "recover" means for a machine that is wedged, filled, or was stopped for using too
/// much: nothing is repaired, it is replaced. It changes nothing a person chose (not the network, not the
/// project) because it starts from the recipe the machine was made from. The old process tree is ended,
/// its layer goes with it, and the screen is cleared and says so.
pub(crate) fn reset_with_sink(
    machines: &Arc<Mutex<Machines>>,
    id: &str,
    cols: u16,
    rows: u16,
    on_text: TextSink,
    on_exit: ExitSink,
) -> Result<OpenedMachine, String> {
    // The lock is held from the old machine's end to the new one's start. The old reader thread, when it
    // sees its machine end, asks who is current, and has to find the new one and not an empty place.
    let mut guard = machines.lock().map_err(|_| "machine state poisoned")?;
    let old = guard
        .entries
        .remove(id)
        .ok_or_else(|| format!("no such machine: {id}"))?;
    let recipe = old.recipe.clone();
    terminal::kill_and_reap(old.session.child);
    if let Some(stage) = old.stage {
        stage.release();
    }
    // A full reset of the terminal (RIS), then a line that says what happened.
    on_text(
        id,
        "\x1bc[machine reset: a clean project, nothing from before]\r\n".to_string(),
    );
    match start(
        &mut guard,
        machines,
        id,
        &recipe,
        (cols, rows),
        &on_text,
        &on_exit,
    ) {
        Ok(project) => Ok(OpenedMachine {
            id: id.to_string(),
            copy_on_write: project.copy_on_write,
            note: project.reason,
        }),
        Err(why) => {
            // The machine is gone and could not be remade: say so the way an exit does.
            on_exit(id.to_string());
            Err(why)
        }
    }
}

fn sinks(app: &tauri::AppHandle) -> (TextSink, ExitSink) {
    use tauri::Emitter;
    let (out, gone) = (app.clone(), app.clone());
    (
        Arc::new(move |id, text| {
            let _ = out.emit(
                MACHINE_OUTPUT_EVENT,
                MachineOutput {
                    id: id.to_string(),
                    data: text,
                },
            );
        }),
        Arc::new(move |id| {
            let _ = gone.emit(MACHINE_EXIT_EVENT, MachineExit { id });
        }),
    )
}

/// The directory a machine's project layer is mounted over: the system's temporary directory.
fn layer_base() -> PathBuf {
    std::env::temp_dir()
}

fn recipe_for(root_path: Option<&str>, network: bool) -> Recipe {
    Recipe {
        root_path: root_path.map(str::to_string),
        network,
        path_var: std::env::var("PATH").unwrap_or_default(),
        uid: None,
        limits: Limits::default(),
        layer_base: Some(layer_base()),
    }
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
) -> Result<OpenedMachine, String> {
    let (on_text, on_exit) = sinks(&app);
    open_with_sink(
        machines,
        &recipe_for(root_path, network),
        cols,
        rows,
        on_text,
        on_exit,
    )
}

/// Reset a machine to a clean project. See [`reset_with_sink`].
pub fn reset(
    app: tauri::AppHandle,
    machines: &Arc<Mutex<Machines>>,
    id: &str,
    cols: u16,
    rows: u16,
) -> Result<OpenedMachine, String> {
    let (on_text, on_exit) = sinks(&app);
    reset_with_sink(machines, id, cols, rows, on_text, on_exit)
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
    let entry = guard
        .entries
        .get_mut(id)
        .ok_or_else(|| format!("no such machine: {id}"))?;
    entry.session.send(data)
}

/// Resize the machine's PTY, so a command wraps at the pane's width.
pub fn resize(
    machines: &Arc<Mutex<Machines>>,
    id: &str,
    cols: u16,
    rows: u16,
) -> Result<(), String> {
    let guard = machines.lock().map_err(|_| "machine state poisoned")?;
    let entry = guard
        .entries
        .get(id)
        .ok_or_else(|| format!("no such machine: {id}"))?;
    entry.session.resize(cols, rows)
}

/// Close a machine and reap what runs in it. A second close is a clean no-op.
pub fn close(machines: &Arc<Mutex<Machines>>, id: &str) -> Result<(), String> {
    let entry = {
        let mut guard = machines.lock().map_err(|_| "machine state poisoned")?;
        guard.entries.remove(id)
    };
    if let Some(entry) = entry {
        terminal::kill_and_reap(entry.session.child);
        if let Some(stage) = entry.stage {
            stage.release();
        }
    }
    Ok(())
}

/// Close every machine. Called when the app exits, so a quit does not leave a jail running.
pub fn close_all(machines: &Arc<Mutex<Machines>>) {
    let entries: Vec<Entry> = if let Ok(mut guard) = machines.lock() {
        guard.entries.drain().map(|(_, e)| e).collect()
    } else {
        return;
    };
    for entry in entries {
        terminal::kill_and_reap(entry.session.child);
        if let Some(stage) = entry.stage {
            stage.release();
        }
    }
}

#[cfg(test)]
mod tests;
