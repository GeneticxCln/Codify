from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from engine import home
from engine.db import connect
from engine.executor import ExecutorService
from engine.laya import LayaService
from engine.role_repair import plan_role_repair
from engine.stats import build_overview, normalize_window
from engine.stats_history import StatsSnapshotService
from engine.stats_import import StatsImportInvalid, StatsImportService
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
        try:
            port = int(str(env).strip())
        except (TypeError, ValueError):
            raise RuntimeError(f"invalid CODIFY_PORT={env!r}: must be an integer 1024-65535")
        if not 1024 <= port <= 65535:
            raise RuntimeError(f"invalid CODIFY_PORT={port}: must be 1024-65535")
        return port
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
    # One frozen statistics document per past day, written lazily on the first
    # stats read after a day ends (see engine/stats_history.py).
    app.state.stats_snapshots = StatsSnapshotService(conn)
    # The stats-history document the user imported from a file, kept so it
    # survives a restart. Separate from snapshots: an imported day is another
    # machine's measurement, not one this engine froze (see engine/stats_import.py).
    app.state.stats_imports = StatsImportService(conn)
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
    if not expected or not secrets.compare_digest(header, f"Bearer {expected}"):
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
    # Constant-time compare: /health is reachable by any local process, so its
    # answer must not leak the token a character at a time via timing.
    return {"ok": True, "authenticated": secrets.compare_digest(header, f"Bearer {expected}")}


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
    # Where the key ACTUALLY landed — the configured backend can throw at write
    # time and the save silently falls through to the file. `backend` alone
    # would let the UI promise "OS keychain" about a key in a JSON file.
    actual = keychain.last_write_backend()
    # A new key can unlock a whole provider's model list, so don't let the
    # catalog serve the pre-key answer from cache.
    catalog: ModelCatalogService | None = getattr(request.app.state, "models", None)
    if catalog:
        catalog.invalidate()
    return {"ok": True, "provider": body.provider, "storage": actual}


@app.get("/settings/agents", response_model=list[AgentConfig])
async def list_agents(request: Request):
    return request.app.state.registry.list_configs()


@app.get("/settings/agents/stats")
async def agent_call_stats(request: Request, limit: int = Query(20, ge=1, le=100)):
    """What actually happened to each role the last time it ran, and how often.

    The roadmap's "per-agent cost/latency stats on Agents settings cards": read
    from the event log the goals already write, so it covers every goal that
    ever ran — the same source the audit and usage endpoints sweep — with no
    new store and no probing of live providers. A card that claims "last call
    1.2s" is quoting the run that happened, not a health check.

    Registered before /settings/agents/{role} for the same reason /repair is:
    FastAPI matches in declaration order, and "stats" is a valid role shape —
    the parameterised route would swallow this one whole.

    Both an agent's success and its failure land here: a `usage` event records
    a completed call (with its duration), an `agent_call_failed` records one
    that did not. `last` is whichever is newer — a role that only ever fails
    shows its failure, never a comforting blank.
    """
    conn = request.app.state.conn
    types = ("usage", "agent_call_failed")
    placeholders = ",".join("?" for _ in types)
    rows = conn.execute(
        f"""SELECT type, payload, timestamp FROM events
            WHERE type IN ({placeholders})
            ORDER BY timestamp DESC, sequence DESC
            LIMIT ?""",
        (*types, max(1, int(limit)) * 20),
    ).fetchall()

    stats: dict[str, dict] = {
        role: {
            "role": role,
            "last_call": None,
            "calls_seen": 0,
            "failures_seen": 0,
            "last_error": None,
        }
        for role in ROLES
    }

    def _load(raw) -> dict:
        try:
            return json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}

    for row in rows:
        p = _load(row["payload"])
        role = p.get("role")
        if role not in stats:
            continue
        entry = stats[role]
        happened_at = row["timestamp"]
        if row["type"] == "usage":
            entry["calls_seen"] += 1
            duration_ms = p.get("duration_ms")
            if entry["last_call"] is None:
                entry["last_call"] = {
                    # duration_ms may be absent on events written before the
                    # field existed; null reads as "unknown", not "instant".
                    "duration_ms": duration_ms if isinstance(duration_ms, (int, float)) else None,
                    "provider": p.get("provider"),
                    "model": p.get("model"),
                    "at": happened_at,
                }
        else:
            entry["failures_seen"] += 1
            if entry["last_error"] is None:
                entry["last_error"] = {
                    "code": p.get("code"),
                    "message": p.get("message"),
                    "provider": p.get("provider"),
                    "model": p.get("model"),
                    "at": happened_at,
                }

    return {"stats": [stats[role] for role in ROLES], "scanned_events": len(rows)}


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
    # `plan.changed` already means the target is set (see the property in
    # `engine/role_repair.py`); this says so where the target is dereferenced.
    if plan.changed and plan.target is not None:
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
    for ws in ws_service.list_workspaces():
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
        "stats_retention_days": {
            "value": settings.get_int("stats_retention_days"),
            # The band mirrors SettingsService.SPEC's clamp. 0 keeps everything;
            # the max is the bound a fat-fingered "999999" clamps down to.
            "min": 0,
            "max": 730,
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
        if key in ("parallel_width", "stats_retention_days"):
            if isinstance(value, bool):
                raise ApiError(422, "invalid_value", f"{key} must be an integer, not a boolean")
            try:
                out[key] = settings.set_int(key, int(value))
            except (TypeError, ValueError):
                raise ApiError(422, "invalid_value", f"{key} must be an integer")
        else:
            raise ApiError(400, "unknown_setting", f"unknown engine setting: {key}")
    return {"saved": out}


@app.get("/workspaces")
async def list_ws(request: Request):
    return request.app.state.workspaces.list_workspaces()


@app.get("/workspaces/{workspace_id}")
async def get_ws(workspace_id: str, request: Request):
    return request.app.state.workspaces.get(workspace_id)


@app.delete("/workspaces/{workspace_id}")
async def delete_ws(
    workspace_id: str,
    request: Request,
    delete_goals: bool = False,
):
    """Forget a workspace, and with it the goal history recorded against it.

    Declared before nothing in particular (no sibling path shape to shadow) but
    after the GET so the route table reads in CRUD order. Two things this
    deliberately does *not* do, both stated in the response so a caller never
    has to guess:

    - it never touches the directory at `root_path`. This deletes Codify's
      record of a folder, not the folder. A user reading "delete workspace"
      should never have to wonder whether their project just went away.
    - it never deletes goals implicitly. `delete_goals=true` is the explicit
      opt-in, and a workspace with history is a 409 (with the goal count)
      until the caller asks for it, instead of the bare IntegrityError the FK
      used to produce.
    """
    return request.app.state.workspaces.delete(workspace_id, delete_goals=delete_goals)


@app.post("/goals")
async def create_goal(body: GoalCreate, request: Request):
    goal = request.app.state.goals.create(body)
    _spawn(request.app, request.app.state.executor.run_planning(goal.id), goal.id)
    return goal


@app.get("/goals")
async def list_goals(
    request: Request,
    workspace_id: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Goal history, newest first (active goals lead, so a just-dispatched goal
    never sinks under a wall of finished runs).

    The one read the docs' data model always implied and the API never shipped:
    every goal is persisted, but with no list route the only survivors of an app
    restart were the goals still open in a browser tab. `GET /goals/{id}` plus
    `GET /goals/{id}/events?after=0` is the restore path — this route is how a
    client finds out which ids to restore.
    """
    return request.app.state.goals.list_goals(
        workspace_id=workspace_id, status=status, limit=limit, offset=offset
    )


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

    Re-published IN_PROGRESS for a step already running (the role-transition
    heartbeat inside a step) is idempotent here: `add` on a set member. A
    terminal status for a step that was never seen starting (the log opens
    mid-run) discards nothing — `discard` tolerates unknown members.
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


def _usage_from_events(events: list) -> dict:
    """Token totals from the usage events the providers report.

    Providers carry usage fields in every response; the orchestrator records
    them as events so totals come free from the same store that feeds the
    timeline. Per-role and per-model splits show which agent (or which
    fallback target) is actually spending the tokens. Shared by the usage
    endpoint and the audit export so the two can never disagree.
    """
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    by_role: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    calls = 0
    for e in events:
        if e.type != "usage":
            continue
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
    return {"calls": calls, "totals": totals, "by_role": by_role, "by_model": by_model}


@app.get("/goals/{goal_id}/usage")
async def get_goal_usage(goal_id: str, request: Request):
    """Token totals for a goal, from the usage events the providers report.

    The parallel numbers come from the same log: peak steps in flight at once
    (what the width cap actually bounded) and how many waves the plan took.

    An in-flight goal reports its totals up to *now* — the snapshot grows as
    the run spends, matching how the audit endpoint snapshots a live run.
    """
    request.app.state.goals.get(goal_id)  # 404 if unknown
    events = request.app.state.goals.events_after(goal_id, 0)
    usage = _usage_from_events(events)
    parallel = _parallel_peak_from_events(events)
    return {
        "goal_id": goal_id,
        **usage,
        "parallel_peak": parallel["peak"],
        "parallel_waves": parallel["waves"],
    }


@app.get("/goals/{goal_id}/audit")
async def get_goal_audit(goal_id: str, request: Request):
    """The goal's audit trail as one structured document, for export or review.

    Everything auditable about a run, reconstructed from the event log the
    same way the usage endpoint works — so it covers every goal that ever
    ran, with no new event types or schema. Plan edits (with before/after
    values), provider fallbacks (who was skipped, why, who answered instead),
    errors and failures (with the responsible role), fix-retry loops, and
    the outcome/status timeline all come from the same events the chat
    transcript renders; this is the machine-readable form of the same story.
    """
    goals = request.app.state.goals
    goal = goals.get(goal_id)  # 404 if unknown
    events = goals.events_after(goal_id, 0)
    steps = {s.id: s for s in goals.steps(goal_id)}

    def step_label(step_id: str | None) -> str | None:
        if not step_id or step_id not in steps:
            return None
        s = steps[step_id]
        return f"{s.title} ({s.id})"

    plan_edits = []
    fallbacks = []
    fix_retries = []
    errors = []
    status_timeline = []
    step_outcomes: dict[str, dict] = {}
    running: set[str] = set()
    for e in events:
        p = e.payload or {}
        when = e.timestamp
        if e.type == "plan_updated":
            changed = {}
            for field, ch in (p.get("changes") or {}).items():
                if ch.get("before") != ch.get("after"):
                    changed[field] = {"from": ch.get("before"), "to": ch.get("after")}
            if changed:
                plan_edits.append({
                    "step": step_label(e.step_id),
                    "fields": list(changed),
                    "changes": changed,
                    "at": when,
                })
        elif e.type == "provider_fallback":
            fallbacks.append({
                "role": p.get("role"),
                "step": step_label(e.step_id),
                "from": p.get("from"),
                "to": p.get("to"),
                "code": p.get("code"),
                "detail": p.get("detail"),
                "at": when,
            })
        elif e.type == "fix_retry":
            fix_retries.append({
                "step": step_label(e.step_id),
                "attempt": p.get("attempt"),
                "max_attempts": p.get("max_attempts"),
                "reason": p.get("reason"),
                "at": when,
            })
        elif e.type == "error":
            errors.append({
                "step": step_label(e.step_id),
                "role": p.get("role"),
                "code": p.get("code"),
                "message": p.get("message"),
                "at": when,
            })
        elif e.type == "goal_status":
            status_timeline.append({"status": p.get("status"), "version": p.get("version"), "at": when})
        elif e.type == "step_status":
            entry = step_outcomes.setdefault(e.step_id, {})
            entry["step"] = step_label(e.step_id)
            status = p.get("status")
            if status == "IN_PROGRESS":
                # The engine re-publishes IN_PROGRESS at every role transition
                # inside a step (fixer → verifier → critic → scribe); only a
                # start from a not-running state is a real attempt, or every
                # step would report its role count as its attempt count.
                if e.step_id not in running:
                    running.add(e.step_id)
                    entry["attempts"] = entry.get("attempts", 0) + 1
                    entry["started_at"] = when
            elif status in ("COMPLETED", "FAILED", "CANCELLED"):
                running.discard(e.step_id)
                entry["status"] = status
                entry["ended_at"] = when
    # A step open at the end (killed engine) keeps its last known status:
    # report IN_PROGRESS rather than pretending it finished.
    for entry in step_outcomes.values():
        entry.setdefault("status", "IN_PROGRESS")

    parallel = _parallel_peak_from_events(events)
    usage = _usage_from_events(events)

    # Silent roles: the engine announced them (agent_assigned — it decided this
    # role should run) but no model call ever completed (zero usage events).
    # That difference is worth surfacing: it usually means the role was skipped
    # by a guard, its calls failed silently, or a fallback absorbed its work.
    # A role the engine never assigned (e.g. the Laya gate deciding to skip
    # itself) is a deliberate no, not silence — so it never appears here.
    # A goal that is still RUNNING gets no verdict yet: its roles may simply
    # not have taken their turn, and calling that "silent" would cry wolf on
    # every healthy in-flight run. Only terminal goals are judged.
    silent_roles: list[dict] = []
    if goal.status in ("COMPLETED", "FAILED", "CANCELLED"):
        assigned: dict[str, str] = {}
        for e in events:
            if e.type == "agent_assigned" and (e.payload or {}).get("role"):
                role = e.payload["role"]
                model = f"{e.payload.get('provider') or '?'}/{e.payload.get('model') or '?'}"
                # Last assignment wins: a role re-assigned after a retry
                # carries its latest configuration.
                assigned[role] = model
        spent = set(usage.get("by_role", {}))
        silent_roles = [
            {"role": role, "assigned_model": assigned[role]}
            for role in sorted(assigned)
            if role not in spent
        ]

    return {
        "goal_id": goal_id,
        "prompt": goal.title,
        "workspace_id": goal.workspace_id,
        "mode": {
            "plan_only": goal.plan_only,
            "dry_run": goal.dry_run,
            "parallel": goal.parallel,
        },
        "final_status": goal.status,
        "created_at": goal.created_at,
        "status_timeline": status_timeline,
        "plan_edits": plan_edits,
        "fallbacks": fallbacks,
        "fix_retries": fix_retries,
        "errors": errors,
        "step_outcomes": [
            {"step_id": sid, **entry} for sid, entry in step_outcomes.items()
        ],
        "parallel_peak": parallel["peak"],
        "parallel_waves": parallel["waves"],
        # Same aggregation the usage endpoint serves — who spent what, per
        # role and per model (fallback targets show up here too).
        "usage": usage,
        # Roles that were assigned but never completed a model call.
        "silent_roles": silent_roles,
    }


async def _sweep_stats(conn) -> tuple[list[dict], list[dict]]:
    """The raw material the stats views aggregate: goal rows and parsed call
    events. One loader for both the live overview and the snapshot writer, so
    the two can never read different worlds."""
    goal_rows = conn.execute(
        "SELECT id, status, created_at, updated_at FROM goals ORDER BY created_at DESC LIMIT 5000"
    ).fetchall()
    events = conn.execute(
        """SELECT type, payload, timestamp FROM events
           WHERE type IN ('usage', 'agent_call_failed')
           ORDER BY timestamp DESC LIMIT 20000"""
    ).fetchall()
    parsed = []
    for row in events:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        parsed.append({"type": row["type"], "payload": payload, "timestamp": row["timestamp"]})
    return [dict(r) for r in goal_rows], parsed


@app.get("/stats/overview")
async def stats_overview(request: Request, window: int = Query(0)):
    """Cross-goal statistics: outcomes, success rate, spend, and a daily trend.

    The per-goal endpoints answer "what happened in this run"; this answers the
    question that needs many runs — is this setup working, what does it cost,
    is it trending better or worse. Everything is aggregated from the stores
    that already exist (the goals table and the `usage` / `agent_call_failed`
    events), so it covers goals that ran before any of this existed.

    The arithmetic lives in `engine/stats.py` as a pure function over plain
    dicts — same shape as `role_repair` — so the numbers are testable without
    HTTP and cannot drift between how they are computed and how they are tested.

    The first read after a day ends also freezes that day's final document into
    `stats_snapshots` (`engine/stats_history.py`), which is what lets the trend
    survive engine restarts and outlive the log entries it was computed from —
    and enforces the `stats_retention_days` policy (Settings → Engine: how many
    recent days to keep, 0 = keep everything), so the table's growth is a
    decision rather than an accident. A snapshot or prune failure is swallowed:
    history maintenance must never be able to fail the read that happens to
    trigger it.

    `window` is in days: 1, 7, 30, or 0 = all time (the default, and the only
    honest view for a fresh install). An unknown window clamps to 7 rather than
    erroring, because a stats view has no broken state to refuse.
    """
    conn = request.app.state.conn
    goals, parsed = await _sweep_stats(conn)
    now = time.time()

    # Freeze yesterday, once, then enforce retention. The service owns the day
    # boundary and the "already frozen" check; this only decides that a
    # failure to maintain history is not worth failing the present. Deliberately
    # silent: log events live in the per-goal stream, and a snapshot has no
    # goal to attribute itself to. Pruning on read (not only on freeze) is what
    # makes a lowered policy take effect without waiting for tomorrow.
    try:
        snapshots: StatsSnapshotService = request.app.state.stats_snapshots
        snapshots.maybe_snapshot(conn, goals, parsed, now)
        retention = request.app.state.settings.get_int("stats_retention_days")
        snapshots.prune(retention)
    except Exception:
        pass

    window_days = normalize_window(window)
    overview = build_overview(goals, parsed, window_days=window_days, now=now)
    return {**overview, "generated_at": now}


@app.get("/stats/history")
async def stats_history(request: Request, limit: int = Query(120, ge=0, le=730)):
    """One frozen document per past day, oldest first — the long memory.

    `limit=0` returns every stored frozen day, which is the full-history export
    path; a positive limit keeps the chart request bounded.

    Each entry is the complete overview document that day ended with, computed
    by the same engine build that served it live and stored at full fidelity:
    per-role and per-model spend, call durations, and the daily rows are all
    still in there, so a week-old question ("which model was I spending on
    last Tuesday?") stays answerable no matter what has since happened to the
    live log. The current day is deliberately absent — it is still moving and
    belongs to `/stats/overview`.

    `day_stats` is the entry's own calendar day, extracted from the frozen
    document's daily rows and stated explicitly. The document's top-level
    `goals` block is cumulative-to-that-day — reading it as "that day's
    outcomes" is the mistake that made a chart double-count its window — so
    the per-day view the trend charts need is a named field, not an inference.
    """
    snapshots: StatsSnapshotService = request.app.state.stats_snapshots
    days = snapshots.history(limit=limit)
    out = []
    for entry in days:
        doc = entry["document"]
        day_row = next(
            (r for r in doc.get("daily", []) if r.get("date") == entry["day"]), None
        )
        out.append({
            "day": entry["day"],
            "day_stats": day_row
            or {
                # A frozen day always has activity (a day with none gets no
                # row), so this is belt-and-braces — but a zeroed row reads as
                # an honest empty day rather than crashing the chart.
                "date": entry["day"], "created": 0, "succeeded": 0,
                "failed": 0, "cancelled": 0, "total_tokens": 0, "calls": 0,
            },
            **doc,
        })
    return {"days": out}


@app.get("/stats/import")
async def get_stats_import(request: Request):
    """The currently-imported stats-history document, if there is one.

    This is what makes an import survive a restart: the Stats panel asks for it
    on open and gets back the same frozen days it showed before. `imported` is
    false rather than a 404 when nothing is stored, because "no import yet" is
    a normal state the panel renders every day, not a missing resource.
    """
    service: StatsImportService = request.app.state.stats_imports
    stored = service.get()
    if stored is None:
        return {"imported": False, "days": [], "source": None, "imported_at": None}
    return {
        "imported": True,
        "days": stored["days"],
        "source": stored["source"] or None,
        "imported_at": stored["imported_at"],
        "exported_at": stored["exported_at"],
    }


@app.post("/stats/import")
async def post_stats_import(body: dict, request: Request):
    """Validate and persist an exported stats-history document.

    The engine re-validates rather than trusting the client: a hand-edited file,
    a different build of the UI, or a hand-rolled curl must not be able to get
    an unvalidated document into the store where the chart would later render
    it as measured fact. A refusal is a 422 carrying the engine's own reason,
    so the panel can show the same wording its client-side check would have.

    The document *replaces* any previous import rather than merging — one
    imported file at a time, matching what the panel holds.
    """
    service: StatsImportService = request.app.state.stats_imports
    # `source` rides alongside the document rather than inside it: it is a label
    # about the upload, not part of the exported artifact, and must not become
    # part of what gets re-exported.
    document = {k: v for k, v in (body or {}).items() if k != "source"}
    try:
        result = service.replace(document, (body or {}).get("source"))
    except StatsImportInvalid as exc:
        raise ApiError(422, exc.code, exc.message) from exc
    return {"imported": True, **result}


@app.delete("/stats/import")
async def delete_stats_import(request: Request):
    """Forget the current import. Idempotent: clearing an empty import is fine."""
    service: StatsImportService = request.app.state.stats_imports
    return {"imported": False, "cleared": service.clear()}


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


@app.delete("/goals/{goal_id}")
async def delete_goal(goal_id: str, request: Request):
    """Delete a goal and everything recorded about it.

    The event log, plan steps, and dry-run proposals cascade with it. The
    response counts them so the UI can state what went rather than shrugging
    with "deleted".

    Refused while the goal is PLANNING or RUNNING, and re-checked here against
    the executor's live driver set: a coroutine that outlives its row would keep
    publishing events for a goal that no longer exists. The user cancels first,
    which is also the only honest way to stop work already touching files.
    """
    goals = request.app.state.goals
    goal = goals.get(goal_id)  # 404 if unknown
    executor = getattr(request.app.state, "executor", None)
    if goal.status in ("PLANNING", "RUNNING") or (
        executor is not None and executor.is_driving(goal_id)
    ):
        raise ApiError(
            409, "goal_in_progress",
            f"this goal is {goal.status.lower()} — cancel it before deleting it",
            {"goal_id": goal_id, "status": goal.status},
        )
    return goals.delete(goal_id)


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
async def apply_goal(goal_id: str, body: VersionedAction, request: Request):
    """Replay a completed dry-run's proposed changes for real.

    The guards run here, synchronously, so a request that cannot possibly
    succeed answers 409 — the previous shape started a background task and
    returned success unconditionally, so "apply" on a goal with nothing
    proposed looked like it worked and quietly did nothing.

    `expected_version` closes the double-apply window: applying flips the
    goal's version (stored proposals are cleared, status moves), so a second
    apply from a client that planned against the pre-apply view must 409, not
    re-run — two applies would mark a healthy goal FAILED via the guard below.
    """
    goals = request.app.state.goals
    g = goals.get(goal_id)
    if not g.dry_run:
        raise ApiError(409, "not_dry_run", "only dry-run goals can be applied")
    if g.status not in ("COMPLETED", "FAILED"):
        raise ApiError(409, "illegal_status", f"cannot apply from {g.status}")
    if body.expected_version != g.version:
        raise ApiError(409, "version_conflict", "version mismatch", {"current": g.model_dump()})
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
    goals.set_plan_only(goal_id, False)
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
                import sys
                print(f"background task failed with no goal: {exc}", file=sys.stderr)
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

    Only one driver may run a goal at a time: the retry endpoint spawns this
    coroutine too, and two concurrent drivers would re-run the same IN_PROGRESS
    steps — two fixers on the same files, the exact torn write the batching
    gate exists to prevent. The executor's `claim_driver` guard makes a second
    claim a no-op.
    """
    executor = app.state.executor
    if not executor.claim_driver(goal_id):
        executor._log(goal_id, None, "info", "driver already running — join skipped")
        return
    try:
        await _run_steps_locked(app, goal_id)
    finally:
        executor.release_driver(goal_id)


async def _run_steps_locked(app: FastAPI, goal_id: str) -> None:
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
            elif batch:
                await executor.run_step(goal_id, batch[0].id)
            else:
                # An unprovable head step (no suggested paths) runs alone rather
                # than crashing the driver with an IndexError.
                await executor.run_step(goal_id, remaining.pop(0).id)
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
    authenticated = secrets.compare_digest(auth_header, f"Bearer {expected_token}")

    await websocket.accept()

    if not authenticated:
        try:
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            auth_msg = json.loads(msg)
            token = str(auth_msg.get("token") or "")
            if auth_msg.get("type") == "auth" and secrets.compare_digest(token, expected_token):
                authenticated = True
        except Exception:
            pass

    if not authenticated:
        await websocket.close(code=4401)
        return

    # Authenticated, but the goal must exist too — checking only after auth so
    # an unauthenticated peer can't probe goal ids by watching which close code
    # comes back (4401 before auth, 4404 after).
    try:
        websocket.app.state.goals.get(goal_id)
    except ApiError:
        await websocket.close(code=4404)
        return

    after = 0
    try:
        misses = 0
        while True:
            try:
                websocket.app.state.goals.get(goal_id)
            except ApiError:
                await websocket.close(code=4404)
                return
            batch = websocket.app.state.goals.events_after(goal_id, after)[:500]
            for event in batch:
                await websocket.send_text(event.model_dump_json())
                after = event.sequence
            if not batch:
                misses += 1
            else:
                misses = 0
            if misses > 20:
                # Goal deleted mid-loop would otherwise spin forever; re-check
                # above already closes it. Reset counter to keep polling cheap.
                misses = 0
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        pass


def main() -> None:
    import socket

    import uvicorn

    port = pick_port()
    # Where this run's state actually lands, before anything can write. An isolated
    # run says so out loud, and the half-redirected case is called out instead of
    # being discovered later by finding a smoke-test key in a real keychain.
    # stderr, because the Tauri shell parses stdout for the boot handshake.
    print(home.startup_notice(), file=sys.stderr, flush=True)
    # Bind and LISTEN before announcing readiness. The handshake is the boot
    # contract: whoever reads `CODIFY_ENGINE token=… port=…` is promised a
    # connectable socket, and announcing before `listen()` left a window where
    # that promise was false — the desktop shell read "ready", connected, and
    # hit connection-refused (also the source of a 1-in-5 flake in the
    # wire-level stream tests). uvicorn serves the pre-bound socket, so the
    # port is owned by this process end to end.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    print(f"CODIFY_ENGINE token={BOOT_TOKEN} port={port}", flush=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
