"""The benchmark harness: run a tier's tasks and report what happened.

Three things this refuses to do, and all three are why the numbers are worth
reading:

* **A canned run never claims task quality.** Checks are declared in the
  manifest as harness checks (did the pipeline run, did bytes reach the disk)
  or quality checks (`file_contains`, `test_command`). Under `CannedProvider`
  the second kind is reported as `skipped`, never as passed — a harness that
  writes the file it then asserts exists has proved nothing about a model.
* **A missing repository is an error, not an empty pass.** `repos` is empty
  until someone runs `vendor.py` deliberately; a tier whose repos are absent
  fails with that fact rather than scoring zero tasks as 100%.
* **A configured run happens outside the user's history.** It reads their
  agent configs and uses their keys through `Keychain`, but writes its goals
  into a scratch database — a benchmark that polluted the run history it is
  supposed to be measuring would be its own kind of broken.

Run it through `make bench-smoke` / `make bench`, or directly:

    python3 -m benchmarks.runner --tier smoke --report /tmp/bench.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import sqlite3
import subprocess  # noqa: S404 — argv lists only, never a shell
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from benchmarks.provider import CannedFactory, CannedProvider
from engine.db import connect, default_db_path
from engine.executor import ExecutorService
from engine.laya import GateCall, LayaDecision, LayaService
from engine.models import ROLES, AgentConfigUpdate, GoalCreate, WorkspaceCreate
from engine.providers import Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.spawn_guard import guarded_argv, guarded_env
from engine.services import (
    AgentRegistryService,
    GoalService,
    SettingsService,
    WorkspaceService,
)
from engine.trace import TraceService

BENCH_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = BENCH_DIR / "manifest.json"

# Checks that measure the harness rather than a model. Everything else in a
# task's `checks` is a quality claim and needs a real provider to be honest.
HARNESS_CHECKS = frozenset({"goal_completed", "stages", "files_written"})


class BenchmarkError(RuntimeError):
    """A benchmark could not be run at all — distinct from a failed task."""


class SkippedGate(LayaService):
    """A benchmark gates nothing.

    The gate's behaviour is covered by docs/05 and `tests/test_laya.py`.
    Re-running it here would inject a decision the run was not asked for, and
    on a machine with neither the SDK nor a model it would skip anyway — a
    reason to leave it out rather than to report it as a result.
    """

    def __init__(self) -> None:
        super().__init__(registry=None)

    async def decide(
        self, state: dict[str, Any], on_call: GateCall | None = None,
    ) -> LayaDecision:
        return LayaDecision(engine="skipped", skipped_reason="benchmarks gate nothing")


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    """Read the manifest. A malformed one is an error, not a zero-task run."""
    if not path.is_file():
        raise BenchmarkError(f"no manifest at {path}")
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f"manifest at {path} is not readable JSON: {exc}") from exc
    if not isinstance(data.get("tasks"), list):
        raise BenchmarkError(f"manifest at {path} has no `tasks` list")
    return data


def resolve_repo(repo_name: str, root: Path = BENCH_DIR) -> Path:
    """Where a task's repository lives.

    Fixtures are committed under `benchmarks/fixtures`. Anything else is a
    vendored repository, absent until `benchmarks.vendor` has been run — which
    says so instead of letting the tier score an empty run as perfect.
    """
    fixture = root / "fixtures" / repo_name
    if fixture.is_dir():
        return fixture
    vendored = root / "repos" / repo_name
    if vendored.is_dir():
        return vendored
    raise BenchmarkError(
        f"repository {repo_name!r} is not available: looked in {fixture} and "
        f"{vendored}. Committed fixtures live in benchmarks/fixtures; third-party "
        "repositories are never committed here and must be fetched deliberately "
        "with `python3 -m benchmarks.vendor`, which prints each repository's "
        "license and writes a NOTICE.md for it. See docs/08-benchmarks.md."
    )


def materialize(repo_name: str, dest: Path, root: Path = BENCH_DIR) -> Path:
    """Copy a task's repository into a scratch directory.

    Always a copy: a benchmark whose fixer writes into the fixture breaks every
    later run, and one that wrote into a user's checkout would be worse than
    having no benchmark at all.
    """
    source = resolve_repo(repo_name, root)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, dirs_exist_ok=True)
    return dest


def split_checks(
    task: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(harness_checks, quality_checks)` — see the module docstring."""
    harness: list[dict[str, Any]] = []
    quality: list[dict[str, Any]] = []
    for check in task.get("checks", []):
        target = harness if check.get("type") in HARNESS_CHECKS else quality
        target.append(check)
    return harness, quality


# How long a task's test command may run, and the code a timeout reports. The
# code is `timeout(1)`'s, matching the sandbox: a timeout is an outcome a reader
# can tell apart from a test failure, not another red test.
TEST_TIMEOUT_S = 120
TIMEOUT_EXIT = 124


def _kill_group(pid: int, sig: int) -> None:
    """Signal the command's whole process group, ignoring one that is already gone."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def _run_test_command(argv: list[str], workspace: Path) -> tuple[int, str]:
    """Run a task's test command under the same guard the sandbox uses.

    The argv is the manifest's — a committed, reviewed file, not model output —
    but the process it starts is a full test suite that can fork, background and
    ignore SIGTERM, which is the hung-command shape this project has already been
    bitten by. So the three facts the sandbox's spawn depends on are reproduced
    exactly: the guard leads, the command leads a session of its own, and a
    timeout kills the group rather than the child.
    """
    if not argv[0] or "/" in argv[0] or "\\" in argv[0]:
        raise BenchmarkError(f"test_command argv[0] must be a basename: {argv[0]!r}")
    env = guarded_env({
        k: os.environ[k]
        for k in ("PATH", "HOME", "LANG", "TERM", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")
        if k in os.environ
    })
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv list, no shell, manifest-owned
            guarded_argv(argv),
            cwd=str(workspace),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise BenchmarkError(f"{argv[0]} could not start: {exc}") from exc

    try:
        stdout, stderr = proc.communicate(timeout=TEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_group(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        tail = f"timed out after {TEST_TIMEOUT_S}s — the whole process group was killed"
        return TIMEOUT_EXIT, f"{stderr or stdout or ''}\n{tail}"
    return proc.returncode, stderr or stdout or ""


def run_check(
    check: dict[str, Any],
    workspace: Path,
    events: list[dict[str, Any]],
    goal_status: str,
    new_files: list[str],
) -> tuple[str, str, str]:
    """Evaluate one check. Returns `(status, detail, kind)`.

    status is `passed` | `failed` | `skipped`; kind is `harness` | `quality`.
    """
    kind = "harness" if check.get("type") in HARNESS_CHECKS else "quality"
    check_type = str(check.get("type"))

    if check_type == "goal_completed":
        if goal_status == "COMPLETED":
            return "passed", "goal reached COMPLETED", kind
        return "failed", f"goal ended {goal_status}", kind

    if check_type == "stages":
        expect = [str(s) for s in check.get("expect", [])]
        got = [
            str(e["payload"].get("stage"))
            for e in events
            if e.get("type") == "stage_result"
        ]
        missing = [s for s in expect if s not in got]
        if missing:
            return "failed", f"stages never ran: {', '.join(missing)}", kind
        return "passed", f"{len(got)} stage results; all {len(expect)} expected ran", kind

    if check_type == "files_written":
        minimum = int(check.get("min", 1))
        if len(new_files) >= minimum:
            shown = ", ".join(new_files[:3])
            return "passed", f"{len(new_files)} new file(s): {shown}", kind
        return "failed", f"{len(new_files)} new file(s), wanted {minimum}+", kind

    if check_type in ("file_exists", "file_contains"):
        target = workspace / str(check["path"])
        if not target.is_file():
            return "failed", f"{check['path']} does not exist", kind
        if check_type == "file_contains":
            wanted = str(check.get("text", ""))
            body = target.read_text(encoding="utf-8", errors="replace")
            if wanted not in body:
                return "failed", f"{check['path']} does not contain {wanted!r}", kind
            return "passed", f"{check['path']} contains {wanted!r}", kind
        return "passed", f"{check['path']} exists", kind

    if check_type == "test_command":
        argv = [str(a) for a in check.get("argv", [])]
        if not argv:
            return "failed", "test_command names no argv", kind
        try:
            returncode, output = _run_test_command(argv, workspace)
        except BenchmarkError as exc:
            return "failed", str(exc), kind
        if returncode == 0:
            return "passed", f"`{' '.join(argv)}` exited 0", kind
        if returncode == TIMEOUT_EXIT:
            return "failed", output.strip() or f"timed out after {TEST_TIMEOUT_S}s", kind
        tail = output.strip().splitlines()[-3:]
        return "failed", f"exit {returncode}: {' / '.join(tail) or 'no output'}", kind

    return "failed", f"unknown check type {check_type!r}", kind


def _read_engine_configs(engine_db: Path) -> list[dict[str, Any]]:
    """Read the engine's agent configs, leaving them untouched.

    Reading and writing are separate because the pre-flight check has no
    business writing anywhere: it only asks which roles have a model.
    """
    if not engine_db.is_file():
        raise BenchmarkError(
            f"no engine database at {engine_db} — a configured tier needs the "
            "models already set up in Settings → Agents, or use `--tier smoke`, "
            "which needs nothing"
        )
    source = sqlite3.connect(engine_db)
    # The engine's own `connect` sets this, and a bare connection hands back
    # plain tuples — which `dict()` rejects rather than misreads.
    source.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in source.execute("SELECT * FROM agent_configs")]
    except sqlite3.Error as exc:
        # A file at the path is not proof it is the engine's store — an empty
        # or foreign database reaches here too, and that is a diagnosis, not a
        # crash.
        raise BenchmarkError(
            f"{engine_db} is not a Codify engine database ({exc}) — pass "
            "--engine-db, or run `--tier smoke`"
        ) from exc
    finally:
        source.close()
    if not rows:
        raise BenchmarkError(f"{engine_db} holds no agent configurations")
    return rows


def _with_model(rows: list[dict[str, Any]]) -> list[str]:
    """The roles among `rows` that have a model named."""
    return [str(r["role"]) for r in rows if str(r.get("model_name") or "").strip()]


def configured_models(engine_db: Path) -> list[str]:
    """Which configured roles have a model — used to fail before spending."""
    return _with_model(_read_engine_configs(engine_db))


def seed_agent_configs(engine_db: Path, conn: sqlite3.Connection) -> list[str]:
    """Copy the engine's agent configs into the scratch store.

    A configured run has to use *the user's* models but must not write its
    goals and events into their history, so the configs come across instead of
    the run happening in place. Keys are never copied: they resolve through
    `Keychain`, which reads the same store the engine reads, so the benchmark
    never handles them.

    Returns the roles that have a model and are ready to run.
    """
    rows = _read_engine_configs(engine_db)
    columns = list(rows[0])
    # S608: these identifiers are the engine's own column names, read from its
    # schema above; every value travels as a bound parameter.
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT OR REPLACE INTO agent_configs ({','.join(columns)}) VALUES ({placeholders})"  # noqa: S608
    for row in rows:
        conn.execute(sql, tuple(row[c] for c in columns))
    conn.commit()
    return _with_model(rows)


def _files(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


async def run_task(
    task: dict[str, Any],
    work_root: Path,
    *,
    canned: bool,
    engine_db: Path | None = None,
    attempt: int = 1,
    trace: bool = False,
) -> dict[str, Any]:
    """Run one task end to end: measurements, harness checks, quality checks.

    `trace` records every model call in the task's own store (`docs/04` §8), which is how a real model's
    raw replies are read afterwards and committed as fixtures.

    `attempt` numbers repeats of the same task, so each gets a workspace and a store of its own: two
    runs sharing either would measure the first run's leftovers.
    """
    started = time.monotonic()
    slug = str(task["id"]) if attempt == 1 else f"{task['id']}-attempt{attempt}"
    workspace = work_root / slug
    materialize(str(task["repo"]), workspace)
    before = _files(workspace)

    conn = connect(work_root / f"{slug}.db")
    try:
        goals = GoalService(conn)
        workspaces = WorkspaceService(conn)
        ws = workspaces.create(
            WorkspaceCreate(name=f"bench-{task['id']}", root_path=str(workspace))
        )

        if canned:
            provider = CannedProvider([dict(w) for w in task.get("canned_write", [])])
            registry = AgentRegistryService(conn, CannedFactory(provider), Keychain())
            for role in ROLES:
                registry.set_config(
                    role, AgentConfigUpdate(provider="ollama", model_name="bench")
                )
        else:
            ready = seed_agent_configs(engine_db or default_db_path(), conn)
            missing = [r for r in ROLES if r not in ready]
            if missing:
                raise BenchmarkError(
                    "these roles have no model configured: "
                    f"{', '.join(missing)} — set them in Settings → Agents, or run "
                    "`--tier smoke`"
                )
            registry = AgentRegistryService(conn, ProviderFactory(Keychain()), Keychain())

        goal = goals.create(
            GoalCreate(
                workspace_id=ws.id,
                title=str(task["title"]),
                description=str(task.get("description", "")),
                trace=trace,
            )
        )
        executor = ExecutorService(
            goals, workspaces, registry, SandboxService(),
            laya=SkippedGate(), tracer=TraceService(conn),
        )
        executor.settings = SettingsService(conn)

        await executor.run_planning(goal.id)
        # The same driver loop `app._run_steps_locked` runs, minus the
        # batching a single-step fixture never needs: run what is left, stop
        # if the goal stopped itself, then close it out. Finishing here is the
        # point — without it the goal sits at RUNNING and `goal_completed`
        # would fail a run that in fact succeeded.
        remaining = [s for s in goals.steps(goal.id) if s.status != "COMPLETED"]
        if remaining and goals.get(goal.id).status != "RUNNING":
            goals.update_status(goal.id, goals.get(goal.id).version, "RUNNING")
        for step in remaining:
            await executor.run_step(goal.id, step.id)
            if goals.get(goal.id).status != "RUNNING":
                break
        if goals.get(goal.id).status == "RUNNING":
            goals.update_status(goal.id, goals.get(goal.id).version, "COMPLETED")

        # Annotated, because the comprehension would otherwise settle on
        # `EventType | dict[str, Any]` and every payload read below would be a
        # union attribute access rather than a dict one.
        events: list[dict[str, Any]] = [
            {"type": e.type, "payload": dict(e.payload)}
            for e in goals.events_after(goal.id, 0)
        ]
        status = goals.get(goal.id).status
    finally:
        conn.close()

    new_files = sorted(_files(workspace) - before)
    harness, quality = split_checks(task)

    checks: list[dict[str, Any]] = []
    for check in harness:
        state, detail, kind = run_check(check, workspace, events, status, new_files)
        checks.append({
            "type": check.get("type"), "kind": kind, "status": state, "detail": detail,
        })

    quality_results: list[dict[str, Any]] = []
    for check in quality:
        if canned:
            quality_results.append({
                "type": check.get("type"),
                "kind": "quality",
                "status": "skipped",
                "detail": "needs a real provider — a canned run cannot claim task quality",
            })
        else:
            state, detail, kind = run_check(check, workspace, events, status, new_files)
            quality_results.append({
                "type": check.get("type"), "kind": kind, "status": state, "detail": detail,
            })

    wall_ms = round((time.monotonic() - started) * 1000)
    stage_ms: dict[str, int] = {}
    tokens = 0
    for event in events:
        if event["type"] != "stage_result":
            continue
        stage = str(event["payload"].get("stage"))
        stage_ms[stage] = int(event["payload"].get("duration_ms") or 0)
        tokens += int(event["payload"].get("tokens") or 0)

    return {
        "id": task["id"],
        "attempt": attempt,
        "tier": task.get("tier"),
        "status": status,
        "wall_ms": wall_ms,
        "stage_ms": stage_ms,
        # Synthetic under the canned provider, and flagged as such so a report
        # cannot be read as a spend measurement when none was taken.
        "tokens": tokens,
        "tokens_are_synthetic": canned,
        "checks": checks,
        "quality_checks": quality_results,
        "call_health": call_health(events),
        "failure": failure_of(events),
        "passed": all(c["status"] == "passed" for c in checks)
        and all(q["status"] != "failed" for q in quality_results),
    }


def call_health(events: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """How the model's calls went, per role, counted from the run's own events.

    `ran` is the number of times the role's stage reported; `failed_calls` counts every call the
    executor recorded as failed; `reasks` counts the ones it answered by asking the same model again
    (`agent_call_failed` with `retrying`) — the measure of how often a reply was not usable the first
    time, which a task's pass/fail cannot show. Roles with nothing to count are absent, not zero.
    """
    ran: dict[str, int] = {}
    reasks: dict[str, int] = {}
    failed: dict[str, int] = {}
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") == "stage_result":
            role = str(payload.get("role") or payload.get("stage"))
            ran[role] = ran.get(role, 0) + 1
        elif event.get("type") == "agent_call_failed":
            role = str(payload.get("role"))
            failed[role] = failed.get(role, 0) + 1
            if payload.get("retrying"):
                reasks[role] = reasks.get(role, 0) + 1
    return {"ran": ran, "reasks": reasks, "failed_calls": failed}


def failure_of(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Why the run failed: its last `error` event, clipped — or None when it recorded none.

    Last, because a role's earlier failures may have been recovered from (a fallback, a re-ask) and the
    one that ended the goal is the one that came last.
    """
    errors = [e for e in events if e.get("type") == "error"]
    if not errors:
        return None
    payload = errors[-1].get("payload") or {}
    return {
        "code": payload.get("code"),
        "role": payload.get("role"),
        "message": str(payload.get("message") or "")[:400],
    }


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a run: harness pass rate, quality outcomes, cost, slow stage."""
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    stage_totals: dict[str, int] = {}
    for result in results:
        for stage, ms in result["stage_ms"].items():
            stage_totals[stage] = stage_totals.get(stage, 0) + ms
    quality = [q for r in results for q in r["quality_checks"]]
    health: dict[str, dict[str, int]] = {}
    for result in results:
        counted = result.get("call_health") or {}
        for field in ("ran", "reasks", "failed_calls"):
            for role, n in (counted.get(field) or {}).items():
                health.setdefault(role, {"ran": 0, "reasks": 0, "failed_calls": 0})[field] += int(n)
    failure_codes: dict[str, int] = {}
    for result in results:
        failure = result.get("failure")
        if failure:
            code = str(failure.get("code"))
            failure_codes[code] = failure_codes.get(code, 0) + 1
    return {
        "tasks": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(100 * passed / total) if total else None,
        "quality_passed": sum(1 for q in quality if q["status"] == "passed"),
        "quality_failed": sum(1 for q in quality if q["status"] == "failed"),
        "quality_skipped": sum(1 for q in quality if q["status"] == "skipped"),
        "tokens": sum(int(r["tokens"]) for r in results),
        "tokens_are_synthetic": any(r["tokens_are_synthetic"] for r in results),
        "wall_ms": sum(int(r["wall_ms"]) for r in results),
        "stage_ms": dict(sorted(stage_totals.items(), key=lambda kv: -kv[1])),
        "call_health": health,
        "failure_codes": failure_codes,
    }


def _run_one(
    task: dict[str, Any], work_root: Path, *, canned: bool, engine_db: Path | None, attempt: int,
    trace: bool = False,
) -> dict[str, Any]:
    """`run_task`, with a crash turned into the failed result it is.

    `BenchmarkError` is not caught: it is a diagnosis of the setup (a role with no model, a missing
    repository), true of every task, and the run should stop and say so. Anything else escaping a task is
    that task's failure — the cause is recorded and the run goes on.
    """
    started = time.monotonic()
    try:
        return asyncio.run(run_task(
            task, work_root, canned=canned, engine_db=engine_db, attempt=attempt, trace=trace,
        ))
    except BenchmarkError:
        raise
    except Exception as exc:  # noqa: BLE001 — whatever a task did, the run records it and continues
        error = f"{type(exc).__name__}: {exc}"
        return {
            "id": task["id"],
            "attempt": attempt,
            "tier": task.get("tier"),
            "status": "ERRORED",
            "error": error,
            "wall_ms": round((time.monotonic() - started) * 1000),
            "stage_ms": {},
            "tokens": 0,
            "tokens_are_synthetic": canned,
            "checks": [{"type": "run", "kind": "harness", "status": "failed", "detail": error}],
            "quality_checks": [],
            "call_health": {"ran": {}, "reasks": {}, "failed_calls": {}},
            "passed": False,
        }


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _percentage(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not 0 <= value <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a benchmark tier.")
    parser.add_argument("--tier", default="smoke", help="manifest tier id (default: smoke)")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--only", default=None, help="run a single task id")
    parser.add_argument(
        "--engine-db",
        type=Path,
        default=None,
        help="engine database to read agent configs from (configured tiers only)",
    )
    parser.add_argument(
        "--repeat",
        type=_positive_int,
        default=1,
        metavar="N",
        help="run every task N times, each in its own workspace: one run of a model is an anecdote",
    )
    parser.add_argument(
        "--min-pass-rate",
        type=_percentage,
        default=None,
        metavar="PCT",
        help="exit 1 when fewer than PCT%% of the runs pass — the regression floor for a recorded baseline",
    )
    parser.add_argument(
        "--record",
        type=Path,
        default=None,
        metavar="DIR",
        help="trace every model call and keep each task's store and workspace in DIR "
             "(so a real model's raw replies can be read, and committed as fixtures)",
    )
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
        tier = manifest.get("tiers", {}).get(args.tier)
        if tier is None:
            known = ", ".join(sorted(manifest.get("tiers", {}))) or "none"
            raise BenchmarkError(f"unknown tier {args.tier!r}; known tiers: {known}")
        tasks = [t for t in manifest["tasks"] if t.get("tier") == args.tier]
        if args.only:
            tasks = [t for t in tasks if t.get("id") == args.only]
            if not tasks:
                raise BenchmarkError(f"no task {args.only!r} in tier {args.tier!r}")
        if not tasks:
            raise BenchmarkError(f"tier {args.tier!r} has no tasks to run")
        canned = str(tier.get("provider", "configured")) == "canned"
        # Everything resolved before anything runs: a tier that fails halfway
        # has spent the budget and said nothing about what it reached.
        for task in tasks:
            resolve_repo(str(task["repo"]))
        if not canned:
            # Fail before spending: discovering missing models after the first
            # goal means paying for that goal to learn a config was absent.
            missing = [r for r in ROLES if r not in configured_models(args.engine_db or default_db_path())]
            if missing:
                raise BenchmarkError(
                    "these roles have no model configured: "
                    f"{', '.join(missing)}. Set them in Settings → Agents, pass "
                    "--engine-db, or run `--tier smoke`."
                )
    except BenchmarkError as exc:
        print(f"benchmark: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"benchmark: {exc}", file=sys.stderr)
        return 2

    work_root = Path(tempfile.mkdtemp(prefix="codify-bench-"))
    try:
        results = []
        for attempt in range(1, args.repeat + 1):
            for task in tasks:
                result = _run_one(
                    task, work_root, canned=canned, engine_db=args.engine_db, attempt=attempt,
                    trace=args.record is not None,
                )
                results.append(result)
                if args.record is not None:
                    # Kept as each task finishes, not only at the end: a run killed part-way (a container
                    # restart, an OOM, a closed terminal) never reaches the `finally` below, and a baseline
                    # that takes an hour on a CPU was lost with it.
                    shutil.copytree(work_root, args.record, dirs_exist_ok=True)
                # As each one finishes, not only in the summary: a slow real model makes a run long, and
                # a run that dies late should leave a record of the tasks that were done.
                label = task["id"] if args.repeat == 1 else f"{task['id']} (run {attempt})"
                print(f"finished {label}: {result['status']}, {'ok' if result['passed'] else 'FAIL'}, "
                      f"{round(result['wall_ms'] / 1000)} s", flush=True)
    finally:
        if args.record is not None:
            # Kept before it is removed: the stores hold the traced calls, the workspaces what was written.
            shutil.copytree(work_root, args.record, dirs_exist_ok=True)
        shutil.rmtree(work_root, ignore_errors=True)

    summary = summarise(results)
    report = {
        "tier": args.tier,
        "provider": "canned" if canned else "configured",
        "summary": summary,
        "tasks": results,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"tier      {args.tier} ({report['provider']} provider)")
    rate = summary["pass_rate"]
    print(f"harness   {summary['passed']}/{summary['tasks']} passed"
          + (f" ({rate}%)" if rate is not None else ""))
    if summary["quality_skipped"]:
        print(f"quality   {summary['quality_skipped']} skipped — "
              "a canned run cannot claim task quality")
    if summary["quality_failed"]:
        print(f"quality   {summary['quality_failed']} failed")
    if summary["quality_passed"]:
        print(f"quality   {summary['quality_passed']} passed")
    if summary["tokens_are_synthetic"]:
        print(f"tokens    {summary['tokens']} (synthetic — not a spend measurement)")
    print(f"wall      {summary['wall_ms']} ms")
    if summary["stage_ms"]:
        slowest = next(iter(summary["stage_ms"]))
        print(f"slowest   {slowest} at {summary['stage_ms'][slowest]} ms")
    if summary["failure_codes"]:
        print(f"failures  {summary['failure_codes']}")
    for role, counts in summary["call_health"].items():
        if counts["reasks"] or counts["failed_calls"]:
            print(f"health    {role}: {counts['reasks']} re-ask(s) and {counts['failed_calls']} failed call(s) "
                  f"over {counts['ran']} run(s)")
    for result in results:
        label = result["id"] if args.repeat == 1 else f"{result['id']} (run {result['attempt']})"
        print(f"  {'ok  ' if result['passed'] else 'FAIL'} {label}")
        # Quality checks too: a task that completes and does the wrong thing fails no harness check, and
        # without these the console said FAIL and nothing beneath it (the reason was in the JSON alone).
        # Only a *failed* quality check: a skipped one (a canned run cannot claim quality) is already
        # counted on the `quality N skipped` line, and listing it under every passing task is noise.
        for check in result["checks"]:
            if check["status"] != "passed":
                print(f"         {check['type']}: {check['detail']}")
        for check in result.get("quality_checks", []):
            if check["status"] == "failed":
                print(f"         {check['type']}: {check['detail']}")

    if args.min_pass_rate is not None:
        actual = 100 * summary["passed"] / summary["tasks"]
        if actual < args.min_pass_rate:
            print(f"floor     {actual:.0f}% of runs passed, below the floor of {args.min_pass_rate:g}%")
            return 1
        print(f"floor     {actual:.0f}% of runs passed, at or above the floor of {args.min_pass_rate:g}%")
        return 0
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
