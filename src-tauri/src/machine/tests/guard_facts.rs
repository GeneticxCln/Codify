//! The guard, asserted as **facts**: a loop that never ends is ended, a program that takes too much memory
//! is stopped and the pane says why, a reset brings the machine back clean, and a machine that is busy but
//! modest is left alone. And the pure parts: how a process tree is read and how a byte count is said.
//!
//! These run a real jail, so like the rest they need `bwrap` and user namespaces and fail loudly without them.
//! The limits are made small *in the recipe* so a test does in seconds what a person's limits take minutes to
//! reach; the code that enforces them is the code that runs in production.

use super::*;

/// A machine with its limits turned down.
fn limited(ws: &Path, tweak: impl FnOnce(&mut Limits)) -> Jail {
    let mut r = recipe(ws, false, Some(&std::env::temp_dir()));
    tweak(&mut r.limits);
    Jail::start(r)
}

/// Wait until `done()` is true, or fail with what the machine said.
fn wait_for(jail: &Jail, what: &str, secs: u64, done: impl Fn() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(secs);
    while !done() {
        assert!(
            Instant::now() < deadline,
            "{what}; the machine said:\n{}",
            jail.transcript()
        );
        std::thread::sleep(Duration::from_millis(50));
    }
}

// ── the pure parts ────────────────────────────────────────────────────────────────────────────────

#[test]
fn a_parent_is_read_from_after_the_last_parenthesis_because_a_name_may_hold_any() {
    assert_eq!(guard::parse_ppid("123 (bash) S 45 123 123 0 -1"), Some(45));
    // A command called `a) S 9 (b` is the case that breaks a reader that splits on the first `)`.
    assert_eq!(
        guard::parse_ppid("77 (a) S 9 (b) R 31 77 77 0 -1"),
        Some(31)
    );
    assert_eq!(guard::parse_ppid("77 (sh with spaces) S 12 1 1"), Some(12));
    assert_eq!(guard::parse_ppid("garbage"), None);
    assert_eq!(guard::parse_ppid("1 (init)"), None);
    assert_eq!(guard::parse_state("77 (a) S 9 (b) Z 31 77"), Some('Z'));
    assert_eq!(guard::parse_state("5 (x y) R 1 5"), Some('R'));
    assert_eq!(guard::parse_state("nothing"), None);
}

#[test]
fn memory_is_read_as_proportional_size_and_total_memory_from_the_kernels_own_text() {
    let rollup = "55d0c0a00000-7ffd4b1ff000 ---p 00000000 00:00 0   [rollup]\n\
                  Rss:              204800 kB\nPss:              157921 kB\nPss_Anon:     100 kB\n";
    assert_eq!(guard::parse_pss(rollup), Some(157_921 * 1024));
    assert_eq!(guard::parse_pss("Rss: 5 kB\n"), None);
    assert_eq!(
        guard::parse_meminfo_total(
            "MemFree: 1 kB\nMemTotal:       16318760 kB\nMemAvailable: 9 kB\n"
        ),
        Some(16_318_760 * 1024)
    );
    assert_eq!(guard::parse_meminfo_total("MemFree: 1 kB\n"), None);
}

#[test]
fn a_machine_may_use_half_the_computer_and_never_more_than_four_gigabytes() {
    const GIB: u64 = 1024 * 1024 * 1024;
    assert_eq!(guard::default_memory_bytes(Some(4 * GIB)), 2 * GIB);
    assert_eq!(guard::default_memory_bytes(Some(64 * GIB)), 4 * GIB);
    assert_eq!(guard::default_memory_bytes(None), 2 * GIB);
    let d = Limits::default();
    assert_eq!(d.cpu_secs, 1800);
    assert_eq!(d.file_bytes, GIB);
    assert_eq!(d.layer_bytes, GIB);
    assert!(
        d.interval <= Duration::from_secs(1),
        "the guard looks too seldom to catch anything"
    );
}

#[test]
fn what_the_pane_is_told_names_the_limit_and_never_contradicts_itself() {
    assert_eq!(guard::human(300 * 1024 * 1024), "300 MB");
    assert_eq!(guard::human(3_968 * 1024 * 1024), "3.9 GB");
    let said = guard::notice(2 * 1024 * 1024 * 1024);
    assert!(
        said.contains("machine stopped: it used more than the 2.0 GB of memory a machine may use"),
        "the notice does not name the limit it was stopped at: {said}"
    );
    assert!(
        said.contains("Reset"),
        "the notice does not say what to do: {said}"
    );
    assert!(
        said.starts_with("\r\n") && said.ends_with("\r\n"),
        "the notice would run into a prompt"
    );
}

#[test]
fn a_process_tree_is_found_from_its_root_and_a_dead_root_has_none() {
    let me = std::process::id();
    let tree = guard::tree(me);
    assert_eq!(tree.first(), Some(&me), "the root is not first");
    assert!(
        guard::tree(u32::MAX - 1).is_empty(),
        "a pid that does not exist has a tree"
    );
    // This process's own memory is real and is counted, and a tree is the *sum* of its processes: listing
    // the same process twice is twice the memory, which is how a total differs from a largest.
    let one = guard::tree_bytes(&[me]);
    assert!(one > 1024 * 1024);
    assert!(
        guard::tree_bytes(&[me, me]) > one * 3 / 2,
        "a tree's memory is not the sum of its processes'"
    );
    assert!(guard::tree_bytes(&tree) >= one);
    let (verdict, pids) = guard::look(me, u64::MAX);
    assert_eq!(verdict, guard::Verdict::Fine);
    assert!(!pids.is_empty());
    let (verdict, _) = guard::look(me, 1024);
    assert!(matches!(verdict, guard::Verdict::Over(used) if used > 1024));
}

// ── the guard as a fact ───────────────────────────────────────────────────────────────────────────

#[test]
fn a_loop_that_never_ends_is_ended_by_the_kernel_and_the_machine_carries_on() {
    let ws = tempfile_dir("guard-cpu");
    let jail = limited(&ws, |l| l.cpu_secs = 2);

    let started = Instant::now();
    // The loop is a child: the limit is per process, and the shell that started it is not the one that spins.
    let said = jail.run(&format!("sh -c 'while :; do :; done'; echo RC=$?; {DONE}"));
    assert!(
        started.elapsed() < Duration::from_secs(25),
        "the loop ran for {:?}",
        started.elapsed()
    );
    let rc: u32 = said
        .rsplit("RC=")
        .next()
        .map(|r| {
            r.chars()
                .take_while(char::is_ascii_digit)
                .collect::<String>()
        })
        .and_then(|d| d.parse().ok())
        .unwrap_or_else(|| panic!("no exit status: {said}"));
    // A process ended by a signal reports 128 plus its number: SIGXCPU is 24, and SIGKILL (9) is what
    // the kernel escalates to if it is ignored.
    assert!(
        rc == 128 + 24 || rc == 128 + 9,
        "the loop ended with status {rc}, not by the CPU limit: {said}"
    );
    // The machine is where it was: the same shell, still answering.
    let said = jail.run(&format!("echo ALIVE-$((6*7)); {DONE}"));
    assert!(said.contains("ALIVE-42"));
    assert!(
        !jail.exited.load(Ordering::SeqCst),
        "the shell was ended along with the loop"
    );
}

#[test]
fn a_file_larger_than_the_limit_cannot_be_written() {
    let ws = tempfile_dir("guard-file");
    let jail = limited(&ws, |l| l.file_bytes = 4 * 1024 * 1024);

    let said = jail.run(&format!(
        "sh -c 'dd if=/dev/zero of=/tmp/f bs=1M count=64 2>/dev/null'; echo RC=$?; \
         echo SIZE=$(stat -c %s /tmp/f); {DONE}"
    ));
    let size: u64 = said
        .rsplit("SIZE=")
        .next()
        .map(|r| {
            r.chars()
                .take_while(char::is_ascii_digit)
                .collect::<String>()
        })
        .and_then(|d| d.parse().ok())
        .unwrap_or_else(|| panic!("no size: {said}"));
    assert!(
        size <= 4 * 1024 * 1024,
        "a file of {size} bytes was written past a 4 MiB limit: {said}"
    );
    assert!(!said.contains("RC=0"), "dd was not stopped: {said}");
}

#[test]
fn a_program_that_takes_too_much_memory_stops_the_machine_and_the_pane_says_why() {
    let ws = tempfile_dir("guard-mem");
    let jail = limited(&ws, |l| {
        l.memory_bytes = 200 * 1024 * 1024;
        l.interval = Duration::from_millis(200);
    });

    // 600 MB, written (not merely reserved) so it is resident, then held for a minute.
    write(
        &jail.machines,
        &jail.id,
        "python3 -c \"b = b'x' * (600 * 1024 * 1024); import time; time.sleep(60)\"\n",
    )
    .unwrap();
    wait_for(
        &jail,
        "the machine was never stopped for its memory",
        30,
        || jail.exited.load(Ordering::SeqCst),
    );

    let said = jail.transcript();
    assert!(
        said.contains("[machine stopped: it used more than the 200 MB of memory a machine may use"),
        "the pane was not told why: {said}"
    );
    // Nothing of it is left running on the host.
    wait_for(
        &jail,
        "a process of the stopped machine is still running",
        10,
        || guard::tree(jail.root_pid()).is_empty(),
    );
}

#[test]
fn a_stopped_machine_is_reset_to_a_clean_working_one() {
    let ws = tempfile_dir("guard-reset");
    std::fs::write(ws.join("a.txt"), "original\n").unwrap();
    let jail = limited(&ws, |l| {
        l.memory_bytes = 200 * 1024 * 1024;
        l.interval = Duration::from_millis(200);
    });
    jail.run(&format!(
        "echo scribble > /work/a.txt; echo junk > /work/junk.txt; {DONE}"
    ));
    write(
        &jail.machines,
        &jail.id,
        "python3 -c \"b = b'x' * (600 * 1024 * 1024); import time; time.sleep(60)\"\n",
    )
    .unwrap();
    wait_for(&jail, "the machine was never stopped", 30, || {
        jail.exited.load(Ordering::SeqCst)
    });
    // The stopped machine is not writable-to-nothing: typing into it has no shell to reach, and the
    // window is told the machine ended, which is what lets the person and the assistant reach for Reset.
    jail.exited.store(false, Ordering::SeqCst);

    let reopened = reset_with_sink(
        &jail.machines,
        &jail.id,
        200,
        50,
        jail.on_text.clone(),
        jail.on_exit.clone(),
    )
    .expect("the stopped machine could not be reset");
    assert_eq!(
        reopened.id, jail.id,
        "a reset must keep the machine's identity"
    );
    assert_eq!(reopened.copy_on_write, jail.opened.copy_on_write);
    jail.until("machine reset", 15);
    jail.until("[machine]", 15);

    let said = jail.run(&format!(
        "echo A=$(cat /work/a.txt) J=$([ -e /work/junk.txt ] && echo yes || echo no) U=$(id -u); {DONE}"
    ));
    assert!(
        said.contains("A=original") && said.contains("J=no"),
        "the reset machine kept what the old one did: {said}"
    );
    assert!(
        !jail.exited.load(Ordering::SeqCst),
        "a reset announced an exit; the window would show a live machine as ended"
    );
    // And the host's file never changed at any point.
    assert_eq!(
        std::fs::read_to_string(ws.join("a.txt")).unwrap(),
        "original\n"
    );
    assert!(!ws.join("junk.txt").exists());
}

#[test]
fn a_reset_ends_everything_the_old_machine_was_running_and_changes_nothing_a_person_chose() {
    let ws = tempfile_dir("reset-chosen");
    let layers = tempfile_dir("reset-layers");
    let marker = format!("{}", 5000 + std::process::id() % 1000);
    let jail = Jail::start(recipe(&ws, false, Some(&layers)));
    assert_eq!(std::fs::read_dir(&*layers).unwrap().count(), 1);
    jail.run(&format!("sleep {marker} & echo STARTED; {DONE}"));
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
    let old_root = jail.root_pid();

    reset_with_sink(
        &jail.machines,
        &jail.id,
        120,
        40,
        jail.on_text.clone(),
        jail.on_exit.clone(),
    )
    .unwrap();

    wait_for(
        &jail,
        "a process started before the reset outlived it",
        10,
        || !running(),
    );
    assert_ne!(jail.root_pid(), old_root, "the reset kept the old process");
    // The old machine's layer went with it and the new one has its own: one mount point, not two.
    assert_eq!(
        std::fs::read_dir(&*layers).unwrap().count(),
        1,
        "a reset left the old layer's mount point behind (or made none)"
    );
    // The screen was cleared (a full terminal reset) before the line that says what happened.
    assert!(
        jail.transcript().contains("\u{1b}c[machine reset"),
        "the pane was not cleared and told: {:?}",
        jail.transcript()
    );
    jail.until("[machine]", 15);
    // The recipe the machine was made from is the recipe it is remade from: still no network, still the
    // same project, and the pane's size is the one the window gave.
    let said = jail.run(&format!(
        "echo NET=$(grep -c ':' /proc/net/dev) W=$(ls /work | wc -l) COLS=$(tput cols 2>/dev/null || stty size | cut -d' ' -f2); {DONE}"
    ));
    assert!(
        said.contains("NET=1"),
        "the reset machine has a network the person did not choose: {said}"
    );
    assert!(
        said.contains("COLS=120"),
        "the reset machine ignored the size it was given: {said}"
    );
    // The old machine's output ended when it was killed, and that is not the machine exiting: the new one is
    // running. (By now the old reader has long since seen its end and been told it is not the current one.)
    assert!(
        !jail.exited.load(Ordering::SeqCst),
        "a reset of a live machine announced an exit; the window would show a running machine as ended"
    );
}

#[test]
fn a_machine_that_is_busy_but_modest_is_left_alone() {
    let ws = tempfile_dir("guard-modest");
    let jail = limited(&ws, |l| {
        l.memory_bytes = 300 * 1024 * 1024;
        l.interval = Duration::from_millis(100);
    });

    // 60 MB held for three seconds is well inside 300 MB, and the guard looks thirty times in that time.
    let said = jail.run(&format!(
        "python3 -c \"b = b'x' * (60 * 1024 * 1024); import time; time.sleep(3)\"; echo DONEPY-$((6*7)); {DONE}"
    ));
    assert!(
        said.contains("DONEPY-42"),
        "the program did not finish: {said}"
    );
    assert!(
        !jail.exited.load(Ordering::SeqCst),
        "a modest machine was stopped: {}",
        jail.transcript()
    );
    assert!(!jail.transcript().contains("machine stopped"));
}

#[test]
fn resetting_a_machine_that_does_not_exist_is_an_error_and_starts_nothing() {
    let machines = Arc::new(Mutex::new(Machines::default()));
    let err = reset_with_sink(
        &machines,
        "mach-99",
        80,
        24,
        Arc::new(|_, _| {}),
        Arc::new(|_| {}),
    )
    .unwrap_err();
    assert!(err.contains("no such machine"), "{err}");
    assert!(machines.lock().unwrap().entries.is_empty());
}
