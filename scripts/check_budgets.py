#!/usr/bin/env python3
"""Fail when the built UI has grown past a recorded baseline (docs/21 P4).

Reads `ui/dist` after `make build-ui` and compares two sizes with the baselines in `ui/budgets.json`:

* `dist_total_bytes`: every file in the build, and
* `entry_chunk_bytes`: the module script `index.html` loads on first paint, which is what a window must fetch and parse
  before it shows anything. Settings, Stats and the editor load on demand and are not in it.

A budget is a **baseline with a tolerance**, not an absolute limit (docs/21 decision 7): growth over the tolerance fails,
anything under passes, and a build that shrank says so and leaves lowering the baseline to a person. Raising a baseline is
an edit to the JSON, and `reason` and `measured_at` are required for every entry so that the edit has to say why in the
diff a reviewer reads.

No network, no display, no build: it only reads files, so it is the same on every machine. Exit status: 0 within budget,
1 over budget, 2 when it cannot tell (no build, no baseline file, a malformed entry).

    python3 scripts/check_budgets.py [--dist ui/dist] [--budgets ui/budgets.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIST = ROOT / "ui" / "dist"
DEFAULT_BUDGETS = ROOT / "ui" / "budgets.json"

METRICS = ("dist_total_bytes", "entry_chunk_bytes")

# The module script `index.html` boots from. Vite writes it as `<script type="module" crossorigin src="/assets/...">`.
_ENTRY = re.compile(r"""<script\b[^>]*\btype=["']module["'][^>]*\bsrc=["']([^"']+)["']""")


class BudgetError(Exception):
    """The budgets cannot be checked at all: not a failure of the build, a failure to look."""


def measure(dist: Path) -> dict[str, int]:
    """The sizes in bytes of a built UI."""
    index = dist / "index.html"
    if not index.is_file():
        raise BudgetError(f"{index} does not exist; run `make build-ui` first")
    found = _ENTRY.search(index.read_text(encoding="utf-8"))
    if found is None:
        raise BudgetError(f"{index} loads no module script, so there is no entry chunk to measure")
    entry = (dist / found.group(1).lstrip("/")).resolve()
    if not entry.is_relative_to(dist.resolve()) or not entry.is_file():
        raise BudgetError(f"the entry script {found.group(1)!r} is not a file inside {dist}")
    total = sum(path.stat().st_size for path in dist.rglob("*") if path.is_file())
    return {"dist_total_bytes": total, "entry_chunk_bytes": entry.stat().st_size}


def load_budgets(path: Path) -> tuple[float, dict[str, int]]:
    """`(tolerance as a fraction, {metric: baseline bytes})`, validated."""
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BudgetError(f"{path} is not readable JSON: {exc}") from exc
    tolerance = data.get("tolerance_pct")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 100:
        raise BudgetError(f"{path}: tolerance_pct must be a number from 0 to 100")
    budgets = data.get("budgets")
    if not isinstance(budgets, dict):
        raise BudgetError(f"{path} has no `budgets` object")
    baselines: dict[str, int] = {}
    for metric in METRICS:
        entry = budgets.get(metric)
        if not isinstance(entry, dict):
            raise BudgetError(f"{path}: no budget for {metric}")
        baseline = entry.get("baseline")
        if isinstance(baseline, bool) or not isinstance(baseline, int) or baseline <= 0:
            raise BudgetError(f"{path}: {metric}.baseline must be a positive whole number of bytes")
        for required in ("reason", "measured_at"):
            if not isinstance(entry.get(required), str) or not entry[required].strip():
                raise BudgetError(f"{path}: {metric}.{required} is required: a baseline says why it is what it is")
        baselines[metric] = baseline
    unknown = sorted(set(budgets) - set(METRICS))
    if unknown:
        raise BudgetError(f"{path}: unknown budget(s) {unknown}; a budget nothing checks is a promise nothing keeps")
    return tolerance / 100, baselines


def compare(measured: dict[str, int], baselines: dict[str, int], tolerance: float) -> tuple[list[str], list[str]]:
    """`(failures, notes)`. A failure is growth past the tolerance; a note is a build that is well under its baseline."""
    failures: list[str] = []
    notes: list[str] = []
    for metric in METRICS:
        size, baseline = measured[metric], baselines[metric]
        limit = int(baseline * (1 + tolerance))
        change = (size - baseline) / baseline * 100
        if size > limit:
            failures.append(
                f"{metric}: {size:,} bytes is {change:+.1f}% over the baseline {baseline:,} "
                f"(limit {limit:,}, tolerance {tolerance * 100:g}%)"
            )
        elif size < baseline * (1 - tolerance):
            notes.append(f"{metric}: {size:,} bytes is {change:+.1f}% against the baseline {baseline:,}; consider lowering it")
    return failures, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the built UI against its size budgets.")
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST)
    parser.add_argument("--budgets", type=Path, default=DEFAULT_BUDGETS)
    args = parser.parse_args(argv)
    try:
        measured = measure(args.dist)
        tolerance, baselines = load_budgets(args.budgets)
    except BudgetError as exc:
        print(f"budgets: {exc}", file=sys.stderr)
        return 2
    failures, notes = compare(measured, baselines, tolerance)
    for metric in METRICS:
        print(f"budgets: {metric} {measured[metric]:,} bytes (baseline {baselines[metric]:,})")
    for note in notes:
        print(f"budgets: note: {note}")
    for failure in failures:
        print(f"budgets: FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
