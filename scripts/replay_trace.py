#!/usr/bin/env python3
"""Replay a goal's recorded model calls, with no provider in the loop.

Thin CLI wrapper around `engine.replay.replay` (docs/04 §8.4).

Usage:
    python3 scripts/replay_trace.py --goal <goal-id> [--db PATH] \
            [--from TREE] [--into DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from engine.db import default_db_path
from engine.replay import (
    _ReplayFactory as _ReplayFactory,
    _ReplayedGate as _ReplayedGate,
    _gate_verdict as _gate_verdict,
    _stage_sequence as _stage_sequence,
    replay as replay,
)
from engine.services import ApiError
from engine.trace import TraceMismatch

EXIT_OK = 0
EXIT_DIVERGED = 1
EXIT_UNUSABLE = 2


def _print(report: dict[str, Any]) -> None:
    print(f"recorded : {report['recorded_calls']} calls")
    print(f"served   : {report['served_calls']} calls")
    if report.get("gate_calls_replayed"):
        print(
            f"gate     : {report['gate_calls_replayed']} call(s) replayed from the "
            f"recorded verdict"
        )
    if report.get("conductor_calls_skipped"):
        print(
            f"conductor: {report['conductor_calls_skipped']} call(s) recorded, not served — a "
            "replay re-runs the typed role calls, not the tool-calling loop"
        )
    print(f"status   : {report['status']}")
    print(f"scratch  : {report['scratch']}")
    if report["stages"]:
        print("stages   :")
        for stage, outcome in report["stages"]:
            print(f"  {stage:<10} {outcome}")
    print(f"source   : {report['source']}")
    if report["diverged"]:
        print(f"\nDIVERGED: {report['diverged']}", file=sys.stderr)
        print(
            "If this run wrote files, re-run with --from pointing at the tree as "
            "it was before it: the prompts were built from that tree.",
            file=sys.stderr,
        )
    elif report["matched"]:
        print("\nMATCH: every recorded call was served, in order.")
    else:
        print(
            f"\nDIVERGED: {report['recorded_calls']} calls recorded but only "
            f"{report['served_calls']} served — the run stopped short of the "
            "recording.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay a goal's recorded model calls without a provider.",
    )
    parser.add_argument("--goal", required=True, help="the goal id to replay")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="engine database (default: the engine's own)",
    )
    parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        default=None,
        help=(
            "tree to replay against — the workspace as it was when the run "
            "started (default: the recorded goal's own workspace)"
        ),
    )
    parser.add_argument(
        "--into",
        type=Path,
        default=None,
        help="directory to write the replay's copy into (default: a fresh temp dir)",
    )
    args = parser.parse_args(argv)

    db = args.db or default_db_path()
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return EXIT_UNUSABLE

    try:
        conn = sqlite3.connect(f"file:{quote(str(db.resolve()))}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        print(f"could not open database {db}: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        report = asyncio.run(replay(conn, args.goal, args.source, args.into))
    except sqlite3.Error as exc:
        print(f"could not read database {db}: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    except ApiError as exc:
        print(exc.message, file=sys.stderr)
        return EXIT_UNUSABLE
    except TraceMismatch as exc:
        print(f"DIVERGED: {exc}", file=sys.stderr)
        return EXIT_DIVERGED
    finally:
        conn.close()

    _print(report)
    return EXIT_OK if report["matched"] else EXIT_DIVERGED


if __name__ == "__main__":
    raise SystemExit(main())
