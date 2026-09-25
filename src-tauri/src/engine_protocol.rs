//! The engine contract, kept out of the Tauri shell so it can be tested.
//!
//! Everything here is pure: a line of stdout, a directory, a string. These used to
//! live inside `launch_engine` and the command handlers, where the only way to reach
//! them was to boot the app — a window, a webview and a spawned Python process before
//! the first assertion — so nothing reached them at all. The two that parse output
//! from another process and decide *where* to look for it had never been run by a
//! test in their lives. `cargo test` covers this module (see `check-tauri` in the
//! Makefile), and the shell in `lib.rs` keeps the parts that genuinely need a
//! process: spawning, reading, killing.

use std::path::{Path, PathBuf};

/// The marker the engine's boot line starts with — the boot contract with the shell,
/// which is why it lives here beside the parser rather than in two places.
const HANDSHAKE_MARKER: &str = "CODIFY_ENGINE";

/// Fences the login shell's `$PATH` in the output of [`login_path_probe`].
///
/// The whole output cannot be treated as the PATH: an rc file may print anything it
/// likes, and a shell in verbose mode prints the command it is running. Reading one
/// marked line is what makes the answer independent of that chatter.
const LOGIN_PATH_MARKER: &str = "__CODIFY_LOGIN_PATH__=";

/// The one-liner the login shell is asked to run, as a single `-c` argument.
///
/// `printf` rather than `echo`: it takes no options, so a PATH that happens to start
/// with `-` is printed rather than interpreted.
pub(crate) fn login_path_probe() -> String {
    format!("printf '\\n{LOGIN_PATH_MARKER}%s\\n' \"$PATH\"")
}

/// The login shell's PATH, pulled out of its output, or `None` when it never arrived.
///
/// The *last* marked line wins: rc files run before the probe does, and one that
/// prints a line of its own beginning with the marker (or a shell echoing the probe
/// under `set -x`) must not be mistaken for the answer.
pub(crate) fn parse_login_path(output: &str) -> Option<String> {
    output
        .lines()
        .filter_map(|line| line.strip_prefix(LOGIN_PATH_MARKER))
        .next_back()
        .map(str::trim)
        .filter(|path| !path.is_empty())
        .map(str::to_string)
}

/// The PATH the engine should run under: what this process already has, then every
/// entry the login shell adds that it does not have yet — plus how many that was.
/// POSIX-separated; the only caller is gated on `unix`.
///
/// A union, not a replacement, and the inherited half keeps precedence. Replacing the
/// PATH would make the app resolve, say, `/usr/bin/python3` over the virtualenv
/// interpreter it was launched from — breaking the one case that works today, in
/// exchange for matching the terminal's ordering in a case where both find the same
/// tool. Appending cannot break a PATH that already worked; it can only make a tool
/// that was missing findable.
///
/// The count is returned rather than left to the caller: zero is the interesting case
/// ("your shell's PATH was already covered") and reporting it is what distinguishes
/// that from a shell that never answered.
pub(crate) fn merge_path(inherited: &str, login: &str) -> (String, usize) {
    let mut entries: Vec<&str> = Vec::new();
    for entry in inherited.split(':') {
        let entry = entry.trim();
        if !entry.is_empty() && !entries.contains(&entry) {
            entries.push(entry);
        }
    }
    let inherited_entries = entries.len();
    for entry in login.split(':') {
        let entry = entry.trim();
        if !entry.is_empty() && !entries.contains(&entry) {
            entries.push(entry);
        }
    }
    (entries.join(":"), entries.len() - inherited_entries)
}

/// Where `python3 -m engine` has to run: the checkout that contains `engine/`.
///
/// Tauri's dev flow runs with `src-tauri` as the working directory, so that one
/// segment is stripped. Anywhere else the working directory is taken as it is — this
/// is a guess about how the app was started, not a fact about where the engine lives,
/// which is why a wrong guess has to come out as `launch_problem`'s message rather
/// than as an empty window.
pub(crate) fn project_root_from(cwd: &Path) -> PathBuf {
    if cwd.ends_with("src-tauri") {
        cwd.parent().unwrap_or(cwd).to_path_buf()
    } else {
        cwd.to_path_buf()
    }
}

/// Parse the engine's boot line: `CODIFY_ENGINE token=<hex> port=<int>`.
///
/// `None` for anything else, including a line carrying the marker but no usable
/// port — the caller treats "not ready yet" and "never" the same way, and a handshake
/// read half-way must not park a token next to a missing port. Field order and extra
/// whitespace do not matter: this is the other process's stdout, and a stricter
/// parser would fail a boot over spacing.
pub(crate) fn parse_handshake(line: &str) -> Option<(String, u16)> {
    if !line.starts_with(HANDSHAKE_MARKER) {
        return None;
    }
    let mut token = None;
    let mut port = None;
    for field in line.split_whitespace().skip(1) {
        if let Some(value) = field.strip_prefix("token=") {
            token = Some(value.to_string());
        } else if let Some(value) = field.strip_prefix("port=") {
            port = value.parse::<u16>().ok();
        }
    }
    token.zip(port)
}

/// Why `python3 -m engine` cannot deliver a handshake from `root`, when it cannot.
///
/// The launcher runs a bare `python3` in the directory it guessed, so the commonest
/// failure is not an OS error at all: started outside the checkout, the interpreter
/// comes up, fails to import `engine`, and exits — a traceback on a stderr no window
/// user ever sees, and a shell that can only report "engine not ready". Naming the
/// missing piece here is what turns that into something a person can act on. A
/// missing *interpreter* needs no check: the spawn fails with the OS error, which the
/// launcher reports as-is.
pub(crate) fn launch_problem(root: &Path) -> Option<String> {
    if root.join("engine").join("__main__.py").is_file() {
        return None;
    }
    Some(format!(
        "No engine to run at {} — this shell starts `python3 -m engine` from the project \
         checkout, so the app has to be launched from the Codify repository (with the \
         engine's dependencies installed).",
        root.display()
    ))
}

/// Why the engine is not running when it came up and then exited before reporting a
/// port.
///
/// The checkout was there and the spawn succeeded, so the failure is inside the engine
/// itself — a missing dependency, an unreadable config, a port it could not bind. Its
/// traceback went to the stderr this shell inherits, which is a terminal the window user
/// may not have in front of them, so the message has to name the command that reproduces
/// it — and say it in the directory the shell actually ran from, not in the abstract.
pub(crate) fn engine_exited_problem(root: &Path) -> String {
    format!(
        "The engine started but exited before it reported a port. Run `python3 -m engine` in \
         {} to see what it printed.",
        root.display()
    )
}

/// The role name goes into a URL path; a frontend bug (or a compromise) must not be
/// able to traverse into other engine endpoints via `..%2f`-style sequences. Roles
/// are lowercase slugs — anything else is refused here, at the boundary, rather than
/// being escaped into a path and trusted to the engine.
pub(crate) fn valid_role(role: &str) -> Result<(), String> {
    let ok = !role.is_empty()
        && role.len() <= 64
        && role
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-');
    if ok {
        Ok(())
    } else {
        Err("invalid role identifier".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A directory of this test's own, so a stray `engine/` in the system temp
    /// directory can never decide the outcome.
    fn scratch_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "codify-engine-protocol-{}-{name}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("scratch directory");
        dir
    }

    #[test]
    fn a_boot_line_yields_its_token_and_port() {
        assert_eq!(
            Some(("9f2c".to_string(), 7430)),
            parse_handshake("CODIFY_ENGINE token=9f2c port=7430")
        );
    }

    #[test]
    fn field_order_and_extra_whitespace_do_not_matter() {
        assert_eq!(
            Some(("9f2c".to_string(), 7431)),
            parse_handshake("CODIFY_ENGINE   port=7431  token=9f2c  ")
        );
    }

    #[test]
    fn a_line_that_is_not_a_handshake_is_refused() {
        for line in [
            "",
            "no handshake here token=9f2c port=7430",
            "CODIFY_ENGINE_STARTING",
            // Wrapped in something else: only the start of the line is the contract.
            // A parser that searched anywhere in the line would accept a log
            // prefix as a handshake, which is the difference `starts_with` buys.
            "[engine] CODIFY_ENGINE token=9f2c port=7430",
        ] {
            assert_eq!(None, parse_handshake(line), "must not parse: {line:?}");
        }
    }

    #[test]
    fn a_handshake_missing_either_field_is_not_a_handshake() {
        // A token with no port would hand the UI an unusable connection, so the
        // line is refused and the launcher keeps reading instead.
        for line in [
            "CODIFY_ENGINE token=9f2c",
            "CODIFY_ENGINE port=7430",
            "CODIFY_ENGINE token=9f2c port=not-a-number",
            "CODIFY_ENGINE token=9f2c port=99999",
            "CODIFY_ENGINE",
        ] {
            assert_eq!(None, parse_handshake(line), "must not parse: {line:?}");
        }
    }

    #[test]
    fn the_shell_directory_is_stripped_off_the_project_root() {
        assert_eq!(
            PathBuf::from("/home/me/Codify"),
            project_root_from(Path::new("/home/me/Codify/src-tauri"))
        );
    }

    #[test]
    fn any_other_working_directory_is_taken_as_it_stands() {
        for cwd in [
            "/tmp",
            "/home/me/Codify",
            "/home/me/Codify/ui",
            "/src-tauri",
        ] {
            let expected = if cwd == "/src-tauri" {
                PathBuf::from("/")
            } else {
                PathBuf::from(cwd)
            };
            assert_eq!(expected, project_root_from(Path::new(cwd)), "cwd {cwd}");
        }
    }

    #[test]
    fn a_directory_with_no_engine_names_itself_as_the_problem() {
        let dir = scratch_dir("bare");
        let problem = launch_problem(&dir).expect("a bare directory has no engine to run");
        assert!(
            problem.contains(&dir.display().to_string()),
            "the message has to name the directory it looked in: {problem}"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_checkout_that_has_the_engine_module_has_no_problem() {
        let dir = scratch_dir("checkout");
        std::fs::create_dir_all(dir.join("engine")).expect("engine directory");
        std::fs::write(
            dir.join("engine").join("__main__.py"),
            "from engine.app import main\n",
        )
        .expect("entrypoint");
        assert_eq!(None, launch_problem(&dir));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_engine_that_exited_names_the_directory_to_reproduce_it_in() {
        let root = Path::new("/home/me/Codify");
        let problem = engine_exited_problem(root);
        // The user has to be able to copy the directory and the command out of the
        // message; a generic "the engine exited" would leave them with no next step.
        assert!(
            problem.contains("/home/me/Codify") && problem.contains("python3 -m engine"),
            "the message has to name both the directory and the command: {problem}"
        );
    }

    #[test]
    fn a_reporting_shell_hands_back_its_path() {
        // Shaped like real output: an rc banner, the marked line, then whatever the
        // shell prints on its way out.
        let output = "welcome to your shell\n\
                      __CODIFY_LOGIN_PATH__=/home/me/.local/bin:/usr/bin:/bin\n\
                      bye\n";
        assert_eq!(
            Some("/home/me/.local/bin:/usr/bin:/bin".to_string()),
            parse_login_path(output)
        );
    }

    #[test]
    fn the_last_marked_line_wins() {
        // An rc file is free to print a line that starts with the marker; only the
        // probe's own line comes last, so a first-match parser would return the rc
        // file's line and quietly hand the engine the wrong PATH.
        let output = "__CODIFY_LOGIN_PATH__=/from/an/rc/file\n\
                      __CODIFY_LOGIN_PATH__=/the/real/one\n";
        assert_eq!(Some("/the/real/one".to_string()), parse_login_path(output));
    }

    #[test]
    fn silence_and_an_empty_path_are_both_no_answer() {
        for output in [
            "",
            "welcome\n",
            // The shell ran but $PATH was empty: nothing to merge, and "" must not
            // become a PATH entry of its own.
            "__CODIFY_LOGIN_PATH__=\n",
            "__CODIFY_LOGIN_PATH__=   \n",
        ] {
            assert_eq!(None, parse_login_path(output), "output {output:?}");
        }
    }
    #[test]
    fn merging_appends_only_what_the_inherited_path_lacks() {
        // The count is what the launcher logs, and "0 added" has to mean the shell was
        // asked and had nothing to offer — not that the ask never happened.
        assert_eq!(
            ("/usr/bin:/bin:/home/me/.local/bin".to_string(), 1),
            merge_path("/usr/bin:/bin", "/home/me/.local/bin:/usr/bin:/bin")
        );
        assert_eq!(
            ("/usr/bin:/bin".to_string(), 0),
            merge_path("/usr/bin:/bin", "/bin:/usr/bin")
        );
        assert_eq!(String::new(), merge_path("", "").0);
    }

    #[test]
    fn merging_keeps_the_inherited_path_first_and_whole() {
        // Precedence is the point: an entry the process already had — a virtualenv's
        // `bin`, a test harness's shim — must not be moved behind the /usr/bin the
        // login shell also lists.
        assert_eq!(
            ("/venv/bin:/usr/bin:/bin:/home/me/.local/bin".to_string(), 1),
            merge_path(
                "/venv/bin:/usr/bin:/bin",
                "/usr/bin:/bin:/home/me/.local/bin"
            )
        );
        // Duplicates inside the login PATH collapse, and empty segments — a trailing
        // or doubled `:` — never become entries of their own (an empty entry means
        // "the current directory" to a shell).
        assert_eq!(
            (
                "/usr/bin:/bin:/opt/tools:/home/me/.local/bin".to_string(),
                2
            ),
            merge_path(
                "/usr/bin::/bin:",
                "/opt/tools:/usr/bin:/opt/tools::/home/me/.local/bin"
            )
        );
        // Nothing inherited at all: every entry the shell offers is new.
        assert_eq!(
            ("/opt:/usr/bin".to_string(), 2),
            merge_path("", "/opt:/usr/bin")
        );
    }

    #[test]
    fn role_identifiers_are_lowercase_slugs() {
        for role in ["fixer", "laya_gate", "coder-1", "librarian"] {
            assert!(valid_role(role).is_ok(), "{role:?} must be accepted");
        }
        let longest_allowed = "y".repeat(64);
        assert!(
            valid_role(&longest_allowed).is_ok(),
            "64 characters is still a slug"
        );

        let too_long = "x".repeat(65);
        for role in [
            // Empty, capitalised, or a path: the three shapes a traversal needs.
            "",
            "Fixer",
            "fixer/gate",
            "../settings/agents",
            "..%2fgoals",
            "fix er",
            "fix.er",
            too_long.as_str(),
        ] {
            assert!(valid_role(role).is_err(), "{role:?} must be refused");
        }
    }
}
