//! The project layer, asserted as **facts** about a real jail: a write inside the machine lands in the machine,
//! the person's files are exactly as they were, and dropping the layer leaves nothing behind. Plus the pure
//! parts (the script, the argv, the refusals) that a refactor would otherwise change quietly.
//!
//! Like the rest of the jail's tests these need `bwrap`, `unshare` and unprivileged user namespaces and
//! **fail loudly without them**: a guarantee that is skipped on the host that lacks the mechanism passes
//! exactly where it is false.

use super::*;

/// The names in `dir`, sorted: a stand-in for "the host's directory is exactly as it was".
fn names(dir: &Path) -> Vec<String> {
    let mut found: Vec<String> = std::fs::read_dir(dir)
        .unwrap()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .collect();
    found.sort();
    found
}

// ── the pure parts ────────────────────────────────────────────────────────────────────────────────

#[test]
fn the_mount_script_is_fixed_text_that_names_only_its_parameters() {
    let script = layer::MOUNT_SCRIPT;
    // Everything variable arrives as a positional parameter. The script never contains a path, a size or
    // any text from a request: the three it reads are `$1`..`$3`, and what it runs is `"$@"`.
    assert!(script.starts_with("stage=$1; size=$2; lower=$3; shift 3"));
    assert!(script.trim_end().ends_with("exec \"$@\""));
    for forbidden in [
        "/home", "/work", "/srv", "eval", "sudo", "chmod", "chown", "--bind",
    ] {
        assert!(
            !script.contains(forbidden),
            "the mount script mentions {forbidden}"
        );
    }
    // Both mounts are made inside the new namespaces, with `userxattr`, and a failed mount is a distinct exit.
    assert!(script.contains("mount -t tmpfs") && script.contains("mount -t overlay"));
    assert!(script.contains("userxattr"));
    assert!(script.contains("exit 96") && script.contains("exit 97"));
    assert_eq!(layer::MOUNT_FAILED, 97);
}

#[test]
fn the_launch_is_unshare_then_the_fixed_script_then_bwrap_and_the_jails_own_argv() {
    let jail = vec![
        "--unshare-all".to_string(),
        "--chdir".to_string(),
        "/work".to_string(),
    ];
    let args = layer::launch_args(
        Path::new("/tmp/codify-machine-1-0"),
        512,
        Path::new("/srv/project"),
        Path::new("/usr/bin/bwrap"),
        &jail,
    );
    assert_eq!(&args[..4], ["--user", "--map-root-user", "--mount", "--"]);
    assert_eq!(&args[4..8], ["/bin/sh", "-c", layer::MOUNT_SCRIPT, "sh"]);
    // The script's parameters, in the order it reads them, and then the program it execs.
    assert_eq!(
        &args[8..12],
        [
            "/tmp/codify-machine-1-0",
            "512",
            "/srv/project",
            "/usr/bin/bwrap"
        ]
    );
    assert_eq!(&args[12..], jail.as_slice());
}

#[test]
fn a_project_whose_path_an_overlay_cannot_hold_is_refused_before_anything_is_mounted() {
    for bad in ["/srv/a,b", "/srv/a:b", "/srv/a\\b", "/srv/a\nb"] {
        let why = layer::overlay_safe(Path::new(bad)).unwrap_err();
        assert!(why.contains("path"), "{bad:?} said: {why}");
    }
    assert!(layer::overlay_safe(Path::new("/srv/my project (v2)")).is_ok());
    assert!(layer::overlay_safe(Path::new("/home/ünï/code")).is_ok());
}

#[test]
fn a_stage_is_an_empty_owner_only_directory_that_is_removed_but_never_emptied() {
    use std::os::unix::fs::PermissionsExt;
    let base = tempfile_dir("stage");
    let stage = layer::Stage::create(&base).unwrap();
    let dir = stage.dir().to_path_buf();
    assert!(dir.starts_with(&*base) && dir.is_dir());
    assert_eq!(
        std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(
        std::fs::read_dir(&dir).unwrap().count(),
        0,
        "a stage is born empty"
    );
    assert_eq!(stage.merged(), dir.join("merged"));
    stage.release();
    assert!(!dir.exists(), "release left the directory");

    // A directory with something in it is left alone: release is `remove_dir`, never `remove_dir_all`.
    let stage = layer::Stage::create(&base).unwrap();
    let dir = stage.dir().to_path_buf();
    std::fs::write(dir.join("keep"), "x").unwrap();
    stage.release();
    assert!(
        dir.join("keep").exists(),
        "release deleted what was in the directory"
    );
    // Two stages never share a name.
    let (a, b) = (
        layer::Stage::create(&base).unwrap(),
        layer::Stage::create(&base).unwrap(),
    );
    assert_ne!(a.dir(), b.dir());
    a.release();
    b.release();
}

// ── stage-leak: a stage is made only after its path has been checked ─────────────────────────────────

#[test]
fn a_stage_that_cannot_be_made_safe_leaves_nothing_behind() {
    use std::os::unix::ffi::OsStrExt;
    let parent = tempfile_dir("stage-unsafe");
    // One base per refusal `overlay_safe` has: each of `,` `:` `\`, a control character, and a name that
    // is not UTF-8. A refused stage must not leave its directory in the base the host was handed.
    let mut bases: Vec<PathBuf> = ["comma,base", "colon:base", "back\\slash", "new\nline"]
        .iter()
        .map(|n| parent.join(n))
        .collect();
    bases.push(parent.join(std::ffi::OsStr::from_bytes(b"not-utf8-\xff")));
    for base in &bases {
        std::fs::create_dir_all(base).unwrap();
        let err = layer::Stage::create(base).unwrap_err();
        assert!(err.contains("path"), "{base:?}: {err}");
        assert!(
            std::fs::read_dir(base).unwrap().next().is_none(),
            "a refused stage left something in {base:?}"
        );
        // The probe makes a stage first, so it must leave nothing either; and the person-facing door,
        // `make_layer`, reports the refusal and also leaves nothing.
        let err = layer::probe_uncached(base, Path::new("/nonexistent/unshare"), "/usr/bin:/bin")
            .unwrap_err();
        assert!(err.contains("path"), "{base:?}: {err}");
        assert!(
            std::fs::read_dir(base).unwrap().next().is_none(),
            "a probe in an unsafe base left something in {base:?}"
        );
        let workspace = tempfile_dir("stage-unsafe-ws");
        let err = make_layer(base, &workspace, "/usr/bin:/bin")
            .map(|_| ())
            .unwrap_err();
        assert!(err.contains("path"), "{base:?}: {err}");
        assert!(
            std::fs::read_dir(base).unwrap().next().is_none(),
            "make_layer in an unsafe base left something in {base:?}"
        );
    }
}

#[test]
fn asking_whether_the_layer_works_fails_with_a_sentence_where_it_does_not() {
    let base = tempfile_dir("probe-bad");
    let err = layer::probe_uncached(&base, Path::new("/nonexistent/unshare"), "/usr/bin:/bin")
        .unwrap_err();
    assert!(err.contains("could not run unshare"), "{err}");
    assert!(
        names(&base).is_empty(),
        "a failed probe left something in {}: {:?}",
        base.display(),
        names(&base)
    );
    // And a project whose path cannot be put in an overlay never reaches the probe.
    let comma = tempfile_dir("comma,dir");
    let err = make_layer(&base, &comma, "/usr/bin:/bin")
        .map(|_| ())
        .unwrap_err();
    assert!(err.contains("path"), "{err}");
}

// ── the layer as a fact ───────────────────────────────────────────────────────────────────────────

#[test]
fn this_host_can_make_the_layer_and_leaves_nothing_behind_when_it_asks() {
    let base = tempfile_dir("probe-ok");
    let path = std::env::var("PATH").unwrap_or_default();
    let unshare = layer::find_on_path("unshare", &path)
        .expect("these tests need unshare (util-linux) — run `make doctor`");
    layer::probe_uncached(&base, &unshare, &path)
        .unwrap_or_else(|why| panic!("this host cannot make the project layer: {why}"));
    assert!(
        names(&base).is_empty(),
        "the probe left {:?} behind",
        names(&base)
    );
}

#[test]
fn writes_made_in_the_machine_stay_in_the_machine_and_the_hosts_files_are_untouched() {
    let ws = tempfile_dir("cow-workspace");
    std::fs::write(ws.join("a.txt"), "original\n").unwrap();
    std::fs::write(ws.join("gone.txt"), "delete me\n").unwrap();
    std::fs::create_dir(ws.join("dir")).unwrap();
    std::fs::write(ws.join("dir/inner.txt"), "inner\n").unwrap();
    let jail = Jail::open(&ws, false);
    assert!(
        jail.opened.copy_on_write,
        "this host could not make the layer: {:?}",
        jail.opened.note
    );

    // The machine can change its own project in every way a person would: edit, append, create, delete,
    // make and remove directories, replace a directory, rename.
    let said = jail.run(&format!(
        "echo changed > /work/a.txt; echo more >> /work/a.txt; echo fresh > /work/new.txt; \
         rm /work/gone.txt; mkdir /work/made && echo made > /work/made/f; \
         rm -r /work/dir && mkdir /work/dir && echo again > /work/dir/other.txt; \
         mv /work/new.txt /work/moved.txt; \
         echo A=$(cat /work/a.txt | tr '\\n' ,) G=$([ -e /work/gone.txt ] && echo yes || echo no) \
         M=$(cat /work/moved.txt) D=$(ls /work/dir | tr '\\n' ,) N=$((6*7)); {DONE}"
    ));
    assert!(
        said.contains("A=changed,more,")
            && said.contains("G=no")
            && said.contains("M=fresh")
            && said.contains("D=other.txt,")
            && said.contains("N=42"),
        "the machine cannot work in its own project: {said}"
    );

    // And the host's directory is exactly what it was: not one byte, not one name.
    assert_eq!(
        std::fs::read_to_string(ws.join("a.txt")).unwrap(),
        "original\n"
    );
    assert_eq!(
        std::fs::read_to_string(ws.join("gone.txt")).unwrap(),
        "delete me\n"
    );
    assert_eq!(
        std::fs::read_to_string(ws.join("dir/inner.txt")).unwrap(),
        "inner\n"
    );
    assert_eq!(names(&ws), ["a.txt", "dir", "gone.txt"]);
    assert_eq!(names(&ws.join("dir")), ["inner.txt"]);
}

#[test]
fn dropping_the_layer_leaves_the_base_untouched_and_a_new_machine_starts_clean() {
    let layers = tempfile_dir("cow-layers");
    let ws = tempfile_dir("cow-drop");
    std::fs::write(ws.join("a.txt"), "original\n").unwrap();

    let first = Jail::start(recipe(&ws, false, Some(&layers)));
    assert!(first.opened.copy_on_write, "{:?}", first.opened.note);
    assert_eq!(
        names(&layers).len(),
        1,
        "an open machine has exactly one layer mount point"
    );
    first.run(&format!(
        "echo scribble > /work/a.txt; echo junk > /work/junk.txt; {DONE}"
    ));
    let machines = first.machines.clone();
    let id = first.id.clone();
    close(&machines, &id).unwrap();
    assert!(
        names(&layers).is_empty(),
        "closing the machine left its layer's mount point: {:?}",
        names(&layers)
    );
    drop(first);

    // The layer is gone and the base was never touched; a fresh machine sees the project as it is.
    assert_eq!(
        std::fs::read_to_string(ws.join("a.txt")).unwrap(),
        "original\n"
    );
    assert_eq!(names(&ws), ["a.txt"]);
    let second = Jail::start(recipe(&ws, false, Some(&layers)));
    let said = second.run(&format!(
        "echo A=$(cat /work/a.txt) J=$([ -e /work/junk.txt ] && echo yes || echo no); {DONE}"
    ));
    assert!(
        said.contains("A=original") && said.contains("J=no"),
        "the new machine is not clean: {said}"
    );
}

#[test]
fn the_layer_has_a_size_and_filling_it_is_a_full_disk_in_the_machine_and_nothing_more() {
    let ws = tempfile_dir("cow-size");
    std::fs::write(ws.join("a.txt"), "original\n").unwrap();
    let mut r = recipe(&ws, false, Some(&std::env::temp_dir()));
    r.limits.layer_bytes = 8 * 1024 * 1024;
    let jail = Jail::start(r);

    let said = jail.run(&format!(
        "echo ERR=$(dd if=/dev/zero of=/work/big bs=1M count=64 2>&1 | grep -c 'No space left on device'); \
         echo FULL=$(df -k /work | tail -1 | tr -s ' ' | cut -d' ' -f2); {DONE}"
    ));
    assert!(
        said.contains("ERR=1"),
        "a write past the layer's size was not refused: {said}"
    );
    assert!(
        said.contains("FULL=8192"),
        "the layer is not the size it was given: {said}"
    );
    // The machine can still work, and the host is as it was.
    let said = jail.run(&format!("rm /work/big; echo OK-$((6*7)); {DONE}"));
    assert!(said.contains("OK-42"));
    assert_eq!(names(&ws), ["a.txt"]);
}

#[test]
fn a_project_the_layer_cannot_hold_is_shown_read_only_and_the_machine_says_why() {
    let parent = tempfile_dir("cow-comma-parent");
    let ws = parent.join("a,b");
    std::fs::create_dir(&ws).unwrap();
    std::fs::write(ws.join("a.txt"), "original\n").unwrap();
    let jail = Jail::open(&ws, false);

    assert!(!jail.opened.copy_on_write);
    let why = jail.opened.note.clone().expect("no reason was given");
    assert!(
        why.contains("path"),
        "the reason does not name the path: {why}"
    );
    // It is the read-only project it always was, and no weaker a jail for it.
    let said = jail.run(&format!("touch /work/new 2>&1; echo RC=$?; {DONE}"));
    assert!(
        said.contains("Read-only file system") && said.contains("RC=1"),
        "{said}"
    );
    assert!(!ws.join("new").exists());
}

#[test]
fn a_layer_that_cannot_be_made_leaves_a_read_only_machine_that_says_why() {
    let ws = tempfile_dir("cow-nobase");
    std::fs::write(ws.join("a.txt"), "original\n").unwrap();
    // The place the layer's mount point would be made does not exist: the host cannot make the layer.
    let nowhere = ws.join("no/such/base");
    let jail = Jail::start(recipe(&ws, false, Some(&nowhere)));

    assert!(
        !jail.opened.copy_on_write,
        "a machine claims a layer it does not have"
    );
    let why = jail.opened.note.clone().expect("no reason was given");
    assert!(
        why.contains("could not"),
        "the reason does not say what failed: {why}"
    );
    let said = jail.run(&format!("touch /work/new 2>&1; echo RC=$?; {DONE}"));
    assert!(
        said.contains("Read-only file system") && said.contains("RC=1"),
        "{said}"
    );
    assert_eq!(names(&ws), ["a.txt"]);
}

#[test]
fn a_layered_machine_is_the_same_jail_in_every_other_way() {
    let ws = tempfile_dir("cow-jail");
    let jail = Jail::open(&ws, false);
    assert!(jail.opened.copy_on_write, "{:?}", jail.opened.note);

    // Capabilities, user, pid namespace and network are the jail's, not the launcher's: the launcher's
    // namespace makes the person root, and none of that is visible from inside.
    let said = jail.run(&format!(
        "grep CapEff /proc/self/status; echo UID=$(id -u); echo PID=$$; \
         cat /proc/net/dev | grep -c ':' ; [ -e /home/machine ] && echo SCRATCH-$((1+1)); {DONE}"
    ));
    assert!(
        said.contains("0000000000000000"),
        "the layered jail holds capabilities: {said}"
    );
    assert!(said.contains(&format!("UID={UNPRIVILEGED_ID}")), "{said}");
    assert!(said.contains("SCRATCH-2"), "{said}");
    let host_pid = std::process::id();
    let said = jail.run(&format!(
        "[ -e /proc/{host_pid} ] && echo SEE-$((1+1)) || echo BLIND-$((1+1)); {DONE}"
    ));
    assert!(
        said.contains("BLIND-2") && !said.contains("SEE-2"),
        "{said}"
    );
    // The environment is still the built one: nothing of the launcher's, nothing of the shell's.
    let said = jail.run(&format!(
        "env | sort | cut -d= -f1 | tr '\\n' ' '; echo; {DONE}"
    ));
    for leaked in [
        "CODIFY",
        "TOKEN",
        "DISPLAY",
        "WAYLAND",
        "DBUS",
        "SSH_AUTH_SOCK",
    ] {
        assert!(
            !said.contains(leaked),
            "the layered jail's environment holds {leaked}: {said}"
        );
    }
}
