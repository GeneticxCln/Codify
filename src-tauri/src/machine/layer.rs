//! The machine's copy-on-write view of the project, and how it is made.
//!
//! **What it is.** The project is the *lower* layer of an overlay filesystem and is never written: it is the
//! person's own directory, bound read-only. Everything the machine changes lands in an *upper* layer that is a
//! size-capped `tmpfs` living in the machine's own mount namespace. So an assistant can edit, build, run a test
//! suite that writes next to the code and make a mess, and the person's files are exactly as they were. The layer
//! has no existence outside the machine: when the last process in the jail ends the namespace is gone and the
//! layer with it, which is what makes "discard everything and start clean" a restart and not a cleanup.
//!
//! **How it is made, and why that way.** `bwrap` 0.9 has no overlay option (they arrive in 0.11), and an overlay
//! needs `CAP_SYS_ADMIN` in *some* user namespace. So the launcher puts one step in front of the jail: `unshare`
//! makes a user namespace in which the person is root and a mount namespace of its own, a short **fixed** script
//! mounts the `tmpfs` and the overlay, and the script then `exec`s `bwrap` with the jail's argv, binding the
//! overlay's merged directory at `/work`. Nothing the script does reaches the host: the mounts are in a private
//! namespace, the only host path it names is an empty directory this module created for the purpose, and no
//! request text is ever part of the script (paths arrive as positional parameters, not as script text).
//!
//! **It is a feature, not a requirement.** Not every host can do this (the kernel must allow an unprivileged
//! overlay mount, `unshare` and `mount` must be there, and the upper layer's filesystem must support what an
//! overlay needs). [`probe`] asks by doing it, once, against a throwaway project; where it cannot, the machine
//! is the read-only one it has always been, and says so. There is no case in which the layer's absence loosens
//! the jail.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};

/// The exit status of the mount script when a mount fails, so a probe can tell "could not mount" from
/// "the program it ran failed".
pub(crate) const MOUNT_FAILED: i32 = 97;

/// The script the launcher runs inside the new user and mount namespaces, before the jail. Fixed text: the
/// stage directory, the size and the project arrive as `$1`..`$3` and the program to run as the rest.
///
/// `userxattr` is what lets an overlay be mounted without privilege in the initial namespace: it keeps the
/// overlay's bookkeeping in `user.*` attributes, which a `tmpfs` supports from Linux 6.6 and every ordinary
/// disk filesystem always has. On a kernel where it does not, [`probe`] fails and the machine is read-only.
pub(crate) const MOUNT_SCRIPT: &str = r#"stage=$1; size=$2; lower=$3; shift 3
mount -t tmpfs -o "size=$size,mode=0700" tmpfs "$stage" || exit 96
mkdir "$stage/upper" "$stage/ovwork" "$stage/merged" || exit 96
mount -t overlay overlay -o "userxattr,lowerdir=$lower,upperdir=$stage/upper,workdir=$stage/ovwork" "$stage/merged" || exit 97
exec "$@""#;

/// The program that starts the new namespaces, and its fixed flags. The person is root *inside* the user
/// namespace this makes, which is how they may mount; to the host they are the same person.
pub(crate) const UNSHARE_FLAGS: [&str; 4] = ["--user", "--map-root-user", "--mount", "--"];

/// What a machine's project is, as seen from the outside: the sentence the pane shows and a flag the
/// assistant is told, so neither has to guess whether writing under `/work` can work.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectNote {
    /// Changes made under `/work` stay in this machine and are discarded with it.
    pub copy_on_write: bool,
    /// Why it is not, when it is not: the reason this host could not make the layer.
    pub reason: Option<String>,
}

impl ProjectNote {
    pub(crate) fn layered() -> ProjectNote {
        ProjectNote {
            copy_on_write: true,
            reason: None,
        }
    }

    pub(crate) fn read_only(reason: impl Into<String>) -> ProjectNote {
        ProjectNote {
            copy_on_write: false,
            reason: Some(reason.into()),
        }
    }
}

/// Whether a path can appear in an overlay's option string. `,` ends an option, `:` separates lower
/// directories and `\` escapes; none of them can be written into `lowerdir=` safely, so a project whose path
/// holds one is shown read-only rather than risk the overlay reading the path as something else.
pub(crate) fn overlay_safe(path: &Path) -> Result<(), String> {
    let Some(text) = path.to_str() else {
        return Err("its path is not valid UTF-8".to_string());
    };
    if let Some(bad) = text
        .chars()
        .find(|c| matches!(c, ',' | ':' | '\\') || c.is_control())
    {
        return Err(format!(
            "its path contains {bad:?}, which an overlay's options cannot hold"
        ));
    }
    Ok(())
}

/// An empty directory for the layer's `tmpfs` to be mounted over, inside the machine's namespace. It is the
/// only thing the host ever holds for a machine's layer, it is empty for as long as it exists (the mounts are
/// in a private namespace), and it is removed with `remove_dir`, which refuses a directory that is not.
#[derive(Debug)]
pub(crate) struct Stage {
    dir: PathBuf,
}

static STAGE_SEQ: AtomicU64 = AtomicU64::new(0);

impl Stage {
    /// Make a fresh, empty, owner-only directory under `base`.
    pub(crate) fn create(base: &Path) -> Result<Stage, String> {
        use std::os::unix::fs::DirBuilderExt;
        let n = STAGE_SEQ.fetch_add(1, Ordering::SeqCst);
        let dir = base.join(format!("codify-machine-{}-{n}", std::process::id()));
        // The path is checked before the directory exists: a refused path must leave nothing behind, and
        // there is no `Stage` yet to release if the check came after.
        overlay_safe(&dir)?;
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&dir)
            .map_err(|e| format!("could not make a place for the project layer: {e}"))?;
        Ok(Stage { dir })
    }

    pub(crate) fn dir(&self) -> &Path {
        &self.dir
    }

    /// Where the overlay is mounted, inside the machine's namespace: what the jail binds at `/work`.
    pub(crate) fn merged(&self) -> PathBuf {
        self.dir.join("merged")
    }

    /// Remove the empty directory. Never recursive: if something is in it, it is left, because a directory
    /// that is not empty is not the one this created.
    pub(crate) fn release(self) {
        let _ = std::fs::remove_dir(&self.dir);
    }
}

/// The argv that starts the jail behind the overlay: `unshare`'s flags, the fixed script, then `bwrap` and
/// its arguments as the script's trailing parameters. A pure function of its inputs, so a test can read it.
pub(crate) fn launch_args(
    stage: &Path,
    size_bytes: u64,
    lower: &Path,
    bwrap: &Path,
    jail: &[String],
) -> Vec<String> {
    let mut args: Vec<String> = UNSHARE_FLAGS.iter().map(|s| s.to_string()).collect();
    args.extend(
        ["/bin/sh", "-c", MOUNT_SCRIPT, "sh"]
            .iter()
            .map(|s| s.to_string()),
    );
    args.push(stage.to_string_lossy().into_owned());
    args.push(size_bytes.to_string());
    args.push(lower.to_string_lossy().into_owned());
    args.push(bwrap.to_string_lossy().into_owned());
    args.extend(jail.iter().cloned());
    args
}

/// `name` on `path_var`, if it is there and executable.
pub(crate) fn find_on_path(name: &str, path_var: &str) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    path_var
        .split(':')
        .filter(|dir| !dir.is_empty())
        .map(|dir| Path::new(dir).join(name))
        .find(|candidate| {
            std::fs::metadata(candidate)
                .map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
                .unwrap_or(false)
        })
}

/// What was learned the one time this host was asked, per place the stage is made.
static PROBED: OnceLock<Mutex<Vec<Probe>>> = OnceLock::new();

/// One place the stage is made, and whether this host could make a layer there (or why not).
type Probe = (PathBuf, Result<(), String>);

/// Ask whether this host can make the layer, by making one: a throwaway project with a directory in it, the
/// real mount script, and a program that deletes the directory and makes it again, which is the part of an
/// overlay a filesystem without `user.*` attributes cannot do. The answer is remembered per `base`.
pub(crate) fn probe(base: &Path, unshare: &Path, path_var: &str) -> Result<(), String> {
    let cache = PROBED.get_or_init(|| Mutex::new(Vec::new()));
    if let Ok(known) = cache.lock() {
        if let Some((_, answer)) = known.iter().find(|(b, _)| b == base) {
            return answer.clone();
        }
    }
    let answer = probe_uncached(base, unshare, path_var);
    if let Ok(mut known) = cache.lock() {
        known.push((base.to_path_buf(), answer.clone()));
    }
    answer
}

pub(crate) fn probe_uncached(base: &Path, unshare: &Path, path_var: &str) -> Result<(), String> {
    use std::process::{Command, Stdio};
    let stage = Stage::create(base)?;
    let lower = base.join(format!(
        "codify-machine-probe-{}-{}",
        std::process::id(),
        STAGE_SEQ.fetch_add(1, Ordering::SeqCst)
    ));
    let made = std::fs::create_dir_all(lower.join("d"))
        .and_then(|()| std::fs::write(lower.join("d/x"), b"x"));
    let outcome = made
        .map_err(|e| format!("could not make a throwaway project to try on: {e}"))
        .and_then(|()| {
            overlay_safe(&lower)?;
            if lower.to_string_lossy().contains('\'') || stage.dir().to_string_lossy().contains('\'') {
                return Err("its path contains a quote".to_string());
            }
            let merged = stage.merged();
            let check = format!(
                "rm -r '{m}/d' && mkdir '{m}/d' && test -z \"$(ls -A '{m}/d')\" && test -f '{l}/d/x'",
                m = merged.display(),
                l = lower.display()
            );
            let mut args: Vec<String> = UNSHARE_FLAGS.iter().map(|s| s.to_string()).collect();
            args.extend(
                ["/bin/sh", "-c", MOUNT_SCRIPT, "sh"]
                    .iter()
                    .map(|s| s.to_string()),
            );
            args.push(stage.dir().to_string_lossy().into_owned());
            args.push((8u64 * 1024 * 1024).to_string());
            args.push(lower.to_string_lossy().into_owned());
            args.extend(["/bin/sh".into(), "-c".into(), check]);
            let out = Command::new(unshare)
                .args(&args)
                .env_clear()
                .env("PATH", path_var)
                .stdin(Stdio::null())
                .output()
                .map_err(|e| format!("could not run unshare: {e}"))?;
            if out.status.success() {
                return Ok(());
            }
            let said = String::from_utf8_lossy(&out.stderr);
            let first = said.lines().next().unwrap_or("").trim();
            Err(match out.status.code() {
                Some(MOUNT_FAILED) => format!(
                    "this system will not mount an overlay without privilege ({first})"
                ),
                Some(96) => format!("this system will not mount a tmpfs without privilege ({first})"),
                Some(_) => format!(
                    "an overlay mounted here but cannot replace a directory, which is what \
                     deleting and recreating one needs ({first})"
                ),
                None => "the probe was stopped by a signal".to_string(),
            })
        });
    // The probe's own leftovers, removed one named path at a time and never recursively: the two files and
    // directories it made, then the empty stage.
    let _ = std::fs::remove_file(lower.join("d/x"));
    let _ = std::fs::remove_dir(lower.join("d"));
    let _ = std::fs::remove_dir(&lower);
    stage.release();
    outcome
}
