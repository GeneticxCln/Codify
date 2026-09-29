"""A dead stderr pipe must not stop the engine from dying with its shell.

The orphan that got away: `terminate` printed its "parent is gone" line to
stderr *before* arming the deadline and signalling itself, and the engine's
stderr is a pipe whose only reader is the desktop shell. A SIGKILLed shell
takes that read end with it, so the print — the handler's first act — raised
`BrokenPipeError` inside the watchdog thread. Thread dead, deadline unarmed,
SIGTERM unsent: the engine outlived its shell forever, holding the port and
the database, with nothing in any log to explain it — because the log *was*
the pipe that died.

`tests/test_engine_death_e2e.py` cannot see this: its launcher leaves the
engine's stderr inherited down to the test process, which drains it, so a
print on a "dead" pipe always had a reader. These tests build the real shape —
a handler whose stderr is a pipe nobody reads — in a real child process,
because the defect lives exactly where a thread's silent death is invisible
to any assertion made after it.

The second hole is the boot: the watch used to arm only in `serve()`, after
the heavy imports, so a shell dying mid-boot left an engine watching nothing.
`engine/__main__.py` now starts the watch first thing; that is pinned here by
reading the entrypoint's source, plus a real early exit that must be quiet on
the handshake channel and bounded like every other death.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import watchdog

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The child gets this long to die on its own after the SIGTERM. The default
# disposition (nothing has installed a handler yet in a boot that never got
# there) is immediate; the deadline path is HARD_EXIT_GRACE_S, so this covers
# every legitimate way the child ends while still catching a hang.
_DEATH_BUDGET_S = watchdog.HARD_EXIT_GRACE_S + 3.0

# The exact defect, quoted from the original `terminate` (before this fix) so
# the regression test fails on the code that had it and not on something nearby.
# The child's namespace has `watchdog`, `os`, `signal`, `sys`, `time` — nothing else.
_ORIGINAL_TERMINATE_BODY = (
    "print('[engine] parent process 4321 is gone — exiting', file=sys.stderr, flush=True)\n"
    "    watchdog.arm_hard_deadline()\n"
    "    os.kill(os.getpid(), signal.SIGTERM)"
)

# The fixed shape: act first, explain afterwards, with the explanation guarded.
_FIXED_TERMINATE_BODY = (
    "watchdog.arm_hard_deadline()\n"
    "    os.kill(os.getpid(), signal.SIGTERM)\n"
    "    try:\n"
    "        print('[engine] parent process 4321 is gone — exiting', file=sys.stderr, flush=True)\n"
    "    except OSError:\n"
    "        pass"
)


def _child_source(handler_body: str) -> str:
    """A child that installs `handler_body` as its death handler and dies by it.

    The handler runs with stderr pointed at a pipe **nobody reads** — the
    killed-shell shape, which the e2e suite's inherited pipes cannot reproduce.
    The parent reads the child's *liveness*, not its log: silence on that pipe
    is the whole point.
    """
    return textwrap.dedent(
        """
        import os, signal, sys, threading, time
        sys.path.insert(0, {root!r})
        from engine import watchdog

        READ_END_DIES_WITH_THE_SHELL = os.pipe()
        os.close(READ_END_DIES_WITH_THE_SHELL[0])
        os.dup2(READ_END_DIES_WITH_THE_SHELL[1], 2)
        os.close(READ_END_DIES_WITH_THE_SHELL[1])
        # fd 2 is now a pipe with no reader; `sys.stderr` still points at fd 2,
        # so the handler's first write raises BrokenPipeError — exactly what a
        # SIGKILLed shell leaves behind.

        def death_handler(parent_pid: int) -> None:
            {body}

        # The real loop, started directly: `start_parent_watchdog` prints its
        # arming line to stderr, and stderr here is deliberately a dead pipe —
        # that print would kill the child before the handler is exercised.
        # `watch` itself says nothing; it is the handler under test that must
        # survive (or die on) the pipe.
        threading.Thread(
            target=watchdog.watch,
            args=(4321,),
            kwargs={{"on_gone": death_handler, "is_gone": lambda pid: pid == 4321}},
            daemon=True,
        ).start()
        # Live until the handler ends us; a watchdog thread that dies on its
        # own leaves this sleeping — 10s, because the parent's timeout is the
        # only thing that can end a child whose handler failed, and the two
        # windows have to stay the same order of size.
        time.sleep(10)
        """
    ).format(root=str(PROJECT_ROOT), body=handler_body)


class DeadPipeDeathTests(unittest.TestCase):
    """The handler must end the process even when its log has nowhere to go."""

    # How long the child is given to die before it is declared an orphan. The
    # handler fires within milliseconds of boot (the watched pid is gone at the
    # first poll), so two seconds is generous to the fix and unambiguous about
    # the defect.
    _ORPHAN_VERDICT_S = 2.0

    def _child(self, handler_body: str) -> tuple[bool, int | None]:
        """Run the child; answer `(still alive after the verdict window, code)`.

        Liveness is the receipt the dead pipe cannot give: a handler that works
        ends the process inside the window; one that died on its own log line
        leaves the child running — the orphan — and it is killed here the way
        the real world ends one.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "CODIFY_HOME": tmp}
            env.pop(watchdog.ENV_PARENT_PID, None)  # the child arms its own watch
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, our own source
                [sys.executable, "-c", _child_source(handler_body)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=str(PROJECT_ROOT),
            )
            try:
                time.sleep(self._ORPHAN_VERDICT_S)
                alive = proc.poll() is None
                if alive:
                    proc.kill()  # the orphan, ended the way its reaper would
                proc.wait(timeout=10)
            finally:
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
            return alive, proc.returncode

    def test_the_death_handler_ends_the_process_with_nobody_reading_stderr(self) -> None:
        alive, code = self._child(_FIXED_TERMINATE_BODY)
        self.assertFalse(
            alive,
            "the fixed handler must end the process even with a dead stderr pipe",
        )
        self.assertEqual(
            -signal.SIGTERM,
            code,
            "the death must be the handler's own signal, not a timeout",
        )

    def test_the_print_before_acting_is_the_shape_that_orphans(self) -> None:
        # The negative control, and it is a control: the original handler,
        # verbatim, injected into the harness. It must orphan on ANY engine —
        # it passed before the fix and passes after it — because what it pins
        # is that this harness genuinely reproduces the orphan. Without it, the
        # sibling test's pass could mean the setup can no longer create the
        # conditions rather than that the handler now survives them.
        alive, code = self._child(_ORIGINAL_TERMINATE_BODY)
        self.assertTrue(
            alive,
            "the print-first handler must leave the child orphaned (still running "
            "after its parent died); if it dies by itself, this test no longer "
            "reproduces the defect and the pin has stopped guarding anything",
        )
        self.assertEqual(
            -signal.SIGKILL,
            code,
            "the only way this child ends is its reaper — nothing in it stops itself",
        )

    def test_the_shipped_handler_acts_before_it_explains(self) -> None:
        # In-process pin of the order: arm, signal, and only then write.
        order: list[str] = []

        class Recording:
            def write(self, _text: str) -> int:
                order.append("write")
                return len(_text)

            def flush(self) -> None:
                return None

        with (
            patch.object(
                watchdog, "arm_hard_deadline", side_effect=lambda *a, **k: order.append("arm")
            ),
            patch.object(
                os, "kill", side_effect=lambda pid, sig: order.append(f"kill:{pid}:{sig}")
            ),
            patch.object(sys, "stderr", Recording()),
        ):
            watchdog.terminate(4321)
        # `print` may issue two writes (text, then the newline) — the pin is on
        # the order, not on CPython's write count: arm, signal, then writes only.
        self.assertEqual(
            ["arm", f"kill:{os.getpid()}:{signal.SIGTERM}"],
            order[:2],
            "the deadline is armed and the signal delivered before the handler "
            "tries to say anything",
        )
        self.assertTrue(
            len(order) > 2 and all(entry == "write" for entry in order[2:]),
            f"only writes may follow the signal, saw: {order}",
        )

    def test_a_broken_stderr_cannot_kill_the_handler(self) -> None:
        # The guard around the explanation: a write that raises must not take
        # the handler down before its work is done.
        class Broken:
            def write(self, _text: str) -> int:
                raise OSError("broken pipe")

            def flush(self) -> None:
                raise OSError("broken pipe")

        with (
            patch.object(watchdog, "arm_hard_deadline") as arm,
            patch.object(os, "kill") as kill,
            patch.object(sys, "stderr", Broken()),
        ):
            watchdog.terminate(4321)  # must not raise
        arm.assert_called_once()
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    def test_the_production_handler_acts_before_it_explains(self) -> None:
        # `on_parent_gone` is the handler both arming points actually install.
        # Same pin as `terminate`: the stop is delivered before the write.
        order: list[str] = []

        class Recording:
            def write(self, _text: str) -> int:
                order.append("write")
                return len(_text)

            def flush(self) -> None:
                return None

        def arm() -> bool:
            order.append("arm")
            return True

        with (
            patch.object(watchdog, "arm_death_signal", side_effect=arm),
            patch.object(sys, "stderr", Recording()),
        ):
            watchdog.on_parent_gone(4321)
        self.assertEqual(
            ["arm"],
            order[:1],
            "the stop must be delivered before the handler says anything",
        )
        self.assertTrue(
            len(order) > 1 and all(entry == "write" for entry in order[1:]),
            f"only writes may follow the stop, saw: {order}",
        )

    def test_the_production_handler_survives_a_dead_stderr(self) -> None:
        class Broken:
            def write(self, _text: str) -> int:
                raise OSError("broken pipe")

            def flush(self) -> None:
                raise OSError("broken pipe")

        with (
            patch.object(watchdog, "arm_death_signal") as arm,
            patch.object(sys, "stderr", Broken()),
        ):
            watchdog.on_parent_gone(4321)  # must not raise
        arm.assert_called_once()

    def test_the_dead_pipe_cannot_stop_the_deadline_either(self) -> None:
        # The deadline's own line had the same hole: a print that raises before
        # `hard_exit` runs would orphan the engine at the deadline on the hang
        # path — the bounded shutdown would become an unbounded one.
        class Broken:
            def write(self, _text: str) -> int:
                raise OSError("broken pipe")

            def flush(self) -> None:
                raise OSError("broken pipe")

        with (
            patch.object(sys, "stderr", Broken()),
            patch.object(watchdog, "hard_exit") as leave,
        ):
            watchdog.arm_hard_deadline(0.01).join(timeout=2.0)
        leave.assert_called_once_with()


class BootWatchTests(unittest.TestCase):
    """The watch is armed before the heavy imports, or the boot is unguarded."""

    def test_the_entrypoint_starts_the_watch_before_importing_the_app(self) -> None:
        source = (PROJECT_ROOT / "engine" / "__main__.py").read_text(encoding="utf-8")
        watch_line = source.index("watchdog.start_parent_watchdog(")
        app_line = source.index("from engine.app import main")
        self.assertLess(
            watch_line,
            app_line,
            "the parent watch must be armed before engine.app is imported: the "
            "import is the boot window, and a shell dying inside it leaves an "
            "engine that never armed anything",
        )

    def test_a_second_start_does_not_run_two_watchers(self) -> None:
        # `__main__` starts the watch and `serve()` calls in again; the second
        # call must find the first alive rather than double the pollers (and
        # double-arm the deadline on the same death). The memo is poked
        # directly because a *production-shaped* watcher in this test process
        # would have to watch a real parent — a poller this suite does not
        # want, and the one race a thread-start test cannot dodge.
        sentinel = threading.Thread(target=time.sleep, args=(0.05,))
        sentinel.start()
        with patch.object(watchdog, "_watchdog_thread", sentinel):
            with patch.dict(os.environ, {watchdog.ENV_PARENT_PID: "4321"}):
                self.assertIsNone(
                    watchdog.start_parent_watchdog(),
                    "a live production watcher must not be re-armed",
                )

    def test_a_dead_memo_never_blocks_a_new_watch(self) -> None:
        # The memo holds the thread, not the decision: a finished watcher must
        # not keep the next arming from happening.
        finished = threading.Thread(target=lambda: None)
        finished.start()
        finished.join()
        with patch.object(watchdog, "_watchdog_thread", finished):
            with patch.dict(os.environ, {watchdog.ENV_PARENT_PID: "4321"}):
                thread = watchdog.start_parent_watchdog(
                    4321, on_parent_death=lambda pid: None
                )
        try:
            self.assertIsNotNone(thread, "a dead memo must not block a new watch")
        finally:
            if thread is not None:
                thread.join(timeout=5.0)


class EntrypointExitTests(unittest.TestCase):
    """What `python3 -m engine` does when the parent is already gone at boot."""

    def test_a_boot_with_a_dead_parent_ends_itself_and_says_nothing_on_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "CODIFY_HOME": tmp, watchdog.ENV_PARENT_PID: "4321"}
            started = time.monotonic()
            run = subprocess.run(  # noqa: S603 — the real entrypoint, hermetic home
                [sys.executable, "-m", "engine"],
                capture_output=True,
                timeout=_DEATH_BUDGET_S + 15.0,
                env=env,
                cwd=str(PROJECT_ROOT),
                check=False,
            )
        elapsed = time.monotonic() - started
        self.assertIn(
            run.returncode,
            (-signal.SIGTERM, 0),
            "the boot must end itself when the parent it was handed is already "
            "gone — the signal's default disposition if it lands during the "
            "imports, the bounded shutdown if it lands after",
        )
        self.assertLess(elapsed, _DEATH_BUDGET_S, "the boot death must be bounded")
        self.assertEqual(
            b"",
            run.stdout,
            "nothing on stdout before the handshake channel exists",
        )


if __name__ == "__main__":
    unittest.main()
