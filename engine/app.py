from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from engine.db import connect
from engine.executor import ExecutorService
from engine.models import (
    AgentConfig,
    AgentConfigUpdate,
    GoalCreate,
    GoalDetail,
    ProviderKeyUpdate,
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
    app.state.keychain = keychain
    app.state.factory = factory
    app.state.registry = AgentRegistryService(conn, factory, keychain)
    app.state.workspaces = WorkspaceService(conn)
    app.state.goals = GoalService(conn)
    app.state.sandbox = SandboxService()
    app.state.executor = ExecutorService(app.state.goals, app.state.workspaces, app.state.registry, app.state.sandbox)
    app.state.token = BOOT_TOKEN

    # Auto-seed default workspace for cwd if no workspaces exist
    if not app.state.workspaces.list():
        cwd = str(Path.cwd().resolve())
        name = Path(cwd).name or "Codify"
        try:
            app.state.workspaces.create(WorkspaceCreate(name=name, root_path=cwd))
        except Exception:
            pass

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


@app.get("/settings/keys")
async def get_keys(request: Request):
    keychain: Keychain = getattr(request.app.state, "keychain", None) or Keychain()
    from engine.models import BUILTIN_PROVIDERS
    return [
        {
            "provider": slug,
            "has_key": keychain.has_provider_key(slug),
            "protocol": meta["protocol"],
            "base_url": meta["base_url"],
            "needs_key": meta["needs_key"],
        }
        for slug, meta in BUILTIN_PROVIDERS.items()
    ]


@app.post("/settings/keys")
async def save_key(body: ProviderKeyUpdate, request: Request):
    keychain: Keychain = getattr(request.app.state, "keychain", None) or Keychain()
    keychain.set_provider_key(body.provider, body.api_key)
    return {"ok": True, "provider": body.provider}


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
async def list_available_models(request: Request):
    import httpx
    keychain: Keychain = getattr(request.app.state, "keychain", None) or Keychain()
    models = []

    # 1. Check local Ollama models
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get("http://127.0.0.1:11434/api/tags")
            if r.status_code == 200:
                data = r.json()
                for m in data.get("models", []):
                    name = m.get("name")
                    if name and not name.startswith("nomic-embed"):
                        param_size = m.get("details", {}).get("parameter_size", "local")
                        models.append({
                            "id": name,
                            "name": name,
                            "provider": "ollama",
                            "description": f"Ollama local model ({param_size})",
                            "available": True,
                        })
    except Exception:
        pass

    # 2. Cloud models
    cloud_models = [
        {"id": "claude-3-7-sonnet-latest", "name": "Claude 3.7 Sonnet", "provider": "anthropic", "description": "Top-tier coding & CoT reasoning"},
        {"id": "claude-3-5-sonnet-latest", "name": "Claude 3.5 Sonnet", "provider": "anthropic", "description": "High-speed precision coding"},
        {"id": "gpt-4o", "name": "GPT-4o", "provider": "openai", "description": "OpenAI flagship multi-modal"},
        {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "provider": "openai", "description": "Fast & cost-effective"},
        {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash", "provider": "google", "description": "Ultra low latency & high speed"},
        {"id": "gemini-1.5-pro", "name": "Gemini 1.5 Pro", "provider": "google", "description": "2M token context window"},
        {"id": "deepseek-chat", "name": "DeepSeek V3", "provider": "deepseek", "description": "High-performance open weights API"},
        {"id": "deepseek-reasoner", "name": "DeepSeek R1", "provider": "deepseek", "description": "CoT reasoning & math"},
    ]

    for cm in cloud_models:
        has_key = keychain.has_provider_key(cm["provider"])
        models.append({
            **cm,
            "available": has_key,
        })

    return models


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
