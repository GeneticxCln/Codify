"""No engine module starts a process anywhere but the guarded spawn sites.

The dynamic tests (`test_sandbox.py`, `test_git.py`, `test_sandbox_orphans_e2e.py`)
prove that every guarded spawn dies with the engine. This module proves something
cheaper and broader: that the list of places a process *can* start is still the
list those tests cover. `subprocess.Popen` in a fresh helper — a one-line
convenience, exactly how the next unguarded spawn arrives — would be invisible to
every dynamic test, because none of them greps for new spawns. This scan does:

* every `engine/**/*.py`, `benchmarks/**/*.py` and `scripts/**/*.py` is parsed (no
  import, no execution), and every call that resolves to the `subprocess` module — attribute form (`subprocess.run`), aliased
  imports (`import subprocess as sp`), or `from subprocess import run` — is a
  finding unless its (file, call) site is allowlisted below with a justification;
* the same walk flags the process-starting members of `os` (`system`, `popen`,
  `spawn*`, `exec*`, `posix_spawn*`) and of asyncio (`create_subprocess_exec`,
  `create_subprocess_shell`), which bypass a "no subprocess" rule by spelling;

The allowlist is the freeze. Each entry is a deliberate, guarded spawn — the
guard's own Popen, and the choke points the dynamic tests pin — and each carries
its justification in the same table, so the review conversation for a future
spawn is written down where the change happens: route it through an existing
choke point, or justify a new one here and give it a dynamic test. An entry no
longer backed by code is reported as stale, so the table cannot quietly outlive
the spawns it names.

`benchmarks` and `scripts` are scanned for the same reason: a benchmark that
shells out unguarded leaves a hung test suite writing into a scratch tree, and
it lives outside `engine/` only by accident of layout. A freeze scoped to one
directory is a freeze the next directory walks around.

A static scan cannot prove semantics; it freezes decision sites. That is the
point: a spawn that cannot appear unannounced cannot silently regress to
unguarded.

`src-tauri/` is scanned too, by a second lexical half. The desktop shell starts
processes — it spawns and kills `python3 -m engine` — and a freeze scoped to the
engine's own languages is a freeze the shell walks around. That half cannot use
an AST (Rust has none here), so it strips comments and matches the calls a reader
would recognise; the same honesty applies, and the weakness is stated rather than
hidden. It is the test that keeps a terminal from appearing without a decision
behind it.
"""

from __future__ import annotations

import ast
import re
import unittest
from collections.abc import Iterator
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Every (file, call) site in engine/ that may start a process, with the reason it
# is allowed to exist. Files are repo-relative; calls are the exact subprocess
# member invoked (`run`, `Popen`, …). Everything not named here is a failure.
GUARDED_SPAWN_SITES: dict[str, dict[str, str]] = {
    "engine/spawn_guard.py": {
        "Popen": "the guard itself: the one process the engine spawns to lead a command's group and kill it when the engine dies",
    },
    "engine/sandbox.py": {
        "Popen": "the model-driven command: guarded_argv/guarded_env + start_new_session, pinned by tests/test_sandbox.py",
    },
    "engine/git.py": {
        "Popen": "the git choke point: every command through _run_bytes, behind guarded_argv/guarded_env, a session of its own and a whole-group kill on timeout (the conductor's model-chosen reads are bounded), pinned by tests/test_git.py and tests/test_sandbox_read_only_git.py",
    },
    "engine/library.py": {
        "Popen": "the regex search worker (engine/regex_worker.py): guarded_argv/guarded_env + start_new_session, killed with SIGKILL at a hard limit. CPython's `re` cannot be interrupted and holds the GIL, so a model-supplied pattern can only be bounded by a process the engine can kill; the argv is a fixed script path, the pattern travels as JSON on stdin, pinned by tests/test_library_regex_bound.py",
    },
    "engine/app.py": {
        "run": "the folder picker: _picker_command's guarded_argv/guarded_env + start_new_session, pinned by tests/test_sandbox.py and the live-engine e2e",
    },
    "benchmarks/runner.py": {
        "Popen": "a task's test command: manifest-owned argv, guarded_argv/guarded_env + start_new_session + a whole-group kill on timeout, pinned by tests/test_benchmark_runner.py. Deliberately NOT routed through SandboxService: that allowlist is a security boundary for model-proposed argv, and widening it for a reviewed manifest would weaken it for every agent in the pipeline",
    },
    "scripts/check_history.py": {
        "run": "the per-commit build check: git worktree/rev-list/diff and the gate's own leg binaries, each an argv list with capture_output and no shell. Deliberately NOT routed through SandboxService: that allowlist is a security boundary for MODEL-proposed argv, and this script is run by a developer, never by an agent. Its argv is assembled here from a fixed table and a git range the developer typed — there is no request to validate, and validate_argv would reject `node --experimental-strip-types --test tests/*.test.ts` because of the glob, which is the check's whole point. The spawned process is the gate's own toolchain, it runs in a throwaway worktree outside the repository, and it is torn down on every path out including SIGINT (see the atexit/signal handling). Pinned by tests/test_check_history.py",
    },
    "scripts/run_tests.py": {
        "run": "the suite runner `make test-engine` goes through, which starts `sys.executable -m unittest` with the caller's arguments appended. A fixed argv prefix, no shell, and the arguments are a developer's or CI's rather than anything a model produced. Not routed through SandboxService for the reason check_history.py gives, and with an extra one: the thing being spawned IS the thing that spawns everything else, so a guard around it would be a guard around the guard. Its whole job is to notice that the suite died part-way and say so, which it cannot do from inside the process it is watching. Pinned by tests/test_run_tests_guard.py",
    },
    "scripts/preview_engine.py": {
        "Popen": "the preview launcher `make run-engine-preview` goes through: it starts `sys.executable -m engine` with the developer's arguments appended to a fixed argv prefix, no shell, and the environment passed through untouched so CODIFY_HOME-style isolation composes. Not routed through SandboxService or spawn_guard for the reason the other scripts give — the argv is the engine's own entry point chosen by a developer, there is no model-proposed request to validate — and with one more: this script must be the engine's *parent*, because the engine's parent watchdog (`CODIFY_PARENT_PID` from src-tauri's own boot) makes the spawning process the engine's lease on life, and `start_new_session=True` would orphan the engine from the script's exit handling rather than bind the two. Pinned by tests/test_preview_engine.py, which boots the real engine end to end and stops it again.",
    },
    "scripts/tabs_smoke.py": {
        "Popen": "the tab-restoration smoke (`make smoke-tabs`): launches the shell binary built from this checkout (`src-tauri/target/debug/codify-desktop`) with `CODEIFY_TABS_SMOKE=1` and a throwaway `CODIFY_HOME`, and nothing else on the argv — the shell's own mode (frozen by lib.rs's `the_tabs_smoke_is_gated_on_its_env_var_alone` and its seed tests), which boots the engine as an ordinary launch does, seeds `PUT /shell/tabs` with one row, and exits when the UI seats a webview for the seed key — a key the UI cannot mint, so the seat is the whole chain: engine row → pull → adopt → restore → seat. Not routed through SandboxService or spawn_guard: there is no model-proposed request to validate — the argv is a path this script computes from its own location. The environment passes through untouched (session bus, WAYLAND_DISPLAY/DISPLAY, DMABUF override) except for the two facts that make the run safe and honest: the throwaway CODIFY_HOME (a smoke that writes rows into the developer's real strip is a smoke that vandalises it) and the mode variable itself. The child inherits this script's foreground session so Ctrl-C stops both; the script's own SIGINT/SIGTERM handler and atexit backstop terminate it on every other path out. Deliberately not part of `check`/`ci`: it needs a real display and a real engine boot, which no CI leg here has.",
        "run": "the `cargo build` that produces the binary above when it is missing or `--rebuild` is passed: fixed argv `[cargo, build]`, cwd src-tauri, output inherited, no shell. Same authority as embed_smoke.py's build — a developer harness building its own subject before running it.",
    },
    "scripts/embed_smoke.py": {
        "Popen": "the embedded-browser first-paint smoke (`make smoke-embed`): launches the shell binary built from this checkout (`src-tauri/target/debug/codify-desktop`) with `CODEIFY_EMBED_SMOKE=<url>` and nothing else on the argv — the shell's own smoke mode, which seats one webview, never starts an engine (frozen by browser.rs's `the_smoke_mode_is_gated_reports_and_never_starts_the_engine`), and exits when all three of its deliverables are in — the paint line, the page's own report of what it is showing, and that page read back through the `codify-bridge` scheme the AI uses (frozen by browser.rs's `the_smoke_run_waits_for_a_paint_and_a_report` and webview_bridge.rs's `the_smoke_reader_and_this_module_agree_on_the_line`) — or on its own 20s timeout, with this script killing it past `--timeout` as backstop. Not routed through SandboxService or spawn_guard: there is no model-proposed request to validate — the argv is a path this script computes from its own location — and the environment MUST pass through untouched (session bus, WAYLAND_DISPLAY/DISPLAY, any user-set DMABUF override), because a sanitised environment would diagnose a machine nobody is on; that is the entire point of a first-paint smoke. The one environment addition beyond the gate itself is the bridge opt-out (`CODEIFY_EMBED_SMOKE_NO_BRIDGE`, set by `--no-bridge`), which is opt-out precisely so the default run measures the feature. The child inherits this script's foreground session rather than a new one, same as preview_engine, so Ctrl-C stops both. Deliberately not part of `check`/`ci`: it needs a real display, which no CI leg here has.",
        "run": "the `cargo build` that produces the binary above when it is missing or `--rebuild` is passed: fixed argv `[cargo, build]`, cwd src-tauri, output inherited, no shell. Same authority as the other scripts' toolchain spawns — a developer harness building its own subject before running it.",
    },
}

# The Rust half of the freeze, for `src-tauri/`.
#
# Why a second table and a second scan rather than folding Rust into the Python
# one: there is no Python AST for Rust, so this half is lexical — comments are
# stripped and each pattern names a call that can start a process or open a pty.
# That is weaker than the AST walk above and says so. A static scan cannot prove
# semantics on either side; it freezes decision sites. What matters is that a
# process cannot appear unannounced in a file the engine's dynamic tests never
# touch.
#
# This directory is scanned at all because it starts processes and always did:
# the desktop shell spawns `python3 -m engine` and kills it on exit. A freeze
# scoped to `engine/` is a freeze `src-tauri/` walks around.
RUST_SPAWN_SCAN_ROOTS = ("src-tauri/src",)

RUST_PROCESS_START_PATTERNS: dict[str, re.Pattern[str]] = {
    "Command::new": re.compile(r"\bCommand::new\s*\("),
    "CommandBuilder::new": re.compile(r"\bCommandBuilder::new\s*\("),
    ".spawn()": re.compile(r"\.spawn\s*\("),
    ".spawn_unchecked()": re.compile(r"\.spawn_unchecked\s*\("),
    "spawn_command()": re.compile(r"\.spawn_command\s*\("),
    ".output()": re.compile(r"\.output\s*\("),
    # Opening a pty is not a process, but it is the choke point every PTY spawn
    # has to pass through. Freezing it is how "a terminal was opened" stays a
    # decision someone made.
    "native_pty_system()": re.compile(r"\bnative_pty_system\s*\("),
    "openpty()": re.compile(r"\bopenpty\s*\("),
}

# Deliberately absent: browser webview creation (`WebviewWindowBuilder::build`
# in src-tauri/src/browser.rs) is not a process start. A webview is a window
# the shell renders in its own process — no child, no pty, nothing to reap on
# exit — so freezing it here would claim a process boundary it does not have.
# What a webview does need is isolation from *invoke*, and that is asserted
# where the decision lives: browser.rs's capability-set test parses
# src-tauri/capabilities/*.json and fails if any permission reaches a
# `browser-*` label.

RUST_GUARDED_SPAWN_SITES: dict[str, dict[str, str]] = {
    "src-tauri/src/lib.rs": {
        "Command::new": "the engine launcher and the login-shell PATH probe: the shell owns the engine's process lifecycle, and both of these are the shell's own children, killed on exit by the RunEvent::Exit handler",
        ".spawn()": "the engine child process: spawned into its own process group so the exit handler can kill it, and killed unconditionally when the app goes — a silently-skipped kill leaks a stray engine holding the port and the DB",
        ".output()": "the login shell's own output, to inherit the user's PATH rather than guessing one: short-lived, bounded by a timeout, and it changes nothing on disk",
    },
    "src-tauri/src/terminal.rs": {
        "CommandBuilder::new": "the user's own shell for a terminal pane. Deliberately NOT routed through the engine's SandboxService: that is the agent's privileged path (docs/00 §6.6, only verifier-proposed argv reaches it) and a user typing at a prompt is a different authority. argv comes from $SHELL, never from a request; cwd is pinned by pin_cwd to a registered workspace root",
        "native_pty_system()": "the pty a terminal pane runs in: the choke point every PTY spawn passes through, and the reason a terminal is a terminal rather than a pipe",
        "openpty()": "the pty pair for a pane, including the one the close/reap test opens without spawning anything",
        "spawn_command()": "the shell behind a pane: spawned through the pty above with cwd already pinned, and killed by codify_terminal_close or by the app's exit handler so a closed window does not leave a shell holding the workspace",
    },
}

# A line whose stripped form starts with one of these is not code. Doc comments
# and examples inside them are the most likely source of a false positive in a
# lexical scan, and a scanner that cries wolf stops being read.
RUST_COMMENT_PREFIXES = ("//",)

# The subprocess members that actually start a process. The module also carries
# pure helpers (`CompletedProcess`, `list2cmdline`) that must NOT be frozen — a
# scanner that flags those stops being read the first week.
SUBPROCESS_SPAWN_MEMBERS = {
    "run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput",
}

# Process-starting calls that never spell "subprocess": the os family and the
# asyncio subprocess creators. Keys are dotted call heads; bare names imported
# from those modules (e.g. `from os import system`) are resolved to the same
# dotted form before comparison.
DIRECT_OS_SPAWN_CALLS = {
    "os.system", "os.popen",
    "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe",
    "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe",
    "os.posix_spawn", "os.posix_spawnp",
    "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "os.execl", "os.execle", "os.execlp", "os.execlpe",
    "os.fork", "os.forkpty",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
}


# Every package whose Python is reviewed alongside the engine. `benchmarks` and
# `scripts` are here because they start processes, not because they are adjacent
# to one that does.
SPAWN_SCAN_ROOTS = ("engine", "benchmarks", "scripts")


def _iter_source_files() -> Iterator[Path]:
    for root in SPAWN_SCAN_ROOTS:
        for path in sorted((PROJECT_ROOT / root).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _spawn_findings(tree: ast.AST, rel: str) -> list[tuple[int, str]]:
    """(line, dotted call) for every process-starting call in one module.

    Both spellings count: `subprocess.run(...)` (any alias of the module) and
    `run(...)` from `from subprocess import run`. Only members that actually start
    a process are findings — `subprocess.CompletedProcess(...)` builds a result
    object and starts nothing.
    """
    module_aliases: dict[str, str] = {}
    name_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"subprocess", "os", "asyncio"}:
                    module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in {
            "subprocess", "os", "asyncio"
        }:
            if any(alias.name == "*" for alias in node.names):
                raise AssertionError(
                    f"{rel}:{node.lineno} star-imports {node.module} — spell the module "
                    "so spawns stay findable"
                )
            for alias in node.names:
                name_aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted: str | None = None
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            module = module_aliases.get(node.func.value.id)
            if module is not None:
                dotted = f"{module}.{node.func.attr}"
        elif isinstance(node.func, ast.Name):
            dotted = name_aliases.get(node.func.id)
        if dotted is None:
            continue
        module, _, member = dotted.partition(".")
        if module == "subprocess" and member in SUBPROCESS_SPAWN_MEMBERS:
            findings.append((node.lineno, dotted))
        elif dotted in DIRECT_OS_SPAWN_CALLS:
            findings.append((node.lineno, dotted))
    return findings


def _iter_rust_files() -> Iterator[Path]:
    for root in RUST_SPAWN_SCAN_ROOTS:
        for path in sorted((PROJECT_ROOT / root).rglob("*.rs")):
            # `target/` is build output, not source; it can live under the root
            # after a workspace build and is nobody's decision site.
            if "target" not in path.parts:
                yield path


def _rust_spawn_findings(text: str) -> list[tuple[int, str]]:
    """(line, call) for every process- or pty-starting call in one Rust file.

    Lexical rather than parsed. The patterns are named by the call a reader would
    recognise — `Command::new`, `.spawn()`, `spawn_command()` — so the freeze is
    readable from the table rather than from a regex.

    Comments are stripped first: a doc comment showing a spawn is prose, and
    flagging prose is how a scanner gets ignored. A spawn hidden inside a macro is
    beyond what a lexical scan sees; the same honesty as the Python half, which
    also freezes decision sites rather than proving semantics.
    """
    findings: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith(RUST_COMMENT_PREFIXES):
            continue
        for call, pattern in RUST_PROCESS_START_PATTERNS.items():
            if pattern.search(raw):
                findings.append((lineno, call))
    return findings


class NoUnguardedSpawns(unittest.TestCase):
    """The freeze: every spawn site in the reviewed packages is justified."""

    def test_every_spawn_is_a_guarded_allowlisted_site(self) -> None:
        unexpected: list[str] = []
        for path in _iter_source_files():
            rel = str(path.relative_to(PROJECT_ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            for line, dotted in _spawn_findings(tree, rel):
                call = dotted.rpartition(".")[2]
                if call not in GUARDED_SPAWN_SITES.get(rel, {}):
                    unexpected.append(
                        f"{rel}:{line} `{dotted}(...)` — not an allowlisted guarded spawn"
                    )
        self.assertEqual(
            [], unexpected,
            "\nA process can start outside the guarded spawn sites. Route it through an"
            "\nexisting choke point (engine/sandbox.py, engine/git.py, the picker in"
            "\nengine/app.py — all via engine/spawn_guard.py's guarded_argv/guarded_env"
            "\nwith start_new_session=True), or, if it is the next deliberate guarded"
            "\nspawn, add its (file, call) site to GUARDED_SPAWN_SITES with a"
            "\njustification and give it a dynamic orphan test.",
        )

    def test_the_allowlist_never_outlives_the_spawns_it_names(self) -> None:
        stale: list[str] = []
        for rel, calls in GUARDED_SPAWN_SITES.items():
            path = PROJECT_ROOT / rel
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            present = {dotted for _, dotted in _spawn_findings(tree, rel)}
            for call in calls:
                if f"subprocess.{call}" not in present:
                    stale.append(
                        f"{rel}: allowlisted `{call}` starts no process any more — "
                        "drop the entry or restore the guarded spawn"
                    )
        self.assertEqual([], stale, "\nThe allowlist must name only live spawn sites.")

    def test_every_rust_spawn_is_an_allowlisted_site(self) -> None:
        """The same freeze over `src-tauri/`.

        A terminal is a shell, so this is the test that keeps one from appearing
        without a decision behind it. The desktop shell starts processes already
        — it spawns and kills the engine — and a pane that shelled out on its own
        would be invisible to every Python test in this repository.
        """
        unexpected: list[str] = []
        for path in _iter_rust_files():
            rel = str(path.relative_to(PROJECT_ROOT))
            for line, call in _rust_spawn_findings(path.read_text(encoding="utf-8")):
                if call not in RUST_GUARDED_SPAWN_SITES.get(rel, {}):
                    unexpected.append(
                        f"{rel}:{line} `{call}` — not an allowlisted guarded spawn"
                    )
        self.assertEqual(
            [],
            unexpected,
            "\nA process can start in src-tauri/ outside the guarded spawn sites. Route it"
            "\nthrough an existing choke point, or justify a new entry in"
            "\nRUST_GUARDED_SPAWN_SITES with the reason it is safe and a test that pins it."
            "\nNote that the engine's SandboxService is not an option here: it is the agent's"
            "\nprivileged path (docs/00 §6.6) and a user-driven process is a different"
            "\nauthority — see terminal.rs's module docs.",
        )

    def test_the_rust_allowlist_never_outlives_the_spawns_it_names(self) -> None:
        stale: list[str] = []
        for rel, calls in RUST_GUARDED_SPAWN_SITES.items():
            path = PROJECT_ROOT / rel
            present = {call for _, call in _rust_spawn_findings(path.read_text(encoding="utf-8"))}
            for call in calls:
                if call not in present:
                    stale.append(
                        f"{rel}: allowlisted `{call}` starts no process any more — "
                        "drop the entry or restore the guarded spawn"
                    )
        self.assertEqual([], stale, "\nThe allowlist must name only live spawn sites.")


if __name__ == "__main__":
    unittest.main()
