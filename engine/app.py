from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from engine import home
from engine.db import connect
from engine.executor import ExecutorService
from engine.laya import LayaService
from engine.role_repair import plan_role_repair
from engine.model_catalog import ModelCatalogService
from engine.models import (
    BUILTIN_PROVIDERS,
    ROLE_JOB,
    ROLE_ORDER,
    ROLE_TIMING,
    AgentConfig,
    AgentConfigUpdate,
    GoalCreate,
    GoalDetail,
    PlanStepUpdate,
    ProviderKeyUpdate,
    ROLES,
    VersionedAction,
    WorkspaceCreate,
)
from engine.providers import Keychain, ProviderError, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import (
    AgentRegistryService,
    ApiError,
    GoalService,
    SettingsService,
    WorkspaceService,
)

BOOT_TOKEN = os.environ.get("CODIFY_BOOT_TOKEN") or secrets.token_hex(32)


def pick_port() -> int:
    env = os.environ.get("CODIFY_PORT")
    if env:
        return int(env)
    for port in range(7430, 7441):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port in 7430-7440")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The keychain is built first so a role-id migration (coder→fixer, ...) can
    # carry that role's stored credential onto the new id in the same step.
    keychain = Keychain()
    conn = connect(on_role_migrated=keychain.rename_role_key)
    factory = ProviderFactory(keychain)
    app.state.conn = conn
    app.state.keychain = keychain
    app.state.factory = factory
    app.state.registry = AgentRegistryService(conn, factory, keychain)
    app.state.workspaces = WorkspaceService(conn)
    app.state.goals = GoalService(conn)
    app.state.sandbox = SandboxService()
    app.state.settings = SettingsService(conn)
    app.state.models = ModelCatalogService(app.state.registry, keychain)
    # The gate needs the registry for its LLM fallback when the real Laya SDK is
    # not installed; without it every goal's gate would silently skip.
    app.state.laya = LayaService(registry=app.state.registry)
    app.state.executor = ExecutorService(
        app.state.goals,
        app.state.workspaces,
        app.state.registry,
        app.state.sandbox,
        laya=app.state.laya,
    )
    app.state.executor.settings = app.state.settings
    app.state.token = BOOT_TOKEN

    # ── Rescue goals orphaned by the last process ──────────────────────────
    # A goal in PLANNING or RUNNING when the engine died has no coroutine
    # driving it anymore. PLANNING was the worst wedge: start refuses (planning
    # "in progress" forever), and cancel refused too — so the goal could be
    # neither run nor deleted and its chat entry streamed nothing forever.
    # RUNNING gets the same treatment: the step runner is gone, so a goal that
    # claims to be running is a lie that a fresh boot must correct. Steps of a
    # rescued RUNNING goal keep whatever status they died in (PENDING for
    # un-started steps, IN_PROGRESS/COMPLETED for ones mid-flight); a retry
    # picks up from there. PAUSED is deliberately untouched — it is a state the
    # user chose, and /start handles it.
    rescued = app.state.goals.fail_orphaned_active_goals()
    for goal_id, previous, message in rescued:
        try:
            app.state.executor._log(goal_id, None, "warn", message)
        except Exception:
            pass
        try:
            app.state.executor._fail(
                goal_id, None, "engine_restarted",
                f"engine restarted while this goal was {previous} — it was not running anymore",
                role=None,
            )
        except Exception:
            pass

    # No workspace is created here on purpose.
    #
    # The engine used to auto-seed one from its own working directory whenever the
    # database had none. Two problems with that: the directory the engine runs in
    # is an implementation detail (for a source checkout it is Codify's own tree,
    # so the first prompt would have an agent editing the app itself), and it
    # silently chose a target the user never picked. A workspace is now an
    # explicit choice — the folder picker in the command bar — and a fresh install
    # simply has none until one is chosen, which is what the UI already documented.
    yield
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except Exception:
        pass
    conn.close()


app = FastAPI(title="Codify Engine", lifespan=lifespan)


@app.middleware("http")
async def auth(request: Request, call_next):
    # CORS preflights carry no Authorization header; let them through.
    if request.method == "OPTIONS":
        return await call_next(request)
    expected = getattr(request.app.state, "token", None) or BOOT_TOKEN
    header = request.headers.get("authorization", "")
    if not expected or header != f"Bearer {expected}":
        return JSONResponse({"code": "unauthorized", "message": "missing or invalid token"}, status_code=401)
    return await call_next(request)


# The desktop UI is a separate origin (Vite dev server on :5173, or the Tauri
# webview on tauri://localhost / http://tauri.localhost), so every fetch it
# makes to 127.0.0.1:<port> is a cross-origin request. Browsers block those
# without CORS headers. Allow-list loopback UI origins only — the engine itself
# stays bound to 127.0.0.1 and still requires the bearer token.
#
# NOTE: registered AFTER the auth middleware on purpose. Starlette's
# add_middleware inserts at position 0, so the LAST-registered middleware is
# the OUTERMOST one. CORS must sit outside auth so it can answer preflight
# OPTIONS requests and attach headers to 401 responses.
UI_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=UI_ORIGINS,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(ApiError)
async def api_error(_req, exc: ApiError):
    body = {"code": exc.code, "message": exc.message}
    if exc.extra:
        body.update(exc.extra)
    return JSONResponse(body, status_code=exc.status)


@app.get("/health")
async def health(request: Request):
    # authenticated=False means the request arrived without a valid bearer
    # token — still proof the engine is up (UI uses this as a fallback when
    # Tauri IPC has not delivered the token yet).
    expected = getattr(request.app.state, "token", None) or BOOT_TOKEN
    header = request.headers.get("authorization", "")
    return {"ok": True, "authenticated": header == f"Bearer {expected}"}


@app.get("/settings/providers")
async def list_providers(request: Request):
    return request.app.state.registry.provider_catalog()


@app.get("/settings/laya")
async def laya_status(request: Request):
    """Capability report for the System-1 gate (no model weights loaded here).

    The UI uses this to say *which* engine is gating goals — the real in-process
    Laya SDK, the `laya` role's configured LLM answering the same typed contract,
    or nothing at all.
    """
    laya: LayaService = getattr(request.app.state, "laya", None) or LayaService()
    return laya.status()


@app.get("/settings/keys")
async def get_keys(request: Request):
    keychain: Keychain = getattr(request.app.state, "keychain", None) or Keychain()

    # Where a saved key will actually go. The UI has to state this rather than
    # promise "your OS keychain": on a machine without a usable keyring the key
    # lands in a 0600 file, and quietly doing that would be the kind of lying
    # the settings screen exists to avoid.
    backend = keychain.backend
    return [
        {
            "provider": slug,
            "has_key": keychain.has_provider_key(slug),
            "protocol": meta["protocol"],
            "base_url": meta["base_url"],
            "needs_key": meta["needs_key"],
            "storage": backend,
            "storage_detail": keychain.describes_backend(),
            # Why. `file` means "no usable keyring" *or* "this run was pointed at
            # its own store"; a client that assumes the first one tells an isolated
            # run its machine is missing a keyring.
            "storage_reason": keychain.storage_reason(),
        }
        for slug, meta in BUILTIN_PROVIDERS.items()
    ]


@app.post("/settings/keys")
async def save_key(body: ProviderKeyUpdate, request: Request):
    keychain: Keychain = getattr(request.app.state, "keychain", None) or Keychain()
    try:
        keychain.set_provider_key(body.provider, body.api_key)
    except ProviderError as exc:
        # A machine without a usable OS keyring (headless Linux, no Secret
        # Service, `keyring` not installed) gets a clear, actionable error
        # instead of an unhandled 500 from inside the settings screen.
        raise ApiError(503, exc.code, exc.message) from exc
    # A new key can unlock a whole provider's model list, so don't let the
    # catalog serve the pre-key answer from cache.
    catalog: ModelCatalogService | None = getattr(request.app.state, "models", None)
    if catalog:
        catalog.invalidate()
    return {"ok": True, "provider": body.provider}


@app.get("/settings/agents", response_model=list[AgentConfig])
async def list_agents(request: Request):
    return request.app.state.registry.list_configs()


@app.post("/settings/agents/repair")
async def repair_agents(request: Request):
    """Point the roles that cannot run at a model this engine has discovered.

    One action, and deliberately not "apply one model to every role": a role that
    works is left exactly as it is. The rule lives in `engine/role_repair.py` and
    the response carries the reason for every change *and* every refusal to change,
    so the screen can show what happened instead of asserting success.
    """
    registry: AgentRegistryService = request.app.state.registry
    keychain: Keychain = getattr(request.app.state, "keychain", None) or Keychain()
    catalog_service: ModelCatalogService | None = getattr(request.app.state, "models", None)

    configs = [cfg.model_dump() for cfg in registry.list_configs()]
    key_status = [
        {
            "provider": slug,
            "has_key": keychain.has_provider_key(slug),
            "needs_key": meta["needs_key"],
        }
        for slug, meta in BUILTIN_PROVIDERS.items()
    ]
    catalog = await catalog_service.get(refresh=True) if catalog_service else {
        "models": [], "providers": []
    }

    plan = plan_role_repair(
        configs=configs,
        key_status=key_status,
        provider_status=catalog.get("providers") or [],
        catalog=catalog.get("models") or [],
    )

    repaired: list[dict] = []
    if plan.changed:
        for role, reason in plan.to_repair:
            patch: dict = {"provider": plan.target["provider"], "model_name": plan.target["model"]}
            # The protocol comes from the catalog entry the engine discovered, so a
            # role moved onto a provider speaks that provider's wire format. The
            # endpoint is deliberately NOT sent: a provider switch resets it to the
            # provider's own default, while an unchanged provider keeps whatever
            # endpoint the user configured (a proxy, say).
            protocol = next(
                (
                    m.get("protocol")
                    for m in catalog.get("models") or []
                    if m.get("provider") == plan.target["provider"]
                    and m.get("id") == plan.target["model"]
                ),
                None,
            )
            if protocol:
                patch["protocol"] = protocol
            updated = registry.set_config(role, AgentConfigUpdate(**patch))
            repaired.append(
                {
                    "role": role,
                    "reason": reason,
                    "provider": updated.provider,
                    "model": updated.model_name,
                    "base_url": updated.base_url,
                }
            )
        if catalog_service:
            catalog_service.invalidate()

    # Roles that need fixing and a repair that could not reach them are different
    # outcomes. Reporting both as "nothing needed fixing" would be a green result on
    # a broken install, so the roles left unfixed are named with their reasons.
    unfixable = []
    if not plan.changed:
        unfixable = [
            {"role": role, "reason": reason} for role, reason in plan.to_repair
        ]
        if not unfixable:
            # Nothing was proven broken. A target only matters to roles that need one,
            # so there is no reason to explain the absence of one.
            plan.target_reason = ""

    return {
        "changed": bool(repaired),
        "target": plan.target,
        "target_reason": plan.target_reason,
        "repaired": repaired,
        "unfixable": unfixable,
        "left_alone": [
            {"role": role, "reason": reason} for role, reason in plan.left_alone
        ],
        "notes": plan.notes,
    }


@app.get("/settings/roles")
async def list_roles(request: Request):
    """What each slot is for, and when it runs.

    The settings screen shows this instead of keeping its own copy: two lists in
    two places is how a screen ends up describing abilities the engine no longer
    grants.
    """
    configs = {cfg.role: cfg for cfg in request.app.state.registry.list_configs()}
    return [
        {
            "role": role,
            "display_name": configs[role].display_name if role in configs else role.title(),
            "job": ROLE_JOB.get(role, ""),
            "timing": ROLE_TIMING.get(role, ""),
            "order": index,
        }
        for index, role in enumerate(ROLE_ORDER)
    ]


@app.get("/settings/agents/{role}", response_model=AgentConfig)
async def get_agent(role: str, request: Request):
    return request.app.state.registry.get_config(role)


@app.put("/settings/agents/{role}", response_model=AgentConfig)
async def put_agent(role: str, patch: AgentConfigUpdate, request: Request):
    updated = request.app.state.registry.set_config(role, patch)
    # The role may now point at a different provider/endpoint, which changes
    # which models are discoverable at all.
    catalog: ModelCatalogService | None = getattr(request.app.state, "models", None)
    if catalog:
        catalog.invalidate()
    return updated


@app.post("/settings/agents/{role}/test-connection")
async def test_agent(role: str, request: Request):
    if role not in ROLES:
        raise ApiError(404, "unknown_role", f"Unknown agent role {role}")
    try:
        provider, cfg = request.app.state.registry.get_provider_for(role)
        ok, message = await provider.test_connection(cfg.model_name)
        return {"ok": ok, "message": message}
    except ProviderError as exc:
        return {"ok": False, "message": exc.message}
    except ApiError:
        raise
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@app.post("/workspaces")
async def create_ws(body: WorkspaceCreate, request: Request):
    return request.app.state.workspaces.create(body)


@app.post("/workspaces/browse")
async def browse_workspace(request: Request):
    def _pick():
        code = """
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk
dialog = Gtk.FileChooserNative.new('Select Workspace Directory', None, Gtk.FileChooserAction.SELECT_FOLDER, '_Select', '_Cancel')
res = dialog.run()
if res == Gtk.ResponseType.ACCEPT:
    print(dialog.get_filename())
dialog.destroy()
while Gtk.events_pending():
    Gtk.main_iteration_do(False)
"""
        try:
            p = subprocess.run(["python3", "-c", code], capture_output=True, text=True, timeout=120)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except Exception:
            pass
        return None

    path = await asyncio.to_thread(_pick)
    if not path:
        return {"cancelled": True}

    folder_name = Path(path).name or path
    ws_service: WorkspaceService = request.app.state.workspaces
    for ws in ws_service.list():
        if ws.root_path == path:
            return {"cancelled": False, "workspace": ws.model_dump()}

    new_ws = ws_service.create(WorkspaceCreate(name=folder_name, root_path=path))
    return {"cancelled": False, "workspace": new_ws.model_dump()}


@app.get("/models")
async def list_available_models(request: Request, refresh: bool = False):
    """The model catalog, discovered live from every configured provider.

    `refresh=true` bypasses the short cache; the UI passes it when it opens so a
    model released since the last launch is there without reinstalling anything.
    There is deliberately no fallback list: if discovery finds nothing, the
    answer is nothing, plus the per-provider reason why.
    """
    catalog: ModelCatalogService = getattr(request.app.state, "models", None) or ModelCatalogService(
        request.app.state.registry,
        getattr(request.app.state, "keychain", None) or Keychain(),
    )
    return await catalog.get(refresh=refresh)


@app.get("/models/recent")
async def recent_models(request: Request, limit: int = Query(5, ge=1, le=25)):
    """Models that actually answered recently, newest first, from the event log.

    What the chat's model menu orders by. It is deliberately *not* derived from
    `goals.provider/model`, which record what the command bar asked for: roles run
    on their own configured models, so the two differ whenever a role is configured
    and the intent is stale. A menu that labelled one of those "last run" would be
    naming a model the engine never called.
    """
    return request.app.state.goals.recent_run_models(limit)


@app.get("/settings/engine")
async def get_engine_settings(request: Request):
    """The engine-wide settings screen values, each with its clamp bounds so the
    UI can validate before saving instead of discovering a clamp after."""
    settings: SettingsService = request.app.state.settings
    return {
        "parallel_width": {
            "value": settings.get_int("parallel_width"),
            "min": 1,
            "max": 16,
        },
    }


@app.put("/settings/engine")
async def put_engine_settings(body: dict, request: Request):
    """Persist engine-wide settings. Only known keys are accepted; each clamps
    to its band, and the response echoes what was actually stored so the UI
    shows the truth rather than what the user typed."""
    settings: SettingsService = request.app.state.settings
    out: dict[str, int] = {}
    for key, value in body.items():
        if key == "parallel_width":
            try:
                out[key] = settings.set_int(key, int(value))
            except (TypeError, ValueError):
                raise ApiError(422, "invalid_value", f"{key} must be an integer")
        else:
            raise ApiError(400, "unknown_setting", f"unknown engine setting: {key}")
    return {"saved": out}


@app.get("/workspaces")
async def list_ws(request: Request):
    return request.app.state.workspaces.list()


@app.get("/workspaces/{workspace_id}")
async def get_ws(workspace_id: str, request: Request):
    return request.app.state.workspaces.get(workspace_id)


@app.post("/goals")
async def create_goal(body: GoalCreate, request: Request):
    goal = request.app.state.goals.create(body)
    _spawn(request.app, request.app.state.executor.run_planning(goal.id), goal.id)
    return goal


@app.get("/goals/{goal_id}")
async def get_goal(goal_id: str, request: Request):
    g = request.app.state.goals.get(goal_id)
    return GoalDetail(**g.model_dump(), steps=request.app.state.goals.steps(goal_id))


def _parallel_peak_from_events(events: list) -> dict:
    """Peak step concurrency and wave count, from the step_status log.

    Every step's run is bracketed by IN_PROGRESS/terminal step_status events
    the executor already publishes, so sweeping them in sequence reconstructs
    exactly how many steps were in flight at once — no new event type, no
    schema, and it works retroactively for goals that ran before this existed.

    A step that never gets a terminal status (the goal died mid-step) is
    still open at the sweep's end, so the peak includes it rather than
    under-reporting. Waves count the times a running step *starts while all
    currently-running steps have finished* — a sequential goal of N steps
    reports N waves, a fully-parallel one reports 1.
    """
    active: set[str] = set()
    peak = 0
    waves = 0
    for e in events:
        if e.type != "step_status" or not e.step_id:
            continue
        status = (e.payload or {}).get("status")
        if status == "IN_PROGRESS":
            # A step starting while others already run is the same wave; a
            # start onto an empty set is a new one.
            if not active:
                waves += 1
            active.add(e.step_id)
            peak = max(peak, len(active))
        elif status in ("COMPLETED", "FAILED", "CANCELLED"):
            active.discard(e.step_id)
    # Steps still open at the end (a killed engine) stay counted.
    peak = max(peak, len(active))
    return {"peak": peak, "waves": waves}


@app.get("/goals/{goal_id}/usage")
async def get_goal_usage(goal_id: str, request: Request):
    """Token totals for a goal, from the usage events the providers report.

    Providers carry usage fields in every response; the orchestrator records
    them as events so totals come free from the same store that feeds the
    timeline. Per-role and per-model splits let the user see which agent (or
    which fallback target) is actually spending the tokens.

    The parallel numbers come from the same log: peak steps in flight at once
    (what the width cap actually bounded) and how many waves the plan took.
    """
    request.app.state.goals.get(goal_id)  # 404 if unknown
    events = request.app.state.goals.events_after(goal_id, 0)
    usage_events = [e for e in events if e.type == "usage"]
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    by_role: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    calls = 0
    for e in usage_events:
        p = e.payload or {}
        calls += 1
        for bucket, key in ((by_role, p.get("role") or "unknown"),
                            (by_model, f"{p.get('provider') or '?'}/{p.get('model') or '?'}")):
            b = bucket.setdefault(
                key, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}
            )
            b["input_tokens"] += p.get("input_tokens") or 0
            b["output_tokens"] += p.get("output_tokens") or 0
            b["total_tokens"] += p.get("total_tokens") or 0
            b["calls"] += 1
        # Totals accumulate once per event — not inside the bucket loop, where
        # two buckets would count every event twice.
        totals["input_tokens"] += p.get("input_tokens") or 0
        totals["output_tokens"] += p.get("output_tokens") or 0
        totals["total_tokens"] += p.get("total_tokens") or 0
    parallel = _parallel_peak_from_events(events)
    return {
        "goal_id": goal_id,
        "calls": calls,
        "totals": totals,
        "by_role": by_role,
        "by_model": by_model,
        "parallel_peak": parallel["peak"],
        "parallel_waves": parallel["waves"],
    }


@app.patch("/goals/{goal_id}/steps/{step_id}")
async def patch_step(goal_id: str, step_id: str, body: PlanStepUpdate, request: Request):
    """Edit a plan step's title/description/paths before execution.

    Version-protected like start/pause: expected_version is the goal version
    the client last saw, so concurrent edits can't silently clobber each other.
    """
    patch = body.model_dump(exclude={"expected_version"})
    return request.app.state.goals.update_step(goal_id, step_id, body.expected_version, patch)


@app.post("/goals/{goal_id}/steps/{step_id}/retry")
async def retry_step(goal_id: str, step_id: str, body: VersionedAction, request: Request):
    g = request.app.state.goals.get(goal_id)
    if g.status not in ("RUNNING", "PAUSED", "FAILED"):
        raise ApiError(409, "illegal_status", f"cannot retry from {g.status}")
    updated_step = await request.app.state.executor.retry_step(goal_id, step_id, body.expected_version)
    _spawn(request.app, _run_steps(request.app, goal_id), goal_id)
    return updated_step


@app.post("/goals/{goal_id}/pause")
async def pause_goal(goal_id: str, body: VersionedAction, request: Request):
    g = request.app.state.goals.get(goal_id)
    if g.status != "RUNNING":
        raise ApiError(409, "illegal_status", f"cannot pause from {g.status}")
    return request.app.state.goals.update_status(goal_id, body.expected_version, "PAUSED")


@app.post("/goals/{goal_id}/cancel")
async def cancel_goal(goal_id: str, body: VersionedAction, request: Request):
    g = request.app.state.goals.get(goal_id)
    # PLANNING is cancellable: planning runs in the background, and a goal stuck
    # there (slow planner, or one orphaned before the boot rescue existed) could
    # otherwise be neither started nor stopped. CANCELLED from PLANNING also
    # beats the planning coroutine's own `_set_status(PENDING)` — the runner
    # checks status before that transition and leaves a cancelled goal alone.
    if g.status not in ("PLANNING", "RUNNING", "PAUSED", "PENDING"):
        raise ApiError(409, "illegal_status", f"cannot cancel from {g.status}")
    return request.app.state.goals.update_status(goal_id, body.expected_version, "CANCELLED")


@app.get("/goals/{goal_id}/events")
async def goal_events(goal_id: str, request: Request, after: int = Query(0)):
    request.app.state.goals.get(goal_id)
    return request.app.state.goals.events_after(goal_id, after)


@app.post("/goals/{goal_id}/start")
async def start_goal(goal_id: str, body: VersionedAction, request: Request):
    g = request.app.state.goals.get(goal_id)
    if g.plan_only:
        raise ApiError(
            409,
            "plan_only",
            "goal is plan-only; enable execution first via /goals/{id}/enable-execution",
        )
    if g.status == "PLANNING":
        raise ApiError(409, "illegal_status", "planning is still in progress")
    if g.status not in ("PENDING", "PAUSED"):
        raise ApiError(409, "illegal_status", f"cannot start from {g.status}")
    running = request.app.state.goals.update_status(goal_id, body.expected_version, "RUNNING")
    _spawn(request.app, _run_steps(request.app, goal_id), goal_id)
    return running


@app.post("/goals/{goal_id}/apply")
async def apply_goal(goal_id: str, request: Request):
    """Replay a completed dry-run's proposed changes for real.

    The guards run here, synchronously, so a request that cannot possibly
    succeed answers 409 — the previous shape started a background task and
    returned success unconditionally, so "apply" on a goal with nothing
    proposed looked like it worked and quietly did nothing.
    """
    goals = request.app.state.goals
    g = goals.get(goal_id)
    if not g.dry_run:
        raise ApiError(409, "not_dry_run", "only dry-run goals can be applied")
    if g.status not in ("COMPLETED", "FAILED"):
        raise ApiError(409, "illegal_status", f"cannot apply from {g.status}")
    if not goals.has_proposed_files(goal_id):
        raise ApiError(409, "nothing_to_apply", "dry-run produced no proposed changes")
    _spawn(request.app, request.app.state.executor.apply_goal(goal_id), goal_id)
    return {"applied": True, "goal_id": goal_id}


@app.post("/goals/{goal_id}/enable-execution")
async def enable_execution(goal_id: str, body: VersionedAction, request: Request):
    """Lift the plan-only guard on a goal, leaving it ready to start.

    plan_only itself is not version-protected (it is a pre-execution toggle,
    like dry_run), but expected_version is still validated so a client cannot
    enable execution based on a stale view of the goal.
    """
    goals = request.app.state.goals
    g = goals.get(goal_id)
    if g.status not in ("PENDING", "PAUSED"):
        raise ApiError(409, "illegal_status", f"cannot enable execution from {g.status}")
    # The version is not decoration: a client answering "yes, execute this plan"
    # from a stale view must not silently enable a plan the user has since edited.
    if body.expected_version != g.version:
        raise ApiError(409, "version_conflict", "version mismatch", {"current": g.model_dump()})
    if not g.plan_only:
        return g  # idempotent
    updated = goals.set_plan_only(goal_id, False)
    request.app.state.executor._set_status(goal_id, "PENDING", None)
    return request.app.state.goals.get(goal_id)


def _spawn(app: FastAPI, coro, goal_id: str | None = None) -> None:
    """Run a pipeline coroutine in the background, without losing its failure.

    A bare ``asyncio.create_task`` drops its exception on the floor when nobody
    awaits it, so a crashed planning or execution run left the chat waiting
    forever with nothing explaining why. Failures here are surfaced as an error
    event and fail the goal instead of vanishing into the event loop.
    """

    async def runner() -> None:
        try:
            await coro
        except Exception as exc:  # last line of defence for background work
            if goal_id is None:
                return
            try:
                app.state.executor._fail(
                    goal_id, None, getattr(exc, "code", "internal_error"), str(exc)
                )
            except Exception:
                pass

    asyncio.create_task(runner())


async def _run_steps(app: FastAPI, goal_id: str) -> None:
    """Execute a goal's pending steps, honoring the goal's parallel flag.

    Sequential by default, exactly as before. With parallel=True the steps
    are taken in path-disjoint batches (see ExecutorService._independent_batch)
    and each batch runs concurrently; the shared resources — sandbox and git —
    are serialized inside run_step. A COMPLETED step is skipped in both modes
    (a resumed goal replays only what is left).
    """
    executor = app.state.executor
    remaining = [s for s in app.state.goals.steps(goal_id) if s.status != "COMPLETED"]
    while remaining:
        refreshed = app.state.goals.get(goal_id)
        if refreshed.status != "RUNNING":
            return
        if refreshed.parallel:
            batch = executor._independent_batch(remaining)
            batch_ids = {s.id for s in batch}
            remaining = [s for s in remaining if s.id not in batch_ids]
            if len(batch) > 1:
                try:
                    await executor._run_parallel(goal_id, batch, None)
                except ApiError as exc:
                    # The dispatch re-check found the proof stale (the plan was
                    # edited between batching and dispatch). Not a failure: drop
                    # back into the loop, which re-reads the plan and re-batches
                    # from what is actually stored now.
                    executor._log(goal_id, None, "warn", f"batch refused, re-batching: {exc.message}")
                    remaining = [s for s in app.state.goals.steps(goal_id) if s.status != "COMPLETED"]
                    continue
            else:
                await executor.run_step(goal_id, batch[0].id)
        else:
            step = remaining.pop(0)
            await executor.run_step(goal_id, step.id)
        if app.state.goals.get(goal_id).status != "RUNNING":
            return
    g = app.state.goals.get(goal_id)
    if g.status == "RUNNING":
        try:
            executor._set_status(goal_id, "COMPLETED", None)
        except ApiError:
            # Version conflict: another coroutine already updated the goal status.
            pass


@app.websocket("/ws/goals/{goal_id}")
async def ws_goal(websocket: WebSocket, goal_id: str):
    expected_token = getattr(websocket.app.state, "token", None) or BOOT_TOKEN
    auth_header = websocket.headers.get("authorization", "")
    authenticated = auth_header == f"Bearer {expected_token}"

    await websocket.accept()

    if not authenticated:
        try:
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            auth_msg = json.loads(msg)
            if auth_msg.get("type") == "auth" and auth_msg.get("token") == expected_token:
                authenticated = True
        except Exception:
            pass

    if not authenticated:
        await websocket.close(code=4401)
        return

    after = 0
    try:
        while True:
            for event in websocket.app.state.goals.events_after(goal_id, after):
                await websocket.send_text(event.model_dump_json())
                after = event.sequence
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        pass


def main() -> None:
    import uvicorn

    port = pick_port()
    # Where this run's state actually lands, before anything can write. An isolated
    # run says so out loud, and the half-redirected case is called out instead of
    # being discovered later by finding a smoke-test key in a real keychain.
    # stderr, because the Tauri shell parses stdout for the boot handshake.
    print(home.startup_notice(), file=sys.stderr, flush=True)
    print(f"CODIFY_ENGINE token={BOOT_TOKEN} port={port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
