from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from types import FrameType
from typing import Any, TypeVar
from urllib.parse import urlparse

from fastapi import FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from engine import capabilities, home, speech, watchdog
from engine.db import connect
from engine.executor import ExecutorService
from engine.laya import LayaService
from engine.role_repair import plan_role_repair
from engine.spawn_guard import guarded_argv, guarded_env
from engine.stats import build_overview, normalize_window
from engine.trace import TraceService
from engine.webview_bridge import BridgeAnswer, WebviewBridge
from engine.metrics import (
    STAGE_SUCCESS_OUTCOMES,
    failure_breakdown,
    role_success_rate,
    stage_costs,
)
from engine.stats_history import StatsSnapshotService
from engine.stats_import import StatsImportInvalid, StatsImportService
from engine.model_catalog import ModelCatalogService
from engine.catalog_watch import CatalogWatch
from engine.models import (
    BUILTIN_PROVIDERS,
    ErrorBody,
    ROLE_JOB,
    ROLE_ORDER,
    ROLE_TIMING,
    AgentConfig,
    AgentConfigUpdate,
    Conversation,
    ConversationCreate,
    ConversationTurn,
    ConversationUpdate,
    Event,
    Goal,
    GoalConversationUpdate,
    GoalCreate,
    GoalDetail,
    PlanStep,
    PlanStepUpdate,
    ShellTab,
    ShellTabWrite,
    ProviderKeyUpdate,
    ROLES,
    SpeakRequest,
    TurnCreate,
    VersionedAction,
    TraceToggle,
    Workspace,
    WorkspaceCreate,
    WorkspaceDesignContract,
)
from engine.providers import Keychain, ProviderError, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import (
    AgentRegistryService,
    ApiError,
    ConversationService,
    GoalService,
    SettingsService,
    ShellTabService,
    WorkspaceService,
)

# Stable across restarts of the same state directory, so a client that cached
# it stays authenticated; `home.boot_token` says why, and what it costs.
BOOT_TOKEN = os.environ.get(home.ENV_BOOT_TOKEN) or home.boot_token()


def pick_port() -> int:
    env = os.environ.get("CODIFY_PORT")
    if env:
        try:
            port = int(str(env).strip())
        except (TypeError, ValueError) as err:
            raise RuntimeError(
                f"invalid CODIFY_PORT={env!r}: must be an integer 1024-65535"
            ) from err
        if not 1024 <= port <= 65535:
            raise RuntimeError(f"invalid CODIFY_PORT={port}: must be 1024-65535")
        return port
    for port in ENGINE_PORTS:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port in 7430-7440")


ENGINE_PORTS = range(7430, 7441)


def _bind_listening(port: int) -> tuple[socket.socket, int]:
    """Bind and listen on `port`, or — when the port was ours to choose — on the next free one.

    `pick_port` binds and releases, so anything that takes the port before this runs (a second
    engine starting in the same instant, an unrelated program) used to crash the boot with
    "Address already in use" while the next of the eleven ports the engine may use was free. A
    port the caller *asked for* (`CODIFY_PORT`) is a request and is never swapped: an engine
    quietly serving somewhere nobody expects it is worse than one that says it could not start.
    """
    requested = bool(os.environ.get("CODIFY_PORT"))
    candidates = [port] if requested else [port, *(p for p in ENGINE_PORTS if p != port)]
    failure = OSError("no port was tried")  # replaced by the first real failure; candidates is never empty
    for candidate in candidates:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", candidate))
            sock.listen(128)
        except OSError as exc:
            sock.close()
            failure = exc
            continue
        return sock, candidate
    raise failure


# Shutdown's WAL checkpoint is a tidy-up — it truncates the log file — and SQLite
# recovers a WAL on the next open regardless, so it must never be able to hold the
# process. `TRUNCATE` waits for every reader to release the log, and a reader is
# exactly what a cancelled turn leaves behind; `db.connect`'s five-second timeout was
# inherited here, which is longer than the whole shutdown grace it was hiding in.
SHUTDOWN_CHECKPOINT_BUSY_MS = 250


def _truncate_wal(conn: sqlite3.Connection) -> None:
    """Truncate the WAL on the way out, best-effort and bounded.

    A checkpoint that cannot get the log within a quarter of a second gives up: the
    cost of giving up is a larger `-wal` file that the next open recovers from, and
    the cost of waiting is an engine that outlives the app that stopped it.
    """
    try:
        conn.execute(f"PRAGMA busy_timeout = {SHUTDOWN_CHECKPOINT_BUSY_MS}")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The keychain is built first so a role-id migration (coder→fixer, ...) can
    # carry that role's stored credential onto the new id in the same step.
    keychain = Keychain()
    conn = connect(on_role_migrated=keychain.rename_role_key)
    # Closed on every road out: a service constructor or the orphan rescue raising before the app
    # starts, or a shutdown step raising after it stopped, used to leave the store's handle open
    # (review of 2026-09-29, finding 5).
    try:
        async with _serve(app, keychain, conn):
            yield
    finally:
        try:
            _truncate_wal(conn)
        finally:
            conn.close()


@asynccontextmanager
async def _serve(app: FastAPI, keychain: Keychain, conn: sqlite3.Connection) -> AsyncIterator[None]:
    """Wire the services onto `app.state`, run the engine, and unwire it — with the store already open."""
    factory = ProviderFactory(keychain)
    app.state.conn = conn
    app.state.keychain = keychain
    app.state.factory = factory
    app.state.registry = AgentRegistryService(conn, factory, keychain)
    app.state.workspaces = WorkspaceService(conn)
    # Threads of turns — what a tab points at. Separate from `goals` because a
    # goal is one run and a conversation is the question several runs answer.
    app.state.conversations = ConversationService(conn)
    # The open tabs and their order — the one piece of the window's state that
    # means something outside the window, so a second window opens onto the same
    # strip. What each tab is *showing* stays in the window that shows it.
    app.state.shell_tabs = ShellTabService(conn)
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
    # Load the gate's checkpoints in the background, so the first message does not
    # pay for it. Off the event loop and never awaited: the engine's readiness
    # (the handshake the desktop shell waits for) does not depend on it.
    app.state.laya.start_warming()
    # Trace recording (docs/04 §8). Attached whether or not a goal asked for
    # it: the service is a reader of `goals.trace`, and a goal that has not
    # asked records nothing. Wired here rather than per-goal so switching
    # tracing on mid-run takes effect on the very next call.
    app.state.traces = TraceService(conn)
    # The queue of page questions the shell is polling for (docs/04 §9). One
    # instance, shared: the routes and the executor's `read_page` must be
    # looking at the same pending set, or a read waits on a future nobody holds.
    app.state.bridge = WebviewBridge()
    app.state.executor = ExecutorService(
        app.state.goals,
        app.state.workspaces,
        app.state.registry,
        app.state.sandbox,
        laya=app.state.laya,
        tracer=app.state.traces,
        bridge=app.state.bridge,
    )
    app.state.executor.settings = app.state.settings
    app.state.token = BOOT_TOKEN

    # Engine-level WebSocket connections, each with a queue the watcher can hand a
    # frame to. One queue per connection rather than a send from the watcher: two
    # tasks writing one socket is how frames interleave halfway through.
    # (Annotated on the local, not the attribute: mypy refuses a type declaration
    # in an assignment to a non-`self` attribute, and the readers of it annotate
    # their own local the same way `/models` does.)
    engine_conns: set[asyncio.Queue[str]] = set()
    app.state.engine_conns = engine_conns

    def _publish_engine_event(frame: dict[str, Any]) -> None:
        text = json.dumps(frame)
        for queue in list(engine_conns):
            # A full queue means a client that is connected and not reading. It is
            # dropped rather than awaited: the next frame supersedes this one, and
            # blocking here would hold up the sweep for every other client.
            try:
                queue.put_nowait(text)
            except asyncio.QueueFull:
                pass

    app.state.catalog_watch = CatalogWatch(app.state.models, publish=_publish_engine_event)
    # On state, not just in the closure: the fan-out is the thing a test needs to
    # reach, and a publisher that can only be called from inside the lifespan is a
    # publisher whose only test is the whole engine booting.
    app.state.publish_engine_event = _publish_engine_event

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

    # Started here, before the yield, so the watcher is running for the whole life
    # of the app. It asks nothing until a client subscribes
    # (`engine/catalog_watch.py`), so a quiet engine spends nothing on it.
    watch_task = asyncio.create_task(app.state.catalog_watch.run())
    app.state.catalog_watch_task = watch_task
    # One recorder for the engine's life (engine/speech.py): a microphone left open by a closed
    # window is closed here, and its file deleted, rather than left to the two-minute cap.
    app.state.recorder = speech.Recorder()
    try:
        yield
    finally:
        await asyncio.to_thread(app.state.recorder.discard)
        # Stopped before the database closes, and before the socket does: a task
        # that outlived the state directory it was watching for is an orphan with a
        # publish callback into a store that is being closed underneath it.
        watch_task.cancel()
        try:
            await watch_task
        except asyncio.CancelledError:
            pass
        # A goal that was RUNNING a moment ago is not running now. The coroutine
        # driving it stops existing the moment this process does, and nothing else
        # will ever say so: the goal sits in the database claiming to be mid-run
        # until the next boot's rescue rewrites it, which is a post-mortem rather
        # than a record. Recording it here is why the desktop shell asks this
        # process to stop before it kills it (`docs/09` §5.4.1) — a hard exit skips
        # this block entirely, and then the boot-time rescue is all there is.
        for goal_id, previous, message in app.state.goals.fail_orphaned_active_goals(
            "the engine is shutting down"
        ):
            try:
                app.state.executor._log(goal_id, None, "warn", message)
            except Exception:
                pass
            try:
                app.state.executor._fail(
                    goal_id, None, "engine_interrupted",
                    f"the engine shut down while this goal was {previous} — "
                    "it was not running anymore",
                    role=None,
                )
            except Exception:
                pass


# The engine's refusals, declared once and applied to every route by
# `FastAPI(responses=…)`. Declared at the app rather than repeated on every
# decorator because the alternative does not survive: forty-odd routes each
# carrying their own `responses={...}` is forty-odd chances to forget one, and a
# forgotten one is a refusal the schema does not describe — which is exactly how
# `ErrorBody` ended up declared in `models.py` and referenced by nothing.
#
# Every status the engine raises is here. `test_error_contract.py` scans
# `engine/` for `ApiError(<status>` and fails if a status is raised that is not
# declared below, so the list cannot quietly fall behind the code.
#
# 422 is not an exception to the rule, because the engine now answers a rejected
# body in the same shape as every other refusal (see `request_validation_error`
# below) — with the field-level errors kept as an extra. That was worth doing
# rather than documenting a second shape: the UI's error reader handles `detail`
# only as a string, so FastAPI's default array was a bare "HTTP 422" on screen.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "model": ErrorBody,
        "description": "Understood and refused — an invalid root, a path that escapes, a refused pin.",
    },
    401: {
        "model": ErrorBody,
        "description": "Missing or invalid boot token (docs/00 §6.3). Every request requires it.",
    },
    404: {
        "model": ErrorBody,
        "description": "No such workspace, goal or step.",
    },
    409: {
        "model": ErrorBody,
        "description": (
            "The resource moved under the caller, or refuses in its current state: "
            "`version_conflict`, `illegal_status`, `trace_locked`, `goal_in_progress`."
        ),
    },
    422: {
        "model": ErrorBody,
        "description": (
            "A refused body: a field that failed validation (`code: invalid_request`, "
            "with the field errors under `detail`) or a body the engine understood "
            "and declined."
        ),
    },
    502: {
        "model": ErrorBody,
        "description": (
            "A provider the engine called on the caller's behalf refused or could not be reached; "
            "the code is the provider error's own (`provider_http`, `provider_unreachable`, …) and the "
            "message carries the provider's reason with any credential redacted. Raised by the voice "
            "routes (`/audio/dictation/stop`, `/audio/speak`)."
        ),
    },
    503: {
        "model": ErrorBody,
        "description": "A dependency is unavailable — a credential that cannot be stored, for instance.",
    },
}

app = FastAPI(title="Codify Engine", lifespan=lifespan, responses=ERROR_RESPONSES)


def _same_secret(offered: str, expected: str) -> bool:
    """Constant-time equality for a credential the *client* chose the bytes of.

    `secrets.compare_digest` refuses two `str` arguments when either has a
    non-ASCII character and raises `TypeError` instead of answering. A header
    value or a websocket auth message is attacker-chosen, so the raise escaped
    the auth check: an unauthenticated request with `Authorization: Bearer é`
    got a 500 and a traceback from every route, `/health` included, where it
    should have got a 401. Comparing the UTF-8 bytes keeps the constant-time
    property and cannot raise on content.
    """
    return secrets.compare_digest(offered.encode("utf-8"), expected.encode("utf-8"))


@app.middleware("http")
async def auth(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    # CORS preflights carry no Authorization header; let them through.
    if request.method == "OPTIONS":
        return await call_next(request)
    expected = getattr(request.app.state, "token", None) or BOOT_TOKEN
    header = request.headers.get("authorization", "")
    if not expected or not _same_secret(header, f"Bearer {expected}"):
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
    # The IPv6 loopback, because the dev server binds it and a browser that
    # lands on `http://[::1]:5173` — address bar, bookmark, or a restored tab —
    # sends that literal as its Origin. `localhost` and `[::1]` are different
    # origins to a browser (separate localStorage, separate Origin header), so
    # "the localhost URL works" does not cover this one: without it every
    # request from such a tab dies at preflight and the app shows Offline
    # behind a perfectly healthy engine. Pinned in tests/test_cors.py.
    "http://[::1]:5173",
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
async def api_error(_req: Request, exc: ApiError) -> JSONResponse:
    body = {"code": exc.code, "message": exc.message}
    if exc.extra:
        body.update(exc.extra)
    return JSONResponse(body, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def request_validation_error(
    _req: Request, exc: RequestValidationError,
) -> JSONResponse:
    """Answer a malformed body in the engine's shape, not FastAPI's.

    FastAPI's default for a rejected body is `{"detail": [...]}`, which broke the
    promise the rest of the API makes: a caller that reads `code` and `message`
    got neither, and the UI — whose reader handles `detail` only as a *string* —
    fell through to showing a bare "HTTP 422". The field-level errors were there
    all along in an array nobody was reading.

    So they are kept, as an extra on the declared shape. `ErrorBody` allows
    extras precisely for this: `code` and `message` are what every caller may
    rely on, and the refusal may attach the facts behind it. The result is that
    every error this API can return is one documented shape, and a validation
    failure names the fields that were rejected.
    """
    # `jsonable_encoder`, as FastAPI's own handler does: pydantic keeps the live exception a
    # validator raised in `ctx["error"]`, and `JSONResponse` cannot serialise it. Without this
    # the one custom validator the engine has — the refusal that enforces invariant 8 — was
    # answered with HTTP 500 instead of the 422 it was written to produce.
    errors = jsonable_encoder(exc.errors())
    fields: list[str] = []
    for err in errors:
        loc = [str(part) for part in err.get("loc", []) if part != "body"]
        fields.append(".".join(loc) or "body")
    shown = ", ".join(fields[:5])
    if len(fields) > 5:
        shown += f" (+{len(fields) - 5} more)"
    return JSONResponse(
        {
            "code": "invalid_request",
            "message": (
                f"{len(errors)} field(s) rejected by validation: {shown}"
                if fields
                else "the request body was rejected by validation"
            ),
            # Kept verbatim, so nothing a caller could have learned from
            # FastAPI's own report is lost by answering in our shape.
            "detail": errors,
        },
        status_code=422,
    )


@app.get("/health")
async def health() -> dict[str, bool]:
    # Reaching this handler at all means the request carried the boot token: the
    # auth middleware answers every other caller with a 401 before routing. The UI
    # reads that 401 as "engine up, token stale" (`checkEngineHealth` in api.ts),
    # so `authenticated` here is always true. It stays in the body because that is
    # the shape clients already parse; it used to be recomputed here, which made
    # the `false` branch unreachable code that looked like a second line of defence.
    return {"ok": True, "authenticated": True}


@app.get("/settings/providers")
async def list_providers(request: Request) -> dict[str, Any]:
    registry: AgentRegistryService = request.app.state.registry
    return registry.provider_catalog()


@app.get("/settings/laya")
async def laya_status(request: Request) -> dict[str, Any]:
    """Capability report for the System-1 gate (no model weights loaded here).

    The UI uses this to say *which* engine is gating goals — the real in-process
    Laya SDK, the `laya` role's configured LLM answering the same typed contract,
    or nothing at all.
    """
    laya: LayaService = getattr(request.app.state, "laya", None) or LayaService()
    return laya.status()


@app.get("/settings/runtime")
async def runtime_capabilities() -> dict[str, Any]:
    """What this engine is running as, and what that interpreter can import.

    Separate from `/settings/laya` on purpose. That route answers "which engine is
    gating goals" and honours `CODIFY_LAYA_SDK`; this one answers "which interpreter
    is this, and could it use the SDK at all". Collapsing them into one number is how
    a UI ends up contradicting itself — the gate can be off because it was told to be,
    or because the package is not there, and those need different fixes.

    No request state: the answer is a property of the process, so it is the same
    every call and worth having even when the gate is healthy.
    """
    return capabilities.capability_report()


def _bridge(request: Request) -> WebviewBridge:
    """The one bridge, created on first use rather than only at lifespan.

    The tests that mount the app without booting it — which is most of them —
    reach a route before any lifespan has run, and a route that raised
    `AttributeError` there would be testing the harness rather than the
    bridge. Same shape as `/settings/laya`'s own fallback.
    """
    bridge: WebviewBridge | None = getattr(request.app.state, "bridge", None)
    if bridge is None:
        bridge = WebviewBridge()
        request.app.state.bridge = bridge
    return bridge


@app.get("/bridge/state")
async def bridge_state(request: Request) -> dict[str, Any]:
    """Whether a shell is polling, and what is in flight.

    Read-only and cheap, and it exists so the shell can decide whether to
    start polling at all rather than discovering on its first long poll that
    the engine it just restarted is not the one it thought.
    """
    return _bridge(request).state()


@app.get("/bridge/next")
async def bridge_next(
    request: Request,
    wait: float = Query(20.0, ge=0.0, le=60.0),
) -> dict[str, Any]:
    """The oldest unanswered page question, or `{"id": null}`.

    A long poll rather than a WebSocket, deliberately. Every socket in this
    engine carries one goal's events and nothing else, and a second long-lived
    socket on the same paths is how two conversations end up interleaved
    (see `tests/stream_isolation.py`). This route is a request with a reply,
    so it cannot outlive its caller or land a frame on anybody's stream.

    Calling it is also the liveness signal: a poll that arrives refreshes the
    bridge's idea of when a shell was last seen, and one that never arrives
    again is how the engine learns the desktop app is gone.
    """
    pending = await _bridge(request).next_request(wait_s=wait)
    if pending is None:
        return {"id": None}
    return pending


@app.post("/bridge/answer")
async def bridge_answer(request: Request, body: BridgeAnswer) -> dict[str, Any]:
    """Land one page answer on the question it belongs to.

    `accepted: false` is the ordinary answer to a late or forged one, not an
    error: the id is checked against the questions actually outstanding, and a
    page is free to fetch the reply URL as many times as it likes.
    """
    bridge = _bridge(request)
    payload = body.result if body.ok else {"error": body.error or "no reason given"}
    accepted = bridge.answer(body.id, body.ok, payload)
    return {"accepted": accepted}


@app.get("/settings/keys")
async def get_keys(request: Request) -> list[dict[str, Any]]:
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
async def save_key(body: ProviderKeyUpdate, request: Request) -> dict[str, Any]:
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
async def list_agents(request: Request) -> list[AgentConfig]:
    registry: AgentRegistryService = request.app.state.registry
    return registry.list_configs()


@app.get("/settings/agents/stats")
async def agent_call_stats(request: Request, limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
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

    stats: dict[str, dict[str, Any]] = {
        role: {
            "role": role,
            "last_call": None,
            "calls_seen": 0,
            "failures_seen": 0,
            "last_error": None,
        }
        for role in ROLES
    }

    def _load(raw: Any) -> dict[str, Any]:
        try:
            parsed: dict[str, Any] = json.loads(raw or "{}")
            return parsed
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

    # How often the role did its job, over every run the log still holds — not
    # over the same bounded scan above, which is about "what happened last" and
    # would make a rate depend on how many events happen to fit in the limit.
    # The rate is about the ROLE (a verifier that reported `fail` worked), and
    # it rides on this card because that is where someone decides whether to
    # re-prompt a role. Null when the role has never finished a run, so a card
    # for an unused role reads as unknown rather than as broken.
    try:
        measured = await _sweep_metrics(conn)
        outcomes = await asyncio.to_thread(role_success_rate, measured, now=time.time())
    except Exception:
        outcomes = {}
    for role in ROLES:
        measured_role: dict[str, Any] | None = outcomes.get(role)
        if not measured_role:
            continue
        stats[role]["runs"] = measured_role["runs"]
        stats[role]["success_rate"] = measured_role["success_rate"]
        stats[role]["outcomes"] = measured_role["outcomes"]
        stats[role]["tokens"] = measured_role["tokens"]

    return {
        "stats": [{**stats[role], **outcomes.get(role, {})} for role in ROLES],
        "scanned_events": len(rows),
    }


@app.post("/settings/agents/repair")
async def repair_agents(request: Request) -> dict[str, Any]:
    """Point the roles that cannot run at a model this engine has discovered.

    One action, and deliberately not "apply one model to every role": a role that
    works is left exactly as it is. The rule lives in `engine/role_repair.py` and
    the response carries the reason for every change *and* every refusal to change,
    so the screen can show what happened instead of asserting success.
    """
    registry: AgentRegistryService = request.app.state.registry
    catalog_service: ModelCatalogService | None = getattr(request.app.state, "models", None)

    # The same rows and the same key table the goal preflight reads (`AgentRegistryService`): built-in
    # providers, custom ones a role points at, and each role's own credential. Two hand-built copies of
    # "does this role have a key" is how this endpoint came to repoint working custom-provider roles.
    configs = registry.configs_with_key_state()
    key_status = [{"provider": slug, **status} for slug, status in registry.provider_key_status().items()]
    catalog = await catalog_service.get(refresh=True) if catalog_service else {
        "models": [], "providers": []
    }

    plan = plan_role_repair(
        configs=configs,
        key_status=key_status,
        provider_status=catalog.get("providers") or [],
        catalog=catalog.get("models") or [],
    )

    repaired: list[dict[str, Any]] = []
    # `plan.changed` already means the target is set (see the property in
    # `engine/role_repair.py`); this says so where the target is dereferenced.
    if plan.changed and plan.target is not None:
        for role, reason in plan.to_repair:
            patch: dict[str, Any] = {"provider": plan.target["provider"], "model_name": plan.target["model"]}
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
async def list_roles(request: Request) -> list[dict[str, Any]]:
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
async def get_agent(role: str, request: Request) -> AgentConfig:
    registry: AgentRegistryService = request.app.state.registry
    return registry.get_config(role)


@app.put("/settings/agents/{role}", response_model=AgentConfig)
async def put_agent(role: str, patch: AgentConfigUpdate, request: Request) -> AgentConfig:
    registry: AgentRegistryService = request.app.state.registry
    updated = registry.set_config(role, patch)
    # The role may now point at a different provider/endpoint, which changes
    # which models are discoverable at all.
    catalog: ModelCatalogService | None = getattr(request.app.state, "models", None)
    if catalog:
        catalog.invalidate()
    return updated


@app.post("/settings/agents/{role}/test-connection")
async def test_agent(role: str, request: Request) -> dict[str, Any]:
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
async def create_ws(body: WorkspaceCreate, request: Request) -> Workspace:
    workspaces: WorkspaceService = request.app.state.workspaces
    return workspaces.create(body)


# Inside the picker's own `-c` source, so the process can be told apart from every
# other `python3 -c` on the box: a `-c` process advertises no file path, only its
# source, and `pgrep -f` reads exactly that (unquoted) out of /proc/<pid>/cmdline.
PICKER_MARKER = "codify-folder-picker"


def _picker_command() -> tuple[list[str], dict[str, str]]:
    """The guarded argv and environment the folder picker runs under.

    The GTK dialog is deliberately a subprocess — GTK is never imported into the
    engine, and a dialog that outlives its request is not the engine's memory to
    hold — but a subprocess outliving the *engine* is exactly the stray the guard
    exists for (see engine/spawn_guard.py): a native dialog lives until a human
    chooses, and a closed window used to leave it running against an engine that
    is gone. The timeout still bounds the engine-alive case; the guard covers the
    one the timeout cannot see. The environment is inherited unchanged (the guard
    only adds the pid handover) because DISPLAY/WAYLAND are what put the dialog on
    the user's screen. Its own function so tests can spawn the exact command the
    route runs without opening GTK.
    """
    # Exit codes are the protocol (`PICKER_UNAVAILABLE` / `PICKER_NO_DISPLAY` below): 0 with a path on
    # stdout is a choice, 0 with nothing is the person closing the dialog, and anything else is a
    # dialog that could not open. The route used to read every nonzero exit as "cancelled".
    code = f"""
# {PICKER_MARKER}
import sys
try:
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk
except Exception as exc:
    sys.stderr.write('codify-picker:unavailable: PyGObject with GTK 3 is not importable (%s)\\n' % exc)
    sys.exit({PICKER_UNAVAILABLE})
initialised = Gtk.init_check()
if not (initialised[0] if isinstance(initialised, tuple) else initialised):
    sys.stderr.write('codify-picker:no-display: GTK could not open a display\\n')
    sys.exit({PICKER_NO_DISPLAY})
dialog = Gtk.FileChooserNative.new('Select Workspace Directory', None, Gtk.FileChooserAction.SELECT_FOLDER, '_Select', '_Cancel')
res = dialog.run()
if res == Gtk.ResponseType.ACCEPT:
    print(dialog.get_filename())
dialog.destroy()
while Gtk.events_pending():
    Gtk.main_iteration_do(False)
"""
    return (
        guarded_argv([shutil.which("python3") or sys.executable, "-c", code]),
        guarded_env(),
    )


# What the GTK script exits with when it cannot show a dialog (see `_picker_command`).
PICKER_UNAVAILABLE = 3
PICKER_NO_DISPLAY = 4
# A dialog waits for a person, so this is long — but it is a bound: a dialog nobody answers is killed,
# and everything it started with it, instead of being left open on a screen.
PICKER_TIMEOUT_S = 120


class _Picked:
    """One attempt at a folder dialog: what it says, and whether the next one should be tried."""

    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind  # "chosen" | "cancelled" | "unavailable" | "failed" | "timeout"
        self.detail = detail


def _run_picker(argv: list[str], env: dict[str, str], *, cancel_codes: tuple[int, ...] = ()) -> _Picked:
    """Run one dialog under the guard, in a session of its own, and classify how it ended."""
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv is a fixed dialog command, wrapped by guarded_argv right here; no shell
            # Guarded here, at the spawn, whoever built the argv: a caller that already did is not doubled
            # (`guarded_argv` is idempotent) and one that forgot cannot start an unguarded dialog.
            guarded_argv(argv),
            env=guarded_env(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            # The caller's half of the guard contract (see spawn_guard.py): the guard must lead the
            # session — and so the group — it kills.
            start_new_session=True,
        )
    except OSError as exc:
        return _Picked("unavailable", f"the dialog could not be started: {exc.strerror or exc}")
    try:
        out, err = proc.communicate(timeout=PICKER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        SandboxService._kill_group(proc.pid, sig=signal.SIGKILL)
        proc.communicate()
        return _Picked("timeout", f"the folder dialog did not answer within {PICKER_TIMEOUT_S} s and was closed")
    chosen = out.strip()
    if proc.returncode == 0:
        return _Picked("chosen", chosen) if chosen else _Picked("cancelled")
    reason = " ".join(err.strip().split())[-300:]
    if proc.returncode in (PICKER_UNAVAILABLE, PICKER_NO_DISPLAY):
        return _Picked("unavailable", reason.split(": ", 1)[-1] if reason else "the dialog could not open")
    if proc.returncode in cancel_codes and "display" not in err.lower():
        # zenity and kdialog exit 1 for Cancel — and zenity exits 1 for "cannot open display" too,
        # which only its stderr distinguishes.
        return _Picked("cancelled")
    return _Picked("failed", reason or f"the dialog exited with status {proc.returncode}")


def _fallback_pickers() -> list[tuple[str, list[str]]]:
    """Desktop dialogs to try when the GTK script cannot run: `zenity` (GNOME and most others), then `kdialog`.

    A machine with a desktop and a Python that has no PyGObject — every venv, conda or pyenv — is the
    common case, and these are what it usually does have.
    """
    found: list[tuple[str, list[str]]] = []
    zenity = shutil.which("zenity")
    if zenity:
        found.append(("zenity", [zenity, "--file-selection", "--directory", "--title=Select Workspace Directory"]))
    kdialog = shutil.which("kdialog")
    if kdialog:
        found.append(("kdialog", [kdialog, "--getexistingdirectory", str(Path.home()), "--title", "Select Workspace Directory"]))
    return found


@app.post("/workspaces/browse")
async def browse_workspace(request: Request) -> dict[str, Any]:
    def _pick() -> _Picked:
        argv, env = _picker_command()
        first = _run_picker(argv, env)
        if first.kind in ("chosen", "cancelled", "timeout"):
            return first
        # The GTK script could not show a dialog (or died): what else does this machine have?
        problems = [f"the GTK dialog: {first.detail}"]
        for name, command in _fallback_pickers():
            attempt = _run_picker(command, dict(os.environ), cancel_codes=(1,))
            if attempt.kind in ("chosen", "cancelled", "timeout"):
                return attempt
            problems.append(f"{name}: {attempt.detail}")
        return _Picked("unavailable", "; ".join(problems))

    outcome = await asyncio.to_thread(_pick)
    if outcome.kind == "cancelled":
        return {"cancelled": True}
    if outcome.kind != "chosen":
        if outcome.kind == "timeout":
            message = outcome.detail
        else:
            message = (
                f"No folder dialog could be opened ({outcome.detail}). Type the folder's path instead, or "
                "install one of: zenity, kdialog, or PyGObject for the Python that runs the engine."
            )
        raise ApiError(503, "picker_unavailable", message)
    path = outcome.detail

    folder_name = Path(path).name or path
    ws_service: WorkspaceService = request.app.state.workspaces
    for ws in ws_service.list_workspaces():
        if ws.root_path == path:
            return {"cancelled": False, "workspace": ws.model_dump()}

    new_ws = ws_service.create(WorkspaceCreate(name=folder_name, root_path=path))
    return {"cancelled": False, "workspace": new_ws.model_dump()}


@app.get("/models")
async def list_available_models(request: Request, refresh: bool = False) -> dict[str, Any]:
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
async def recent_models(request: Request, limit: int = Query(5, ge=1, le=25)) -> list[dict[str, Any]]:
    """Models that actually answered recently, newest first, from the event log.

    What the chat's model menu orders by. It is deliberately *not* derived from
    `goals.provider/model`, which record what the command bar asked for: roles run
    on their own configured models, so the two differ whenever a role is configured
    and the intent is stale. A menu that labelled one of those "last run" would be
    naming a model the engine never called.
    """
    goals: GoalService = request.app.state.goals
    return goals.recent_run_models(limit)


# The engine-wide settings, and the only place a key's shape is declared.
#
# Both tables exist so `GET` and `PUT` cannot disagree about what exists: the
# read is built by iterating them and the write accepts the same keys, so adding
# a setting to one and forgetting the other is a diff rather than a setting
# nobody can change. The bands mirror `SettingsService.SPEC`, which owns the
# clamp; these are what the UI is told so it can validate before saving instead
# of discovering a clamp afterwards.
ENGINE_INT_SETTINGS: dict[str, tuple[int, int]] = {
    "parallel_width": (1, 16),
    "stats_retention_days": (0, 730),
    "trace_retention_days": (0, 730),
    "conductor_max_turns": (1, 40),
    "conductor_max_moves": (0, 60),
    # A switch rather than a number a user has to know the meaning of, stored as
    # the 0/1 the engine already reads.
    "conductor_drives_execution": (0, 1),
    # Read each answer aloud as it arrives (Settings → Audio). Off by default: a
    # voice that starts talking unasked is not something a fresh install does.
    "tts_auto_read": (0, 1),
}

# The integer settings that are really switches, so a checkbox's `true` is a 1
# rather than the truthiness trap it would be for a number someone chooses.
ENGINE_SWITCH_SETTINGS = frozenset({"conductor_drives_execution", "tts_auto_read"})

# The conductor's own provider and model, keyed to the longest value each
# accepts. They are settings rather than an `AgentConfig` row because
# docs/00 §6.1 fixes `AgentRole` at eight and a row in `agent_configs` *is* a
# role (see `SettingsService.STRING_SPEC` for the same argument on that side).
ENGINE_STRING_SETTINGS: dict[str, int] = {
    "conductor_provider": 64,
    "conductor_model": 128,
    "conductor_fallback_provider": 64,
    "conductor_fallback_model": 128,
    # Voice (Settings → Audio, engine/speech.py): which provider and model turn
    # speech into text and back, the voice that reads, and the microphone. Engine
    # settings rather than a role for the conductor's reason: neither is a stage.
    "stt_provider": 64,
    "stt_model": 128,
    "stt_language": 16,
    "tts_provider": 64,
    "tts_model": 128,
    "tts_voice": 64,
    # A PipeWire node name; empty is the session's default source.
    "audio_input": 200,
    # A custom speech provider's own address (a local speech server, say), so it
    # needs no agent role to define it. Built-in providers ignore it.
    "stt_base_url": 500,
    "tts_base_url": 500,
}

# The slug shape `AgentConfigUpdate` enforces, so a conductor pointed at a
# provider that cannot exist is refused here for the same reason it would be
# there — and an empty string is not a slug, because empty is how the setting is
# cleared back to "borrow the scribe's row".
PROVIDER_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Which of the string settings name a provider rather than free text. Named
# rather than inferred from the key, so a setting added later does not inherit a
# validation rule it never agreed to.
SLUG_SETTINGS = frozenset({
    "conductor_provider", "conductor_fallback_provider", "stt_provider", "tts_provider",
})

# Which of them are a server's address: http(s) with a host, or empty to clear it.
# Whether a *key* may go there is decided per request (`key_destination_problem`),
# because the key and the address can be saved in either order.
URL_SETTINGS = frozenset({"stt_base_url", "tts_base_url"})


@app.get("/settings/engine")
async def get_engine_settings(request: Request) -> dict[str, Any]:
    """The engine-wide settings screen values, each with its clamp bounds so the
    UI can validate before saving instead of discovering a clamp after."""
    settings: SettingsService = request.app.state.settings
    out: dict[str, Any] = {
        key: {"value": settings.get_int(key), "min": low, "max": high}
        for key, (low, high) in ENGINE_INT_SETTINGS.items()
    }
    out.update({
        # A string setting has no band to clamp into, so it carries the length it
        # accepts instead — the one bound a free-text field can be wrong about.
        key: {"value": settings.get_str(key), "max": limit}
        for key, limit in ENGINE_STRING_SETTINGS.items()
    })
    return out


def _clean_engine_string(key: str, value: Any) -> str:
    """One string setting, stripped, bounded, and slug-checked where it is a slug.

    Split out of the handler because the rules are per-key and the loop below
    should stay a list of the keys it accepts.
    """
    if not isinstance(value, str):
        raise ApiError(422, "invalid_value", f"{key} must be a string")
    text = value.strip()
    limit = ENGINE_STRING_SETTINGS[key]
    if len(text) > limit:
        raise ApiError(
            422, "invalid_value", f"{key} must be at most {limit} characters"
        )
    if key in SLUG_SETTINGS and text and not PROVIDER_SLUG_RE.match(text):
        raise ApiError(422, "invalid_value", f"{key} is not a provider slug")
    if key in URL_SETTINGS and text:
        parsed = urlparse(text)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ApiError(422, "invalid_value", f"{key} must be an http(s) address, such as http://127.0.0.1:8000/v1")
    return text


@app.put("/settings/engine")
async def put_engine_settings(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Persist engine-wide settings. Only known keys are accepted; each clamps
    to its band, and the response echoes what was actually stored so the UI
    shows the truth rather than what the user typed.

    A conductor provider saved without a model is stored rather than refused,
    because "clear it" has to be one call and a provider on its own has a
    meaning already: the executor reads half a pair as "not configured" and
    borrows the scribe's row (`ExecutorService._conductor_config`).
    """
    settings: SettingsService = request.app.state.settings
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key in ENGINE_INT_SETTINGS:
            # A boolean is refused for a number the user is *choosing*: JSON's
            # `true` reaching `int()` is a truthiness trap, not a 1. The 0/1
            # switches are the exception — a checkbox genuinely sends one, and
            # their clamp turns anything truthy into 1.
            if isinstance(value, bool) and key not in ENGINE_SWITCH_SETTINGS:
                raise ApiError(422, "invalid_value", f"{key} must be an integer, not a boolean")
            try:
                out[key] = settings.set_int(key, int(value))
            except (TypeError, ValueError) as err:
                raise ApiError(422, "invalid_value", f"{key} must be an integer") from err
        elif key in ENGINE_STRING_SETTINGS:
            out[key] = settings.set_str(key, _clean_engine_string(key, value))
        else:
            raise ApiError(400, "unknown_setting", f"unknown engine setting: {key}")
    return {"saved": out}


# ── voice (engine/speech.py, docs/04) ─────────────────────────────────────────


def _recorder(app: FastAPI) -> speech.Recorder:
    """The engine's one recorder, made on first use so a test that builds `app.state` by hand has one."""
    recorder: speech.Recorder | None = getattr(app.state, "recorder", None)
    if recorder is None:
        recorder = speech.Recorder()
        app.state.recorder = recorder
    return recorder


def _speech_target(request: Request, what: str) -> speech.SpeechTarget:
    settings: SettingsService = request.app.state.settings
    keychain: Keychain = getattr(request.app.state, "keychain", None) or Keychain()
    return speech.resolve(
        request.app.state.registry, keychain,
        settings.get_str(f"{what}_provider"), settings.get_str(f"{what}_model"), what=what,
        base_url=settings.get_str(f"{what}_base_url"),
    )


@app.get("/audio/status")
async def audio_status(request: Request) -> dict[str, Any]:
    """Whether dictation and read-aloud can run now (and if not, why), and whether a mic can be recorded."""
    settings: SettingsService = request.app.state.settings
    keychain: Keychain = getattr(request.app.state, "keychain", None) or Keychain()
    available, reason = speech.Recorder.available()
    return {
        "dictation": speech.configured(settings, request.app.state.registry, keychain, "stt"),
        "read_aloud": speech.configured(settings, request.app.state.registry, keychain, "tts"),
        "recorder": {
            "available": available,
            "reason": reason,
            "recording": _recorder(request.app).recording(),
            "max_seconds": speech.MAX_DICTATION_S,
        },
        "auto_read": bool(settings.get_int("tts_auto_read")),
    }


@app.get("/audio/inputs")
async def audio_inputs() -> dict[str, Any]:
    """The microphones PipeWire knows. Off the loop: it asks a subprocess."""
    return await asyncio.to_thread(speech.inputs)


@app.post("/audio/dictation/start")
async def start_dictation(request: Request) -> dict[str, Any]:
    """Start recording the microphone for dictation.

    Refused before anything is spawned when dictation has nowhere to go: a recording that could
    only ever be thrown away is a microphone opened for nothing.
    """
    _speech_target(request, "stt")
    settings: SettingsService = request.app.state.settings
    return await asyncio.to_thread(_recorder(request.app).start, settings.get_str("audio_input"))


@app.post("/audio/dictation/stop")
async def stop_dictation(request: Request) -> dict[str, Any]:
    """Stop recording and answer with what was said. The recording is deleted either way."""
    wav, seconds = await asyncio.to_thread(_recorder(request.app).stop)
    target = _speech_target(request, "stt")
    settings: SettingsService = request.app.state.settings
    try:
        text = await speech.transcribe(
            target, wav, settings.get_str("stt_language"),
            transport=getattr(request.app.state, "speech_transport", None),
        )
    except ProviderError as exc:
        raise ApiError(502, exc.code, exc.message) from exc
    return {"text": text, "seconds": round(seconds, 1)}


@app.post("/audio/dictation/cancel")
async def cancel_dictation(request: Request) -> dict[str, Any]:
    """End and delete a recording without sending it anywhere."""
    return {"cancelled": await asyncio.to_thread(_recorder(request.app).cancel)}


@app.post("/audio/speak")
async def speak(body: SpeakRequest, request: Request) -> Response:
    """Read `text` aloud: WAV audio from the read-aloud provider, straight back, never stored."""
    text = body.text.strip()
    if not text:
        raise ApiError(400, "text_empty", "there is nothing to read aloud")
    if len(text) > speech.MAX_SPEAK_CHARS:
        raise ApiError(
            400, "text_too_long", f"read-aloud takes at most {speech.MAX_SPEAK_CHARS} characters at a time",
        )
    target = _speech_target(request, "tts")
    settings: SettingsService = request.app.state.settings
    voice = settings.get_str("tts_voice")
    if not voice:
        raise ApiError(409, "tts_not_configured", "read-aloud has no voice yet: name one in Settings → Audio")
    try:
        audio = await speech.synthesize(
            target, voice, text, transport=getattr(request.app.state, "speech_transport", None),
        )
    except ProviderError as exc:
        raise ApiError(502, exc.code, exc.message) from exc
    return Response(content=audio, media_type="audio/wav")


@app.get("/workspaces")
async def list_ws(request: Request) -> list[Workspace]:
    workspaces: WorkspaceService = request.app.state.workspaces
    return workspaces.list_workspaces()


@app.get("/workspaces/{workspace_id}")
async def get_ws(workspace_id: str, request: Request) -> Workspace:
    workspaces: WorkspaceService = request.app.state.workspaces
    return workspaces.get(workspace_id)


@app.put("/workspaces/{workspace_id}/design-contract", response_model=Workspace)
async def put_workspace_design_contract(
    workspace_id: str, body: WorkspaceDesignContract, request: Request
) -> Workspace:
    """Pin the brand contract the design agent must obey (`""` clears it).

    A workspace property rather than an agent one: the right `DESIGN.md` is a
    fact about the repository, so two workspaces pointing at different repos can
    hold different brand contracts while the design role keeps one config.
    """
    workspaces: WorkspaceService = request.app.state.workspaces
    return workspaces.set_design_contract(workspace_id, body.path)


@app.delete("/workspaces/{workspace_id}")
async def delete_ws(
    workspace_id: str,
    request: Request,
    delete_goals: bool = False,
) -> dict[str, Any]:
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
    workspaces: WorkspaceService = request.app.state.workspaces
    return workspaces.delete(workspace_id, delete_goals=delete_goals)


@app.post("/conversations", response_model=Conversation)
async def create_conversation(body: ConversationCreate, request: Request) -> Conversation:
    """Start a thread of turns in a workspace.

    The unit a tab points at. It is not a goal: a goal is one run with a plan and
    a verifier, and a conversation is the question several runs answer. Held here
    rather than in the client so a thread outlives the window that opened it —
    the defect this replaces was a transcript in React state that vanished on
    reload.
    """
    conversations: ConversationService = request.app.state.conversations
    return conversations.create(body)


@app.get("/conversations", response_model=list[Conversation])
async def list_conversations(
    request: Request,
    workspace_id: str | None = None,
    include_archived: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[Conversation]:
    """Threads, most recently touched first, for the side panel.

    Archived threads are hidden unless asked for: a tab list is what the user is
    working on, and burying it under every finished thread is the same mistake
    `list_goals` avoids by leading with active goals.
    """
    conversations: ConversationService = request.app.state.conversations
    return conversations.list_conversations(
        workspace_id=workspace_id,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )


@app.get("/conversations/{conversation_id}", response_model=Conversation)
async def get_conversation(conversation_id: str, request: Request) -> Conversation:
    conversations: ConversationService = request.app.state.conversations
    return conversations.get(conversation_id)


@app.get("/conversations/{conversation_id}/turns", response_model=list[ConversationTurn])
async def list_conversation_turns(
    conversation_id: str, request: Request
) -> list[ConversationTurn]:
    """The thread's turns, oldest first.

    Derived from the goals that answer them rather than stored separately, so
    there is no second record to get out of step. This is what a tab loads to
    rebuild its transcript after a restart.
    """
    conversations: ConversationService = request.app.state.conversations
    return conversations.turns(conversation_id)


@app.post("/conversations/{conversation_id}/turns", response_model=Goal)
async def create_conversation_turn(
    conversation_id: str, body: TurnCreate, request: Request
) -> Goal:
    """Say something in a thread. The route a chat actually posts to.

    Until this existed, every message in this app was a `POST /goals`, so
    typing "hi" started a librarian, a designer, a planner and a fixer and
    produced a plan for a greeting. The gate had already classified the request
    as a question — `LayaDecision.intent` has been computed on every goal since
    the gate existed and was only ever used to append a warning — so the fix is
    not a new model path but honouring a signal the engine was already
    producing. See docs/09 §10.

    The response is a `Goal` because a turn *is* one, with `mode="chat"` and no
    steps ever: `events.goal_id` is NOT NULL, so the event log is the WebSocket,
    the audit trail and the stats feed, and a turn stored anywhere else would
    have nowhere to write a single streamed token. Reusing the row is what buys
    the client all of that for free. Clients read `mode` to tell a turn from a
    run; nothing else about the shape differs.
    """
    goals: GoalService = request.app.state.goals
    # 404 on an unknown thread, before anything is written.
    request.app.state.conversations.get(conversation_id)
    goal = goals.create_turn(conversation_id, body)
    _spawn(request.app, request.app.state.executor.run_chat(goal.id), goal.id)
    return goal


@app.patch("/conversations/{conversation_id}", response_model=Conversation)
async def rename_conversation(
    conversation_id: str, body: ConversationUpdate, request: Request
) -> Conversation:
    """Rename a thread. The only mutable thing about one.

    `ConversationUpdate` forbids extra fields, so a body that tried to set
    `archived` or `workspace_id` here is a 422 rather than a silent rewrite — the
    same reasoning as invariant 2 on `POST /goals`.
    """
    conversations: ConversationService = request.app.state.conversations
    return conversations.rename(conversation_id, body)


@app.post("/conversations/{conversation_id}/archive", response_model=Conversation)
async def archive_conversation(
    conversation_id: str,
    request: Request,
    archived: bool = True,
) -> Conversation:
    """Archive or restore a thread. Archived rather than deleted, because the
    runs it holds still belong to it and the history is not the user's to lose.
    """
    conversations: ConversationService = request.app.state.conversations
    return conversations.set_archived(conversation_id, archived)


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str, request: Request
) -> dict[str, Any]:
    """Drop the thread and keep its runs.

    Goals reference a conversation `ON DELETE SET NULL`, so every run it held
    survives as a single-turn thread in the history. Deleting a tab is not a way
    to delete an audit trail.
    """
    conversations: ConversationService = request.app.state.conversations
    return conversations.delete(conversation_id)


@app.get("/shell/tabs", response_model=list[ShellTab])
async def list_shell_tabs(request: Request) -> list[ShellTab]:
    """The open tabs, in the order they are read.

    The read half of a shared strip, and the reason a second window opens onto
    the same tabs rather than onto whatever its own profile happened to hold.
    It is also how a window learns what *another* window did: the two do not
    wait for each other, they meet here and in the response to a write.
    """
    shell_tabs: ShellTabService = request.app.state.shell_tabs
    return shell_tabs.list_tabs()


@app.put("/shell/tabs", response_model=list[ShellTab])
async def upsert_shell_tab(
    body: ShellTabWrite, request: Request
) -> list[ShellTab]:
    """Record one tab — opened, navigated, retitled — and return the strip.

    One tab per call rather than the whole layout, because the whole layout is
    the thing two windows disagree about until they sync. A per-tab write is
    the unit two writers can both be right about: the tab I changed, at the
    place I put it, and nothing of anybody else's.

    The response is the merged strip, so the caller's own write and everything
    else it had not seen arrive together.
    """
    shell_tabs: ShellTabService = request.app.state.shell_tabs
    return shell_tabs.upsert(body)


@app.delete("/shell/tabs/{key}", response_model=list[ShellTab])
async def delete_shell_tab(key: str, request: Request) -> list[ShellTab]:
    """Close a tab for every window, and return the strip that is left.

    Shared-strip semantics, which is the whole reason this is not a per-window
    preference: a tab closed in one window is closed in all of them, and the
    windows that had it showing fall back to a neighbour the way a closed tab
    does in a single window. `localStorage` mirrors this so a window that was
    offline when the tab closed removes it when it next talks to the engine.
    """
    shell_tabs: ShellTabService = request.app.state.shell_tabs
    return shell_tabs.remove(key)


@app.put("/goals/{goal_id}/conversation", response_model=Goal)
async def attach_goal_conversation(
    goal_id: str, body: GoalConversationUpdate, request: Request
) -> Goal:
    """Put an existing goal into a thread.

    The other half of `POST /goals`: a goal created before conversations (or
    restored from history) has no thread, and until this route the link was the
    panel's rather than the store's — one restart later the run read as its own
    thread again (docs/09 §6). The link is checked through the same code that
    checks it at creation: unknown goal 404, unknown thread 404, cross-workspace
    422.
    """
    goals: GoalService = request.app.state.goals
    return goals.attach_conversation(goal_id, body)


@app.post("/goals")
async def create_goal(body: GoalCreate, request: Request) -> Goal:
    goals: GoalService = request.app.state.goals
    goal = goals.create(body)
    _spawn(request.app, request.app.state.executor.run_planning(goal.id), goal.id)
    return goal


@app.get("/goals")
async def list_goals(
    request: Request,
    workspace_id: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[Goal]:
    """Goal history, newest first (active goals lead, so a just-dispatched goal
    never sinks under a wall of finished runs).

    The one read the docs' data model always implied and the API never shipped:
    every goal is persisted, but with no list route the only survivors of an app
    restart were the goals still open in a browser tab. `GET /goals/{id}` plus
    `GET /goals/{id}/events?after=0` is the restore path — this route is how a
    client finds out which ids to restore.
    """
    goals: GoalService = request.app.state.goals
    return goals.list_goals(
        workspace_id=workspace_id, status=status, limit=limit, offset=offset
    )


@app.get("/goals/{goal_id}")
async def get_goal(goal_id: str, request: Request) -> GoalDetail:
    g = request.app.state.goals.get(goal_id)
    return GoalDetail(**g.model_dump(), steps=request.app.state.goals.steps(goal_id))


def _parallel_peak_from_events(events: list[Any]) -> dict[str, Any]:
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


def _usage_from_events(events: list[Any]) -> dict[str, Any]:
    """Token totals from the usage events the providers report.

    Providers carry usage fields in every response; the orchestrator records
    them as events so totals come free from the same store that feeds the
    timeline. Per-role and per-model splits show which agent (or which
    fallback target) is actually spending the tokens. Shared by the usage
    endpoint and the audit export so the two can never disagree.
    """
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    by_role: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
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
async def get_goal_usage(goal_id: str, request: Request) -> dict[str, Any]:
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


def _silent_roles(
    status: str, events: list[Any], usage: dict[str, Any],
) -> list[dict[str, Any]]:
    """Roles the engine announced but that never spent anything.

    A role is announced by `agent_assigned` — the engine decided it should run
    — and a role that runs calls a model, which is a `usage` event. A gap
    between the two is worth surfacing: it usually means the role was skipped
    by a guard, its calls failed silently, or a fallback absorbed its work.

    Two things are deliberately *not* silence:

    * A role the engine never assigned (the Laya gate deciding to skip itself)
      is a deliberate no.
    * A goal still in flight gets no verdict at all. Its roles may simply not
      have had their turn, and calling that "silent" would cry wolf on every
      healthy in-progress run. Only terminal goals are judged.

    This is why the gate's model call has to be booked like every other role's.
    The gate publishes `agent_assigned` and then, for the SDK, spends nothing —
    it runs in-process, with no provider and no tokens. Judged on spend alone,
    a gate that correctly answered `allow` was reported as a role that never
    ran, on every goal, on every install.

    So "did it run" is asked twice, and a role has to fail both to be called
    silent: it spent nothing *and* the engine never recorded a stage outcome
    saying it did its job. That second question is the same one the Stats screen
    asks when it scores a role, read from the same table, so the audit and the
    stats cannot disagree about whether a stage ran.
    """
    if status not in ("COMPLETED", "FAILED", "CANCELLED"):
        return []
    assigned: dict[str, str] = {}
    for e in events:
        # The conductor's own announcement names the role whose *configuration* it borrows (the
        # scribe's), and its calls are booked as `conductor`. Counting it as the scribe being assigned
        # flagged `scribe` as silent on every turn the conductor drove.
        if (e.payload or {}).get("conductor"):
            continue
        if e.type == "agent_assigned" and (e.payload or {}).get("role"):
            role = e.payload["role"]
            model = f"{e.payload.get('provider') or '?'}/{e.payload.get('model') or '?'}"
            # Last assignment wins: a role re-assigned after a retry carries its
            # latest configuration.
            assigned[role] = model
    spent = set(usage.get("by_role", {}))
    # A stage that recorded an outcome the engine counts as the role doing its
    # job ran, whatever it cost. The gate under the SDK is the case that needs
    # this: it answers in-process, so it books no tokens and would otherwise be
    # reported as a role that never ran.
    achieved = {
        str((e.payload or {}).get("role"))
        for e in events
        if e.type == "stage_result"
        and str((e.payload or {}).get("outcome")) in STAGE_SUCCESS_OUTCOMES.get(
            str((e.payload or {}).get("role")), frozenset()
        )
    }
    return [
        {"role": role, "assigned_model": assigned[role]}
        for role in sorted(assigned)
        if role not in spent and role not in achieved
    ]


@app.get("/goals/{goal_id}/audit")
async def get_goal_audit(goal_id: str, request: Request) -> dict[str, Any]:
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
    step_outcomes: dict[str, dict[str, Any]] = {}
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
    silent_roles = _silent_roles(goal.status, events, usage)

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


def _load_stats(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The synchronous read behind `_sweep_stats`; it runs on a worker thread, never the loop's."""
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


async def _sweep_stats(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The raw material the stats views aggregate: goal rows and parsed call
    events. One loader for both the live overview and the snapshot writer, so
    the two can never read different worlds."""
    return await _read_off_the_loop(conn, _load_stats)


_T = TypeVar("_T")


def _store_file(conn: sqlite3.Connection) -> str | None:
    """The file behind `conn`, or None for a store that is not one (an in-memory database)."""
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main":
            return str(row["file"]) or None
    return None


async def _read_off_the_loop(conn: sqlite3.Connection, load: Callable[[sqlite3.Connection], _T]) -> _T:
    """Run a read on a worker thread, over a second connection that can only read.

    A stats sweep is a query, a JSON parse of up to 20,000 rows and some arithmetic, about 145 ms at the
    cap (`docs/03` §3 4.3), and on the loop's own thread that is 145 ms in which nothing else runs: every
    goal streaming at the moment stalls with it. The engine's connection stays where it is, because it
    is the writer and its transactions are the loop's business; WAL lets a second connection read while
    it writes, and `query_only` makes "read" a fact about the connection rather than a habit of the
    caller. It is opened inside the worker and closed there, so nothing is left for a garbage collector
    to find (Python 3.13 warns about a connection it has to close for you).

    A store with no file cannot be opened twice, so it is read where it is, on the caller's thread.
    """
    path = _store_file(conn)
    if path is None:
        return load(conn)

    def run() -> _T:
        reader = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        try:
            reader.row_factory = sqlite3.Row
            reader.execute("PRAGMA query_only = ON")
            return load(reader)
        finally:
            reader.close()

    return await asyncio.to_thread(run)


# The event types the stage/role metrics are computed from. A separate loader
# from `_sweep_stats` on purpose: that one feeds the frozen daily snapshot,
# whose document shape is already stored per day, while these are the
# measurement events (what each stage achieved, and what failed). Rewriting the
# snapshot's inputs would silently change what a stored day means.

# Upper bound on events one stats request reads; see `_sweep_metrics`.
_METRIC_SWEEP_LIMIT = 20000

_METRIC_EVENT_TYPES = (
    "usage", "agent_call_failed", "stage_result", "error",
    "fix_retry", "test_result", "step_status",
)


def _metric_sweep_sql() -> str:
    """The query behind `_sweep_metrics`, in one place so a test can plan the real one.

    The *newest* `_METRIC_SWEEP_LIMIT` rows, returned oldest-first. The bound has
    to cut from the old end: an `ORDER BY timestamp LIMIT n` keeps the first n
    ever written, so once the table outgrew the limit every view built on this
    froze on ancient history and never threw. The outer sort puts them back in
    the chronological order `recovery_counts` depends on. The inner
    `ORDER BY timestamp DESC, sequence DESC` is what `idx_events_time` serves.
    """
    placeholders = ",".join("?" for _ in _METRIC_EVENT_TYPES)
    return f"""SELECT * FROM (
                SELECT type, payload, timestamp, goal_id, step_id, sequence FROM events
                WHERE type IN ({placeholders})
                ORDER BY timestamp DESC, sequence DESC LIMIT {_METRIC_SWEEP_LIMIT}
            ) ORDER BY timestamp, sequence"""  # noqa: S608 — placeholders and a module constant only


def _load_metrics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The synchronous read behind `_sweep_metrics`; it runs on a worker thread, never the loop's."""
    rows = conn.execute(_metric_sweep_sql(), _METRIC_EVENT_TYPES).fetchall()
    parsed = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        parsed.append({
            "type": row["type"],
            "payload": payload,
            "timestamp": row["timestamp"],
            "goal_id": row["goal_id"],
            "step_id": row["step_id"],
            "sequence": row["sequence"],
        })
    return parsed


async def _sweep_metrics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every measurement event, with the ids recovery counting needs.

    `goal_id`/`step_id` are included because "did the retry get the step past
    its failure" is a question about one step of one goal; the per-goal
    aggregation that answers it cannot group on a list that has thrown the ids
    away.
    """
    return await _read_off_the_loop(conn, _load_metrics)


@app.get("/stats/overview")
async def stats_overview(request: Request, window: int = Query(0)) -> dict[str, Any]:
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

    # Recordings are the other thing that grows without a policy (docs/04
    # §8). Pruned at the same moment and on the same terms as the snapshot
    # above: on read, so lowering it takes effect now rather than at some next
    # run of a job that may not happen, and swallowed, because forgetting a
    # recording must never be able to fail the read that asked for one.
    try:
        traces_pruned: TraceService = request.app.state.traces
        traces_pruned.delete_older_than(
            request.app.state.settings.get_int("trace_retention_days")
        )
    except Exception:
        pass

    window_days = normalize_window(window)
    overview = await asyncio.to_thread(build_overview, goals, parsed, window_days=window_days, now=now)
    # The per-stage and per-role view (docs/04 §4.7). Read from a second sweep
    # over the measurement events, and failing that read is not allowed to take
    # the overview down with it: the goal-level numbers above are the ones
    # people have relied on longest, and a metrics problem should degrade this
    # view rather than remove the other.
    try:
        measured = await _sweep_metrics(conn)
        stages = await asyncio.to_thread(stage_costs, measured, window_days, now=now)
        roles = await asyncio.to_thread(role_success_rate, measured, window_days, now=now)
    except Exception:
        stages, roles = [], {}
    return {
        **overview,
        "by_stage": stages,
        "by_role_outcome": roles,
        "generated_at": now,
    }


@app.get("/stats/failures")
async def stats_failures(request: Request, window: int = Query(0)) -> dict[str, Any]:
    """What went wrong across every goal: by cause, by role, by stage.

    The aggregation lives in `engine/metrics.py` as a pure function, for the
    same reason `engine/stats.py` does: the numbers are the product here, so
    they are tested directly rather than through HTTP.

    Unlike the overview, a failure view over an install that has never failed
    is *empty*, and it says so: `total: 0` with no rows, never a table of
    zeros that reads as "nothing is going wrong" when the truth is "there is
    nothing to know yet". The recovery rate is null for the same reason — no
    retries is not a perfect record, it is no evidence.
    """
    conn = request.app.state.conn
    window_days = normalize_window(window)
    now = time.time()
    events = await _sweep_metrics(conn)
    breakdown = await asyncio.to_thread(failure_breakdown, events, window_days, now=now)
    return {
        **breakdown,
        "window_days": window_days,
        "generated_at": now,
    }


@app.get("/stats/history")
async def stats_history(request: Request, limit: int = Query(120, ge=0, le=730)) -> dict[str, Any]:
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
async def get_stats_import(request: Request) -> dict[str, Any]:
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
async def post_stats_import(body: dict[str, Any], request: Request) -> dict[str, Any]:
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
async def delete_stats_import(request: Request) -> dict[str, Any]:
    """Forget the current import. Idempotent: clearing an empty import is fine."""
    service: StatsImportService = request.app.state.stats_imports
    return {"imported": False, "cleared": service.clear()}


@app.patch("/goals/{goal_id}/steps/{step_id}")
async def patch_step(goal_id: str, step_id: str, body: PlanStepUpdate, request: Request) -> PlanStep:
    """Edit a plan step's title/description/paths before execution.

    Version-protected like start/pause: expected_version is the goal version
    the client last saw, so concurrent edits can't silently clobber each other.
    """
    patch = body.model_dump(exclude={"expected_version"})
    goals: GoalService = request.app.state.goals
    return goals.update_step(goal_id, step_id, body.expected_version, patch)


@app.post("/goals/{goal_id}/steps/{step_id}/retry")
async def retry_step(goal_id: str, step_id: str, body: VersionedAction, request: Request) -> PlanStep:
    g = request.app.state.goals.get(goal_id)
    if g.status not in ("RUNNING", "PAUSED", "FAILED"):
        raise ApiError(409, "illegal_status", f"cannot retry from {g.status}")
    executor: ExecutorService = request.app.state.executor
    # Everything that can be refused is decided here, and the driver is claimed here; the step
    # itself runs in the background and the stream reports how it went. The request used to
    # await the whole step, holding the connection for minutes with no driver claimed.
    updated_step = executor.begin_retry(
        goal_id, step_id, body.expected_version,
        # The critic's notes are what a conductor acts on when it takes the step again, so a retry it will
        # drive keeps them. The recipe's fixer never reads them, and for it they would only sit on the timeline.
        keep_notes=executor.conductor_can_drive(goal_id),
    )
    _spawn(request.app, _retry_and_drive(request.app, goal_id, step_id), goal_id)
    return updated_step


@app.get("/goals/{goal_id}/trace")
async def get_goal_trace(goal_id: str, request: Request) -> dict[str, Any]:
    """What this goal recorded: every model call, in order, without the bodies.

    A summary rather than the recording itself. The digests are enough to
    answer the question the summary exists for ("which call is the one that
    went wrong, and was it the same prompt the recording holds?") and asking
    for a body is a deliberate second step — a recording is a copy of model
    output about the user's code, and the route that returns it all should not
    be the one a panel opens on load.
    """
    request.app.state.goals.get(goal_id)  # 404 if unknown
    traces: TraceService = request.app.state.traces
    return traces.summary(goal_id)


@app.delete("/goals/{goal_id}/trace")
async def delete_goal_trace(goal_id: str, request: Request) -> dict[str, Any]:
    """Forget a goal's recording. Idempotent, and always allowed.

    Deleting is never gated on the goal's status: a recording of a finished run
    is exactly what someone wants gone, and making them cancel or wait for a
    retry to be able to remove a copy of their own code would be a strange
    thing to enforce.
    """
    request.app.state.goals.get(goal_id)  # 404 if unknown
    traces: TraceService = request.app.state.traces
    return {"deleted": traces.delete(goal_id)}


@app.put("/goals/{goal_id}/trace")
async def put_goal_trace(goal_id: str, body: TraceToggle, request: Request) -> Goal:
    """Record this goal's model calls, or stop (docs/04 §8).

    Only while the goal is PLANNING: a recording that starts halfway is a trace
    of half a run, and the replay it implies would be missing the calls that
    shaped the first half. Turning it *off* works at any time, because deleting
    what was recorded is the user's call rather than a state change.
    """
    goals: GoalService = request.app.state.goals
    return goals.set_trace(goal_id, body.enabled)


@app.post("/goals/{goal_id}/pause")
async def pause_goal(goal_id: str, body: VersionedAction, request: Request) -> Goal:
    g = request.app.state.goals.get(goal_id)
    if g.status != "RUNNING":
        raise ApiError(409, "illegal_status", f"cannot pause from {g.status}")
    goals: GoalService = request.app.state.goals
    return goals.update_status(goal_id, body.expected_version, "PAUSED")


@app.post("/goals/{goal_id}/cancel")
async def cancel_goal(goal_id: str, body: VersionedAction, request: Request) -> Goal:
    g = request.app.state.goals.get(goal_id)
    # PLANNING is cancellable: planning runs in the background, and a goal stuck
    # there (slow planner, or one orphaned before the boot rescue existed) could
    # otherwise be neither started nor stopped. CANCELLED from PLANNING also
    # beats the planning coroutine's own `_set_status(PENDING)` — the runner
    # checks status before that transition and leaves a cancelled goal alone.
    if g.status not in ("PLANNING", "RUNNING", "PAUSED", "PENDING"):
        raise ApiError(409, "illegal_status", f"cannot cancel from {g.status}")
    goals: GoalService = request.app.state.goals
    return goals.update_status(goal_id, body.expected_version, "CANCELLED")


@app.delete("/goals/{goal_id}")
async def delete_goal(goal_id: str, request: Request) -> dict[str, Any]:
    """Delete a goal and everything recorded about it.

    The event log, plan steps, and dry-run proposals cascade with it. The
    response counts them so the UI can state what went rather than shrugging
    with "deleted".

    Refused while the goal is PLANNING or RUNNING, and re-checked here against
    the executor's live driver set: a coroutine that outlives its row would keep
    publishing events for a goal that no longer exists. The user cancels first,
    which is also the only honest way to stop work already touching files.
    """
    goals: GoalService = request.app.state.goals
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
async def goal_events(goal_id: str, request: Request, after: int = Query(0)) -> list[Event]:
    goals: GoalService = request.app.state.goals
    goals.get(goal_id)
    return goals.events_after(goal_id, after)


@app.post("/goals/{goal_id}/start")
async def start_goal(goal_id: str, body: VersionedAction, request: Request) -> Goal:
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
    executor = getattr(request.app.state, "executor", None)
    if executor is not None and executor.is_driving(goal_id):
        # A turn's `plan` move leaves the goal PENDING while the turn is still running, and a second driver
        # started now would write the same steps as the first (review of 2026-09-29, finding 2).
        raise ApiError(
            409, "driver_busy",
            "this goal is still being worked on — start it once the turn has finished",
            {"goal_id": goal_id, "status": g.status},
        )
    goals: GoalService = request.app.state.goals
    running = goals.update_status(goal_id, body.expected_version, "RUNNING")
    _spawn(request.app, _run_steps(request.app, goal_id), goal_id)
    return running


@app.post("/goals/{goal_id}/apply")
async def apply_goal(goal_id: str, body: VersionedAction, request: Request) -> dict[str, Any]:
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
async def enable_execution(goal_id: str, body: VersionedAction, request: Request) -> Goal:
    """Lift the plan-only guard on a goal, leaving it ready to start.

    plan_only itself is not version-protected (it is a pre-execution toggle,
    like dry_run), but expected_version is still validated so a client cannot
    enable execution based on a stale view of the goal.
    """
    goals: GoalService = request.app.state.goals
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
    executor: ExecutorService = request.app.state.executor
    executor._set_status(goal_id, "PENDING", None)
    return goals.get(goal_id)


def _spawn(
    app: FastAPI, coro: Coroutine[Any, Any, Any], goal_id: str | None = None
) -> None:
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

    task = asyncio.create_task(runner())
    # The event loop keeps only a weak reference to a task, so one that nothing else holds can
    # be collected while it is still running (a documented CPython hazard) — a goal's driver
    # vanishing mid-step with no error at all. Held here until it is done.
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


async def _retry_and_drive(app: FastAPI, goal_id: str, step_id: str) -> None:
    """Run the retried step, then drive whatever is left, holding the driver `begin_retry` claimed."""
    executor = app.state.executor
    try:
        if executor.conductor_can_drive(goal_id):
            # The conductor owns every step it is given, a retried one included: it resumes at the step the
            # person named and carries on through whatever is still open, pausing the goal with a reason if it
            # cannot finish one. Running the recipe's `run_step` here would be the second pipeline the
            # conductor is supposed to have replaced.
            await executor.run_conductor_resume(goal_id, focus_step_id=step_id)
        else:
            await executor.run_step(goal_id, step_id)
            # Only a goal still RUNNING is driven further: a retried step that failed, or a Cancel that
            # landed during it, ends here (`_run_steps_locked` checks).
            await _run_steps_locked(app, goal_id)
    finally:
        executor.release_driver(goal_id)


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
    # Only a RUNNING goal is driven. The retry route spawns this after the retried step has
    # finished, and a Cancel that landed during that step used to be followed by a full
    # conductor run — model spend on a goal the user had stopped.
    if app.state.goals.get(goal_id).status != "RUNNING":
        return
    # An approved plan is driven by the conductor when this install has one, and
    # by the engine's own sequence when it does not. The choice lives here rather
    # than in the route because the executor is the only side that can see
    # whether a tool-capable model is actually configured — and the fallback is
    # what lets the conductor be the default without a machine losing its ability
    # to execute a plan at all.
    if executor.conductor_can_drive(goal_id):
        # And that is the end of the engine's part. The conductor completes every step it is given, or
        # pauses the goal with a reason and the person's Start picks it up. There used to be a sweep here that
        # ran any step left open through the fixed recipe, from scratch: a conductor that wrote a step and
        # ran out of calls before `summarize` had the fixer run on it again. The recipe is still how a plan
        # is driven when this install has no tool-capable model, for `parallel` goals, and through
        # `POST /goals`; it is no longer a second driver behind the first.
        await executor.run_conductor_resume(goal_id)
        return
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


@app.websocket("/ws/engine")
async def ws_engine(websocket: WebSocket) -> None:
    """Engine-level frames: today, one — the model catalogue moved.

    A sibling of `/ws/goals/{id}`, not an extension of it. A catalogue belongs to
    no goal, and a frame with no `goal_id` and no `sequence` in a durable,
    sequenced, per-goal log is the cross-stream contamination
    `tests/stream_isolation.py` exists to prevent — so this channel carries its
    own small union (`engine/catalog_watch.EngineEventType`) and the goal event
    union is untouched.

    Auth is byte for byte `/ws/goals/{id}`'s: a boot token on the Upgrade, or the
    same `{"type": "auth"}` first frame for a browser, which cannot set headers
    on a WebSocket. 4401 for a bad token, invariant 3 (docs/00 §6.3) holding on
    a socket exactly as it does on every route.

    The read loop exists only to notice the peer leaving. An idle engine-level
    connection sends nothing, and a client that sends nothing is how a dead
    socket gets noticed at all.
    """
    expected_token = getattr(websocket.app.state, "token", None) or BOOT_TOKEN
    auth_header = websocket.headers.get("authorization", "")
    authenticated = _same_secret(auth_header, f"Bearer {expected_token}")

    await websocket.accept()

    if not authenticated:
        try:
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            auth_msg = json.loads(msg)
            token = str(auth_msg.get("token") or "")
            if auth_msg.get("type") == "auth" and _same_secret(token, expected_token):
                authenticated = True
        except Exception:
            pass

    if not authenticated:
        await _close_quietly(websocket, 4401)
        return

    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=8)
    conns: set[asyncio.Queue[str]] = websocket.app.state.engine_conns
    conns.add(queue)
    watch: CatalogWatch = websocket.app.state.catalog_watch
    # Subscribing is what makes the engine ask the providers at all, so it is
    # paired with the unsubscribe in the `finally` rather than left to the socket
    # closing: a client that vanished without a close frame is exactly the case
    # that would otherwise leave the engine polling on.
    watch.subscribe()
    # Named outside the loop so the `finally` can reach whichever pair is live: a handler cancelled while it
    # waits (the server stopping, a test client leaving) is not on the path that cleans them, and the two
    # tasks would run on with nobody to read their result.
    getter: asyncio.Task[str] | None = None
    receiver: asyncio.Task[str] | None = None
    try:
        while True:
            getter = asyncio.create_task(queue.get())
            receiver = asyncio.create_task(websocket.receive_text())
            done, pending = await asyncio.wait(
                {getter, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            # Then wait for the cancelled ones, so whatever they raised is
            # retrieved. A `receive` cancelled while a frame is being sent ends in
            # "cannot receive once a disconnect message has been received", and an
            # unretrieved task exception is printed to stderr by the event loop —
            # on every frame, forever. `return_exceptions` also cannot swallow
            # *this* coroutine's cancellation: a cancel delivered here propagates
            # out of the gather rather than being collected as a result.
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if getter in done:
                await websocket.send_text(getter.result())
                continue
            # The receive side finished first. That is either a client frame this
            # channel has no use for, or the peer going away — and the two must
            # not be confused: calling `receive` again on a socket that has already
            # delivered its disconnect raises, so a loop that treated both as
            # "ignore it and carry on" would spin on the exception forever instead
            # of noticing the client left.
            if receiver.cancelled() or receiver.exception() is not None:
                break
            # A client frame. Ignored — the channel is one-directional — and the
            # loop continues, because a client that can send one can still receive.
            continue
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            # Whatever is still running is cancelled, and everything is awaited so its outcome is
            # retrieved (an unretrieved `WebSocketDisconnect` is printed by the event loop, once per
            # connection, for a client that merely left).
            children = [t for t in (getter, receiver) if t is not None]
            for child in children:
                if not child.done():
                    child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
        finally:
            # Last, and unconditionally: this is what the watch loop gates provider traffic on, and it
            # is also the moment a caller can rely on this handler having nothing left to do.
            watch.unsubscribe()
            conns.discard(queue)


async def _close_quietly(websocket: WebSocket, code: int) -> None:
    """Close a socket whose peer may already be gone, without a traceback for a client that merely left.

    Closing a WebSocket the peer has hung up on raises (`RuntimeError` from Starlette's state check, or
    `WebSocketDisconnect`), and an exception escaping a handler is a traceback in the engine's log for
    someone who just closed a tab. The refusal stands either way; there is only nobody left to hear it.
    """
    try:
        await websocket.close(code=code)
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.websocket("/ws/goals/{goal_id}")
async def ws_goal(websocket: WebSocket, goal_id: str) -> None:
    expected_token = getattr(websocket.app.state, "token", None) or BOOT_TOKEN
    auth_header = websocket.headers.get("authorization", "")
    authenticated = _same_secret(auth_header, f"Bearer {expected_token}")

    await websocket.accept()

    if not authenticated:
        try:
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            auth_msg = json.loads(msg)
            token = str(auth_msg.get("token") or "")
            if auth_msg.get("type") == "auth" and _same_secret(token, expected_token):
                authenticated = True
        except Exception:
            pass

    if not authenticated:
        await _close_quietly(websocket, 4401)
        return

    # Authenticated, but the goal must exist too — checking only after auth so
    # an unauthenticated peer can't probe goal ids by watching which close code
    # comes back (4401 before auth, 4404 after).
    try:
        websocket.app.state.goals.get(goal_id)
    except ApiError:
        await _close_quietly(websocket, 4404)
        return

    # The socket is read as well as written, and that is what ends this handler. It used to only
    # write, so a client that had gone was noticed only when a `send` failed — and a goal with no
    # new events never sends. The UI closes the socket itself on every terminal status, so every
    # finished goal that was ever viewed left a coroutine waking four times a second to re-read the
    # goal for nobody (idle engine CPU 0.2 % -> 13.9 % of a core over 500 views, until restart).
    # `ws_engine` reads its socket for the same reason.
    receiver = asyncio.create_task(websocket.receive_text())
    after = 0
    try:
        while True:
            try:
                websocket.app.state.goals.get(goal_id)
            except ApiError:
                await _close_quietly(websocket, 4404)
                return
            # `limit` rather than `[...][:500]`: the slice happened *after* the
            # read, so every tick parsed the goal's whole remaining log to send
            # at most 500 of it. A deleted goal is caught by the `get` above on
            # the next tick, which is 250 ms away — the counter that used to
            # stand here counted misses and then reset itself without ever
            # changing what the loop did.
            batch = websocket.app.state.goals.events_after(goal_id, after, limit=500)
            for event in batch:
                await websocket.send_text(event.model_dump_json())
                after = event.sequence
            # The wait for the next tick is a wait on the receiver, so a peer leaving ends the loop
            # at once instead of at the next failed send.
            done, _ = await asyncio.wait({receiver}, timeout=0.25)
            if not done:
                continue
            if receiver.cancelled() or receiver.exception() is not None:
                # The peer went away (a disconnect surfaces as an exception from `receive`; calling
                # it again on a socket that has already delivered one raises, so do not).
                return
            # A frame from the client: this channel is one-directional and has no use for it, but a
            # client that can send one can still receive, so read on.
            receiver = asyncio.create_task(websocket.receive_text())
    except WebSocketDisconnect:
        pass
    finally:
        # Retrieved either way: an unretrieved exception from a cancelled `receive` is printed by
        # the event loop, once per connection, for a client that merely left.
        if not receiver.done():
            receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)


def serve() -> None:
    """Bind, announce, and run until the server stops. Returns; never exits.

    **Why this is not `main()`.** The process entry point has to leave the way
    the deadline does — `os._exit`, because interpreter finalization joins
    threads nobody can cancel, and that join is where an orphan engine came
    from. That is correct for a process and fatal for a test: calling `main()`
    in-process took the *test runner* down with it, silently, at exit 0, roughly
    halfway through the suite. `make` saw a clean exit and reported success while
    549 of 1054 tests had never run, so a broken engine could not be caught by a
    green gate — the gate was the thing that was broken.

    So the guarantee lives on the entry point and the work lives here. A test can
    boot the real server without ending the world, and `tests/test_home.py` says
    so where it calls it.
    """
    import uvicorn

    port = pick_port()
    # The state store is opened — and closed — *before* anything announces readiness. It used to be opened
    # by uvicorn's startup, after the handshake: a corrupt database meant the engine said "ready" and then
    # failed its own startup and exited, and a shell that read the handshake connected to a socket nobody
    # was going to serve. Opened with the keychain's callback, so a role migration still carries its key.
    try:
        connect(on_role_migrated=Keychain().rename_role_key).close()
    except sqlite3.Error as exc:
        print(f"engine: cannot open the state store at {home.db_path()}: {exc}", file=sys.stderr, flush=True)
        raise
    # Where this run's state actually lands, before anything can write. An isolated
    # run says so out loud, and the half-redirected case is called out instead of
    # being discovered later by finding a smoke-test key in a real keychain.
    # stderr, because the Tauri shell parses stdout for the boot handshake.
    print(home.startup_notice(), file=sys.stderr, flush=True)
    # Which interpreter this actually is, and whether the gate's SDK is importable
    # *by it*. A capability installed into a different environment than the one the
    # shell spawned fails silently otherwise — see engine/capabilities.py.
    for line in capabilities.startup_lines():
        print(line, file=sys.stderr, flush=True)
    # Armed before the port is claimed: a shell that dies while this engine is
    # binding must not be the reason the next launch finds the port taken. Inert
    # unless the desktop shell set `CODIFY_PARENT_PID` — see engine/watchdog.py.
    # The handler is the pipe-safe one: after a hard shell death the stderr pipe
    # below this process may have no reader, and a handler that prints before it
    # acts dies on its own log line (`arm_death_signal` explains that order).
    watchdog.start_parent_watchdog(on_parent_death=watchdog.on_parent_gone)
    # Bind and LISTEN before announcing readiness. The handshake is the boot
    # contract: whoever reads `CODIFY_ENGINE token=… port=…` is promised a
    # connectable socket, and announcing before `listen()` left a window where
    # that promise was false — the desktop shell read "ready", connected, and
    # hit connection-refused (also the source of a 1-in-5 flake in the
    # wire-level stream tests). uvicorn serves the pre-bound socket, so the
    # port is owned by this process end to end.
    sock, port = _bind_listening(port)
    # `hard_exit_s` is announced rather than left for the shell to know: this is
    # the engine's own bound on how long a stop request can take, and the shell
    # waits exactly that long before escalating to SIGKILL (src-tauri/src/lib.rs).
    # A copy of the number in the shell would be a second source of truth for a
    # deadline whose whole job is to be longer than the work it cuts off — and
    # the failure mode of a stale copy is the one this handshake exists to stop.
    print(
        f"CODIFY_ENGINE token={BOOT_TOKEN} port={port} "
        f"hard_exit_s={watchdog.HARD_EXIT_GRACE_S:g}",
        flush=True,
    )
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        # A stop request has to finish in bounded time. uvicorn's default here is
        # "wait for in-flight work, forever", and this engine's in-flight work is a
        # turn against a model — minutes of it, on a local one — or an event stream
        # the window holds open. That default is how a closed app left an engine
        # behind with its port already released and the database still held.
        timeout_graceful_shutdown=watchdog.GRACEFUL_SHUTDOWN_S,
    )
    # `capture_signals` installs `self.handle_exit` for SIGTERM/SIGINT when `run`
    # starts, so the method is the whole hook — overriding it is what puts the
    # deadline on the signal path as well as on the parent-death path. The override
    # *wraps*: the graceful shutdown is still uvicorn's, with a bound behind it.
    class BoundedShutdown(uvicorn.Server):
        def handle_exit(self, sig: int, frame: FrameType | None) -> None:
            watchdog.arm_hard_deadline()
            super().handle_exit(sig, frame)

    server = BoundedShutdown(config)
    server.run(sockets=[sock])
    # Reached only when no signal arrived — a captured one is re-raised on the way out
    # of `capture_signals`, which ends the process inside `run`. Everything the engine
    # has to say or write has been said, and what is left is interpreter finalization,
    # which waits on threads that cannot be cancelled; `main` leaves the way the
    # deadline does, and this function simply returns.


def main() -> None:
    """The process entry point: serve, then leave the way the deadline does.

    Kept separate from `serve()` on purpose, and the separation is a test —
    `test_main_leaves_the_way_the_deadline_does` runs this in a child process and
    fails if the hard exit is ever dropped, because dropping it looks like a fix
    for the truncation above and is in fact the orphan bug `hard_exit` documents.
    """
    serve()
    watchdog.hard_exit()


if __name__ == "__main__":
    main()
