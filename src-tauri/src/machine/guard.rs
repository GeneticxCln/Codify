//! What keeps a machine from taking the computer with it.
//!
//! A jail decides what code can *reach*; it says nothing about how much of the computer that code may *use*.
//! A loop that never ends, a program that allocates until the kernel starts killing things, or a fork bomb are
//! all inside the rules and all bad for the person who is also running a desktop. This module holds the limits,
//! and the one thing that has to be done by watching: the memory a machine is using.
//!
//! **Three kinds of limit, each by the mechanism that can enforce it without privilege:**
//!
//! * **Set once, enforced by the kernel.** The launcher sets `RLIMIT_CPU` (a process that burns more than a
//!   generous number of CPU seconds is ended, which is what a runaway loop is), `RLIMIT_FSIZE` (no single file
//!   larger than a gigabyte) and `RLIMIT_NPROC` (no fork bomb). They are per process, so a build that runs many
//!   short processes is not touched and a loop in any one of them is. They are in the jail's own argv
//!   ([`super::jail_argv`]) and need nothing from the host.
//! * **Sized, enforced by the kernel.** The scratch mounts and the project layer are `tmpfs` with a size, so
//!   filling them is "No space left on device" and not the host's memory.
//! * **Watched, enforced by this module.** Memory used by *processes* is not a per-process limit anyone can set
//!   (`RLIMIT_AS` counts address space, which a runtime such as Go or the JVM reserves far beyond what it
//!   uses), and a cgroup memory limit needs a delegated cgroup, which means systemd or root. So the shell
//!   **watches**: every half second it walks the jail's process tree from `/proc`, adds up each process's
//!   *proportional* set size (`Pss`, which counts a shared page once across the processes that share it), and
//!   when the total is over the limit it ends every process in the machine, **says why on the pane**, and the
//!   machine is then an exited machine that can be reset.
//!
//! **What this does not do, and says so.** The watch is a poll: a program that allocates faster than half a
//! second can overshoot the limit before it is seen, by as much as the machine can allocate in that time.
//! It cannot stop a **kernel** bug: the jail runs on the host's kernel, there is no guest to panic, and a
//! kernel fault that user space can reach is the host's, whatever limits are set here. And it is not a cgroup:
//! on a system with a delegated cgroup (systemd's `systemd-run --user --scope -p MemoryMax=…`) a kernel-enforced
//! limit would be stronger, and is the next step, left out because it cannot be run or tested without systemd.

use std::collections::{HashMap, HashSet};
use std::time::Duration;

/// The most a machine may use, and how closely it is watched.
#[derive(Debug, Clone)]
pub struct Limits {
    /// Memory, in bytes, summed over every process in the jail by proportional set size.
    pub memory_bytes: u64,
    /// CPU seconds one process may use before the kernel ends it.
    pub cpu_secs: u64,
    /// The largest single file a process may write, in bytes.
    pub file_bytes: u64,
    /// The size of the project layer: what the machine may change under `/work` in total.
    pub layer_bytes: u64,
    /// How often the memory is looked at.
    pub interval: Duration,
}

const GIB: u64 = 1024 * 1024 * 1024;

impl Default for Limits {
    fn default() -> Limits {
        Limits {
            memory_bytes: default_memory_bytes(total_memory_bytes()),
            // Half an hour of CPU for one process: far more than any step of a build, and a loop that
            // never ends is ended well inside the hour it would otherwise take someone to notice.
            cpu_secs: 30 * 60,
            file_bytes: GIB,
            layer_bytes: GIB,
            interval: Duration::from_millis(500),
        }
    }
}

/// Half the computer's memory, and never more than four gigabytes: a machine is one program among the
/// person's, and the cap is the share it may take, not the whole.
pub(crate) fn default_memory_bytes(total: Option<u64>) -> u64 {
    match total {
        Some(total) => (total / 2).min(4 * GIB),
        None => 2 * GIB,
    }
}

fn total_memory_bytes() -> Option<u64> {
    std::fs::read_to_string("/proc/meminfo")
        .ok()
        .and_then(|text| parse_meminfo_total(&text))
}

pub(crate) fn parse_meminfo_total(text: &str) -> Option<u64> {
    let line = text.lines().find(|l| l.starts_with("MemTotal:"))?;
    let kib: u64 = line.split_whitespace().nth(1)?.parse().ok()?;
    kib.checked_mul(1024)
}

/// The parent of a process, from the text of its `/proc/<pid>/stat`. The command name is in parentheses
/// and may itself hold spaces and parentheses, so the fields are read from after the *last* `)`.
pub(crate) fn parse_ppid(stat: &str) -> Option<u32> {
    let after = &stat[stat.rfind(')')? + 1..];
    after.split_whitespace().nth(1)?.parse().ok()
}

/// The state letter of a process (`R`, `S`, `Z`…), from the same text. A zombie is a process that has
/// ended and is waiting to be reaped: it holds no memory and runs nothing, and a machine whose only
/// remaining process is one has ended.
pub(crate) fn parse_state(stat: &str) -> Option<char> {
    let after = &stat[stat.rfind(')')? + 1..];
    after.split_whitespace().next()?.chars().next()
}

/// Every live process and its parent. Zombies are left out: they are not using anything.
fn parents() -> HashMap<u32, u32> {
    let mut map = HashMap::new();
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return map;
    };
    for entry in entries.flatten() {
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|n| n.parse::<u32>().ok())
        else {
            continue;
        };
        let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) else {
            continue;
        };
        if parse_state(&stat) == Some('Z') {
            continue;
        }
        if let Some(ppid) = parse_ppid(&stat) {
            map.insert(pid, ppid);
        }
    }
    map
}

/// `root` and every live process that descends from it. The jail's processes reparent to the jail's own init
/// when their parent ends, and that init is a descendant of `root`, so nothing in a machine is outside this.
pub(crate) fn tree(root: u32) -> Vec<u32> {
    tree_from(&parents(), root)
}

/// The walk behind [`tree`], over a map of pid to parent, so a test can hand it any map it likes. `root`
/// first, then its descendants breadth-first, each **once**: `/proc` is read a process at a time, not
/// atomically, so a pid that is reused while it is being read can make the map say that a process is its own
/// ancestor, and a walk that trusted the map would never end and would fill memory as it went. A process
/// already listed is not listed again, so the walk is bounded by the number of processes in the map. This
/// closes the loop, not the reuse: a stranger whose recorded parent is in the tree is still listed, and a
/// pid reused between this sample and [`kill_all`] is still killed.
pub(crate) fn tree_from(parents: &HashMap<u32, u32>, root: u32) -> Vec<u32> {
    if !parents.contains_key(&root) {
        return Vec::new();
    }
    let mut kids: HashMap<u32, Vec<u32>> = HashMap::new();
    for (pid, ppid) in parents {
        kids.entry(*ppid).or_default().push(*pid);
    }
    let mut out = vec![root];
    let mut seen: HashSet<u32> = HashSet::from([root]);
    let mut at = 0;
    while at < out.len() {
        if let Some(children) = kids.get(&out[at]) {
            for child in children {
                if seen.insert(*child) {
                    out.push(*child);
                }
            }
        }
        at += 1;
    }
    out
}

/// The proportional set size, in bytes, from the text of `/proc/<pid>/smaps_rollup`.
pub(crate) fn parse_pss(rollup: &str) -> Option<u64> {
    let line = rollup.lines().find(|l| l.starts_with("Pss:"))?;
    let kib: u64 = line.split_whitespace().nth(1)?.parse().ok()?;
    kib.checked_mul(1024)
}

/// One process's memory. `Pss` where the kernel gives it; otherwise the resident size from `statm`, which
/// counts shared pages in every process that maps them and so can only overstate.
fn process_bytes(pid: u32) -> u64 {
    if let Some(bytes) = std::fs::read_to_string(format!("/proc/{pid}/smaps_rollup"))
        .ok()
        .and_then(|t| parse_pss(&t))
    {
        return bytes;
    }
    std::fs::read_to_string(format!("/proc/{pid}/statm"))
        .ok()
        .and_then(|t| {
            t.split_whitespace()
                .nth(1)
                .and_then(|p| p.parse::<u64>().ok())
        })
        .map_or(0, |pages| pages * 4096)
}

/// The memory the jail's whole process tree is using.
pub(crate) fn tree_bytes(pids: &[u32]) -> u64 {
    pids.iter().map(|p| process_bytes(*p)).sum()
}

/// End every process in the list, the root first: its death takes the jail's init with it
/// (`--die-with-parent`), and the kernel ends everything in a PID namespace when its init goes.
pub(crate) fn kill_all(pids: &[u32]) {
    for pid in pids {
        // SAFETY: `kill` takes two integers and touches no memory. A pid that has since gone is ESRCH,
        // which is the outcome wanted.
        unsafe {
            libc::kill(*pid as libc::pid_t, libc::SIGKILL);
        }
    }
}

/// A byte count a person can read: `3.9 GB`, `512 MB`.
pub(crate) fn human(bytes: u64) -> String {
    const MB: f64 = 1024.0 * 1024.0;
    let b = bytes as f64;
    if b >= 1024.0 * MB {
        format!("{:.1} GB", b / (1024.0 * MB))
    } else {
        format!("{:.0} MB", b / MB)
    }
}

/// The line the pane shows when the guard has ended a machine. It names the limit and not the amount seen:
/// that is one sample of a machine still growing, and a machine only just over says "4.0 GB … may use 4.0 GB",
/// which reads as a mistake.
pub(crate) fn notice(limit: u64) -> String {
    format!(
        "\r\n[machine stopped: it used more than the {} of memory a machine may use. \
         Reset it to start again from a clean project.]\r\n",
        human(limit)
    )
}

/// What one look at a machine found.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Verdict {
    /// Within the limit, or nothing left to look at.
    Fine,
    /// Over it: how much it was using.
    Over(u64),
}

/// One look: the tree under `root`, its memory, and whether that is more than `limit`. Split from the loop
/// so a test can ask it directly.
pub(crate) fn look(root: u32, limit: u64) -> (Verdict, Vec<u32>) {
    let pids = tree(root);
    if pids.is_empty() {
        return (Verdict::Fine, pids);
    }
    let used = tree_bytes(&pids);
    if used > limit {
        (Verdict::Over(used), pids)
    } else {
        (Verdict::Fine, pids)
    }
}

/// Watch a machine until it ends or is reset. `still_mine` says whether this machine, in this generation, is
/// still the one being watched; `say` writes a line to its pane. On a breach the notice is said **first**, so
/// it is on the screen before the machine's output stream ends, and then every process is ended.
pub(crate) fn watch(
    root: u32,
    limits: &Limits,
    still_mine: impl Fn() -> bool,
    say: impl Fn(String),
) {
    loop {
        std::thread::sleep(limits.interval);
        if !still_mine() {
            return;
        }
        let (verdict, pids) = look(root, limits.memory_bytes);
        if pids.is_empty() {
            return;
        }
        if let Verdict::Over(_) = verdict {
            say(notice(limits.memory_bytes));
            kill_all(&pids);
            return;
        }
    }
}
