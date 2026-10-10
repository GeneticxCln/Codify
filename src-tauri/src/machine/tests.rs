//! The jail, asserted twice: as a **definition** (the argv, a pure function, and the rules around it), and
//! as a **fact** (a real `bwrap` jail, driven through a real PTY, asked what it can see).
//!
//! The second half is the one that matters. A flag list that reads right and a jail that holds are
//! different claims, and the first is what a refactor quietly breaks. These tests need `bwrap` and
//! unprivileged user namespaces, and **fail loudly without them** rather than skipping: a containment
//! guarantee that is skipped on the machine that lacks the mechanism is a guarantee that passes
//! exactly where it is false. `make doctor` says whether this machine has them.

use super::*;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

fn production_source() -> &'static str {
    let all = include_str!("../machine.rs");
    all.split_once("#[cfg(test)]")
        .expect("machine.rs ends in its test module")
        .0
}

/// Every file of the machine's production code: the jail, its project layer and its guard.
fn all_production_source() -> String {
    [
        production_source(),
        include_str!("layer.rs"),
        include_str!("guard.rs"),
    ]
    .join("\n")
}

fn layout() -> HostLayout {
    HostLayout {
        top: vec![
            TopLevel::Link {
                at: "/bin".into(),
                target: "usr/bin".into(),
            },
            TopLevel::Dir("/lib32".into()),
        ],
        etc: vec!["/etc/ssl".into(), "/etc/passwd".into()],
        resolv: Some(PathBuf::from("/run/systemd/resolve/stub-resolv.conf")),
    }
}

fn spec(network: bool, uid: u32) -> JailSpec {
    JailSpec {
        workspace: PathBuf::from("/srv/project"),
        network,
        uid,
        project: ProjectView::ReadOnly,
        limits: Limits::default(),
    }
}

fn layered_spec(uid: u32) -> JailSpec {
    JailSpec {
        project: ProjectView::CopyOnWrite {
            merged: PathBuf::from("/tmp/codify-machine-1-0/merged"),
        },
        ..spec(false, uid)
    }
}

/// A recipe for a real machine on `workspace`, with its layer made under `layers`.
fn recipe(workspace: &Path, network: bool, layers: Option<&Path>) -> Recipe {
    Recipe {
        root_path: Some(workspace.to_string_lossy().into_owned()),
        network,
        path_var: std::env::var("PATH").unwrap_or_default(),
        uid: None,
        limits: Limits::default(),
        layer_base: layers.map(Path::to_path_buf),
    }
}

fn has_pair(argv: &[String], a: &str, b: &str) -> bool {
    argv.windows(2).any(|w| w[0] == a && w[1] == b)
}

fn has_triple(argv: &[String], a: &str, b: &str, c: &str) -> bool {
    argv.windows(3).any(|w| w[0] == a && w[1] == b && w[2] == c)
}

// ── the jail as a definition ─────────────────────────────────────────────────────────────────────

#[test]
fn the_jail_is_defined_as_much_by_what_it_leaves_out_as_by_what_it_has() {
    let argv = jail_argv(&spec(false, 1000), &layout());

    for needed in ["--unshare-all", "--die-with-parent", "--clearenv"] {
        assert!(argv.iter().any(|a| a == needed), "the jail lost {needed}");
    }
    assert!(
        has_pair(&argv, "--cap-drop", "ALL"),
        "capabilities are not dropped"
    );
    assert!(
        !argv.iter().any(|a| a == "--share-net"),
        "a machine with no network switched on shares the host's"
    );

    // Nothing is writable that is not a tmpfs. A read-write bind of anything is a door onto the host.
    for flag in &argv {
        assert!(
            !flag.starts_with("--bind") && !flag.starts_with("--dev-bind"),
            "{flag} is a writable or device bind; the jail may only bind read-only"
        );
    }
    // The workspace is shown read-only, and the shell starts in it.
    assert!(
        has_triple(&argv, "--ro-bind", "/srv/project", WORK),
        "the workspace is not bound read-only at /work"
    );
    assert!(has_pair(&argv, "--chdir", WORK));

    // None of the things a person keeps that an assistant must not reach are even named.
    let all = argv.join("\u{1}");
    for secret in [
        ".ssh",
        ".gnupg",
        ".aws",
        ".kube",
        ".codify",
        "keyring",
        "dbus",
        "wayland",
        "X11",
        "/run/user",
    ] {
        assert!(!all.contains(secret), "the jail's argv names {secret}");
    }

    // `--clearenv` comes before every `--setenv`, so the listed variables are *all* the environment.
    let clear = argv.iter().position(|a| a == "--clearenv").unwrap();
    let keys: Vec<&str> = argv
        .windows(2)
        .enumerate()
        .filter(|(_, w)| w[0] == "--setenv")
        .map(|(i, w)| {
            assert!(
                i > clear,
                "an environment variable is set before the environment is cleared"
            );
            w[1].as_str()
        })
        .collect();
    let mut expected: Vec<&str> = JAIL_ENV.iter().map(|(k, _)| *k).collect();
    let mut got = keys.clone();
    expected.sort_unstable();
    got.sort_unstable();
    assert_eq!(
        got, expected,
        "the jail's environment is not exactly JAIL_ENV"
    );
}

#[test]
fn behind_the_layer_the_only_writable_bind_is_the_layers_merged_directory_at_work() {
    let argv = jail_argv(&layered_spec(1000), &layout());

    // Exactly one writable bind, from the overlay's merged directory, to /work, and nothing else writable.
    let binds: Vec<&[String]> = argv
        .windows(3)
        .filter(|w| w[0] == "--bind" || w[0] == "--dev-bind")
        .collect();
    assert_eq!(
        binds.len(),
        1,
        "the layered jail has {} writable binds",
        binds.len()
    );
    assert_eq!(binds[0][0], "--bind");
    assert_eq!(binds[0][1], "/tmp/codify-machine-1-0/merged");
    assert_eq!(binds[0][2], WORK);

    // The person's own directory is not bound into the jail at all: it is the overlay's lower layer, and the
    // launcher's script is the only thing that names it.
    assert!(
        !argv.iter().any(|a| a == "/srv/project"),
        "the workspace itself is bound into a layered jail"
    );
    assert!(!has_triple(&argv, "--ro-bind", "/srv/project", WORK));

    // The launcher's namespace makes the person root, so the jail is told what to be, whoever started it.
    let id = UNPRIVILEGED_ID.to_string();
    assert!(has_triple(&argv, "--uid", &id, "--gid") && has_pair(&argv, "--gid", &id));
    // Everything else about the jail is unchanged: no capabilities, no network, nothing inherited.
    for needed in ["--unshare-all", "--die-with-parent", "--clearenv"] {
        assert!(
            argv.iter().any(|a| a == needed),
            "the layered jail lost {needed}"
        );
    }
    assert!(has_pair(&argv, "--cap-drop", "ALL"));
    assert!(!argv.iter().any(|a| a == "--share-net"));
}

#[test]
fn the_shell_starts_behind_every_limit_the_kernel_can_enforce() {
    let mut limited = spec(false, 1000);
    limited.limits = Limits {
        cpu_secs: 90,
        file_bytes: 8 * 1024 * 1024,
        ..Limits::default()
    };
    let argv = jail_argv(&limited, &layout());
    let wrapper = argv.last().expect("the jail ends in the shell's wrapper");
    for expected in [
        "ulimit -c 0".to_string(),
        format!("ulimit -u {NPROC}"),
        "ulimit -t 90".to_string(),
        "ulimit -f 8192".to_string(),
        "export PS1".to_string(),
    ] {
        assert!(
            wrapper.contains(&expected),
            "the wrapper lacks `{expected}`: {wrapper}"
        );
    }
    // The limits are set before the shell starts, and the shell is the last thing the wrapper does.
    assert!(wrapper.find("ulimit -t").unwrap() < wrapper.find("exec").unwrap());
}

// ── ps1: each shell gets a prompt it can expand ───────────────────────────────────────────────────

/// The wrapper's two halves: what runs when the host has `bash`, and what runs when it does not.
fn wrapper_halves() -> (String, String) {
    let argv = jail_argv(&spec(false, 1000), &layout());
    let wrapper = argv.last().expect("the jail ends in the shell's wrapper");
    let (bash, fallback) = wrapper
        .split_once("exec \"$b\"")
        .expect("the wrapper execs the bash it found");
    (bash.to_string(), fallback.to_string())
}

#[test]
fn the_fallback_shell_gets_a_prompt_it_can_expand_and_bash_keeps_its_own() {
    let (bash, fallback) = wrapper_halves();
    // Bash's prompt is byte-for-byte what it always was: `\w` and `\\$` are bash's own escapes.
    let bash_prompt = "PS1='[machine] \\w \\\\$ '";
    assert_eq!(bash.matches("PS1=").count(), 1, "{bash}");
    assert!(bash.contains(bash_prompt), "bash's prompt changed: {bash}");
    // POSIX `sh` (dash and its kind) expands parameters in `PS1` and nothing else: `\w` and `\$` are shown
    // as typed. So the fallback sets its own, from `$PWD`, in the same fixed script, and then execs `sh`.
    assert!(
        fallback.contains("PS1='[machine] $PWD $ '") && fallback.contains("export PS1"),
        "the fallback has no prompt of its own: {fallback}"
    );
    assert_eq!(fallback.matches("PS1=").count(), 1, "{fallback}");
    assert!(
        !fallback.contains('\\'),
        "a backslash escape in the fallback's prompt is shown literally by `sh`: {fallback}"
    );
    assert!(fallback.trim_end().ends_with("exec sh"), "{fallback}");
    // Bash's prompt is assigned only on the branch that finds bash: it is part of the `command -v bash`
    // chain, so what the fallback shell starts with is its own prompt and not bash's.
    assert!(
        bash.contains(&format!(
            "b=$(command -v bash) && {bash_prompt} && export PS1 && "
        )),
        "bash's prompt is not on bash's branch: {bash}"
    );
}

#[test]
fn the_network_is_the_persons_choice_and_only_theirs() {
    let on = jail_argv(&spec(true, 1000), &layout());
    assert_eq!(on.iter().filter(|a| *a == "--share-net").count(), 1);
    assert!(
        has_triple(
            &on,
            "--ro-bind",
            "/run/systemd/resolve/stub-resolv.conf",
            "/etc/resolv.conf"
        ),
        "a machine with a network cannot resolve a name"
    );
    let off = jail_argv(&spec(false, 1000), &layout());
    assert!(
        !off.iter().any(|a| a.contains("resolv.conf")),
        "a machine with no network was given a resolver to use"
    );
}

#[test]
fn a_root_shell_never_becomes_root_in_the_jail() {
    let root = jail_argv(&spec(false, 0), &layout());
    assert!(has_triple(&root, "--uid", "1000", "--gid") && has_pair(&root, "--gid", "1000"));
    let user = jail_argv(&spec(false, 1000), &layout());
    assert!(
        !user.iter().any(|a| a == "--uid"),
        "a user's own uid was remapped for no reason"
    );
}

#[test]
fn a_merged_usr_host_and_an_unmerged_one_are_both_expressed() {
    let argv = jail_argv(&spec(false, 1000), &layout());
    assert!(has_triple(&argv, "--symlink", "usr/bin", "/bin"));
    assert!(has_triple(&argv, "--ro-bind", "/lib32", "/lib32"));
    assert!(has_triple(&argv, "--ro-bind", "/usr", "/usr"));
}

#[test]
fn what_stops_a_jail_from_being_made_is_said_before_trying() {
    let table = |entries: &'static [(&'static str, &'static str)]| {
        move |path: &str| {
            entries
                .iter()
                .find(|(p, _)| *p == path)
                .map(|(_, v)| v.to_string())
        }
    };
    // Disabled outright: refused, root or not.
    let none = table(&[("/proc/sys/user/max_user_namespaces", "0\n")]);
    assert!(userns_refusal(false, none)
        .unwrap()
        .contains("max_user_namespaces"));
    assert!(userns_refusal(true, none).is_some());
    // Debian's switch binds an unprivileged user and not root.
    let debian = table(&[("/proc/sys/kernel/unprivileged_userns_clone", "0\n")]);
    assert!(userns_refusal(false, debian)
        .unwrap()
        .contains("unprivileged_userns_clone"));
    assert!(userns_refusal(true, debian).is_none());
    // Ubuntu's AppArmor restriction is not a refusal: it cannot say whether `bwrap` is among the blocked.
    let ubuntu = table(&[(
        "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
        "1\n",
    )]);
    assert!(userns_refusal(false, ubuntu).is_none());
    // Nothing readable, nothing to say.
    assert!(userns_refusal(false, |_: &str| None).is_none());
}

#[test]
fn bwrap_is_found_on_the_path_or_not_at_all() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile_dir("find-bwrap");
    assert!(find_bwrap("").is_none(), "an empty PATH found a bwrap");
    assert!(
        find_bwrap(&dir.to_string_lossy()).is_none(),
        "an empty directory found one"
    );

    let file = dir.join("bwrap");
    std::fs::write(&file, "#!/bin/sh\n").unwrap();
    std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o644)).unwrap();
    assert!(
        find_bwrap(&dir.to_string_lossy()).is_none(),
        "a file that cannot be executed was taken for bwrap"
    );

    std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!(
        find_bwrap(&format!("/nonexistent:{}", dir.display())),
        Some(file.clone())
    );

    std::fs::remove_file(&file).unwrap();
    std::fs::create_dir(&file).unwrap();
    assert!(
        find_bwrap(&dir.to_string_lossy()).is_none(),
        "a directory named bwrap was taken for it"
    );
}

#[test]
fn a_workspace_that_would_hand_over_the_persons_secrets_is_refused() {
    let home = PathBuf::from("/home/someone");
    let state = vec![
        PathBuf::from("/home/someone/.codify"),
        PathBuf::from("/var/lib/codify"),
    ];
    let refused = |p: &str| protected_reason(Path::new(p), Some(&home), &state);

    for p in [
        "/",
        "/home",
        "/home/someone",
        "/home/someone/.ssh",
        "/home/someone/.ssh/keys",
        "/home/someone/.gnupg",
        "/home/someone/.aws",
        "/home/someone/.kube/config.d",
        "/home/someone/.codify",
        "/home/someone/.codify/workspaces/a",
        "/var/lib/codify/db",
    ] {
        assert!(refused(p).is_some(), "{p} may be shown to a machine");
    }
    // Ordinary places, and a name that only *looks* like a protected one.
    for p in [
        "/home/someone/projects/app",
        "/home/someone/.sshfs-notes",
        "/srv/project",
        "/home/other",
    ] {
        assert!(refused(p).is_none(), "{p} was refused: {:?}", refused(p));
    }
    // With no home known, only the root and Codify's state can be named.
    assert!(protected_reason(Path::new("/"), None, &[]).is_some());
    assert!(protected_reason(Path::new("/home/someone"), None, &[]).is_none());
}

#[test]
fn a_machine_opens_only_on_a_real_absolute_directory() {
    assert!(pin_workspace(None).unwrap_err().contains("no root path"));
    assert!(pin_workspace(Some("   "))
        .unwrap_err()
        .contains("no root path"));
    assert!(pin_workspace(Some("work/dir"))
        .unwrap_err()
        .contains("not absolute"));
    assert!(pin_workspace(Some("/definitely/not/here"))
        .unwrap_err()
        .contains("cannot be resolved"));

    let dir = tempfile_dir("pin-workspace");
    let file = dir.join("a-file");
    std::fs::write(&file, "x").unwrap();
    assert!(pin_workspace(Some(&file.to_string_lossy()))
        .unwrap_err()
        .contains("not a directory"));
    assert!(pin_workspace(Some("/"))
        .unwrap_err()
        .contains("filesystem root"));

    let ok = pin_workspace(Some(&dir.to_string_lossy()))
        .expect("a scratch directory is a fine workspace");
    assert_eq!(
        ok,
        std::fs::canonicalize(dir.as_path()).unwrap(),
        "the path is not resolved before it is bound"
    );
}

#[test]
fn nothing_here_can_start_the_users_shell() {
    let src = production_source();
    let everything = all_production_source();
    // One builder in the jail's module. Its program is `bwrap`, or `unshare` running the fixed mount script
    // that then execs `bwrap`: never anything else. The jail's shell is named in the jail's own argv. (The
    // needle is assembled at run time: `tests/test_no_unguarded_spawns.py` reads this file too, and a literal
    // would be a spawn site of its own.)
    let builder = format!("{}::new(", "CommandBuilder");
    assert_eq!(
        src.matches(&builder).count(),
        1,
        "machine.rs builds a second command; every one of them has to be inside the jail"
    );
    assert!(src.contains(&format!("{builder}program)")));
    let flat = src.split_whitespace().collect::<Vec<_>>().join(" ");
    assert!(
        flat.contains("unshare.clone(), layer::launch_args(")
            && flat.contains("_ => (bwrap, argv)"),
        "the program is no longer exactly one of bwrap and the layer's launcher"
    );
    for banned in ["shell_command", "terminal::open", "\"SHELL\"", "$SHELL"] {
        assert!(
            !everything.contains(banned),
            "the machine's code mentions {banned}: a machine must have no way to start the person's own shell"
        );
    }
    // The probe is the only other process the machine's code starts, and it is `unshare` over the same fixed
    // script and a program of its own, never the person's shell and never a request's text.
    let probe = format!("{}::new(", "Command");
    let layer_src = include_str!("layer.rs");
    assert_eq!(layer_src.matches(&probe).count(), 1);
    assert!(layer_src.contains(&format!("{probe}unshare)")));
    assert_eq!(src.matches(&probe).count(), 0);
    assert_eq!(include_str!("guard.rs").matches(&probe).count(), 0);
    // The shell's own environment does not reach `bwrap` or `unshare` either.
    assert!(src.contains("cmd.env_clear()") && layer_src.contains(".env_clear()"));
    // The one writable bind in the source is the layer's merged directory, at /work; there is no device
    // bind. (The argv tests assert the same on the result.)
    assert_eq!(src.matches("\"--bind\"").count(), 1);
    assert!(src.contains(
        "ProjectView::CopyOnWrite { merged } => push(&[\"--bind\", &merged.to_string_lossy(), WORK])"
    ));
    assert!(!everything.contains("\"--dev-bind\""));
}

#[test]
fn without_bwrap_there_is_a_sentence_and_no_process() {
    let dir = tempfile_dir("no-bwrap");
    let bare = |root: &str| Recipe {
        path_var: String::new(),
        uid: Some(1000),
        ..recipe(Path::new(root), false, None)
    };
    let err = prepare(&bare(&dir.to_string_lossy()))
        .map(|_| ())
        .unwrap_err();
    assert!(
        err.contains("bubblewrap"),
        "the failure does not say what is missing: {err}"
    );
    // And a workspace that is refused is refused before bwrap is even looked for.
    let err = prepare(&bare("/")).map(|_| ()).unwrap_err();
    assert!(err.contains("filesystem root"), "{err}");
}

#[test]
fn a_machine_has_its_own_ids_and_events_and_state() {
    let machines = Arc::new(Mutex::new(Machines::default()));
    assert!(write(&machines, "term-1", "x")
        .unwrap_err()
        .contains("no such machine"));
    assert!(resize(&machines, "term-1", 80, 24)
        .unwrap_err()
        .contains("no such machine"));
    assert!(
        close(&machines, "term-1").is_ok(),
        "closing what is not there is a no-op"
    );
    assert_eq!(MACHINE_OUTPUT_EVENT, "machine-output");
    assert_eq!(MACHINE_EXIT_EVENT, "machine-exit");
    assert_ne!(
        MACHINE_OUTPUT_EVENT, "terminal-output",
        "a machine's output would be filed as a terminal's"
    );
    let wire = serde_json::to_value(MachineOutput {
        id: "mach-1".into(),
        data: "x".into(),
    })
    .unwrap();
    assert_eq!(wire, serde_json::json!({"id": "mach-1", "data": "x"}));
}

#[test]
fn the_window_listens_for_the_events_this_module_emits() {
    let ui = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("ui/src/shellEvents.ts");
    let text =
        std::fs::read_to_string(&ui).unwrap_or_else(|e| panic!("{} must exist: {e}", ui.display()));
    for event in [MACHINE_OUTPUT_EVENT, MACHINE_EXIT_EVENT] {
        assert!(
            text.contains(&format!("\"{event}\"")),
            "ui/src/shellEvents.ts does not name {event:?} — the shell emits it and the window would never hear it"
        );
    }
}

// ── the jail as a fact ────────────────────────────────────────────────────────────────────────────

/// A directory of this test's own, removed with it.
struct Scratch(PathBuf);
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
impl std::ops::Deref for Scratch {
    type Target = PathBuf;
    fn deref(&self) -> &PathBuf {
        &self.0
    }
}
fn tempfile_dir(tag: &str) -> Scratch {
    let dir = std::env::temp_dir().join(format!("codify-machine-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    Scratch(dir)
}

/// A real machine, with a transcript.
struct Jail {
    machines: Arc<Mutex<Machines>>,
    id: String,
    /// Whether this host gave the machine a project layer, and why not if it did not.
    opened: OpenedMachine,
    out: Arc<Mutex<String>>,
    exited: Arc<AtomicBool>,
    on_text: TextSink,
    on_exit: ExitSink,
    /// How much of the transcript has been read past.
    seen: Mutex<usize>,
}

impl Jail {
    /// A machine with the project layer, as the shell opens one.
    fn open(workspace: &Path, network: bool) -> Jail {
        Jail::start(recipe(workspace, network, Some(&std::env::temp_dir())))
    }

    /// A machine whose project is bound read-only, which is what a host that cannot make the layer gets.
    fn read_only(workspace: &Path) -> Jail {
        Jail::start(recipe(workspace, false, None))
    }

    fn start(recipe: Recipe) -> Jail {
        let machines = Arc::new(Mutex::new(Machines::default()));
        let out = Arc::new(Mutex::new(String::new()));
        let exited = Arc::new(AtomicBool::new(false));
        let (sink, flag) = (out.clone(), exited.clone());
        let on_text: TextSink = Arc::new(move |_, text| sink.lock().unwrap().push_str(&text));
        let on_exit: ExitSink = Arc::new(move |_| flag.store(true, Ordering::SeqCst));
        let opened = open_with_sink(
            &machines,
            &recipe,
            200,
            50,
            on_text.clone(),
            on_exit.clone(),
        )
        .expect(
            "these tests build a real jail and need bwrap and user namespaces — run `make doctor`",
        );
        let jail = Jail {
            machines,
            id: opened.id.clone(),
            opened,
            out,
            exited,
            on_text,
            on_exit,
            seen: Mutex::new(0),
        };
        // The first prompt: the machine is up and reading.
        jail.until("[machine]", 15);
        jail
    }

    /// The host pid of the machine's first process, whose tree is everything in it.
    fn root_pid(&self) -> u32 {
        let guard = self.machines.lock().unwrap();
        guard.entries[&self.id]
            .session
            .child
            .process_id()
            .expect("the machine has a pid")
    }

    fn transcript(&self) -> String {
        self.out.lock().unwrap().clone()
    }

    /// Wait for `needle` to appear after what has already been read, returning the new text.
    fn until(&self, needle: &str, secs: u64) -> String {
        let from = *self.seen.lock().unwrap();
        let deadline = Instant::now() + Duration::from_secs(secs);
        loop {
            let all = self.transcript();
            if let Some(at) = all[from..].find(needle) {
                let end = from + at + needle.len();
                *self.seen.lock().unwrap() = end;
                return all[from..end].to_string();
            }
            assert!(
                Instant::now() < deadline,
                "timed out waiting for {needle:?}; the machine said:\n{all}"
            );
            std::thread::sleep(Duration::from_millis(25));
        }
    }

    /// Run one line and return what it printed. The line must end by printing a token that does
    /// not appear in its own text (`DONE-$((6*7))` prints `DONE-42` and echoes `DONE-$((6*7))`).
    ///
    /// **The transcript contains the echoed command as well as its output.** An assertion that looks for
    /// a word the command itself spells (`echo OK`, `HIDDEN`) passes whatever the command did, which is
    /// how a check of the scratch home and of the host's `$HOME` went vacuous. Every token a test looks
    /// for is computed (`TOKEN-$((1+1))`), so only a command that ran can print it.
    fn run(&self, line: &str) -> String {
        write(&self.machines, &self.id, &format!("{line}\n")).unwrap();
        self.until("DONE-42", 20)
    }
}

impl Drop for Jail {
    fn drop(&mut self) {
        let _ = close(&self.machines, &self.id);
    }
}

const DONE: &str = "echo DONE-$((6*7))";

#[test]
fn a_read_only_project_is_read_only_and_the_hosts_files_are_untouched() {
    let ws = tempfile_dir("ro-workspace");
    std::fs::write(ws.join("a.txt"), "original\n").unwrap();
    let jail = Jail::read_only(&ws);
    assert!(
        !jail.opened.copy_on_write && jail.opened.note.is_some(),
        "a machine with no layer does not say why"
    );

    let said = jail.run(&format!("cat /work/a.txt; {DONE}"));
    assert!(
        said.contains("original"),
        "the machine cannot read the workspace: {said}"
    );

    let said = jail.run(&format!("touch /work/new 2>&1; echo RC=$?; {DONE}"));
    assert!(
        said.contains("Read-only file system") && said.contains("RC=1"),
        "a file was created in the workspace: {said}"
    );
    let said = jail.run(&format!(
        "echo more >> /work/a.txt 2>&1; echo RC=$?; {DONE}"
    ));
    assert!(
        said.contains("RC=1"),
        "the workspace file was appended to: {said}"
    );
    let said = jail.run(&format!("rm /work/a.txt 2>&1; echo RC=$?; {DONE}"));
    assert!(
        said.contains("RC=1"),
        "the workspace file was removed: {said}"
    );

    assert!(
        !ws.join("new").exists(),
        "a file made inside the jail exists on the host"
    );
    assert_eq!(
        std::fs::read_to_string(ws.join("a.txt")).unwrap(),
        "original\n",
        "the host's file changed"
    );
}

#[test]
fn the_prompt_in_a_real_jail_names_the_directory_the_person_is_in() {
    let ws = tempfile_dir("prompt");
    let jail = Jail::open(&ws, false);
    // The first prompt was read by `start`; the one after a command is the prompt a person sees between
    // commands, and bash has expanded `\w` and `\$` in it.
    jail.run(DONE);
    jail.until("[machine] /work $ ", 15);
    jail.run(&format!("cd /tmp; {DONE}"));
    jail.until("[machine] /tmp $ ", 15);
}

#[test]
fn the_scratch_is_writable_and_it_is_not_the_hosts_home() {
    let ws = tempfile_dir("scratch");
    let jail = Jail::open(&ws, false);

    let said = jail.run(&format!(
        "echo $((6*7)) > ~/note && echo NOTE-$(cat ~/note); echo HOME=$HOME; {DONE}"
    ));
    assert!(
        said.contains("NOTE-42") && said.contains("HOME=/home/machine"),
        "the scratch home is not usable: {said}"
    );
    let said = jail.run(&format!("echo x > /tmp/t && echo TMPOK-$((1+1)); {DONE}"));
    assert!(said.contains("TMPOK-2"), "/tmp is not writable: {said}");

    // The host's real home, and Codify's state, are not there to be found.
    let real_home = std::env::var("HOME").unwrap_or_else(|_| "/root".into());
    let said = jail.run(&format!(
        "[ -e '{real_home}' ] && echo HOMEVIS-$((1+1)) || echo HOMEHID-$((1+1)); \
         [ -e '{real_home}/.codify' ] && echo STATEVIS-$((1+1)) || echo STATEHID-$((1+1)); {DONE}"
    ));
    assert!(
        said.contains("HOMEHID-2")
            && said.contains("STATEHID-2")
            && !said.contains("HOMEVIS-2")
            && !said.contains("STATEVIS-2"),
        "the host's home is visible from the jail: {said}"
    );
    // The only home in the jail is the scratch one: not this user's, and not anyone else's.
    let said = jail.run(&format!("echo HOMES=$(ls /home | tr '\\n' ' ')END; {DONE}"));
    assert!(
        said.contains("HOMES=machine END"),
        "the homes in the jail are not exactly the scratch home: {said}"
    );
}

#[test]
fn the_environment_is_built_and_not_inherited() {
    // A variable this process has and the jail must not.
    std::env::set_var("CODIFY_MACHINE_TEST_SECRET", "s3cr3t-value");
    let ws = tempfile_dir("env");
    let jail = Jail::open(&ws, false);
    let said = jail.run(&format!("env | sort | tr '\\n' ' '; echo; {DONE}"));
    assert!(
        !said.contains("CODIFY_MACHINE_TEST_SECRET") && !said.contains("s3cr3t-value"),
        "the shell's environment reached the jail: {said}"
    );
    let allowed: Vec<&str> = JAIL_ENV
        .iter()
        .map(|(k, _)| *k)
        .chain(["_", "SHLVL", "PWD", "OLDPWD"])
        .collect();
    let env_line = said
        .lines()
        .find(|l| l.contains("HOME=/home/machine"))
        .unwrap_or_else(|| panic!("no environment printed: {said}"));
    for pair in env_line.split_whitespace().filter(|w| w.contains('=')) {
        let key = pair.split('=').next().unwrap();
        assert!(
            allowed.contains(&key),
            "{key} is in the jail's environment and is not in JAIL_ENV: {env_line}"
        );
    }
}

#[test]
fn with_no_network_there_is_only_loopback_and_the_hosts_loopback_is_not_reachable() {
    use std::net::TcpListener;
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let ws = tempfile_dir("net-off");
    let jail = Jail::open(&ws, false);

    let said = jail.run(&format!("cat /proc/net/dev; {DONE}"));
    // `/proc/net/dev` rows are `name:` followed by counters; nothing else in the transcript is.
    let interfaces: Vec<&str> = said
        .lines()
        .filter_map(|l| l.trim_start().split_once(':'))
        .filter(|(name, rest)| {
            !name.contains(' ') && rest.trim_start().starts_with(|c: char| c.is_ascii_digit())
        })
        .map(|(name, _)| name)
        .collect();
    assert_eq!(
        interfaces,
        ["lo"],
        "the jail sees network interfaces besides loopback: {said}"
    );

    let said = jail.run(&format!(
        "(exec 3<>/dev/tcp/127.0.0.1/{port}) 2>/dev/null && echo OPEN-$((1+1)) || echo SHUT-$((1+1)); {DONE}"
    ));
    // The tokens are computed (`OPEN-2`, `SHUT-2`), so the echo of the command cannot be taken for the answer.
    assert!(
        said.contains("SHUT-2") && !said.contains("OPEN-2"),
        "the jail reached a listener on the host's loopback: {said}"
    );
    drop(listener);
}

#[test]
fn with_network_on_the_hosts_loopback_is_reachable_which_is_the_limit_the_tab_states() {
    use std::net::TcpListener;
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let ws = tempfile_dir("net-on");
    let jail = Jail::open(&ws, true);
    let said = jail.run(&format!(
        "(exec 3<>/dev/tcp/127.0.0.1/{port}) 2>/dev/null && echo OPEN-$((1+1)) || echo SHUT-$((1+1)); {DONE}"
    ));
    // This is asserted, not hoped: the docs and the tab say a machine with network can reach the host's
    // own services, and a test that proved only the safe half would let that sentence drift.
    assert!(
        said.contains("OPEN-2"),
        "a machine opened with network could not reach the host's loopback: {said}"
    );
    drop(listener);
}

#[test]
fn the_jail_has_its_own_pid_namespace_no_capabilities_and_an_unprivileged_user() {
    let ws = tempfile_dir("pid-caps");
    let jail = Jail::open(&ws, false);
    let host_pid = std::process::id();

    let said = jail.run(&format!(
        "echo PID=$$; kill -0 {host_pid} 2>&1; echo KRC=$?; {DONE}"
    ));
    // The transcript echoes the command as well as printing its output, so the number is read after the
    // *last* marker, which is the output.
    let pid: u32 = said
        .rsplit("PID=")
        .next()
        .map(|rest| {
            rest.chars()
                .take_while(char::is_ascii_digit)
                .collect::<String>()
        })
        .and_then(|digits| digits.parse().ok())
        .unwrap_or_else(|| panic!("no pid printed: {said}"));
    assert!(
        pid < 50,
        "the shell has pid {pid}, which is a host pid and not a jail's"
    );
    assert!(
        said.contains("KRC=1"),
        "the host's process {host_pid} is visible from the jail: {said}"
    );
    // `/proc` must be the jail's own. A bind of the host's would show every process of this user, and a
    // process's `environ` and `cmdline` are readable by the same uid: that is the Tauri shell's, boot token
    // and all.
    let said = jail.run(&format!(
        "[ -e /proc/{host_pid} ] && echo SEE-$((1+1)) || echo BLIND-$((1+1)); {DONE}"
    ));
    assert!(
        said.contains("BLIND-2") && !said.contains("SEE-2"),
        "the host's /proc is visible from the jail: {said}"
    );
    let said = jail.run(&format!(
        "echo PROCS=$(ls /proc | grep -c '^[0-9]'); {DONE}"
    ));
    let procs: u32 = said
        .rsplit("PROCS=")
        .next()
        .map(|rest| {
            rest.chars()
                .take_while(char::is_ascii_digit)
                .collect::<String>()
        })
        .and_then(|d| d.parse().ok())
        .unwrap_or_else(|| panic!("no process count: {said}"));
    assert!(
        procs < 15,
        "the jail's /proc lists {procs} processes: {said}"
    );

    let said = jail.run(&format!("grep CapEff /proc/self/status; {DONE}"));
    assert!(
        said.contains("0000000000000000"),
        "the jail holds capabilities: {said}"
    );

    let host_uid = unsafe { libc::geteuid() };
    // Behind the project layer the jail is always told its uid; without it, a person keeps their own.
    let want = if host_uid == 0 || jail.opened.copy_on_write {
        UNPRIVILEGED_ID
    } else {
        host_uid
    };
    let said = jail.run(&format!("echo UID=$(id -u); {DONE}"));
    assert!(
        said.contains(&format!("UID={want}")),
        "the jail runs as the wrong user (wanted {want}): {said}"
    );
}

#[test]
fn the_pty_is_the_jails_terminal_so_job_control_and_ctrl_c_work() {
    let ws = tempfile_dir("tty");
    let jail = Jail::open(&ws, false);
    // `--new-session` would have detached the jail from this PTY; bash then says it has no job control.
    let said = jail.transcript();
    assert!(
        !said.contains("no job control") && !said.contains("Inappropriate ioctl"),
        "the shell has no controlling terminal: {said}"
    );
    let said = jail.run(&format!("[ -t 0 ] && echo TTY-$((1+1)); {DONE}"));
    assert!(said.contains("TTY-2"), "stdin is not a terminal: {said}");
    // Ctrl-C reaches a foreground process through the tty, and the shell is still there after.
    write(&jail.machines, &jail.id, "sleep 300\n").unwrap();
    std::thread::sleep(Duration::from_millis(400));
    write(&jail.machines, &jail.id, "\u{3}").unwrap();
    let said = jail.run(&format!("echo AFTER-$((6*7)); {DONE}"));
    assert!(
        said.contains("AFTER-42"),
        "Ctrl-C did not return the shell to its prompt: {said}"
    );
}

#[test]
fn closing_a_machine_ends_everything_that_was_running_in_it() {
    let ws = tempfile_dir("close");
    let marker = format!("{}", 4000 + std::process::id() % 1000);
    let jail = Jail::open(&ws, false);
    let _ = jail.run(&format!("sleep {marker} & echo STARTED; {DONE}"));
    let needle = format!("sleep\0{marker}\0");
    let running = || {
        std::fs::read_dir("/proc")
            .unwrap()
            .flatten()
            .filter_map(|e| std::fs::read(e.path().join("cmdline")).ok())
            .any(|c| String::from_utf8_lossy(&c).contains(&needle))
    };
    // `sleep N &` forks, and until the child has exec'd its /proc cmdline is still its parent's. `jail.run`
    // returns as soon as the marker is printed, which can be before that, so one look at /proc is a race
    // (it failed once on CI with "never started"). Look until it shows, for as long as a slow runner needs.
    let started_by = Instant::now() + Duration::from_secs(5);
    while !running() {
        assert!(
            Instant::now() < started_by,
            "the background process never started, so this proves nothing"
        );
        std::thread::sleep(Duration::from_millis(25));
    }

    let machines = jail.machines.clone();
    let id = jail.id.clone();
    close(&machines, &id).unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while running() {
        assert!(
            Instant::now() < deadline,
            "a process started in the machine outlived it"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[test]
fn a_machine_that_exits_says_so() {
    let ws = tempfile_dir("exit");
    let jail = Jail::open(&ws, false);
    write(&jail.machines, &jail.id, "exit\n").unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !jail.exited.load(Ordering::SeqCst) {
        assert!(
            Instant::now() < deadline,
            "the machine's end was never announced"
        );
        std::thread::sleep(Duration::from_millis(25));
    }
}

#[test]
fn only_so_many_machines_may_be_open_and_a_write_has_a_size() {
    let ws = tempfile_dir("cap");
    let layers = tempfile_dir("cap-layers");
    let machines = Arc::new(Mutex::new(Machines::default()));
    let open_one = |machines: &Arc<Mutex<Machines>>| {
        open_with_sink(
            machines,
            &recipe(&ws, false, Some(&layers)),
            80,
            24,
            Arc::new(|_, _| {}),
            Arc::new(|_| {}),
        )
    };
    let mut ids = Vec::new();
    for _ in 0..MAX_MACHINES {
        ids.push(
            open_one(&machines)
                .expect("a machine under the cap did not open")
                .id,
        );
    }
    let err = open_one(&machines).unwrap_err();
    assert!(err.contains("already open"), "the cap said: {err}");

    let big = "x".repeat(MAX_WRITE_BYTES + 1);
    assert!(write(&machines, &ids[0], &big)
        .unwrap_err()
        .contains("at most"));
    assert!(write(&machines, &ids[0], "echo fine\n").is_ok());

    close(&machines, &ids[0]).unwrap();
    let again = open_one(&machines).expect("closing one did not make room");
    ids.push(again.id);
    assert_eq!(
        std::fs::read_dir(&*layers).unwrap().count(),
        MAX_MACHINES,
        "one layer mount point per open machine, and closing one released its own"
    );
    close_all(&machines);
    assert_eq!(
        std::fs::read_dir(&*layers).unwrap().count(),
        0,
        "close_all left layer mount points behind"
    );
    assert!(
        write(&machines, &ids[1], "x")
            .unwrap_err()
            .contains("no such machine"),
        "close_all left a machine open"
    );
}

mod guard_facts;
mod layer_facts;

// ── the reader thread ─────────────────────────────────────────────────────────────────────────────

/// A reader that hands out `left` short lines, one per `read`, far enough apart (more than a batch window) that
/// each reaches the listener as a piece of its own.
struct Lines {
    left: usize,
}

impl std::io::Read for Lines {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        if self.left == 0 {
            return Ok(0);
        }
        self.left -= 1;
        std::thread::sleep(Duration::from_millis(30));
        let line = b"old output\n";
        buf[..line.len()].copy_from_slice(line);
        Ok(line.len())
    }
}

/// What `run_reader` delivered, and whether it reported an exit, for a machine whose epoch in the registry is
/// whatever `epoch_after(delivered_so_far)` says.
fn run_lines(
    lines: usize,
    started_for: u64,
    epoch_after: impl Fn(usize) -> Option<u64> + Send + 'static,
) -> (usize, bool) {
    use std::sync::atomic::AtomicUsize;
    let delivered = Arc::new(AtomicUsize::new(0));
    let exited = Arc::new(AtomicBool::new(false));
    let (seen, count, flag) = (delivered.clone(), delivered.clone(), exited.clone());
    run_reader(
        Lines { left: lines },
        started_for,
        move || epoch_after(seen.load(Ordering::SeqCst)),
        move |_| {
            count.fetch_add(1, Ordering::SeqCst);
        },
        move || flag.store(true, Ordering::SeqCst),
    );
    (
        delivered.load(Ordering::SeqCst),
        exited.load(Ordering::SeqCst),
    )
}

#[test]
fn a_reader_whose_machine_was_replaced_drops_what_it_still_holds_and_reports_no_exit() {
    // The registry holds this reader's epoch (1) until three pieces have been delivered, and then a
    // replacement's (2): a reset happened in the middle of the stream. What the old reader still held
    // would land after the reset's own line, on the clean screen of the new machine. Without the check all
    // ten arrive, and the end of the stream is reported as an exit the window would show as a dead machine.
    let (delivered, exited) = run_lines(10, 1, |so_far| Some(if so_far >= 3 { 2 } else { 1 }));
    assert_eq!(
        delivered, 3,
        "a replaced machine's reader kept delivering after the reset"
    );
    assert!(!exited, "a reset was reported as an exit");
}

#[test]
fn a_reader_whose_machine_is_still_current_delivers_everything_and_reports_the_exit() {
    let (delivered, exited) = run_lines(5, 1, |_| Some(1));
    assert_eq!(delivered, 5, "output of a live machine was dropped");
    assert!(exited, "a machine that ended was not reported as ended");
}

#[test]
fn a_reader_whose_machine_was_closed_delivers_its_last_output_and_reports_the_exit() {
    // A closed machine has no entry at all, which is not the same as a replaced one: it has always been
    // reported as an exit, and what it said last is still shown.
    let (delivered, exited) = run_lines(5, 1, |_| None);
    assert_eq!(delivered, 5, "a closed machine's output was dropped");
    assert!(exited, "a closed machine was not reported as ended");
}
