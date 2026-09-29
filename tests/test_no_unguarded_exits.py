"""Every way the engine can end or signal *this* process, and why each is allowed.

`tests/test_no_unguarded_spawns.py` freezes the places a process can *start*. This
is the other half: the places one can *stop*. They are the same class of risk and
they were found the same way — not by reading the code for a bug, but by noticing
that the suite had stopped.

**What it cost.** `engine.app.main` ended the process through `os._exit` and two
tests called it in-process. The runner was gone at 505 of 1054 tests, silently,
with exit status 0, and `make test-engine` reported success. Half the engine was
not being checked, which is the state in which a project accumulates real breakage
while the gate stays green — the reason that reads as "unstable" when it is
actually "unwatched".

**Why a static freeze.** `os._exit` in a request path is invisible to every
dynamic test: nothing crashes until the day the code runs in the runner, and by
then the failure is a vanished process rather than a failed assertion. This walks
every module under `engine/` (parsed, never imported or executed) and reports each
call that can end or signal the host: `os._exit`, `os.killpg`, `os.kill` at our own
pid, `signal.raise_signal`, `os.abort`, and the `os` members that start a
replacement process. Allowlisted sites carry their justification in the table
below, where the change happens, and an entry no longer backed by code is reported
as stale so the freeze cannot outlive what it names.

The scan is deliberately narrower than "any use of the word `kill`". `_kill_group`
in `sandbox.py` is a method that decides *whose* group to signal, and the decision
inside it — never signal a group this process leads — is the safety property worth
having. Freezing on names would have banned the function that carries the fix.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENGINE = PROJECT_ROOT / "engine"

# `module.Class.method` -> why this process is allowed to end, here and only here.
# Every entry is a deliberate exit with a named alternative for tests: either an
# injectable callback or the suite's standing `patch.object(os, "_exit")`.
ALLOWED_EXITS: dict[str, str] = {
    "watchdog.hard_exit": (
        "the deadline. Interpreter finalization joins threads nobody can cancel "
        "(`asyncio.to_thread` on a sandboxed command), and that join is where an "
        "orphan engine came from: the signal arrived, the socket closed, and the "
        "process stayed in the join holding its database and its port. Reached "
        "only from the process entry point and from the armed deadline timer. "
        "Tests substitute `patch.object(os, \"_exit\")`; the boot path a test can "
        "call is `serve()`, which returns"
    ),
    "watchdog.terminate": (
        "the parent watchdog's action: arm the deadline, then SIGTERM ourselves so "
        "uvicorn's shutdown runs. Now that `hermetic.disarm_parent_watchdogs` "
        "clears `CODIFY_PARENT_PID` for the suite, no test can reach this by "
        "accident; `test_watchdog.py` calls it with `os.kill` patched"
    ),
    "watchdog.arm_death_signal": (
        "the death both arming points deliver (`on_parent_gone` from the "
        "entrypoint and from `serve`): arm the deadline, then SIGTERM ourselves "
        "before anything can print on the stderr pipe a hard-killed shell left "
        "without a reader. The print-first order was the orphan itself — the "
        "write raised BrokenPipeError in the watchdog thread and the engine "
        "outlived its shell holding the port and the database. Inert without "
        "`CODIFY_PARENT_PID`, which `hermetic.disarm_parent_watchdogs` clears; "
        "tests reach it with `os.kill` patched"
    ),
    "spawn_guard.Guard._leave": (
        "the guard's own exit, and the seam that makes `kill_group` callable from a "
        "test. It checks leadership first: a guard that does not lead its process "
        "group must not signal that group, because a guard spawned without "
        "`start_new_session=True` shares the engine's group and would SIGKILL the "
        "engine and the desktop shell it exists to outlive"
    ),
    "spawn_guard.Guard.kill_group": (
        "SIGKILL of the command's own group once the engine is gone, so a command "
        "that ignores TERM cannot outlive everyone. Gated on `getpgrp() == getpid()` "
        "for the reason the `_leave` entry gives"
    ),
    "spawn_guard.reproduce": (
        "re-raise a signal death on the guard so the engine reads `returncode` as "
        "the command's real outcome rather than a guard exit code. Guard-only, and "
        "only reached in the guard process"
    ),
    "sandbox._signal_alone": (
        "the single-pid fallback in `_kill_group`, reached precisely when the group "
        "is not safe to signal — including the case where a child was never given "
        "its own session and `getpgid` answers with the engine's own group. "
        "Signalling one pid cannot reach anything but that pid"
    ),
    "sandbox.SandboxService._kill_group": (
        "a timed-out sandboxed command has to take its whole group with it, or a "
        "command that spawned children outlives the turn. It asks whether the group "
        "is one this process leads and refuses to signal it if so; `start_new_session="
        "True` on the spawn is what makes the group the child's, and this is the "
        "check that stops a mis-spawned command from killing the app. Reachable "
        "from a request, which is why it is on this list at all"
    ),
}

# The `os` members that can end this process, or hand it to a replacement one.
# `kill` is here in full: the freeze below narrows it to self-directed calls,
# while the staleness check wants the wider set so a function that merely
# *mentions* `os.kill` still counts as live.
DANGEROUS_MEMBERS = frozenset(
    {
        "_exit",
        "kill",
        "killpg",
        "abort",
        "execl",
        "execv",
        "execve",
        "posix_spawn",
        "posix_spawnp",
        "popen",
        "spawnv",
        "system",
    }
)

# Members that are dangerous only when aimed at us. `os.kill(child_pid, …)` is
# ordinary supervision; `os.kill(os.getpid(), …)` is this process asking to die.
_ALWAYS_DANGEROUS = DANGEROUS_MEMBERS - {"kill"}


class _ExitCollector(ast.NodeVisitor):
    """Every call to a dangerous `os` member, tagged with its enclosing name.

    One visitor, built once at module level and given its state through the
    constructor. Defining it inside the loop that walks the files would close
    over the loop's variables, and a late-bound name is exactly the kind of bug a
    freeze must not be able to have: the scan would quietly stop finding things.
    """

    def __init__(self, module: str, *, self_directed_only: bool = True) -> None:
        self.stack: list[str] = [module]
        #: When false, `os.kill` at any pid counts. The staleness check wants
        #: that; the freeze does not, because killing a child is supervision.
        self.self_directed_only = self_directed_only
        #: (dotted name, member label, lineno) for every dangerous call.
        self.calls: list[tuple[str, str, int]] = []

    def _dotted(self) -> str:
        return ".".join(self.stack)

    def _push(self, name: str, node: ast.AST) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._push(node.name, node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._push(node.name, node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._push(node.name, node)

    @staticmethod
    def _is_self_directed(node: ast.Call) -> bool:
        """`os.kill(os.getpid()…)`, `os.kill(os.getpgrp()…)` — aimed at us."""
        if not node.args:
            return False
        first = node.args[0]
        return (
            isinstance(first, ast.Call)
            and isinstance(first.func, ast.Attribute)
            and first.func.attr in {"getpid", "getpgrp"}
        )

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner, member = func.value.id, func.attr
            if owner == "os" and member in DANGEROUS_MEMBERS:
                if (
                    member == "kill"
                    and self.self_directed_only
                    and not self._is_self_directed(node)
                ):
                    self.generic_visit(node)
                    return
                label = "os.kill(self)" if member == "kill" else f"os.{member}"
                self.calls.append((self._dotted(), label, node.lineno))
            elif owner == "signal" and member == "raise_signal":
                self.calls.append((self._dotted(), "signal.raise_signal", node.lineno))
        self.generic_visit(node)


def _scan(*, self_directed_only: bool = True) -> list[tuple[str, str, int]]:
    """Every exit site in the engine, as (file, "name label", unused)."""
    found: list[tuple[str, str, int]] = []
    for path in sorted(ENGINE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _ExitCollector(path.stem, self_directed_only=self_directed_only)
        collector.visit(tree)
        for dotted, label, lineno in collector.calls:
            found.append((f"{path.relative_to(PROJECT_ROOT)}:{lineno}", f"{dotted} {label}", 0))
    return found


class ProcessExitScanTest(unittest.TestCase):
    def test_no_module_ends_the_process_outside_the_allowlist(self) -> None:
        findings = [
            f"{where} {what} can end this process. Add it to ALLOWED_EXITS with a "
            "reason, or route it through a site that already has one."
            for where, what, _ in _scan()
            if what.rsplit(" ", 1)[0] not in ALLOWED_EXITS
        ]
        self.assertEqual(
            findings,
            [],
            "these can end or signal the engine's own process:\n  " + "\n  ".join(findings),
        )

    def test_every_allowlisted_exit_is_still_reachable(self) -> None:
        """A freeze whose table outlives its code is a freeze nobody reads.

        Deliberately wider than the check above: it asks only whether the name
        still touches something dangerous, so a site that was rewritten to kill a
        *child* rather than itself is still counted as live rather than reported
        as stale and quietly deleted from the table.
        """
        live = {what.rsplit(" ", 1)[0] for _where, what, _ in _scan(self_directed_only=False)}
        stale = sorted(set(ALLOWED_EXITS) - live)
        self.assertEqual(
            stale,
            [],
            f"ALLOWED_EXITS names {stale}, which no longer calls anything that can "
            "end the process — delete the entries rather than leaving the table to "
            "drift",
        )

    def test_every_allowlisted_entry_explains_itself(self) -> None:
        for name, reason in ALLOWED_EXITS.items():
            self.assertGreater(
                len(reason),
                80,
                f"{name} is allowlisted with no reasoning. A freeze is only worth "
                "reading if each entry says why this site and not another",
            )


if __name__ == "__main__":
    unittest.main()
