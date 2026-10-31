//! What an AppImage's launcher puts into the environment, taken back out of every program the shell starts.
//!
//! The AppImage runs the shell through linuxdeploy's `AppRun`, which sets, for the whole process
//! tree, `LD_LIBRARY_PATH` to the libraries the image carries (Ubuntu 22.04's GLib, OpenSSL, PCRE2
//! and the rest), `PYTHONHOME` and `PYTHONPATH` to directories inside the image, and — through the
//! GTK plugin's hook — `GI_TYPELIB_PATH`, `GIO_MODULE_DIR`, `GTK_PATH`, `GDK_PIXBUF_MODULE_FILE`
//! and their siblings. The shell's own WebKit needs every one of them. Nothing the shell starts
//! does: the engine has its own Python (`engine_protocol::engine_env`), and the programs *it*
//! starts — the system `python3` behind the folder dialog, `git`, a project's own test command —
//! are the machine's, and loading the image's libraries breaks them. On a rolling distribution the
//! folder dialog died with `libgirepository-2.0.so.0: undefined symbol: g_once_init_leave_pointer`:
//! the system's GObject introspection, linked against a GLib newer than the 2.72 the image put
//! first on the library path.
//!
//! So each spawn site applies [`restored`]: every entry of every variable that points inside the
//! image (`$APPDIR`) is dropped, a variable left with nothing is removed, and everything else is
//! left exactly as it was. That is the original value for every list `AppRun` prepends to, which
//! is all of the path-like ones. Two non-path values it sets cannot be told apart from a person's
//! own and are left alone: `PYTHONDONTWRITEBYTECODE=1` and the hook's `GTK_THEME`.
//!
//! Outside an AppImage there is no `$APPDIR` and this changes nothing.

use std::ffi::{OsStr, OsString};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};

/// The image's mount point, when this process is running from an AppImage: `$APPDIR`, but only
/// beside an `$APPIMAGE`, and only when it is an absolute path below `/` — an empty or relative
/// `APPDIR` from some other tool must not make every relative entry look like the image's.
pub(crate) fn appdir() -> Option<PathBuf> {
    appdir_from(
        std::env::var_os("APPIMAGE").as_deref(),
        std::env::var_os("APPDIR").as_deref(),
    )
}

fn appdir_from(appimage: Option<&OsStr>, appdir: Option<&OsStr>) -> Option<PathBuf> {
    appimage?;
    let dir = PathBuf::from(appdir?);
    (dir.is_absolute() && dir.parent().is_some()).then_some(dir)
}

fn inside(entry: &[u8], appdir: &Path) -> bool {
    !entry.is_empty() && Path::new(OsStr::from_bytes(entry)).starts_with(appdir)
}

/// `value` without its entries inside `appdir` (and without the empty ones the launcher's
/// `…:$ORIGINAL` leaves when there was no original); `None` when nothing is left, and the value
/// unchanged when nothing in it was the image's.
pub(crate) fn without_appdir(value: &OsStr, appdir: &Path) -> Option<OsString> {
    let entries: Vec<&[u8]> = value.as_bytes().split(|byte| *byte == b':').collect();
    if !entries.iter().any(|entry| inside(entry, appdir)) {
        return Some(value.to_os_string());
    }
    let kept: Vec<&[u8]> = entries
        .into_iter()
        .filter(|entry| !entry.is_empty() && !inside(entry, appdir))
        .collect();
    (!kept.is_empty()).then(|| OsString::from_vec(kept.join(&b':')))
}

/// The changes that take the image back out of `env`: `(name, Some(value))` to set and
/// `(name, None)` to remove. Empty outside an AppImage (`appdir` is `None`).
pub(crate) fn restored<I>(env: I, appdir: Option<&Path>) -> Vec<(OsString, Option<OsString>)>
where
    I: IntoIterator<Item = (OsString, OsString)>,
{
    let Some(appdir) = appdir else {
        return Vec::new();
    };
    env.into_iter()
        .filter_map(|(name, value)| {
            let cleaned = without_appdir(&value, appdir);
            (cleaned.as_ref() != Some(&value)).then_some((name, cleaned))
        })
        .collect()
}

/// [`restored`] for this process's own environment, ready to apply to a command.
pub(crate) fn restored_here() -> Vec<(OsString, Option<OsString>)> {
    restored(std::env::vars_os(), appdir().as_deref())
}

#[cfg(test)]
mod tests {
    use super::*;

    const APPDIR: &str = "/tmp/.mount_CodifyAb12";

    fn env(pairs: &[(&str, &str)]) -> Vec<(OsString, OsString)> {
        pairs
            .iter()
            .map(|(name, value)| (OsString::from(name), OsString::from(value)))
            .collect()
    }

    fn change(name: &str, value: Option<&str>) -> (OsString, Option<OsString>) {
        (OsString::from(name), value.map(OsString::from))
    }

    #[test]
    fn outside_an_appimage_nothing_changes() {
        let seen = env(&[(
            "LD_LIBRARY_PATH",
            "/tmp/.mount_CodifyAb12/usr/lib/:/opt/lib",
        )]);
        assert!(restored(seen, None).is_empty());
    }

    #[test]
    fn the_launchers_prefix_comes_off_and_the_original_value_is_what_is_left() {
        // As linuxdeploy's AppRun writes it: the image's ten directories, then `:` and the original.
        let ld = format!(
            "{APPDIR}/usr/lib/:{APPDIR}/usr/lib/i386-linux-gnu/:{APPDIR}/usr/lib/x86_64-linux-gnu/:\
             {APPDIR}/usr/lib32/:{APPDIR}/usr/lib64/:{APPDIR}/lib/:{APPDIR}/lib/i386-linux-gnu/:\
             {APPDIR}/lib/x86_64-linux-gnu/:{APPDIR}/lib32/:{APPDIR}/lib64/:/opt/cuda/lib64"
        );
        let xdg = format!("{APPDIR}/usr/share:/usr/share:/usr/local/share");
        let path = format!("{APPDIR}/usr/bin/:{APPDIR}/usr/sbin/:/home/me/.local/bin:/usr/bin");
        let changes = restored(
            env(&[
                ("LD_LIBRARY_PATH", &ld),
                ("XDG_DATA_DIRS", &xdg),
                ("PATH", &path),
            ]),
            Some(Path::new(APPDIR)),
        );
        assert_eq!(
            changes,
            vec![
                change("LD_LIBRARY_PATH", Some("/opt/cuda/lib64")),
                change("XDG_DATA_DIRS", Some("/usr/share:/usr/local/share")),
                change("PATH", Some("/home/me/.local/bin:/usr/bin")),
            ]
        );
    }

    #[test]
    fn a_variable_that_was_only_the_images_is_removed() {
        // No original LD_LIBRARY_PATH leaves a trailing `:` — an empty entry, which the dynamic
        // linker reads as "the current directory". It goes with the rest.
        let ld = format!("{APPDIR}/usr/lib/:{APPDIR}/lib64/:");
        let changes = restored(
            env(&[
                ("LD_LIBRARY_PATH", &ld),
                ("PYTHONHOME", &format!("{APPDIR}/usr/")),
                (
                    "GI_TYPELIB_PATH",
                    &format!("{APPDIR}//usr/lib/girepository-1.0"),
                ),
                (
                    "GDK_PIXBUF_MODULE_FILE",
                    &format!("{APPDIR}//usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"),
                ),
                ("APPDIR", APPDIR),
            ]),
            Some(Path::new(APPDIR)),
        );
        for name in [
            "LD_LIBRARY_PATH",
            "PYTHONHOME",
            "GI_TYPELIB_PATH",
            "GDK_PIXBUF_MODULE_FILE",
            "APPDIR",
        ] {
            assert!(
                changes.contains(&change(name, None)),
                "{name} should be removed: {changes:?}"
            );
        }
    }

    #[test]
    fn what_is_not_the_images_is_left_exactly_as_it_was() {
        let changes = restored(
            env(&[
                ("HOME", "/home/me"),
                ("GTK_THEME", "Adwaita:dark"),
                ("APPIMAGE", "/home/me/Downloads/Codify_1.3.0_amd64.AppImage"),
                // Shares the mount's spelling as a string, not as a directory.
                ("SOMETHING", "/tmp/.mount_CodifyAb12-other/lib"),
                ("EMPTY", ""),
                ("KEEPS_ITS_COLONS", "/a::/b"),
            ]),
            Some(Path::new(APPDIR)),
        );
        assert!(changes.is_empty(), "{changes:?}");
    }

    #[test]
    fn the_appdir_needs_an_appimage_and_an_absolute_path() {
        let image = Some(OsStr::new("/home/me/Codify.AppImage"));
        assert_eq!(
            appdir_from(image, Some(OsStr::new(APPDIR))),
            Some(PathBuf::from(APPDIR))
        );
        // An APPDIR some other tool left behind, with no AppImage running this process.
        assert_eq!(appdir_from(None, Some(OsStr::new(APPDIR))), None);
        // A relative or root APPDIR would make every entry look like the image's.
        for bad in ["", "relative/dir", "/"] {
            assert_eq!(appdir_from(image, Some(OsStr::new(bad))), None, "{bad:?}");
        }
        assert_eq!(appdir_from(image, None), None);
    }

    #[test]
    fn every_program_the_shell_starts_from_its_own_environment_goes_through_it() {
        // The engine, the login shell asked for its PATH, and the terminal pane's shells. The
        // machine tab clears its environment entirely and needs nothing from here.
        let lib = include_str!("lib.rs");
        let terminal = include_str!("terminal.rs");
        assert_eq!(
            lib.matches("appimage_env::restored_here()").count(),
            2,
            "engine + login shell"
        );
        assert!(
            lib.contains("appimage_env::without_appdir"),
            "the engine's merged PATH"
        );
        assert_eq!(
            terminal.matches("appimage_env::restored_here()").count(),
            1,
            "the terminal's shell"
        );
    }
}
