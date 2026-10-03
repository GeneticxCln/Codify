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
    assert!(userns_refusal(false, &none)
        .unwrap()
        .contains("max_user_namespaces"));
    assert!(userns_refusal(true, &none).is_some());
    // Debian's switch binds an unprivileged user and not root.
    let debian = table(&[("/proc/sys/kernel/unprivileged_userns_clone", "0\n")]);
    assert!(userns_refusal(false, &debian)
        .unwrap()
        .contains("unprivileged_userns_clone"));
    assert!(userns_refusal(true, &debian).is_none());
    // Ubuntu's AppArmor restriction is not a refusal: it cannot say whether `bwrap` is among the blocked.
    let ubuntu = table(&[(
        "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
        "1\n",
    )]);
    assert!(userns_refusal(false, &ubuntu).is_none());
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
    // One builder, and it is bwrap's. The jail's shell is named in the jail's own argv. (The needle is
    // assembled at run time: `tests/test_no_unguarded_spawns.py` reads this file too, and a literal
    // would be a spawn site of its own.)
    let builder = format!("{}::new(", "CommandBuilder");
    assert_eq!(
        src.matches(&builder).count(),
        1,
        "machine.rs builds a second command; every one of them has to be inside the jail"
    );
    assert!(src.contains(&format!("{builder}bwrap)")));
    for banned in ["shell_command", "terminal::open", "\"SHELL\"", "$SHELL"] {
        assert!(
            !src.contains(banned),
            "machine.rs mentions {banned}: a machine must have no way to start the person's own shell"
        );
    }
    // The shell's own environment does not reach bwrap either.
    assert!(src.contains("cmd.env_clear()"));
    // No writable bind, anywhere in the source (the argv test above asserts it on the result).
    assert!(!src.contains("\"--bind\"") && !src.contains("\"--dev-bind\""));
}

#[test]
fn without_bwrap_there_is_a_sentence_and_no_process() {
    let dir = tempfile_dir("no-bwrap");
    let err = prepare(Some(&dir.to_string_lossy()), false, "", Some(1000))
        .map(|_| ())
        .unwrap_err();
    assert!(
        err.contains("bubblewrap"),
        "the failure does not say what is missing: {err}"
    );
    // And a workspace that is refused is refused before bwrap is even looked for.
    let err = prepare(Some("/"), false, "", Some(1000))
        .map(|_| ())
        .unwrap_err();
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
    out: Arc<Mutex<String>>,
    exited: Arc<AtomicBool>,
    /// How much of the transcript has been read past.
    seen: Mutex<usize>,
}

impl Jail {
    fn open(workspace: &Path, network: bool) -> Jail {
        let machines = Arc::new(Mutex::new(Machines::default()));
        let cmd = prepare(
            Some(&workspace.to_string_lossy()),
            network,
            &std::env::var("PATH").unwrap_or_default(),
            None,
        )
        .expect(
            "these tests build a real jail and need bwrap and user namespaces — run `make doctor`",
        );
        let out = Arc::new(Mutex::new(String::new()));
        let exited = Arc::new(AtomicBool::new(false));
        let (sink, flag) = (out.clone(), exited.clone());
        let id = open_with_sink(
            &machines,
            cmd,
            200,
            50,
            move |_, text| sink.lock().unwrap().push_str(&text),
            move |_| flag.store(true, Ordering::SeqCst),
        )
        .expect("the jail did not start");
        let jail = Jail {
            machines,
            id,
            out,
            exited,
            seen: Mutex::new(0),
        };
        // The first prompt: the machine is up and reading.
        jail.until("[machine]", 15);
        jail
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
fn the_workspace_is_read_only_and_the_hosts_files_are_untouched() {
    let ws = tempfile_dir("ro-workspace");
    std::fs::write(ws.join("a.txt"), "original\n").unwrap();
    let jail = Jail::open(&ws, false);

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
    let want = if host_uid == 0 {
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
    assert!(
        running(),
        "the background process never started, so this proves nothing"
    );

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
    let machines = Arc::new(Mutex::new(Machines::default()));
    let mut ids = Vec::new();
    for _ in 0..MAX_MACHINES {
        let cmd = prepare(
            Some(&ws.to_string_lossy()),
            false,
            &std::env::var("PATH").unwrap_or_default(),
            None,
        )
        .expect(
            "these tests build a real jail and need bwrap and user namespaces — run `make doctor`",
        );
        ids.push(
            open_with_sink(&machines, cmd, 80, 24, |_, _| {}, |_| {})
                .expect("a machine under the cap did not open"),
        );
    }
    let cmd = prepare(
        Some(&ws.to_string_lossy()),
        false,
        &std::env::var("PATH").unwrap_or_default(),
        None,
    )
    .unwrap();
    let err = open_with_sink(&machines, cmd, 80, 24, |_, _| {}, |_| {}).unwrap_err();
    assert!(err.contains("already open"), "the cap said: {err}");

    let big = "x".repeat(MAX_WRITE_BYTES + 1);
    assert!(write(&machines, &ids[0], &big)
        .unwrap_err()
        .contains("at most"));
    assert!(write(&machines, &ids[0], "echo fine\n").is_ok());

    close(&machines, &ids[0]).unwrap();
    let cmd = prepare(
        Some(&ws.to_string_lossy()),
        false,
        &std::env::var("PATH").unwrap_or_default(),
        None,
    )
    .unwrap();
    let again = open_with_sink(&machines, cmd, 80, 24, |_, _| {}, |_| {})
        .expect("closing one did not make room");
    ids.push(again);
    close_all(&machines);
    assert!(
        write(&machines, &ids[1], "x")
            .unwrap_err()
            .contains("no such machine"),
        "close_all left a machine open"
    );
}
