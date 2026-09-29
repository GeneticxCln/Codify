"""Check that every commit in a range builds, not just the one at the tip.

`make ci` answers one question: is the tree you are about to share green? It
cannot answer a different one — whether each commit *on the way* was green.
Those are not the same property, and the difference is invisible until someone
runs `git bisect`.

The gap is not hypothetical in this repository. Its UI tests are mostly
contract tests that read a source file's text rather than a module's
behaviour, so a commit that adds a component and a test asserting the shell
mounts it fails until the commit that mounts it. Split one coherent change
into "the parts" and "the wiring", and every commit in between is red — which
is exactly what happened here: seven commits, five of which did not build, and
a history that reads beautifully and cannot be bisected.

So this walks a range and runs the decisive legs at each commit. It is not a
second gate. `make ci` is the gate and stays the gate, on the tip, on the
whole 3.10 + host + UI + Rust surface. This asks a different question of
different commits, which is why it is a separate target, is not part of
`check` or `ci`, and runs at pre-push only when the push carries more than one
new commit — the case where a red middle is actually possible. A one-commit
push is `make ci`'s problem, and the tool says so rather than passing quietly.

Deliberately *not* run per commit, because they cannot be the thing that
decides, and the cost is the reason people learn to bypass a gate:

* the declared-minimum (3.10) leg — it provisions an interpreter, and doing
  that once per commit is minutes per commit for a property a single run at
  the tip already covers for the code that shipped;
* `cargo check` — Rust in this repository moves with the shell, and a UI split
  does not break it;
* the Vite production build — `tsc --noEmit` over the same sources is what
  would fail first, and it is seconds instead of a bundling run.

Each commit is checked in its own detached `git worktree` outside the
repository. The working tree is never checked out, never stashed and never
written to, so running this while you have unsaved work in flight is safe —
which is not a property a bisect-driving tool can be assumed to have.

    python3 scripts/check_history.py                      # origin/<branch>..HEAD
    python3 scripts/check_history.py --range 224d8b3..d967a99
    python3 scripts/check_history.py --legs lint,typecheck,python

Exits non-zero if any commit fails, naming the commit, the leg, and the first
line of its output — because a report of thirty "FAIL"s is a report nobody
acts on.
"""

from __future__ import annotations

import argparse
import atexit
import os
import shutil
import signal
import subprocess  # noqa: S404 — argv lists only, never a shell
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The files that decide whether one `ui/node_modules` can serve every commit in
# a range. If any of them moves, a symlinked tree is testing old dependencies
# against new code, which is a false PASS — worse than no check at all — so the
# range is installed properly instead.
UI_DEPS = ("ui/package.json", "ui/package-lock.json")

# Per-commit output that says nothing, stripped before hunting for the reason.
_NOISE = (
    "npm notice",
    "Executing <Task",
    "loading package",
    "npm warn",
)


# ── the pure half: what to run, and what to say about the answer ──────────────
#
# Separated from the half that runs things so the policy can be tested without
# a worktree, a network, or the gate re-entering its own suite.


@dataclass(frozen=True)
class Leg:
    """One check, as the argv that produces it and where it runs."""

    name: str
    argv: tuple[str, ...]
    cwd: str = "."


@dataclass
class CommitResult:
    """What one commit did, per leg, and why the first failure failed.

    `blocked` is how a commit that could not be checked at all is kept
    distinguishable from one that was checked and passed. It is a field rather
    than a pseudo-leg because "no legs ran" and "every leg passed" must never
    read the same — that conflation is the exact failure this tool exists to
    stop, and a sentinel value inside `legs` is how it would creep back in.
    """

    sha: str
    subject: str
    legs: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    blocked: str = ""

    @property
    def failed(self) -> list[str]:
        return [name for name, verdict in self.legs.items() if verdict != "ok"]

    @property
    def ok(self) -> bool:
        """Green means: something ran, and all of it passed."""
        return not self.blocked and bool(self.legs) and not self.failed


def skip_reason(commit_count: int) -> str | None:
    """Why this range is not worth checking, or None if it is.

    The threshold is 1, and the reason is specific: with a single commit there
    is no middle, so every commit in the range is the tip, and `make ci` has
    already answered the only question there is. A range that changes nothing
    has no tip worth re-asking about.
    """
    if commit_count <= 0:
        return "the range names no commits — nothing has been added since the remote"
    if commit_count == 1:
        return (
            "the range is one commit, so it has no middle: every commit in it is "
            "the tip, and `make ci` already gates that"
        )
    return None


def first_error_line(leg: str, output: str) -> str:
    """The one line of `output` that names the failure.

    Each leg reports differently — mypy says `file:line: error: …`, unittest
    says `FAIL: name`, tsc says `error TS…`, node --test says `✖ name` — and a
    reader who has to scroll a wall of output to find out which of thirty-odd
    lines matters will stop reading the report. So the first line matching that
    leg's own marker wins, and anything unrecognised falls back to the first
    line that is not stack noise.
    """
    markers: dict[str, tuple[str, ...]] = {
        "typecheck": ("error:",),
        "python": ("FAIL:", "ERROR:"),
        "tsc": ("error TS",),
        "tsc-tests": ("error TS",),
        "uitest": ("✖ ",),
    }
    wanted = markers.get(leg, ())
    lines = output.splitlines()
    for line in lines:
        if any(marker in line for marker in wanted):
            return line.strip()[:160]
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(_NOISE) or stripped.startswith("at "):
            continue
        if line.startswith((" ", "\t", "Traceback")):
            continue
        return stripped[:160]
    return "(no output)"


def verdict(results: Sequence[CommitResult]) -> int:
    """The exit code: non-zero unless every commit was checked and passed."""
    return 0 if all(result.ok for result in results) else 1


def format_table(results: Sequence[CommitResult], legs: Sequence[str]) -> str:
    """The report. One row per commit, one column per leg, a reason under the
    first failure of each broken row."""
    width = max((len(result.sha) for result in results), default=7)
    header = "  ".join([f"{'commit':<{width}}", *legs])
    lines = [f"  {header}", f"  {'-' * len(header)}"]
    for result in results:
        cells = "  ".join(f"{result.legs.get(leg, '?'):<{max(len(leg), 4)}}" for leg in legs)
        lines.append(f"  {result.sha:<{width}}  {cells}  {result.subject}")
        if result.blocked:
            lines.append(f"  {'':<{width}}  └─ not checked: {result.blocked}")
        for leg in result.failed:
            lines.append(f"  {'':<{width}}  └─ {leg}: {result.reasons.get(leg, '')}")
    return "\n".join(lines)


# ── the half that touches the machine ─────────────────────────────────────────


def _run(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
    """argv, never a shell. Returns (status, combined output)."""
    completed = subprocess.run(  # noqa: S603 — argv list, no shell, no user input
        list(argv),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def _git(argv: Sequence[str], cwd: Path | None = None) -> tuple[int, str]:
    return _run(["git", *argv], cwd or ROOT)  # noqa: S607 — git is on PATH by design


def _tool(name: str, venv: Path) -> str:
    """A dev tool from PATH or the project venv — the same fallback order the
    Makefile uses, so a leg here fails exactly when `make ci` would."""
    found = shutil.which(name)
    if found:
        return found
    in_venv = venv / "bin" / name
    if in_venv.exists():
        return str(in_venv)
    return ""


def build_legs(venv: Path, node_modules: Path) -> list[Leg]:
    """The decisive legs, cheapest first. A leg whose tool is missing is still
    listed, and reports itself missing rather than passing — a check that could
    not run must never read as a check that passed."""
    tsc = str(node_modules / ".bin" / "tsc")
    return [
        Leg("lint", (_tool("ruff", venv) or "ruff", "check", "engine", "tests", "scripts", "benchmarks")),
        Leg("typecheck", (_tool("mypy", venv) or "mypy",)),
        Leg("python", (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py")),
        Leg("tsc", (tsc, "--noEmit"), "ui"),
        Leg("tsc-tests", (tsc, "--noEmit", "-p", "tsconfig.test.json"), "ui"),
        Leg(
            "uitest",
            ("node", "--experimental-strip-types", "--test", "tests/*.test.ts"),
            "ui",
        ),
    ]


_worktrees_created: list[Path] = []


def _remove_worktree(path: Path) -> None:
    """Tear a worktree down, and prune its administrative files. Runs on the
    error path too: a checker that leaves a worktree behind on a Ctrl-C is a
    checker people stop running."""
    _git(["worktree", "remove", "--force", str(path)])
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    if path in _worktrees_created:
        _worktrees_created.remove(path)


def _cleanup_all() -> None:
    for path in list(_worktrees_created):
        _remove_worktree(path)


def _install_signal_cleanup() -> None:
    """Tear the worktrees down on Ctrl-C too.

    `atexit` does not run for SIGINT's default disposition, and a sweep that
    leaves a worktree behind when you stop it is a sweep that fills the disk
    and gets deleted rather than run again.
    """

    def _stop(signum: int, _frame: object) -> None:
        _cleanup_all()
        sys.exit(130)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _stop)


def _deps_moved(commits: Sequence[str]) -> bool:
    """Whether any commit in the range touches the UI dependency files."""
    if not commits:
        return False
    spec = f"{commits[0]}^..{commits[-1]}" if len(commits) > 1 else f"{commits[0]}^..{commits[0]}"
    status, out = _git(["diff", "--name-only", spec, "--", *UI_DEPS])
    return status == 0 and bool(out.strip())


def _commits_in_range(spec: str) -> tuple[list[str], str]:
    """(shas oldest first, problem).

    `problem` is empty on success — including a range that resolved to no
    commits, which is a legitimate answer and not a failure. It is non-empty
    only when git could not read the range at all, and the caller turns that
    into exit 2 rather than a green range nobody asked about.
    """
    status, out = _git(["rev-list", "--reverse", spec])
    if status != 0:
        first = next((line for line in out.splitlines() if line.strip()), "unknown git error")
        return [], f"`{spec}` did not resolve: {first.strip()}"
    return [line for line in out.split() if line], ""


def _subject(sha: str) -> str:
    status, out = _git(["log", "-1", "--format=%s", sha])
    return out.strip() if status == 0 else ""


def check_one(commit: str, root: Path, legs: Sequence[Leg], install: bool) -> CommitResult:
    """Check out `commit` into its own worktree and run the legs there."""
    worktree = Path(tempfile.mkdtemp(prefix="codify-history-", dir=str(root)))
    _worktrees_created.append(worktree)
    result = CommitResult(commit[:7], _subject(commit))

    status, out = _git(["worktree", "add", "--detach", "--force", str(worktree), commit])
    if status != 0:
        result.blocked = f"worktree failed: {out.strip()[:140]}"
        _remove_worktree(worktree)
        return result

    try:
        _prepare_ui_deps(worktree, install, result)
        for leg in legs:
            if result.blocked:
                break
            code, output = _run(leg.argv, worktree / leg.cwd)
            if code == 0:
                result.legs[leg.name] = "ok"
            else:
                result.legs[leg.name] = "FAIL"
                result.reasons[leg.name] = first_error_line(leg.name, output)
        return result
    finally:
        _remove_worktree(worktree)


def _prepare_ui_deps(worktree: Path, install: bool, result: CommitResult) -> None:
    """Give the worktree a `ui/node_modules`.

    A symlink to the main checkout's is the fast path and is only honest while
    the dependency files are identical across the range; the caller decides that
    with `_deps_moved`. When they moved, `npm ci` runs for real, because
    checking new code against old dependencies is a false pass.
    """
    source = ROOT / "ui" / "node_modules"
    if not source.is_dir():
        if install:
            code, _ = _run(["npm", "ci"], worktree / "ui")
            if code != 0:
                result.blocked = "`npm ci` failed — no network, or a broken lockfile"
        else:
            result.blocked = (
                f"no {source.relative_to(ROOT)} to reuse, and --install-deps was not given"
            )
        return
    if not install:
        os.symlink(source, worktree / "ui" / "node_modules", target_is_directory=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--range", default="", help="git range; default origin/<branch>..HEAD")
    parser.add_argument("--legs", default="", help="comma-separated subset of the legs")
    parser.add_argument(
        "--install-deps",
        action="store_true",
        help="run `npm ci` per worktree instead of reusing ui/node_modules",
    )
    parser.add_argument(
        "--worktrees", default="", help="parent directory for the worktrees (default: a temp dir)"
    )
    args = parser.parse_args(argv)

    venv = ROOT / ".venv"
    spec = args.range
    if not spec:
        status, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
        spec = f"origin/{branch.strip()}..HEAD" if status == 0 else ""

    commits, problem = _commits_in_range(spec) if spec else ([], "no branch to compare against")
    if problem:
        print(f"check-history: {problem}", file=sys.stderr)
        return 2
    if not commits:
        # A range that resolved to nothing is the skip rule answering, not an
        # error. It says so in the rule's own words rather than echoing the spec.
        print(f"check-history: nothing to check — {skip_reason(0)}")
        return 0

    reason = skip_reason(len(commits))
    if reason:
        print(f"check-history: nothing to check — {reason}")
        return 0

    legs = build_legs(venv, ROOT / "ui" / "node_modules")
    if args.legs:
        wanted = {name.strip() for name in args.legs.split(",") if name.strip()}
        unknown = wanted - {leg.name for leg in legs}
        if unknown:
            print(f"check-history: unknown legs {sorted(unknown)}; known: "
                  f"{[leg.name for leg in legs]}", file=sys.stderr)
            return 2
        legs = [leg for leg in legs if leg.name in wanted]

    install = args.install_deps or _deps_moved(commits)
    parent = Path(args.worktrees) if args.worktrees else Path(tempfile.mkdtemp(prefix="codify-hist-"))
    parent.mkdir(parents=True, exist_ok=True)

    # Armed before the sweep, not after: the sweep is minutes per commit, and a
    # Ctrl-C or a crash in its middle is the one moment the worktrees — and the
    # parent directory — are neither finished with nor claimed by a caller via
    # --worktrees. Registered any later, the first commit's `finally` tears its
    # own worktree down but everything else strands. (Registering one shutdown
    # per call is also why this sits after the early returns: a skipped run has
    # nothing to clean.)

    print(
        f"check-history: {len(commits)} commits in {spec} — "
        f"{len(legs)} {'leg' if len(legs) == 1 else 'legs'} each"
    )
    if install:
        print("  ui dependencies changed in this range, so each worktree gets its own install")
    print("  a leg that cannot run reports itself missing; it never counts as a pass\n")

    atexit.register(_cleanup_all)
    _install_signal_cleanup()

    results: list[CommitResult] = []
    for commit in commits:
        print(f"  {commit[:7]} {_subject(commit)}")
        sys.stdout.flush()
        results.append(check_one(commit, parent, legs, install))

    print()
    print(format_table(results, [leg.name for leg in legs]))
    code = verdict(results)
    broken = sum(1 for result in results if not result.ok)
    print()
    if code == 0:
        print(f"check-history: all {len(commits)} commits build.")
    else:
        verb = "does" if broken == 1 else "do"
        print(
            f"check-history: {broken} of {len(commits)} commits {verb} not build. The tip may "
            f"be green — `make ci` gates the tip, and this is the part it cannot see.\n"
            f"  `git bisect` will land on one of these and fail there.\n"
            f"  Fix by reordering (the shell and the engine's app.py have to arrive before\n"
            f"  the commits whose tests assert on them), or squash the range back to one commit."
        )
    if not args.worktrees:
        shutil.rmtree(parent, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
