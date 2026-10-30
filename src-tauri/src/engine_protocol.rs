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

/// The field carrying the engine's own bound on a stop request, in seconds.
///
/// The shell asks the engine to stop and then waits exactly this long before it
/// escalates to `SIGKILL`, so an interrupted turn gets recorded instead of being
/// cut off mid-write. It is announced rather than duplicated because a second copy
/// in Rust is free to drift, and drift here is a turn that vanishes.
const HANDSHAKE_HARD_EXIT_FIELD: &str = "hard_exit_s";

/// What the engine said about itself on its way up.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Handshake {
    pub token: String,
    pub port: u16,
    /// How long this engine promises a stop request to take, in seconds — or `None`
    /// when the line did not say. `None` is a normal outcome, not a parse failure:
    /// the token and port are what the app cannot boot without, and an engine that
    /// does not announce a bound still gets asked politely.
    pub hard_exit_s: Option<f64>,
}

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
/// Colon-separated, as PATH is on every platform Codify runs on.
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

/// Whether `dir` is a checkout `python3 -m engine` can run in.
fn holds_engine(dir: &Path) -> bool {
    dir.join("engine").join("__main__.py").is_file()
}

/// The checkout the engine runs from, decided in the order a person would expect.
///
/// A window opened from the applications menu (a `.desktop` file, the Linux way to
/// "install" a build that has no installer) starts in `$HOME`, and "the working
/// directory is the checkout" — the only rule there was — turned every such launch into
/// the "No engine to run" message. So, in order:
///
/// 1. **`CODIFY_ROOT`**, when set. It is taken *as given even when it is wrong*: a
///    mistyped variable must come out as `launch_problem` naming that path, never be
///    quietly replaced by a guess that happens to work on this machine and not the next.
/// 2. **The working directory** (with `src-tauri` stripped), when it holds the engine.
///    This is the dev flow, and it wins over the binary's location so that running a
///    second checkout from inside it runs that checkout.
/// 3. **An ancestor of the executable** that holds the engine. A binary built in place
///    lives at `<checkout>/src-tauri/target/{debug,release}/codify`, so the checkout is
///    a few parents up, and this is what makes a launcher work from any directory.
/// 4. **The working directory anyway**, so the failure message names where the shell
///    actually looked first.
pub(crate) fn resolve_project_root(
    explicit: Option<&str>,
    exe: Option<&Path>,
    cwd: &Path,
) -> PathBuf {
    if let Some(root) = explicit.map(str::trim).filter(|root| !root.is_empty()) {
        return PathBuf::from(root);
    }
    let from_cwd = project_root_from(cwd);
    if holds_engine(&from_cwd) {
        return from_cwd;
    }
    if let Some(exe) = exe {
        if let Some(found) = exe.ancestors().skip(1).find(|dir| holds_engine(dir)) {
            return found.to_path_buf();
        }
    }
    from_cwd
}

/// The directory under the app's resources that a packaged build carries its engine in:
/// `python/` (a CPython of its own), `engine/` and `BUNDLE.json`, as
/// `scripts/bundle_engine.py` builds it. `/usr/lib/Codify/engine-bundle` in the deb, and the
/// same path inside an AppImage's mount.
pub(crate) const BUNDLE_DIR: &str = "engine-bundle";

/// Which kind of engine a launch runs, and so how it is started and how it is described.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum EngineSource {
    /// `CODIFY_ROOT` named it, and it is used as given.
    Explicit,
    /// The engine this package was built with.
    Bundled,
    /// A checkout found from the working directory or the executable.
    Checkout,
}

/// Where the engine runs, under which interpreter, and why that one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct EngineLaunch {
    pub root: PathBuf,
    pub interpreter: PathBuf,
    pub source: EngineSource,
}

fn bundled_interpreter(bundle: &Path) -> PathBuf {
    bundle.join("python").join("bin").join("python3")
}

/// Whether `dir` is a whole bundle: its own interpreter as well as the engine. A bundle with
/// one and not the other is a broken install, and running the engine half under the PATH's
/// `python3` would hide that behind whatever that interpreter happens to have installed.
fn holds_bundle(dir: &Path) -> bool {
    bundled_interpreter(dir).is_file() && holds_engine(dir)
}

/// The engine to run, in the order a person would expect.
///
/// 1. **`CODIFY_ROOT`**, when set, as given even when it is wrong (see `resolve_project_root`):
///    it is how a packaged app is pointed at a checkout, and the only way to override 2.
/// 2. **The bundled engine**, when this build carries a whole one. A package always runs the
///    engine it was built with, so the shell and the engine it talks to come from one commit;
///    a checkout that happens to be the working directory does not replace it. A dev build has
///    no bundle, so for it nothing changes.
/// 3. **A checkout**, found as `resolve_project_root` finds one.
pub(crate) fn resolve_engine(
    explicit: Option<&str>,
    bundle: Option<&Path>,
    exe: Option<&Path>,
    cwd: &Path,
) -> EngineLaunch {
    if let Some(root) = explicit.map(str::trim).filter(|root| !root.is_empty()) {
        let root = PathBuf::from(root);
        return EngineLaunch {
            interpreter: engine_interpreter(&root),
            root,
            source: EngineSource::Explicit,
        };
    }
    if let Some(bundle) = bundle.filter(|dir| holds_bundle(dir)) {
        return EngineLaunch {
            root: bundle.to_path_buf(),
            interpreter: bundled_interpreter(bundle),
            source: EngineSource::Bundled,
        };
    }
    let root = resolve_project_root(None, exe, cwd);
    EngineLaunch {
        interpreter: engine_interpreter(&root),
        root,
        source: EngineSource::Checkout,
    }
}

/// The environment a launch sets and removes, beyond the PATH the launcher merges.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct EngineEnv {
    pub set: Vec<(&'static str, std::ffi::OsString)>,
    pub remove: Vec<&'static str>,
}

/// `PYTHONPATH` is the engine's root for every launch, as it always was. A bundled engine
/// also gets the isolation its read-only, self-contained interpreter needs:
///
/// * `PYTHONDONTWRITEBYTECODE` — the bundle is precompiled (`unchecked-hash`) and cannot be
///   written to, so an attempt to cache bytecode beside it is a failed write on every import;
/// * `PYTHONNOUSERSITE` — a package in `~/.local/lib/python3.x` must not shadow a wheel the
///   lock pinned, or the engine a person runs is not the one that was tested;
/// * `PYTHONHOME` removed — inherited from a shell that set it for another Python, it would
///   send this interpreter looking for its standard library in someone else's install.
///
/// A checkout's engine keeps the user's environment exactly as before: its interpreter is the
/// developer's own, and user site-packages are a developer's tool.
pub(crate) fn engine_env(launch: &EngineLaunch) -> EngineEnv {
    let mut set = vec![("PYTHONPATH", launch.root.clone().into_os_string())];
    let mut remove = Vec::new();
    if launch.source == EngineSource::Bundled {
        set.push(("PYTHONDONTWRITEBYTECODE", "1".into()));
        set.push(("PYTHONNOUSERSITE", "1".into()));
        remove.push("PYTHONHOME");
    }
    EngineEnv { set, remove }
}

/// The interpreter `python3 -m engine` should actually run under: the checkout's own
/// `.venv` when it has one, and the bare name `python3` otherwise.
///
/// The two are not interchangeable, and the difference stays invisible until
/// something optional is installed in one of them and not the other. `make test` runs
/// `.venv/bin/python`, but the app spawns whatever `python3` the login shell's PATH
/// resolves to, which for a window launched from a `.desktop` file is
/// `/usr/bin/python3`. Install the gate's Laya SDK into the venv — the documented
/// way, and the only one that leaves a system interpreter alone — and the app cannot
/// see it, so the gate quietly keeps paying an LLM call per goal for an SDK that is
/// installed and sitting in the same checkout.
///
/// A preference, not a requirement, and the fallback is the whole reason this is
/// safe: no `.venv`, or one without a `python3` in `bin`, resolves the name on PATH
/// exactly as before. That case is not hypothetical — the venv is the developer's
/// machine, not a property of the repository, and a fresh checkout still has to
/// start.
///
/// Only `bin/python3` is looked for, deliberately. `bin/python` is not a synonym
/// here, and on a checkout where the two disagree the one the venv documents is the
/// one to run.
pub(crate) fn engine_interpreter(project_root: &Path) -> PathBuf {
    let venv = project_root.join(".venv").join("bin").join("python3");
    if venv.is_file() {
        venv
    } else {
        PathBuf::from("python3")
    }
}

/// Parse the engine's boot line: `CODIFY_ENGINE token=<hex> port=<int>`.
///
/// `None` for anything else, including a line carrying the marker but no usable
/// port — the caller treats "not ready yet" and "never" the same way, and a handshake
/// read half-way must not park a token next to a missing port. Field order and extra
/// whitespace do not matter: this is the other process's stdout, and a stricter
/// parser would fail a boot over spacing.
pub(crate) fn parse_handshake(line: &str) -> Option<Handshake> {
    if !line.starts_with(HANDSHAKE_MARKER) {
        return None;
    }
    let mut token = None;
    let mut port = None;
    let mut hard_exit_s = None;
    for field in line.split_whitespace().skip(1) {
        if let Some(value) = field.strip_prefix("token=") {
            token = Some(value.to_string());
        } else if let Some(value) = field.strip_prefix("port=") {
            port = value.parse::<u16>().ok();
        } else if let Some(value) = field.strip_prefix(&format!("{HANDSHAKE_HARD_EXIT_FIELD}=")) {
            // Nonsense is `None` rather than a refusal: a bad bound must not stop
            // the app from booting. `NaN` and the infinities parse as floats and
            // are exactly the values that would turn the stop path's arithmetic
            // into a wait of no length or no end.
            hard_exit_s = value
                .parse::<f64>()
                .ok()
                .filter(|seconds| seconds.is_finite() && *seconds > 0.0);
        }
    }
    Some(Handshake {
        token: token?,
        port: port?,
        hard_exit_s,
    })
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
        "No engine to run at {} — a packaged Codify carries its own engine, and this build has \
         none, so it starts `python3 -m engine` from a project checkout: launch the app from \
         the Codify repository, from a binary built inside it, or with CODIFY_ROOT pointing at \
         it (and with the engine's dependencies installed).",
        root.display()
    ))
}

/// Why the engine is not running when it came up and then exited before reporting a
/// port.
///
/// The checkout was there and the spawn succeeded, so the failure is inside the engine
/// itself — a missing dependency, an unreadable config, a port it could not bind. Its
/// stderr is tailed and shown in the window (`engine_log.rs`), so the traceback is
/// already in front of the user; this says what that tail cannot — the command to run
/// for the whole of it, in the directory the shell actually ran from, not in the
/// abstract.
pub(crate) fn engine_exited_problem(launch: &EngineLaunch) -> String {
    let which = if launch.source == EngineSource::Bundled {
        "The engine this package carries"
    } else {
        "The engine"
    };
    format!(
        "{which} started but exited before it reported a port. What it printed is \
         below; run `{} -m engine` in {} to see the whole of it.",
        launch.interpreter.display(),
        launch.root.display()
    )
}

/// What the shell does when an engine that had started has exited: start another, or stop and say why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum AfterExit {
    /// Start a fresh engine. `counted` is false for a restart a person asked for, which the budget does not charge.
    Restart { counted: bool },
    /// The app is closing: the exit was this shell's own doing, and nothing is started.
    ShuttingDown,
    /// The engine has exited too often, too fast; the sentence the window shows instead of starting it again.
    GaveUp(String),
}

/// How many unasked-for restarts a supervisor allows inside its window before it stops and says so.
///
/// An engine that crashes once (a provider library that segfaulted, an out-of-memory kill) is worth starting again
/// without asking anybody: the window shows Offline for a few seconds and then a healthy engine on a new port. One that
/// crashes on every boot is not, and restarting it for ever would hide the problem behind a pill that flickers.
#[derive(Debug, Clone)]
pub(crate) struct RestartBudget {
    max: usize,
    window: std::time::Duration,
    recent: std::collections::VecDeque<std::time::Instant>,
}

impl RestartBudget {
    pub(crate) fn new(max: usize, window: std::time::Duration) -> Self {
        Self {
            max,
            window,
            recent: std::collections::VecDeque::new(),
        }
    }

    /// Spend one restart at `now`, or refuse when `max` have already been spent inside the window.
    fn spend(&mut self, now: std::time::Instant) -> bool {
        while self
            .recent
            .front()
            .is_some_and(|at| now.duration_since(*at) >= self.window)
        {
            self.recent.pop_front();
        }
        if self.recent.len() >= self.max {
            return false;
        }
        self.recent.push_back(now);
        true
    }

    /// Decide what follows an exit. `requested` is a person's Restart; `shutting_down` is the app closing.
    pub(crate) fn after_exit(
        &mut self,
        shutting_down: bool,
        requested: bool,
        now: std::time::Instant,
    ) -> AfterExit {
        if shutting_down {
            return AfterExit::ShuttingDown;
        }
        if requested {
            // A person asked, so whatever was failing has had someone look at it: the budget starts over.
            self.recent.clear();
            return AfterExit::Restart { counted: false };
        }
        if self.spend(now) {
            return AfterExit::Restart { counted: true };
        }
        AfterExit::GaveUp(format!(
            "The engine stopped {} times within {} minutes, so it was not started again. What it printed last is \
             below; press Restart engine once the cause is fixed.",
            self.max + 1,
            self.window.as_secs() / 60
        ))
    }
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
            Some(Handshake {
                token: "9f2c".to_string(),
                port: 7430,
                hard_exit_s: None
            }),
            parse_handshake("CODIFY_ENGINE token=9f2c port=7430")
        );
    }

    #[test]
    fn field_order_and_extra_whitespace_do_not_matter() {
        assert_eq!(
            Some(Handshake {
                token: "9f2c".to_string(),
                port: 7431,
                hard_exit_s: None
            }),
            parse_handshake("CODIFY_ENGINE   port=7431  token=9f2c  ")
        );
    }

    #[test]
    fn the_engines_own_stop_bound_is_read_when_it_announces_one() {
        // The shell waits this long before escalating to SIGKILL, so it is read
        // from the engine rather than written down twice.
        let handshake =
            parse_handshake("CODIFY_ENGINE token=9f2c port=7430 hard_exit_s=6").expect("handshake");
        assert_eq!(Some(6.0), handshake.hard_exit_s);
    }

    #[test]
    fn a_bound_that_could_not_be_waited_on_is_ignored_rather_than_obeyed() {
        // Every one of these parses as a float except the last, and every one of
        // them is worse than no answer: `NaN` and the infinities make the stop
        // path's arithmetic meaningless, and a zero or negative bound would have
        // the shell kill an engine before it had been asked politely.
        for announced in ["0", "-3", "nan", "inf", "-inf", "soon"] {
            let handshake = parse_handshake(&format!(
                "CODIFY_ENGINE token=9f2c port=7430 hard_exit_s={announced}"
            ))
            .expect("the token and port still parse");
            assert_eq!(
                None, handshake.hard_exit_s,
                "must not be believed: {announced}"
            );
        }
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

    fn checkout(name: &str) -> PathBuf {
        let dir = scratch_dir(name);
        let engine = dir.join("engine");
        std::fs::create_dir_all(&engine).expect("engine directory");
        std::fs::write(engine.join("__main__.py"), "").expect("engine entry point");
        dir
    }

    #[test]
    fn a_launch_from_the_home_directory_finds_the_checkout_the_binary_was_built_in() {
        let root = checkout("exe-ancestor");
        let exe = root.join("src-tauri/target/release/codify");
        let home = scratch_dir("exe-ancestor-home");

        assert_eq!(root, resolve_project_root(None, Some(&exe), &home));
    }

    #[test]
    fn the_working_directory_wins_when_it_holds_the_engine() {
        // Running a second checkout from inside it must run that checkout, not the one the
        // binary happens to sit in.
        let here = checkout("cwd-wins-here");
        let other = checkout("cwd-wins-other");
        let exe = other.join("src-tauri/target/debug/codify");

        assert_eq!(here, resolve_project_root(None, Some(&exe), &here));
        assert_eq!(
            here,
            resolve_project_root(None, Some(&exe), &here.join("src-tauri"))
        );
    }

    #[test]
    fn codify_root_is_taken_as_given_even_when_it_is_wrong() {
        let real = checkout("explicit-real");
        let exe = real.join("src-tauri/target/debug/codify");
        let mistyped = "/definitely/not/a/checkout";

        let chosen = resolve_project_root(Some(mistyped), Some(&exe), &real);

        assert_eq!(
            PathBuf::from(mistyped),
            chosen,
            "a wrong CODIFY_ROOT was silently replaced"
        );
        assert!(launch_problem(&chosen).is_some());
    }

    #[test]
    fn an_empty_codify_root_is_not_a_choice() {
        let root = checkout("explicit-empty");
        assert_eq!(root, resolve_project_root(Some("  "), None, &root));
    }

    #[test]
    fn with_no_checkout_anywhere_the_working_directory_is_named_in_the_failure() {
        let nowhere = scratch_dir("nowhere");
        let exe = nowhere.join("bin/codify");

        let chosen = resolve_project_root(None, Some(&exe), &nowhere);

        assert_eq!(nowhere, chosen);
        assert!(launch_problem(&chosen)
            .expect("no engine here")
            .contains("CODIFY_ROOT"));
    }

    #[test]
    fn the_projects_own_virtualenv_is_the_interpreter_when_it_has_one() {
        // The whole point: the SDK is installed here, and a shell-resolved
        // `python3` cannot see it, so the gate would stay on its LLM fallback.
        let dir = scratch_dir("venv");
        let interpreter = dir.join(".venv").join("bin").join("python3");
        std::fs::create_dir_all(interpreter.parent().unwrap()).expect("venv bin");
        std::fs::write(&interpreter, "#!/bin/sh\n").expect("interpreter");
        assert_eq!(interpreter, engine_interpreter(&dir));
    }

    #[test]
    fn a_checkout_without_one_still_resolves_the_name_on_path() {
        // Not a hypothetical: the venv belongs to the developer's machine, not to
        // the repository, so a fresh clone has to start exactly as it did before.
        let dir = scratch_dir("no-venv");
        assert_eq!(PathBuf::from("python3"), engine_interpreter(&dir));

        // And a `.venv` with no interpreter in it is no more usable than none.
        std::fs::create_dir_all(dir.join(".venv").join("bin")).expect("venv bin");
        assert_eq!(PathBuf::from("python3"), engine_interpreter(&dir));
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
        let launch = EngineLaunch {
            root: PathBuf::from("/home/me/Codify"),
            interpreter: PathBuf::from("python3"),
            source: EngineSource::Checkout,
        };
        let problem = engine_exited_problem(&launch);
        // The user has to be able to copy the directory and the command out of the
        // message; a generic "the engine exited" would leave them with no next step.
        assert!(
            problem.contains("/home/me/Codify") && problem.contains("python3 -m engine"),
            "the message has to name both the directory and the command: {problem}"
        );
    }

    #[test]
    fn a_bundled_engine_that_exited_names_its_own_interpreter() {
        // `python3 -m engine` in /usr/lib/Codify/engine-bundle would run the PATH's Python, which
        // has none of the bundle's packages: the command to reproduce it has to be the bundle's.
        let bundle = PathBuf::from("/usr/lib/Codify/engine-bundle");
        let launch = EngineLaunch {
            interpreter: bundled_interpreter(&bundle),
            root: bundle,
            source: EngineSource::Bundled,
        };
        let problem = engine_exited_problem(&launch);
        assert!(
            problem.starts_with("The engine this package carries"),
            "{problem}"
        );
        assert!(
            problem.contains(
                "/usr/lib/Codify/engine-bundle/python/bin/python3 -m engine` in /usr/lib/Codify/engine-bundle"
            ),
            "{problem}"
        );
    }

    fn bundle(name: &str) -> PathBuf {
        let dir = checkout(name);
        let bin = dir.join("python").join("bin");
        std::fs::create_dir_all(&bin).expect("bundle interpreter directory");
        std::fs::write(bin.join("python3"), "").expect("bundle interpreter");
        dir
    }

    #[test]
    fn a_package_runs_the_engine_it_carries_with_its_own_interpreter() {
        let carried = bundle("bundled-carried");
        let home = scratch_dir("bundled-home");
        let launch = resolve_engine(
            None,
            Some(&carried),
            Some(Path::new("/usr/bin/codify-desktop")),
            &home,
        );
        assert_eq!(launch.source, EngineSource::Bundled);
        assert_eq!(launch.root, carried);
        assert_eq!(launch.interpreter, carried.join("python/bin/python3"));
    }

    #[test]
    fn a_checkout_in_the_working_directory_does_not_replace_the_carried_engine() {
        // Started from a terminal that happens to sit in a checkout, an installed app still talks
        // to the engine it was built with — not to whatever commit that checkout is on.
        let carried = bundle("bundled-over-cwd");
        let here = checkout("bundled-cwd-checkout");
        let launch = resolve_engine(None, Some(&carried), None, &here);
        assert_eq!(launch.source, EngineSource::Bundled);
        assert_eq!(launch.root, carried);
    }

    #[test]
    fn codify_root_still_points_a_package_at_a_checkout() {
        let carried = bundle("bundled-under-root");
        let theirs = checkout("bundled-codify-root");
        let launch = resolve_engine(theirs.to_str(), Some(&carried), None, &carried);
        assert_eq!(launch.source, EngineSource::Explicit);
        assert_eq!(launch.root, theirs);
        assert_eq!(launch.interpreter, PathBuf::from("python3"));
    }

    #[test]
    fn half_a_bundle_is_not_a_bundle() {
        // The engine without its interpreter: falls through to the checkout rules, which name the
        // working directory, instead of running the bundle's code under the PATH's Python.
        let broken = checkout("bundled-half");
        let home = scratch_dir("bundled-half-home");
        let launch = resolve_engine(None, Some(&broken), None, &home);
        assert_eq!(launch.source, EngineSource::Checkout);
        assert_eq!(launch.root, home);
        assert!(launch_problem(&launch.root).is_some());
    }

    #[test]
    fn with_no_bundle_the_checkout_order_is_unchanged() {
        let root = checkout("no-bundle-exe");
        let exe = root.join("src-tauri/target/release/codify");
        let home = scratch_dir("no-bundle-home");
        let missing = scratch_dir("no-bundle-resources").join(BUNDLE_DIR);
        for bundle in [None, Some(missing.as_path())] {
            let launch = resolve_engine(None, bundle, Some(&exe), &home);
            assert_eq!(launch.source, EngineSource::Checkout);
            assert_eq!(launch.root, resolve_project_root(None, Some(&exe), &home));
        }
    }

    #[test]
    fn a_bundled_engine_is_isolated_and_a_checkout_keeps_its_environment() {
        let carried = PathBuf::from("/usr/lib/Codify/engine-bundle");
        let bundled = engine_env(&EngineLaunch {
            interpreter: bundled_interpreter(&carried),
            root: carried.clone(),
            source: EngineSource::Bundled,
        });
        assert!(bundled
            .set
            .contains(&("PYTHONPATH", carried.clone().into_os_string())));
        assert!(bundled
            .set
            .contains(&("PYTHONDONTWRITEBYTECODE", "1".into())));
        assert!(bundled.set.contains(&("PYTHONNOUSERSITE", "1".into())));
        assert_eq!(bundled.remove, vec!["PYTHONHOME"]);

        let root = PathBuf::from("/home/me/Codify");
        let checkout = engine_env(&EngineLaunch {
            interpreter: PathBuf::from("python3"),
            root: root.clone(),
            source: EngineSource::Checkout,
        });
        assert_eq!(checkout.set, vec![("PYTHONPATH", root.into_os_string())]);
        assert!(checkout.remove.is_empty());
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

    fn minutes(n: u64) -> std::time::Duration {
        std::time::Duration::from_secs(60 * n)
    }

    #[test]
    fn an_engine_that_crashes_is_started_again_up_to_the_budget_and_then_named() {
        let start = std::time::Instant::now();
        let mut budget = RestartBudget::new(3, minutes(5));
        for n in 0..3 {
            assert_eq!(
                budget.after_exit(false, false, start + std::time::Duration::from_secs(n)),
                AfterExit::Restart { counted: true },
                "crash {n} inside the budget is restarted without asking anybody"
            );
        }
        match budget.after_exit(false, false, start + std::time::Duration::from_secs(4)) {
            AfterExit::GaveUp(why) => {
                assert!(
                    why.contains("4 times"),
                    "the sentence counts the exits: {why}"
                );
                assert!(
                    why.contains("Restart engine"),
                    "and names the way out: {why}"
                );
            }
            other => panic!("a fourth crash in five minutes was restarted again: {other:?}"),
        }
    }

    #[test]
    fn crashes_spread_out_past_the_window_never_run_the_budget_out() {
        let start = std::time::Instant::now();
        let mut budget = RestartBudget::new(3, minutes(5));
        for n in 0..10u64 {
            assert_eq!(
                budget.after_exit(false, false, start + minutes(6 * n)),
                AfterExit::Restart { counted: true },
                "one crash every six minutes is ten separate accidents, not a loop"
            );
        }
    }

    #[test]
    fn a_person_asking_is_never_refused_and_starts_the_budget_over() {
        let start = std::time::Instant::now();
        let mut budget = RestartBudget::new(1, minutes(5));
        assert_eq!(
            budget.after_exit(false, false, start),
            AfterExit::Restart { counted: true }
        );
        assert!(matches!(
            budget.after_exit(false, false, start),
            AfterExit::GaveUp(_)
        ));
        assert_eq!(
            budget.after_exit(false, true, start),
            AfterExit::Restart { counted: false },
            "the Restart button works when the budget is spent"
        );
        assert_eq!(
            budget.after_exit(false, false, start),
            AfterExit::Restart { counted: true },
            "and a crash after it has the budget back"
        );
    }

    #[test]
    fn nothing_is_started_while_the_app_is_closing() {
        let start = std::time::Instant::now();
        let mut budget = RestartBudget::new(3, minutes(5));
        assert_eq!(
            budget.after_exit(true, false, start),
            AfterExit::ShuttingDown
        );
        assert_eq!(
            budget.after_exit(true, true, start),
            AfterExit::ShuttingDown,
            "not even when a Restart was pressed on the way out"
        );
    }
}
