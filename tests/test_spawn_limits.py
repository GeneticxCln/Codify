"""A spawned command runs under the limits its spawn site asked for, and cannot raise them.

`engine/spawn_guard.py` applies `Limits` to itself with `setrlimit` just before it starts the command, so the command is
born under them. Nothing in `engine/` limited a child's memory, core size or file size before this; a wall-clock timeout
was the only bound. These tests run real guarded children (the limits are a kernel fact: only a process can show them) and
never change this process's own limits.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import resource
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine import library
from engine.sandbox import COMMAND_LIMITS, SandboxService
from engine.spawn_guard import (
    ENV_LIMITS,
    Limits,
    apply_limits,
    guarded_argv,
    guarded_env,
    limits_from_env,
)

GIB = 1024**3


def under_guard(code: str, limits: Limits | None, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """Run `python -c code` as a guarded child with `limits`, the way a spawn site does."""
    return subprocess.run(  # noqa: S603 — the guard and this interpreter, with a test's own code
        guarded_argv([sys.executable, "-c", code]),
        env=guarded_env({}, limits),
        capture_output=True,
        text=True,
        timeout=timeout,
        start_new_session=True,
        check=False,
    )


class TestTheLaunchersSide(unittest.TestCase):
    def test_limits_travel_as_json_and_only_the_named_ones(self) -> None:
        env = guarded_env({}, Limits(address_space=GIB, core_bytes=0))
        self.assertEqual({"as": GIB, "core": 0}, json.loads(env[ENV_LIMITS]))

    def test_no_limits_means_no_variable_and_a_stale_one_is_dropped(self) -> None:
        self.assertNotIn(ENV_LIMITS, guarded_env({}, None))
        self.assertNotIn(ENV_LIMITS, guarded_env({ENV_LIMITS: '{"as": 1}'}, None))
        self.assertNotIn(ENV_LIMITS, guarded_env({}, Limits()))

    def test_a_request_the_guard_cannot_trust_is_ignored(self) -> None:
        for raw in ("", "not json", "[1]", '"x"', '{"as": -1}', '{"as": true}', '{"as": "9"}', '{"nproc": 5}', '{"as": 1.5}'):
            with self.subTest(raw=raw):
                self.assertEqual({}, limits_from_env({ENV_LIMITS: raw}))

    def test_known_names_with_non_negative_integers_are_kept(self) -> None:
        self.assertEqual(
            {"as": GIB, "cpu": 5, "fsize": 0, "core": 0},
            limits_from_env({ENV_LIMITS: json.dumps({"as": GIB, "cpu": 5, "fsize": 0, "core": 0, "other": 1})}),
        )


class TestApplyingThem(unittest.TestCase):
    def test_a_request_above_the_inherited_hard_limit_is_clamped_not_raised(self) -> None:
        # Done in a child so this process's own limits are untouched.
        code = (
            "import json, resource\n"
            "from engine.spawn_guard import apply_limits\n"
            "resource.setrlimit(resource.RLIMIT_CORE, (0, 4096))\n"
            "print(json.dumps(apply_limits({'core': 10**9})))\n"
            "print(resource.getrlimit(resource.RLIMIT_CORE))\n"
        )
        out = subprocess.run(  # noqa: S603 — this interpreter and a test's own code
            [sys.executable, "-c", code], capture_output=True, text=True, check=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        ).stdout.splitlines()
        self.assertEqual({"core": 4096}, json.loads(out[0]))
        self.assertEqual("(4096, 4096)", out[1])

    def test_nothing_requested_changes_nothing(self) -> None:
        before = resource.getrlimit(resource.RLIMIT_CORE)
        self.assertEqual({}, apply_limits({}))
        self.assertEqual(before, resource.getrlimit(resource.RLIMIT_CORE))


class TestARealChildIsHeldToThem(unittest.TestCase):
    def test_the_child_sees_the_limits_it_was_given(self) -> None:
        done = under_guard(
            "import resource\n"
            "print(resource.getrlimit(resource.RLIMIT_CORE)[0], resource.getrlimit(resource.RLIMIT_FSIZE)[0])\n",
            Limits(core_bytes=0, file_bytes=12345),
        )
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("0 12345", done.stdout.strip())

    def test_without_limits_the_child_has_the_engines(self) -> None:
        done = under_guard("import resource\nprint(resource.getrlimit(resource.RLIMIT_FSIZE)[0])\n", None)
        self.assertEqual(str(resource.getrlimit(resource.RLIMIT_FSIZE)[0]), done.stdout.strip())

    def test_a_child_past_its_address_space_gets_a_memory_error(self) -> None:
        done = under_guard(
            "try:\n"
            "    blob = bytearray(2 * 1024**3)\n"
            "    print('allocated')\n"
            "except MemoryError:\n"
            "    print('memory error')\n",
            Limits(address_space=GIB),
        )
        self.assertEqual("memory error", done.stdout.strip(), done.stderr)

    def test_the_same_allocation_without_the_limit_succeeds(self) -> None:
        # The control: the cap above is what stopped it, not the machine.
        done = under_guard("blob = bytearray(2 * 1024**3)\nprint('allocated')\n", None)
        self.assertEqual("allocated", done.stdout.strip(), done.stderr)

    def test_a_child_cannot_raise_its_own_limit_past_the_hard_one(self) -> None:
        done = under_guard(
            "import resource\n"
            "try:\n"
            "    resource.setrlimit(resource.RLIMIT_FSIZE, (10**9, 10**9))\n"
            "    print('raised')\n"
            "except ValueError:\n"
            "    print('refused')\n",
            Limits(file_bytes=1000),
        )
        self.assertEqual("refused", done.stdout.strip(), done.stderr)

    def test_a_writer_stops_at_its_file_size(self) -> None:
        # Python ignores SIGXFSZ, so the write fails with EFBIG; a C tool without that would be killed by the signal.
        # Either way the file cannot grow past the cap, which is the guarantee.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "big"
            done = under_guard(
                f"with open({str(target)!r}, 'wb') as f:\n    f.write(b'x' * 1_000_000)\n",
                Limits(file_bytes=4096),
            )
            self.assertNotEqual(0, done.returncode)
            self.assertTrue(
                done.returncode == -signal.SIGXFSZ or "File too large" in done.stderr,
                f"{done.returncode}: {done.stderr}",
            )
            self.assertLessEqual(target.stat().st_size, 4096)


class TestTheSpawnSitesAskForThem(unittest.TestCase):
    def test_a_sandbox_command_runs_with_no_core_and_a_file_size_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "show.py").write_text(
                "import resource\n"
                "print(resource.getrlimit(resource.RLIMIT_CORE)[0], resource.getrlimit(resource.RLIMIT_FSIZE)[0])\n",
                encoding="utf-8",
            )
            result = SandboxService().run_command(str(root), ["python3", "show.py"], timeout_s=30)
        self.assertEqual(0, result["exit_code"], result["stderr"])
        self.assertEqual(f"0 {COMMAND_LIMITS.file_bytes}", result["stdout"].strip())

    def test_the_constants_say_what_the_docs_say(self) -> None:
        self.assertEqual(0, COMMAND_LIMITS.core_bytes)
        self.assertEqual(2 * GIB, COMMAND_LIMITS.file_bytes)
        self.assertIsNone(COMMAND_LIMITS.address_space, "a build tool or a JVM reserves far more than it uses")
        self.assertIsNone(COMMAND_LIMITS.cpu_seconds)

    def test_the_file_worker_is_capped_in_memory_and_cpu_and_leaves_no_core(self) -> None:
        limits = library._regex_worker_limits(15.0)
        self.assertEqual(library.REGEX_WORKER_ADDRESS_SPACE, limits.address_space)
        self.assertEqual(45, limits.cpu_seconds, "the budget plus thirty seconds")
        self.assertEqual(0, limits.core_bytes)

    def test_a_real_search_still_works_under_the_worker_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "a.txt").write_text("needle in a haystack\n", encoding="utf-8")
            found = library.LibraryService(str(root)).search("needle", regex=True)
        self.assertEqual(["a.txt"], [m["path"] for m in found["matches"]])


if __name__ == "__main__":
    unittest.main()
