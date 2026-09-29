"""`scripts/doctor.sh` tells the truth about a machine, in both directions.

It exists because the README's prerequisites drifted from what `make ci` needs, and a
checker that says "all good" on a machine that is missing something is worse than the
prose it replaces. So these run the real script (bash, not a reimplementation) against a
machine built from stubs, and most of the cases are the ones where something is *absent*:
each missing piece must be named, must carry the command that fixes it, and must turn the
exit status non-zero.

`DOCTOR_TOOL_PATH` is the seam: it replaces PATH for finding tools only, so a test can
present a machine without `cargo` even when the developer running the suite has one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "doctor.sh"

# Each stub answers the few questions the doctor asks and nothing else.
STUBS: dict[str, str] = {
    # The real interpreter, so `python3 -c '...'` genuinely runs the doctor's probes.
    "python3": f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
    "git": '#!/bin/sh\necho "git version 2.43.0"\n',
    "node": '#!/bin/sh\ncase "$1" in --version) echo v24.21.0;; *) exit "${DOCTOR_STUB_NODE_EXIT:-0}";; esac\n',
    "npm": '#!/bin/sh\necho 10.9.7\n',
    "cargo": '#!/bin/sh\necho "cargo 1.94.1"\n',
    "rustc": '#!/bin/sh\necho "rustc 1.94.1"\n',
    # `pkg-config --exists LIB` fails for every name in DOCTOR_STUB_MISSING_LIBS.
    "pkg-config": (
        '#!/bin/sh\nlib="$2"\n'
        'for m in $DOCTOR_STUB_MISSING_LIBS; do [ "$m" = "$lib" ] && exit 1; done\nexit 0\n'
    ),
    "uv": "#!/bin/sh\nexit 0\n",
    "xvfb-run": "#!/bin/sh\nexit 0\n",
}


class DoctorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            self.install(name, body)

    def install(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def remove(self, name: str) -> None:
        (self.bin / name).unlink()

    def doctor(self, **env: str) -> subprocess.CompletedProcess[str]:
        # No inherited display, so each test states what the machine has.
        base = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        base["DOCTOR_TOOL_PATH"] = str(self.bin)
        base.update(env)
        return subprocess.run(
            [str(SCRIPT)], env=base, capture_output=True, text=True, timeout=60,
        )


class TestAHealthyMachine(DoctorCase):
    def test_everything_present_exits_zero_and_says_what_next(self) -> None:
        done = self.doctor()

        self.assertEqual(0, done.returncode, done.stdout)
        self.assertIn("Everything 'make ci' needs is here", done.stdout)
        self.assertNotIn("MISSING", done.stdout)

    def test_a_display_instead_of_xvfb_is_enough(self) -> None:
        self.remove("xvfb-run")

        done = self.doctor(DISPLAY=":0")

        self.assertEqual(0, done.returncode, done.stdout)


class TestEveryMissingPieceIsNamedAndFixable(DoctorCase):
    def assertMissing(self, done: subprocess.CompletedProcess[str], *needles: str) -> None:
        self.assertEqual(1, done.returncode, "a missing piece must fail the check\n" + done.stdout)
        self.assertIn("MISSING", done.stdout)
        for needle in needles:
            self.assertIn(needle, done.stdout)

    def test_no_cargo_points_at_rustup(self) -> None:
        self.remove("cargo")

        self.assertMissing(self.doctor(DISPLAY=":0"), "cargo/rustc not installed", "rustup")

    def test_a_missing_system_library_is_named_with_its_package_line(self) -> None:
        done = self.doctor(DISPLAY=":0", DOCTOR_STUB_MISSING_LIBS="webkit2gtk-4.1 libsoup-3.0")

        self.assertMissing(done, "webkit2gtk-4.1 libsoup-3.0", "libwebkit2gtk-4.1-dev")
        # The ones that were found are still reported as found.
        self.assertIn("ok       gtk+-3.0", done.stdout)

    def test_the_unverified_package_lines_say_they_are_unverified(self) -> None:
        # Only Debian/Ubuntu names were run against a real build; claiming the same
        # for dnf and pacman would be a claim nobody checked.
        done = self.doctor(DISPLAY=":0", DOCTOR_STUB_MISSING_LIBS="openssl")

        self.assertIn("Fedora (per Tauri's docs, not verified here)", done.stdout)
        self.assertIn("Arch (per Tauri's docs, not verified here)", done.stdout)
        self.assertIn("Debian/Ubuntu (verified)", done.stdout)

    def test_no_display_and_no_xvfb_is_missing(self) -> None:
        self.remove("xvfb-run")

        self.assertMissing(self.doctor(), "no display and no xvfb-run", "xvfb")

    def test_a_node_outside_the_range_says_which_range(self) -> None:
        self.assertMissing(
            self.doctor(DISPLAY=":0", DOCTOR_STUB_NODE_EXIT="1"),
            "outside the range", "22.22.2",
        )

    def test_no_uv_and_no_python310_means_the_floor_leg_cannot_run(self) -> None:
        self.remove("uv")

        self.assertMissing(self.doctor(DISPLAY=":0"), "floor leg", "uv")

    def test_a_python_that_cannot_make_a_venv_names_the_debian_package(self) -> None:
        self.install(
            "python3",
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *ensurepip*) exit 1;;\n'
            '  *version_info*) exit 0;;\n'
            '  *platform*) echo 3.12.0;;\n'
            "esac\n",
        )

        self.assertMissing(self.doctor(DISPLAY=":0"), "cannot create a virtual environment", "python3-venv")

    def test_no_git_is_missing(self) -> None:
        self.remove("git")

        self.assertMissing(self.doctor(DISPLAY=":0"), "git is not installed")

    def test_several_problems_are_all_reported_in_one_run(self) -> None:
        self.remove("cargo")
        self.remove("git")

        done = self.doctor(DISPLAY=":0")

        self.assertEqual(1, done.returncode)
        self.assertIn("cargo/rustc not installed", done.stdout)
        self.assertIn("git is not installed", done.stdout)


class TestItIsWired(unittest.TestCase):
    def test_the_script_is_executable(self) -> None:
        self.assertTrue(os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable")

    def test_the_makefile_has_the_target(self) -> None:
        makefile = (SCRIPT.parent.parent / "Makefile").read_text(encoding="utf-8")
        self.assertRegex(makefile, r"(?m)^doctor:")
        self.assertRegex(makefile, r"(?m)^\tscripts/doctor\.sh$")


if __name__ == "__main__":
    unittest.main()
