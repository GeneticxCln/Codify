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
"""

from __future__ import annotations

import ast
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
        "run": "the git choke point: every command through _run_bytes, behind guarded_argv/guarded_env, pinned by tests/test_git.py",
    },
    "engine/app.py": {
        "run": "the folder picker: _picker_command's guarded_argv/guarded_env + start_new_session, pinned by tests/test_sandbox.py and the live-engine e2e",
    },
    "benchmarks/runner.py": {
        "Popen": "a task's test command: manifest-owned argv, guarded_argv/guarded_env + start_new_session + a whole-group kill on timeout, pinned by tests/test_benchmark_runner.py. Deliberately NOT routed through SandboxService: that allowlist is a security boundary for model-proposed argv, and widening it for a reviewed manifest would weaken it for every agent in the pipeline",
    },
}

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


if __name__ == "__main__":
    unittest.main()
