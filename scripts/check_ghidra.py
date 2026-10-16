#!/usr/bin/env python3
"""Run `analyze_binary` against a REAL Ghidra and say whether it works (`make check-ghidra`).

The gate drives a stand-in for `analyzeHeadless` (`tests/fake_ghidra.py`), which proves the engine's door but cannot prove
that `engine/ghidra/CodifyExport.java` compiles and runs inside Ghidra. This is that check (`docs/14` §4 item 6). It is
**not** part of `make check` or `make ci`, and it does not skip: with no usable Ghidra it fails, naming why, because a
passing result must mean Ghidra was exercised.

What it does, in a throwaway `CODIFY_HOME` so nothing of yours is touched:

1. analyses a small executable (default: this machine's `true`, or `--binary PATH`) and checks the answer has functions,
   imports and sections, that a second question is served from the cache, and that no run directory is left behind;
2. with `--cancel`, starts an analysis of a larger file (`--big PATH`, default this interpreter), cancels it after a few
   seconds, and checks that the cancel came back promptly and that no process of that run survives it.

Exit 0 when every check passes, 1 when one fails (or Ghidra is unusable), 2 for a bad invocation.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import binary_analysis as ba  # noqa: E402


def say(ok: bool, what: str) -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
    return ok


def processes_under(marker: str) -> list[int]:
    """Pids whose command line mentions `marker` (Linux /proc). A zombie has no command line, so it is not counted."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid():
            try:
                if marker.encode() in (entry / "cmdline").read_bytes():
                    found.append(int(entry.name))
            except OSError:
                continue
    return found


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", help="a small executable to analyse (default: this machine's `true`)")
    parser.add_argument("--cancel", action="store_true", help="also check that a Cancel kills a real run")
    parser.add_argument("--big", help="the larger file for --cancel (default: this Python interpreter)")
    parser.add_argument("--cancel-after", type=float, default=5.0, help="seconds before the cancel (default 5)")
    args = parser.parse_args(argv)

    small = Path(args.binary or shutil.which("true") or "").resolve()
    if not small.is_file():
        print(f"no binary to analyse: {small}", file=sys.stderr)
        return 2

    try:
        install = ba.locate_ghidra()
    except ba.BinaryAnalysisError as exc:
        print(f"FAIL  Ghidra is not usable ({exc.code}): {exc}")
        return 1
    print(f"Ghidra {install.version} at {install.root}")

    results: list[bool] = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        os.environ["CODIFY_HOME"] = str(base / "home")
        workspace = base / "ws"
        workspace.mkdir()
        shutil.copyfile(small, workspace / "target.bin")
        print(f"analysing {small.name} ({small.stat().st_size:,} bytes)")
        try:
            started = time.monotonic()
            summary = ba.analyze_binary(str(workspace), "target.bin")
            took = time.monotonic() - started
            functions = ba.analyze_binary(str(workspace), "target.bin", "functions", limit=5)
            imports = ba.analyze_binary(str(workspace), "target.bin", "imports", limit=5)
            sections = ba.analyze_binary(str(workspace), "target.bin", "sections", limit=5)
        except ba.BinaryAnalysisError as exc:
            print(f"FAIL  the analysis did not run ({exc.code}): {exc}")
            return 1
        results.append(say("functions" in summary and "0 functions" not in summary, f"summary lists functions ({took:.0f} s)"))
        results.append(say("Functions (" in functions and "(0 match)" not in functions, "functions view has rows"))
        results.append(say("Imports (" in imports, "imports view answers"))
        results.append(say("Memory blocks (" in sections and "Memory blocks (0)" not in sections, "sections view has rows"))
        results.append(say("from an earlier run" in imports, "the second question was served from the cache"))
        runs = list((Path(os.environ["CODIFY_HOME"]) / "binary-analysis" / "runs").iterdir())
        results.append(say(not runs, "no run directory is left behind"))

        if args.cancel:
            big = Path(args.big or sys.executable).resolve()
            shutil.copyfile(big, workspace / "big.bin")
            print(f"cancelling an analysis of {big.name} ({big.stat().st_size:,} bytes) after {args.cancel_after:g} s")
            cancel = threading.Event()
            timer = threading.Timer(args.cancel_after, cancel.set)
            timer.start()
            started = time.monotonic()
            code = ""
            try:
                ba.analyze_binary(str(workspace), "big.bin", cancel=cancel)
            except ba.BinaryAnalysisError as exc:
                code = exc.code
            finally:
                timer.cancel()
            took = time.monotonic() - started
            if code == "" :
                results.append(say(False, "the big analysis finished before the cancel; use a larger --big"))
            else:
                results.append(say(code == "cancelled", f"the run ended as cancelled (got {code!r})"))
                results.append(say(took < args.cancel_after + 5, f"and promptly ({took:.1f} s)"))
                deadline = time.monotonic() + 5
                while processes_under(tmp) and time.monotonic() < deadline:
                    time.sleep(0.1)
                results.append(say(not processes_under(tmp), "no process of that run survives the cancel"))

    print("PASS" if all(results) else "FAIL")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
