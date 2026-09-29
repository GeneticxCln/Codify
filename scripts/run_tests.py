"""Run the Python suite, and refuse to call a dead run a pass.

**Why this exists.** `unittest` prints `Ran N tests in …s` and then `OK` or
`FAILED` only if it reaches the end of the run. A suite that dies on the way
there — `os._exit` from a code path under test, a segfault in a native
dependency, an OOM kill, a `SIGKILL` from a watchdog that reached the wrong
process group — prints neither, and the shell sees the exit code that death
produced. `os._exit(0)` produces **zero**, so `make test-engine` reported success
for a run in which 549 of 1054 tests had never executed. A green gate that means
"the runner got far enough to have an opinion, and the opinion was OK" is the
only kind worth having; "the runner stopped" is not an opinion.

So the exit code is not trusted on its own. The summary line is the runner's own
account of what it did, and this fails when there is no account. It does not
re-run anything and it does not parse individual results: the question is
binary, and a second opinion about *which* tests passed would be a worse place to
start than unittest's.

Arguments are passed through to `python -m unittest` untouched, so this is a
wrapper and not a runner with opinions:

    python3 scripts/run_tests.py discover -s tests -p "test_*.py" -v
"""
from __future__ import annotations

import re
import subprocess
import sys

# unittest writes the summary to stderr. `Ran N tests` and the verdict are
# separate lines and either order of the verdict's own decoration is fine
# ("OK (skipped=1)", "FAILED (failures=1, errors=2)").
_SUMMARY = re.compile(r"^Ran \d+ tests? in ", re.MULTILINE)
_VERDICT = re.compile(r"^(OK|FAILED)\b", re.MULTILINE)


def completed(output: str) -> bool:
    """Whether `output` contains unittest's own account of a finished run.

    Both halves are required. A run that printed `Ran …` and then died during
    teardown has one; a run that was killed mid-test has neither. Requiring only
    the count would pass the first, which is the case this whole file exists for.
    """
    return bool(_SUMMARY.search(output) and _VERDICT.search(output))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        args = ["discover", "-s", "tests", "-p", "test_*.py", "-v"]

    proc = subprocess.run(
        [sys.executable, "-m", "unittest", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)

    if proc.returncode == 0 and not completed(output):
        sys.stderr.write(
            "\n"
            "FATAL: the test process exited 0 without reporting a result.\n"
            f"  unittest printed {len(output.splitlines())} lines and no "
            "'Ran N tests' / OK|FAILED summary,\n"
            "  so it died part-way through (os._exit, a signal, or a killed\n"
            "  process) rather than finishing. That is not a pass, and the\n"
            "  tests after the cut never ran. Look for a code path calling\n"
            "  engine.watchdog.hard_exit, or anything that signals its own\n"
            "  process group.\n"
        )
        return 1
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
