from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from engine.db import connect
from engine.executor import ExecutorService
from engine.models import (
    AgentConfig,
    AgentConfigUpdate,
    GoalCreate,
    GoalDetail,
    ROLES,
    VersionedAction,
    WorkspaceCreate,
)
from engine.providers import Keychain, ProviderError, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, ApiError, GoalService, WorkspaceService

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
    conn = connect()
    keychain = Keychain()
    factory = ProviderFactory(keychain)
    app.state.conn = conn
    app.state.registry = AgentRegistryService(conn, factory, keychain)
    app.state.workspaces = WorkspaceService(conn)
    app.state.goals = GoalService(conn)
    app.state.sandbox = SandboxService()
    app.state.executor = ExecutorService(app.state.goals, app.state.workspaces, app.state.registry, app.state.sandbox)
    app.state.token = BOOT_TOKEN
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
    expected = getattr(request.app.state, "token", None) or BOOT_TOKEN
    header = request.headers.get("authorization", "")
    if not expected or header != f"Bearer {expected}":
        return JSONResponse({"code": "unauthorized", "message": "missing or invalid token"}, status_code=401)
    return await call_next(request)


@app.exception_handler(ApiError)
async def api_error(_req, exc: ApiError):
    body = {"code": exc.code, "message": exc.message}
    if exc.extra:
        body.update(exc.extra)
    return JSONResponse(body, status_code=exc.status)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/settings/providers")
async def list_providers(request: Request):
    return request.app.state.registry.provider_catalog()


@app.get("/settings/agents", response_model=list[AgentConfig])
async def list_agents(request: Request):
    return request.app.state.registry.list_configs()


@app.get("/settings/agents/{role}", response_model=AgentConfig)
async def get_agent(role: str, request: Request):
    return request.app.state.registry.get_config(role)


@app.put("/settings/agents/{role}", response_model=AgentConfig)
async def put_agent(role: str, patch: AgentConfigUpdate, request: Request):
    return request.app.state.registry.set_config(role, patch)


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


@app.get("/workspaces")
async def list_ws(request: Request):
    return request.app.state.workspaces.list()


@app.get("/workspaces/{workspace_id}")
async def get_ws(workspace_id: str, request: Request):
    return request.app.state.workspaces.get(workspace_id)


@app.post("/goals")
async def create_goal(body: GoalCreate, request: Request):
    goal = request.app.state.goals.create(body)
    asyncio.create_task(request.app.state.executor.run_planning(goal.id))
    return goal


@app.get("/goals/{goal_id}")
async def get_goal(goal_id: str, request: Request):
    g = request.app.state.goals.get(goal_id)
    return GoalDetail(**g.model_dump(), steps=request.app.state.goals.steps(goal_id))


@app.post("/goals/{goal_id}/steps/{step_id}/retry")
async def retry_step(goal_id: str, step_id: str, body: VersionedAction, request: Request):
    g = request.app.state.goals.get(goal_id)
    if g.status not in ("RUNNING", "PAUSED", "FAILED"):
        raise ApiError(409, "illegal_status", f"cannot retry from {g.status}")
    updated_step = await request.app.state.executor.retry_step(goal_id, step_id, body.expected_version)
    asyncio.create_task(_run_steps(request.app, goal_id))
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
    if g.status not in ("RUNNING", "PAUSED", "PENDING"):
        raise ApiError(409, "illegal_status", f"cannot cancel from {g.status}")
    return request.app.state.goals.update_status(goal_id, body.expected_version, "CANCELLED")


@app.get("/goals/{goal_id}/events")
async def goal_events(goal_id: str, request: Request, after: int = Query(0)):
    request.app.state.goals.get(goal_id)
    return request.app.state.goals.events_after(goal_id, after)


@app.post("/goals/{goal_id}/start")
async def start_goal(goal_id: str, body: VersionedAction, request: Request):
    g = request.app.state.goals.get(goal_id)
    if g.status == "PLANNING":
        raise ApiError(409, "illegal_status", "planning is still in progress")
    if g.status not in ("PENDING", "PAUSED"):
        raise ApiError(409, "illegal_status", f"cannot start from {g.status}")
    running = request.app.state.goals.update_status(goal_id, body.expected_version, "RUNNING")
    asyncio.create_task(_run_steps(request.app, goal_id))
    return running


async def _run_steps(app: FastAPI, goal_id: str) -> None:
    for step in app.state.goals.steps(goal_id):
        refreshed = app.state.goals.get(goal_id)
        if refreshed.status != "RUNNING":
            return
        if step.status == "COMPLETED":
            continue
        await app.state.executor.run_step(goal_id, step.id)
        refreshed = app.state.goals.get(goal_id)
        if refreshed.status != "RUNNING":
            return
    g = app.state.goals.get(goal_id)
    if g.status == "RUNNING":
        try:
            app.state.executor._set_status(goal_id, "COMPLETED", None)
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
    print(f"CODIFY_ENGINE token={BOOT_TOKEN} port={port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
