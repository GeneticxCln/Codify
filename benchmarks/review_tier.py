"""The review tier: does a reviewer find a defect that was planted in a diff, and does it cry wolf on a clean one?

A review task is not a goal. The fixer's own output is whatever the model writes today, so a defect cannot be
planted in it; this tier instead hands the reviewer a **fixed diff** against the committed fixture, once through the
standard critic and once through deep review (docs/21 P1), and scores what came back. That measures the reviewers
and not the model that happened to write the change.

Two kinds of task, and the second is what keeps the first honest:

* a **defect** task: the diff carries one planted defect, and the reviewer should name it;
* a **control** task: the diff is correct, and every blocker or major finding, and every request for changes, is a
  false alarm.

Like `repo_scale`, quality checks are `skipped` under the canned provider and never `passed`: a canned reviewer's
reply is written by the harness, so a pass would claim a recall nobody measured. What the canned run does prove is the
wiring and the cost: deep mode recorded its lenses, standard mode recorded none, and neither exceeded its call cap.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from benchmarks.runner import BenchmarkError, SkippedGate, _build_registry, materialize
from engine import review
from engine.db import connect
from engine.executor import ExecutorService
from engine.executor_support import AgentOutputInvalid, CriticRejection
from engine.fs import FileSystemService
from engine.models import GoalCreate, PlanStep, WorkspaceCreate
from engine.providers import ProviderError
from engine.sandbox import SandboxService
from engine.services import GoalService, SettingsService, WorkspaceService
from engine.trace import TraceService

MODES = ("standard", "deep")

# The most model calls one reviewed step may cost in each mode: the critic's own answer plus the two read-only
# command rounds it may ask for, and in deep mode the two model lenses on top. Stated here and in docs/21 as the
# worst case, not the typical one.
CALL_CAPS = {"standard": 3, "deep": 5}

REVIEW_CHECKS = frozenset({
    "max_calls", "lens_events", "review_report_includes", "critic_requests_changes", "critic_approves",
    "no_blocking_findings",
})
# Checks about the harness: they are true or false of the wiring whatever a model said.
HARNESS_REVIEW_CHECKS = frozenset({"max_calls", "lens_events"})


def expand_review_modes(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every review task twice, once per mode, with the mode in its id; other tasks are untouched."""
    out: list[dict[str, Any]] = []
    for task in tasks:
        if task.get("kind") != "review":
            out.append(task)
            continue
        for mode in MODES:
            out.append({**task, "id": f"{task['id']}@{mode}", "mode": mode})
    return out


def _severity_at_least(found: str, floor: str) -> bool:
    ranks = review.SEVERITIES
    return ranks.index(found) <= ranks.index(floor)


def run_review_check(
    check: dict[str, Any], *, mode: str, events: list[dict[str, Any]], decision: str, model_calls: int,
) -> tuple[str, str]:
    """Evaluate one review check: `(status, detail)` with status `passed` or `failed`."""
    kind = str(check.get("type"))
    reports = [e["payload"] for e in events if e["type"] == "review_report"]
    lenses = [e["payload"] for e in events if e["type"] == "review_lens"]
    findings = [f for r in reports for f in r.get("findings", [])]

    if kind == "max_calls":
        cap = check.get("max")
        limit = int(cap[mode] if isinstance(cap, dict) else cap if cap is not None else CALL_CAPS[mode])
        if model_calls <= limit:
            return "passed", f"{model_calls} model call(s), cap {limit}"
        return "failed", f"{model_calls} model call(s), over the cap of {limit}"

    if kind == "lens_events":
        if mode == "standard":
            if lenses or reports:
                return "failed", "standard review recorded lens events; it must record none"
            return "passed", "standard review recorded no lens events"
        names = [e.get("lens") for e in lenses]
        if names == list(review.LENSES) and len(reports) == 1:
            return "passed", "every lens recorded, and one report"
        return "failed", f"lens events were {names} with {len(reports)} report(s)"

    if kind == "review_report_includes":
        if mode == "standard":
            return "skipped", "a standard review has no report to search"
        floor = str(check.get("min_severity", "major"))
        wanted = [str(w).lower() for w in check.get("any_of", [])]
        for f in findings:
            text = " ".join(str(f.get(k, "")) for k in ("scope", "category", "evidence", "recommendation")).lower()
            if _severity_at_least(str(f.get("severity")), floor) and any(w in text for w in wanted):
                return "passed", f"a {f.get('severity')} finding at {f.get('scope')} names it"
        return "failed", f"no {floor}-or-worse finding mentions any of {wanted}"

    if kind == "critic_requests_changes":
        return ("passed", "the critic requested changes") if decision == "request-changes" else (
            "failed", f"the critic's decision was {decision}, not request-changes")

    if kind == "critic_approves":
        return ("passed", "the critic approved a clean change") if decision == "approve" else (
            "failed", f"the critic's decision was {decision} on a clean change")

    if kind == "no_blocking_findings":
        if mode == "standard":
            return "skipped", "a standard review has no report to search"
        loud = [f for f in findings if f.get("severity") in ("blocker", "major")]
        if not loud:
            return "passed", "no blocker or major finding on a clean change"
        return "failed", f"{len(loud)} blocker/major finding(s) on a clean change"

    return "failed", f"unknown review check {kind!r}"


async def run_review_task(
    task: dict[str, Any], work_root: Path, *, canned: bool, engine_db: Path | None = None, attempt: int = 1,
) -> dict[str, Any]:
    """Review one fixed diff in one mode and score it. See the module docstring."""
    mode = str(task["mode"])
    if mode not in MODES:
        raise BenchmarkError(f"unknown review mode {mode!r}; known: {', '.join(MODES)}")
    started = time.monotonic()
    slug = str(task["id"]).replace("@", "-") + ("" if attempt == 1 else f"-attempt{attempt}")
    workspace = work_root / slug
    materialize(str(task["repo"]), workspace)

    conn = connect(work_root / f"{slug}.db")
    decision = "approve"
    reasons: list[str] = []
    try:
        goals = GoalService(conn)
        workspaces = WorkspaceService(conn)
        ws = workspaces.create(WorkspaceCreate(name=f"bench-{slug}", root_path=str(workspace)))
        registry, _provider = _build_registry(conn, task, canned=canned, engine_db=engine_db, conductor=False)
        goal = goals.create(GoalCreate(
            workspace_id=ws.id, title=str(task["title"]), description=str(task.get("description", "")),
        ))
        settings = SettingsService(conn)
        settings.set_int("review_deep", 1 if mode == "deep" else 0)
        executor = ExecutorService(
            goals, workspaces, registry, SandboxService(), laya=SkippedGate(), tracer=TraceService(conn),
        )
        executor.settings = settings
        goals.update_status(goal.id, goals.get(goal.id).version, "RUNNING")
        diffs = [dict(d) for d in task["diff"]]
        step = PlanStep(
            id=f"bench-{slug}", goal_id=goal.id, ordinal=0, title=str(task["title"]),
            description=str(task.get("description", "")), status="PENDING",
            suggested_paths=[str(d["path"]) for d in diffs],
        )
        verdict = {
            "argv": None, "ran": False, "verdict": "skip", "exit_code": None,
            "explanation": "the benchmark hands the reviewer a fixed diff; the review is what is measured",
        }
        try:
            await executor._critic(
                goal.id, step, FileSystemService(str(workspace)), diffs, {}, verdict, ws_root=str(workspace),
            )
        except CriticRejection as exc:
            decision, reasons = "request-changes", [str(r) for r in exc.reasons]
        except (AgentOutputInvalid, ProviderError) as exc:
            decision, reasons = "invalid", [str(exc)]
        events: list[dict[str, Any]] = [
            {"type": e.type, "payload": dict(e.payload)} for e in goals.events_after(goal.id, 0)
        ]
    finally:
        conn.close()

    model_calls = sum(1 for e in events if e["type"] == "usage" and e["payload"].get("role") == "critic")
    is_defect = bool(task.get("defect"))
    harness: list[dict[str, Any]] = []
    quality: list[dict[str, Any]] = []
    for check in task.get("checks", []):
        kind = str(check.get("type"))
        harness_kind = kind in HARNESS_REVIEW_CHECKS
        if not harness_kind and canned:
            quality.append({
                "type": kind, "kind": "quality", "status": "skipped",
                "detail": "needs a real provider — a canned reviewer cannot claim to have found a defect",
            })
            continue
        status, detail = run_review_check(check, mode=mode, events=events, decision=decision, model_calls=model_calls)
        (harness if harness_kind else quality).append({
            "type": kind, "kind": "harness" if harness_kind else "quality", "status": status, "detail": detail,
        })

    findings = [f for e in events if e["type"] == "review_report" for f in e["payload"].get("findings", [])]
    loud = [f for f in findings if f.get("severity") in ("blocker", "major")]
    return {
        "id": task["id"],
        "attempt": attempt,
        "tier": task.get("tier"),
        "driver": "review",
        "conductor_calls": None,
        "status": "REVIEWED",
        "wall_ms": round((time.monotonic() - started) * 1000),
        "stage_ms": {},
        "tokens": 0,
        "tokens_are_synthetic": canned,
        "checks": harness,
        "quality_checks": quality,
        "call_health": {"ran": {}, "reasks": {}, "failed_calls": {}},
        "failure": None,
        "passed": all(c["status"] == "passed" for c in harness) and all(q["status"] != "failed" for q in quality),
        "review": {
            "mode": mode,
            "defect": is_defect,
            "decision": decision,
            "reasons": reasons,
            "model_calls": model_calls,
            "blocking_findings": len(loud),
            "quality_measured": not canned,
        },
    }


def summarise_review(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Recall and false alarms per mode, from the results that carry a `review` block; None when none do.

    Counts, not rates, because a rate over a handful of tasks reads as a precision it does not have. Under a canned
    provider the quality counts are None: nothing was measured, and zero would say something was.
    """
    out: dict[str, Any] = {}
    for mode in MODES:
        rows = [r for r in results if (r.get("review") or {}).get("mode") == mode]
        if not rows:
            continue
        measured = all(r["review"]["quality_measured"] for r in rows)
        defects = [r for r in rows if r["review"]["defect"]]
        controls = [r for r in rows if not r["review"]["defect"]]

        def passed(row: dict[str, Any], kind: str) -> bool:
            return any(q["type"] == kind and q["status"] == "passed" for q in row["quality_checks"])

        out[mode] = {
            "runs": len(rows),
            "defect_runs": len(defects),
            "control_runs": len(controls),
            "model_calls": sum(r["review"]["model_calls"] for r in rows),
            "max_calls_in_a_run": max(r["review"]["model_calls"] for r in rows),
            "critic_caught": sum(1 for r in defects if passed(r, "critic_requests_changes")) if measured else None,
            "report_found": sum(1 for r in defects if passed(r, "review_report_includes")) if measured else None,
            "false_alarms": sum(
                1 for r in controls
                if r["review"]["decision"] != "approve" or r["review"]["blocking_findings"] > 0
            ) if measured else None,
            "quality_measured": measured,
        }
    return out or None
